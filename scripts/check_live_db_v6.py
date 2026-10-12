"""Copy the live database, upgrade the copy to v6, and report what changed.

Never touches the original file, so a running server cannot be disturbed.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.database import Database

SOURCE = Path("backend/data/vmaker.db")


def peek(path: Path, label: str) -> None:
    with sqlite3.connect(path) as c:
        version = c.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {
            table: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("projects", "tasks", "task_events", "assets", "asset_versions")
            if table in tables
        }
        print(f"{label}: version={version} capability_settings={'capability_settings' in tables} {counts}")


if not SOURCE.is_file():
    print("SKIP no live database at", SOURCE)
else:
    # SQLite keeps the copied WAL handles alive briefly on Windows, so cleanup must be tolerant.
    with TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        copy = Path(folder) / "live-copy.db"
        shutil.copy2(SOURCE, copy)
        peek(copy, "before")
        db = Database(copy)
        db.initialize()
        db.initialize()  # idempotent
        peek(copy, "after ")
        with db.connect() as c:
            rows = c.execute(
                "SELECT capability,provider,endpoint,model,api_key_env,unit_cost FROM capability_settings ORDER BY capability"
            ).fetchall()
            for row in rows:
                print("  ", tuple(row))
        # The copy must still be readable end to end after the upgrade.
        with db.connect() as c:
            projects = c.execute("SELECT COUNT(*) FROM projects WHERE deleted_at IS NULL").fetchone()[0]
            tasks = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        print(f"data intact: projects={projects} tasks={tasks}")
    print("LIVE_COPY_V6_OK")
