"""Small SQLite persistence layer with explicit, restart-safe migrations."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(trim(name)) > 0),
    story TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL,
    aspect_ratio TEXT NOT NULL,
    target_duration_sec INTEGER NOT NULL CHECK(target_duration_sec > 0),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'zh-CN',
    creative_intent TEXT NOT NULL DEFAULT '',
    episode_strategy TEXT NOT NULL DEFAULT 'single',
    visual_style TEXT NOT NULL DEFAULT '',
    audience TEXT NOT NULL DEFAULT '',
    narration TEXT NOT NULL DEFAULT 'balanced',
    budget_limit REAL NOT NULL DEFAULT 0,
    outputs_json TEXT NOT NULL DEFAULT '[]',
    archived_at TEXT,
    deleted_at TEXT,
    copied_from_id TEXT
);
CREATE TABLE IF NOT EXISTS workflow_stages (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    stage_key TEXT NOT NULL, name TEXT NOT NULL, position INTEGER NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(project_id, stage_key), UNIQUE(project_id, position)
);
CREATE TABLE IF NOT EXISTS provider_settings (
    id INTEGER PRIMARY KEY CHECK(id = 1), provider TEXT NOT NULL CHECK(provider IN ('demo', 'openai', 'deepseek')),
    endpoint TEXT NOT NULL, model TEXT NOT NULL, api_key_env TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capability_settings (
    capability TEXT PRIMARY KEY CHECK(capability IN ('text', 'image', 'speech', 'video')),
    provider TEXT NOT NULL CHECK(length(trim(provider)) > 0),
    endpoint TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
    api_key_env TEXT NOT NULL DEFAULT '', unit_cost REAL NOT NULL DEFAULT 0,
    options_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS story_analysis (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL UNIQUE, provider TEXT NOT NULL, model TEXT NOT NULL, result_json TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id) ON DELETE CASCADE, task_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed', 'canceled', 'paused')),
    payload_json TEXT NOT NULL DEFAULT '{}', result_id TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    progress REAL NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '', capability TEXT NOT NULL DEFAULT '',
    estimated_cost REAL NOT NULL DEFAULT 0, actual_cost REAL, provider TEXT, model TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cost_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id),
    task_id TEXT REFERENCES tasks(id),
    entry_type TEXT NOT NULL CHECK(entry_type IN ('estimate','reserve','settle','release','refund','manual_adjustment')),
    amount REAL NOT NULL CHECK(amount >= 0), provider TEXT, model TEXT, note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bible_versions (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('draft', 'approved')),
    source_analysis_id TEXT NOT NULL REFERENCES story_analysis(id), style_json TEXT NOT NULL,
    demo INTEGER NOT NULL DEFAULT 1, generator TEXT NOT NULL DEFAULT 'demo-deterministic-v1',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, approved_at TEXT,
    UNIQUE(project_id, version)
);
CREATE TABLE IF NOT EXISTS bible_entities (
    id TEXT PRIMARY KEY, bible_version_id TEXT NOT NULL REFERENCES bible_versions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('character', 'location', 'prop')), position INTEGER NOT NULL,
    name TEXT NOT NULL, data_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('draft', 'approved')),
    UNIQUE(bible_version_id, kind, position)
);
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version INTEGER NOT NULL, position INTEGER NOT NULL, title TEXT NOT NULL, synopsis TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'approved')), source_analysis_id TEXT NOT NULL REFERENCES story_analysis(id),
    demo INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(project_id, version, position)
);
CREATE TABLE IF NOT EXISTS scenes (
    id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    position INTEGER NOT NULL, title TEXT NOT NULL, setting TEXT NOT NULL, purpose TEXT NOT NULL,
    action TEXT NOT NULL, dialogue TEXT NOT NULL, duration_sec INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'approved')), UNIQUE(episode_id, position)
);
CREATE TABLE IF NOT EXISTS shots (
    id TEXT PRIMARY KEY, scene_id TEXT NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
    position INTEGER NOT NULL, duration_sec INTEGER NOT NULL CHECK(duration_sec > 0), shot_size TEXT NOT NULL,
    camera TEXT NOT NULL, action TEXT NOT NULL, narration_dialogue TEXT NOT NULL,
    render_strategy TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('draft', 'approved')),
    revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(scene_id, position)
);
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('image','audio','subtitle','video','document')),
    source_type TEXT NOT NULL CHECK(source_type IN ('demo','import','export')),
    source_shot_id TEXT REFERENCES shots(id) ON DELETE SET NULL,
    source_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asset_versions (
    id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK(version > 0), sha256 TEXT NOT NULL CHECK(length(sha256)=64),
    mime_type TEXT NOT NULL, relative_path TEXT NOT NULL, byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
    demo INTEGER NOT NULL DEFAULT 0 CHECK(demo IN (0,1)), metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL, UNIQUE(asset_id, version), UNIQUE(asset_id, sha256)
);
CREATE TABLE IF NOT EXISTS timeline_versions (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK(version > 0), timeline_json TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK(length(sha256)=64), created_at TEXT NOT NULL,
    UNIQUE(project_id, version)
);
CREATE TABLE IF NOT EXISTS export_jobs (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    timeline_version_id TEXT NOT NULL REFERENCES timeline_versions(id),
    status TEXT NOT NULL CHECK(status IN ('pending','running','completed','completed_with_warnings','failed')),
    format TEXT NOT NULL, warning TEXT, error TEXT, created_at TEXT NOT NULL,
    started_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS export_artifacts (
    id TEXT PRIMARY KEY, export_job_id TEXT NOT NULL REFERENCES export_jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, relative_path TEXT NOT NULL, mime_type TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK(length(sha256)=64), byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_created_at ON projects(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_stages_project_position ON workflow_stages(project_id, position);
CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks(status, created_at);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_cost_ledger_project ON cost_ledger(project_id, id);
CREATE INDEX IF NOT EXISTS idx_cost_ledger_task ON cost_ledger(task_id, id);
CREATE INDEX IF NOT EXISTS idx_story_analysis_project ON story_analysis(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bible_versions_project ON bible_versions(project_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_bible_entities_version ON bible_entities(bible_version_id, kind, position);
CREATE INDEX IF NOT EXISTS idx_episodes_project ON episodes(project_id, version DESC, position);
CREATE INDEX IF NOT EXISTS idx_scenes_episode ON scenes(episode_id, position);
CREATE INDEX IF NOT EXISTS idx_shots_scene ON shots(scene_id, position);
CREATE INDEX IF NOT EXISTS idx_assets_project ON assets(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_asset_versions_asset ON asset_versions(asset_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_timelines_project ON timeline_versions(project_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_exports_project ON export_jobs(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_export_artifacts_job ON export_artifacts(export_job_id, created_at);
"""

PROJECT_ADDITIONS: tuple[tuple[str, str], ...] = (
    ("language", "TEXT NOT NULL DEFAULT 'zh-CN'"),
    ("creative_intent", "TEXT NOT NULL DEFAULT ''"),
    ("episode_strategy", "TEXT NOT NULL DEFAULT 'single'"),
    ("visual_style", "TEXT NOT NULL DEFAULT ''"),
    ("audience", "TEXT NOT NULL DEFAULT ''"),
    ("narration", "TEXT NOT NULL DEFAULT 'balanced'"),
    ("budget_limit", "REAL NOT NULL DEFAULT 0"),
    ("outputs_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("archived_at", "TEXT"),
    ("deleted_at", "TEXT"),
    ("copied_from_id", "TEXT"),
)

TASK_ADDITIONS: tuple[tuple[str, str], ...] = (
    ("progress", "REAL NOT NULL DEFAULT 0"),
    ("message", "TEXT NOT NULL DEFAULT ''"),
    ("capability", "TEXT NOT NULL DEFAULT ''"),
    ("estimated_cost", "REAL NOT NULL DEFAULT 0"),
    ("actual_cost", "REAL"),
    ("provider", "TEXT"),
    ("model", "TEXT"),
)


class Database:
    """Open short-lived SQLite connections suitable for FastAPI sync handlers."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            # CREATE IF NOT EXISTS handles new databases; ALTER/rebuild handles legacy files.
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(projects)")}
            for name, definition in PROJECT_ADDITIONS:
                if name not in columns:
                    connection.execute(f"ALTER TABLE projects ADD COLUMN {name} {definition}")
            task_columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
            for name, definition in TASK_ADDITIONS:
                if name not in task_columns:
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
            task_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
            ).fetchone()
            task_sql = str(task_sql_row["sql"] if task_sql_row else "")
            if "'paused'" not in task_sql:
                # SQLite cannot alter a CHECK constraint, so the task table is rebuilt in place.
                # This runs outside a transaction on purpose: inside one the foreign_keys pragmas
                # are silently ignored and DROP TABLE would cascade into task_events.
                connection.executescript("""
PRAGMA foreign_keys = OFF;
CREATE TABLE tasks_rebuild (
    id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','canceled','paused')),
    payload_json TEXT NOT NULL DEFAULT '{}', result_id TEXT, error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '', capability TEXT NOT NULL DEFAULT '',
    estimated_cost REAL NOT NULL DEFAULT 0, actual_cost REAL, provider TEXT, model TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
INSERT INTO tasks_rebuild
    (id,project_id,task_type,status,payload_json,result_id,error,attempts,progress,message,capability,
     estimated_cost,actual_cost,provider,model,created_at,updated_at,started_at,finished_at)
    SELECT id,project_id,task_type,
           CASE WHEN status='pending' THEN 'queued' ELSE status END,
           payload_json,result_id,error,attempts,progress,message,capability,
           estimated_cost,actual_cost,provider,model,created_at,updated_at,started_at,finished_at
    FROM tasks;
DROP TABLE tasks;
ALTER TABLE tasks_rebuild RENAME TO tasks;
PRAGMA foreign_keys = ON;
""")
            connection.executescript(SCHEMA)
            # Same constraint-change problem as ``tasks``: adding the 'video' capability means the
            # CHECK must change, and SQLite cannot ALTER it. Rebuild in place, keeping every row.
            settings_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='capability_settings'"
            ).fetchone()
            settings_sql = str(settings_sql_row["sql"] if settings_sql_row else "")
            if "'video'" not in settings_sql:
                connection.executescript("""
CREATE TABLE capability_settings_rebuild (
    capability TEXT PRIMARY KEY CHECK(capability IN ('text','image','speech','video')),
    provider TEXT NOT NULL CHECK(length(trim(provider)) > 0),
    endpoint TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
    api_key_env TEXT NOT NULL DEFAULT '', unit_cost REAL NOT NULL DEFAULT 0,
    options_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL
);
INSERT INTO capability_settings_rebuild
    (capability,provider,endpoint,model,api_key_env,unit_cost,options_json,updated_at)
    SELECT capability,provider,endpoint,model,api_key_env,unit_cost,options_json,updated_at
    FROM capability_settings;
DROP TABLE capability_settings;
ALTER TABLE capability_settings_rebuild RENAME TO capability_settings;
""")
            self._seed_capability_settings(connection)
            # The former API exposed "auto"; map legacy rows to the nearest new low-friction mode.
            connection.execute("UPDATE projects SET mode='draft' WHERE mode='auto'")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            # demo 的名称随能力实现变过（图像 SVG→位图、视频 无实现→本地 FFmpeg 占位片段），
            # 沿用旧名称会误导用户。这一步必须每次启动都跑：上面那段只在 v6→v7 迁移时执行。
            Database._rename_legacy_demo_models(connection)

    @staticmethod
    def _rename_legacy_demo_models(connection: sqlite3.Connection) -> None:
        """Rename only the rows that still hold seeded demo model names.

        A user who edited the provider or the model keeps their value untouched.
        """
        now = datetime.now(UTC).isoformat()
        connection.execute(
            "UPDATE capability_settings SET model='demo-bitmap-v1', updated_at=?"
            " WHERE capability='image' AND provider='demo' AND model='demo-svg-v1'", (now,))
        connection.execute(
            "UPDATE capability_settings SET model='demo-ffmpeg-v1', updated_at=?"
            " WHERE capability='video' AND provider='demo' AND model='demo-unsupported-v1'", (now,))

    @staticmethod
    def _seed_capability_settings(connection: sqlite3.Connection) -> None:
        """Create one settings row per capability, adopting the legacy text row on first upgrade.

        Only missing capabilities are inserted, so an existing configuration is never overwritten.
        """
        now = datetime.now(UTC).isoformat()
        existing = {str(row["capability"]) for row in connection.execute(
            "SELECT capability FROM capability_settings").fetchall()}
        if "text" not in existing:
            legacy = connection.execute(
                "SELECT provider, endpoint, model, api_key_env FROM provider_settings WHERE id=1"
            ).fetchone()
            text_row = (
                (str(legacy["provider"]), str(legacy["endpoint"]), str(legacy["model"]), str(legacy["api_key_env"]))
                if legacy is not None
                else ("demo", "https://api.openai.com/v1", "deterministic-v1", "OPENAI_API_KEY")
            )
            connection.execute(
                "INSERT INTO capability_settings (capability,provider,endpoint,model,api_key_env,unit_cost,options_json,updated_at)"
                " VALUES ('text',?,?,?,?,0,'{}',?)", (*text_row, now))
        # Media capabilities default to the honest local generators, never to a guessable cloud model.
        # ``video`` renders a real placeholder clip with the local FFmpeg when one is available, and
        # reports the missing dependency clearly when it is not.
        for capability, model, key_env in (("image", "demo-bitmap-v1", "OPENAI_API_KEY"),
                                           ("speech", "demo-tone-v1", "OPENAI_API_KEY"),
                                           ("video", "demo-ffmpeg-v1", "OPENAI_API_KEY")):
            if capability not in existing:
                connection.execute(
                    "INSERT INTO capability_settings"
                    " (capability,provider,endpoint,model,api_key_env,unit_cost,options_json,updated_at)"
                    " VALUES (?,'demo','',?,?,0,'{}',?)",
                    (capability, model, key_env, now))
        # demo 占位图从 SVG 改成位图（FFmpeg 与剪映都读不了 SVG）。这里只处理 v6→v7 迁移时新建的行；
        # 已经是 v7 的库由 initialize() 里的 _rename_legacy_demo_image_model 负责。
        connection.execute(
            "UPDATE capability_settings SET model='demo-bitmap-v1', updated_at=?"
            " WHERE capability='image' AND provider='demo' AND model='demo-svg-v1'", (now,))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
