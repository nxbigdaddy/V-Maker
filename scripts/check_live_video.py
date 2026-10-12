"""Live end-to-end check for the video capability, against a real running backend.

Runs a mock image-to-video service on loopback that speaks the convention V-Maker documents
(submit -> poll -> download), points the live backend's `video` capability at it, generates a
video for one shot, and verifies what actually landed on disk — including HTTP Range support,
because a `<video>` element cannot seek without it.

Requires the backend to be started with the test key in its environment:

    $env:VMAKER_TEST_VIDEO_KEY="sk-live-mock-video-key-123456"
    python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKEND = "http://127.0.0.1:8765/api"
MOCK_PORT = 8801
MOCK_KEY = "sk-live-mock-video-key-123456"
# Not a playable file: this only proves the bytes travel through unchanged.
VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"vmaker-mock-video-payload" * 64

requests: list[dict] = []
poll_count = {"n": 0}


class MockVideoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        requests.append({
            "method": "POST", "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": json.loads(raw.decode("utf-8")),
        })
        self._json({"id": "job_live_1", "status": "queued"})

    def do_GET(self):
        requests.append({"method": "GET", "path": self.path,
                         "authorization": self.headers.get("Authorization")})
        if self.path.endswith("/files/out.mp4"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(VIDEO_BYTES)))
            self.end_headers()
            self.wfile.write(VIDEO_BYTES)
            return
        poll_count["n"] += 1
        if poll_count["n"] == 1:
            self._json({"status": "processing", "progress": 0.35, "message": "rendering"})
        else:
            self._json({"status": "succeeded", "progress": 1.0,
                        "video_url": f"http://127.0.0.1:{MOCK_PORT}/files/out.mp4"})


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{BACKEND}{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read(), {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as error:
        return error.code, error.read(), {k.lower(): v for k, v in error.headers.items()}


def json_call(method: str, path: str, body: dict | None = None):
    status, raw, _ = call(method, path, body)
    return status, (json.loads(raw) if raw else None)


server = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockVideoHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
print(f"mock video service on http://127.0.0.1:{MOCK_PORT}")

try:
    status, settings = json_call("PUT", "/capability-settings/video", {
        "provider": "openai",
        "endpoint": f"http://127.0.0.1:{MOCK_PORT}/v1",
        "model": "mock-video-v1",
        "api_key_env": "VMAKER_TEST_VIDEO_KEY",
        "unit_cost": 0.3,
    })
    assert status == 200, (status, settings)
    print("video capability:", settings["provider"], settings["model"], "configured =", settings["configured"])
    assert settings["configured"] is True

    # The connection test must never trigger a real generation.
    status, probe = json_call("POST", "/capability-settings/video/test", {"live": True})
    assert status == 200, (status, probe)
    print("probe:", {k: probe[k] for k in ("configured", "reachable", "ok", "live")})
    print("  message:", probe["message"][:100])
    assert all(item["path"].startswith("/v1/video") is False for item in requests), requests

    status, project = json_call("POST", "/projects", {
        "name": "视频链路实机验证",
        "story": "老放映员把最后一卷胶片交给了一个从没看过电影的孩子。",
        "target_duration_sec": 20,
        "budget_limit": 10,
    })
    assert status == 201, (status, project)
    project_id = project["id"]

    _, pipeline = json_call("POST", f"/projects/{project_id}/pipeline", {"require_review": False})
    for _ in range(90):
        _, task = json_call("GET", f"/tasks/{pipeline['id']}")
        if task["status"] in ("completed", "failed", "paused", "canceled"):
            break
        time.sleep(1)
    assert task["status"] == "completed", task

    _, shots = json_call("GET", f"/projects/{project_id}/shots")
    shot_id = shots[0]["id"]
    print("shots:", len(shots), "using", shot_id)

    status, video_task = json_call("POST", f"/projects/{project_id}/shots/{shot_id}/video")
    assert status == 201, (status, video_task)
    print("video task:", video_task["task_type"], "capability =", video_task["capability"],
          "estimated_cost =", video_task["estimated_cost"])
    assert video_task["task_type"] == "shot_video" and video_task["capability"] == "video"
    assert video_task["estimated_cost"] == 0.3

    seen_message = ""
    for _ in range(120):
        _, task = json_call("GET", f"/tasks/{video_task['id']}")
        seen_message = task.get("message") or seen_message
        if task["status"] in ("completed", "failed", "paused", "canceled"):
            break
        time.sleep(0.5)
    print("video task final:", task["status"], "|", task.get("error") or task.get("message"))
    assert task["status"] == "completed", task

    _, assets = json_call("GET", f"/projects/{project_id}/assets")
    videos = [a for a in assets if a["kind"] == "video"]
    print("video assets:", len(videos))
    assert videos, "no video asset was registered"
    version = videos[-1]["latest_version"]
    print("mime:", version["mime_type"], "demo:", version["demo"], "bytes:", version["byte_size"])
    assert version["demo"] is False and version["mime_type"] == "video/mp4"
    print("metadata:", {k: version["metadata"].get(k) for k in ("provider", "model", "duration_sec")})

    path = f"/projects/{project_id}/assets/{videos[-1]['id']}/versions/{version['id']}/content"
    status, body, headers = call("GET", path)
    assert status == 200 and body == VIDEO_BYTES, (status, len(body))
    print("served bytes match the mock exactly:", len(body))
    assert headers.get("x-content-type-options") == "nosniff"

    status, body, headers = call("GET", path, headers={"Range": "bytes=0-9"})
    print("range:", status, len(body), headers.get("content-range"))
    assert status == 206 and len(body) == 10, (status, len(body))

    print("mock saw:", [(r["method"], r["path"]) for r in requests])
    submits = [r for r in requests if r["method"] == "POST" and r["path"].endswith("/video/generations")]
    assert len(submits) == 1, submits
    payload = submits[0]["body"]
    print("submit body keys:", sorted(payload))
    assert payload["model"] == "mock-video-v1" and payload["duration_sec"] >= 1
    assert submits[0]["authorization"] == f"Bearer {MOCK_KEY}"

    _, all_tasks = json_call("GET", f"/projects/{project_id}/tasks")
    assert MOCK_KEY not in json.dumps(all_tasks, ensure_ascii=False)
    print("secret never appears in task records")

    json_call("DELETE", f"/projects/{project_id}")
    print("LIVE_VIDEO_OK")
finally:
    try:
        restored = json_call("PUT", "/capability-settings/video", {
            "provider": "demo", "endpoint": "", "model": "demo-unsupported-v1",
            "api_key_env": "OPENAI_API_KEY", "unit_cost": 0,
        })
        print("video capability restored to:", restored[1]["provider"], restored[1]["model"])
    except Exception as error:  # noqa: BLE001 - cleanup must not mask the real result
        print("WARNING: could not restore video capability:", error)
    server.shutdown()
