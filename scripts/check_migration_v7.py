"""Ad-hoc check for the schema v7 capability upgrade (adds the 'video' capability).

Runs against a copy of the live database and against a synthetic v6 file, so a real user's
configuration can never be harmed by the rebuild.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.database import SCHEMA_VERSION, Database

SOURCE = Path("backend/data/vmaker.db")
V6_SCHEMA = """
CREATE TABLE capability_settings (
    capability TEXT PRIMARY KEY CHECK(capability IN ('text','image','speech')),
    provider TEXT NOT NULL CHECK(length(trim(provider)) > 0),
    endpoint TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
    api_key_env TEXT NOT NULL DEFAULT '', unit_cost REAL NOT NULL DEFAULT 0,
    options_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL
);
"""


def rows(db: Database) -> list[tuple]:
    with db.connect() as c:
        return [tuple(r) for r in c.execute(
            "SELECT capability,provider,endpoint,model,unit_cost FROM capability_settings ORDER BY capability"
        ).fetchall()]


def version(db: Database) -> int:
    with db.connect() as c:
        return int(c.execute("PRAGMA user_version").fetchone()[0])


print("=== 1. synthetic v6 -> v7 keeps a user-edited row and gains 'video' ===")
with TemporaryDirectory(ignore_cleanup_errors=True) as folder:
    path = Path(folder) / "v6.db"
    raw = sqlite3.connect(path)
    raw.executescript(V6_SCHEMA)
    raw.execute(
        "INSERT INTO capability_settings VALUES"
        " ('image','openai','https://mine.test/v1','my-image', 'MY_IMG_KEY', 0.25,'{}','n')"
    )
    raw.execute(
        "INSERT INTO capability_settings VALUES"
        " ('text','deepseek','https://api.deepseek.com/v1','deepseek-chat','DEEPSEEK_API_KEY',0,'{}','n')"
    )
    raw.execute("PRAGMA user_version = 6")
    raw.commit()
    raw.close()

    db = Database(path)
    db.initialize()
    db.initialize()  # idempotent
    print("version:", version(db), "expected", SCHEMA_VERSION)
    for row in rows(db):
        print("  ", row)
    assert version(db) == SCHEMA_VERSION == 7
    assert rows(db) == [
        ("image", "openai", "https://mine.test/v1", "my-image", 0.25),
        ("speech", "demo", "", "demo-tone-v1", 0.0),
        ("text", "deepseek", "https://api.deepseek.com/v1", "deepseek-chat", 0.0),
        ("video", "demo", "", "demo-unsupported-v1", 0.0),
    ]
    with db.connect() as c:
        sql = str(c.execute("SELECT sql FROM sqlite_master WHERE name='capability_settings'").fetchone()[0])
        assert "'video'" in sql

print("=== 2. live database copy ===")
if SOURCE.is_file():
    with TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        copy = Path(folder) / "live.db"
        shutil.copy2(SOURCE, copy)
        before = sqlite3.connect(copy)
        before_version = before.execute("PRAGMA user_version").fetchone()[0]
        before.close()
        db = Database(copy)
        db.initialize()
        print("before version:", before_version, "-> after:", version(db))
        for row in rows(db):
            print("  ", row)
        assert version(db) == 7
else:
    print("SKIP (no live database)")

print("CAPABILITY_V7_OK")
