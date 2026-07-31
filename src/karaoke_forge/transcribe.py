from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .align import RecognizedWord


class TranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class TranscriptionResult:
    words: list[RecognizedWord]
    detected_language: str | None
    language_probability: float | None


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


def transcribe_with_faster_whisper(
    audio_path: str | Path,
    *,
    model: str = "small",
    language: str | None = None,
    device: str = "auto",
    compute_type: str = "default",
    beam_size: int = 5,
    initial_prompt: str | None = None,
    vad_filter: bool = False,
    progress: Callable[[str], None] | None = None,
) -> TranscriptionResult:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscriptionError(
            "The alignment engine is not installed. Run "
            '`pip install -e ".[align]"` (or `pip install karaoke-forge[align]`).'
        ) from exc

    audio = Path(audio_path)
    if not audio.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio}")
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

    if progress:
        progress(f"Whisper 模型 {model} 已就绪，开始识别音频。")

    try:
        segments, info = whisper_model.transcribe(
            str(audio),
            language=language,
            beam_size=beam_size,
            word_timestamps=True,
            # Speech-oriented VAD can reject long stretches of vocals mixed with
            # dense music. Scan the complete track by default for song alignment.
            vad_filter=vad_filter,
            condition_on_previous_text=True,
            initial_prompt=initial_prompt[:4000] if initial_prompt else None,
        )
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
