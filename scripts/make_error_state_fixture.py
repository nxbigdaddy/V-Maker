"""Create a durable fixture project that really contains failing states.

Error-state UI needs genuine failures to render, and the probe is read-only by design (it never
creates anything). Two honest failures are cheap here and cost nothing:

* **failed task** — the local video placeholder cannot be a real clip, so one shot-video request
  fails on purpose with a real backend message;
* **failed export** — flipping one byte of an asset makes its SHA-256 check fail, and the export
  refuses to ship it. The asset file is restored immediately afterwards, so only the *record* of
  the failed export remains.

Run against a live backend; it is idempotent and only adds what is missing:

    python scripts/make_error_state_fixture.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND = "http://127.0.0.1:8765/api"
FIXTURE_NAME = "失败态实机验证"


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


def wait(task_id: str, label: str) -> dict:
    for _ in range(240):
        _, task = api("GET", f"/tasks/{task_id}")
        if task["status"] in ("completed", "failed", "paused", "canceled"):
            return task
        time.sleep(1)
    raise AssertionError(f"{label} did not finish")


def ensure_failed_task(project_id: str) -> None:
    _, tasks = api("GET", f"/projects/{project_id}/tasks")
    if any(task["status"] == "failed" for task in tasks):
        print("failed task: already present")
        return
    _, shots = api("GET", f"/projects/{project_id}/shots")
    assert shots, "fixture project has no shots"
    status, created = api("POST", f"/projects/{project_id}/shots/{shots[0]['id']}/video")
    assert status == 201, (status, created)
    task = wait(created["id"], "shot video task")
    print("shot video task:", task["status"])
    print("error:", (task.get("error") or "")[:200])
    assert task["status"] == "failed", f"demo 视频 provider 应当如实失败，实际 {task['status']}"
    assert (task.get("error") or "").strip(), "失败任务必须带有后端给出的原因"


def ensure_failed_export(project_id: str, force: bool = False) -> None:
    _, exports = api("GET", f"/projects/{project_id}/exports")
    assert isinstance(exports, list), f"导出列表接口应返回数组，实际 {type(exports).__name__}"
    stale = [job for job in exports if job["status"] == "failed" and job.get("format") == "auto"]
    if any(job["status"] == "failed" for job in exports) and not force:
        print("failed export: already present")
        return
    if stale:
        # 保留请求格式的修复之前留下的记录（format 被写死成 auto）。造一条新的，让夹具反映当前行为。
        print(f"note: {len(stale)} failed export(s) recorded before the requested-format fix")
    _, diagnostics = api("GET", "/system/diagnostics")
    projects_root = Path(diagnostics["data_dir"]["path"]) / "projects"
    _, assets = api("GET", f"/projects/{project_id}/assets")
    assert assets, "fixture project has no assets"
    version = assets[0]["latest_version"]
    target = projects_root / project_id / version["relative_path"]
    original = target.read_bytes()
    print("asset to corrupt:", version["relative_path"], len(original), "bytes")
    try:
        damaged = bytearray(original)
        damaged[-1] ^= 0xFF
        target.write_bytes(bytes(damaged))
        status, export = api("POST", f"/projects/{project_id}/exports", {"format": "portable_draft"})
        assert status == 201, (status, export)
        print("export format:", export["format"], "| status:", export["status"])
        print("export error:", (export.get("error") or "")[:200])
        assert export["status"] == "failed", f"校验和不符时导出必须失败，实际 {export['status']}"
        assert "SHA-256" in (export.get("error") or ""), export.get("error")
        assert not export["artifacts"], "失败的导出不应留下产物"
    finally:
        # 必须还原：夹具只保留「失败的导出记录」，不破坏项目素材本身。
        target.write_bytes(original)
        print("asset restored")


_, projects = api("GET", "/projects")
existing = next((p for p in projects if p["name"] == FIXTURE_NAME), None)
if existing:
    project_id = str(existing["id"])
    print("reusing fixture project:", project_id)
else:
    _, project = api("POST", "/projects", {
        "name": FIXTURE_NAME,
        "story": "一位修表匠在停摆的钟楼里寻找最后一个整点。",
        "target_duration_sec": 8, "aspect_ratio": "16:9",
    })
    project_id = str(project["id"])
    print("created fixture project:", project_id)
    _, pipeline = api("POST", f"/projects/{project_id}/pipeline", {"require_review": False})
    task = wait(pipeline["id"], "pipeline")
    print("pipeline:", task["status"])
    assert task["status"] == "completed", task

ensure_failed_task(project_id)
ensure_failed_export(project_id, force="force" in sys.argv)

# 导出失败时后端也会把对应的 export 任务标成 failed，所以失败原因在「任务中心」里也找得到。
_, tasks = api("GET", f"/projects/{project_id}/tasks")
print("tasks now:", [(t["task_type"], t["status"]) for t in tasks])
assert any(t["task_type"] == "export" and t["status"] == "failed" for t in tasks), \
    "失败的导出应当在任务中心留下一个 failed 任务"
print("FIXTURE_READY", project_id)
