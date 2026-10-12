"""Background consumer for the persistent ``queued`` task queue.

``backend.main`` recovers interrupted tasks back to ``queued`` on startup and the GUI can
requeue a failed task with ``POST /api/tasks/{id}/retry``. Before this module existed nothing
drained that queue: the endpoints only ran a task through ``BackgroundTasks`` in the request
that created it, so a recovered or retried task stayed "排队中" forever.

``TaskWorker`` owns exactly that gap. It never reimplements execution - every task is handed
to ``TaskExecutor.execute`` - and it serializes every execution behind one lock so the polling
thread can never run a task at the same time as a manual ``POST /api/tasks/{id}/execute``.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

from backend.analysis import AnalysisRepository, TaskExecutor
from backend.database import Database
from backend.providers import scrub_secret

logger = logging.getLogger(__name__)

# A worker error string is diagnostic metadata, never a credential dump.
MAX_ERROR_LENGTH = 500


def _safe_error(exc: object) -> str:
    """One short, secret-free line for ``stats()['last_error']``."""
    return scrub_secret(exc, None)[:MAX_ERROR_LENGTH]


class TaskWorker:
    """Single-flight consumer for queued tasks, safe to drive manually or from its own thread."""

    def __init__(self, database: Database, *, poll_interval: float = 1.0, enabled: bool = True,
                 stale_after_seconds: float = 30.0) -> None:
        self.database = database
        self.poll_interval = max(0.0, float(poll_interval))
        self.enabled = bool(enabled)
        # A task queued by this same process is normally run by BackgroundTasks within milliseconds.
        # If one is *still* queued after this grace period, that scheduling failed, and the worker
        # adopts it instead of leaving it stranded forever.
        self.stale_after_seconds = max(0.0, float(stale_after_seconds))
        # Guards task selection plus execution, so one process runs at most one task at a time.
        self._execute_lock = threading.Lock()
        # Guards the small pieces of mutable state below, independently of task execution.
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._executed = 0
        self._failed = 0
        self._last_task_id: str | None = None
        self._last_error: str | None = None
        self._last_finished_at: float | None = None
        # Unix time from which this worker owns the queue, set by ``start()``. Manual
        # ``run_pending_once()`` calls leave it unset and therefore consider every queued task.
        self._start_unix: float | None = None

    # --- public API --------------------------------------------------------------------

    def run_pending_once(self) -> str | None:
        """Claim and run at most one queued task; return its id, or None when the queue is empty.

        Selection and execution share one lock with the polling loop, so a task picked up here
        can never be executed concurrently by the background thread.
        """
        with self._execute_lock:
            if self._stop_event.is_set():
                # A stop() that arrived while another round held the lock wins: claim no more work.
                return None
            task_id = self._next_queued_task(self._start_unix)
            if task_id is None:
                return None
            with self._state_lock:
                self._last_task_id = task_id
            try:
                task = TaskExecutor(self.database).execute(task_id)
            except Exception as exc:  # a broken task must never take the worker down
                self._record_failure(exc)
                logger.exception("task %s raised while executing", task_id)
                return task_id
            status = str(task["status"]) if task else ""
            if status == "failed":
                error = str(task.get("error") or "") if task else ""
                self._record_failure(error[:MAX_ERROR_LENGTH] or "task failed")
            else:
                self._record_success()
            return task_id

    def start(self) -> None:
        """Start the daemon polling thread; calling it twice is a no-op."""
        with self._state_lock:
            if not self.enabled or (self._thread is not None and self._thread.is_alive()):
                return
            # Recovered and retried work is older than this instant, so it is all claimable.
            self._start_unix = time.time()
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, name="vmaker-task-worker", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the thread to exit and join it; calling it twice is safe."""
        with self._state_lock:
            thread = self._thread
        self._stop_event.set()  # interrupts the poll sleep immediately
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():  # pragma: no cover - only on a hung task
                logger.warning("task worker did not stop within %.1fs", timeout)
        with self._state_lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def stats(self) -> dict[str, Any]:
        """A snapshot for diagnostics/tests: ``failed`` is the failed subset of ``executed``."""
        with self._state_lock:
            thread, executed, failed = self._thread, self._executed, self._failed
            return {
                "enabled": self.enabled,
                "running": thread is not None and thread.is_alive(),
                "poll_interval": self.poll_interval,
                "executed": executed,
                "failed": failed,
                "last_task_id": self._last_task_id,
                "last_error": self._last_error,
                "last_finished_at": self._last_finished_at,
            }

    # --- internals ---------------------------------------------------------------------

    def _loop(self) -> None:
        """Poll forever until ``stop()``; one bad round must never kill the thread."""
        while not self._stop_event.is_set():
            try:
                self.run_pending_once()
            except Exception as exc:  # defence in depth: run_pending_once already guards the task
                self._record_failure(exc)
                logger.exception("task worker round failed")
            if self._stop_event.is_set():
                break  # stop() arrived during this round: do not sleep, do not claim again
            # An idle queue sleeps instead of spinning, and stop() wakes the sleep at once.
            self._stop_event.wait(self.poll_interval)

    def _next_queued_task(self, since_unix: float | None = None) -> str | None:
        """Oldest queued task id, without changing its status (``TaskExecutor`` claims it).

        ``since_unix`` restricts the thread to work that existed when it started. Everything
        queued later was created by this running process, and those endpoints already run their
        task through ``BackgroundTasks``; skipping them keeps the queue from being run twice.
        ``created_at`` is an ISO-8601 UTC string, so a comparison against a formatted instant is
        both valid and index-friendly.

        A task that is still ``queued`` long after it was last touched is the exception: its
        ``BackgroundTasks`` scheduling evidently never happened, so the worker adopts it. Without
        that, a task created during this process could sit in ``queued`` forever — the very defect
        this worker exists to remove.
        """
        query = "SELECT id FROM tasks WHERE status='queued'"
        conditions: list[str] = []
        parameters: list[str] = []
        if since_unix is not None:
            conditions.append("created_at < ?")
            parameters.append(datetime.fromtimestamp(since_unix, UTC).isoformat())
            conditions.append("updated_at < ?")
            parameters.append(datetime.fromtimestamp(time.time() - self.stale_after_seconds, UTC).isoformat())
        if conditions:
            query += " AND (" + " OR ".join(conditions) + ")"
        query += " ORDER BY created_at, rowid LIMIT 1"
        with self.database.connect() as connection:
            row = connection.execute(query, tuple(parameters)).fetchone()
        return str(row["id"]) if row is not None else None

    def _record_success(self) -> None:
        with self._state_lock:
            self._executed += 1
            self._last_error = None
            self._last_finished_at = time.time()

    def _record_failure(self, error: object) -> None:
        with self._state_lock:
            # ``failed`` is the subset of ``executed`` that ended as a failed task or raised.
            self._executed += 1
            self._failed += 1
            self._last_error = _safe_error(error)
            self._last_finished_at = time.time()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TaskWorker(enabled={self.enabled}, poll_interval={self.poll_interval})"


__all__ = ["TaskWorker", "MAX_ERROR_LENGTH"]
