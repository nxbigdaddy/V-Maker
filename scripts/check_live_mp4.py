"""Live end-to-end check for the FFmpeg MP4 export path.

This is the first time the MP4 renderer is exercised for real, so it runs the same project twice:

  A. right after the one-click pipeline, whose demo media are SVG placeholders;
  B. after shot images are generated as real PNG bitmaps and the timeline is rebuilt.

Expected: (A) must not silently claim success — FFmpeg has no SVG decoder, and (B) must produce a
genuinely decodable H.264/AAC MP4 whose duration matches the timeline.

Requires the backend to be running with the image test key and a real ffmpeg:

    $env:VMAKER_TEST_IMAGE_KEY="sk-live-mock-image-key-123456"
    $env:VMAKER_FFMPEG_PATH="<path to ffmpeg.exe>"
    python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND = "http://127.0.0.1:8765/api"
MOCK_PORT = 8803
MOCK_KEY = "sk-live-mock-image-key-123456"
ASPECT_RATIO = os.getenv("VMAKER_CHECK_RATIO", "16:9")

import imageio_ffmpeg  # noqa: E402 - bundled runtime helper
from PIL import Image  # noqa: E402

FFMPEG = os.getenv("VMAKER_FFMPEG_PATH") or imageio_ffmpeg.get_ffmpeg_exe()

buffer = io.BytesIO()
Image.new("RGB", (640, 360), (52, 96, 160)).save(buffer, format="PNG")
PNG_BYTES = buffer.getvalue()
print(f"ffmpeg: {FFMPEG}")
print(f"mock PNG: {len(PNG_BYTES)} bytes")

seen: list[dict] = []


class MockImageHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        seen.append({"model": body.get("model")})
        payload = json.dumps({"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode()}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{BACKEND}{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def json_call(method: str, path: str, body: dict | None = None):
    status, raw = call(method, path, body)
    return status, (json.loads(raw) if raw else None)


def wait_task(task_id: str, label: str):
    for _ in range(300):
        _, task = json_call("GET", f"/tasks/{task_id}")
        if task["status"] in ("completed", "failed", "paused", "canceled"):
            print(f"{label}: {task['status']} | {task.get('error') or ''}")
            return task
        time.sleep(1)
    raise AssertionError(f"{label} did not finish")


EXPECTED_CANVAS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}


def probe_mp4(path: Path) -> dict:
    """Decode the whole file and read its real streams/duration back out of ffmpeg."""
    decode = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True, timeout=600)
    info = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, timeout=120)
    text = info.stderr or ""
    duration = re.search(r"Duration: (\d+):(\d+):([\d.]+)", text)
    seconds = None
    if duration:
        seconds = int(duration.group(1)) * 3600 + int(duration.group(2)) * 60 + float(duration.group(3))
    size = re.search(r"Video:.*?, (\d+)x(\d+)", text)
    return {
        "bytes": path.stat().st_size,
        "decode_exit": decode.returncode,
        "decode_errors": (decode.stderr or "").strip()[:300],
        "duration_sec": seconds,
        "width": int(size.group(1)) if size else None,
        "height": int(size.group(2)) if size else None,
        "video": "Video: h264" in text,
        "audio": "Audio: aac" in text,
    }


def export_mp4(project_id: str, label: str) -> dict:
    status, export = json_call("POST", f"/projects/{project_id}/exports", {"format": "mp4"})
    assert status == 201, (status, export)
    print(f"\n=== {label} ===")
    print("format:", export["format"], "| status:", export["status"])
    print("warning:", (export.get("warning") or "")[:400])
    print("artifacts:", [(a["kind"], a["mime_type"], a["byte_size"]) for a in export["artifacts"]])
    return export


server = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockImageHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
print(f"mock image service on http://127.0.0.1:{MOCK_PORT}")

out_dir = Path(os.getenv("TEMP", ".")) / "vmaker-mp4-check"
out_dir.mkdir(parents=True, exist_ok=True)
project_id = None

PURE_DEMO = os.getenv("VMAKER_CHECK_PURE_DEMO") == "1"

try:
    if PURE_DEMO:
        # 纯 demo：不配置任何 provider、不生成镜头图像，验证「开箱即用」的项目能否出 MP4。
        print("pure demo mode: 不配置任何 provider，不生成镜头图像")
    else:
        status, settings = json_call("PUT", "/capability-settings/image", {
            "provider": "openai", "endpoint": f"http://127.0.0.1:{MOCK_PORT}/v1",
            "model": "mock-image-v1", "api_key_env": "VMAKER_TEST_IMAGE_KEY", "unit_cost": 0.05,
        })
        assert status == 200 and settings["configured"] is True, (status, settings)

    status, project = json_call("POST", "/projects", {
        "name": f"MP4 实机验证 {ASPECT_RATIO}", "story": "一台旧收音机在拆迁的城市里寻找最后一次播出的旋律。",
        "target_duration_sec": 10, "budget_limit": 10, "aspect_ratio": ASPECT_RATIO,
    })
    assert status == 201, (status, project)
    project_id = project["id"]
    print(f"project: {project_id} | aspect_ratio={project.get('aspect_ratio')}")

    _, pipeline = json_call("POST", f"/projects/{project_id}/pipeline", {"require_review": False})
    task = wait_task(pipeline["id"], "pipeline")
    assert task["status"] == "completed", task

    # ---------------- A. 素材仍是 SVG 占位图 ----------------
    _, assets = json_call("GET", f"/projects/{project_id}/assets")
    mimes = sorted({(a["latest_version"] or {}).get("mime_type", "?") for a in assets})
    print("asset mimes before image generation:", mimes)

    demo_export = export_mp4(project_id, "A. demo（默认占位素材）请求 MP4")
    demo_kinds = [a["kind"] for a in demo_export["artifacts"]]
    if demo_kinds == ["mp4"]:
        print("A 行为：默认 demo 素材直接产出了真实 MP4（位图占位图）")
    else:
        assert demo_export["format"] != "mp4", "素材不支持时不能声称产出了 MP4"
        assert demo_export["status"] == "completed_with_warnings"
        assert "ffmpeg" in (demo_export["warning"] or "").lower()
        print("A 行为：如实回退，未把草稿包冒充成 MP4")

    if PURE_DEMO:
        mp4 = [a for a in demo_export["artifacts"] if a["kind"] == "mp4"]
        assert mp4, f"纯 demo 项目没有产出 MP4：{[a['kind'] for a in demo_export['artifacts']]}"
        assert demo_export["status"] == "completed", demo_export
        status, raw = call("GET", f"/artifacts/{mp4[0]['id']}/download")
        assert status == 200 and raw[4:8] == b"ftyp", (status, raw[:12])
        target = out_dir / "pure-demo.mp4"
        target.write_bytes(raw)
        probe = probe_mp4(target)
        print("\n--- ffmpeg 回读结果（纯 demo） ---")
        for key, value in probe.items():
            print(f"  {key}: {value}")
        assert probe["decode_exit"] == 0, probe
        assert probe["video"] and probe["audio"], probe
        want = EXPECTED_CANVAS[ASPECT_RATIO]
        assert (probe["width"], probe["height"]) == want, (probe, want)
        print(f"\n开箱即用的 demo 项目产出真实 MP4：{probe['width']}x{probe['height']}，"
              f"{probe['duration_sec']:.1f}s")
        print("LIVE_MP4_PURE_DEMO_OK")
        raise SystemExit(0)

    # ---------------- B. 生成真实位图并重建时间线 ----------------
    status, image_task = json_call("POST", f"/projects/{project_id}/media-images")
    assert status == 201, (status, image_task)
    assert wait_task(image_task["id"], "image generation")["status"] == "completed"
    assert seen, "mock image service was never called"

    _, assets = json_call("GET", f"/projects/{project_id}/assets")
    raster = [a for a in assets if "svg" not in ((a["latest_version"] or {}).get("mime_type") or "")]
    print("raster image assets:", len(raster))

    status, timeline = json_call("POST", f"/projects/{project_id}/timeline")
    assert status == 201, (status, timeline)
    print("timeline version:", timeline["version"])

    real_export = export_mp4(project_id, "B. 位图素材请求 MP4")
    mp4 = [a for a in real_export["artifacts"] if a["kind"] == "mp4"]
    assert mp4, f"no mp4 artifact: {[a['kind'] for a in real_export['artifacts']]}"
    assert real_export["format"] == "mp4"

    status, raw = call("GET", f"/artifacts/{mp4[0]['id']}/download")
    assert status == 200, status
    target = out_dir / "video.mp4"
    target.write_bytes(raw)
    print("downloaded mp4:", len(raw), "bytes ->", target)

    probe = probe_mp4(target)
    print("\n--- ffmpeg 回读结果 ---")
    for key, value in probe.items():
        print(f"  {key}: {value}")

    assert raw[4:8] == b"ftyp", f"not an ISO-BMFF file: {raw[:12]!r}"
    assert probe["decode_exit"] == 0, probe
    assert probe["video"] and probe["audio"], probe
    expected = float(timeline["timeline"]["duration_sec"])
    assert probe["duration_sec"] is not None
    assert abs(probe["duration_sec"] - expected) < 1.5, (probe["duration_sec"], expected)
    # 成片画布必须跟项目的宽高比一致，不能所有项目都出横屏。
    want = EXPECTED_CANVAS[ASPECT_RATIO]
    assert (probe["width"], probe["height"]) == want, (
        f"{ASPECT_RATIO} 项目应输出 {want[0]}x{want[1]}，实际 {probe['width']}x{probe['height']}")
    print(f"\n真实可解码 MP4：H.264 + AAC，{probe['width']}x{probe['height']}，"
          f"{probe['duration_sec']:.1f}s（时间线 {expected:.1f}s）")
    print("LIVE_MP4_OK")
finally:
    server.shutdown()
    # 恢复图像能力，避免把测试配置留在用户环境里。
    json_call("PUT", "/capability-settings/image", {
        "provider": "demo", "endpoint": "", "model": "demo-svg-v1", "api_key_env": "", "unit_cost": 0,
    })
    print("image capability restored to demo")
    # 清掉本次检查建的项目，免得测试项目堆在用户的首页上。
    if project_id:
        status, _ = call("DELETE", f"/projects/{project_id}")
        print(f"test project removed: HTTP {status}")
