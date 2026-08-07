import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from karaoke_forge.editor import (
    apply_pronunciation_rows,
    document_to_editor_rows,
    pronunciation_to_editor_rows,
)
from karaoke_forge.formats import parse_yrc, read_lyrics
from karaoke_forge.models import KaraokeToken, LyricLine, LyricsDocument
from karaoke_forge.netease import NeteaseSongInfo, NeteaseTrack
from karaoke_forge.projects import load_workspace_project
from karaoke_forge.pronunciation import PronunciationLine, PronunciationUnit
from karaoke_forge.web import (
    EDITOR_STOP_GATE_JS,
    TOKEN_TIMELINE_JS,
    _cached_public_netease_preview_info,
    _editor_clip_target,
    _editor_document_with_pending_changes,
    _file_path,
    _is_loopback_host,
    _NeteaseSessionBroker,
    _next_playable_editor_line,
    _prepare_lyrics,
    _recent_workspace_offer,
    _record_web_error,
    _safe_stem,
    _select_subtitle_preview_sample,
    apply_editor_line_action,
    auto_configure_model_network_for_web,
    configure_model_network_for_web,
    create_web_app,
    environment_markdown,
    export_editor_project,
    exported_project_for_make,
    handoff_editor_to_make,
    handoff_make_readiness,
    launch_web_app,
    load_editor_project,
    prepare_make_editor_job,
    prepare_subtitle_material_preview,
    preview_editor_audio_line,
    run_align_job,
    run_convert_job,
    run_make_job,
    save_editor_pronunciation_workspace,
    subtitle_preview_html,
    undo_editor_line_action,
)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "[::1]", "localhost"])
def test_managed_browser_login_accepts_only_loopback_hosts(host: str) -> None:
    assert _is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "karaoke.local", ""])
def test_managed_browser_login_rejects_remote_hosts(host: str) -> None:
    assert not _is_loopback_host(host)


def test_netease_session_broker_rejects_old_managed_tokens_after_account_change() -> None:
    broker = _NeteaseSessionBroker("old-managed-token")
    generation = broker.begin_explicit_login(disable_existing=True)

    assert broker.commit_explicit_login("new-managed-token", generation)
    assert not broker.managed_token_allowed("old-managed-token")
    assert broker.managed_token_allowed("new-managed-token")
    assert broker.managed_token_allowed("manual-session-token")


def test_captured_netease_session_stays_in_server_state(monkeypatch, tmp_path) -> None:
    secret = "server-only-session-secret"
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(
        "karaoke_forge.web.capture_netease_music_u",
        lambda: secret,
    )
    app = create_web_app(managed_netease_login=True)
    login_callbacks = [
        block_function
        for block_function in app.fns.values()
        if getattr(block_function.fn, "__name__", "") == "capture_netease_login_wrapper"
    ]

    assert login_callbacks
    callback = login_callbacks[0]
    assert type(callback.outputs[0]).__name__ == "State"
    assert [type(output).__name__ for output in callback.outputs[1:3]] == [
        "Textbox",
        "Textbox",
    ]

    begin_callback = next(
        block_function
        for block_function in app.fns.values()
        if getattr(block_function.fn, "__name__", "") == "begin_netease_login"
    )
    login_generation = begin_callback.fn()[0]
    result = callback.fn(login_generation)

    assert result[0] == secret
    assert result[1:3] == ("", "")
    assert secret not in repr(result[1:])
    client_response = asyncio.run(app.postprocess_data(callback, list(result), None))
    assert client_response[0] is None
    assert secret not in repr(client_response)

    remote_result = callback.fn(
        login_generation,
        SimpleNamespace(client=SimpleNamespace(host="192.168.1.20")),
    )
    assert secret not in repr(remote_result)
    assert "远程监听模式" in remote_result[3]

    protected_jobs = {
        "make_wrapper",
        "prepare_make_editor_wrapper",
        "netease_wrapper",
        "make_after_editor_handoff",
    }
    for block_function in app.fns.values():
        if getattr(block_function.fn, "__name__", "") not in protected_jobs:
            continue
        assert any(type(component).__name__ == "State" for component in block_function.inputs)
        component_parameters = [
            parameter
            for name, parameter in inspect.signature(block_function.fn).parameters.items()
            if name not in {"request", "progress"}
        ]
        assert len(block_function.inputs) == len(component_parameters)
        assert not any(
            str(getattr(component, "label", "")).startswith("手动 MUSIC_U")
            for component in block_function.inputs
        )
    lyrics = tmp_path / "restored-project.json"
    lyrics.write_text(
        json.dumps(
            LyricsDocument(lines=[LyricLine(text="Restored lyric", start=0.0, end=2.0)]).to_dict()
        ),
        encoding="utf-8",
    )
    restored_files = {
        "\N{CIRCLED DIGIT ONE}": tmp_path / "restored.wav",
        "\N{CIRCLED DIGIT TWO}": tmp_path / "restored.mp4",
        "\N{CIRCLED DIGIT THREE}": lyrics,
        "\u6ca1\u6709 MV": tmp_path / "restored.jpg",
    }
    for marker, path in restored_files.items():
        if marker != "\N{CIRCLED DIGIT THREE}":
            path.write_bytes(b"restored")
    workspace = SimpleNamespace(
        manifest=tmp_path / "karaoke-forge-project.json",
        lyrics_project=lyrics,
        audio=restored_files["\N{CIRCLED DIGIT ONE}"],
        video=restored_files["\N{CIRCLED DIGIT TWO}"],
        cover=restored_files["\u6ca1\u6709 MV"],
        font_files=(),
        settings={},
        name="Restored project",
    )
    monkeypatch.setattr(
        "karaoke_forge.web.load_workspace_project",
        lambda _manifest: workspace,
    )
    restore_callback = next(
        block_function
        for block_function in app.fns.values()
        if getattr(block_function.fn, "__name__", "") == "restore_recent_workspace"
    )
    restored = restore_callback.fn(str(workspace.manifest))

    assert len(restore_callback.outputs) == len(restored) == 22
    assert restored[-2]["visible"] is False
    assert "已恢复工程" in restored[-1]
    for marker, expected in restored_files.items():
        index = next(
            index
            for index, output in enumerate(restore_callback.outputs)
            if marker in str(getattr(output, "label", ""))
        )
        assert restored[index] == str(expected)

    monkeypatch.setattr("karaoke_forge.web.load_recent_workspace", lambda _root: workspace)
    offered = _recent_workspace_offer()
    assert offered[0] == str(workspace.manifest)
    assert "Restored project" in offered[1]
    assert offered[2] is True

    refresh_callback = next(
        block_function
        for block_function in app.fns.values()
        if getattr(block_function.fn, "__name__", "") == "refresh_recent_workspace_offer"
    )
    refreshed = refresh_callback.fn()
    assert refreshed[0] == str(workspace.manifest)
    assert "Restored project" in refreshed[1]
    assert refreshed[2]["visible"] is True
    assert refreshed[3]["interactive"] is True

    blank_callback = next(
        block_function
        for block_function in app.fns.values()
        if getattr(block_function.fn, "__name__", "") == "start_blank_workspace_choice"
    )
    blank = blank_callback.fn()
    assert blank[0]["visible"] is False
    assert "没有被删除" in blank[1]

    def broken_lyrics(_path):
        raise ValueError("broken lyrics")

    monkeypatch.setattr("karaoke_forge.web.read_lyrics", broken_lyrics)
    broken_restore = restore_callback.fn(str(workspace.manifest))
    assert broken_restore[-2]["visible"] is False
    assert "暂时无法恢复" in broken_restore[-1]

    monkeypatch.setattr("karaoke_forge.web.load_recent_workspace", lambda _root: None)
    empty_offer = _recent_workspace_offer()
    assert empty_offer[0] == ""
    assert "没有找到" in empty_offer[1]
    assert empty_offer[2] is False


def test_saved_netease_session_is_restored_only_for_local_clients(monkeypatch) -> None:
    secret = "persisted-server-only-secret"
    app = create_web_app(
        managed_netease_login=True,
        initial_netease_music_u=secret,
    )
    callback = next(
        block_function
        for block_function in app.fns.values()
        if getattr(block_function.fn, "__name__", "")
        == "ensure_netease_session_for_download"
    )
    state_inputs = [component for component in callback.inputs if type(component).__name__ == "State"]
    assert state_inputs
    assert state_inputs[0].value == ""
    assert secret not in json.dumps(app.config, ensure_ascii=False, default=str)

    monkeypatch.setattr("karaoke_forge.web.managed_netease_profile_exists", lambda: True)
    monkeypatch.setattr("karaoke_forge.web.acquire_netease_music_u", lambda: secret)

    result = callback.fn(
        "https://music.163.com/song?id=42",
        None,
        None,
        True,
        "",
        SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")),
    )
    assert result[0] == secret
    client_response = asyncio.run(app.postprocess_data(callback, list(result), None))
    assert client_response[0] is None
    assert secret not in repr(client_response)

    remote = callback.fn(
        "https://music.163.com/song?id=42",
        None,
        None,
        True,
        secret,
        SimpleNamespace(client=SimpleNamespace(host="192.168.1.20")),
    )
    assert remote[0] == ""
    remote_client = asyncio.run(app.postprocess_data(callback, list(remote), None))
    assert remote_client[0] is None
    assert secret not in repr(remote_client)

    make_callback = next(
        block_function
        for block_function in app.fns.values()
        if getattr(block_function.fn, "__name__", "") == "make_wrapper"
    )
    captured_credentials: dict[str, str] = {}

    def fake_run_make_job(*args, **_kwargs):
        captured_credentials["cookie_browser"] = args[19]
        captured_credentials["cookie_profile"] = args[20]
        captured_credentials["music_u"] = args[21]
        return SimpleNamespace(
            status="done",
            video=None,
            files=[],
            log="",
            output_dir=None,
        )

    monkeypatch.setattr("karaoke_forge.web.run_make_job", fake_run_make_job)
    make_inputs = [getattr(component, "value", None) for component in make_callback.inputs]
    for index, component in enumerate(make_callback.inputs):
        if type(component).__name__ == "State":
            make_inputs[index] = secret
        elif "Cookie 浏览器" in str(getattr(component, "label", "")):
            make_inputs[index] = "edge"
        elif "Profile" in str(getattr(component, "label", "")):
            make_inputs[index] = "Default"
    make_callback.fn(
        *make_inputs,
        request=SimpleNamespace(client=SimpleNamespace(host="192.168.1.20")),
        progress=lambda *_args, **_kwargs: None,
    )
    assert captured_credentials == {
        "cookie_browser": "",
        "cookie_profile": "",
        "music_u": "",
    }

    clear_callback = next(
        block_function
        for block_function in app.fns.values()
        if getattr(block_function.fn, "__name__", "") == "clear_netease_login_wrapper"
    )
    monkeypatch.setattr("karaoke_forge.web.clear_netease_login_profile", lambda: "cleared")
    cleared = clear_callback.fn(
        SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    )
    assert cleared[:3] == ("", "", "")

    captured_credentials.clear()
    make_callback.fn(
        *make_inputs,
        request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")),
        progress=lambda *_args, **_kwargs: None,
    )
    assert captured_credentials["music_u"] == ""

    def unexpected_reuse_after_logout():
        raise AssertionError("logout must disable managed profile reuse for this server")

    monkeypatch.setattr(
        "karaoke_forge.web.acquire_netease_music_u",
        unexpected_reuse_after_logout,
    )
    stale_after_logout = callback.fn(
        "https://music.163.com/song?id=42",
        None,
        None,
        True,
        secret,
        SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")),
    )
    assert stale_after_logout[0] == ""


def test_launch_restores_saved_netease_session_only_for_loopback(monkeypatch, tmp_path) -> None:
    secret = "startup-server-only-secret"
    reuse_calls: list[float] = []
    app_options: list[dict[str, object]] = []

    class FakeApp:
        def queue(self, **_kwargs):
            return self

        def launch(self, **_kwargs):
            return None

    def reuse(timeout_seconds: float) -> str:
        reuse_calls.append(timeout_seconds)
        return secret

    def fake_create_web_app(**kwargs):
        app_options.append(kwargs)
        return FakeApp()

    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr("karaoke_forge.web.try_reuse_netease_music_u", reuse)
    monkeypatch.setattr("karaoke_forge.web.create_web_app", fake_create_web_app)

    launch_web_app(host="127.0.0.1", port=17860, open_browser=False)
    launch_web_app(host="0.0.0.0", port=17861, open_browser=False)

    assert reuse_calls == [12.0]
    assert app_options[0] == {
        "managed_netease_login": True,
        "initial_netease_music_u": secret,
    }
    assert app_options[1] == {
        "managed_netease_login": False,
        "initial_netease_music_u": "",
    }


def test_logout_wins_over_an_in_flight_netease_session_reuse(monkeypatch) -> None:
    secret = "managed-session-secret"
    app = create_web_app(
        managed_netease_login=True,
        initial_netease_music_u=secret,
    )
    ensure_callback = next(
        block_function
        for block_function in app.fns.values()
        if getattr(block_function.fn, "__name__", "")
        == "ensure_netease_session_for_download"
    )
    clear_callback = next(
        block_function
        for block_function in app.fns.values()
        if getattr(block_function.fn, "__name__", "") == "clear_netease_login_wrapper"
    )
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    monkeypatch.setattr("karaoke_forge.web.managed_netease_profile_exists", lambda: True)
    monkeypatch.setattr("karaoke_forge.web.clear_netease_login_profile", lambda: "cleared")

    def acquire_then_logout() -> str:
        assert clear_callback.fn(request)[:3] == ("", "", "")
        return secret

    monkeypatch.setattr(
        "karaoke_forge.web.acquire_netease_music_u",
        acquire_then_logout,
    )
    result = ensure_callback.fn(
        "https://music.163.com/song?id=42",
        None,
        None,
        True,
        secret,
        request,
    )
    assert result[0] == ""
    assert "结果已安全丢弃" in result[1]

    def unexpected_acquire():
        raise AssertionError("a late reuse result must not re-enable login")

    monkeypatch.setattr("karaoke_forge.web.acquire_netease_music_u", unexpected_acquire)
    assert (
        ensure_callback.fn(
            "https://music.163.com/song?id=42",
            None,
            None,
            True,
            secret,
            request,
        )[0]
        == ""
    )


def test_logout_wins_over_an_in_flight_explicit_netease_login(monkeypatch) -> None:
    secret = "late-explicit-login-secret"
    app = create_web_app(managed_netease_login=True)
    begin_callback = next(
        block_function
        for block_function in app.fns.values()
        if getattr(block_function.fn, "__name__", "") == "begin_netease_login"
    )
    capture_callback = next(
        block_function
        for block_function in app.fns.values()
        if getattr(block_function.fn, "__name__", "") == "capture_netease_login_wrapper"
    )
    clear_callback = next(
        block_function
        for block_function in app.fns.values()
        if getattr(block_function.fn, "__name__", "") == "clear_netease_login_wrapper"
    )
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    monkeypatch.setattr("karaoke_forge.web.clear_netease_login_profile", lambda: "cleared")

    def capture_then_logout() -> str:
        assert clear_callback.fn(request)[:3] == ("", "", "")
        return secret

    monkeypatch.setattr("karaoke_forge.web.capture_netease_music_u", capture_then_logout)
    login_generation = begin_callback.fn(request)[0]
    result = capture_callback.fn(login_generation, request)

    assert result[0] == ""
    assert "登录结果已安全丢弃" in result[3]
    assert secret not in repr(result)


def test_expired_saved_netease_session_is_acquired_only_when_audio_is_needed(
    monkeypatch,
    tmp_path,
) -> None:
    app = create_web_app(managed_netease_login=True)
    callback = next(
        block_function
        for block_function in app.fns.values()
        if getattr(block_function.fn, "__name__", "")
        == "ensure_netease_session_for_download"
    )
    calls: list[str] = []
    monkeypatch.setattr("karaoke_forge.web.managed_netease_profile_exists", lambda: True)

    def acquire() -> str:
        calls.append("acquire")
        return "renewed-server-only-secret"

    monkeypatch.setattr("karaoke_forge.web.acquire_netease_music_u", acquire)
    local_request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))

    result = callback.fn(
        "https://music.163.com/song?id=42",
        None,
        None,
        True,
        "",
        local_request,
    )
    assert result[0] == "renewed-server-only-secret"
    assert "自动确认 / 重新连接" in result[1]
    assert "renewed-server-only-secret" not in repr(result[1:])

    audio = tmp_path / "local.wav"
    audio.write_bytes(b"audio")
    skipped = callback.fn(
        "https://music.163.com/song?id=42",
        str(audio),
        None,
        True,
        "",
        local_request,
    )
    assert skipped[0] == ""

    already_connected = callback.fn(
        "https://music.163.com/song?id=42",
        None,
        None,
        True,
        "renewed-server-only-secret",
        local_request,
    )
    assert already_connected[0] == "renewed-server-only-secret"

    manually_supplied = callback.fn(
        "https://music.163.com/song?id=42",
        None,
        None,
        True,
        "manual-session-token",
        local_request,
    )
    assert manually_supplied[0] == "manual-session-token"
    assert calls == ["acquire", "acquire"]


def test_environment_help_prefers_one_click_dedicated_edge_login() -> None:
    source = inspect.getsource(create_web_app)
    help_text = source.split("### 第一次使用", 1)[1].split('"""', 1)[0]

    assert "一键登录" in help_text
    assert "专用 Edge" in help_text
    assert "可自动读取已退出的登录浏览器" not in help_text
    assert "保持开启时可粘贴 MUSIC_U" not in help_text


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
    assert ".kf-live-karaoke-measure" in TOKEN_TIMELINE_JS
    assert ".kf-karaoke-token-core" in TOKEN_TIMELINE_JS
    assert "__karaokeForgeTokenAuditionGuardUntil" in EDITOR_STOP_GATE_JS
    assert "__karaokeForgeSuppressAutoAdvanceUntil" not in EDITOR_STOP_GATE_JS
    assert "const stoppedLine = Number(args[3])" in EDITOR_STOP_GATE_JS
    assert "currentLine !== stoppedLine" in EDITOR_STOP_GATE_JS
    assert "__karaokeForgeEditorMutationGuardLine" in EDITOR_STOP_GATE_JS
    assert '"#editor-current-line input"' in EDITOR_STOP_GATE_JS
    assert "window.setTimeout(resolve, 55)" in EDITOR_STOP_GATE_JS


def test_auto_advance_skips_hidden_blank_and_untimed_lines() -> None:
    document = LyricsDocument(
        lines=[
            LyricLine("first", 1.0, 2.0),
            LyricLine("", 2.0, 3.0),
            LyricLine("   ", None, None),
            LyricLine("hidden", 3.0, 4.0, hidden=True),
            LyricLine("next", 4.0, 5.0),
        ]
    )

    assert _next_playable_editor_line(document, 1) == 5
    assert _next_playable_editor_line(document, 5) is None


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
    assert "模型下载" in report
    assert "网易云一键登录组件" in report
    assert "素材不会自动上传到公网" in report


def test_web_model_network_requires_explicit_mirror_consent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(tmp_path))

    status = configure_model_network_for_web("mirror", "", False)

    assert "没有保存" in status
    assert "显式确认" in status
    assert not (tmp_path / "model-download.json").exists()


def test_web_model_auto_detection_never_falls_back_to_a_mirror(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(tmp_path))
    monkeypatch.setattr("karaoke_forge.web.auto_detect_local_proxies", lambda: ())
    monkeypatch.setattr(
        "karaoke_forge.web.test_model_download_network",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=False,
            detail_zh="official blocked",
        ),
    )

    status, mode, _proxy, confirmed = auto_configure_model_network_for_web()

    assert "不会自动切换到未校验镜像" in status
    assert mode == "modelscope"
    assert not confirmed
    assert not (tmp_path / "model-download.json").exists()


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

    # Clearing the text is a common precursor to using the explicit delete
    # button. The delete action must not be blocked by the empty-text guard.
    insert_undone[1][1][4] = ""
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
    monkeypatch.setattr(
        "karaoke_forge.web.find_runtime_executable",
        lambda _name: "ffmpeg",
    )

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


def test_web_editor_rejects_an_empty_audio_placeholder(
    tmp_path: Path,
    monkeypatch,
) -> None:
    audio = tmp_path / "audio-song.wav"
    audio.write_bytes(b"audio")
    document = parse_yrc("[1000,1000](1000,1000,0)Hello\n")
    monkeypatch.setattr(
        "karaoke_forge.web.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("FFmpeg must not receive an empty browser placeholder")
        ),
    )

    with pytest.raises(ValueError, match="空占位文件"):
        preview_editor_audio_line(
            str(audio),
            document.to_dict(),
            document_to_editor_rows(document),
            1,
        )


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


def test_make_page_downloads_logged_in_netease_audio_for_calibration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    music_u = "manual-session-secret"
    placeholder = tmp_path / "audio-song.wav"
    placeholder.write_bytes(b"audio")
    video = tmp_path / "silent-mv.webm"
    video.write_bytes(b"video without audio")
    downloaded = tmp_path / "account-track.m4a"
    downloaded.write_bytes(b"downloaded account audio")
    track = NeteaseTrack(
        song_id="3318995013",
        title="h2o.wav",
        artists=("CVLTE", "TSS"),
        canonical_url="https://music.163.com/song?id=3318995013",
        audio_path=downloaded,
        page_lyrics="[00:01.00]First\n[00:03.00]\n[00:05.00]Third\n",
        authenticated=True,
        quality_level="lossless",
        access_tier="vip",
    )
    monkeypatch.setattr("karaoke_forge.web.probe_media_has_audio", lambda _path: False)

    def fake_download(_link, _output_dir, **kwargs):
        assert kwargs["cookie_browser"] == "chrome"
        assert kwargs["cookie_browser_profile"] == "Default"
        assert kwargs["music_u"] == music_u
        return track

    monkeypatch.setattr("karaoke_forge.web.download_netease_track", fake_download)

    result = prepare_make_editor_job(
        str(placeholder),
        str(video),
        None,
        "",
        "vip-calibration",
        "ja",
        "small",
        "auto",
        False,
        netease_link=track.canonical_url,
        use_netease_lyrics=True,
        rights_confirmed=True,
        timing_refinement="off",
        output_root=str(tmp_path / "outputs"),
        auto_english_pronunciation=False,
        cookie_browser="chrome",
        cookie_browser_profile="Default",
        music_u=music_u,
    )

    assert result.project is not None
    assert result.audio == str(downloaded)
    assert len(result.rows) == 3
    assert result.rows[1][4] == ""
    assert "已忽略网页产生的空音频占位文件" in result.log
    assert "已使用网易云登录账号可播放的完整音频" in result.log
    assert music_u not in result.log
    assert music_u not in Path(result.project).read_text(encoding="utf-8")


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


def test_make_page_prepares_editor_with_audio_and_cover_but_no_mv(tmp_path: Path) -> None:
    audio = tmp_path / "song.wav"
    cover = tmp_path / "cover.jpg"
    lyrics = tmp_path / "lyrics.lrc"
    audio.write_bytes(b"audio")
    cover.write_bytes(b"image")
    lyrics.write_text("[00:01.00]Hello world\n", encoding="utf-8")

    result = prepare_make_editor_job(
        str(audio),
        None,
        str(lyrics),
        "",
        "cover-project",
        "自动识别",
        "small",
        "auto",
        False,
        timing_refinement="off",
        output_root=str(tmp_path / "outputs"),
        cover_file=str(cover),
        cover_background="ocean",
    )

    assert result.project is not None
    workspace = load_workspace_project(Path(result.project).parent / "karaoke-forge-project.json")
    assert workspace.settings["cover_background"] == "ocean"
    assert workspace.settings["alignment_language"] == "自动识别"
    assert workspace.settings["alignment_model"] == "small"
    assert workspace.settings["alignment_device"] == "auto"
    assert workspace.settings["alignment_separate_vocals"] is False
    assert workspace.settings["timing_refinement"] == "off"
    assert "音频、MV/封面和字体已保存" in result.status


def test_make_job_renders_cover_mode_and_custom_font_without_mv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    audio = tmp_path / "song.wav"
    cover = tmp_path / "cover.jpg"
    font = tmp_path / "pretty.otf"
    lyrics = tmp_path / "lyrics.lrc"
    for path in (audio, cover, font):
        path.write_bytes(b"asset")
    lyrics.write_text("[00:01.00]Hello world\n", encoding="utf-8")

    def fake_make(_audio, video, source, output, assets, *, options, **_kwargs):
        assert video is None
        assert options.cover_image == cover
        assert options.font_files == (font,)
        assert options.cover_background == "paper"
        assert options.cover_style == "spectrum"
        assert options.cover_waveform is False
        output = Path(output)
        output.write_bytes(b"rendered")
        assets = Path(assets)
        assets.mkdir(parents=True)
        exported = assets / "lyrics.json"
        document = read_lyrics(source)
        exported.write_text(json.dumps(document.to_dict()), encoding="utf-8")
        return SimpleNamespace(
            video=output,
            exports={"json": exported},
            document=document,
            alignment_report=None,
            sync_result=None,
        )

    monkeypatch.setattr("karaoke_forge.web.make_karaoke_video", fake_make)
    result = run_make_job(
        str(audio),
        None,
        str(lyrics),
        "",
        "cover-karaoke",
        "自动识别",
        "small",
        "auto",
        False,
        "快速预览",
        0.0,
        "Pretty",
        58,
        "#FFFFFF",
        "#FFD54A",
        72,
        timing_refinement="off",
        output_root=str(tmp_path / "outputs"),
        cover_file=str(cover),
        font_files=[str(font)],
        cover_background="paper",
        cover_style="spectrum",
        cover_waveform=False,
    )

    assert result.video is not None
    assert "旋转专辑封面" in result.status


def test_make_job_can_return_original_and_instrumental_downloads(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.wav"
    video = tmp_path / "mv.mp4"
    lyrics = tmp_path / "lyrics.lrc"
    for path in (audio, video):
        path.write_bytes(b"media")
    lyrics.write_text("[00:01.00]Hello world\n", encoding="utf-8")

    def fake_make(_audio, _video, source, output, assets, *, options, **_kwargs):
        assert options.export_original is True
        assert options.export_instrumental is True
        output = Path(output)
        instrumental = output.with_name(f"{output.stem}-instrumental.mp4")
        output.write_bytes(b"original")
        instrumental.write_bytes(b"instrumental")
        assets = Path(assets)
        assets.mkdir(parents=True)
        exported = assets / "lyrics.json"
        document = read_lyrics(source)
        exported.write_text(json.dumps(document.to_dict()), encoding="utf-8")
        return SimpleNamespace(
            video=output,
            videos={"original": output, "instrumental": instrumental},
            exports={"json": exported},
            document=document,
            alignment_report=None,
            sync_result=None,
        )

    monkeypatch.setattr("karaoke_forge.web.make_karaoke_video", fake_make)
    result = run_make_job(
        str(audio),
        str(video),
        str(lyrics),
        "",
        "dual-karaoke",
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
        timing_refinement="off",
        output_root=str(tmp_path / "outputs"),
        export_original=True,
        export_instrumental=True,
    )

    assert "原声版 + 无人声伴奏版" in result.status
    assert any(path.endswith("dual-karaoke.mp4") for path in result.files)
    assert any(path.endswith("dual-karaoke-instrumental.mp4") for path in result.files)


def test_make_job_rejects_empty_final_video_selection(tmp_path) -> None:
    result = run_make_job(
        None,
        None,
        None,
        "",
        "empty",
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
        output_root=str(tmp_path),
        export_original=False,
        export_instrumental=False,
    )

    assert result.video is None
    assert "至少选择" in result.status


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


def test_subtitle_preview_sample_matches_ass_rows_and_token_progress() -> None:
    document = LyricsDocument(
        lines=[
            LyricLine(text="Before", start=0.0, end=2.0),
            LyricLine(
                text="holdme",
                start=2.0,
                end=6.0,
                translation="抱紧我",
                tokens=[
                    KaraokeToken(text="hold", start=2.0, end=3.0),
                    KaraokeToken(text="me", start=5.0, end=6.0),
                ],
            ),
            LyricLine(text="Next line", start=6.0, end=8.0),
            LyricLine(text="Later", start=8.0, end=10.0),
        ]
    )

    sample = _select_subtitle_preview_sample(document)

    assert sample.text == "Next line\nholdme"
    assert sample.translation == "抱紧我"
    assert sample.timestamp == pytest.approx(3.6)
    assert sample.highlight_progress == pytest.approx(4 / 6)
    assert sample.active_row == 1
    assert "逐字" in sample.description


def test_subtitle_preview_embeds_material_frame_and_preserves_row_parity() -> None:
    preview = subtitle_preview_html(
        "Microsoft YaHei",
        64,
        "#FFFFFF",
        "#FFD54A",
        80,
        True,
        36,
        "#EAF4FF",
        False,
        26,
        "#FFFFFF",
        "Upper line\nLower line",
        "",
        background_data_url="data:image/jpeg;base64,anBlZw==",
        preview_badge="MV <01:20>",
        material_mode=True,
        highlight_progress=0.5,
        active_row=0,
    )

    assert 'class="kf-preview-background"' in preview
    assert "data:image/jpeg;base64,anBlZw==" in preview
    assert 'data-kf-material="true"' in preview
    assert "MV &lt;01:20&gt;" in preview
    assert "让歌声与画面在这里相遇" not in preview
    assert '<span style="color:#FFD54A;">Upper</span>' in preview
    assert '<span style="color:#FFD54A;">Lower</span>' not in preview


def test_material_preview_prefers_mv_frame_and_real_lyrics(tmp_path, monkeypatch) -> None:
    video = tmp_path / "mv.mp4"
    lyrics = tmp_path / "lyrics.json"
    frame = tmp_path / "frame.jpg"
    video.write_bytes(b"video")
    frame.write_bytes(b"jpeg")
    document = LyricsDocument(
        lines=[
            LyricLine(text="First", start=0.0, end=2.0),
            LyricLine(text="Current", start=2.0, end=4.0, translation="现在"),
            LyricLine(text="Next", start=4.0, end=6.0),
        ]
    )
    lyrics.write_text(json.dumps(document.to_dict(), ensure_ascii=False), encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_mv(source, timestamp, offset):
        captured.update(source=source, timestamp=timestamp, offset=offset)
        return frame, float(timestamp) + float(offset)

    monkeypatch.setattr("karaoke_forge.web._cached_video_preview_frame", fake_mv)
    monkeypatch.setattr(
        "karaoke_forge.web._cached_cover_preview_frame",
        lambda *_args, **_kwargs: pytest.fail("MV preview must win over cover preview"),
    )

    result = prepare_subtitle_material_preview(
        None,
        str(video),
        None,
        str(lyrics),
        "",
        1.25,
        False,
        "adaptive",
        "turntable",
        True,
    )

    assert captured["source"] == video
    assert captured["offset"] == 1.25
    assert "Current" in result[0]
    assert result[1] == "现在"
    assert result[2].startswith("data:image/jpeg;base64,")
    assert "MV 实景" in result[3]
    assert result[4] is True
    assert "对应歌词时刻" in result[7]


def test_material_preview_reuses_selected_no_mv_scene(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.wav"
    cover = tmp_path / "cover.jpg"
    frame = tmp_path / "frame.jpg"
    audio.write_bytes(b"audio")
    cover.write_bytes(b"image")
    frame.write_bytes(b"jpeg")
    captured: dict[str, object] = {}

    def fake_cover(source_cover, source_audio, timestamp, theme, style, waveform):
        captured.update(
            cover=source_cover,
            audio=source_audio,
            timestamp=timestamp,
            theme=theme,
            style=style,
            waveform=waveform,
        )
        return frame, 12.0

    monkeypatch.setattr("karaoke_forge.web._cached_cover_preview_frame", fake_cover)

    result = prepare_subtitle_material_preview(
        str(audio),
        None,
        str(cover),
        None,
        "First line\nSecond line\nThird line",
        0.0,
        False,
        "ocean",
        "spectrum",
        False,
    )

    assert captured["cover"] == cover
    assert captured["audio"] == audio
    assert captured["theme"] == "ocean"
    assert captured["style"] == "spectrum"
    assert captured["waveform"] is False
    assert result[2].startswith("data:image/jpeg;base64,")
    assert "无 MV 成片样式" in result[3]
    assert "当前无 MV 主题" in result[7]


def test_netease_link_only_previews_selected_virtual_mv_scene(tmp_path, monkeypatch) -> None:
    cover = tmp_path / "online-cover.jpg"
    frame = tmp_path / "virtual-scene.jpg"
    cover.write_bytes(b"cover")
    frame.write_bytes(b"jpeg")
    captured: dict[str, object] = {}
    info = NeteaseSongInfo(
        song_id="42",
        title="Linked Song",
        artists=("Artist",),
        canonical_url="https://music.163.com/song?id=42",
        page_lyrics="[00:00.00]First line\n[00:03.00]Second line",
        translated_lyrics="[00:00.00]第一行\n[00:03.00]第二行",
        cover_url="https://p1.music.126.net/cover.jpg",
    )

    metadata_calls: list[str] = []

    def fetch_info(link: str):
        metadata_calls.append(link)
        return info

    _cached_public_netease_preview_info.cache_clear()
    monkeypatch.setattr("karaoke_forge.web.fetch_public_netease_info", fetch_info)
    monkeypatch.setattr("karaoke_forge.web._cached_online_preview_cover", lambda _url: cover)

    def fake_cover(source_cover, source_audio, timestamp, theme, style, waveform):
        captured.update(
            cover=source_cover,
            audio=source_audio,
            timestamp=timestamp,
            theme=theme,
            style=style,
            waveform=waveform,
        )
        return frame, 0.65

    monkeypatch.setattr("karaoke_forge.web._cached_cover_preview_frame", fake_cover)

    result = prepare_subtitle_material_preview(
        None,
        None,
        None,
        None,
        "",
        0.0,
        True,
        "sunset",
        "vinyl",
        True,
        "https://music.163.com/song?id=42",
    )

    assert captured["cover"] == cover
    assert captured["audio"] is None
    assert captured["theme"] == "sunset"
    assert captured["style"] == "vinyl"
    assert captured["waveform"] is True
    assert "First line" in result[0] or "Second line" in result[0]
    assert result[2].startswith("data:image/jpeg;base64,")
    assert "无 MV 成片样式" in result[3]
    assert "波形布局示意" in result[3]
    assert "专辑封面静态预览" not in result[3]
    assert "真实音乐波形" in result[7]

    prepare_subtitle_material_preview(
        None,
        None,
        None,
        None,
        "",
        0.0,
        True,
        "ocean",
        "halo",
        False,
        "https://music.163.com/song?id=42",
    )
    assert metadata_calls == ["https://music.163.com/song?id=42"]
    _cached_public_netease_preview_info.cache_clear()


def test_material_preview_accepts_none_for_optional_online_links() -> None:
    result = prepare_subtitle_material_preview(
        None,
        None,
        None,
        None,
        "",
        0.0,
        True,
        "adaptive",
        "turntable",
        True,
        None,
        None,
        None,
    )

    assert len(result) == 8
    assert result[4] is False


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


def test_downloaded_netease_audio_is_saved_before_temporary_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"image")
    downloaded: list[Path] = []

    def fake_download(_link, output_dir, **_kwargs):
        audio = Path(output_dir) / "audio.m4a"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"complete audio")
        downloaded.append(audio)
        return NeteaseTrack(
            song_id="42",
            title="Downloaded Song",
            artists=("Artist",),
            canonical_url="https://music.163.com/song?id=42",
            audio_path=audio,
            page_lyrics="[00:01.00]Hello\n",
            is_preview=False,
        )

    def fake_make(_audio, _video, lyrics, output, assets, **_kwargs):
        document = read_lyrics(lyrics)
        output = Path(output)
        output.write_bytes(b"rendered")
        assets = Path(assets)
        assets.mkdir(parents=True)
        exported = assets / "lyrics.json"
        exported.write_text(json.dumps(document.to_dict()), encoding="utf-8")
        return SimpleNamespace(
            video=output,
            exports={"json": exported},
            document=document,
            alignment_report=None,
            sync_result=None,
        )

    monkeypatch.setattr("karaoke_forge.web.download_netease_track", fake_download)
    monkeypatch.setattr("karaoke_forge.web.make_karaoke_video", fake_make)

    result = run_make_job(
        None,
        None,
        None,
        "",
        "downloaded-project",
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
        netease_link="https://music.163.com/song?id=42",
        rights_confirmed=True,
        timing_refinement="off",
        cover_file=str(cover),
    )

    manifest = next(
        Path(path) for path in result.files if path.endswith("karaoke-forge-project.json")
    )
    workspace = load_workspace_project(manifest)
    assert result.video is not None
    assert workspace.audio is not None and workspace.audio.read_bytes() == b"complete audio"
    assert downloaded and not downloaded[0].exists()
    assert "已保存进工程" in result.log
