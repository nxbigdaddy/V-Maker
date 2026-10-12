"""Live proof that a task recovered after an interrupted run is actually executed.

Two phases so the backend can be restarted in between:

    python scripts/check_live_worker.py prepare   # creates a project + a fake interrupted task
    # ... restart the backend here ...
    python scripts/check_live_worker.py verify    # asserts the worker finished it on its own

The key point: nothing in the verify phase calls /execute. If the queue worker is missing, the task
sits at 'queued' forever, which is exactly the defect this checks for.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8765/api"
STATE = Path("backend/data/worker-check.json")


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def prepare() -> int:
    status, project = call("POST", "/projects", {
        "name": "队列恢复实机验证",
        "story": "夜班列车员发现每位乘客的终点站都写在同一张旧车票上。",
        "target_duration_sec": 20,
        "budget_limit": 5,
    })
    assert status == 201, (status, project)
    project_id = project["id"]

    # Write the row directly: this is the state the database is left in after a hard kill.
    from backend.database import Database
    from backend.repository import utc_now

    task_id = "task_interrupted_live"
    now = utc_now()
    database = Database("backend/data/vmaker.db")
    with database.connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO tasks"
            " (id,project_id,task_type,status,payload_json,attempts,progress,message,capability,"
            "  estimated_cost,provider,model,created_at,updated_at,started_at)"
            " VALUES (?,?,'story_analysis','running','{}',1,0.4,'Analyzing story','story_analysis',"
            "         0,'demo','deterministic-v1',?,?,?)",
            (task_id, project_id, now, now, now),
        )
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"task_id": task_id, "project_id": project_id}), encoding="utf-8")
    print("prepared project", project_id, "task", task_id, "status=running")
    print("now restart the backend, then run: python scripts/check_live_worker.py verify")
    return 0


def verify() -> int:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    task_id = state["task_id"]
    deadline = time.time() + 60
    task = None
    while time.time() < deadline:
        _, task = call("GET", f"/tasks/{task_id}")
        if task["status"] in ("completed", "failed", "canceled", "paused"):
            break
        time.sleep(0.5)
    assert task is not None
    print("task status:", task["status"], "| attempts:", task["attempts"], "| message:", task["message"])
    events = [event["event_type"] for event in task.get("events", [])]
    print("events:", events)

    # Recovery must have happened, and the worker must have finished it without any manual execute.
    assert "recovered" in events, f"startup recovery did not requeue the task: {events}"
    assert task["status"] == "completed", f"worker never executed the recovered task: {task}"
    assert task["attempts"] == 2, task["attempts"]

    _, project = call("GET", f"/projects/{state['project_id']}")
    _, tasks = call("GET", f"/projects/{state['project_id']}/tasks")
    print("project tasks:", [(item["task_type"], item["status"]) for item in tasks])
    call("DELETE", f"/projects/{state['project_id']}")
    STATE.unlink(missing_ok=True)
    print("LIVE_WORKER_OK")
    return 0


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "verify"
    raise SystemExit(prepare() if phase == "prepare" else verify())
