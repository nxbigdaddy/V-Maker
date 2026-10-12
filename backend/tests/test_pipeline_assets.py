"""Verified asset-content serving and the resumable one-click production pipeline."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.responses import FileResponse

from backend.analysis import AnalysisRepository
from backend.database import SCHEMA
from backend.main import create_app
from backend.pipeline import STAGE_LABELS, STAGES

# Observed on this machine: starlette 1.7.0 FileResponse implements single-range requests itself,
# so "Range: bytes=0-9" is answered with 206 and a 10-byte body. The probe keeps the test honest
# on an older starlette that would ignore the header and answer 200 with the whole file.
_RANGE_SUPPORTED = hasattr(FileResponse, "_parse_range_header")


def _project(client: TestClient, **overrides) -> dict:
    payload = {"name": "流水线测试", "story": "女孩帮助机器人回家。", "target_duration_sec": 8}
    payload.update(overrides)
    response = client.post("/api/projects", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _project_with_demo_media(client: TestClient) -> tuple[dict, list[dict]]:
    project = _project(client)
    task = client.post(f"/api/projects/{project['id']}/analysis-tasks").json()
    client.post(f"/api/tasks/{task['id']}/execute")
    assert client.post(f"/api/projects/{project['id']}/screenplay/generate").status_code == 201
    generated = client.post(f"/api/projects/{project['id']}/demo-media")
    assert generated.status_code == 201, generated.text
    assets = client.get(f"/api/projects/{project['id']}/assets").json()
    assert assets
    return project, assets


def _workflow_status(client: TestClient, project_id: str) -> dict[str, str]:
    dashboard = client.get(f"/api/projects/{project_id}/dashboard")
    assert dashboard.status_code == 200, dashboard.text
    return {row["stage_key"]: row["status"] for row in dashboard.json()["workflow_stages"]}


def _stage_completed(task: dict) -> list[str]:
    return [event["message"] for event in task["events"] if event["event_type"] == "stage_completed"]


def _payload(path: Path, task_id: str) -> dict:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT payload_json FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row is not None
    return json.loads(row[0])


def _pipeline_task(client: TestClient, project_id: str, *, require_review: bool = True,
                   stages: tuple[str, ...] | None = None) -> str:
    task = AnalysisRepository(client.app.state.database).create_task(
        project_id, "pipeline",
        payload={"stages": list(stages or STAGES), "require_review": require_review,
                 "stage_index": 0, "completed_stages": []},
        capability="pipeline", estimated_cost=0, provider="local", model="pipeline-v1",
    )
    assert task is not None and task["status"] == "queued"
    return str(task["id"])


def test_asset_version_content_is_verified_and_pinned(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "assets.db")) as client:
        project, assets = _project_with_demo_media(client)
        asset = next(item for item in assets if item["kind"] == "image")
        version = asset["latest_version"]
        scoped = f"/api/projects/{project['id']}/assets/{asset['id']}/versions/{version['id']}/content"
        direct = f"/api/asset-versions/{version['id']}/content"

        for url in (scoped, direct):
            response = client.get(url)
            assert response.status_code == 200, response.text
            assert response.content
            assert len(response.content) == version["byte_size"]
            assert response.headers["content-type"].startswith(version["mime_type"])
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
            assert response.headers["etag"] == f'"{version["sha256"]}"'
            assert "sandbox" in response.headers["content-security-policy"]
            assert response.headers["content-disposition"].startswith("inline")
        assert client.get(scoped).content == client.get(direct).content

        ranged = client.get(scoped, headers={"Range": "bytes=0-9"})
        if _RANGE_SUPPORTED:
            assert ranged.status_code == 206, ranged.text
            assert len(ranged.content) == 10
            assert ranged.headers["content-range"] == f"bytes 0-9/{version['byte_size']}"
            assert ranged.content == client.get(scoped).content[:10]
        else:  # pragma: no cover - only reachable on a starlette without Range support
            assert ranged.status_code == 200
            assert len(ranged.content) == version["byte_size"]

        forged = client.get(f"/api/projects/{project['id']}/assets/{asset['id']}/versions/aver_forged/content")
        assert forged.status_code == 404 and forged.json()["detail"] == "asset content not found"
        assert client.get(f"/api/projects/{project['id']}/assets/asset_forged/versions/{version['id']}/content").status_code == 404
        assert client.get(f"/api/asset-versions/aver_forged/content").status_code == 404
        assert client.get(f"/api/projects/missing/assets/{asset['id']}/versions/{version['id']}/content").status_code == 404

        other = _project(client)
        assert client.get(f"/api/projects/{other['id']}/assets/{asset['id']}/versions/{version['id']}/content").status_code == 404

        assert client.delete(f"/api/projects/{project['id']}").status_code == 204
        assert client.get(scoped).status_code == 404
        assert client.get(direct).status_code == 404


def test_asset_content_rejects_tampered_file(tmp_path: Path) -> None:
    database_path = tmp_path / "assets_tamper.db"
    with TestClient(create_app(database_path)) as client:
        project, assets = _project_with_demo_media(client)
        version = assets[0]["latest_version"]
        target = database_path.parent / "projects" / project["id"] / version["relative_path"]
        assert target.is_file()
        assert client.get(f"/api/asset-versions/{version['id']}/content").status_code == 200
        target.write_bytes(target.read_bytes() + b"tampered")
        assert client.get(f"/api/asset-versions/{version['id']}/content").status_code == 404
        assert client.get(
            f"/api/projects/{project['id']}/assets/{assets[0]['id']}/versions/{version['id']}/content"
        ).status_code == 404


def test_pipeline_review_gates_pause_resume_and_complete(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "pipeline_review.db"
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "missing-ffmpeg"))
    with TestClient(create_app(database_path)) as client:
        project = _project(client)
        task_id = _pipeline_task(client, project["id"], require_review=True)

        first = client.post(f"/api/tasks/{task_id}/execute").json()
        assert first["status"] == "paused"
        assert "审核" in first["message"]
        assert first["finished_at"] is None
        assert first["progress"] == pytest.approx(2 / 6)
        assert _stage_completed(first) == ["故事分析", "项目圣经"]
        parked = _workflow_status(client, project["id"])
        # The reviewed stage keeps the status its generator reported; nothing later may start early.
        # Pausing must never lower dashboard progress, so produced stages are never pushed back.
        assert parked["story_analysis"] == "completed"
        assert parked["project_bible"] == "in_progress"
        assert parked["screenplay"] == "pending" and parked["storyboard"] == "pending"
        assert list(parked.values()).count("in_progress") == 1
        payload = _payload(database_path, task_id)
        assert payload["stage_index"] == 2 and payload["completed_stages"] == ["analysis", "bible"]

        assert client.post(f"/api/tasks/{task_id}/resume").json()["status"] == "queued"
        second = client.post(f"/api/tasks/{task_id}/execute").json()
        assert second["status"] == "paused" and "审核" in second["message"]
        assert _stage_completed(second) == ["故事分析", "项目圣经", "剧本与分镜"]
        approved = _workflow_status(client, project["id"])
        assert approved["project_bible"] == "completed"  # resuming past the gate is the approval
        # generate_script reports its own keys as completed; the gate must not push them back.
        assert approved["screenplay"] == "completed" and approved["storyboard"] == "completed"
        assert approved["assets"] == "pending" and approved["timeline"] == "pending"
        assert _payload(database_path, task_id)["stage_index"] == 3

        assert client.post(f"/api/tasks/{task_id}/resume").json()["status"] == "queued"
        final = client.post(f"/api/tasks/{task_id}/execute").json()
        assert final["status"] == "completed"
        assert final["progress"] == 1
        assert _stage_completed(final) == ["故事分析", "项目圣经", "剧本与分镜", "本地占位素材", "时间线", "导出"]
        assert set(_workflow_status(client, project["id"]).values()) == {"completed"}

        assert client.get(f"/api/projects/{project['id']}/assets").json()
        assert client.get(f"/api/projects/{project['id']}/timeline").status_code == 200
        with sqlite3.connect(database_path) as connection:
            exports = connection.execute("SELECT COUNT(*) FROM export_jobs WHERE project_id=?", (project["id"],)).fetchone()[0]
            analyses = connection.execute("SELECT COUNT(*) FROM story_analysis WHERE project_id=?", (project["id"],)).fetchone()[0]
        assert exports == 1 and analyses == 1


def test_pipeline_without_review_completes_in_one_run(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "pipeline_auto.db"
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "missing-ffmpeg"))
    with TestClient(create_app(database_path)) as client:
        project = _project(client)
        task_id = _pipeline_task(client, project["id"], require_review=False)
        result = client.post(f"/api/tasks/{task_id}/execute").json()
        assert result["status"] == "completed"
        assert result["progress"] == 1
        assert _stage_completed(result) == ["故事分析", "项目圣经", "剧本与分镜", "本地占位素材", "时间线", "导出"]
        stages = _workflow_status(client, project["id"])
        assert len(stages) == 8 and set(stages.values()) == {"completed"}
        assert client.get(f"/api/projects/{project['id']}/assets").json()
        assert client.get(f"/api/projects/{project['id']}/timeline").status_code == 200
        with sqlite3.connect(database_path) as connection:
            exports = connection.execute("SELECT COUNT(*) FROM export_jobs WHERE project_id=?", (project["id"],)).fetchone()[0]
        assert exports == 1


def test_canceled_pipeline_cannot_resume_or_execute(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "pipeline_cancel.db")) as client:
        project = _project(client)
        task_id = _pipeline_task(client, project["id"])
        canceled = client.post(f"/api/tasks/{task_id}/cancel").json()
        assert canceled["status"] == "canceled"
        assert client.post(f"/api/tasks/{task_id}/resume").status_code == 409
        assert client.post("/api/tasks/task_missing/resume").status_code == 404
        executed = client.post(f"/api/tasks/{task_id}/execute").json()
        assert executed["status"] == "canceled"
        assert _stage_completed(executed) == []
        assert client.get(f"/api/projects/{project['id']}/analysis").status_code == 404
        assert set(_workflow_status(client, project["id"]).values()) == {"pending"}


def test_pipeline_resume_continues_from_breakpoint(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "pipeline_resume.db"
    monkeypatch.setenv("VMAKER_FFMPEG_PATH", str(tmp_path / "missing-ffmpeg"))
    with TestClient(create_app(database_path)) as client:
        project = _project(client)
        task_id = _pipeline_task(client, project["id"], require_review=True)

        client.post(f"/api/tasks/{task_id}/execute")
        payload = _payload(database_path, task_id)
        assert payload["stage_index"] == 2 and payload["completed_stages"] == ["analysis", "bible"]

        client.post(f"/api/tasks/{task_id}/resume")
        assert client.post(f"/api/tasks/{task_id}/execute").json()["status"] == "paused"
        payload = _payload(database_path, task_id)
        assert payload["stage_index"] == 3
        assert payload["completed_stages"] == ["analysis", "bible", "production_plan"]

        client.post(f"/api/tasks/{task_id}/resume")
        final = client.post(f"/api/tasks/{task_id}/execute").json()
        assert final["status"] == "completed"
        completed = _stage_completed(final)
        # Every stage ran exactly once; resuming never re-runs an already finished stage.
        assert completed == [STAGE_LABELS[stage] for stage in STAGES]
        assert len(completed) == len(set(completed))
        payload = _payload(database_path, task_id)
        assert payload["stage_index"] == len(STAGES) and payload["completed_stages"] == list(STAGES)


def _write_v4_database(path: Path) -> None:
    """A v4 file: tasks CHECK allows 'pending' and has no 'paused' state."""
    schema = SCHEMA.replace("'canceled', 'paused'", "'canceled', 'pending'")
    assert schema != SCHEMA and "task_events" in schema
    now = "2024-01-01T00:00:00+00:00"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(schema)
        connection.execute("PRAGMA user_version = 4")
        connection.execute(
            "INSERT INTO projects (id,name,story,mode,aspect_ratio,target_duration_sec,status,created_at,updated_at)"
            " VALUES ('prj_v4','旧项目','一个旧故事。','standard','16:9',30,'draft',?,?)", (now, now))
        connection.execute(
            "INSERT INTO tasks (id,project_id,task_type,status,payload_json,attempts,progress,message,capability,"
            "estimated_cost,created_at,updated_at) VALUES ('task_v4','prj_v4','story_analysis','pending','{}',0,0,"
            "'Queued','story_analysis',0,?,?)", (now, now))
        connection.execute(
            "INSERT INTO task_events (task_id,event_type,message,created_at) VALUES ('task_v4','created','v4 event kept',?)",
            (now,))


def test_v4_database_migrates_pending_tasks_and_keeps_events(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy_v4.db"
    _write_v4_database(database_path)
    with sqlite3.connect(database_path) as connection:
        assert "'pending'" in connection.execute("SELECT sql FROM sqlite_master WHERE name='tasks'").fetchone()[0]

    with TestClient(create_app(database_path)) as client:
        migrated = client.get("/api/tasks/task_v4").json()
        assert migrated["status"] == "queued"
        assert [event["message"] for event in migrated["events"]] == ["v4 event kept"]
        assert client.get("/api/system/diagnostics").json()["schema_version"] == 7
        with sqlite3.connect(database_path) as connection:
            task_sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone()[0]
            events = connection.execute("SELECT COUNT(*) FROM task_events WHERE task_id='task_v4'").fetchone()[0]
        assert "'paused'" in task_sql and "'pending'" not in task_sql and events == 1

        repo = AnalysisRepository(client.app.state.database)
        created = repo.create_task("prj_v4", "pipeline")
        assert created is not None
        task_id = str(created["id"])
        assert repo.claim(task_id) is not None
        paused = repo.pause(task_id, 1, "「项目圣经」已完成，请审核后再继续制作")
        assert paused is not None and paused["status"] == "paused" and "审核" in paused["message"]
        assert client.post(f"/api/tasks/{task_id}/resume").json()["status"] == "queued"


def test_pipeline_endpoint_contract(tmp_path: Path) -> None:
    database_path = tmp_path / "pipeline_api.db"
    with TestClient(create_app(database_path)) as client:
        project = _project(client)
        invalid = client.post(f"/api/projects/{project['id']}/pipeline", json={"stages": ["analysis", "bake_cake"]})
        assert invalid.status_code == 422 and "bake_cake" in invalid.json()["detail"]
        assert client.post(f"/api/projects/{project['id']}/pipeline", json={"stages": "analysis"}).status_code == 422
        assert client.post("/api/projects/prj_missing/pipeline", json={}).status_code == 404

        created = client.post(f"/api/projects/{project['id']}/pipeline",
                              json={"stages": ["analysis"], "require_review": False})
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["task_type"] == "pipeline" and body["capability"] == "pipeline"
        assert body["provider"] == "local" and body["model"] == "pipeline-v1"
        assert body["estimated_cost"] == 0
        assert body["events"][0]["event_type"] == "created"
        payload = _payload(database_path, body["id"])
        assert payload["stages"] == ["analysis"] and payload["require_review"] is False
        assert "stage_index" in payload and "completed_stages" in payload


def test_pipeline_reserves_budget_for_external_text_provider(tmp_path: Path, monkeypatch) -> None:
    """A pipeline that calls a real provider must not bypass the project hard limit."""
    database_path = tmp_path / "pipeline_budget.db"
    monkeypatch.delenv("MISSING_PIPELINE_TEST_KEY", raising=False)
    with TestClient(create_app(database_path)) as client:
        project = _project(client, budget_limit=0.5)
        client.put("/api/provider-settings", json={
            "provider": "deepseek", "endpoint": "https://example.invalid/v1", "model": "x",
            "api_key_env": "MISSING_PIPELINE_TEST_KEY",
        })

        # demo provider and local-only stages stay free
        local_only = client.post(f"/api/projects/{project['id']}/pipeline",
                                 json={"stages": ["demo_media"], "require_review": False}).json()
        assert local_only["estimated_cost"] == 0 and local_only["provider"] == "local"

        gated = client.post(f"/api/projects/{project['id']}/pipeline",
                            json={"stages": ["analysis", "bible"], "require_review": True}).json()
        assert gated["estimated_cost"] == 1 and gated["provider"] == "deepseek"

        # 1.0 reserved against a 0.5 hard limit: executing must be rejected, not silently charged
        executed = client.post(f"/api/tasks/{gated['id']}/execute").json()
        assert executed["status"] == "failed"
        assert "budget limit exceeded" in executed["error"]
        assert any(event["event_type"] == "budget_rejected" for event in executed["events"])

        costs = client.get(f"/api/projects/{project['id']}/costs").json()
        assert costs["budget"]["reserved"] == 0.0
        assert costs["budget"]["available"] == 0.5
