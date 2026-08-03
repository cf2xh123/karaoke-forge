import json
from pathlib import Path
from types import SimpleNamespace

from karaoke_forge.editor import (
    apply_pronunciation_rows,
    document_to_editor_rows,
    pronunciation_to_editor_rows,
)
from karaoke_forge.formats import parse_yrc
from karaoke_forge.models import KaraokeToken, LyricLine, LyricsDocument
from karaoke_forge.netease import NeteaseSongInfo
from karaoke_forge.pronunciation import PronunciationLine, PronunciationUnit
from karaoke_forge.web import (
    EDITOR_STOP_GATE_JS,
    TOKEN_TIMELINE_JS,
    _editor_clip_target,
    _editor_document_with_pending_changes,
    _file_path,
    _prepare_lyrics,
    _record_web_error,
    _safe_stem,
    apply_editor_line_action,
    environment_markdown,
    export_editor_project,
    exported_project_for_make,
    handoff_editor_to_make,
    handoff_make_readiness,
    load_editor_project,
    prepare_make_editor_job,
    preview_editor_audio_line,
    run_align_job,
    run_convert_job,
    run_make_job,
    save_editor_pronunciation_workspace,
    subtitle_preview_html,
    undo_editor_line_action,
)


def test_token_timeline_script_supports_context_delete_and_drag_pan() -> None:
    assert "deleteTokenBlock" in TOKEN_TIMELINE_JS
    assert 'tokenContextMenu.id = "kf-token-context-menu"' in TOKEN_TIMELINE_JS
    assert 'document.querySelector("#editor-save-tokens")' in TOKEN_TIMELINE_JS
    assert 'scrollArea.classList.add("is-panning")' in TOKEN_TIMELINE_JS
    assert "seekFromTimelinePointer" in TOKEN_TIMELINE_JS
    assert "__karaokeForgeDraggingPlayhead" in TOKEN_TIMELINE_JS
    assert 'closest?.(".kf-token-playhead")' in TOKEN_TIMELINE_JS
    assert "pauseForEditorMutation" in TOKEN_TIMELINE_JS
    assert 'event.target.closest?.(".kf-token-text")' in TOKEN_TIMELINE_JS
    assert '"#editor-save-tokens, #editor-save-tokens button' in TOKEN_TIMELINE_JS
    assert "workspaceLinesMatch" in TOKEN_TIMELINE_JS
    assert '"#editor-current-line input"' in TOKEN_TIMELINE_JS
    assert "__karaokeForgeTokenAuditionActive" in TOKEN_TIMELINE_JS
    assert "capturedTimeline.isConnected" in TOKEN_TIMELINE_JS
    assert "clearAuditionAfterLineChange" in TOKEN_TIMELINE_JS
    assert "__karaokeForgeTokenAuditionGuardUntil" in EDITOR_STOP_GATE_JS
    assert "__karaokeForgeSuppressAutoAdvanceUntil" not in EDITOR_STOP_GATE_JS
    assert "const stoppedLine = Number(args[3])" in EDITOR_STOP_GATE_JS
    assert "currentLine !== stoppedLine" in EDITOR_STOP_GATE_JS
    assert "__karaokeForgeEditorMutationGuardLine" in EDITOR_STOP_GATE_JS
    assert '"#editor-current-line input"' in EDITOR_STOP_GATE_JS
    assert "window.setTimeout(resolve, 55)" in EDITOR_STOP_GATE_JS


def test_safe_stem_removes_windows_path_characters() -> None:
    assert _safe_stem("  my:karaoke*video?.mp4  ") == "my-karaoke-video"
    assert _safe_stem("", fallback="song") == "song"


def test_file_path_accepts_gradio_file_data_dict() -> None:
    assert _file_path({"path": "C:/temp/lyrics.json"}) == Path("C:/temp/lyrics.json")


def test_uploaded_lyrics_take_priority_over_stale_pasted_text(tmp_path: Path) -> None:
    source = tmp_path / "edited.json"
    source.write_text('{"version":1,"metadata":{},"lines":[]}', encoding="utf-8")

    selected = _prepare_lyrics(str(source), "旧的粘贴歌词", tmp_path)

    assert selected == source
    assert not (tmp_path / "lyrics.txt").exists()


def test_exported_json_is_selected_and_inspected_for_make(tmp_path: Path) -> None:
    source = tmp_path / "edited.json"
    source.write_text(
        '{"version":1,"metadata":{"word_timing":"manual"},"lines":['
        '{"text":"AB","start":1.0,"end":2.0,"tokens":['
        '{"text":"A","start":1.0,"end":1.5},'
        '{"text":"B","start":1.5,"end":2.0}]}]}',
        encoding="utf-8",
    )

    selected, status = exported_project_for_make([str(tmp_path / "edited.ass"), str(source)])

    assert selected == str(source)
    assert "已载入 edited.json" in status
    assert "1 行含逐词时间" in status


def test_web_errors_are_written_to_a_persistent_log(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path))
    try:
        raise ValueError("example export failure")
    except ValueError as exc:
        target = _record_web_error("editor-export", exc)

    assert target == tmp_path / "karaoke-forge-errors.log"
    assert "editor-export" in target.read_text(encoding="utf-8")
    assert "example export failure" in target.read_text(encoding="utf-8")


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


def test_export_saves_pending_token_edits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    document = parse_yrc("[1000,3000](1000,1000,0)A(2000,1000,0)B(3000,1000,0)C\n")

    payload, rows, _status, _files, _directory = export_editor_project(
        document.to_dict(),
        document_to_editor_rows(document),
        1,
        "",
        [],
        "pending-token-edit",
        '[{"text":"A","start":1.0,"end":2.0},{"text":"C","start":3.0,"end":4.0}]',
    )

    assert rows[0][4] == "AC"
    assert [token["text"] for token in payload["lines"][0]["tokens"]] == ["A", "C"]


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


def test_web_editor_empty_undo_is_a_safe_noop(tmp_path: Path) -> None:
    source = tmp_path / "lyrics.lrc"
    source.write_text("[00:01.00]First\n[00:03.00]Second\n", encoding="utf-8")
    payload, rows, _status, line_number, *_rest = load_editor_project(str(source))

    result = undo_editor_line_action(payload, rows, line_number, {})

    assert result[0] == payload
    assert "暂无可撤销修改" in result[8]
    assert result[9] == {}


def test_pending_token_edit_is_saved_before_navigation_and_can_be_undone(
    tmp_path: Path,
) -> None:
    source = tmp_path / "lyrics.lrc"
    source.write_text("[00:01.00]A\n[00:03.00]B\n", encoding="utf-8")
    payload, rows, _status, _line_number, *_rest = load_editor_project(str(source))
    changed, snapshot = _editor_document_with_pending_changes(
        payload,
        rows,
        1,
        '[{"text":"改","start":1.0,"end":2.98}]',
    )

    assert changed.lines[0].text == "改"
    assert snapshot is not None
    undone = undo_editor_line_action(
        changed.to_dict(),
        [[1, "显示", 1.0, 2.98, "改", ""], [2, "显示", 3.0, 4.98, "B", ""]],
        2,
        snapshot,
    )
    assert undone[2] == 1
    assert undone[1][0][4] == "A"


def test_pending_pronunciation_is_saved_before_navigation_and_can_be_undone(
    tmp_path: Path,
) -> None:
    source = tmp_path / "lyrics.lrc"
    source.write_text("[00:01.00]A\n[00:03.00]B\n", encoding="utf-8")
    payload, rows, _status, _line_number, *_rest = load_editor_project(str(source))

    changed, snapshot = _editor_document_with_pending_changes(
        payload,
        rows,
        1,
        None,
        "ei",
        [["A", "ei", 0, 1]],
    )

    assert changed.lines[0].pronunciation == "ei"
    assert changed.lines[0].pronunciation_units[0].reading == "ei"
    assert snapshot is not None
    undone = undo_editor_line_action(
        changed.to_dict(),
        rows,
        2,
        snapshot,
    )
    assert undone[2] == 1
    assert undone[3] == ""


def test_pending_token_delete_remaps_the_existing_pronunciation_table() -> None:
    document = parse_yrc("[1000,3000](1000,1000,0)A(2000,1000,0)B(3000,1000,0)C\n")
    document = apply_pronunciation_rows(
        document,
        1,
        [["A", "a", 0, 1], ["B", "b", 1, 2], ["C", "c", 2, 3]],
        "",
    )
    pronunciation_rows = [
        [unit.source, unit.reading, unit.start, unit.end]
        for unit in document.lines[0].pronunciation_units
    ]
    pronunciation_rows[2][1] = "see"

    changed, snapshot = _editor_document_with_pending_changes(
        document.to_dict(),
        document_to_editor_rows(document),
        1,
        '[{"text":"A","start":1.0,"end":2.0},{"text":"C","start":3.0,"end":4.0}]',
        "",
        pronunciation_rows,
    )

    assert snapshot is not None
    assert changed.lines[0].text == "AC"
    assert [
        (unit.source, unit.reading, unit.start, unit.end)
        for unit in changed.lines[0].pronunciation_units
    ] == [("A", "a", 0, 1), ("C", "see", 1, 2)]


def test_saving_pronunciation_after_pending_token_delete_does_not_reapply_old_spans() -> None:
    document = parse_yrc("[1000,3000](1000,1000,0)A(2000,1000,0)B(3000,1000,0)C\n")
    document = apply_pronunciation_rows(
        document,
        1,
        [["A", "a", 0, 1], ["B", "b", 1, 2], ["C", "c", 2, 3]],
        "",
    )
    pronunciation_rows = pronunciation_to_editor_rows(document.lines[0])
    pronunciation_rows[2][1] = "see"

    saved = save_editor_pronunciation_workspace(
        document.to_dict(),
        document_to_editor_rows(document),
        1,
        '[{"text":"A","start":1.0,"end":2.0},{"text":"C","start":3.0,"end":4.0}]',
        "",
        pronunciation_rows,
        {},
    )

    assert saved[1][0][4] == "AC"
    assert saved[3] == [["A", "a", 0, 1], ["C", "see", 1, 2]]
    assert saved[-1]


def test_pending_line_text_edit_ignores_an_unchanged_old_pronunciation_table() -> None:
    document = parse_yrc("[1000,3000](1000,1000,0)A(2000,1000,0)B(3000,1000,0)C\n")
    document = apply_pronunciation_rows(
        document,
        1,
        [["A", "a", 0, 1], ["B", "b", 1, 2], ["C", "c", 2, 3]],
        "",
    )
    rows = document_to_editor_rows(document)
    rows[0][4] = "AC"
    old_pronunciation_rows = [
        [unit.source, unit.reading, unit.start, unit.end]
        for unit in document.lines[0].pronunciation_units
    ]

    changed, snapshot = _editor_document_with_pending_changes(
        document.to_dict(),
        rows,
        1,
        None,
        "",
        old_pronunciation_rows,
    )

    assert snapshot is not None
    assert changed.lines[0].text == "AC"
    assert changed.lines[0].pronunciation_units == []


def test_editor_clip_cache_distinguishes_same_named_audio_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_CACHE_DIR", str(tmp_path / "cache"))
    first = tmp_path / "one" / "videoplayback.m4a"
    second = tmp_path / "two" / "videoplayback.m4a"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    first_target = _editor_clip_target(first, 0, 1.0, 2.0)
    second_target = _editor_clip_target(second, 0, 1.0, 2.0)

    assert first_target != second_target


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

    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"preview")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("karaoke_forge.web.subprocess.run", fake_run)

    clip, status = preview_editor_audio_line(str(audio), payload, rows, 1)

    assert Path(clip).is_file()
    assert "当前歌词试听范围：**1.00s → 2.98s**" in status
    assert str(tmp_path / "cache") in clip
    assert "clip-1.000-2.980" in Path(clip).name
    assert commands[0][commands[0].index("-ss") + 1] == "1.000"
    assert commands[0][commands[0].index("-t") + 1] == "1.980"


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


def test_editor_handoff_waits_for_a_missing_mv_without_losing_project(
    tmp_path: Path,
) -> None:
    project = tmp_path / "confirmed.json"
    project.write_text('{"version":1,"metadata":{},"lines":[]}', encoding="utf-8")

    result = handoff_make_readiness(None, str(project))

    assert result is not None
    assert "校准歌词已载入制作页" in result.status
    assert "请上传对应 MV" in result.status
    assert "无需重复上传" in result.status
    assert result.video is None


def test_editor_handoff_is_ready_to_render_with_project_and_mv(tmp_path: Path) -> None:
    project = tmp_path / "confirmed.json"
    video = tmp_path / "mv.webm"
    project.write_text('{"version":1,"metadata":{},"lines":[]}', encoding="utf-8")
    video.write_bytes(b"video")

    assert handoff_make_readiness(str(video), str(project)) is None


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
        lambda text, **_kwargs: (
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


def test_make_page_keeps_line_timing_when_auto_refinement_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    audio = tmp_path / "song.m4a"
    video = tmp_path / "mv.mp4"
    lyrics = tmp_path / "lyrics.lrc"
    audio.write_bytes(b"audio")
    video.write_bytes(b"video")
    lyrics.write_text("[00:01.00]Hello world\n", encoding="utf-8")
    monkeypatch.setattr(
        "karaoke_forge.web.refine_audio_word_timing_with_fallback",
        lambda *_args, **_kwargs: None,
    )

    result = prepare_make_editor_job(
        str(audio),
        str(video),
        str(lyrics),
        "",
        "fallback-rehearsal",
        "自动识别",
        "small",
        "auto",
        False,
        timing_refinement="auto",
        output_root=str(tmp_path / "custom-output"),
    )

    assert result.project is not None
    assert "自动精修暂不可用" in result.status
    assert "没有生成成功" not in result.status


def test_make_page_returns_low_coverage_project_with_actionable_details(
    tmp_path: Path,
    monkeypatch,
) -> None:
    audio = tmp_path / "song.wav"
    video = tmp_path / "mv.mp4"
    lyrics = tmp_path / "lyrics.txt"
    audio.write_bytes(b"audio")
    video.write_bytes(b"video")
    lyrics.write_text("First lyric\nSecond lyric\n", encoding="utf-8")
    fallback = LyricsDocument(
        lines=[
            LyricLine(
                "First lyric",
                1.0,
                2.0,
                [KaraokeToken("First lyric", 1.0, 2.0)],
            ),
            LyricLine(
                "Second lyric",
                2.0,
                4.0,
                [KaraokeToken("Second lyric", 2.0, 4.0)],
            ),
        ],
        metadata={"alignment_status": "low_coverage_recovery"},
        source_format="aligned",
    )
    monkeypatch.setattr(
        "karaoke_forge.web.align_audio_and_lyrics",
        lambda *_args, **_kwargs: SimpleNamespace(
            document=fallback,
            recovered=True,
            report=SimpleNamespace(coverage=0.1, unmatched_line_indexes=(1,)),
            transcription=SimpleNamespace(
                detected_language="en",
                language_probability=0.91,
            ),
        ),
    )
    monkeypatch.setattr(
        "karaoke_forge.web._materialize_auto_pronunciation",
        lambda _doc, **_kwargs: 0,
    )

    result = prepare_make_editor_job(
        str(audio),
        str(video),
        str(lyrics),
        "",
        "recovered-project",
        "自动识别",
        "small",
        "auto",
        False,
        output_root=str(tmp_path / "outputs"),
    )

    assert result.project is not None
    assert "保底时间轴" in result.status
    assert "第 2 行：Second lyric" in result.status
    assert "识别语言" in result.status
    assert "medium / large-v3" in result.status


def test_make_page_uses_embedded_mv_audio_for_editor_preparation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    video = tmp_path / "mv-with-audio.webm"
    lyrics = tmp_path / "lyrics.lrc"
    video.write_bytes(b"video with audio")
    lyrics.write_text("[00:01.00]Hello world\n", encoding="utf-8")
    monkeypatch.setattr("karaoke_forge.web.probe_media_has_audio", lambda _path: True)

    result = prepare_make_editor_job(
        None,
        str(video),
        str(lyrics),
        "",
        "embedded-audio-rehearsal",
        "自动识别",
        "small",
        "auto",
        False,
        timing_refinement="off",
        output_root=str(tmp_path / "custom-output"),
    )

    assert result.project is not None
    assert result.audio == str(video)
    assert "使用 MV 内嵌完整音轨进行校准" in result.log


def test_make_page_explains_when_mv_has_no_audio(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "silent.webm"
    lyrics = tmp_path / "lyrics.lrc"
    video.write_bytes(b"silent video")
    lyrics.write_text("[00:01.00]Hello world\n", encoding="utf-8")
    monkeypatch.setattr("karaoke_forge.web.probe_media_has_audio", lambda _path: False)

    result = prepare_make_editor_job(
        None,
        str(video),
        str(lyrics),
        "",
        "silent-rehearsal",
        "自动识别",
        "small",
        "auto",
        False,
        timing_refinement="off",
    )

    assert result.project is None
    assert "MV 不含可用音轨" in result.status


def test_subtitle_preview_reflects_translation_pronunciation_and_style(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "karaoke_forge.web.generate_pronunciation",
        lambda text, **_kwargs: PronunciationLine(
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


def test_make_job_can_use_qqmusic_page_lyrics_with_local_audio(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from karaoke_forge.qqmusic import QQMusicSongInfo

    audio = tmp_path / "authorized.flac"
    video = tmp_path / "mv.mp4"
    audio.write_bytes(b"audio")
    video.write_bytes(b"video")
    info = QQMusicSongInfo(
        song_mid="001gQnW91BEDaN",
        title="QQ Linked Song",
        artists=("Artist",),
        canonical_url="https://y.qq.com/n/ryqq_v2/songDetail/001gQnW91BEDaN",
        page_lyrics="[00:01.00]Hello\n[00:02.00]World\n",
    )
    monkeypatch.setattr(
        "karaoke_forge.web.fetch_public_qqmusic_info",
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
        content = Path(lyrics).read_text(encoding="utf-8")
        assert '"source": "QQ Music"' in content
        assert "Hello" in content
        output = Path(output)
        output.write_bytes(b"rendered")
        assets = Path(assets)
        assets.mkdir(parents=True, exist_ok=True)
        exported = assets / "lyrics.lrc"
        exported.write_text("[00:01.00]Hello\n", encoding="utf-8")
        return SimpleNamespace(
            video=output,
            exports={"lrc": exported},
            alignment_report=None,
            sync_result=None,
        )

    monkeypatch.setattr("karaoke_forge.web.make_karaoke_video", fake_make)
    result = run_make_job(
        str(audio),
        str(video),
        None,
        "",
        "qq-linked-karaoke",
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
        rights_confirmed=True,
        output_root=str(tmp_path / "outputs"),
        qqmusic_link=info.canonical_url,
        use_qqmusic_lyrics=True,
    )

    assert result.video is not None
    assert "仅从 QQ 音乐读取公开歌词" in result.log


def test_make_job_can_import_utaten_lyrics_and_furigana(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from karaoke_forge.utaten import UtaTenLyricsInfo

    audio = tmp_path / "authorized.flac"
    video = tmp_path / "mv.mp4"
    audio.write_bytes(b"audio")
    video.write_bytes(b"video")
    info = UtaTenLyricsInfo(
        lyric_id="yh15042710",
        title="Example Song",
        artist="Example Artist",
        canonical_url="https://utaten.com/lyric/yh15042710/",
        lyrics=("迷い", "Wake up"),
        readings=("まよい", "ウェイク up"),
    )
    monkeypatch.setattr(
        "karaoke_forge.web.fetch_public_utaten_info",
        lambda _link: info,
    )

    def fake_make(_audio, _video, lyrics, output, assets, **_kwargs):
        payload = json.loads(Path(lyrics).read_text(encoding="utf-8"))
        assert payload["metadata"]["source"] == "UtaTen"
        assert payload["metadata"]["source_id"] == "yh15042710"
        assert payload["lines"][0]["text"] == "迷い"
        assert payload["lines"][0]["pronunciation"] == "まよい"
        output = Path(output)
        output.write_bytes(b"rendered")
        assets = Path(assets)
        assets.mkdir(parents=True, exist_ok=True)
        exported = assets / "lyrics.json"
        exported.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(
            video=output,
            exports={"json": exported},
            alignment_report=None,
            sync_result=None,
        )

    monkeypatch.setattr("karaoke_forge.web.make_karaoke_video", fake_make)
    result = run_make_job(
        str(audio),
        str(video),
        None,
        "",
        "utaten-karaoke",
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
        rights_confirmed=True,
        output_root=str(tmp_path / "outputs"),
        utaten_link=info.canonical_url,
        use_utaten_lyrics=True,
    )

    assert result.video is not None
    assert "UtaTen" in result.log
    assert "2 行公开歌词和假名" in result.log


def test_make_job_can_use_only_utaten_pronunciation_with_uploaded_lyrics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from karaoke_forge.utaten import UtaTenLyricsInfo, UtaTenPronunciationUnit

    audio = tmp_path / "authorized.flac"
    video = tmp_path / "mv.mp4"
    project = tmp_path / "own-lyrics.json"
    audio.write_bytes(b"audio")
    video.write_bytes(b"video")
    own_document = LyricsDocument(
        lines=[
            LyricLine(
                "[Intro]",
                0.0,
                1.0,
                [KaraokeToken("[Intro]", 0.0, 1.0)],
                pronunciation="イントロ",
            ),
            LyricLine(
                "迷い。",
                1.25,
                2.75,
                [KaraokeToken("迷い。", 1.25, 2.75)],
                pronunciation="めい",
            ),
            LyricLine(
                "Wake  up!",
                3.0,
                4.5,
                [KaraokeToken("Wake  up!", 3.0, 4.5)],
                pronunciation="ウェイクアップ（誤）",
            ),
        ],
        metadata={"source": "My edited lyrics", "word_timing": "manual"},
        source_format="json",
    )
    project.write_text(
        json.dumps(own_document.to_dict(), ensure_ascii=False),
        encoding="utf-8",
    )
    info = UtaTenLyricsInfo(
        lyric_id="example",
        title="Official title",
        artist="Official artist",
        canonical_url="https://utaten.com/lyric/example/",
        lyrics=("迷い", "Wake up"),
        readings=("まよい", "ウェイク up"),
        pronunciation_units=(
            (UtaTenPronunciationUnit("迷", "まよ", 0, 1),),
            (UtaTenPronunciationUnit("Wake", "ウェイク", 0, 4),),
        ),
    )
    monkeypatch.setattr(
        "karaoke_forge.web.fetch_public_utaten_info",
        lambda _link: info,
    )

    def fake_make(_audio, _video, lyrics, output, assets, *, options, **_kwargs):
        payload = json.loads(Path(lyrics).read_text(encoding="utf-8"))
        assert payload["metadata"]["source"] == "My edited lyrics"
        assert payload["metadata"]["auto_pronunciation"] == "false"
        assert [line["text"] for line in payload["lines"]] == [
            "[Intro]",
            "迷い。",
            "Wake  up!",
        ]
        assert [line["start"] for line in payload["lines"]] == [0.0, 1.25, 3.0]
        assert payload["lines"][0]["pronunciation"] is None
        assert payload["lines"][0]["pronunciation_units"] == []
        assert payload["lines"][1]["pronunciation"] is None
        assert payload["lines"][1]["pronunciation_units"] == [
            {"source": "迷", "reading": "まよ", "start": 0, "end": 1}
        ]
        assert payload["lines"][2]["pronunciation_units"] == [
            {"source": "Wake", "reading": "ウェイク", "start": 0, "end": 4}
        ]
        assert not options.style.auto_pronunciation
        output = Path(output)
        output.write_bytes(b"rendered")
        assets = Path(assets)
        assets.mkdir(parents=True, exist_ok=True)
        exported = assets / "lyrics.json"
        exported.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(
            video=output,
            exports={"json": exported},
            alignment_report=None,
            sync_result=None,
        )

    monkeypatch.setattr("karaoke_forge.web.make_karaoke_video", fake_make)
    result = run_make_job(
        str(audio),
        str(video),
        str(project),
        "",
        "official-readings-only",
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
        rights_confirmed=True,
        timing_refinement="off",
        output_root=str(tmp_path / "outputs"),
        utaten_link=info.canonical_url,
        use_utaten_lyrics=False,
        utaten_pronunciation_only=True,
    )

    assert result.video is not None
    assert "仅采用官方注音" in result.log
    assert "匹配 2/3 行" in result.log
    assert "未匹配文字保持原样" in result.log


def test_editor_preparation_can_disable_automatic_english_pronunciation(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "song.wav"
    video = tmp_path / "mv.mp4"
    lyrics = tmp_path / "lyrics.lrc"
    audio.write_bytes(b"audio")
    video.write_bytes(b"video")
    lyrics.write_text("[00:01.00]I you\n", encoding="utf-8")

    result = prepare_make_editor_job(
        str(audio),
        str(video),
        str(lyrics),
        "",
        "no-english-reading",
        "自动识别",
        "small",
        "auto",
        False,
        timing_refinement="off",
        output_root=str(tmp_path / "outputs"),
        auto_english_pronunciation=False,
    )

    assert result.project is not None
    assert result.payload["metadata"]["auto_english_pronunciation"] == "false"
    assert result.payload["lines"][0]["pronunciation"] is None
    assert result.payload["lines"][0]["pronunciation_units"] == []
    assert result.pronunciation_rows == []
    assert "英语片假名自动注音已关闭" in result.status


def test_make_job_prefers_an_uploaded_edited_project_over_stale_pasted_lyrics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    audio = tmp_path / "song.m4a"
    video = tmp_path / "mv.webm"
    project = tmp_path / "edited.json"
    audio.write_bytes(b"audio")
    video.write_bytes(b"video")
    project.write_text(
        '{"version":1,"metadata":{"word_timing":"manual"},"lines":['
        '{"text":"EDITED","start":1.0,"end":2.0,"tokens":['
        '{"text":"EDITED","start":1.0,"end":2.0}]}]}',
        encoding="utf-8",
    )

    def fake_make(_audio, _video, lyrics, output, assets, **_kwargs):
        assert Path(lyrics) == project
        assert "EDITED" in Path(lyrics).read_text(encoding="utf-8")
        output = Path(output)
        output.write_bytes(b"rendered")
        assets = Path(assets)
        assets.mkdir(parents=True)
        exported = assets / "edited.json"
        exported.write_text(project.read_text(encoding="utf-8"), encoding="utf-8")
        return SimpleNamespace(
            video=output,
            exports={"json": exported},
            alignment_report=None,
            sync_result=None,
        )

    monkeypatch.setattr("karaoke_forge.web.make_karaoke_video", fake_make)
    result = run_make_job(
        str(audio),
        str(video),
        str(project),
        "这是制作页以前残留的原歌词",
        "edited-karaoke",
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
    )

    assert result.video is not None
    assert "已生成" in result.status


def test_make_job_uses_mv_audio_without_downloading_netease_audio(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    video = tmp_path / "mv.mp4"
    video.write_bytes(b"video with complete audio")
    info = NeteaseSongInfo(
        song_id="1946664196",
        title="garden.",
        artists=("CVLTE",),
        canonical_url="https://music.163.com/song?id=1946664196",
        duration=215.0,
        page_lyrics="[00:01.00]Hello\n[00:03.00]World\n",
        translated_lyrics="[00:01.00]你好\n[00:03.00]世界\n",
    )
    monkeypatch.setattr("karaoke_forge.web.probe_media_has_audio", lambda _path: True)
    monkeypatch.setattr(
        "karaoke_forge.web.fetch_public_netease_info",
        lambda _link: info,
    )
    monkeypatch.setattr(
        "karaoke_forge.web.download_netease_track",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("MV audio should avoid downloading NetEase audio")
        ),
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
        info.canonical_url,
        True,
        True,
    )

    assert result.video is not None
    assert "MV 内嵌完整音轨" in result.log
    assert "中文翻译" in result.log
