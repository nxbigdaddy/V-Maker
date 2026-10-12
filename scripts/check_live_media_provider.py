"""End-to-end proof that a real HTTP image service flows through V-Maker.

Runs a tiny mock image API on loopback, points the live backend's image capability at it,
generates shots, generates one image per shot, and checks what actually landed on disk.
"""
from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKEND = "http://127.0.0.1:8765/api"
MOCK_PORT = 8799
MOCK_KEY = "sk-live-mock-image-key-987654321"
# A real 1x1 PNG, so magic-byte detection and preview rendering both have valid input.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAAMBAQAY3Y2wAAAAAElFTkSuQmCC"
)

received: list[dict] = []


class MockImageHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the console clean
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        received.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": json.loads(raw.decode("utf-8")),
        })
        payload = json.dumps({
            "data": [{"b64_json": base64.b64encode(PNG_1X1).decode("ascii")}],
            "output_format": "png",
        }).encode("utf-8")
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
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def json_call(method: str, path: str, body: dict | None = None):
    status, raw = call(method, path, body)
    return status, (json.loads(raw) if raw else None)


server = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockImageHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
print(f"mock image service on http://127.0.0.1:{MOCK_PORT}")

try:
    # 1. Point the image capability at the mock service, using a key only the backend process has.
    status, settings = json_call("PUT", "/capability-settings/image", {
        "provider": "openai",
        "endpoint": f"http://127.0.0.1:{MOCK_PORT}/v1",
        "model": "mock-image-v1",
        "api_key_env": "VMAKER_TEST_IMAGE_KEY",
        "unit_cost": 0.25,
    })
    assert status == 200, (status, settings)
    print("image capability:", settings["provider"], settings["model"], "configured =", settings["configured"])
    assert settings["configured"] is True

    # 2. A text capability call must still behave exactly as before (same row, new API).
    _, text = json_call("GET", "/capability-settings/text")
    _, legacy = json_call("GET", "/provider-settings")
    assert text["provider"] == legacy["provider"] and text["model"] == legacy["model"], (text, legacy)
    print("text capability agrees with legacy /provider-settings:", text["provider"], text["model"])

    # 3. Build a project with shots.
    status, project = json_call("POST", "/projects", {
        "name": "真实图像链路验证",
        "story": "修钟人发现城里所有钟表都慢了一分钟，而那一分钟属于他自己。",
        "target_duration_sec": 24,
        "budget_limit": 10,
    })
    assert status == 201, (status, project)
    project_id = project["id"]
    _, pipeline = json_call("POST", f"/projects/{project_id}/pipeline", {"require_review": False})
    for _ in range(60):
        _, task = json_call("GET", f"/tasks/{pipeline['id']}")
        if task["status"] in ("completed", "failed", "paused", "canceled"):
            break
        time.sleep(1)
    assert task["status"] == "completed", task
    _, shots = json_call("GET", f"/projects/{project_id}/shots")
    print("shots:", len(shots))
    assert shots

    # 4. Generate one real image per shot through the mocked HTTP service.
    status, image_task = json_call("POST", f"/projects/{project_id}/media-images")
    assert status == 201, (status, image_task)
    print("image task estimated_cost:", image_task["estimated_cost"], "provider:", image_task["provider"])
    assert image_task["estimated_cost"] == round(0.25 * len(shots), 10)
    for _ in range(120):
        _, task = json_call("GET", f"/tasks/{image_task['id']}")
        if task["status"] in ("completed", "failed", "paused", "canceled"):
            break
        time.sleep(1)
    print("image task:", task["status"], task.get("error") or "")
    assert task["status"] == "completed", task

    # 5. What actually landed: real bytes, honest demo flag, provider metadata.
    _, assets = json_call("GET", f"/projects/{project_id}/assets")
    ai_images = [a for a in assets if a["kind"] == "image" and a["latest_version"]["demo"] is False]
    print("AI images registered:", len(ai_images), "of", len(shots), "shots")
    assert len(ai_images) == len(shots), (len(ai_images), len(shots))
    version = ai_images[0]["latest_version"]
    assert version["mime_type"] == "image/png", version["mime_type"]
    metadata = version["metadata"]
    assert metadata["provider"] == "openai" and metadata["model"] == "mock-image-v1", metadata
    print("metadata provider/model:", metadata["provider"], metadata["model"])
    print("prompt sample:", metadata["prompt"][:70])

    # 6. The served content must be byte-identical to what the mock returned.
    path = f"/projects/{project_id}/assets/{ai_images[0]['id']}/versions/{version['id']}/content"
    status, body = call("GET", path)
    assert status == 200 and body == PNG_1X1, (status, len(body))
    print("served bytes match the mock exactly:", len(body), "bytes")

    # 7. The mock saw a genuine API call with the backend-held key, one per shot.
    assert len(received) == len(shots), len(received)
    assert all(item["path"].endswith("/images/generations") for item in received)
    assert all(item["authorization"] == f"Bearer {MOCK_KEY}" for item in received)
    assert all(item["body"]["response_format"] == "b64_json" for item in received)
    assert all(item["body"]["model"] == "mock-image-v1" for item in received)
    prompts = [item["body"]["prompt"] for item in received]
    assert all(prompt.strip() for prompt in prompts), "every shot must produce a non-empty prompt"
    # Identical storyboard rows legitimately produce identical prompts, so uniqueness is reported,
    # not required: what matters is one authenticated call per shot with that shot's own fields.
    print("mock observed", len(received), "authenticated calls with", len(set(prompts)), "distinct prompts")

    # 8. The secret must never leak into task records or the API surface.
    _, all_tasks = json_call("GET", f"/projects/{project_id}/tasks")
    blob = json.dumps(all_tasks, ensure_ascii=False)
    assert MOCK_KEY not in blob, "the API key leaked into task records"
    _, diagnostics = json_call("GET", "/system/diagnostics")
    assert MOCK_KEY not in json.dumps(diagnostics, ensure_ascii=False)
    print("secret never appears in task records or diagnostics")

    json_call("DELETE", f"/projects/{project_id}")
    print("LIVE_MEDIA_PROVIDER_OK")
finally:
    # Always hand the image capability back to the offline default, so a later GUI session
    # cannot accidentally call a mock service that is no longer running.
    try:
        restored = json_call("PUT", "/capability-settings/image", {
            "provider": "demo", "endpoint": "", "model": "demo-svg-v1",
            "api_key_env": "OPENAI_API_KEY", "unit_cost": 0,
        })
        print("image capability restored to:", restored[1]["provider"], restored[1]["model"])
    except Exception as error:  # noqa: BLE001 - cleanup must never mask the real result
        print("WARNING: could not restore image capability:", error)
    server.shutdown()
