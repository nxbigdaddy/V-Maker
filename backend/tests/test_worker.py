"""Tests for the persistent task-queue worker (``backend.worker.TaskWorker``).

The suite pins ``VMAKER_WORKER_ENABLED=0`` (``conftest.py``) so no test races a background
consumer. These tests therefore either drive ``run_pending_once()`` by hand or opt in with
``create_app(path, start_worker=True)`` explicitly.

Two setup details keep these tests deterministic:

* Tasks are queued through ``AnalysisRepository`` rather than the task endpoints. The endpoints
  schedule ``TaskExecutor`` through ``BackgroundTasks``, and ``TestClient`` runs those tasks
  before the response is even returned - nothing would be left in the queue to observe.
* The worker only claims tasks that existed when ``start()`` ran, because everything queued
  later in the same process is already owned by ``BackgroundTasks``. ``_backdate`` therefore
  marks a task as queued before the process started - the exact shape of a recovered task.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from backend.analysis import AnalysisRepository
from backend.database import Database
from backend.main import create_app
from backend.repository import ProjectRepository
from backend.schemas import ProjectCreate
from backend.worker import MAX_ERROR_LENGTH, TaskWorker

BUDGET_KEY_ENV = "MISSING_WORKER_TEST_KEY"
ANCIENT = "2000-01-01T00:00:00+00:00"


# --- helpers ---------------------------------------------------------------------------

def _backdate(database_path: Path, task_id: str) -> None:
    """Pretend the task was queued before this process started."""
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE tasks SET created_at=? WHERE id=?", (ANCIENT, task_id))
        connection.commit()


def _worker_threads() -> list[threading.Thread]:
    """Every live thread owned by a worker; used to prove stop() really joins."""
    return [thread for thread in threading.enumerate() if thread.name == "vmaker-task-worker"]


def _project_via_api(client: TestClient, story: str) -> str:
    response = client.post("/api/projects", json={"name": "后台队列测试", "story": story})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _queued_task(client: TestClient, project_id: str) -> str:
    """Queue a task without scheduling it, so only an explicit worker call can run it."""
    task = AnalysisRepository(client.app.state.database).create_analysis_task(project_id)
    assert task is not None and task["status"] == "queued"
    return str(task["id"])


def _queued_analysis_task(client: TestClient, story: str = "女孩帮助机器人回家。") -> tuple[str, str]:
    project_id = _project_via_api(client, story)
    return project_id, _queued_task(client, project_id)


def _status(client: TestClient, task_id: str) -> str:
    response = client.get(f"/api/tasks/{task_id}")
    assert response.status_code == 200, response.text
    return str(response.json()["status"])


def _status_of(database: Database, task_id: str) -> str:
    task = AnalysisRepository(database).get_task(task_id)
    assert task is not None
    return str(task["status"])


def _wait_for_task(client: TestClient, task_id: str, wanted: set[str], timeout: float = 5.0) -> dict:
    """Poll a task until it reaches one of ``wanted``; fail with the last observed state."""
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/tasks/{task_id}")
        if response.status_code == 200:
            last = response.json()
            if str(last["status"]) in wanted:
                return last
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} never reached {sorted(wanted)} within {timeout}s; last={last}")


def _wait_for_execution(worker: TaskWorker, count: int, timeout: float = 5.0) -> dict:
    """Wait for the worker's own counters, which are updated just after the task row is."""
    deadline = time.monotonic() + timeout
    stats = worker.stats()
    while time.monotonic() < deadline:
        stats = worker.stats()
        if stats["executed"] >= count:
            return stats
        time.sleep(0.02)
    raise AssertionError(f"worker never executed {count} task(s) within {timeout}s; stats={stats}")


# --- deterministic, single-step coverage -----------------------------------------------

def test_run_pending_once_returns_none_on_empty_queue(tmp_path: Path) -> None:
    database = Database(tmp_path / "empty.db")
    database.initialize()
    worker = TaskWorker(database, poll_interval=1.0)

    assert worker.run_pending_once() is None
    assert worker.stats()["executed"] == 0


def test_run_pending_once_executes_a_queued_task(tmp_path: Path) -> None:
    database_path = tmp_path / "one.db"
    with TestClient(create_app(database_path, start_worker=False)) as client:
        _, task_id = _queued_analysis_task(client)
        _backdate(database_path, task_id)
        worker = TaskWorker(client.app.state.database)

        assert worker.run_pending_once() == task_id
        completed = client.get(f"/api/tasks/{task_id}").json()
        assert completed["status"] == "completed"
        assert completed["attempts"] == 1
        stats = worker.stats()
        assert stats["executed"] == 1 and stats["failed"] == 0
        assert stats["last_task_id"] == task_id and stats["last_error"] is None
        assert stats["last_finished_at"] is not None


def test_run_pending_once_drains_one_task_at_a_time(tmp_path: Path) -> None:
    database_path = tmp_path / "two.db"
    with TestClient(create_app(database_path, start_worker=False)) as client:
        _, task_id = _queued_analysis_task(client)
        _backdate(database_path, task_id)
        worker = TaskWorker(client.app.state.database)

        assert worker.run_pending_once() == task_id
        assert worker.run_pending_once() is None  # nothing is queued any more
        assert worker.stats()["executed"] == 1


def test_recovered_running_task_is_executed_after_restart(tmp_path: Path) -> None:
    """The defect this worker fixes: a task recovered at startup used to sit queued forever."""
    database_path = tmp_path / "recovered.db"
    with TestClient(create_app(database_path, start_worker=False)) as client:
        _, task_id = _queued_analysis_task(client)

    # Simulate the previous process dying while the task was running.
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE tasks SET status='running',message='was running' WHERE id=?", (task_id,))
        connection.commit()

    # A fresh process, in the same order the lifespan uses: initialize, recover, then work.
    database = Database(database_path)
    database.initialize()
    assert AnalysisRepository(database).recover_running() == 1
    _backdate(database_path, task_id)  # it was queued before this process started

    worker = TaskWorker(database)
    assert worker.run_pending_once() == task_id

    task = AnalysisRepository(database).get_task(task_id)
    assert task is not None
    assert task["status"] == "completed"
    assert task["attempts"] == 1  # the interrupted attempt is not counted twice
    assert any(event["event_type"] == "recovered" for event in task["events"])
    assert worker.stats()["last_error"] is None


def test_failed_task_does_not_stop_the_worker(tmp_path: Path) -> None:
    database_path = tmp_path / "failure.db"
    with TestClient(create_app(database_path, start_worker=False)) as client:
        bad_project, bad_task = _queued_analysis_task(client, story="")
        _backdate(database_path, bad_task)
        worker = TaskWorker(client.app.state.database)

        assert worker.run_pending_once() == bad_task  # the analyzer rejects an empty story
        failed = client.get(f"/api/tasks/{bad_task}").json()
        assert failed["status"] == "failed"
        assert failed["error"] == "story is empty"
        stats = worker.stats()
        assert stats["failed"] == 1 and stats["executed"] == 1  # failed is a subset of executed
        assert stats["last_error"] == "story is empty"
        assert len(str(stats["last_error"])) <= MAX_ERROR_LENGTH

        # The worker is still alive and still consumes: one broken task may not park the queue.
        good_project, good_task = _queued_analysis_task(client)
        _backdate(database_path, good_task)
        assert worker.run_pending_once() == good_task
        assert _status(client, good_task) == "completed"
        assert worker.stats()["executed"] == 2 and worker.stats()["failed"] == 1
        assert worker.stats()["last_error"] is None
        assert _status(client, bad_task) == "failed"
        assert bad_project != good_project


def test_a_robust_worker_survives_a_queue_full_of_failures(tmp_path: Path, monkeypatch) -> None:
    """A provider whose key is missing fails every task; the worker keeps going anyway."""
    database_path = tmp_path / "always_failing.db"
    monkeypatch.delenv(BUDGET_KEY_ENV, raising=False)
    with TestClient(create_app(database_path, start_worker=False)) as client:
        changed = client.put("/api/provider-settings", json={
            "provider": "deepseek", "endpoint": "https://example.invalid/v1",
            "model": "missing-model", "api_key_env": BUDGET_KEY_ENV,
        })
        assert changed.status_code == 200 and changed.json()["configured"] is False
        worker = TaskWorker(client.app.state.database)

        for index in range(2):
            _, task_id = _queued_analysis_task(client)
            _backdate(database_path, task_id)
            assert worker.run_pending_once() == task_id
            assert _status(client, task_id) == "failed"
            assert BUDGET_KEY_ENV in str(client.get(f"/api/tasks/{task_id}").json()["error"])
            assert worker.stats()["failed"] == index + 1

        assert worker.stats()["executed"] == 2
        assert worker.run_pending_once() is None  # the loop survived every failure


def test_worker_disabled_by_argument_leaves_the_queue_alone(tmp_path: Path) -> None:
    database_path = tmp_path / "disabled.db"
    with TestClient(create_app(database_path, start_worker=False)) as client:
        _, task_id = _queued_analysis_task(client)
        _backdate(database_path, task_id)
        time.sleep(0.1)  # long enough for a started worker to have claimed it
        assert _status(client, task_id) == "queued"
        assert client.app.state.worker.stats() == {
            "enabled": False, "running": False, "poll_interval": client.app.state.worker.poll_interval,
            "executed": 0, "failed": 0, "last_task_id": None, "last_error": None, "last_finished_at": None,
        }
        # ... and an explicit call still runs it, so only the automation is off.
        assert client.app.state.worker.run_pending_once() == task_id
        assert _status(client, task_id) == "completed"


def test_disabled_worker_refuses_to_start_a_thread(tmp_path: Path) -> None:
    database = Database(tmp_path / "disabled_flag.db")
    database.initialize()
    project = ProjectRepository(database).create(ProjectCreate(name="关闭 Worker", story="女孩帮助机器人回家。"))
    task = AnalysisRepository(database).create_analysis_task(str(project["id"]))
    assert task is not None
    _backdate(database.path, str(task["id"]))

    off = TaskWorker(database, enabled=False)
    off.start()
    time.sleep(0.05)
    assert off.stats()["running"] is False
    assert off.run_pending_once() == str(task["id"])  # manual execution is unaffected
    assert _status_of(database, str(task["id"])) == "completed"


def test_diagnostics_reports_worker_state(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "diagnostics_on.db", start_worker=True)) as client:
        payload = client.get("/api/system/diagnostics").json()
        assert payload["worker"]["enabled"] is True
        assert payload["worker"]["running"] is True
        assert payload["worker"]["poll_interval"] > 0
        assert set(payload["worker"]) >= {
            "enabled", "running", "poll_interval", "executed", "failed",
            "last_task_id", "last_error", "last_finished_at",
        }

    # ``start_worker=False`` parks the queue, and diagnostics says so instead of pretending.
    with TestClient(create_app(tmp_path / "diagnostics_off.db", start_worker=False)) as client:
        payload = client.get("/api/system/diagnostics").json()
        assert payload["worker"]["enabled"] is False
        assert payload["worker"]["running"] is False
        assert payload["worker"]["poll_interval"] > 0


def test_a_task_is_never_executed_twice(tmp_path: Path) -> None:
    database_path = tmp_path / "once.db"
    with TestClient(create_app(database_path, start_worker=False)) as client:
        _, task_id = _queued_analysis_task(client)
        _backdate(database_path, task_id)
        worker = TaskWorker(client.app.state.database)

        assert worker.run_pending_once() == task_id
        first = client.get(f"/api/tasks/{task_id}").json()
        assert first["status"] == "completed" and first["attempts"] == 1

        assert worker.run_pending_once() is None  # nothing queued is left to double-run
        again = client.get(f"/api/tasks/{task_id}").json()
        assert again["attempts"] == 1 and again["status"] == "completed"
        assert worker.stats()["executed"] == 1

        # ``claim()`` is the last line of defence: a second worker cannot re-enter a running task.
        assert AnalysisRepository(client.app.state.database).claim(task_id) is None
        assert client.get(f"/api/tasks/{task_id}").json()["attempts"] == 1


# --- the background thread itself ------------------------------------------------------

def test_start_and_stop_are_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "idempotent.db")
    database.initialize()
    worker = TaskWorker(database, poll_interval=0.05)

    worker.start()
    worker.start()
    assert worker.stats()["running"] is True
    worker.stop()
    worker.stop()
    assert worker.stats()["running"] is False


def test_background_thread_executes_a_queued_task(tmp_path: Path) -> None:
    database_path = tmp_path / "thread.db"
    app = create_app(database_path, start_worker=True)
    with TestClient(app) as client:
        _, task_id = _queued_analysis_task(client)
        _backdate(database_path, task_id)  # queued as if the previous process had left it

        worker = app.state.worker
        assert worker.stats()["enabled"] is True
        completed = _wait_for_task(client, task_id, {"completed", "failed"})
        assert completed["status"] == "completed", completed
        assert completed["attempts"] == 1
        # The row is committed just before the worker books the round, so wait for both.
        assert _wait_for_execution(worker, 1)["failed"] == 0
        assert worker.stats()["running"] is True  # still polling after finishing the task

        worker.stop()
        assert worker.stats()["running"] is False
        assert _worker_threads() == []
    # Leaving the context manager must stay safe even though stop() already ran.
    assert app.state.worker.stats()["running"] is False


def test_worker_adopts_a_task_stuck_in_the_queue(tmp_path: Path) -> None:
    """A task queued by this same process is skipped only while it is fresh.

    BackgroundTasks normally runs it within milliseconds. If scheduling ever fails, the task must
    not stay in 'queued' forever, so the worker adopts anything that is still queued after the
    grace period — that is the whole point of having a consumer for this queue.
    """
    database = Database(tmp_path / "stale.db")
    database.initialize()
    repository = AnalysisRepository(database)
    with TestClient(create_app(tmp_path / "stale.db", start_worker=False)) as client:
        project = client.post("/api/projects", json={
            "name": "陈旧队列", "story": "邮差在末班车上发现一封写给自己的信。", "target_duration_sec": 20,
        }).json()
        task = repository.create_task(project["id"], "story_analysis", capability="story_analysis")

        # ``start()`` marks the ownership instant; this task was created after it, so a default
        # worker would ignore it...
        worker = TaskWorker(database, poll_interval=0.05, stale_after_seconds=30.0)
        worker._start_unix = time.time() - 1
        assert worker.run_pending_once() is None
        assert repository.get_task(task["id"])["status"] == "queued"

        # ...but once it has been queued past the grace period the worker takes it over.
        stale = TaskWorker(database, poll_interval=0.05, stale_after_seconds=0.0)
        stale._start_unix = time.time() - 1
        assert stale.run_pending_once() == task["id"]
        assert repository.get_task(task["id"])["status"] == "completed"