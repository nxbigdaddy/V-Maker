"""Live review-gate check: the exact click path the GUI uses (start,审核, resume?execute=true)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765/api"


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def wait_settled(task_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, task = call("GET", f"/tasks/{task_id}")
        if task["status"] in ("completed", "failed", "paused", "canceled"):
            return task
        time.sleep(0.5)
    raise AssertionError("task did not settle")


_, project = call("POST", "/projects", {
    "name": "审核门实机验证",
    "story": "灯塔看守人用一生的时间给一艘永远不会靠岸的船写信。",
    "target_duration_sec": 30,
    "budget_limit": 5,
})
project_id = project["id"]

status, task = call("POST", f"/projects/{project_id}/pipeline", {"require_review": True})
assert status == 201, (status, task)
task_id = task["id"]

first = wait_settled(task_id)
print("after start:", first["status"], "|", first["message"])
assert first["status"] == "paused" and "审核" in first["message"], first

dashboard = call("GET", f"/projects/{project_id}/dashboard")[1]
stages = {row["stage_key"]: row["status"] for row in dashboard["workflow_stages"]}
print("stages:", stages)
print("task_summary:", dashboard["task_summary"])
assert dashboard["task_summary"].get("paused") == 1, dashboard["task_summary"]
assert stages["project_bible"] == "in_progress", stages
assert stages["screenplay"] == "pending", stages
progress_before = sum(1 for value in stages.values() if value == "completed")

# The GUI "继续" button: one POST, backend schedules the continuation itself.
status, resumed = call("POST", f"/tasks/{task_id}/resume?execute=true")
assert status == 200 and resumed["status"] == "queued", (status, resumed)
second = wait_settled(task_id)
print("after resume 1:", second["status"], "|", second["message"])
assert second["status"] == "paused", second
stages_2 = {row["stage_key"]: row["status"] for row in call("GET", f"/projects/{project_id}/dashboard")[1]["workflow_stages"]}
progress_after = sum(1 for value in stages_2.values() if value == "completed")
print("progress completed stages:", progress_before, "->", progress_after)
assert progress_after >= progress_before, (progress_before, progress_after)

status, resumed = call("POST", f"/tasks/{task_id}/resume?execute=true")
assert status == 200, (status, resumed)
final = wait_settled(task_id)
print("after resume 2:", final["status"], "|", final["message"], "| progress", final["progress"])
assert final["status"] == "completed", final
assert final["progress"] == 1

stages_final = {row["stage_key"]: row["status"] for row in call("GET", f"/projects/{project_id}/dashboard")[1]["workflow_stages"]}
assert set(stages_final.values()) == {"completed"}, stages_final

# Cancel path: a fresh pipeline is canceled before it ever runs.
_, fresh = call("POST", f"/projects/{project_id}/pipeline", {"require_review": False})
call("POST", f"/tasks/{fresh['id']}/cancel")
_, canceled = call("GET", f"/tasks/{fresh['id']}")
status, detail = call("POST", f"/tasks/{fresh['id']}/resume?execute=true")
print("cancel then resume ->", status, detail)
assert status == 409, (status, detail)

call("DELETE", f"/projects/{project_id}")
print("LIVE_REVIEW_GATE_OK")
