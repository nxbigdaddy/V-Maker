"""Ad-hoc check for the schema v6 capability settings seeding (idempotent, never overwrites)."""
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.database import SCHEMA_VERSION, Database


def dump(db: Database, label: str) -> None:
    with db.connect() as c:
        rows = c.execute(
            "SELECT capability,provider,endpoint,model,api_key_env,unit_cost FROM capability_settings ORDER BY capability"
        ).fetchall()
        print(label, c.execute("PRAGMA user_version").fetchone()[0], [tuple(r) for r in rows])


with TemporaryDirectory() as folder:
    path = Path(folder) / "c6.db"
    db = Database(path)
    db.initialize()
    dump(db, "fresh")

    # A user-configured capability must survive restarts untouched.
    with db.connect() as c:
        c.execute(
            "UPDATE capability_settings SET provider='openai',endpoint='https://example.test/v1',"
            "model='my-image-model',api_key_env='MY_IMAGE_KEY',unit_cost=0.25 WHERE capability='image'"
        )
    db.initialize()
    db.initialize()
    dump(db, "after restart")
    with db.connect() as c:
        row = c.execute(
            "SELECT provider,endpoint,model,api_key_env,unit_cost FROM capability_settings WHERE capability='image'"
        ).fetchone()
        print("image preserved:", tuple(row))
        assert tuple(row) == ("openai", "https://example.test/v1", "my-image-model", "MY_IMAGE_KEY", 0.25)


# Upgrading a v5 database must adopt the legacy text row instead of resetting it.
with TemporaryDirectory() as folder:
    path = Path(folder) / "legacy6.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE provider_settings (
            id INTEGER PRIMARY KEY CHECK(id = 1), provider TEXT NOT NULL, endpoint TEXT NOT NULL,
            model TEXT NOT NULL, api_key_env TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO provider_settings VALUES (1,'deepseek','https://api.deepseek.com/v1','deepseek-chat','DEEPSEEK_API_KEY','n');
        """
    )
    raw.execute("PRAGMA user_version = 5")
    raw.commit()
    raw.close()

    db = Database(path)
    db.initialize()
    with db.connect() as c:
        text = c.execute(
            "SELECT provider,endpoint,model,api_key_env FROM capability_settings WHERE capability='text'"
        ).fetchone()
    print("legacy text adopted:", tuple(text))
    assert tuple(text) == ("deepseek", "https://api.deepseek.com/v1", "deepseek-chat", "DEEPSEEK_API_KEY")
    assert SCHEMA_VERSION == 6

print("CAPABILITY_SETTINGS_OK")
