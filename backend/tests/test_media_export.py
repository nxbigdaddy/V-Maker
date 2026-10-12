from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.media import MediaRepository, verify_mp4_output
from backend.tests.jianying_support import register_svg_images


def _project_with_shots(client: TestClient) -> str:
    project = client.post("/api/projects", json={"name": "媒体测试", "story": "一只猫安全回家。", "target_duration_sec": 8}).json()
    task = client.post(f"/api/projects/{project['id']}/analysis-tasks").json()
    client.post(f"/api/tasks/{task['id']}/execute")
    assert client.post(f"/api/projects/{project['id']}/screenplay/generate").status_code == 201
    return project["id"]


def test_demo_assets_timeline_and_no_ffmpeg_draft_survive_restart(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "media.db"
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "does-not-exist"))
    with TestClient(create_app(path)) as client:
        project_id = _project_with_shots(client)
        generated = client.post(f"/api/projects/{project_id}/demo-media")
        assert generated.status_code == 201, generated.text
        assert generated.json()["demo"] is True
        assert "no real AI" in generated.json()["notice"]
        assets = client.get(f"/api/projects/{project_id}/assets").json()
        shots = client.get(f"/api/projects/{project_id}/shots").json()
        assert len([a for a in assets if a["kind"] == "image"]) == len(shots)
        assert len([a for a in assets if a["kind"] == "audio"]) == len(shots)
        for asset in assets:
            version = asset["latest_version"]
            file_path = path.parent / "projects" / project_id / version["relative_path"]
            assert file_path.is_file()
            assert hashlib.sha256(file_path.read_bytes()).hexdigest() == version["sha256"]
            assert version["demo"] is True
            if asset["kind"] in ("image", "audio"):
                assert asset["source_shot_id"] in {s["id"] for s in shots}

        timeline_response = client.post(f"/api/projects/{project_id}/timeline")
        assert timeline_response.status_code == 201, timeline_response.text
        timeline = timeline_response.json()
        assert timeline["timeline"]["schema"] == "vmaker.neutral-timeline/v1"
        assert len(timeline["timeline"]["tracks"]["video_audio"]) == len(shots)
        # ``portable_draft`` is the explicit "no MP4, generic zip" request. ``auto`` would now prefer
        # a real Jianying draft whenever pyJianYingDraft is installed, which is a different test in
        # test_jianying_export.py: this one stays about the self-contained package.
        export = client.post(f"/api/projects/{project_id}/exports", json={"format": "portable_draft"}).json()
        assert export["status"] == "completed_with_warnings"
        assert export["format"] == "draft_package"
        assert "no MP4" in export["warning"]
        artifact = export["artifacts"][0]
        archive_path = path.parent / "projects" / project_id / artifact["relative_path"]
        assert archive_path.is_file() and archive_path.suffix == ".zip"
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            assert {"timeline.json", "shot-list.csv", "manifest.json", "subtitles.srt"} <= names
            # demo 占位图是位图：FFmpeg 与剪映都读不了 SVG，用 SVG 会让默认项目两条导出都失败。
            assert any(name.endswith(".png") for name in names)
            assert not any(name.endswith(".svg") for name in names)
            assert any(name.endswith(".wav") for name in names)
            manifest = json.loads(archive.read("manifest.json"))
            assert "No MP4" in manifest["notice"]
            assert all(item["sha256"] for item in manifest["files"])

    with TestClient(create_app(path)) as client:
        assets_after = client.get(f"/api/projects/{project_id}/assets")
        assert assets_after.status_code == 200 and assets_after.json()
        timeline_after = client.get(f"/api/projects/{project_id}/timeline")
        assert timeline_after.status_code == 200 and timeline_after.json()["version"] == 1
        export_after = client.get(f"/api/exports/{export['id']}")
        assert export_after.status_code == 200 and export_after.json()["status"] == "completed_with_warnings"


def test_corrupted_asset_fails_the_export_instead_of_being_silently_omitted(tmp_path: Path) -> None:
    """A checksum mismatch is an integrity failure, not a missing file.

    A vanished file may be skipped (and is reported in the warning), but bytes that no longer match
    the recorded SHA-256 mean the file was corrupted or altered. Exporting anyway would quietly
    invalidate the guarantee this package is built on, so the export has to fail loudly.
    """
    path = tmp_path / "tampered.db"
    with TestClient(create_app(path)) as client:
        project_id = _project_with_shots(client)
        assert client.post(f"/api/projects/{project_id}/demo-media").status_code == 201
        assert client.post(f"/api/projects/{project_id}/timeline").status_code == 201

        assets = client.get(f"/api/projects/{project_id}/assets").json()
        victim = assets[0]["latest_version"]
        victim_path = path.parent / "projects" / project_id / victim["relative_path"]
        original = victim_path.read_bytes()
        victim_path.write_bytes(original + b"--tampered")

        export = client.post(f"/api/projects/{project_id}/exports", json={"format": "portable_draft"}).json()
        assert export["status"] == "failed", export
        assert "SHA-256" in (export["error"] or "")
        assert export["artifacts"] == []
        # The project and its metadata must be untouched by the refused export.
        still_there = client.get(f"/api/projects/{project_id}/assets").json()
        assert len(still_there) == len(assets)


def test_missing_asset_is_skipped_and_surfaced_in_the_warning(tmp_path: Path) -> None:
    """Skipping is allowed; skipping *silently* is not.

    Storage cleanup can remove assets, so a vanished file must not fail the whole export — but the
    user has to be told which package is incomplete, which is what the warning and the manifest
    are for.
    """
    path = tmp_path / "missing.db"
    with TestClient(create_app(path)) as client:
        project_id = _project_with_shots(client)
        assert client.post(f"/api/projects/{project_id}/demo-media").status_code == 201
        assert client.post(f"/api/projects/{project_id}/timeline").status_code == 201

        assets = client.get(f"/api/projects/{project_id}/assets").json()
        victim = assets[0]["latest_version"]
        (path.parent / "projects" / project_id / victim["relative_path"]).unlink()

        export = client.post(f"/api/projects/{project_id}/exports", json={"format": "portable_draft"}).json()
        assert export["status"] == "completed_with_warnings", export
        assert export["format"] == "draft_package"
        warning = export["warning"] or ""
        assert "缺失" in warning and "1 个素材文件缺失" in warning, warning

        artifact = export["artifacts"][0]
        archive_path = path.parent / "projects" / project_id / artifact["relative_path"]
        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert len(manifest["files"]) == len(assets) - 1
            skipped = manifest["skipped_files"]
            assert len(skipped) == 1 and skipped[0]["reason"] == "asset file missing"
            assert skipped[0]["relative_path"] == victim["relative_path"]
            # The omitted file must really be absent from the archive, not just unlisted.
            assert not any(name.endswith(Path(victim["relative_path"]).name) for name in archive.namelist())


def test_mp4_with_svg_media_falls_back_with_an_actionable_reason(tmp_path: Path, monkeypatch) -> None:
    """FFmpeg has no SVG decoder, so a project whose shots are SVG can never become an MP4.

    The demo provider now writes bitmaps, but a user's own provider (or older assets) can still hand
    us SVG. The export must not pretend otherwise, and the warning has to name the real cause
    instead of leaking a bare `CalledProcessError`, which tells the user nothing.
    """
    monkeypatch.setattr(MediaRepository, "_ffmpeg", staticmethod(lambda: "ffmpeg"))
    path = tmp_path / "svg_mp4.db"
    with TestClient(create_app(path)) as client:
        project_id = _project_with_shots(client)
        register_svg_images(monkeypatch)
        assert client.post(f"/api/projects/{project_id}/demo-media").status_code == 201
        assert client.post(f"/api/projects/{project_id}/timeline").status_code == 201

        export = client.post(f"/api/projects/{project_id}/exports", json={"format": "mp4"}).json()
        assert export["status"] == "completed_with_warnings", export
        assert export["format"] == "draft_package"
        assert export["artifacts"][0]["kind"] == "draft_package"
        warning = export["warning"] or ""
        assert "不是位图" in warning and "SVG" in warning, warning
        assert "CalledProcessError" not in warning, warning
        # 不能声称产出了 MP4。
        assert "mp4" not in [a["kind"] for a in export["artifacts"]]


def test_demo_media_is_a_bitmap_the_pipeline_can_actually_use(tmp_path: Path) -> None:
    """默认 demo 素材必须是位图，否则开箱即用的项目既出不了 MP4、也进不了剪映。"""
    path = tmp_path / "demo_bitmap.db"
    with TestClient(create_app(path)) as client:
        project_id = _project_with_shots(client)
        assert client.post(f"/api/projects/{project_id}/demo-media").status_code == 201
        images = [a for a in client.get(f"/api/projects/{project_id}/assets").json() if a["kind"] == "image"]
        assert images, "no demo images"
        for asset in images:
            version = asset["latest_version"]
            assert version["mime_type"] == "image/png", version
            assert version["relative_path"].endswith(".png"), version
            body = (path.parent / "projects" / project_id / version["relative_path"]).read_bytes()
            assert body[:8] == b"\x89PNG\r\n\x1a\n"
            assert hashlib.sha256(body).hexdigest() == version["sha256"]


def test_mp4_canvas_follows_the_project_aspect_ratio(tmp_path: Path, monkeypatch) -> None:
    """MP4 画布必须跟随项目宽高比。

    这里以前硬编码 1280x720，所以竖屏（9:16）项目会拿到一个横屏成片 —— 实机验证时发现的。
    用替身捕获 ffmpeg 参数，这样不依赖机器上有没有真的 ffmpeg；产物校验由专门的用例覆盖，
    这里直接跳过，只盯命令行里的画布参数。
    """
    import backend.media as media_module

    monkeypatch.setattr(media_module, "verify_mp4_output", lambda *a, **k: {})
    captured: list[list[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001 - matches subprocess.run usage
        captured.append(list(argv))
        Path(argv[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(argv[-1]).write_bytes(b"stub")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(MediaRepository, "_asset_version_path",
                        lambda self, c, project_id, version_id: Path(f"{version_id}.png"))
    expected = {"16:9": "1920:1080", "9:16": "1080:1920", "1:1": "1080:1080"}

    app = create_app(tmp_path / "canvas.db")
    with TestClient(app) as client:
        repo = MediaRepository(app.state.database)
        for aspect_ratio, scale in expected.items():
            captured.clear()
            project_id = client.post("/api/projects", json={
                "name": f"画布 {aspect_ratio}", "story": "测试画布。", "target_duration_sec": 4,
                "aspect_ratio": aspect_ratio,
            }).json()["id"]
            timeline = {"tracks": {"video_audio": [{
                "position": 1, "duration_sec": 2,
                "image": {"asset_version_id": "img1"}, "audio": {"asset_version_id": "aud1"},
            }]}}
            with app.state.database.connect() as connection:
                repo._render_mp4(connection, project_id, "job1", timeline, "ffmpeg")
            assert captured, aspect_ratio
            filters = " ".join(captured[0])
            assert f"scale={scale}" in filters and f"pad={scale}" in filters, (aspect_ratio, filters)
            # 竖屏项目绝不能输出横屏尺寸。
            assert "1280:720" not in filters, (aspect_ratio, filters)


def _tiny_mp4(tmp_path: Path, seconds: int = 2) -> Path | None:
    """A small but genuine H.264+AAC file, or None when no ffmpeg is available."""
    ffmpeg = MediaRepository._ffmpeg()
    if not ffmpeg:
        return None
    output = tmp_path / "tiny.mp4"
    completed = subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=15:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(output)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    return output if completed.returncode == 0 and output.is_file() else None


def test_verify_mp4_output_accepts_a_good_file_and_catches_damage(tmp_path: Path) -> None:
    """校验器必须真的能挑出坏产物，否则它只是一层新的假绿。"""
    ffmpeg = MediaRepository._ffmpeg()
    if not ffmpeg:
        pytest.skip("no ffmpeg available to build a real file")
    good = _tiny_mp4(tmp_path)
    if good is None:
        pytest.skip("ffmpeg could not produce a test file")

    probe = verify_mp4_output(ffmpeg, good, expected_duration_sec=2, expected_canvas=(320, 240))
    assert probe["has_video"] and probe["has_audio"]

    # 画布不符
    with pytest.raises(ValueError, match="画布"):
        verify_mp4_output(ffmpeg, good, expected_duration_sec=2, expected_canvas=(1080, 1920))
    # 时长不符
    with pytest.raises(ValueError, match="时长"):
        verify_mp4_output(ffmpeg, good, expected_duration_sec=30, expected_canvas=(320, 240))
    # 截断文件：文件头仍然是 ftyp，但内容不完整
    body = good.read_bytes()
    truncated = tmp_path / "truncated.mp4"
    truncated.write_bytes(body[: max(64, len(body) // 3)])
    with pytest.raises(ValueError):
        verify_mp4_output(ffmpeg, truncated, expected_duration_sec=2, expected_canvas=(320, 240))
    # 空文件与非 MP4 文件
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(ValueError):
        verify_mp4_output(ffmpeg, empty, expected_duration_sec=2, expected_canvas=(320, 240))
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"definitely not a video")
    with pytest.raises(ValueError, match="不是 MP4"):
        verify_mp4_output(ffmpeg, junk, expected_duration_sec=2, expected_canvas=(320, 240))


def test_export_never_reports_a_clean_mp4_when_output_verification_fails(
        tmp_path: Path, monkeypatch) -> None:
    """合成「成功」但校验不通过时，导出必须如实回退，而不是标成干净的 mp4。"""
    import backend.media as media_module

    monkeypatch.setattr(MediaRepository, "_ffmpeg", staticmethod(lambda: "ffmpeg"))

    def fake_run(argv, **kwargs):  # noqa: ANN001 - mirrors subprocess.run usage
        Path(argv[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(argv[-1]).write_bytes(b"stub")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def boom(*args, **kwargs):
        raise ValueError("合成后的 MP4 无法完整解码：模拟损坏")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(media_module, "verify_mp4_output", boom)

    path = tmp_path / "verify_fail.db"
    with TestClient(create_app(path)) as client:
        project_id = _project_with_shots(client)
        assert client.post(f"/api/projects/{project_id}/demo-media").status_code == 201
        assert client.post(f"/api/projects/{project_id}/timeline").status_code == 201

        export = client.post(f"/api/projects/{project_id}/exports", json={"format": "mp4"}).json()
        assert export["status"] == "completed_with_warnings", export
        assert export["format"] == "draft_package"
        assert "mp4" not in [a["kind"] for a in export["artifacts"]]
        assert "模拟损坏" in (export["warning"] or ""), export["warning"]


def test_failed_export_keeps_the_format_the_user_asked_for(tmp_path: Path) -> None:
    """导出失败时不能谎称用户请求过 auto：请求的格式必须原样记下来。"""
    path = tmp_path / "failed_format.db"
    with TestClient(create_app(path)) as client:
        project_id = _project_with_shots(client)
        assert client.post(f"/api/projects/{project_id}/demo-media").status_code == 201
        assert client.post(f"/api/projects/{project_id}/timeline").status_code == 201

        assets = client.get(f"/api/projects/{project_id}/assets").json()
        victim = path.parent / "projects" / project_id / assets[0]["latest_version"]["relative_path"]
        original = victim.read_bytes()
        victim.write_bytes(original[:-1] + bytes([original[-1] ^ 0xFF]))
        try:
            export = client.post(f"/api/projects/{project_id}/exports",
                                 json={"format": "portable_draft"}).json()
            assert export["status"] == "failed", export
            assert "SHA-256" in (export["error"] or "")
            assert export["format"] == "portable_draft", export["format"]
        finally:
            victim.write_bytes(original)


def test_export_list_returns_recent_jobs_newest_first(tmp_path: Path) -> None:
    """失败或成功的导出都要能被重新查回来，否则用户再也看不到当时发生了什么。"""
    path = tmp_path / "export_list.db"
    with TestClient(create_app(path)) as client:
        project_id = _project_with_shots(client)
        assert client.post(f"/api/projects/{project_id}/demo-media").status_code == 201
        assert client.post(f"/api/projects/{project_id}/timeline").status_code == 201
        first = client.post(f"/api/projects/{project_id}/exports",
                            json={"format": "portable_draft"}).json()
        second = client.post(f"/api/projects/{project_id}/exports",
                             json={"format": "jianying_draft"}).json()

        listed = client.get(f"/api/projects/{project_id}/exports").json()
        assert isinstance(listed, list) and len(listed) == 2, listed
        assert [job["id"] for job in listed] == [second["id"], first["id"]], "应当最新在前"
        assert all("artifacts" in job for job in listed)
        assert client.get(f"/api/projects/{project_id}/exports", params={"limit": 1}).json()[0]["id"] == second["id"]
        # 别的项目看不到这些导出。
        other = client.post("/api/projects", json={"name": "别的项目", "story": "x", "target_duration_sec": 4}).json()
        assert client.get(f"/api/projects/{other['id']}/exports").json() == []


def test_project_path_rejects_traversal(tmp_path: Path) -> None:
    app = create_app(tmp_path / "safe.db")
    with TestClient(app):
        repo = MediaRepository(app.state.database)
        for unsafe in ("../escape", "x/y", "..", "C:evil"):
            try:
                repo._project_dir(unsafe)
            except ValueError:
                pass
            else:
                raise AssertionError(f"unsafe id accepted: {unsafe}")
        try:
            repo._resolve_relative("safe-id", "../../escape")
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe relative path accepted")
