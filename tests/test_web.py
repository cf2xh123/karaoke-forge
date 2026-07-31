from pathlib import Path
from types import SimpleNamespace

from karaoke_forge.netease import NeteaseSongInfo, NeteaseTrack
from karaoke_forge.pronunciation import PronunciationLine, PronunciationUnit
from karaoke_forge.web import (
    _safe_stem,
    apply_editor_line_action,
    environment_markdown,
    export_editor_project,
    handoff_editor_to_make,
    load_editor_project,
    prepare_make_editor_job,
    preview_editor_audio_line,
    run_align_job,
    run_convert_job,
    run_make_job,
    subtitle_preview_html,
    undo_editor_line_action,
)


def test_safe_stem_removes_windows_path_characters() -> None:
    assert _safe_stem("  my:karaoke*video?.mp4  ") == "my-karaoke-video"
    assert _safe_stem("", fallback="song") == "song"


def test_web_convert_job_exports_downloadable_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    source = tmp_path / "lyrics.lrc"
    source.write_text("[00:01.00]Hello world\n", encoding="utf-8")

    result = run_convert_job(str(source), "srt")

    assert result.video is None
    assert len(result.files) == 1
    assert Path(result.files[0]).is_file()
    assert "✅" in result.status


def test_web_align_job_skips_recognition_for_timed_lyrics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"placeholder")
    lyrics = tmp_path / "lyrics.lrc"
    lyrics.write_text("[00:01.00]Hello\n[00:02.00]World\n", encoding="utf-8")

    result = run_align_job(
        str(audio),
        str(lyrics),
        "",
        "demo",
        "en",
        "small",
        "cpu",
        False,
    )

    assert len(result.files) == 6
    assert all(Path(path).is_file() for path in result.files)
    assert "已有时间轴" in result.status


def test_environment_report_mentions_local_processing() -> None:
    report = environment_markdown()
    assert "FFmpeg" in report
    assert "素材不会自动上传到公网" in report


def test_web_editor_loads_and_exports_hidden_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    source = tmp_path / "lyrics.lrc"
    source.write_text(
        "[00:01.00]Credit\n[00:03.00]Keep me\n",
        encoding="utf-8",
    )
    payload, rows, _status, line_number, whole, units, _preview = load_editor_project(str(source))
    rows[0][1] = "隐藏"

    payload, rows, status, files, _directory = export_editor_project(
        payload,
        rows,
        line_number,
        whole,
        units,
        "edited",
    )

    assert "1 行暂时隐藏" in status
    assert len(files) == 6
    lrc = next(Path(path) for path in files if path.endswith(".lrc") and "enhanced" not in path)
    project = next(Path(path) for path in files if path.endswith(".json"))
    assert "Credit" not in lrc.read_text(encoding="utf-8")
    assert '"hidden": true' in project.read_text(encoding="utf-8")


def test_web_editor_all_hidden_still_exports_recoverable_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    source = tmp_path / "lyrics.lrc"
    source.write_text("[00:01.00]Only line\n", encoding="utf-8")
    payload, rows, _status, line_number, whole, units, _preview = load_editor_project(str(source))
    rows[0][1] = "隐藏"

    _payload, _rows, status, files, _directory = export_editor_project(
        payload,
        rows,
        line_number,
        whole,
        units,
        "all-hidden",
    )

    assert len(files) == 1
    assert files[0].endswith(".json")
    assert "只导出了可恢复的 JSON" in status


def test_web_editor_right_click_actions_can_hide_delete_and_undo(
    tmp_path: Path,
) -> None:
    source = tmp_path / "lyrics.lrc"
    source.write_text(
        "[00:01.00]First\n[00:03.00]Second\n[00:05.00]Third\n",
        encoding="utf-8",
    )
    payload, rows, _status, line_number, *_rest = load_editor_project(str(source))

    hidden = apply_editor_line_action(
        payload,
        rows,
        line_number,
        '{"row":0,"action":"toggle-hidden"}',
    )
    assert hidden[1][0][1] == "隐藏"
    assert "已隐藏第 1 行" in hidden[8]

    inserted = apply_editor_line_action(
        hidden[0],
        hidden[1],
        hidden[2],
        '{"row":0,"action":"insert-after"}',
    )
    assert len(inserted[1]) == 4
    assert inserted[1][1][4] == "新歌词"
    assert "下方插入新行" in inserted[8]

    insert_undone = undo_editor_line_action(
        inserted[0],
        inserted[1],
        inserted[2],
        inserted[9],
    )
    assert len(insert_undone[1]) == 3

    deleted = apply_editor_line_action(
        insert_undone[0],
        insert_undone[1],
        insert_undone[2],
        '{"row":1,"action":"delete"}',
    )
    assert len(deleted[1]) == 2
    assert all(row[4] != "Second" for row in deleted[1])
    assert "已删除第 2 行" in deleted[8]

    restored = undo_editor_line_action(
        deleted[0],
        deleted[1],
        deleted[2],
        deleted[9],
    )
    assert len(restored[1]) == 3
    assert restored[1][1][4] == "Second"
    assert "已撤销" in restored[8]


def test_web_editor_previews_selected_line_audio(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setenv("KARAOKE_FORGE_CACHE_DIR", str(tmp_path / "cache"))
    audio = tmp_path / "song.m4a"
    source = tmp_path / "lyrics.lrc"
    audio.write_bytes(b"audio")
    source.write_text("[00:01.00]A\n[00:03.00]B\n", encoding="utf-8")
    payload, rows, *_rest = load_editor_project(str(source))
    monkeypatch.setattr("karaoke_forge.web.shutil.which", lambda _name: "ffmpeg")

    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(b"preview")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("karaoke_forge.web.subprocess.run", fake_run)

    clip, status = preview_editor_audio_line(str(audio), payload, rows, 1)

    assert Path(clip).is_file()
    assert "当前歌词应在 **1.00s → 2.98s**" in status
    assert str(tmp_path / "cache") in clip


def test_web_editor_handoff_populates_make_inputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    audio = tmp_path / "song.m4a"
    source = tmp_path / "lyrics.lrc"
    audio.write_bytes(b"audio")
    source.write_text("[00:01.00]A\n[00:03.00]B\n", encoding="utf-8")
    payload, rows, *_rest = load_editor_project(str(source))

    result = handoff_editor_to_make(
        payload,
        rows,
        1,
        "",
        [],
        "confirmed",
        str(audio),
    )

    status = result[2]
    project = result[5]
    make_audio = result[6]
    assert "已交给“制作卡拉 OK MV”" in status
    assert Path(project).is_file()
    assert project.endswith(".json")
    assert make_audio == str(audio)


def test_make_page_prepares_editor_without_reuploading_inputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    audio = tmp_path / "song.m4a"
    video = tmp_path / "silent-mv.webm"
    audio.write_bytes(b"audio")
    video.write_bytes(b"video")
    info = NeteaseSongInfo(
        song_id="42",
        title="Linked Song",
        artists=("Artist",),
        canonical_url="https://music.163.com/song?id=42",
        page_lyrics="[00:01.00]Hello world\n",
        word_lyrics="[1000,1000](1000,400,0)Hello(1400,600,0) world\n",
        translated_lyrics="[00:01.00]你好世界\n",
    )
    monkeypatch.setattr(
        "karaoke_forge.web.fetch_public_netease_info",
        lambda _link: info,
    )
    monkeypatch.setattr(
        "karaoke_forge.web.generate_pronunciation",
        lambda text: (
            PronunciationLine(
                (PronunciationUnit(source="Hello", reading="ハロー", start=0, end=5),)
            )
            if text.startswith("Hello")
            else None
        ),
    )
    monkeypatch.setattr(
        "karaoke_forge.web.make_karaoke_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("preparing an editor project must not render video")
        ),
    )

    result = prepare_make_editor_job(
        str(audio),
        str(video),
        None,
        "",
        "linked-rehearsal",
        "自动识别",
        "small",
        "auto",
        False,
        info.canonical_url,
        True,
        True,
        "auto",
        str(tmp_path / "custom-output"),
    )

    assert result.project is not None
    assert Path(result.project).is_file()
    assert result.audio == str(audio)
    assert result.rows[0][5] == "你好世界"
    assert result.pronunciation_rows[0][1] == "ハロー"
    assert result.payload["lines"][0]["pronunciation_units"][0]["reading"] == "ハロー"
    assert "无需重复上传" in result.log
    assert "不下载音频" in result.log
    assert "可校准 KTV 工程已生成" in result.status


def test_subtitle_preview_reflects_translation_pronunciation_and_style(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "karaoke_forge.web.generate_pronunciation",
        lambda text: PronunciationLine(
            (PronunciationUnit(source=text, reading="サンプル"),),
        ),
    )
    preview = subtitle_preview_html(
        "Microsoft YaHei",
        64,
        "#FFFFFF",
        "#FFD54A",
        80,
        True,
        36,
        "#EAF4FF",
        True,
        26,
        "#FFFFFF",
        "It's silence\nbeyond this ocean?",
        "花园。",
    )

    assert "Microsoft YaHei" in preview
    assert "#FFD54A" in preview
    assert "花园。" in preview
    assert "It&#x27;s silence" in preview
    assert "beyond " in preview
    assert "this ocean?" in preview
    assert 'data-kf-layout="ktv-split"' in preview
    assert "KTV 双行布局" in preview
    assert "サンプル" in preview


def test_make_job_can_use_netease_page_lyrics_with_local_audio(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    audio = tmp_path / "authorized.flac"
    video = tmp_path / "mv.mp4"
    audio.write_bytes(b"audio")
    video.write_bytes(b"video")
    info = NeteaseSongInfo(
        song_id="42",
        title="Linked Song",
        artists=("Artist",),
        canonical_url="https://music.163.com/song?id=42",
        page_lyrics="[00:01.00]Hello\n[00:02.00]World\n",
    )
    monkeypatch.setattr(
        "karaoke_forge.web.fetch_public_netease_info",
        lambda _link: info,
    )

    def fake_make(
        _audio,
        _video,
        lyrics,
        output,
        assets,
        **_kwargs,
    ):
        assert Path(lyrics).read_text(encoding="utf-8") == info.page_lyrics
        output = Path(output)
        output.write_bytes(b"rendered")
        assets = Path(assets)
        assets.mkdir(parents=True)
        exported = assets / "lyrics.lrc"
        exported.write_text(info.page_lyrics or "", encoding="utf-8")
        return SimpleNamespace(
            video=output,
            exports={"lrc": exported},
            alignment_report=None,
        )

    monkeypatch.setattr("karaoke_forge.web.make_karaoke_video", fake_make)
    result = run_make_job(
        str(audio),
        str(video),
        None,
        "",
        "linked-karaoke",
        "自动识别",
        "small",
        "auto",
        False,
        "快速预览",
        0.0,
        "Microsoft YaHei",
        58,
        "#FFFFFF",
        "#FFD54A",
        72,
        info.canonical_url,
        True,
        True,
    )

    assert result.video is not None
    assert "已生成" in result.status
    assert "仅从网易云读取" in result.log


def test_make_job_falls_back_to_mv_audio_for_netease_preview(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    video = tmp_path / "mv.mp4"
    preview = tmp_path / "preview.mp3"
    video.write_bytes(b"video with complete audio")
    preview.write_bytes(b"30 second preview")
    track = NeteaseTrack(
        song_id="1946664196",
        title="garden.",
        artists=("CVLTE",),
        canonical_url="https://music.163.com/song?id=1946664196",
        audio_path=preview,
        duration=215.0,
        page_lyrics="[00:01.00]Hello\n[00:03.00]World\n",
        translated_lyrics="[00:01.00]你好\n[00:03.00]世界\n",
        audio_duration=30.0,
        is_preview=True,
    )
    monkeypatch.setattr(
        "karaoke_forge.web.download_netease_track",
        lambda *_args, **_kwargs: track,
    )

    def fake_make(audio, _video, lyrics, output, assets, *, options, **_kwargs):
        assert Path(audio) == video
        assert options.auto_sync
        assert options.style.show_translation
        assert options.style.show_pronunciation
        assert '"translation": "你好"' in Path(lyrics).read_text(encoding="utf-8")
        output = Path(output)
        output.write_bytes(b"rendered")
        assets = Path(assets)
        assets.mkdir(parents=True)
        exported = assets / "lyrics.ass"
        exported.write_text("subtitle", encoding="utf-8")
        return SimpleNamespace(
            video=output,
            exports={"ass": exported},
            alignment_report=None,
            sync_result=None,
        )

    monkeypatch.setattr("karaoke_forge.web.make_karaoke_video", fake_make)
    result = run_make_job(
        None,
        str(video),
        None,
        "",
        "garden-karaoke",
        "自动识别",
        "small",
        "auto",
        False,
        "快速预览",
        0.0,
        "Microsoft YaHei",
        58,
        "#FFFFFF",
        "#FFD54A",
        72,
        track.canonical_url,
        True,
        True,
    )

    assert result.video is not None
    assert "MV 内嵌的完整音轨" in result.log
    assert "中文翻译" in result.log
    assert not preview.exists()
