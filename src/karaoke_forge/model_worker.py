from __future__ import annotations

import argparse
import sys

from .transcribe import PINNED_MODEL_REVISIONS, predownload_faster_whisper_model


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Karaoke Forge isolated model downloader")
    parser.add_argument("model", choices=sorted(PINNED_MODEL_REVISIONS))
    args = parser.parse_args(argv)
    try:
        path = predownload_faster_whisper_model(
            args.model,
            progress=lambda message: print(message, flush=True),
        )
    except Exception as exc:  # noqa: BLE001 - child must return a concise parent-readable failure
        print(f"模型预下载失败：{exc}", file=sys.stderr, flush=True)
        return 2
    print(path, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
