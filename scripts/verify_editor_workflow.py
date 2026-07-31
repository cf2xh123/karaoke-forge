from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

from karaoke_forge.editor import (
    document_from_payload,
    pronunciation_to_editor_rows,
)
from karaoke_forge.web import (
    _editor_undo_snapshot,
    editor_token_workspace,
    export_editor_project,
    load_editor_project,
    preview_editor_audio_line,
    save_editor_token_timing,
    undo_editor_line_action,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("KARAOKE_FORGE_OUTPUT_DIR", str(PROJECT_ROOT / ".smoke" / "editor-flow"))


def _choose_line(payload: dict[str, object]) -> int:
    document = document_from_payload(payload)
    for number, line in enumerate(document.lines, 1):
        if (
            line.start is not None
            and line.end is not None
            and line.end - line.start >= 1.0
            and len(line.tokens) >= 3
        ):
            return number
    raise RuntimeError("项目中没有至少包含 3 个词块的定时歌词行。")


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟用户完成一次歌词载入、试听、改字、撤销和导出")
    parser.add_argument("project", type=Path, help="Karaoke Forge JSON 或带时间轴歌词")
    parser.add_argument("audio", type=Path, help="用于逐句试听的歌曲音频")
    args = parser.parse_args()

    project = args.project.resolve()
    audio = args.audio.resolve()
    if not project.is_file() or not audio.is_file():
        parser.error("歌词项目或音频文件不存在。")

    payload, rows, _status, _selected, _whole, _pronunciation, _preview = load_editor_project(
        project
    )
    selected = _choose_line(payload)
    document = document_from_payload(payload)
    original = copy.deepcopy(document.lines[selected - 1])

    clip, _clip_status = preview_editor_audio_line(audio, payload, rows, selected)
    if not Path(clip).is_file() or Path(clip).stat().st_size <= 0:
        raise RuntimeError("逐句试听片段没有生成。")

    empty_undo = undo_editor_line_action(payload, rows, selected, {})
    if "暂无可撤销" not in str(empty_undo[-2]):
        raise RuntimeError("无修改时的撤销没有返回安全提示。")

    _timeline, token_json = editor_token_workspace(payload, rows, selected)
    entries = json.loads(token_json)
    removed = entries.pop(1)
    undo_snapshot = _editor_undo_snapshot(document, selected)
    saved = save_editor_token_timing(
        payload,
        rows,
        selected,
        json.dumps(entries, ensure_ascii=False),
    )
    edited_payload, edited_rows = saved[0], saved[1]
    edited = document_from_payload(edited_payload).lines[selected - 1]
    expected_times = [
        (token.start, token.end) for index, token in enumerate(original.tokens) if index != 1
    ]
    if [(token.start, token.end) for token in edited.tokens] != expected_times:
        raise RuntimeError("删除词块后其他词块的时间发生了变化。")

    undone = undo_editor_line_action(edited_payload, edited_rows, selected, undo_snapshot)
    restored = document_from_payload(undone[0]).lines[selected - 1]
    if restored != original:
        raise RuntimeError("撤销后没有恢复原歌词行。")

    boundary_entries = json.loads(editor_token_workspace(undone[0], undone[1], selected)[1])
    left, right = boundary_entries[0], boundary_entries[1]
    lower = float(left["start"]) + 0.02
    upper = float(right["end"]) - 0.02
    boundary = min(upper, max(lower, (float(left["end"]) + float(right["start"])) / 2 + 0.02))
    left["end"] = boundary
    right["start"] = boundary
    adjusted = save_editor_token_timing(
        undone[0],
        undone[1],
        selected,
        json.dumps(boundary_entries, ensure_ascii=False),
    )

    adjusted_document = document_from_payload(adjusted[0])
    adjusted_line = adjusted_document.lines[selected - 1]
    exports = export_editor_project(
        adjusted[0],
        adjusted[1],
        selected,
        adjusted_line.pronunciation or "",
        pronunciation_to_editor_rows(adjusted_line),
        "自动验证-编辑结果",
        adjusted[6],
    )
    export_files = [Path(path) for path in exports[3]]
    if len(export_files) != 6 or not all(path.is_file() and path.stat().st_size > 0 for path in export_files):
        raise RuntimeError("编辑结果没有完整导出 6 种歌词格式。")

    print("自动编辑验证通过")
    print(f"歌词行：第 {selected} 行 · {original.text}")
    print(f"试听片段：{clip}")
    print(f"删除并撤销：{removed['text']!r}，其他词时间保持不变")
    print(f"边界拖动：{boundary:.3f}s")
    print(f"导出目录：{exports[4]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
