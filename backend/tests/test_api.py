from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(tmp_path / "test.db")
    with TestClient(app) as test_client:
        yield test_client


def test_health(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "v-maker-backend",
        "database": "ok",
    }


def test_project_crud_and_initial_dashboard(client: TestClient) -> None:
    create_response = client.post(
        "/api/projects",
        json={
            "name": "MVP 短片",
            "story": "一个用于测试的故事。",
            "mode": "standard",
            "aspect_ratio": "16:9",
            "target_duration_sec": 60,
        },
    )
    assert create_response.status_code == 201
    project = create_response.json()
    assert project["id"].startswith("prj_")
    assert project["status"] == "draft"

    list_response = client.get("/api/projects")
    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()] == [project["id"]]

    detail_response = client.get(f"/api/projects/{project['id']}")
    assert detail_response.status_code == 200
    assert detail_response.json()["story"] == "一个用于测试的故事。"

    dashboard_response = client.get(f"/api/projects/{project['id']}/dashboard")
    assert dashboard_response.status_code == 200
    dashboard = dashboard_response.json()
    assert [stage["name"] for stage in dashboard["workflow_stages"]] == [
        "故事分析",
        "项目圣经",
        "剧本",
        "分镜",
        "素材",
        "音频",
        "时间线",
        "导出",
    ]
    assert dashboard["workflow_summary"] == {
        "pending": 8,
        "in_progress": 0,
        "completed": 0,
        "blocked": 0,
        "failed": 0,
    }

    update_response = client.patch(
        f"/api/projects/{project['id']}",
        json={"name": "更新后的短片", "status": "active"},
    )
    assert update_response.status_code == 200
    assert update_response.json()["name"] == "更新后的短片"
    assert update_response.json()["status"] == "active"

    delete_response = client.delete(f"/api/projects/{project['id']}")
    assert delete_response.status_code == 204
    assert client.get(f"/api/projects/{project['id']}").status_code == 404


def test_project_validation_and_missing_project(client: TestClient) -> None:
    response = client.post(
        "/api/projects",
        json={"name": "  ", "target_duration_sec": 0},
    )
    assert response.status_code == 422

    assert client.get("/api/projects/missing").status_code == 404
    assert client.get("/api/projects/missing/dashboard").status_code == 404
    assert client.delete("/api/projects/missing").status_code == 404


def test_cors_allows_vite_localhost(client: TestClient) -> None:
    response = client.options(
        "/api/projects",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
