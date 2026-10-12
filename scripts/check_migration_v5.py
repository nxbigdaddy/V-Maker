"""Ad-hoc migration check for schema v5 (task 'paused' status)."""
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.database import SCHEMA_VERSION, Database

with TemporaryDirectory() as folder:
    db = Database(Path(folder) / "m.db")
    db.initialize()
    with db.connect() as c:
        version = c.execute("PRAGMA user_version").fetchone()[0]
        sql = c.execute("SELECT sql FROM sqlite_master WHERE name='tasks'").fetchone()[0]
        print("version", version, "expected", SCHEMA_VERSION, "has_paused", "'paused'" in sql)
        c.execute(
            "INSERT INTO tasks (id,project_id,task_type,status,created_at,updated_at)"
            " VALUES ('t1',NULL,'x','paused','n','n')"
        )
        c.execute(
            "INSERT INTO task_events (task_id,event_type,message,created_at)"
            " VALUES ('t1','e','m','n')"
        )
    db.initialize()
    db.initialize()
    with db.connect() as c:
        print("paused_rows", c.execute("SELECT COUNT(*) FROM tasks WHERE status='paused'").fetchone()[0])
        print("events_kept", c.execute("SELECT COUNT(*) FROM task_events").fetchone()[0])
        print("indexes", c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name LIKE 'idx_tasks%'").fetchone()[0])
