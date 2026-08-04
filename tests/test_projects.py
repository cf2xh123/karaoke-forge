from karaoke_forge.projects import (
    load_recent_workspace,
    load_workspace_project,
    save_workspace_project,
)


def test_workspace_project_persists_assets_and_recent_pointer(tmp_path) -> None:
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    lyrics = uploads / "song.json"
    audio = uploads / "song.wav"
    cover = uploads / "cover.jpg"
    font = uploads / "pretty.otf"
    lyrics.write_text('{"version": 1, "lines": []}\n', encoding="utf-8")
    for path in (audio, cover, font):
        path.write_bytes(path.name.encode())

    saved = save_workspace_project(
        tmp_path / "projects" / "song",
        name="My Song",
        lyrics_project=lyrics,
        audio=audio,
        cover=cover,
        font_files=(font,),
        settings={"font": "Pretty"},
        recent_root=tmp_path,
    )

    assert saved.manifest.is_file()
    assert saved.audio is not None and saved.audio.parent.name == "project-assets"
    assert saved.cover is not None and saved.cover.is_file()
    assert saved.font_files[0].is_file()
    assert load_workspace_project(saved.manifest).settings == {"font": "Pretty"}
    assert load_recent_workspace(tmp_path) == saved
