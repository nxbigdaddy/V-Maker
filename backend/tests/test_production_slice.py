from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import create_app


def _analyzed_project(client: TestClient, payload: dict | None = None) -> dict:
    body = {"name": "完整切片", "story": "女孩发现机器人。她帮助机器人回家。"}
    body.update(payload or {})
    created = client.post("/api/projects", json=body)
    assert created.status_code == 201, created.text
    project = created.json()
    task = client.post(f"/api/projects/{project['id']}/analysis-tasks")
    assert task.status_code == 201
    # Background execution normally completes before TestClient returns. Explicit execute is idempotent.
    client.post(f"/api/tasks/{task.json()['id']}/execute")
    return project


def test_legacy_database_migrates_safely_and_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
        CREATE TABLE projects (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, story TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL,
          aspect_ratio TEXT NOT NULL, target_duration_sec INTEGER NOT NULL, status TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO projects VALUES ('prj_old','旧项目','旧故事','auto','16:9',42,'draft','2025-01-01','2025-01-01');
        """)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0

    with TestClient(create_app(path)) as client:
        response = client.get("/api/projects/prj_old")
        assert response.status_code == 200
        project = response.json()
        assert project["mode"] == "draft"
        assert project["language"] == "zh-CN"
        assert project["outputs"] == []

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
        assert {"language", "creative_intent", "episode_strategy", "visual_style", "audience",
                "narration", "budget_limit", "outputs_json"} <= columns

    # A second startup proves migrations are idempotent.
    with TestClient(create_app(path)) as client:
        assert client.get("/api/projects/prj_old").status_code == 200


def test_full_frontend_create_contract_and_duration_alias(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "full.db")) as client:
        response = client.post("/api/projects", json={
            "name": "前端契约", "story": "故事", "mode": "professional", "language": "zh-CN",
            "creative_intent": "温暖克制", "episode_strategy": "ai_split", "duration_seconds": 90,
            "aspect_ratio": "9:16", "visual_style": "绘本水彩", "audience": "儿童与家庭",
            "narration": "narration", "budget_limit": 230, "outputs": ["mp4", "jianying"],
        })
        assert response.status_code == 201, response.text
        project = response.json()
        assert project["target_duration_sec"] == 90
        for key in ("language", "creative_intent", "episode_strategy", "visual_style", "audience", "narration", "budget_limit", "outputs"):
            assert project[key] == {"language":"zh-CN", "creative_intent":"温暖克制", "episode_strategy":"ai_split",
                "visual_style":"绘本水彩", "audience":"儿童与家庭", "narration":"narration",
                "budget_limit":230, "outputs":["mp4","jianying"]}[key]
        assert client.post("/api/projects", json={"name":"草稿", "mode":"draft"}).status_code == 201
        assert client.post("/api/projects", json={"name":"标准", "mode":"standard"}).status_code == 201


def test_bible_script_shots_approval_and_restart_persistence(tmp_path: Path) -> None:
    path = tmp_path / "slice.db"
    with TestClient(create_app(path)) as client:
        project = _analyzed_project(client, {
            "duration_seconds": 31, "visual_style": "赛博科幻", "creative_intent": "回家",
            "audience": "全年龄", "outputs": ["mp4"],
        })
        project_id = project["id"]

        bible_response = client.post(f"/api/projects/{project_id}/bible/generate")
        assert bible_response.status_code == 201, bible_response.text
        bible = bible_response.json()
        assert bible["version"] == 1 and bible["status"] == "draft"
        assert bible["demo"] is True and bible["generator"] == "demo-deterministic-v1"
        assert "不是 AI" in bible["style"]["notice"]
        assert set(bible) >= {"characters", "locations", "props", "source_analysis_id"}
        approved = client.post(f"/api/projects/{project_id}/bible-versions/1/approve").json()
        assert approved["status"] == "approved"
        assert all(item["status"] == "approved" for kind in ("characters", "locations", "props") for item in approved[kind])

        generated = client.post(f"/api/projects/{project_id}/screenplay/generate")
        assert generated.status_code == 201, generated.text
        script = generated.json()
        assert script["demo"] is True and script["target_duration_sec"] == 31
        assert sum(shot["duration"] for shot in script["shots"]) == 31
        assert script["episodes"] and script["episodes"][0]["scenes"] and script["shots"]
        shot = script["shots"][0]
        assert set(shot) >= {"order", "duration", "shot_size", "camera", "action",
                             "narration_dialogue", "render_strategy", "status"}

        changed = client.patch(f"/api/shots/{shot['id']}", json={
            "duration": shot["duration"] + 1, "camera": "handheld", "action": "修改后的动作",
            "narration_dialogue": "回家吧", "render_strategy": "image_to_video",
        })
        assert changed.status_code == 200
        assert changed.json()["revision"] == 2
        assert changed.json()["camera"] == "handheld"
        approved_shot = client.post(f"/api/shots/{shot['id']}/approve").json()
        assert approved_shot["status"] == "approved" and approved_shot["revision"] == 3

    with TestClient(create_app(path)) as restarted:
        bible = restarted.get(f"/api/projects/{project_id}/bible").json()
        assert bible["status"] == "approved"
        shots = restarted.get(f"/api/projects/{project_id}/shots").json()
        persisted = next(item for item in shots if item["id"] == shot["id"])
        assert persisted["status"] == "approved"
        assert persisted["camera"] == "handheld"
        assert persisted["narration_dialogue"] == "回家吧"
