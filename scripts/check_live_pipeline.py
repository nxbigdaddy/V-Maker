"""Live one-click pipeline check against a running local backend (http://127.0.0.1:8765)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765/api"


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read(), {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as error:
        return error.code, error.read(), {k.lower(): v for k, v in error.headers.items()}


status, payload, _ = call("POST", "/projects", {
    "name": "一键流水线实机验证",
    "story": "一台旧收音机在拆迁的城市里寻找它最后一次播出的那段旋律。",
    "target_duration_sec": 30,
    "budget_limit": 5,
})
assert status == 201, (status, payload)
project_id = json.loads(payload)["id"]
print("project", project_id)

status, payload, _ = call("POST", f"/projects/{project_id}/pipeline", {"require_review": False})
assert status == 201, (status, payload)
task_id = json.loads(payload)["id"]
print("pipeline task", task_id, "status", json.loads(payload)["status"])

for _ in range(40):
    status, payload, _ = call("GET", f"/tasks/{task_id}")
    task = json.loads(payload)
    if task["status"] in ("completed", "failed", "paused", "canceled"):
        break
    time.sleep(1)
print("final status", task["status"], "progress", task["progress"], "message", task["message"])
assert task["status"] == "completed", task

stages = json.loads(call("GET", f"/projects/{project_id}/dashboard")[1])["workflow_stages"]
print("workflow", {row["stage_key"]: row["status"] for row in stages})
assert all(row["status"] == "completed" for row in stages), stages

assets = json.loads(call("GET", f"/projects/{project_id}/assets")[1])
print("assets", len(assets), sorted({row["kind"] for row in assets}))
assert assets

image = next(row for row in assets if row["kind"] == "image")
audio = next(row for row in assets if row["kind"] == "audio")
for asset in (image, audio):
    version_id = asset["latest_version"]["id"]
    path = f"/projects/{project_id}/assets/{asset['id']}/versions/{version_id}/content"
    status, body, headers = call("GET", path)
    print("content", asset["kind"], status, len(body), headers.get("content-type"), headers.get("x-content-type-options"))
    assert status == 200 and body
    assert headers.get("x-content-type-options") == "nosniff"
    assert headers.get("etag", "").strip('"') == asset["latest_version"]["sha256"]

status, body, headers = call(
    "GET", f"/projects/{project_id}/assets/{audio['id']}/versions/{audio['latest_version']['id']}/content",
    headers={"Range": "bytes=0-9"},
)
print("range", status, len(body), headers.get("content-range"))
assert status == 206 and len(body) == 10, (status, len(body))

status, _, _ = call("GET", "/asset-versions/does_not_exist/content")
print("missing version ->", status)
assert status == 404

call("DELETE", f"/projects/{project_id}")
status, _, _ = call("GET", f"/projects/{project_id}/assets/{image['id']}/versions/{image['latest_version']['id']}/content")
print("after soft delete ->", status)
assert status == 404

print("LIVE_PIPELINE_OK")
