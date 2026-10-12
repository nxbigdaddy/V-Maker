"""Runtime settings for the local-only API."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    host: str = "127.0.0.1"
    port: int = 8765
    # The persistent queue is drained in the background; tests turn this off to stay deterministic.
    worker_enabled: bool = True
    worker_poll_seconds: float = 1.0
    # A task still queued after this long was never picked up by BackgroundTasks; the worker adopts it.
    worker_stale_seconds: float = 30.0
    # Real image-to-video services are asynchronous: the run submits a job, then polls it. These
    # two values decide how often that poll happens and when the whole wait is abandoned.
    video_poll_seconds: float = 3.0
    video_timeout_seconds: float = 600.0
    # Optional path of the user's real JianyingPro draft root (the directory that holds one
    # folder per draft). Unset means "exports only": the generated draft then lives inside the
    # project export directory, which Jianying does not scan by itself.
    jianying_drafts_dir: Path | None = None


# Anything else (including a typo) keeps the worker on rather than silently parking the queue.
_FALSE_VALUES = {"0", "false", "no", "off"}


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSE_VALUES


def _poll_seconds_env(name: str, default: float) -> float:
    """A positive, finite poll interval, or the default: zero would busy-spin the CPU."""
    return _positive_seconds_env(name, default)


def _positive_seconds_env(name: str, default: float) -> float:
    """A positive, finite duration in seconds, or the default.

    A poll interval has to be measurable in whole seconds to be worth configuring, but a total
    timeout is still meaningful below one second: a test can keep the whole wait tiny without
    losing the property that a timeout is a timeout.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def get_settings() -> Settings:
    """Load settings from environment variables with local-safe defaults."""
    default_data_dir = Path(__file__).resolve().parent / "data"
    database_path = Path(
        os.getenv("VMAKER_DATABASE_PATH", str(default_data_dir / "vmaker.db"))
    ).expanduser()
    # An empty or blank value means "not configured" and never a bogus empty path.
    raw_drafts_dir = (os.getenv("VMAKER_JIANYING_DRAFTS_DIR") or "").strip()
    return Settings(
        database_path=database_path,
        host=os.getenv("VMAKER_HOST", "127.0.0.1"),
        port=int(os.getenv("VMAKER_PORT", "8765")),
        worker_enabled=_bool_env("VMAKER_WORKER_ENABLED", True),
        worker_poll_seconds=_poll_seconds_env("VMAKER_WORKER_POLL_SECONDS", 1.0),
        worker_stale_seconds=_poll_seconds_env("VMAKER_WORKER_STALE_SECONDS", 30.0),
        video_poll_seconds=_poll_seconds_env("VMAKER_VIDEO_POLL_SECONDS", 3.0),
        # Deliberately the permissive reader: a sub-second total timeout still means "give up".
        video_timeout_seconds=_positive_seconds_env("VMAKER_VIDEO_TIMEOUT_SECONDS", 600.0),
        # An empty value is "unset"; anything else is expanded so ``~`` works in a .env file.
        jianying_drafts_dir=Path(raw_drafts_dir).expanduser() if raw_drafts_dir else None,
    )
