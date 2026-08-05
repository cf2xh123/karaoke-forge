import pytest

from karaoke_forge.align import AlignmentError, RecognizedWord
from karaoke_forge.formats import parse_lrc, parse_yrc, read_lyrics
from karaoke_forge.media import MediaError
from karaoke_forge.pipeline import (
    AlignOptions,
    _build_initial_prompt,
    _build_lyric_hotwords,
    _prepare_alignment_audio,
    align_audio_and_lyrics,
    normalize_timing_refinement,
    refine_audio_word_timing,
    refine_audio_word_timing_with_fallback,
    resolve_align_options,
    should_refine_timing,
)
from karaoke_forge.transcribe import (
    ForcedAlignmentResult,
    ForcedLineAlignment,
    TranscriptionError,
    TranscriptionResult,
)


def test_alignment_profiles_resolve_to_distinct_quality_tiers() -> None:
    fast = resolve_align_options(AlignOptions(model="profile:fast"))
    balanced = resolve_align_options(AlignOptions(model="profile:balanced"))
    precise = resolve_align_options(AlignOptions(model="profile:precise"))

    assert (fast.profile, fast.model, fast.beam_size) == ("fast", "small", 3)
    assert (balanced.profile, balanced.model, balanced.beam_size) == (
        "balanced",
        "large-v3-turbo",
        5,
    )
    assert (precise.profile, precise.model, precise.beam_size) == (
        "precise",
        "large-v3",
        5,
    )
    assert precise.forced_alignment
    assert precise.prefer_vocal_separation
    assert not precise.separate_vocals


def test_precise_pipeline_reuses_one_model_and_falls_back_without_demucs(
    tmp_path,
    monkeypatch,
) -> None:
    audio = tmp_path / "song.wav"
    lyrics = tmp_path / "lyrics.txt"
    audio.write_bytes(b"audio")
    lyrics.write_text("Hello world\n", encoding="utf-8")
    model = object()
    loaded: list[object] = []
    messages: list[str] = []

    monkeypatch.setattr(
        "karaoke_forge.pipeline.inspect_demucs_runtime",
        lambda: type("Runtime", (), {"ready": False})(),
    )
    monkeypatch.setattr(
        "karaoke_forge.pipeline.load_faster_whisper_model",
        lambda **_kwargs: loaded.append(model) or model,
    )

    def fake_transcribe(*_args, **kwargs):
        assert kwargs["whisper_model"] is model
        return TranscriptionResult(
            words=[
                RecognizedWord("Hello", 1.0, 1.4, 0.95),
                RecognizedWord("world", 1.5, 2.0, 0.95),
            ],
            detected_language="en",
            language_probability=0.99,
        )

    def fake_force(*_args, **kwargs):
        assert kwargs["whisper_model"] is model
        return ForcedAlignmentResult(
            lines=(
                ForcedLineAlignment(
                    line_index=0,
                    words=(
                        RecognizedWord("Hello", 1.05, 1.30, 0.95),
                        RecognizedWord("world", 1.42, 1.92, 0.95),
                    ),
                    mean_confidence=0.95,
                ),
            ),
            attempted_lines=1,
            aligned_lines=1,
            skipped_lines=0,
        )

    monkeypatch.setattr(
        "karaoke_forge.pipeline.transcribe_with_faster_whisper",
        fake_transcribe,
    )
    monkeypatch.setattr(
        "karaoke_forge.pipeline.force_align_lyrics_with_faster_whisper",
        fake_force,
    )

    result = align_audio_and_lyrics(
        audio,
        lyrics,
        options=AlignOptions(model="profile:precise"),
        work_dir=tmp_path / "work",
        progress=messages.append,
    )

    assert loaded == [model]
    assert result.alignment_audio == audio
    assert result.document.metadata["alignment_profile"] == "precise"
    assert result.document.metadata["forced_alignment"] == "ctranslate2-line-bounded"
    assert result.report.forced_alignment_accepted_lines == 1
    assert any("未检测到可用 Demucs" in message for message in messages)


def test_explicit_vocal_separation_remains_strict(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio")

    def unavailable(*_args, **_kwargs):
        raise MediaError("Demucs failed")

    monkeypatch.setattr("karaoke_forge.pipeline.separate_vocals", unavailable)

    with pytest.raises(MediaError, match="Demucs failed"):
        _prepare_alignment_audio(
            audio,
            AlignOptions(separate_vocals=True),
            work_dir=tmp_path / "work",
            progress=None,
        )


def test_precise_vocal_preference_falls_back_on_process_os_error(
    tmp_path,
    monkeypatch,
) -> None:
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio")
    messages: list[str] = []
    monkeypatch.setattr(
        "karaoke_forge.pipeline.inspect_demucs_runtime",
        lambda: type("Runtime", (), {"ready": True})(),
    )

    def unavailable(*_args, **_kwargs):
        raise OSError("cannot start demucs")

    monkeypatch.setattr("karaoke_forge.pipeline.separate_vocals", unavailable)

    result = _prepare_alignment_audio(
        audio,
        resolve_align_options(AlignOptions(model="profile:precise")),
        work_dir=tmp_path / "work",
        progress=messages.append,
    )

    assert result == audio
    assert any("改用原音频继续" in message for message in messages)


def test_plain_lyrics_prompt_preserves_stanza_breaks(tmp_path) -> None:
    lyrics_path = tmp_path / "lyrics.txt"
    raw_text = "第一段\n\n第二段\n"
    lyrics_path.write_text(raw_text, encoding="utf-8")

    prompt = _build_initial_prompt(lyrics_path, read_lyrics(lyrics_path))

    assert prompt == raw_text


def test_lyric_hotwords_are_compact_deduplicated_and_cover_the_song() -> None:
    document = parse_lrc(
        "[00:01.00]alpha repeated\n"
        "[00:03.00]中文歌词重复\n"
        "[00:05.00]alpha ending\n"
    )

    hotwords = _build_lyric_hotwords(document)

    assert hotwords.split().count("alpha") == 1
    assert "中文歌词" in hotwords
    assert "ending" in hotwords
    assert len(hotwords) <= 320


def test_lyric_hotwords_keep_the_end_of_a_long_song_within_the_budget() -> None:
    words = [f"word{index:03d}" for index in range(200)]
    document = parse_lrc(f"[00:01.00]{' '.join(words)}\n")

    hotwords = _build_lyric_hotwords(document, limit=320)

    assert words[0] in hotwords.split()
    assert words[-1] in hotwords.split()
    assert len(hotwords) <= 320


def test_timing_refinement_policy_distinguishes_source_and_synthetic() -> None:
    synthetic = parse_lrc("[00:01.00]Hello\n")
    source = parse_yrc("[1000,500](1000,500,0)Hello\n")

    assert not should_refine_timing(synthetic, "off")
    assert should_refine_timing(synthetic, "auto")
    assert not should_refine_timing(source, "auto")
    assert should_refine_timing(source, "force")
    assert normalize_timing_refinement("force") == "force"
    assert normalize_timing_refinement("force", legacy_refine_word_timing=False) == "off"


def test_refinement_preserves_hidden_project_lines(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio")
    document = parse_lrc("[00:01.00]Hidden\n[00:03.00]Visible\n")
    document.lines[0].hidden = True
    monkeypatch.setattr(
        "karaoke_forge.pipeline.transcribe_with_faster_whisper",
        lambda *_args, **_kwargs: TranscriptionResult(
            words=[RecognizedWord("Visible", 3.0, 4.0, 0.9)],
            detected_language="en",
            language_probability=0.99,
        ),
    )

    result = refine_audio_word_timing(audio, document)

    assert result.document.lines[0].hidden
    assert result.document.lines[0].text == "Hidden"
    assert result.document.lines[1].text == "Visible"


def test_auto_refinement_keeps_timed_lyrics_when_whisper_is_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio")
    document = parse_lrc("[00:01.00]Hello\n")
    messages: list[str] = []

    def unavailable(*_args, **_kwargs):
        raise TranscriptionError("model unavailable")

    monkeypatch.setattr(
        "karaoke_forge.pipeline.transcribe_with_faster_whisper",
        unavailable,
    )

    result = refine_audio_word_timing_with_fallback(
        audio,
        document,
        timing_mode="auto",
        progress=messages.append,
    )

    assert result is None
    assert any("已保留原时间轴并继续" in message for message in messages)


def test_auto_refinement_keeps_timed_lyrics_when_coverage_is_low(
    tmp_path,
    monkeypatch,
) -> None:
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio")
    document = parse_lrc("[00:01.00]Hello\n")
    messages: list[str] = []
    monkeypatch.setattr(
        "karaoke_forge.pipeline.refine_audio_word_timing",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AlignmentError("Alignment coverage is only 10.0%")
        ),
    )

    result = refine_audio_word_timing_with_fallback(
        audio,
        document,
        timing_mode="auto",
        progress=messages.append,
    )

    assert result is None
    assert any("覆盖率不足" in message for message in messages)
    assert any("已保留原时间轴并继续" in message for message in messages)


def test_force_refinement_still_raises_when_whisper_is_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio")
    document = parse_lrc("[00:01.00]Hello\n")

    def unavailable(*_args, **_kwargs):
        raise TranscriptionError("model unavailable")

    monkeypatch.setattr(
        "karaoke_forge.pipeline.transcribe_with_faster_whisper",
        unavailable,
    )

    with pytest.raises(TranscriptionError, match="model unavailable"):
        refine_audio_word_timing_with_fallback(
            audio,
            document,
            timing_mode="force",
        )


def test_plain_lyrics_still_require_whisper(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.wav"
    lyrics = tmp_path / "lyrics.txt"
    audio.write_bytes(b"audio")
    lyrics.write_text("Hello\n", encoding="utf-8")

    def unavailable(*_args, **_kwargs):
        raise TranscriptionError("model unavailable")

    monkeypatch.setattr(
        "karaoke_forge.pipeline.transcribe_with_faster_whisper",
        unavailable,
    )

    with pytest.raises(TranscriptionError, match="model unavailable"):
        align_audio_and_lyrics(audio, lyrics)


def test_pipeline_marks_low_coverage_recovery_for_editor_use(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.wav"
    lyrics = tmp_path / "lyrics.txt"
    audio.write_bytes(b"audio")
    lyrics.write_text("First line\nSecond line\n", encoding="utf-8")
    monkeypatch.setattr(
        "karaoke_forge.pipeline.transcribe_with_faster_whisper",
        lambda *_args, **_kwargs: TranscriptionResult(
            words=[RecognizedWord("完全不同", 1.0, 4.0, 0.8)],
            detected_language="zh",
            language_probability=0.95,
        ),
    )

    result = align_audio_and_lyrics(
        audio,
        lyrics,
        options=AlignOptions(recover_low_coverage=True),
    )

    assert result.recovered
    assert result.report.coverage == 0.0
    assert result.document.is_timed
    assert result.document.metadata["alignment_status"] == "low_coverage_recovery"
    assert result.document.metadata["unmatched_lyric_lines"] == "1,2"
