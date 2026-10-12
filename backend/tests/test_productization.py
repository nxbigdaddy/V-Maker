from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from backend.analysis import AnalysisRepository
from backend.database import Database
from backend.main import create_app


def _project(client: TestClient, **overrides) -> dict:
    payload = {"name": "产品化测试", "story": "女孩帮助机器人回家。"}
    payload.update(overrides)
    response = client.post("/api/projects", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_running_task_recovers_to_queued_on_startup(tmp_path: Path) -> None:
    path = tmp_path / "recover.db"
    with TestClient(create_app(path)) as client:
        project = _project(client)
        task = client.post(f"/api/projects/{project['id']}/analysis-tasks").json()
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE tasks SET status='running',message='was running' WHERE id=?", (task["id"],))
    with TestClient(create_app(path)) as restarted:
        recovered = restarted.get(f"/api/tasks/{task['id']}").json()
        assert recovered["status"] == "queued"
        assert "Recovered" in recovered["message"]
        assert recovered["events"][-1]["event_type"] == "recovered"


def test_cancel_retry_project_list_and_execute_queue(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "queue.db")) as client:
        project = _project(client)
        task = AnalysisRepository(client.app.state.database).create_analysis_task(project["id"])
        assert task is not None
        canceled = client.post(f"/api/tasks/{task['id']}/cancel").json()
        assert canceled["status"] == "canceled"
        retried = client.post(f"/api/tasks/{task['id']}/retry").json()
        assert retried["status"] == "queued"
        completed = client.post("/api/tasks/execute-next").json()
        assert completed["id"] == task["id"] and completed["status"] == "completed"
        listed = client.get(f"/api/projects/{project['id']}/tasks").json()
        assert [item["id"] for item in listed] == [task["id"]]


def test_budget_hard_limit_rejects_and_demo_zero_cost(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "budget.db"
    monkeypatch.delenv("MISSING_PRODUCT_TEST_KEY", raising=False)
    with TestClient(create_app(path)) as client:
        project = _project(client, budget_limit=0.5)
        client.put("/api/provider-settings", json={"provider": "deepseek", "endpoint": "https://example.invalid/v1", "model": "x", "api_key_env": "MISSING_PRODUCT_TEST_KEY"})
        task = client.post(f"/api/projects/{project['id']}/analysis-tasks").json()
        assert task["estimated_cost"] == 1
        rejected = client.post(f"/api/tasks/{task['id']}/execute").json()
        assert rejected["status"] == "failed" and "budget limit exceeded" in rejected["error"]

        repo = AnalysisRepository(client.app.state.database)
        demo = repo.create_task(project["id"], "zero", estimated_cost=0, provider="demo")
        assert demo is not None
        claimed = repo.claim(str(demo["id"]))
        assert claimed is not None and claimed["status"] == "running"
        repo.complete_task(str(demo["id"]), actual_cost=0)
        dashboard = client.get(f"/api/projects/{project['id']}/dashboard").json()
        assert dashboard["budget"] == {"limit": 0.5, "settled": 0.0, "reserved": 0.0, "available": 0.5}
        assert dashboard["task_summary"]["failed"] == 1
        costs = client.get(f"/api/projects/{project['id']}/costs")
        assert costs.status_code == 200 and costs.json()["budget"]["available"] == 0.5
        diagnostics = client.get("/api/system/diagnostics")
        assert diagnostics.status_code == 200 and diagnostics.json()["schema_version"] == 7


def test_diagnostics_reports_its_own_process_and_can_ask_the_manager_to_restart(tmp_path: Path, monkeypatch) -> None:
    """界面上的「后端进程」面板靠这个块显示状态，重启则委托给服务管理器脚本。

    端点必须：如实报告自己是不是被管理器启动的；在没有管理器脚本时拒绝（而不是假装成功）；
    真的去调用管理器时把当前解释器传过去（否则重启可能换成一个缺少可选依赖的 Python）。
    """
    import backend.main as main_module

    monkeypatch.setattr(main_module, "SERVICE_MANAGER", tmp_path / "missing-service.ps1")
    with TestClient(create_app(tmp_path / "service.db")) as client:
        process = client.get("/api/system/diagnostics").json()["process"]
        assert process["pid"] == os.getpid()
        assert process["port"] == 8765
        assert process["managed"] is False          # 测试进程不是管理器启动的
        assert process["can_restart"] is False      # 管理器脚本不存在
        assert "service.ps1" in process["note"]

        refused = client.post("/api/system/backend/restart").json()
        assert refused["ok"] is False and "不存在" in refused["message"]

    # 管理器存在时：应该真的 spawn 它，并把解释器显式传下去。
    calls: list[dict] = []
    monkeypatch.setattr(main_module, "SERVICE_MANAGER", tmp_path / "service.ps1")
    (tmp_path / "service.ps1").write_text("# stub", encoding="utf-8")

    def fake_popen(argv, **kwargs):  # noqa: ANN001 - mirrors subprocess.Popen usage
        calls.append({"argv": argv, "kwargs": kwargs})
        return type("P", (), {"pid": 1})()

    monkeypatch.setattr(main_module.subprocess, "Popen", fake_popen)
    with TestClient(create_app(tmp_path / "service2.db")) as client:
        result = client.post("/api/system/backend/restart").json()
        assert result["ok"] is True and result["action"] == "restart"
        assert calls and calls[0]["argv"][-1] == "restart"
        assert calls[0]["kwargs"]["env"]["VMAKER_PYTHON"] == sys.executable
        # 只用 CREATE_NO_WINDOW。带 DETACHED_PROCESS 时 PowerShell 会立刻退出、什么都不做。
        if os.name == "nt":
            flags = calls[0]["kwargs"]["creationflags"]
            assert flags & getattr(subprocess, "CREATE_NO_WINDOW", 0)
            assert not flags & getattr(subprocess, "DETACHED_PROCESS", 0)


def test_soft_delete_archive_restore_and_copy(tmp_path: Path) -> None:
    path = tmp_path / "projects.db"
    with TestClient(create_app(path)) as client:
        original = _project(client)
        archived = client.post(f"/api/projects/{original['id']}/archive").json()
        assert archived["status"] == "archived" and archived["archived_at"]
        assert client.get("/api/projects").json() == []
        restored = client.post(f"/api/projects/{original['id']}/restore").json()
        assert restored["archived_at"] is None
        copied = client.post(f"/api/projects/{original['id']}/duplicate", json={"name": "复制项目"}).json()
        assert copied["copied_from_id"] == original["id"] and copied["id"] != original["id"]
        assert client.delete(f"/api/projects/{original['id']}").status_code == 204
        assert client.get(f"/api/projects/{original['id']}").status_code == 404
        assert client.post(f"/api/projects/{original['id']}/restore").status_code == 200
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM projects WHERE id=?", (original["id"],)).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM workflow_stages WHERE project_id=?", (original["id"],)).fetchone()[0] == 8


def test_artifact_download_validates_project_and_artifact(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "artifact.db"
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "missing-ffmpeg"))
    with TestClient(create_app(path)) as client:
        project = _project(client, target_duration_sec=4)
        task = client.post(f"/api/projects/{project['id']}/analysis-tasks").json()
        client.post(f"/api/tasks/{task['id']}/execute")
        client.post(f"/api/projects/{project['id']}/screenplay/generate")
        client.post(f"/api/projects/{project['id']}/demo-media")
        client.post(f"/api/projects/{project['id']}/timeline")
        export = client.post(f"/api/projects/{project['id']}/exports", json={}).json()
        artifact = export["artifacts"][0]
        download = client.get(f"/api/projects/{project['id']}/artifacts/{artifact['id']}/download")
        assert download.status_code == 200
        assert download.content and download.headers["content-type"].startswith(artifact["mime_type"])
        direct = client.get(f"/api/artifacts/{artifact['id']}/download")
        assert direct.status_code == 200 and direct.content == download.content
        other = _project(client)
        assert client.get(f"/api/projects/{other['id']}/artifacts/{artifact['id']}/download").status_code == 404
        assert client.get(f"/api/projects/{project['id']}/artifacts/../../etc/passwd/download").status_code == 404
