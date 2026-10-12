"""Storage accounting and safe cleanup: real demo media, real files, no accidental deletion."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app


def _client(tmp_path: Path, name: str = "storage.db") -> TestClient:
    return TestClient(create_app(tmp_path / name))


def _project_with_media(client: TestClient, name: str = "存储测试") -> str:
    project = client.post(
        "/api/projects",
        json={"name": name, "story": "一只猫安全回家。", "target_duration_sec": 8},
    ).json()
    task = client.post(f"/api/projects/{project['id']}/analysis-tasks").json()
    client.post(f"/api/tasks/{task['id']}/execute")
    screenplay = client.post(f"/api/projects/{project['id']}/screenplay/generate")
    assert screenplay.status_code == 201, screenplay.text
    generated = client.post(f"/api/projects/{project['id']}/demo-media")
    assert generated.status_code == 201, generated.text
    return str(project["id"])


def _assets_root(tmp_path: Path) -> Path:
    # MediaRepository stores media next to the SQLite file, in a "projects" directory.
    return tmp_path / "projects"


def _disk_files(root: Path, project_id: str) -> list[Path]:
    return sorted(path for path in (root / project_id).rglob("*") if path.is_file())


def _referenced_relative_paths(client: TestClient) -> list[tuple[str, str]]:
    with client.app.state.database.connect() as connection:
        rows = connection.execute(
            "SELECT a.project_id project_id, av.relative_path relative_path"
            " FROM asset_versions av JOIN assets a ON a.id=av.asset_id"
        ).fetchall()
        artifacts = connection.execute(
            "SELECT j.project_id project_id, ea.relative_path relative_path"
            " FROM export_artifacts ea JOIN export_jobs j ON j.id=ea.export_job_id"
        ).fetchall()
    return [(str(row["project_id"]), str(row["relative_path"])) for row in (*rows, *artifacts)]


def _orphan_file(root: Path, project_id: str, name: str = "scratch.bin", size: int = 128) -> Path:
    orphan = root / project_id / "orphans" / name
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan-payload" * (size // 14 + 1))
    return orphan


# --------------------------------------------------------------------------- 1. counting


def test_storage_summary_counts_real_disk_files(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id = _project_with_media(client)
        root = _assets_root(tmp_path)
        files = _disk_files(root, project_id)
        assert files, "demo media must produce real files"

        body = client.get("/api/system/storage").json()
        assert body["data_dir"] == str(root.resolve())
        assert body["total_bytes"] > 0
        assert body["file_count"] == len(files)
        assert body["file_count"] == sum(item["file_count"] for item in body["projects"])
        assert body["total_bytes"] == sum(item["bytes"] for item in body["projects"])

        entry = next(item for item in body["projects"] if item["project_id"] == project_id)
        assert entry["name"] == "存储测试"
        assert entry["deleted"] is False
        assert entry["file_count"] == len(files)
        assert entry["bytes"] == sum(path.stat().st_size for path in files)
        assert body["orphan_files"] == 0
        assert body["orphan_bytes"] == 0
        assert body["deleted_project_files"] == 0


def test_storage_reports_orphans_and_unknown_project_dirs(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id = _project_with_media(client)
        root = _assets_root(tmp_path)
        orphan = _orphan_file(root, project_id)
        stray = root / "prj_stray" / "leftover.bin"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"stray")

        body = client.get("/api/system/storage").json()
        assert body["orphan_files"] == 2
        assert body["orphan_bytes"] == orphan.stat().st_size + stray.stat().st_size
        assert body["total_bytes"] >= body["orphan_bytes"]

        stray_entry = next(item for item in body["projects"] if item["project_id"] == "prj_stray")
        assert stray_entry["unknown"] is True
        assert stray_entry["name"] == "prj_stray"
        assert stray_entry["file_count"] == 1
        assert stray_entry["deleted"] is False

        entry = next(item for item in body["projects"] if item["project_id"] == project_id)
        assert "unknown" not in entry
        # Largest project first.
        assert [item["bytes"] for item in body["projects"]] == sorted(
            (item["bytes"] for item in body["projects"]), reverse=True
        )


# --------------------------------------------------------------------------- 2. dry run


def test_cleanup_dry_run_lists_targets_without_deleting(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id = _project_with_media(client)
        root = _assets_root(tmp_path)
        orphan = _orphan_file(root, project_id)
        before = client.get("/api/system/storage").json()

        response = client.post(
            "/api/system/storage/cleanup",
            json={"dry_run": True, "include_deleted_projects": False},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["dry_run"] is True
        assert body["removed_files"] == 0
        assert body["removed_bytes"] == 0
        assert body["target_files"] == 1
        assert "orphans/scratch.bin" in body["targets"]
        assert body["kept_referenced"] == len(_referenced_relative_paths(client))
        assert "truncated" not in body

        # Nothing moved on disk, and the reported footprint is unchanged.
        assert orphan.is_file()
        after = client.get("/api/system/storage").json()
        assert after["orphan_bytes"] == before["orphan_bytes"] > 0
        assert after["orphan_files"] == 1

        # Omitting the body must still default to a dry run.
        default_body = client.post("/api/system/storage/cleanup").json()
        assert default_body["dry_run"] is True
        assert orphan.is_file()


def test_cleanup_dry_run_false_removes_only_orphans(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id = _project_with_media(client)
        root = _assets_root(tmp_path)
        orphan = _orphan_file(root, project_id, size=64)
        orphan_size = orphan.stat().st_size
        referenced = _referenced_relative_paths(client)
        assert referenced and all((root / pid / rel).is_file() for pid, rel in referenced)

        body = client.post("/api/system/storage/cleanup", json={"dry_run": False}).json()
        assert body["dry_run"] is False
        assert body["removed_files"] == 1
        assert body["removed_bytes"] == orphan_size > 0
        assert body["failed"] == 0
        assert body["skipped_unsafe"] == 0

        assert not orphan.exists()
        for project, relative_path in referenced:
            assert (root / project / relative_path).is_file(), relative_path
        after = client.get("/api/system/storage").json()
        assert after["orphan_files"] == 0
        assert after["file_count"] == len(referenced)

        # A second pass has nothing left to do and must not fail.
        again = client.post("/api/system/storage/cleanup", json={"dry_run": False})
        assert again.status_code == 200
        assert again.json()["removed_files"] == 0


# --------------------------------------------------------------------------- 3. deleted projects


def test_cleanup_keeps_deleted_project_files_unless_requested(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id = _project_with_media(client, name="待删除项目")
        root = _assets_root(tmp_path)
        orphan = _orphan_file(root, project_id)
        referenced = _referenced_relative_paths(client)

        assert client.delete(f"/api/projects/{project_id}").status_code == 204
        summary = client.get("/api/system/storage").json()
        entry = next(item for item in summary["projects"] if item["project_id"] == project_id)
        assert entry["deleted"] is True
        assert entry["name"] == "待删除项目"
        assert summary["deleted_project_files"] == len(_disk_files(root, project_id))
        assert summary["deleted_project_bytes"] > 0

        # Without the opt-in flag nothing in a soft-deleted project is even a target.
        kept = client.post(
            "/api/system/storage/cleanup",
            json={"dry_run": False, "include_deleted_projects": False},
        ).json()
        assert kept["removed_files"] == 0
        assert kept["target_files"] == 0
        assert orphan.is_file()
        assert all((root / pid / rel).is_file() for pid, rel in referenced)

        # With the flag the orphan goes, and every referenced file survives.
        cleaned = client.post(
            "/api/system/storage/cleanup",
            json={"dry_run": False, "include_deleted_projects": True},
        ).json()
        assert cleaned["removed_files"] == 1
        assert cleaned["target_files"] == 1
        assert not orphan.exists()
        for project, relative_path in referenced:
            assert (root / project / relative_path).is_file(), relative_path


# --------------------------------------------------------------------------- 4. escapes


def test_cleanup_never_deletes_files_outside_the_projects_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside.svg"
    outside.write_bytes(b"outside-root-payload")
    with _client(tmp_path) as client:
        project_id = _project_with_media(client)
        root = _assets_root(tmp_path)
        orphan = _orphan_file(root, project_id)

        # A hostile (or legacy) row that climbs out of the projects root must never be followed.
        with client.app.state.database.connect() as connection:
            connection.execute(
                "INSERT INTO assets (id,project_id,kind,source_type,created_at)"
                " VALUES ('asset_evil',?,'image','import','2024-01-01T00:00:00+00:00')",
                (project_id,),
            )
            for index, escape in enumerate(("../../outside.svg", "../outside.svg")):
                connection.execute(
                    "INSERT INTO asset_versions"
                    " (id,asset_id,version,sha256,mime_type,relative_path,byte_size,demo,metadata_json,created_at)"
                    " VALUES (?,?,?,?,?,?,0,0,'{}','2024-01-01T00:00:00+00:00')",
                    (f"aver_evil_{index}", "asset_evil", index + 1, str(index + 1) * 64,
                     "image/svg+xml", escape),
                )

        body = client.post(
            "/api/system/storage/cleanup",
            json={"dry_run": False, "include_deleted_projects": True},
        ).json()
        assert body["failed"] == 0
        # The escaping rows protect nothing inside the root, so the real orphan is still removed.
        assert body["removed_files"] == 1
        assert not orphan.exists()
        assert outside.is_file()
        assert outside.read_bytes() == b"outside-root-payload"


def test_cleanup_does_not_follow_a_symlink_out_of_the_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside-root-payload")
    with _client(tmp_path) as client:
        project_id = _project_with_media(client)
        root = _assets_root(tmp_path)
        link = root / project_id / "escape.bin"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):  # Windows without developer mode
            pytest.skip("symlinks are unavailable on this host")

        # A link that resolves outside the root is not even counted as storage.
        summary = client.get("/api/system/storage").json()
        assert summary["orphan_files"] == 0

        body = client.post("/api/system/storage/cleanup", json={"dry_run": False}).json()
        assert body["skipped_unsafe"] == 1
        assert body["removed_files"] == 0
        assert body["failed"] == 0
        assert outside.is_file()
        assert outside.read_bytes() == b"outside-root-payload"
        assert link.is_symlink()


# --------------------------------------------------------------------------- 5. edge cases


def test_storage_summary_without_a_projects_root(tmp_path: Path) -> None:
    # The database lives in a directory that does not exist yet, so neither does the media root.
    with TestClient(create_app(tmp_path / "nested" / "storage.db")) as client:
        body = client.get("/api/system/storage").json()
        assert body["total_bytes"] == 0
        assert body["file_count"] == 0
        assert body["orphan_bytes"] == 0
        assert body["orphan_files"] == 0
        assert body["deleted_project_bytes"] == 0
        assert body["deleted_project_files"] == 0
        assert body["projects"] == []
        assert body["data_dir"].endswith("projects")

        cleanup = client.post(
            "/api/system/storage/cleanup",
            json={"dry_run": False, "include_deleted_projects": True},
        )
        assert cleanup.status_code == 200
        assert cleanup.json()["removed_files"] == 0


def test_cleanup_tolerates_empty_directories_and_missing_files(tmp_path: Path, monkeypatch) -> None:
    with _client(tmp_path) as client:
        project = client.post(
            "/api/projects",
            json={"name": "空项目", "story": "空。", "target_duration_sec": 4},
        ).json()
        root = _assets_root(tmp_path)
        empty = root / str(project["id"]) / "assets" / "nothing-here"
        empty.mkdir(parents=True, exist_ok=True)

        body = client.post("/api/system/storage/cleanup", json={"dry_run": False}).json()
        assert body["removed_files"] == 0
        assert body["failed"] == 0
        assert body["skipped_unsafe"] == 0
        assert empty.is_dir()

        # A file that cannot be unlinked (locked, or already gone) is reported, never a 500.
        ghost = root / str(project["id"]) / "assets" / "ghost.bin"
        ghost.write_bytes(b"ghost")
        original_unlink = Path.unlink

        def failing_unlink(self: Path, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            if self == ghost:
                raise PermissionError("file is in use")
            return original_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", failing_unlink)
        response = client.post("/api/system/storage/cleanup", json={"dry_run": False})
        monkeypatch.undo()
        assert response.status_code == 200
        assert response.json()["failed"] == 1
        assert response.json()["removed_files"] == 0
        assert ghost.is_file()


def test_cleanup_truncates_the_target_list(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project = client.post(
            "/api/projects",
            json={"name": "大量垃圾", "story": "垃圾。", "target_duration_sec": 4},
        ).json()
        junk = _assets_root(tmp_path) / str(project["id"]) / "junk"
        junk.mkdir(parents=True, exist_ok=True)
        for index in range(205):
            (junk / f"orphan-{index:03}.bin").write_bytes(b"junk")

        summary = client.get("/api/system/storage").json()
        assert summary["orphan_files"] == 205

        body = client.post("/api/system/storage/cleanup", json={"dry_run": True}).json()
        assert body["truncated"] is True
        assert len(body["targets"]) == 200
        assert body["target_files"] == 205
        assert all(path.is_file() for path in junk.iterdir())


def test_cleanup_rejects_non_boolean_flags(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.post("/api/system/storage/cleanup", json={"dry_run": "no"}).status_code == 422
        assert client.post(
            "/api/system/storage/cleanup", json={"include_deleted_projects": "yes"}
        ).status_code == 422


# ------------------------------------------------- 9. references the database implies indirectly


def test_timeline_json_is_not_treated_as_an_orphan(tmp_path: Path) -> None:
    """create_timeline writes timelines/v<N>.json and get_timeline still advertises that path.

    Nothing stores it in asset_versions/export_artifacts, so an orphan-only rule would delete a
    file the API keeps reporting. It must count as referenced.
    """
    with _client(tmp_path, "timeline_ref.db") as client:
        project_id = _project_with_media(client)
        assert client.post(f"/api/projects/{project_id}/timeline").status_code == 201
        timeline_file = _assets_root(tmp_path) / project_id / "timelines" / "v1.json"
        assert timeline_file.is_file()

        summary = client.get("/api/system/storage").json()
        assert summary["orphan_files"] == 0, summary

        body = client.post("/api/system/storage/cleanup", json={"dry_run": False}).json()
        assert body["removed_files"] == 0
        assert timeline_file.is_file()


def test_running_export_intermediates_are_protected(tmp_path: Path) -> None:
    """A running export writes clip-*.mp4 / concat.txt that no table registers yet."""
    with _client(tmp_path, "running_export.db") as client:
        project_id = _project_with_media(client)
        assert client.post(f"/api/projects/{project_id}/timeline").status_code == 201
        database = client.app.state.database
        job_id = "export_midflight"
        with database.connect() as connection:
            connection.execute(
                "INSERT INTO export_jobs (id,project_id,timeline_version_id,status,format,created_at,started_at)"
                " SELECT ?,?,id,'running','auto',created_at,created_at FROM timeline_versions"
                "  WHERE project_id=? ORDER BY version DESC LIMIT 1",
                (job_id, project_id, project_id),
            )
        export_dir = _assets_root(tmp_path) / project_id / "exports" / job_id
        export_dir.mkdir(parents=True, exist_ok=True)
        clip = export_dir / "clip-0001.mp4"
        clip.write_bytes(b"partial-render")

        summary = client.get("/api/system/storage").json()
        assert summary["orphan_files"] == 0, summary

        body = client.post("/api/system/storage/cleanup", json={"dry_run": False}).json()
        assert body["removed_files"] == 0
        assert clip.is_file(), "in-flight export intermediates must survive cleanup"

        # Once the job is no longer running they become ordinary orphans and can be reclaimed.
        with database.connect() as connection:
            connection.execute("UPDATE export_jobs SET status='failed' WHERE id=?", (job_id,))
        assert client.get("/api/system/storage").json()["orphan_files"] == 1
        after = client.post("/api/system/storage/cleanup", json={"dry_run": False}).json()
        assert after["removed_files"] == 1 and not clip.is_file()
