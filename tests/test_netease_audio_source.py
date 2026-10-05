from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from karaoke_forge import web
from karaoke_forge.formats import read_lyrics, write_json
from karaoke_forge.netease import NeteaseTrack
from karaoke_forge.projects import PROJECT_FILENAME, load_workspace_project
from karaoke_forge.web import prepare_make_editor_job, run_make_job


@pytest.fixture
def media(tmp_path, monkeypatch):
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    video = tmp_path / "independent-visual.mp4"
    video.write_bytes(b"video with unrelated audio")
    downloads = []

    def download(_link, destination, **_kwargs):
        audio = Path(destination) / "online-song.m4a"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"selected online song")
        downloads.append(audio)
        return NeteaseTrack(
            song_id="42", title="Selected song", artists=("Artist",),
            canonical_url="https://music.163.com/song?id=42", audio_path=audio,
            page_lyrics="[00:01.00]Song\n[00:03.00]Again\n",
        )

    def no_video_audio(_video):
        raise AssertionError("Independent visual audio must not be inspected or adopted")

    monkeypatch.setattr("karaoke_forge.web.download_netease_track", download)
    monkeypatch.setattr("karaoke_forge.web.probe_media_has_audio", no_video_audio)
    return video, downloads


def fake_render(audio, video, lyrics, output, assets, **_kwargs):
    assert Path(audio).read_bytes() == b"selected online song"
    assert Path(video).read_bytes() == b"video with unrelated audio"
    output = Path(output)
    output.write_bytes(b"rendered")
    assets = Path(assets)
    assets.mkdir(parents=True)
    document = read_lyrics(lyrics)
    exported = assets / "lyrics.json"
    exported.write_text(write_json(document), encoding="utf-8")
    return SimpleNamespace(
        video=output, exports={"json": exported}, document=document,
        alignment_report=None, sync_result=None,
    )


def render_settings(video, tmp_path):
    return {
        "audio_file": None, "video_file": str(video), "lyrics_file": None, "pasted_lyrics": "",
        "output_name": "Online song", "language": "en", "model": "small", "device": "auto",
        "separate_vocals": False, "quality": "快速预览", "audio_offset": 0, "font": "Arial",
        "font_size": 58, "text_color": "#FFFFFF", "highlight_color": "#FFD54A", "margin_v": 72,
        "netease_link": "https://music.163.com/song?id=42", "rights_confirmed": True,
        "timing_refinement": "off", "output_root": str(tmp_path / "outputs"),
        "prefer_netease_audio": True, "auto_sync": False,
    }


def test_explicit_online_audio_preparation_archives_download_for_later_render(media, tmp_path, monkeypatch):
    video, downloads = media
    result = prepare_make_editor_job(
        None, str(video), None, "", "Online song", "en", "small", "auto", False,
        netease_link="https://music.163.com/song?id=42", rights_confirmed=True,
        prefer_netease_audio=True, timing_refinement="off", auto_english_pronunciation=False,
        output_root=str(tmp_path / "prepared"),
    )
    assert result.project is not None, result.log
    assert result.audio == str(downloads[0])
    workspace = load_workspace_project(Path(result.output_dir) / PROJECT_FILENAME)
    assert workspace.settings["prefer_netease_audio"] is True
    assert workspace.audio.read_bytes() == b"selected online song"
    assert workspace.video.read_bytes() == b"video with unrelated audio"
    monkeypatch.setattr("karaoke_forge.web.make_karaoke_video", fake_render)
    values = render_settings(workspace.video, tmp_path)
    values.update(audio_file=str(workspace.audio), lyrics_file=str(workspace.lyrics_project), netease_link="")
    rendered = run_make_job(**values)
    assert rendered.video is not None, rendered.log
    assert len(downloads) == 1


def test_explicit_online_audio_direct_render_preserves_download_and_independent_picture(media, tmp_path, monkeypatch):
    video, downloads = media
    monkeypatch.setattr("karaoke_forge.web.make_karaoke_video", fake_render)
    result = run_make_job(**render_settings(video, tmp_path))
    assert result.video is not None, result.log
    workspace = load_workspace_project(Path(result.output_dir) / PROJECT_FILENAME)
    assert workspace.settings["prefer_netease_audio"] is True
    assert workspace.audio.read_bytes() == b"selected online song"
    assert workspace.video.read_bytes() == b"video with unrelated audio"
    assert len(downloads) == 1


@pytest.mark.parametrize("job", ["prepare", "render"])
def test_explicit_online_audio_rejects_preview_without_using_independent_video_sound(media, tmp_path, monkeypatch, job):
    video, downloads = media
    original_download = web.download_netease_track
    monkeypatch.setattr(
        web, "download_netease_track",
        lambda *args, **kwargs: replace(original_download(*args, **kwargs), is_preview=True),
    )
    if job == "prepare":
        result = prepare_make_editor_job(
            None, str(video), None, "", "Online song", "en", "small", "auto", False,
            netease_link="https://music.163.com/song?id=42", rights_confirmed=True,
            prefer_netease_audio=True, output_root=str(tmp_path / "outputs"),
        )
        assert result.project is None
    else:
        result = run_make_job(**render_settings(video, tmp_path))
        assert result.video is None
    assert "试听片段" in result.status
    assert video.read_bytes() == b"video with unrelated audio"
    assert len(downloads) == 1 and not downloads[0].exists()
