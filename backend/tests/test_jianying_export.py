"""Real Jianying-draft export: the artifact, its JSON, skips, and every fallback path.

Nothing here claims a generated draft was opened in Jianying — no Jianying exists in this
environment. These tests verify exactly what was produced (a readable ``draft_content.json`` with
the right canvas, timing, and absolute material paths) and that every way the library can let the
user down degrades to the honest portable draft package instead of a failed task.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.jianying import (
    DEFAULT_SHOT_DURATION_SEC,
    LIBRARY_NAME,
    SEC,
    _classify_asset,
    _pick_image_assets,
    library_available,
    library_version,
    sanitize_draft_name,
)
from backend.main import create_app
from backend.tests.jianying_support import (
    add_video_asset,
    build_timeline,
    fake_library,
    generate_demo_media,
    project_with_shots,
    register_png_images,
    register_svg_images,
)

ILLEGAL_NAME_CHARS = '\\/:*?"<>|'
PNG_MIME_TYPES = ("image/png", "image/jpeg", "image/webp")


def _projects_root(database) -> Path:
    return Path(database.path.parent) / "projects"


def _project_dir(database, project_id: str) -> Path:
    return _projects_root(database) / project_id


def _request_export(client: TestClient, project_id: str, fmt: str = "jianying_draft") -> dict:
    response = client.post(f"/api/projects/{project_id}/exports", json={"format": fmt})
    assert response.status_code == 201, response.text
    return response.json()


def _archive_path(database, project_id: str, export: dict) -> Path:
    artifact = export["artifacts"][0]
    return _project_dir(database, project_id) / artifact["relative_path"]


def _draft_json(archive: Path, name: str = "draft_content.json") -> dict:
    with zipfile.ZipFile(archive) as package:
        members = [item for item in package.namelist() if item.endswith(name) and "/" in item]
        assert members, f"{name} missing from {package.namelist()}"
        return json.loads(package.read(members[0]).decode("utf-8"))


def _draft_info(archive: Path) -> dict:
    """The zip's own record of what was built, including the user's draft folder."""
    with zipfile.ZipFile(archive) as package:
        return json.loads(package.read("jianying-draft-info.json").decode("utf-8"))


def _tracks(content: dict) -> dict[str, dict]:
    return {track["type"]: track for track in content["tracks"]}


def _placed_shots(database, project_id: str) -> list[tuple[str, int]]:
    """``(shot_id, duration_sec)`` for the shots the rules actually place on the video track.

    An independent implementation of the same rule the builder uses, so a bug in the builder's own
    filter cannot make this expectation agree with it by construction.
    """
    with database.connect() as connection:
        shots = [dict(row) for row in connection.execute(
            """SELECT sh.id,sh.duration_sec FROM shots sh JOIN scenes sc ON sc.id=sh.scene_id
               JOIN episodes e ON e.id=sc.episode_id
               WHERE e.project_id=? AND e.version=(SELECT MAX(version) FROM episodes WHERE project_id=?)
               ORDER BY e.position,sc.position,sh.position""", (project_id, project_id)).fetchall()]
        rows = [dict(row) for row in connection.execute(
            """SELECT a.kind,a.source_shot_id shot_id,av.relative_path,av.mime_type
               FROM assets a JOIN asset_versions av ON av.asset_id=a.id
               WHERE a.project_id=? AND av.version=(SELECT MAX(v2.version) FROM asset_versions v2
                                                     WHERE v2.asset_id=a.id)""", (project_id,)).fetchall()]
    placed: list[tuple[str, int]] = []
    for shot in shots:
        candidates = [row for row in rows if row["shot_id"] == shot["id"]]
        kinds = [_classify_asset(row["mime_type"], row["relative_path"]) for row in candidates]
        if not any(kind == "bitmap" or (row["kind"] == "video" and kind == "video")
                   for row, kind in zip(candidates, kinds)):
            continue
        duration = int(shot["duration_sec"])
        placed.append((str(shot["id"]), duration if duration > 0 else int(DEFAULT_SHOT_DURATION_SEC)))
    return placed


# --- 1 & 2: the real artifact and its JSON ---------------------------------------------------


def test_jianying_draft_export_produces_an_artifact_with_a_readable_draft(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "jianying.db"
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "no-ffmpeg"))
    register_png_images(monkeypatch)
    with TestClient(create_app(path)) as client:
        project_id = project_with_shots(client, name="剪映草稿", target_duration_sec=8)
        generate_demo_media(client, project_id)
        build_timeline(client, project_id)
        export = _request_export(client, project_id)

        assert export["status"] == "completed", export["warning"]
        assert export["format"] == "jianying_draft"
        artifact = export["artifacts"][0]
        assert artifact["kind"] == "jianying_draft"
        assert artifact["mime_type"] == "application/zip"
        archive = _archive_path(client.app.state.database, project_id, export)
        assert archive.is_file() and archive.suffix == ".zip"
        with zipfile.ZipFile(archive) as package:
            names = package.namelist()
            assert any(name.endswith("/draft_content.json") for name in names)
            assert any(name.endswith("/draft_meta_info.json") for name in names)
            assert "jianying-draft-info.json" in names
            info = json.loads(package.read("jianying-draft-info.json").decode("utf-8"))
        assert info["library"] == LIBRARY_NAME
        assert info["library_version"] == library_version()
        assert "未在真实剪映中验证" in info["notes"]["unverified"]
        assert "绝对路径" in info["notes"]["asset_paths"]
        # The draft lives in a real directory too, not only inside the zip.
        draft_files = list((_project_dir(client.app.state.database, project_id) / "exports").rglob("draft_content.json"))
        assert len(draft_files) == 1 and draft_files[0].is_file()


def test_draft_canvas_duration_and_tracks_are_contiguous(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "jianying_timing.db"
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "no-ffmpeg"))
    monkeypatch.setenv("VMAKER_JIANYING_DRAFTS_DIR", "")  # default: export copy only
    register_png_images(monkeypatch)
    with TestClient(create_app(path)) as client:
        project_id = project_with_shots(client, name="横屏项目", target_duration_sec=10, aspect_ratio="16:9")
        generate_demo_media(client, project_id)
        build_timeline(client, project_id)
        database = client.app.state.database
        placed = _placed_shots(database, project_id)
        assert len(placed) >= 2, "the fixture must produce a multi-shot project"

        export = _request_export(client, project_id)
        content = _draft_json(_archive_path(database, project_id, export))

        # 16:9 -> 1920x1080, and the duration is exactly the sum of the placed shots.
        assert content["canvas_config"]["width"] == 1920
        assert content["canvas_config"]["height"] == 1080
        assert content["fps"] == 30
        assert content["duration"] == sum(duration for _, duration in placed) * SEC

        video = _tracks(content)["video"]
        assert len(video["segments"]) == len(placed)
        cursor = 0
        for segment, (_, duration) in zip(video["segments"], placed):
            timerange = segment["target_timerange"]
            assert timerange["start"] == cursor, "video segments must be back to back with no gap"
            assert timerange["duration"] == duration * SEC
            cursor = timerange["start"] + timerange["duration"]
        assert cursor == content["duration"]


def test_portrait_and_square_canvas_follow_the_project_aspect_ratio(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "no-ffmpeg"))
    register_png_images(monkeypatch)
    expected = {"9:16": (1080, 1920), "1:1": (1080, 1080)}
    with TestClient(create_app(tmp_path / "canvas.db")) as client:
        database = client.app.state.database
        for aspect_ratio, (width, height) in expected.items():
            project_id = project_with_shots(client, name=f"画布{aspect_ratio.replace(':', '-')}",
                                            target_duration_sec=4, aspect_ratio=aspect_ratio)
            generate_demo_media(client, project_id)
            build_timeline(client, project_id)
            export = _request_export(client, project_id)
            content = _draft_json(_archive_path(database, project_id, export))
            assert content["canvas_config"]["width"] == width
            assert content["canvas_config"]["height"] == height


# --- 3: absolute, existing material paths ----------------------------------------------------


def test_material_paths_are_absolute_existing_project_assets(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "jianying_paths.db"
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "no-ffmpeg"))
    register_png_images(monkeypatch)
    with TestClient(create_app(path)) as client:
        project_id = project_with_shots(client, name="素材路径", target_duration_sec=8)
        generate_demo_media(client, project_id)
        build_timeline(client, project_id)
        database = client.app.state.database
        assets_root = _project_dir(database, project_id) / "assets"
        export = _request_export(client, project_id)
        content = _draft_json(_archive_path(database, project_id, export))

        paths = [item["path"] for item in content["materials"]["videos"]]
        paths += [item["path"] for item in content["materials"]["audios"]]
        assert paths, "the draft must reference at least one material"
        for raw in paths:
            material = Path(raw)
            assert material.is_absolute(), raw
            assert material.is_file(), raw
            assert assets_root in material.parents, raw
        # The demo provider's audio really is referenced, and the images are the PNGs we registered.
        assert {Path(item["path"]).suffix for item in content["materials"]["videos"]} == {".png"}
        assert {Path(item["path"]).suffix for item in content["materials"]["audios"]} == {".wav"}
        assert content["materials"]["videos"][0]["type"] == "photo"


# --- 4: SVG-only shots are skipped and reported ----------------------------------------------


def test_svg_only_shot_is_skipped_and_reported(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "jianying_svg.db"
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "no-ffmpeg"))
    with TestClient(create_app(path)) as client:
        project_id = project_with_shots(client, name="SVG混合", target_duration_sec=8)
        shots = client.get(f"/api/projects/{project_id}/shots").json()
        # The first shot is pinned back to SVG (what a "provider returned SVG" project looks like);
        # the rest keep the demo provider's real bitmap output.
        register_svg_images(monkeypatch, shots={str(shots[0]["id"])})
        generate_demo_media(client, project_id)
        build_timeline(client, project_id)
        database = client.app.state.database

        export = _request_export(client, project_id)
        assert export["status"] == "completed_with_warnings", export
        assert "跳过" in export["warning"] and "SVG" in export["warning"]
        content = _draft_json(_archive_path(database, project_id, export))
        video = _tracks(content)["video"]
        assert len(video["segments"]) == len(shots) - 1
        assert content["duration"] == sum(int(shot["duration"]) for shot in shots[1:]) * SEC
        # Skipped means skipped: the SVG is nowhere in the draft.
        assert all(not item["path"].endswith(".svg") for item in content["materials"]["videos"])


def test_shot_whose_media_files_vanished_falls_back_to_the_package(tmp_path: Path, monkeypatch) -> None:
    """A draft with zero segments is not a usable deliverable, so the export delivers the package.

    Deleting an asset file has to be survivable: the shot is skipped, the reason reaches the
    warning, and the user still gets the portable package instead of a failed task.
    """
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "no-ffmpeg"))
    register_png_images(monkeypatch)
    with TestClient(create_app(tmp_path / "jianying_empty.db")) as client:
        project_id = project_with_shots(client, name="素材丢失", target_duration_sec=4)
        generate_demo_media(client, project_id)
        build_timeline(client, project_id)
        database = client.app.state.database
        for path in (_project_dir(database, project_id) / "assets").rglob("*"):
            if path.is_file():
                path.unlink()
        export = _request_export(client, project_id)
        assert export["status"] == "completed_with_warnings"
        assert export["error"] is None
        assert export["format"] == "draft_package"
        assert "没有任何片段" in export["warning"] and "缺失" in export["warning"]
        with database.connect() as connection:
            asset_count = connection.execute(
                "SELECT COUNT(*) FROM assets WHERE project_id=?", (project_id,)).fetchone()[0]
        archive = _archive_path(database, project_id, export)
        with zipfile.ZipFile(archive) as package:
            names = set(package.namelist())
            assert {"timeline.json", "manifest.json"} <= names
            manifest = json.loads(package.read("manifest.json").decode("utf-8"))
        assert manifest["files"] == []
        assert len(manifest["skipped_files"]) == asset_count >= 1
        assert all("missing" in item["reason"] for item in manifest["skipped_files"])


def test_newest_video_asset_wins_over_the_bitmap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "no-ffmpeg"))
    register_png_images(monkeypatch)
    with TestClient(create_app(tmp_path / "jianying_video.db")) as client:
        project_id = project_with_shots(client, name="视频优先", target_duration_sec=4)
        generate_demo_media(client, project_id)
        build_timeline(client, project_id)
        database = client.app.state.database
        shots = client.get(f"/api/projects/{project_id}/shots").json()
        # 视频必须够长：库会拒绝超出素材时长的截取，太短的片段无法证明「视频优先」。
        shot_seconds = float(shots[0].get("duration_sec") or shots[0].get("duration_seconds") or 4)
        video_path, usable = add_video_asset(database, project_id, str(shots[0]["id"]),
                                             _project_dir(database, project_id), name="newest.mp4",
                                             seconds=shot_seconds + 2)
        if not usable:
            pytest.skip("ffmpeg is unavailable, so no decodable MP4 could be generated")
        export = _request_export(client, project_id)
        content = _draft_json(_archive_path(database, project_id, export))
        videos = content["materials"]["videos"]
        assert video_path in [item["path"] for item in videos]
        assert sum(1 for item in videos if item["type"] == "video") == 1


def test_undecodable_video_falls_back_to_the_shots_bitmap(tmp_path: Path, monkeypatch) -> None:
    """A video the library cannot read must cost the shot its clip, not its place on the timeline."""
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "no-ffmpeg"))
    register_png_images(monkeypatch)
    with TestClient(create_app(tmp_path / "jianying_bad_video.db")) as client:
        project_id = project_with_shots(client, name="坏视频", target_duration_sec=4)
        generate_demo_media(client, project_id)
        build_timeline(client, project_id)
        database = client.app.state.database
        shots = client.get(f"/api/projects/{project_id}/shots").json()
        # A truncated file: the row is a valid video asset, the bytes are not a video.
        broken = _project_dir(database, project_id) / "assets" / "manual-video" / "broken.mp4"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_bytes(b"not a video at all")
        with database.connect() as connection:
            connection.execute(
                "INSERT INTO assets (id,project_id,kind,source_type,source_shot_id,source_task_id,created_at)"
                " VALUES ('asset_broken',?,'video','import',?,NULL,'2024-01-01T00:00:00+00:00')",
                (project_id, str(shots[0]["id"])))
            connection.execute(
                "INSERT INTO asset_versions (id,asset_id,version,sha256,mime_type,relative_path,byte_size,"
                "demo,metadata_json,created_at) VALUES ('asset_broken_v1','asset_broken',1,?,?,?,?,0,'{}',"
                "'2024-01-01T00:00:00+00:00')",
                (hashlib.sha256(broken.read_bytes()).hexdigest(), "video/mp4",
                 broken.relative_to(_project_dir(database, project_id)).as_posix(),
                 broken.stat().st_size))
        export = _request_export(client, project_id)
        # The shot survives on its PNG, and the fallback is reported instead of hidden.
        content = _draft_json(_archive_path(database, project_id, export))
        assert len(_tracks(content)["video"]["segments"]) == len(shots)
        assert "首选素材被剪映库拒绝" in export["warning"]
        assert "broken.mp4" not in [item["path"] for item in content["materials"]["videos"]]


# --- 5 & 6: the library is missing, or it throws ---------------------------------------------


def test_missing_library_falls_back_to_the_portable_package(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "jianying_missing.db"
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "no-ffmpeg"))
    # backend.media imports the probe by name, so the name media uses is what must be replaced.
    monkeypatch.setattr("backend.media.jianying_library_available", lambda: False)
    monkeypatch.setattr("backend.media.jianying_library_version", lambda: None)
    register_png_images(monkeypatch)
    with TestClient(create_app(path)) as client:
        project_id = project_with_shots(client, name="库缺失", target_duration_sec=4)
        generate_demo_media(client, project_id)
        build_timeline(client, project_id)
        export = _request_export(client, project_id)

        assert export["status"] == "completed_with_warnings"  # never "failed"
        assert export["error"] is None
        assert export["format"] == "draft_package"
        assert export["artifacts"][0]["kind"] == "draft_package"
        assert "不可用" in export["warning"]
        assert LIBRARY_NAME in export["warning"]
        archive = _archive_path(client.app.state.database, project_id, export)
        with zipfile.ZipFile(archive) as package:
            assert {"timeline.json", "manifest.json"} <= set(package.namelist())


def test_library_that_throws_falls_back_with_the_reason(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "jianying_throws.db"
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "no-ffmpeg"))
    monkeypatch.setattr("backend.media.jianying_library_available", lambda: True)
    monkeypatch.setattr("backend.media.jianying_library_version", lambda: "9.9.9-test")
    monkeypatch.setattr("backend.jianying._load_library", lambda: fake_library())
    register_png_images(monkeypatch)
    with TestClient(create_app(path)) as client:
        project_id = project_with_shots(client, name="库报错", target_duration_sec=4)
        generate_demo_media(client, project_id)
        build_timeline(client, project_id)
        export = _request_export(client, project_id)

        assert export["status"] == "completed_with_warnings"
        assert export["format"] == "draft_package"
        assert "构建失败" in export["warning"]
        assert "OSError" in export["warning"] and "simulated draft folder failure" in export["warning"]
        archive = _archive_path(client.app.state.database, project_id, export)
        with zipfile.ZipFile(archive) as package:
            assert "manifest.json" in package.namelist()


# --- 7: audio follows its video segment ------------------------------------------------------


def test_audio_segments_start_where_their_video_segment_starts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "no-ffmpeg"))
    register_png_images(monkeypatch)
    with TestClient(create_app(tmp_path / "jianying_audio.db")) as client:
        project_id = project_with_shots(client, name="配音对齐", target_duration_sec=8)
        generate_demo_media(client, project_id)
        build_timeline(client, project_id)
        database = client.app.state.database
        export = _request_export(client, project_id)
        content = _draft_json(_archive_path(database, project_id, export))
        tracks = _tracks(content)
        video, audio = tracks["video"]["segments"], tracks["audio"]["segments"]
        assert len(audio) == len(video) >= 2
        for video_segment, audio_segment in zip(video, audio):
            assert (audio_segment["target_timerange"]["start"]
                    == video_segment["target_timerange"]["start"])
            assert (audio_segment["target_timerange"]["duration"]
                    == video_segment["target_timerange"]["duration"])
        assert len(tracks["text"]["segments"]) == len(video)


# --- 8: diagnostics ---------------------------------------------------------------------------


def test_diagnostics_report_the_jianying_library(tmp_path: Path, monkeypatch) -> None:
    drafts_dir = tmp_path / "jianying_root"
    monkeypatch.setenv("VMAKER_JIANYING_DRAFTS_DIR", str(drafts_dir))
    with TestClient(create_app(tmp_path / "diagnostics.db")) as client:
        payload = client.get("/api/system/diagnostics").json()
        jianying = payload["jianying"]
        assert jianying["library"] == LIBRARY_NAME
        assert jianying["available"] is library_available()
        assert jianying["version"] == library_version()
        assert jianying["draft_generation"] is library_available()
        assert jianying["auto_export"] is False
        assert "自动导出" in jianying["message"]
        assert jianying["drafts_dir"]["configured"] is True
        assert jianying["drafts_dir"]["path"] == str(drafts_dir)


def test_diagnostics_say_when_no_draft_root_is_configured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("VMAKER_JIANYING_DRAFTS_DIR", raising=False)
    with TestClient(create_app(tmp_path / "diagnostics_default.db")) as client:
        drafts_dir = client.get("/api/system/diagnostics").json()["jianying"]["drafts_dir"]
        assert drafts_dir["configured"] is False and drafts_dir["path"] is None
        assert "VMAKER_JIANYING_DRAFTS_DIR" in drafts_dir["message"]


# --- 9: mp4 keeps its explicit, historical meaning -------------------------------------------


def test_explicit_mp4_without_ffmpeg_never_becomes_a_jianying_draft(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "jianying_mp4.db"
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "no-ffmpeg"))
    register_png_images(monkeypatch)
    with TestClient(create_app(path)) as client:
        project_id = project_with_shots(client, name="只要MP4", target_duration_sec=4)
        generate_demo_media(client, project_id)
        build_timeline(client, project_id)
        export = _request_export(client, project_id, fmt="mp4")

        assert export["format"] == "draft_package"
        assert export["artifacts"][0]["kind"] == "draft_package"
        assert export["status"] == "completed_with_warnings"
        assert "no MP4" in export["warning"]
        assert LIBRARY_NAME not in export["warning"]
        archive = _archive_path(client.app.state.database, project_id, export)
        assert archive.name == "draft-package.zip"


def test_auto_prefers_a_jianying_draft_only_when_the_library_works(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "jianying_auto.db"
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "no-ffmpeg"))
    register_png_images(monkeypatch)
    with TestClient(create_app(path)) as client:
        project_id = project_with_shots(client, name="自动导出", target_duration_sec=4)
        generate_demo_media(client, project_id)
        build_timeline(client, project_id)
        export = _request_export(client, project_id, fmt="auto")
        assert export["status"] == "completed_with_warnings"
        assert "ffmpeg unavailable" in export["warning"]
        if library_available():
            assert export["format"] == "jianying_draft"
            assert "Jianying draft" in export["warning"]
        else:
            assert export["format"] == "draft_package"
            assert "不可用" in export["warning"]

    monkeypatch.setattr("backend.media.jianying_library_available", lambda: False)
    monkeypatch.setattr("backend.media.jianying_library_version", lambda: None)
    with TestClient(create_app(tmp_path / "jianying_auto_missing.db")) as client:
        project_id = project_with_shots(client, name="自动降级", target_duration_sec=4)
        generate_demo_media(client, project_id)
        build_timeline(client, project_id)
        export = _request_export(client, project_id, fmt="auto")
        assert export["format"] == "draft_package"
        assert "不可用" in export["warning"]


def test_unknown_export_format_is_rejected(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jianying_format.db")) as client:
        project_id = project_with_shots(client, name="非法格式", target_duration_sec=4)
        for bad in ("avi", "JIANYING", "draft", ""):
            response = client.post(f"/api/projects/{project_id}/exports", json={"format": bad})
            assert response.status_code == 422, f"{bad!r} was accepted: {response.text}"


# --- 10: safe, non-destructive draft names ---------------------------------------------------


def test_draft_name_is_sanitized_and_never_overwrites(tmp_path: Path, monkeypatch) -> None:
    drafts_root = tmp_path / "jianying_root"
    monkeypatch.setenv("VMAKER_JIANYING_DRAFTS_DIR", str(drafts_root))
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "no-ffmpeg"))
    register_png_images(monkeypatch)
    with TestClient(create_app(tmp_path / "jianying_names.db")) as client:
        project_id = project_with_shots(client, name='坏名字:A?B*C|D"E<F>G', target_duration_sec=4)
        generate_demo_media(client, project_id)
        build_timeline(client, project_id)
        first = _request_export(client, project_id)
        assert first["artifacts"][0]["kind"] == "jianying_draft"

        folders = sorted(item.name for item in drafts_root.iterdir())
        assert len(folders) == 1, folders
        assert not set(folders[0]) & set(ILLEGAL_NAME_CHARS), folders[0]
        assert folders[0] == sanitize_draft_name('坏名字:A?B*C|D"E<F>G')
        first_content = drafts_root / folders[0] / "draft_content.json"
        original = first_content.read_bytes()

        # A second export for the same project must add a new draft, never replace the first one.
        second = _request_export(client, project_id)
        assert second["artifacts"][0]["kind"] == "jianying_draft"
        after = sorted(item.name for item in drafts_root.iterdir())
        assert len(after) == 2, after
        assert folders[0] in after, "the first user draft was replaced"
        assert first_content.read_bytes() == original, "the first user draft was rewritten"
        assert any(name.endswith("-3") for name in after), after


def test_user_draft_root_keeps_both_drafts_and_reports_the_names(tmp_path: Path, monkeypatch) -> None:
    """Two exports of one project must leave two drafts in the user's root, both intact."""
    drafts_root = tmp_path / "jianying_root"
    monkeypatch.setenv("VMAKER_JIANYING_DRAFTS_DIR", str(drafts_root))
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "no-ffmpeg"))
    register_png_images(monkeypatch)
    with TestClient(create_app(tmp_path / "jianying_root.db")) as client:
        project_id = project_with_shots(client, name="重复导出", target_duration_sec=4)
        generate_demo_media(client, project_id)
        build_timeline(client, project_id)
        first = _request_export(client, project_id)
        first_info = _draft_info(_archive_path(client.app.state.database, project_id, first))
        assert first_info["draft_name"] == "重复导出"
        assert Path(first_info["user_draft_dir"]).name == "重复导出"
        assert Path(first_info["user_draft_dir"]).is_dir()
        original = (Path(first_info["user_draft_dir"]) / "draft_content.json").read_bytes()

        second = _request_export(client, project_id)
        second_info = _draft_info(_archive_path(client.app.state.database, project_id, second))
        # The archive info records the folder the user's root really received.
        assert Path(second_info["user_draft_dir"]).name == "重复导出-3"
        assert sorted(item.name for item in drafts_root.iterdir()) == ["重复导出", "重复导出-3"]
        assert (Path(first_info["user_draft_dir"]) / "draft_content.json").read_bytes() == original
        # Both copies are complete drafts; the export copy keeps the project's own name.
        for info in (first_info, second_info):
            assert sorted(item.name for item in Path(info["user_draft_dir"]).iterdir()) == [
                "draft_content.json", "draft_meta_info.json"]
            assert Path(info["draft_dir"]).is_dir()


def test_unwritable_draft_root_is_only_a_warning(tmp_path: Path, monkeypatch) -> None:
    # A file where a directory is expected: creating the draft root can only fail.
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied", encoding="utf-8")
    monkeypatch.setenv("VMAKER_JIANYING_DRAFTS_DIR", str(blocker / "drafts"))
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "no-ffmpeg"))
    register_png_images(monkeypatch)
    with TestClient(create_app(tmp_path / "jianying_unwritable.db")) as client:
        project_id = project_with_shots(client, name="不可写目录", target_duration_sec=4)
        generate_demo_media(client, project_id)
        build_timeline(client, project_id)
        export = _request_export(client, project_id)

        # The draft still exists and the task still succeeded: a warning, not a failure.
        assert export["status"] == "completed_with_warnings"
        assert export["error"] is None
        assert export["artifacts"][0]["kind"] == "jianying_draft"
        assert "无法写入配置的剪映草稿目录" in export["warning"]
        archive = _archive_path(client.app.state.database, project_id, export)
        with zipfile.ZipFile(archive) as package:
            assert any(name.endswith("/draft_content.json") for name in package.namelist())


def test_draft_directory_holds_the_real_draft_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "no-ffmpeg"))
    register_png_images(monkeypatch)
    with TestClient(create_app(tmp_path / "jianying_tree.db")) as client:
        project_id = project_with_shots(client, name="目录树", target_duration_sec=4)
        generate_demo_media(client, project_id)
        build_timeline(client, project_id)
        export = _request_export(client, project_id)
        export_dir = _project_dir(client.app.state.database, project_id) / "exports" / export["id"]
        assert (export_dir / "jianying-draft.zip").is_file()
        draft_dirs = [item for item in (export_dir / "draft").iterdir() if item.is_dir()]
        assert len(draft_dirs) == 1
        assert sorted(item.name for item in draft_dirs[0].iterdir()) == [
            "draft_content.json", "draft_meta_info.json"]
        assert (export_dir / "draft-package.zip").exists() is False
        # The download route serves the same bytes the artifact recorded.
        artifact = export["artifacts"][0]
        download = client.get(f"/api/projects/{project_id}/artifacts/{artifact['id']}/download")
        assert download.status_code == 200
        assert zipfile.ZipFile(io.BytesIO(download.content)).namelist()


def test_image_pick_prefers_bitmap_and_reports_svg() -> None:
    """The selection rule itself, including the distinction between "SVG only" and "nothing"."""
    svg = {"kind": "image", "mime_type": "image/svg+xml", "relative_path": "a/v1.svg"}
    png = {"kind": "image", "mime_type": "image/png", "relative_path": "b/v1.png"}
    jpeg = {"kind": "image", "mime_type": "image/jpeg", "relative_path": "b/v2.jpg"}
    video = {"kind": "video", "mime_type": "video/mp4", "relative_path": "c/v1.mp4"}
    assert [item for item, _ in _pick_image_assets([svg, png, video])] == [video, png]
    assert [kind for _, kind in _pick_image_assets([svg, png, video])] == ["video", "bitmap"]
    # A video the library cannot decode still leaves the bitmap as a fallback candidate.
    assert [item for item, _ in _pick_image_assets([jpeg, png])] == [jpeg, png]
    assert _pick_image_assets([svg]) == []
    assert _pick_image_assets([]) == []
    assert _classify_asset("image/jpeg", "x/v1.jpg") == "bitmap"
    assert _classify_asset("image/webp", "x/v1.webp") == "bitmap"
    assert _classify_asset("image/svg+xml", "x/v1.svg") == "svg"
    assert _classify_asset("", "x/v1.PNG") == "bitmap"
    assert _classify_asset("", "x/v1.mp4") == "video"
    assert _classify_asset("application/pdf", "x/v1.pdf") == "other"


def test_sanitize_draft_name_removes_windows_illegal_characters() -> None:
    assert sanitize_draft_name('a\\b/c:d*e?f"g<h>i|j') == "a-b-c-d-e-f-g-h-i-j"
    assert sanitize_draft_name("   ") == "V-Maker 草稿"
    assert sanitize_draft_name("") == "V-Maker 草稿"
    long_name = sanitize_draft_name("长" * 200)
    assert len(long_name) <= 60 and len(long_name) >= 1
    assert not [character for character in sanitize_draft_name("a:\x00b") if ord(character) < 32]
