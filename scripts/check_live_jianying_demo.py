"""Live check: does a **pure demo** project (no provider configured) produce usable exports?

This is the "out of the box" question. The demo media used to be SVG, which neither FFmpeg nor
剪映 can read, so a fresh project could produce neither a real MP4 nor a non-empty draft. The demo
placeholder is a bitmap now, and this script proves what that buys us, without configuring any
provider and without spending anything.

Requires the backend to be running (FFmpeg optional; the MP4 half is skipped without it).

    python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

import io
import json
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
    for _ in range(240):
        _, task = api("GET", f"/tasks/{task_id}")
        if task["status"] in ("completed", "failed", "paused", "canceled"):
            return task
        time.sleep(1)
    raise AssertionError("task did not finish")


_, project = api("POST", "/projects", {
    "name": "纯 demo 导出验证", "story": "一只猫在雨夜找到了回家的路。",
    "target_duration_sec": 8, "aspect_ratio": "16:9",
})
project_id = project["id"]
print("project:", project_id)

try:
    _, pipeline = api("POST", f"/projects/{project_id}/pipeline", {"require_review": False})
    task = wait(pipeline["id"])
    print("pipeline:", task["status"])
    assert task["status"] == "completed", task

    _, assets = api("GET", f"/projects/{project_id}/assets")
    mimes = sorted({(a["latest_version"] or {}).get("mime_type", "?") for a in assets})
    print("asset mimes (no provider configured):", mimes)
    assert "image/png" in mimes, "demo 图像应当是位图"
    assert not any("svg" in (m or "") for m in mimes), "demo 图像不应再是 SVG"

    # ---- 剪映草稿：以前全是 SVG，会退化成空草稿再回退成通用包 ----
    _, export = api("POST", f"/projects/{project_id}/exports", {"format": "jianying_draft"})
    print("\n剪映草稿 format:", export["format"], "| status:", export["status"])
    print("warning:", (export.get("warning") or "")[:220])
    kinds = [a["kind"] for a in export["artifacts"]]
    print("artifacts:", kinds)
    assert kinds == ["jianying_draft"], f"纯 demo 项目应能产出真实剪映草稿，实际 {kinds}"

    artifact = export["artifacts"][0]
    status, raw = call("GET", f"/artifacts/{artifact['id']}/download")
    assert status == 200, status
    archive = zipfile.ZipFile(io.BytesIO(raw))
    name = next(n for n in archive.namelist() if n.endswith("draft_content.json"))
    draft = json.loads(archive.read(name).decode("utf-8"))
    print("canvas:", draft["canvas_config"], "| duration(us):", draft["duration"])
    for track in draft["tracks"]:
        print(f"  track {track['type']}: {len(track['segments'])} segments")
    segments = sum(len(track["segments"]) for track in draft["tracks"])
    assert segments > 0, "草稿不应为空"
    assert draft["materials"]["videos"], "草稿应当引用到真实素材"
    print(f"草稿含 {segments} 个片段，引用 {len(draft['materials']['videos'])} 个素材")

    # ---- MP4：没有 ffmpeg 时如实回退，有 ffmpeg 时必须出真片 ----
    _, mp4_export = api("POST", f"/projects/{project_id}/exports", {"format": "mp4"})
    mp4_kinds = [a["kind"] for a in mp4_export["artifacts"]]
    print("\nMP4 format:", mp4_export["format"], "| status:", mp4_export["status"], "| artifacts:", mp4_kinds)
    if mp4_kinds == ["mp4"]:
        status, body = call("GET", f"/artifacts/{mp4_export['artifacts'][0]['id']}/download")
        assert status == 200 and body[4:8] == b"ftyp", (status, body[:12])
        print(f"纯 demo 项目产出真实 MP4：{len(body)} 字节，ISO-BMFF 头正确")
    else:
        assert mp4_export["format"] != "mp4", "没有 ffmpeg 时不能声称产出了 MP4"
        print("未检测到 ffmpeg：如实回退，未把草稿包冒充成 MP4")

    print("\nPURE_DEMO_EXPORT_OK")
finally:
    status, _ = call("DELETE", f"/projects/{project_id}")
    print(f"test project removed: HTTP {status}")
