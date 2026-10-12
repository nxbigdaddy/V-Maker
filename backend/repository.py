"""Project and workflow persistence operations."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from backend.database import Database
from backend.schemas import ProjectCreate, ProjectUpdate

WORKFLOW_STAGES: tuple[tuple[str, str], ...] = (
    ("story_analysis", "故事分析"), ("project_bible", "项目圣经"),
    ("screenplay", "剧本"), ("storyboard", "分镜"), ("assets", "素材"),
    ("audio", "音频"), ("timeline", "时间线"), ("export", "导出"),
)
PROJECT_COLUMNS = (
    "id, name, story, mode, aspect_ratio, target_duration_sec, status, created_at, updated_at, "
    "language, creative_intent, episode_strategy, visual_style, audience, narration, budget_limit, outputs_json, "
    "archived_at, deleted_at, copied_from_id"
)
STAGE_COLUMNS = "id, project_id, stage_key, name, position, status, created_at, updated_at"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _project(row: sqlite3.Row) -> dict[str, object]:
    result = dict(row)
    result["outputs"] = json.loads(str(result.pop("outputs_json")))
    return result


class ProjectRepository:
    def __init__(self, database: Database) -> None: self.database = database

    def create(self, payload: ProjectCreate) -> dict[str, object]:
        project_id, now = f"prj_{uuid4().hex}", utc_now()
        values = payload.model_dump()
        with self.database.connect() as connection:
            connection.execute(
                f"INSERT INTO projects ({PROJECT_COLUMNS}) VALUES ({','.join('?' for _ in range(20))})",
                (project_id, values["name"], values["story"], values["mode"], values["aspect_ratio"],
                 values["target_duration_sec"], values["status"], now, now, values["language"],
                 values["creative_intent"], values["episode_strategy"], values["visual_style"],
                 values["audience"], values["narration"], values["budget_limit"],
                 json.dumps(values["outputs"], ensure_ascii=False), None, None, None),
            )
            connection.executemany(
                "INSERT INTO workflow_stages (id,project_id,stage_key,name,position,status,created_at,updated_at) VALUES (?,?,?,?,?,'pending',?,?)",
                [(f"stage_{uuid4().hex}", project_id, key, name, pos, now, now)
                 for pos, (key, name) in enumerate(WORKFLOW_STAGES, 1)],
            )
            row = connection.execute(f"SELECT {PROJECT_COLUMNS} FROM projects WHERE id=?", (project_id,)).fetchone()
        assert row is not None
        return _project(row)

    def list(self, include_archived: bool = False, include_deleted: bool = False) -> list[dict[str, object]]:
        clauses = []
        if not include_deleted: clauses.append("deleted_at IS NULL")
        if not include_archived: clauses.append("archived_at IS NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connect() as connection:
            rows = connection.execute(f"SELECT {PROJECT_COLUMNS} FROM projects{where} ORDER BY created_at DESC").fetchall()
        return [_project(row) for row in rows]

    def get(self, project_id: str, include_deleted: bool = False) -> dict[str, object] | None:
        deleted = "" if include_deleted else " AND deleted_at IS NULL"
        with self.database.connect() as connection:
            row = connection.execute(f"SELECT {PROJECT_COLUMNS} FROM projects WHERE id=?{deleted}", (project_id,)).fetchone()
        return _project(row) if row else None

    def update(self, project_id: str, payload: ProjectUpdate) -> dict[str, object] | None:
        changes = payload.model_dump(exclude_unset=True, exclude_none=True)
        if "outputs" in changes:
            changes["outputs_json"] = json.dumps(changes.pop("outputs"), ensure_ascii=False)
        if not changes: return self.get(project_id)
        changes["updated_at"] = utc_now()
        assignments = ", ".join(f"{column}=?" for column in changes)
        with self.database.connect() as connection:
            cursor = connection.execute(f"UPDATE projects SET {assignments} WHERE id=?", (*changes.values(), project_id))
            if cursor.rowcount == 0: return None
            row = connection.execute(f"SELECT {PROJECT_COLUMNS} FROM projects WHERE id=?", (project_id,)).fetchone()
        return _project(row) if row else None

    def delete(self, project_id: str) -> bool:
        now = utc_now()
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE projects SET deleted_at=?,updated_at=? WHERE id=? AND deleted_at IS NULL",
                (now, now, project_id),
            )
        return cursor.rowcount > 0

    def archive(self, project_id: str) -> dict[str, object] | None:
        now = utc_now()
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE projects SET archived_at=?,status='archived',updated_at=? WHERE id=? AND deleted_at IS NULL",
                (now, now, project_id),
            )
        return self.get(project_id) if cursor.rowcount else None

    def restore(self, project_id: str) -> dict[str, object] | None:
        now = utc_now()
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE projects SET archived_at=NULL,deleted_at=NULL,status='draft',updated_at=? WHERE id=?",
                (now, project_id),
            )
        return self.get(project_id) if cursor.rowcount else None

    def copy(self, project_id: str, name: str | None = None) -> dict[str, object] | None:
        source = self.get(project_id)
        if source is None: return None
        payload = ProjectCreate(
            name=name or f"{source['name']} 副本", story=source["story"], mode=source["mode"],
            aspect_ratio=source["aspect_ratio"], target_duration_sec=source["target_duration_sec"],
            status="draft", language=source["language"], creative_intent=source["creative_intent"],
            episode_strategy=source["episode_strategy"], visual_style=source["visual_style"],
            audience=source["audience"], narration=source["narration"], budget_limit=source["budget_limit"],
            outputs=source["outputs"],
        )
        copied = self.create(payload)
        with self.database.connect() as connection:
            connection.execute("UPDATE projects SET copied_from_id=? WHERE id=?", (project_id, copied["id"]))
        return self.get(str(copied["id"]))

    def dashboard_stats(self, project_id: str) -> tuple[dict[str, float | None], dict[str, int]]:
        with self.database.connect() as connection:
            project = connection.execute("SELECT budget_limit FROM projects WHERE id=?", (project_id,)).fetchone()
            ledger = connection.execute("""SELECT
                COALESCE(SUM(CASE WHEN entry_type='settle' THEN amount WHEN entry_type='refund' THEN -amount ELSE 0 END),0) settled,
                COALESCE(SUM(CASE WHEN entry_type='reserve' THEN amount WHEN entry_type='release' THEN -amount WHEN entry_type='settle' THEN -amount ELSE 0 END),0) reserved
                FROM cost_ledger WHERE project_id=?""", (project_id,)).fetchone()
            rows = connection.execute("SELECT status,COUNT(*) count FROM tasks WHERE project_id=? GROUP BY status", (project_id,)).fetchall()
        limit = float(project["budget_limit"]); settled = max(0.0, float(ledger["settled"])); reserved = max(0.0, float(ledger["reserved"]))
        return ({"limit": limit, "settled": settled, "reserved": reserved,
                 "available": None if limit == 0 else max(0.0, limit-settled-reserved)},
                {str(row["status"]): int(row["count"]) for row in rows})

    def workflow_stages(self, project_id: str) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute(f"SELECT {STAGE_COLUMNS} FROM workflow_stages WHERE project_id=? ORDER BY position", (project_id,)).fetchall()
        return [dict(row) for row in rows]
