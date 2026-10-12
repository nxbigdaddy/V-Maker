from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "analysis.db"


@pytest.fixture
def client(database_path: Path):
    with TestClient(create_app(database_path)) as test_client:
        yield test_client


def create_project(client: TestClient, story: str = "女孩发现一台机器人。她决定帮助机器人回家。") -> str:
    response = client.post("/api/projects", json={"name": "分析测试", "story": story})
    assert response.status_code == 201
    return response.json()["id"]


def test_create_and_execute_demo_task_persists_result(client: TestClient, database_path: Path) -> None:
    settings = client.get("/api/provider-settings")
    assert settings.status_code == 200
    assert settings.json()["provider"] == "demo"
    assert settings.json()["configured"] is True

    project_id = create_project(client)
    created = client.post(f"/api/projects/{project_id}/analysis-tasks")
    assert created.status_code == 201
    task = created.json()
    assert task["status"] == "queued"
    assert task["events"][0]["event_type"] == "created"

    executed = client.post(f"/api/tasks/{task['id']}/execute")
    assert executed.status_code == 200
    assert executed.json()["status"] == "completed"
    assert executed.json()["attempts"] == 1

    analysis = client.get(f"/api/projects/{project_id}/analysis")
    assert analysis.status_code == 200
    body = analysis.json()
    assert body["provider"] == "demo"
    assert body["model"] == "deterministic-v1"
    assert body["result"]["summary"]
    assert body["result"]["beats"]
    assert "不是 AI" in body["result"]["risks"][0]

    # Verify persistence survives constructing a fresh app process facade.
    with TestClient(create_app(database_path)) as restarted:
        assert restarted.get(f"/api/tasks/{task['id']}").json()["status"] == "completed"
        assert restarted.get(f"/api/projects/{project_id}/analysis").json()["id"] == body["id"]


def test_provider_settings_never_store_secret_and_missing_key_fails(
    client: TestClient, database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_VMAKER_API_KEY", raising=False)
    updated = client.put(
        "/api/provider-settings",
        json={
            "provider": "deepseek",
            "endpoint": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "api_key_env": "TEST_VMAKER_API_KEY",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["configured"] is False
    assert set(updated.json()) == {
        "provider", "endpoint", "model", "api_key_env", "configured", "updated_at"
    }

    project_id = create_project(client)
    task_id = client.post(f"/api/projects/{project_id}/analysis-tasks").json()["id"]
    executed = client.post(f"/api/tasks/{task_id}/execute")
    assert executed.status_code == 200
    assert executed.json()["status"] == "failed"
    assert "TEST_VMAKER_API_KEY" in executed.json()["error"]

    with sqlite3.connect(database_path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(provider_settings)")]
        persisted = connection.execute("SELECT * FROM provider_settings").fetchone()
    assert "api_key" not in columns
    assert "secret" not in columns
    assert persisted is not None


def test_analysis_errors_and_missing_resources(client: TestClient) -> None:
    assert client.post("/api/projects/missing/analysis-tasks").status_code == 404
    assert client.get("/api/tasks/missing").status_code == 404
    assert client.post("/api/tasks/missing/execute").status_code == 404

    project_id = create_project(client, story="")
    task_id = client.post(f"/api/projects/{project_id}/analysis-tasks").json()["id"]
    failed = client.post(f"/api/tasks/{task_id}/execute").json()
    assert failed["status"] == "failed"
    assert failed["error"] == "story is empty"
    assert client.get(f"/api/projects/{project_id}/analysis").status_code == 404
