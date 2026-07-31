from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gradio_client import Client, handle_file


def _local_path(value: object) -> Path:
    if isinstance(value, dict) and value.get("path"):
        value = value["path"]
    if not isinstance(value, (str, Path)):
        raise TypeError(f"无法读取网页返回的文件：{value!r}")
    return Path(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="通过真实 Gradio 会话模拟一次歌词编辑")
    parser.add_argument("project", type=Path)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:7860")
    args = parser.parse_args()

    project = args.project.resolve()
    audio = args.audio.resolve()
    if not project.is_file() or not audio.is_file():
        parser.error("歌词项目或音频文件不存在。")

    client = Client(args.url, verbose=False)
    environment = client.predict(api_name="/environment_markdown")
    if "Demucs 4.1.0" not in environment:
        raise RuntimeError("网页环境检查没有显示已安装的 Demucs。")

    loaded = client.predict(handle_file(project), api_name="/load_editor_project_workspace")
    table, line_number, whole, pronunciation, original_token_json = (
        loaded[0],
        int(loaded[2]),
        loaded[3],
        loaded[4],
        loaded[7],
    )
    if line_number != 1 or len(table["data"]) < 2:
        raise RuntimeError("网页没有完整载入歌词项目。")

    empty_undo = client.predict(table, 1, api_name="/undo_editor_line_action")
    if "暂无可撤销" not in empty_undo[7]:
        raise RuntimeError("无修改撤销没有返回安全提示。")
    table, whole, pronunciation, original_token_json = (
        empty_undo[0],
        empty_undo[2],
        empty_undo[3],
        empty_undo[6],
    )

    advanced = client.predict(
        handle_file(audio),
        table,
        1,
        original_token_json,
        whole,
        pronunciation,
        False,
        api_name="/advance_editor_line_after_playback",
    )
    if int(advanced[1]) != 2:
        raise RuntimeError("关闭循环后没有自动进入下一句。")
    if not (isinstance(advanced[0], dict) and advanced[0].get("__type__") == "update"):
        table = advanced[0]
    whole, pronunciation, original_token_json = advanced[2], advanced[3], advanced[6]
    original_entries = json.loads(original_token_json)
    if len(original_entries) < 3:
        raise RuntimeError("第 2 行没有载入完整逐词时间。")
    clip = client.predict(
        handle_file(audio),
        table,
        2,
        api_name="/editor_audio_with_prefetch",
    )[0]
    clip_path = _local_path(clip)
    if not clip_path.is_file() or clip_path.stat().st_size <= 0:
        raise RuntimeError("自动进入下一句后没有生成逐句试听。")

    edited_entries = json.loads(original_token_json)
    removed = edited_entries.pop(1)
    saved = client.predict(
        table,
        2,
        json.dumps(edited_entries, ensure_ascii=False),
        whole,
        pronunciation,
        api_name="/save_editor_token_timing_workspace",
    )
    if "已保存第 2 行逐词时间" not in saved[6]:
        raise RuntimeError("网页会话没有保存删词结果。")

    undone = client.predict(saved[0], 2, api_name="/undo_editor_line_action")
    if json.loads(undone[6]) != original_entries:
        raise RuntimeError("网页会话撤销后没有恢复原逐词时间。")

    exported = client.predict(
        undone[0],
        2,
        undone[2],
        undone[3],
        "网页会话自动验证",
        undone[6],
        api_name="/export_editor_project",
    )
    if "编辑结果已导出" not in exported[1] or len(exported[2]) != 6:
        raise RuntimeError("网页会话没有完整导出歌词。")

    print("真实网页会话验证通过")
    print(f"载入歌词：{len(table['data'])} 行")
    print(f"逐句试听：{clip_path}")
    print(f"删除并撤销：{removed['text']!r}")
    print(f"导出文件：{len(exported[2])} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
