from concurrent.futures import ThreadPoolExecutor

from karaoke_forge.projects import (
    PROJECT_FILENAME,
    list_workspace_projects,
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


def test_workspace_catalog_lists_valid_projects_newest_first_and_skips_broken(tmp_path) -> None:
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    first_lyrics = uploads / "first.json"
    second_lyrics = uploads / "second.json"
    first_lyrics.write_text('{"version": 1, "lines": []}\n', encoding="utf-8")
    second_lyrics.write_text('{"version": 1, "lines": []}\n', encoding="utf-8")
    first = save_workspace_project(
        tmp_path / "first",
        name="First song",
        lyrics_project=first_lyrics,
        recent_root=tmp_path,
    )
    second = save_workspace_project(
        tmp_path / "second",
        name="Second song",
        lyrics_project=second_lyrics,
        recent_root=tmp_path,
    )
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / PROJECT_FILENAME).write_text("{broken", encoding="utf-8")

    projects = list_workspace_projects(tmp_path)

    assert [project.manifest for project in projects] == [second.manifest, first.manifest]


def test_workspace_catalog_keeps_a_recent_project_outside_the_default_root(tmp_path) -> None:
    root = tmp_path / "default"
    outside = tmp_path / "custom" / "song"
    lyrics = tmp_path / "song.json"
    lyrics.write_text('{"version": 1, "lines": []}\n', encoding="utf-8")
    saved = save_workspace_project(
        outside,
        name="Custom output",
        lyrics_project=lyrics,
        recent_root=root,
    )

    assert [project.manifest for project in list_workspace_projects(root)] == [saved.manifest]


def test_workspace_catalog_remembers_multiple_custom_output_projects(tmp_path) -> None:
    root = tmp_path / "default"
    lyrics = tmp_path / "song.json"
    lyrics.write_text('{"version": 1, "lines": []}\n', encoding="utf-8")
    first = save_workspace_project(
        tmp_path / "custom-a" / "song",
        name="Custom A",
        lyrics_project=lyrics,
        recent_root=root,
    )
    second = save_workspace_project(
        tmp_path / "custom-b" / "song",
        name="Custom B",
        lyrics_project=lyrics,
        recent_root=root,
    )

    manifests = [project.manifest for project in list_workspace_projects(root)]

    assert manifests == [second.manifest, first.manifest]


def test_workspace_catalog_keeps_all_concurrent_saves(tmp_path) -> None:
    root = tmp_path / "default"
    lyrics = tmp_path / "song.json"
    lyrics.write_text('{"version": 1, "lines": []}\n', encoding="utf-8")

    def save(index: int):
        return save_workspace_project(
            tmp_path / f"custom-{index}" / "song",
            name=f"Song {index}",
            lyrics_project=lyrics,
            recent_root=root,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        saved = list(executor.map(save, range(20)))

    manifests = {project.manifest for project in list_workspace_projects(root)}
    assert manifests == {project.manifest for project in saved}
    assert not list(tmp_path.rglob(".*.tmp"))


def test_workspace_manifest_cannot_reference_files_outside_its_directory(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"version": 1, "lines": []}\n', encoding="utf-8")
    manifest = project / PROJECT_FILENAME
    manifest.write_text(
        '{"schema_version": 1, "name": "Unsafe", "lyrics_project": "../outside.json"}',
        encoding="utf-8",
    )

    try:
        load_workspace_project(manifest)
    except ValueError as exc:
        assert "工程目录内" in str(exc)
    else:
        raise AssertionError("An escaping manifest path should have been rejected.")
