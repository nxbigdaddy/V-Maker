"""Legacy v4 -> v5 upgrade check: pending tasks and their events must survive."""
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.database import Database

LEGACY_TASKS = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed')),
    payload_json TEXT NOT NULL DEFAULT '{}', result_id TEXT, error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '', capability TEXT NOT NULL DEFAULT '',
    estimated_cost REAL NOT NULL DEFAULT 0, actual_cost REAL, provider TEXT, model TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL
);
"""

with TemporaryDirectory() as folder:
    path = Path(folder) / "legacy.db"
    raw = sqlite3.connect(path)
    raw.executescript(LEGACY_TASKS)
    raw.execute("PRAGMA user_version = 4")
    raw.execute(
        "INSERT INTO tasks (id, task_type, status, created_at, updated_at)"
        " VALUES ('legacy1','story_analysis','pending','n','n')"
    )
    raw.execute(
        "INSERT INTO task_events (task_id,event_type,message,created_at) VALUES ('legacy1','created','queued','n')"
    )
    raw.commit()
    raw.close()

    db = Database(path)
    db.initialize()
    with db.connect() as c:
        version = c.execute("PRAGMA user_version").fetchone()[0]
        status = c.execute("SELECT status FROM tasks WHERE id='legacy1'").fetchone()[0]
        events = c.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        sql = c.execute("SELECT sql FROM sqlite_master WHERE name='tasks'").fetchone()[0]
        print("version", version, "status", status, "events", events, "has_paused", "'paused'" in sql)
