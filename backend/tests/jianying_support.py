"""Shared scaffolding for the Jianying-draft tests.

Both ``test_media_export.py`` and ``test_jianying_export.py`` need a project whose shots really own
*bitmap* media: the demo image provider writes SVG, which Jianying cannot import. Patching the
registration step is the honest way to produce a PNG without pretending the demo provider changed.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.media import MediaRepository, demo_storyboard_svg

#: A real, complete 1x1 PNG — bytes a bitmap image provider would plausibly have stored.
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63"
    "f8cfc0f01f00050101006d5ef6d90000000049454e44ae426082"
)


def project_with_shots(client: TestClient, *, name: str = "媒体测试", story: str = "一只猫安全回家。",
                       target_duration_sec: int = 8, aspect_ratio: str = "16:9") -> str:
    """Create a project with an analysis and a generated screenplay; return its id."""
    project = client.post("/api/projects", json={
        "name": name, "story": story, "target_duration_sec": target_duration_sec,
        "aspect_ratio": aspect_ratio,
    }).json()
    task = client.post(f"/api/projects/{project['id']}/analysis-tasks").json()
    client.post(f"/api/tasks/{task['id']}/execute")
    assert client.post(f"/api/projects/{project['id']}/screenplay/generate").status_code == 201
    return str(project["id"])


def register_image_bytes(monkeypatch, data: bytes, mime_type: str, suffix: str,
                         shots: set[str] | None = None) -> None:
    """Force demo *image* assets to be exactly these bytes/mime/suffix.

    ``shots=None`` patches every image; a ``shots`` set limits it to those shot ids. The demo
    provider now writes bitmap PNGs, so a test that needs the SVG case (which FFmpeg cannot decode
    and 剪映 cannot import) has to ask for it explicitly instead of relying on the default.
    """
    original = MediaRepository._register_bytes

    def register(self, c, project_id, kind, source_type, data_unused, suffix_unused, mime_unused,
                 source_shot_id=None, **kwargs):
        if kind == "image" and (shots is None or source_shot_id in shots):
            return original(self, c, project_id, kind, source_type, data, suffix, mime_type,
                            source_shot_id, **kwargs)
        return original(self, c, project_id, kind, source_type, data_unused, suffix_unused, mime_unused,
                        source_shot_id, **kwargs)

    monkeypatch.setattr(MediaRepository, "_register_bytes", register)


def register_svg_images(monkeypatch, shots: set[str] | None = None) -> None:
    """Make demo image assets SVG again, for the "Jianying/FFmpeg cannot read this" cases."""
    register_image_bytes(monkeypatch, demo_storyboard_svg(1, "svg fixture"), "image/svg+xml", ".svg", shots)


def register_png_images(monkeypatch, shots: set[str] | None = None) -> None:
    """Make demo *image* assets a real bitmap PNG."""
    register_image_bytes(monkeypatch, PNG_1PX, "image/png", ".png", shots)


class ExplodingDraftFolder:
    """A ``DraftFolder`` stand-in whose ``create_draft`` fails, like a broken installation would."""

    def __init__(self, folder_path: str, fallback_loader=None) -> None:
        self.folder_path = folder_path

    def create_draft(self, draft_name: str, width: int, height: int, fps: int = 30, **kwargs):
        raise OSError("simulated draft folder failure")


def fake_library(*, draft_folder=None) -> SimpleNamespace:
    """The minimal library surface ``backend.jianying`` needs, for monkeypatched tests."""
    return SimpleNamespace(
        DraftFolder=draft_folder or ExplodingDraftFolder,
        TrackSpec=lambda *args, **kwargs: SimpleNamespace(args=args, kwargs=kwargs),
        TrackType=SimpleNamespace(video="video", audio="audio", text="text"),
        Timerange=lambda start, duration: SimpleNamespace(start=start, duration=duration),
        VideoMaterial=lambda *args, **kwargs: SimpleNamespace(args=args),
        VideoSegment=lambda *args, **kwargs: SimpleNamespace(args=args),
        AudioMaterial=lambda *args, **kwargs: SimpleNamespace(args=args),
        AudioSegment=lambda *args, **kwargs: SimpleNamespace(args=args),
        TextSegment=lambda *args, **kwargs: SimpleNamespace(args=args),
        TextStyle=lambda *args, **kwargs: SimpleNamespace(args=args),
    )


def generate_demo_media(client: TestClient, project_id: str) -> None:
    response = client.post(f"/api/projects/{project_id}/demo-media")
    assert response.status_code == 201, response.text


def build_timeline(client: TestClient, project_id: str) -> None:
    response = client.post(f"/api/projects/{project_id}/timeline")
    assert response.status_code == 201, response.text


def shot_durations(client: TestClient, project_id: str) -> dict[str, int]:
    """``shot_id -> duration_sec`` for the project's current shots."""
    shots = client.get(f"/api/projects/{project_id}/shots").json()
    return {str(shot["id"]): int(shot["duration"]) for shot in shots}


MINIMAL_MP4 = bytes.fromhex(
    "0000001c667479706d703432000002006d70343269736f6d00000008667265650000002d6d646174"
)


def real_mp4(path: Path, seconds: float = 2) -> bool:
    """Write a genuine, decodable MP4 when ffmpeg is available. Returns whether it was generated.

    ``pyJianYingDraft`` reads a material's real duration and size, so a hand-written stub is
    *rejected* — which is itself worth knowing, but it cannot demonstrate "the newest video wins".
    Pass ``seconds`` at least as long as the shot: the library refuses a timerange that would run
    past the material, and a too-short clip therefore proves nothing about video preference.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # 夹具需要一个「真正能用」的 ffmpeg，而 VMAKER_FFMPEG_PATH 在这些用例里被故意设成不存在的路径
    # （为了让导出走草稿分支）。两件事要分开，否则这条用例会在有 ffmpeg 的机器上被误跳过。
    saved = os.environ.pop("VMAKER_FFMPEG_PATH", None)
    try:
        ffmpeg = MediaRepository._ffmpeg()
    finally:
        if saved is not None:
            os.environ["VMAKER_FFMPEG_PATH"] = saved
    if ffmpeg:
        completed = subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i", f"color=c=blue:s=64x64:d={seconds}:r=30", "-t", str(seconds),
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        if completed.returncode == 0 and path.is_file() and path.stat().st_size > 0:
            return True
    path.write_bytes(MINIMAL_MP4)
    return False


def add_video_asset(database, project_id: str, shot_id: str, root: Path,
                    name: str = "shot.mp4", seconds: float = 2) -> tuple[str, bool]:
    """Register a video asset row pointing at a real file, mimicking a video provider's output.

    Returns ``(absolute path, usable)``: ``usable`` is False when only a stub could be written, so a
    test can state what it actually verified instead of assuming a decoder it may not have.
    ``seconds`` must cover the shot's own duration, or the library will refuse the clip.
    """
    path = root / "assets" / "manual-video" / name
    usable = real_mp4(path, seconds)
    relative = path.relative_to(root).as_posix()
    asset_id = f"asset_video_{shot_id[-8:]}"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO assets (id,project_id,kind,source_type,source_shot_id,source_task_id,created_at)"
            " VALUES (?,?,'video','import',?,NULL,'2024-01-01T00:00:00+00:00')",
            (asset_id, project_id, shot_id))
        connection.execute(
            "INSERT INTO asset_versions (id,asset_id,version,sha256,mime_type,relative_path,byte_size,"
            "demo,metadata_json,created_at) VALUES (?,?,1,?,?,?,?,0,'{}','2024-01-01T00:00:00+00:00')",
            (f"{asset_id}_v1", asset_id, digest, "video/mp4", relative, path.stat().st_size))
    return str(path), usable
