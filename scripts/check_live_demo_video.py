"""Live check: the demo video capability now produces a real placeholder clip.

Previously `demo` refused outright ("a placeholder cannot be a real video"), so the video path was
never exercisable without paying a service. With a local FFmpeg the placeholder image becomes a real
decodable MP4, which means the whole video path can finally be run for real:

* the shot gets a genuine video asset, marked as a **placeholder** (not AI-generated);
* the Jianying exporter's "a video asset beats the bitmap" branch runs against a real clip instead
  of a corrupt stub — that branch used to be skipped on this machine;
* the MP4 export still uses bitmaps for compositing, so nothing here changes its behaviour.

Also verifies the honest failure branch: with no FFmpeg the task must fail and write nothing.

Requires the backend to be running.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import zipfile

BACKEND = "http://127.0.0.1:8765/api"


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{BACKEND}{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def api(method: str, path: str, body: dict | None = None):
    status, raw = call(method, path, body)
    return status, (json.loads(raw) if raw else None)


def wait(task_id: str) -> dict:
    for _ in range(300):
        _, task = api("GET", f"/tasks/{task_id}")
        if task["status"] in ("completed", "failed", "paused", "canceled"):
            return task
        time.sleep(1)
    raise AssertionError("task did not finish")


try:
    from backend.media import MediaRepository

    ffmpeg = MediaRepository._ffmpeg()
except Exception:  # pragma: no cover - only when run outside the project root
    ffmpeg = None
print("ffmpeg:", ffmpeg or "（不可用，脚本只能验证失败分支）")

_, project = api("POST", "/projects", {
    "name": "demo 占位视频实机验证", "story": "一只猫在雨夜找到了回家的路。",
    "target_duration_sec": 8, "aspect_ratio": "16:9",
})
project_id = project["id"]
print("project:", project_id)

try:
    _, pipeline = api("POST", f"/projects/{project_id}/pipeline", {"require_review": False})
    task = wait(pipeline["id"])
    assert task["status"] == "completed", task
    _, shots = api("GET", f"/projects/{project_id}/shots")
    assert shots, "no shots"

    status, created = api("POST", f"/projects/{project_id}/shots/{shots[0]['id']}/video")
    assert status == 201, (status, created)
    executed = wait(created["id"])
    print("shot video task:", executed["status"], "|", (executed.get("error") or "")[:160])

    if not ffmpeg:
        # 没有 ffmpeg 时必须失败，并且什么都不写。
        assert executed["status"] == "failed", executed
        assert "FFmpeg" in (executed["error"] or ""), executed["error"]
        _, assets = api("GET", f"/projects/{project_id}/assets")
        assert not [a for a in assets if a["kind"] == "video"], "没有 ffmpeg 却产出了视频素材"
        print("no-ffmpeg branch: failed honestly, no video asset")
        raise SystemExit(0)

    assert executed["status"] == "completed", executed
    _, assets = api("GET", f"/projects/{project_id}/assets")
    videos = [a for a in assets if a["kind"] == "video"]
    assert len(videos) == 1, assets
    version = videos[0]["latest_version"]
    print("video asset:", version["mime_type"], version["byte_size"], "bytes | demo =", version["demo"])
    assert version["demo"] is True, "本地占位片段必须标记为 demo，不能冒充 AI 生成"
    assert version["mime_type"] == "video/mp4"

    status, body = call("GET", f"/api/asset-versions/{version['id']}/content".replace("/api", ""))
    assert status == 200 and body[4:8] == b"ftyp", (status, body[:12])
    decode = subprocess.run([ffmpeg, "-v", "error", "-i", "-", "-f", "null", "-"],
                            input=body, capture_output=True, timeout=300)
    print("decode exit:", decode.returncode, "| errors:", decode.stderr.decode("utf-8", "replace")[:120])
    assert decode.returncode == 0, decode.stderr[:300]

    # 剪映草稿应当引用这条真实视频（而不是回退到位图）——这条分支以前在本机一直被跳过。
    status, export = api("POST", f"/projects/{project_id}/exports", {"format": "jianying_draft"})
    assert status == 201, (status, export)
    print("\n剪映草稿:", export["status"], "|", (export.get("warning") or "")[:160])
    artifact = [a for a in export["artifacts"] if a["kind"] == "jianying_draft"]
    assert artifact, [a["kind"] for a in export["artifacts"]]
    status, raw = call("GET", f"/artifacts/{artifact[0]['id']}/download")
    assert status == 200, status
    archive = zipfile.ZipFile(io.BytesIO(raw))
    name = next(n for n in archive.namelist() if n.endswith("draft_content.json"))
    draft = json.loads(archive.read(name).decode("utf-8"))
    materials = draft["materials"]["videos"]
    kinds = sorted({m.get("type") for m in materials})
    print("draft 素材 type:", kinds, "| 数量:", len(materials))
    assert "video" in kinds, f"草稿没有引用真实视频素材：{kinds}"
    print("真实视频素材已进入剪映草稿（此前该分支因缺少可用视频而从未跑过）")

    print("\nDEMO_VIDEO_OK")
finally:
    status, _ = call("DELETE", f"/projects/{project_id}")
    print(f"test project removed: HTTP {status}")
