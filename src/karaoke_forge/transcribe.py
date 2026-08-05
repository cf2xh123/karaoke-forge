from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .align import RecognizedWord
from .models import LyricsDocument
from .text import split_display_units


class TranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class TranscriptionResult:
    words: list[RecognizedWord]
    detected_language: str | None
    language_probability: float | None


@dataclass(frozen=True)
class ForcedLineAlignment:
    line_index: int
    words: tuple[RecognizedWord, ...]
    mean_confidence: float


@dataclass(frozen=True)
class ForcedAlignmentResult:
    lines: tuple[ForcedLineAlignment, ...]
    attempted_lines: int
    aligned_lines: int
    skipped_lines: int


def _is_model_cache_miss(exc: Exception) -> bool:
    """Return whether loading failed because the requested model is not cached."""

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in {
            "IncompleteSnapshotError",
            "LocalEntryNotFoundError",
        }:
            return True
        detail = str(current).lower()
        if any(
            marker in detail
            for marker in (
                "cached snapshot folder",
                "cached snapshot is incomplete",
                "local disk and outgoing traffic",
                "model.bin",
            )
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def _exception_chain_text(exc: Exception) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    return " | ".join(parts)


def _local_only_option_is_unsupported(exc: Exception) -> bool:
    detail = str(exc).lower()
    return (
        isinstance(exc, TypeError)
        and "local_files_only" in detail
        and ("unexpected keyword" in detail or "keyword argument" in detail)
    )


def _model_load_error(model: str, exc: Exception, *, hub_model: bool) -> str:
    detail = str(exc).strip()
    network_markers = (
        "connecterror",
        "connecttimeout",
        "connection timed out",
        "connection attempt failed",
        "name resolution",
        "network is unreachable",
        "offline mode",
        "proxyerror",
        "readtimeout",
        "无法连接",
        "连接尝试失败",
    )
    chain_text = _exception_chain_text(exc).lower()
    unavailable = hub_model and (
        _is_model_cache_miss(exc) or any(marker in chain_text for marker in network_markers)
    )
    if unavailable:
        return (
            f"无法加载 Whisper 模型“{model}”：本机没有完整缓存，且当前无法连接 "
            "Hugging Face 模型服务。首次使用该模型需要联网下载；请检查网络或代理后重试。"
            "如果歌词已有时间轴，也可将“逐字时间精修”设为“关闭”继续。"
        )
    return f"无法加载 Whisper 模型“{model}”：{detail or type(exc).__name__}"


def load_faster_whisper_model(
    *,
    model: str = "small",
    device: str = "auto",
    compute_type: str = "default",
    progress: Callable[[str], None] | None = None,
) -> object:
    """Load one reusable faster-whisper model with offline-first diagnostics."""

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscriptionError(
            "The alignment engine is not installed. Run "
            '`pip install -e ".[align]"` (or `pip install karaoke-forge[align]`).'
        ) from exc

    expanded_model_path = Path(model).expanduser()
    model_is_local = expanded_model_path.is_dir()
    model_source = str(expanded_model_path.resolve()) if model_is_local else model
    if progress:
        if model_is_local:
            progress(f"正在加载本地 Whisper 模型：{model_source}")
        else:
            progress(f"正在检查 Whisper 模型 {model} 的本地缓存…")
    try:
        # Resolve from the local Hugging Face cache first. Besides making offline
        # use deterministic, this avoids waiting for a Hub request when a complete
        # model snapshot is already available on disk.
        if model_is_local:
            whisper_model = WhisperModel(
                model_source,
                device=device,
                compute_type=compute_type,
            )
        else:
            load_without_local_only = False
            try:
                whisper_model = WhisperModel(
                    model,
                    device=device,
                    compute_type=compute_type,
                    local_files_only=True,
                )
            except Exception as exc:
                if _local_only_option_is_unsupported(exc):
                    load_without_local_only = True
                elif not _is_model_cache_miss(exc):
                    raise
                else:
                    load_without_local_only = True
                    if progress:
                        progress(
                            f"本机未缓存 Whisper {model}，正在联网下载；"
                            "当前下载器无法提供可靠百分比，完成后会自动继续。"
                        )
            # Run the network-capable attempt after leaving the cache-miss
            # exception handler. Otherwise an unrelated second failure inherits
            # the cache miss as __context__ and can be misclassified as a Hub outage.
            if load_without_local_only:
                whisper_model = WhisperModel(model, device=device, compute_type=compute_type)
    except Exception as exc:
        raise TranscriptionError(
            _model_load_error(model, exc, hub_model=not model_is_local)
        ) from exc
    return whisper_model


def transcribe_with_faster_whisper(
    audio_path: str | Path,
    *,
    model: str = "small",
    language: str | None = None,
    device: str = "auto",
    compute_type: str = "default",
    beam_size: int = 5,
    initial_prompt: str | None = None,
    hotwords: str | None = None,
    vad_filter: bool = False,
    progress: Callable[[str], None] | None = None,
    whisper_model: object | None = None,
) -> TranscriptionResult:
    audio = Path(audio_path)
    if not audio.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio}")
    if whisper_model is None:
        whisper_model = load_faster_whisper_model(
            model=model,
            device=device,
            compute_type=compute_type,
            progress=progress,
        )

    if progress:
        progress(f"Whisper 模型 {model} 已就绪，开始识别音频。")

    try:
        transcription_options: dict[str, object] = {
            "language": language,
            "beam_size": beam_size,
            "word_timestamps": True,
            # Speech-oriented VAD can reject long stretches of vocals mixed with
            # dense music. Scan the complete track by default for song alignment.
            "vad_filter": vad_filter,
            # Carrying the previous decoded window can make Whisper repeat a
            # chorus and let later timestamps drift out of sync. The supplied
            # official lyrics remain the stable hint for song alignment.
            "condition_on_previous_text": False,
            "initial_prompt": initial_prompt[:600] if initial_prompt else None,
        }
        if hotwords:
            transcription_options["hotwords"] = hotwords[:400]
        try:
            segments, info = whisper_model.transcribe(str(audio), **transcription_options)
        except TypeError as exc:
            # hotwords was added after the first faster-whisper releases. Keep
            # local installations usable while still applying the safer window
            # conditioning options supported by older versions.
            if "hotwords" not in transcription_options or "hotwords" not in str(exc).lower():
                raise
            fallback_hint = transcription_options.pop("hotwords")
            if not transcription_options.get("initial_prompt"):
                transcription_options["initial_prompt"] = fallback_hint
            segments, info = whisper_model.transcribe(str(audio), **transcription_options)
        words: list[RecognizedWord] = []
        for segment in segments:
            if progress:
                progress(f"正在识别 {segment.start:7.2f}s - {segment.end:7.2f}s")
            for word in segment.words or []:
                if word.start is None or word.end is None:
                    continue
                words.append(
                    RecognizedWord(
                        text=word.word,
                        start=float(word.start),
                        end=float(word.end),
                        confidence=(
                            float(word.probability) if word.probability is not None else None
                        ),
                    )
                )
    except Exception as exc:
        raise TranscriptionError(f"Whisper 转写失败：{exc}") from exc

    if not words:
        raise TranscriptionError("Whisper 没有返回逐字时间；请检查音频，或尝试先分离人声。")
    return TranscriptionResult(
        words=words,
        detected_language=getattr(info, "language", None),
        language_probability=getattr(info, "language_probability", None),
    )


def force_align_lyrics_with_faster_whisper(
    audio_path: str | Path,
    lyrics: LyricsDocument,
    *,
    whisper_model: object,
    language: str | None,
    progress: Callable[[str], None] | None = None,
    window_margin: float = 0.60,
) -> ForcedAlignmentResult:
    """Align the user's exact lyric lines inside short, already-located audio windows."""

    try:
        from faster_whisper.audio import decode_audio, pad_or_trim
        from faster_whisper.tokenizer import Tokenizer
    except ImportError as exc:
        raise TranscriptionError(
            "当前 faster-whisper 安装缺少逐行强制对齐组件，已无法启用 KTV 精准模式。"
        ) from exc

    required_attributes = ("encode", "feature_extractor", "hf_tokenizer", "model")
    missing = [name for name in required_attributes if not hasattr(whisper_model, name)]
    native_model = getattr(whisper_model, "model", None)
    if native_model is None or not callable(getattr(native_model, "align", None)):
        missing.append("model.align")
    if missing:
        raise TranscriptionError(
            "当前 faster-whisper 版本不支持逐行强制对齐（缺少 "
            + "、".join(missing)
            + "）；请更新 faster-whisper。"
        )

    audio_path = Path(audio_path)
    if not audio_path.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    sampling_rate = int(whisper_model.feature_extractor.sampling_rate)
    try:
        audio = decode_audio(str(audio_path), sampling_rate=sampling_rate)
    except Exception as exc:
        raise TranscriptionError(f"读取精准对齐音频失败：{exc}") from exc
    duration = len(audio) / sampling_rate

    language = language or lyrics.metadata.get("language")
    if not language:
        try:
            language = str(whisper_model.detect_language(audio=audio)[0])
        except Exception as exc:
            raise TranscriptionError(f"精准对齐无法确定歌词语言：{exc}") from exc
    try:
        tokenizer = Tokenizer(
            whisper_model.hf_tokenizer,
            whisper_model.model.is_multilingual,
            task="transcribe",
            language=language,
        )
    except Exception as exc:
        raise TranscriptionError(f"精准对齐无法创建 {language} 分词器：{exc}") from exc

    aligned_results: list[ForcedLineAlignment] = []
    attempted_lines = 0
    aligned_lines = 0
    skipped_lines = 0
    visible_lines = [
        (line_index, line)
        for line_index, line in enumerate(lyrics.lines)
        if not line.hidden
    ]
    for visible_index, (line_index, line) in enumerate(visible_lines):
        if not line.text.strip() or line.start is None or line.end is None:
            continue
        attempted_lines += 1
        if line.tokens:
            core_start = line.tokens[0].start
            core_end = line.tokens[-1].end
        else:
            core_start = line.start
            core_end = line.end
        window_start = max(0.0, min(line.start, core_start) - window_margin)
        window_end = min(duration, max(core_end, min(line.end, core_end + 1.5)) + window_margin)
        if visible_index:
            _previous_index, previous_line = visible_lines[visible_index - 1]
            if (
                previous_line.start is not None
                and previous_line.end is not None
                and previous_line.end > line.start + 0.05
            ):
                skipped_lines += 1
                if progress:
                    progress(f"精准对齐保留第 {line_index + 1} 行：检测到重叠演唱行")
                continue
        if visible_index + 1 < len(visible_lines):
            _next_index, next_line = visible_lines[visible_index + 1]
            if (
                next_line.start is not None
                and line.end > next_line.start + 0.05
            ):
                skipped_lines += 1
                if progress:
                    progress(f"精准对齐保留第 {line_index + 1} 行：检测到重叠演唱行")
                continue
            if next_line.start is not None and next_line.start > core_end + 0.10:
                window_end = min(window_end, (core_end + next_line.start) / 2)
        if window_end - window_start < 0.20 or window_end - window_start > 28.0:
            skipped_lines += 1
            if progress:
                progress(
                    f"精准对齐跳过第 {line_index + 1} 行：音频窗口 "
                    f"{window_end - window_start:.2f} 秒不在安全范围内"
                )
            continue

        sample_start = max(0, round(window_start * sampling_rate))
        sample_end = min(len(audio), round(window_end * sampling_rate))
        audio_window = audio[sample_start:sample_end]
        try:
            frame_limit = int(whisper_model.feature_extractor.nb_max_frames)
            # faster-whisper 1.0.x pads every feature window to 30 seconds by
            # default. That silently stretches short lyric lines across long
            # silence, so always retain the real frame count before padding.
            features = whisper_model.feature_extractor(audio_window, padding=False)
            num_frames = min(frame_limit, int(features.shape[-1]))
            if num_frames // 2 <= 7:
                raise ValueError("音频窗口没有可用声学帧")
            features = pad_or_trim(features[..., :num_frames], frame_limit)
            encoded = whisper_model.encode(features)
            text_tokens = tokenizer.encode(line.text.strip())
            maximum_tokens = max(1, int(getattr(whisper_model, "max_length", 448)) - 8)
            if not text_tokens or len(text_tokens) > maximum_tokens:
                raise ValueError(f"歌词 token 数 {len(text_tokens)} 超出单行限制")
            native_result = whisper_model.model.align(
                encoded,
                tokenizer.sot_sequence,
                [text_tokens],
                num_frames,
                median_filter_width=7,
            )[0]
            alignment = _native_alignment_words(
                native_result,
                text_tokens,
                tokenizer=tokenizer,
                tokens_per_second=float(whisper_model.tokens_per_second),
            )
        except Exception as exc:  # noqa: BLE001 - one bad native alignment must not abort the song
            skipped_lines += 1
            if progress:
                progress(f"精准对齐保留第 {line_index + 1} 行原时间：{exc}")
            continue

        line_words: list[RecognizedWord] = []
        for item in alignment:
            text = str(item.get("word", ""))
            if not split_display_units(text):
                continue
            start = window_start + max(0.0, float(item.get("start", 0.0)))
            end = window_start + max(0.0, float(item.get("end", 0.0)))
            if end <= start or start > window_end + 0.05:
                continue
            line_words.append(
                RecognizedWord(
                    text=text,
                    start=start,
                    end=min(window_end, end),
                    confidence=float(item.get("probability", 0.0)),
                )
            )
        if not line_words:
            skipped_lines += 1
            continue
        aligned_lines += 1
        confidences = [
            word.confidence for word in line_words if word.confidence is not None
        ]
        aligned_results.append(
            ForcedLineAlignment(
                line_index=line_index,
                words=tuple(line_words),
                mean_confidence=(
                    sum(confidences) / len(confidences) if confidences else 0.0
                ),
            )
        )
        if progress and (aligned_lines == 1 or aligned_lines % 5 == 0):
            progress(f"KTV 精准对齐：已完成 {aligned_lines}/{attempted_lines} 行")

    if not aligned_results:
        raise TranscriptionError("逐行强制对齐没有产生可用时间，已保留 Whisper 粗对齐结果。")
    return ForcedAlignmentResult(
        lines=tuple(aligned_results),
        attempted_lines=attempted_lines,
        aligned_lines=aligned_lines,
        skipped_lines=skipped_lines,
    )


def _native_alignment_words(
    native_result: object,
    text_tokens: list[int],
    *,
    tokenizer: object,
    tokens_per_second: float,
) -> list[dict[str, object]]:
    """Convert CTranslate2 alignment output without version-specific wrappers."""

    try:
        import numpy as np

        pairs = list(native_result.alignments)
        probabilities = list(native_result.text_token_probs)
        if not pairs or not probabilities or tokens_per_second <= 0:
            return []
        text_indices = np.asarray([pair[0] for pair in pairs], dtype=int)
        time_indices = np.asarray([pair[1] for pair in pairs], dtype=float)
        jumps = np.pad(np.diff(text_indices), (1, 0), constant_values=1).astype(bool)
        jump_times = time_indices[jumps] / tokens_per_second
        words, word_tokens = tokenizer.split_to_word_tokens(
            text_tokens + [tokenizer.eot]
        )
        if len(word_tokens) <= 1:
            return []
        boundaries = np.pad(
            np.cumsum([len(tokens) for tokens in word_tokens[:-1]], dtype=int),
            (1, 0),
        )
        if len(boundaries) <= 1 or boundaries[-1] >= len(jump_times):
            raise ValueError("CTranslate2 返回了不完整的歌词边界")

        converted: list[dict[str, object]] = []
        # split_to_word_tokens includes EOT as its final pseudo-word. Like the
        # upstream implementation, boundaries omit it and zip only real words.
        for word_index, word in enumerate(words[: len(boundaries) - 1]):
            token_start = int(boundaries[word_index])
            token_end = int(boundaries[word_index + 1])
            if token_end <= token_start:
                continue
            probability_slice = probabilities[token_start:token_end]
            converted.append(
                {
                    "word": word,
                    "start": float(jump_times[token_start]),
                    "end": float(jump_times[token_end]),
                    "probability": (
                        float(np.mean(probability_slice)) if probability_slice else 0.0
                    ),
                }
            )
        return converted
    except (AttributeError, IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"无法解析 CTranslate2 逐词对齐结果：{exc}") from exc
