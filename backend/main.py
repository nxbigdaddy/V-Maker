"""FastAPI application for V-Maker's local MVP backend."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import APIRouter, BackgroundTasks, Body, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from backend.analysis import AnalysisRepository, TaskExecutor
from backend.config import get_settings
from backend.database import Database
from backend.jianying import (
    LIBRARY_NAME as JIANYING_LIBRARY_NAME,
    library_available as jianying_library_available,
    library_version as jianying_library_version,
)
from backend.probe import test_capability_connection
from backend.repository import ProjectRepository
from backend.production import ProductionRepository
from backend.media import MediaRepository
from backend.pipeline import STAGES as PIPELINE_STAGES
from backend.storage import cleanup_storage, storage_summary
from backend.worker import TaskWorker
from backend.schemas import (
    BibleVersion,
    CapabilitySettings,
    CapabilitySettingsUpdate,
    CapabilityTestRequest,
    CapabilityTestResult,
    ExportRequest,
    HealthResponse,
    Episode,
    ScriptStoryboard,
    Shot,
    ShotUpdate,
    Project,
    ProjectCreate,
    ProjectDashboard,
    ProjectUpdate,
    ProviderSettings,
    ProviderSettingsUpdate,
    StoryAnalysis,
    Task,
    WorkflowStage,
)


@lru_cache(maxsize=4)
def _ffmpeg_version(path: str) -> str | None:
    """Ask the binary itself; cached because diagnostics can be opened repeatedly."""
    try:
        completed = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    first = (completed.stdout or "").splitlines()
    if not first:
        return None
    match = re.search(r"ffmpeg version (\S+)", first[0])
    return match.group(1) if match else first[0][:60]


#: 项目根目录（backend/main.py 的上两级），用于定位服务管理器脚本。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVICE_MANAGER = PROJECT_ROOT / "scripts" / "service.ps1"


def _managed_pid(data_dir: Path) -> int | None:
    """The PID the service manager recorded, if any.

    ``scripts/service.ps1`` owns the backend process (PID file + log). Comparing that record with
    our own PID is the only honest way to answer "am I managed?" — guessing from the environment
    would be wrong exactly when it matters.
    """
    record = data_dir / "backend.pid"
    try:
        raw = record.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def _spawn_service_manager(data_dir: Path, argument: str) -> dict[str, object]:
    """Run the service manager detached, so it can stop/restart the process serving this request.

    The request itself must return first: the manager kills this process. The interpreter is passed
    through so a restart uses the same Python (and therefore the same optional dependencies).
    """
    if not SERVICE_MANAGER.is_file():
        return {"ok": False, "message": f"服务管理器脚本不存在：{SERVICE_MANAGER}"}
    environment = dict(os.environ)
    environment["VMAKER_PYTHON"] = sys.executable
    # 只用 CREATE_NO_WINDOW。**不要**加 DETACHED_PROCESS：那样子进程没有控制台，
    # Windows PowerShell 会立刻以 0 退出、什么都不做（实测 0 字节输出），于是重启静默失败。
    # 单独用 CREATE_NO_WINDOW 时实测能正常运行，且父进程退出后仍会跑完。
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    service_log = logs / "service.log"
    try:
        # 管理器自己的输出要留下：否则一次失败的重启只会表现为"界面一直连不上"，毫无线索。
        with open(service_log, "ab") as sink:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(SERVICE_MANAGER), argument],
                cwd=str(PROJECT_ROOT), env=environment, creationflags=flags,
                stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
            )
    except OSError as exc:
        return {"ok": False, "message": f"无法启动服务管理器：{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "action": argument,
        "service_log": str(service_log),
        "message": ("已请求重启后端；本页会在几秒内失去连接，随后自动重试。"
                    "若十秒后仍未恢复，请查看服务日志（service.log）与后端日志。"
                    if argument == "restart" else
                    "已请求停止后端；停止后界面将无法再连接，需要重新启动它。"),
    }

DEFAULT_CORS_ORIGINS = [
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "tauri://localhost",
    "https://tauri.localhost",
]


def _asset_content_response(path: Path, mime_type: str, filename: str, sha256: str) -> FileResponse:
    """Serve one verified asset version inline with a locked-down, immutable contract."""
    return FileResponse(
        path,
        media_type=mime_type,
        filename=filename,
        content_disposition_type="inline",
        headers={
            "ETag": f'"{sha256}"',
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            # Inline SVG must never execute scripts when a browser opens it directly.
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        },
    )


def create_app(database_path: Path | str | None = None, *,
               start_worker: bool | None = None) -> FastAPI:
    """Build the API application.

    ``start_worker`` decides whether the lifespan drains the persistent task queue:
    ``None`` (the default) follows ``VMAKER_WORKER_ENABLED``, while an explicit value
    overrides it so a caller can keep the queue untouched.
    """
    settings = get_settings()
    database = Database(database_path or settings.database_path)
    # An explicit argument wins over the environment; ``None`` follows VMAKER_WORKER_ENABLED.
    worker_enabled = settings.worker_enabled if start_worker is None else bool(start_worker)
    worker = TaskWorker(database, poll_interval=settings.worker_poll_seconds,
                        enabled=worker_enabled,
                        stale_after_seconds=settings.worker_stale_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        # Interrupted tasks go back to the queue first, then the worker takes over the queue.
        AnalysisRepository(database).recover_running()
        if worker_enabled:
            worker.start()
        try:
            yield
        finally:
            worker.stop()

    application = FastAPI(
        title="V-Maker Local API",
        description="Local-only FastAPI backend for the V-Maker desktop MVP.",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.database = database
    application.state.worker = worker
    application.add_middleware(
        CORSMiddleware,
        allow_origins=DEFAULT_CORS_ORIGINS,
        allow_origin_regex=r"^(https?://(localhost|127\.0\.0\.1)(:\d+)?|tauri://localhost)$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    router = APIRouter(prefix="/api")

    @application.exception_handler(ValidationError)
    async def schema_error_handler(_: Request, exc: ValidationError) -> JSONResponse:
        """A capability test validates a merged draft with the same rules the PUT route uses.

        Body-schema violations are already handled natively by FastAPI; this only covers the
        explicit ``model_validate`` a connection test performs on the merged configuration.
        """
        errors = [
            {"loc": ["body", *[str(item) for item in error.get("loc", ())]],
             "msg": str(error.get("msg", "")), "type": str(error.get("type", ""))}
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})

    def repository() -> ProjectRepository:
        return ProjectRepository(application.state.database)

    def analysis_repository() -> AnalysisRepository:
        return AnalysisRepository(application.state.database)

    def production_repository() -> ProductionRepository:
        return ProductionRepository(application.state.database)

    def media_repository() -> MediaRepository:
        return MediaRepository(application.state.database)

    def start_shot_media_task(task_type: str, project_id: str, shot_ids: list[str] | None,
                              background_tasks: BackgroundTasks,
                              repo: AnalysisRepository) -> dict[str, object]:
        """Queue and kick off one shot-level media task, mapping contract violations to HTTP codes."""
        task, reason = repo.create_shot_media_task(project_id, task_type, shot_ids)
        if task is None:
            if reason == "project not found":
                raise HTTPException(status_code=404, detail="project not found")
            raise HTTPException(status_code=422, detail="shot_ids must reference shots of this project")
        task["events"] = repo.get_task(str(task["id"]))["events"]  # type: ignore[index]
        background_tasks.add_task(TaskExecutor(application.state.database).execute, str(task["id"]))
        return task

    @router.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        try:
            with database.connect() as connection:
                connection.execute("SELECT 1").fetchone()
        except sqlite3.Error as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="database unavailable",
            ) from exc
        return HealthResponse(status="ok", service="v-maker-backend", database="ok")

    @router.post(
        "/projects", response_model=Project, status_code=status.HTTP_201_CREATED, tags=["projects"]
    )
    def create_project(
        payload: ProjectCreate, repo: ProjectRepository = Depends(repository)
    ) -> dict[str, object]:
        project = repo.create(payload)
        MediaRepository(application.state.database).ensure_project(str(project["id"]))
        return project

    @router.get("/projects", response_model=list[Project], tags=["projects"])
    def list_projects(
        include_archived: bool = Query(False), include_deleted: bool = Query(False),
        repo: ProjectRepository = Depends(repository),
    ) -> list[dict[str, object]]:
        return repo.list(include_archived=include_archived, include_deleted=include_deleted)

    @router.get("/projects/{project_id}", response_model=Project, tags=["projects"])
    def get_project(
        project_id: str, repo: ProjectRepository = Depends(repository)
    ) -> dict[str, object]:
        project = repo.get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return project

    @router.patch("/projects/{project_id}", response_model=Project, tags=["projects"])
    def update_project(
        project_id: str,
        payload: ProjectUpdate,
        repo: ProjectRepository = Depends(repository),
    ) -> dict[str, object]:
        project = repo.update(project_id, payload)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return project

    @router.delete(
        "/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["projects"]
    )
    def delete_project(
        project_id: str, repo: ProjectRepository = Depends(repository)
    ) -> Response:
        if not repo.delete(project_id):
            raise HTTPException(status_code=404, detail="project not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/projects/{project_id}/archive", response_model=Project, tags=["projects"])
    def archive_project(project_id: str, repo: ProjectRepository = Depends(repository)):
        result = repo.archive(project_id)
        if result is None: raise HTTPException(status_code=404, detail="project not found")
        return result

    @router.post("/projects/{project_id}/restore", response_model=Project, tags=["projects"])
    def restore_project(project_id: str, repo: ProjectRepository = Depends(repository)):
        result = repo.restore(project_id)
        if result is None: raise HTTPException(status_code=404, detail="project not found")
        return result

    @router.post("/projects/{project_id}/duplicate", response_model=Project, status_code=201, tags=["projects"])
    @router.post("/projects/{project_id}/copy", response_model=Project, status_code=201, tags=["projects"])
    def copy_project(project_id: str, payload: dict = Body(default_factory=dict), repo: ProjectRepository = Depends(repository)):
        result = repo.copy(project_id, payload.get("name"))
        if result is None: raise HTTPException(status_code=404, detail="project not found")
        MediaRepository(application.state.database).ensure_project(str(result["id"]))
        return result

    @router.get(
        "/projects/{project_id}/dashboard",
        response_model=ProjectDashboard,
        tags=["projects"],
    )
    def project_dashboard(
        project_id: str, repo: ProjectRepository = Depends(repository)
    ) -> ProjectDashboard:
        project_data = repo.get(project_id)
        if project_data is None:
            raise HTTPException(status_code=404, detail="project not found")
        stages_data = repo.workflow_stages(project_id)
        stage_models = [WorkflowStage.model_validate(item) for item in stages_data]
        summary: Counter[str] = Counter(stage.status for stage in stage_models)
        budget, task_summary = repo.dashboard_stats(project_id)
        return ProjectDashboard(
            project=Project.model_validate(project_data), workflow_stages=stage_models,
            workflow_summary={"pending": summary["pending"], "in_progress": summary["in_progress"],
                "completed": summary["completed"], "blocked": summary["blocked"], "failed": summary["failed"]},
            budget=budget, task_summary={key: task_summary.get(key, 0) for key in ("queued","running","paused","completed","failed","canceled")},
        )

    @router.get("/projects/{project_id}/costs", tags=["projects"])
    def project_costs(project_id: str, repo: ProjectRepository = Depends(repository)):
        if repo.get(project_id) is None: raise HTTPException(status_code=404, detail="project not found")
        budget, _ = repo.dashboard_stats(project_id)
        with application.state.database.connect() as connection:
            entries = [dict(row) for row in connection.execute(
                "SELECT id,project_id,task_id,entry_type,amount,provider,model,note,created_at FROM cost_ledger WHERE project_id=? ORDER BY id", (project_id,)
            ).fetchall()]
        return {"project_id": project_id, "currency": "CNY", "budget": {
            "currency": "CNY", "hard_limit": budget["limit"], "limit": budget["limit"], "spent": budget["settled"], "settled": budget["settled"], "reserved": budget["reserved"], "available": budget["available"]
        }, "spent": budget["settled"], "reserved": budget["reserved"], "hard_limit": budget["limit"], "items": entries, "entries": entries}

    @router.get("/system/diagnostics", tags=["system"])
    def diagnostics():
        import sys
        data_dir = application.state.database.path.parent.resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        with application.state.database.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            queued = connection.execute("SELECT COUNT(*) FROM tasks WHERE status='queued'").fetchone()[0]
        configured_ffmpeg = os.getenv("VMAKER_FFMPEG_PATH")
        # 与导出使用同一个解析函数，避免界面说「没有 FFmpeg」而导出却能出片这种自相矛盾。
        ffmpeg_path = MediaRepository._ffmpeg()
        if ffmpeg_path:
            if configured_ffmpeg and Path(configured_ffmpeg).is_file():
                ffmpeg_source = "configured"
            elif shutil.which("ffmpeg") == ffmpeg_path:
                ffmpeg_source = "path"
            else:
                ffmpeg_source = "imageio-ffmpeg"
        writable = os.access(data_dir, os.W_OK)
        worker_stats = worker.stats()
        # The Jianying library is optional: report honestly what it can and cannot do, and never
        # claim a draft was verified in a real Jianying installation (it was not).
        jianying_available = jianying_library_available()
        jianying_version = jianying_library_version()
        drafts_dir = settings.jianying_drafts_dir
        return {
            "ok": True, "status": "ok", "database": "ok", "schema_version": version, "queued_tasks": queued,
            "health": {"ok": True, "message": f"SQLite schema v{version}"},
            "ffmpeg": {
                "available": bool(ffmpeg_path),
                "version": _ffmpeg_version(ffmpeg_path) if ffmpeg_path else None,
                "source": ffmpeg_source if ffmpeg_path else None,
                "path": ffmpeg_path,
                "message": ffmpeg_path or "未检测到 FFmpeg；将输出通用草稿包（可安装可选项 imageio-ffmpeg，或设置 VMAKER_FFMPEG_PATH）",
            },
            # 后端进程自身的信息：界面上的「后端服务」面板据此显示状态与可执行的操作。
            "process": {
                "pid": os.getpid(),
                "port": int(os.getenv("VMAKER_PORT", "8765") or 8765),
                "python": sys.executable,
                "managed": _managed_pid(data_dir) == os.getpid(),
                "pid_file": str(data_dir / "backend.pid"),
                "log_file": str(data_dir / "logs" / "backend.out.log"),
                "service_log_file": str(data_dir / "logs" / "service.log"),
                "manager": str(SERVICE_MANAGER),
                "manager_available": SERVICE_MANAGER.is_file(),
                "can_restart": SERVICE_MANAGER.is_file(),
                # 停止之后这个进程就没了，界面也就无法再请求「启动」——那一步只能由启动器或命令行做。
                "note": "重启/停止由 scripts/service.ps1 执行；「启动」必须用 start-vmaker.bat backend 或该脚本。",
            },
            "jianying": {
                "library": JIANYING_LIBRARY_NAME,
                "available": jianying_available,
                "version": jianying_version,
                "capability": "draft_generation" if jianying_available else "unavailable",
                "draft_generation": jianying_available,
                "auto_export": False,
                "message": (
                    f"{JIANYING_LIBRARY_NAME} {jianying_version or '（版本未知）'} 可用：可生成剪映草稿；"
                    "不做自动导出（该库的自动导出只支持剪映 6 及以下）"
                    if jianying_available else
                    f"未安装或不兼容 {JIANYING_LIBRARY_NAME}：导出时回退到通用草稿包"
                ),
                "unverified": "未在真实剪映中验证：草稿能否被剪映打开、draft_meta_info.json 中 draft_name 为空是否有影响，均未确认",
                "asset_paths": "草稿中的素材为绝对路径引用；删除项目素材会让草稿失效，草稿只在同一台机器上可用",
                "svg": "剪映不支持 SVG；只有 SVG 素材的镜头会被跳过并记录在导出的 warning 中",
                "drafts_dir": {
                    "configured": bool(drafts_dir),
                    "path": str(drafts_dir) if drafts_dir else None,
                    "writable": bool(drafts_dir) and os.access(drafts_dir, os.W_OK),
                    "message": (
                        f"已配置剪映草稿根目录：{drafts_dir}（草稿会额外写入该目录）"
                        if drafts_dir else
                        "未配置 VMAKER_JIANYING_DRAFTS_DIR；草稿只写入项目导出目录，剪映不会自动看到它"
                    ),
                },
            },
            "data_dir": {"path": str(data_dir), "writable": writable},
            "desktop": {"available": False, "message": "当前后端独立运行；Tauri Python sidecar 尚未捆绑"},
            "python": {"version": sys.version.split()[0]},
            # ``enabled`` reports the effective state: a worker the lifespan never started consumes nothing.
            "worker": {**worker_stats, "enabled": bool(worker_enabled and worker_stats["enabled"])},
        }

    @router.post("/system/backend/restart", tags=["system"])
    def restart_backend():
        """Ask the service manager to restart this backend.

        The reply is sent before the restart happens — the manager has to kill the very process
        serving this request. The UI is expected to lose the connection and retry.
        """
        return _spawn_service_manager(application.state.database.path.parent, "restart")

    @router.post("/system/backend/stop", tags=["system"])
    def stop_backend():
        """Ask the service manager to stop this backend.

        After this succeeds there is no API left to ask for a start: that has to come from
        ``start-vmaker.bat backend`` or ``scripts/service.ps1 start``.
        """
        return _spawn_service_manager(application.state.database.path.parent, "stop")

    @router.get("/provider-settings", response_model=ProviderSettings, tags=["analysis"])
    @router.get("/settings/text-provider", response_model=ProviderSettings, tags=["analysis"])
    def get_provider_settings(
        repo: AnalysisRepository = Depends(analysis_repository),
    ) -> dict[str, object]:
        return repo.get_provider_settings()

    @router.put("/provider-settings", response_model=ProviderSettings, tags=["analysis"])
    @router.put("/settings/text-provider", response_model=ProviderSettings, tags=["analysis"])
    def update_provider_settings(
        payload: ProviderSettingsUpdate,
        repo: AnalysisRepository = Depends(analysis_repository),
    ) -> dict[str, object]:
        return repo.update_provider_settings(payload)

    # --- capability settings: one row per capability, text shared with the routes above ---

    @router.get("/capability-settings", response_model=list[CapabilitySettings], tags=["settings"])
    def list_capability_settings(repo: AnalysisRepository = Depends(analysis_repository)):
        return repo.list_capability_settings()

    @router.get("/capability-settings/{capability}", response_model=CapabilitySettings, tags=["settings"])
    def get_capability_settings(capability: str, repo: AnalysisRepository = Depends(analysis_repository)):
        result = repo.get_capability_settings(capability)
        if result is None:
            raise HTTPException(status_code=404, detail="capability not found")
        return result

    @router.put("/capability-settings/{capability}", response_model=CapabilitySettings, tags=["settings"])
    def update_capability_settings(
        capability: str,
        payload: CapabilitySettingsUpdate,
        repo: AnalysisRepository = Depends(analysis_repository),
    ):
        result = repo.update_capability_settings(capability, payload)
        if result is None:
            raise HTTPException(status_code=404, detail="capability not found")
        return result

    @router.post("/capability-settings/{capability}/test", response_model=CapabilityTestResult,
                 tags=["settings"])
    def test_capability_settings(
        capability: str,
        payload: CapabilityTestRequest | None = Body(default=None),
    ) -> dict[str, object]:
        """Check a capability's configuration before it is ever used for real work.

        The body is optional and never persisted: fields it omits are read from the saved row.
        """
        result = test_capability_connection(
            application.state.database, capability, payload or CapabilityTestRequest())
        if result is None:
            raise HTTPException(status_code=404, detail="capability not found")
        return result

    @router.post(
        "/projects/{project_id}/analysis-tasks",
        response_model=Task,
        status_code=status.HTTP_201_CREATED,
        tags=["analysis"],
    )
    def create_analysis_task(
        project_id: str,
        background_tasks: BackgroundTasks,
        repo: AnalysisRepository = Depends(analysis_repository),
    ) -> dict[str, object]:
        task = repo.create_analysis_task(project_id)
        if task is None:
            raise HTTPException(status_code=404, detail="project not found")
        task["events"] = repo.get_task(str(task["id"]))["events"]  # type: ignore[index]
        background_tasks.add_task(TaskExecutor(application.state.database).execute, str(task["id"]))
        return task

    @router.get("/tasks/{task_id}", response_model=Task, tags=["analysis"])
    def get_task(
        task_id: str, repo: AnalysisRepository = Depends(analysis_repository)
    ) -> dict[str, object]:
        task = repo.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task

    @router.get("/projects/{project_id}/tasks", response_model=list[Task], tags=["tasks"])
    def list_project_tasks(project_id: str, repo: AnalysisRepository = Depends(analysis_repository)):
        result = repo.list_tasks(project_id)
        if result is None: raise HTTPException(status_code=404, detail="project not found")
        return result

    @router.post("/tasks/{task_id}/cancel", response_model=Task, tags=["tasks"])
    def cancel_task(task_id: str, repo: AnalysisRepository = Depends(analysis_repository)):
        result = repo.cancel(task_id)
        if result is None: raise HTTPException(status_code=404, detail="task not found")
        return result

    @router.post("/tasks/{task_id}/retry", response_model=Task, tags=["tasks"])
    def retry_task(task_id: str, background_tasks: BackgroundTasks, execute: bool = False,
                   repo: AnalysisRepository = Depends(analysis_repository)):
        try: result = repo.retry(task_id)
        except ValueError as exc: raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None: raise HTTPException(status_code=404, detail="task not found")
        if execute: background_tasks.add_task(TaskExecutor(application.state.database).execute, task_id)
        return result

    @router.post("/tasks/execute-next", response_model=Task | None, tags=["tasks"])
    def execute_next_task():
        return TaskExecutor(application.state.database).execute()

    @router.post("/tasks/{task_id}/execute", response_model=Task, tags=["analysis"])
    def execute_task(task_id: str) -> dict[str, object]:
        task = TaskExecutor(application.state.database).execute(task_id)
        if task is None: raise HTTPException(status_code=404, detail="task not found")
        return task

    @router.get(
        "/projects/{project_id}/analysis", response_model=StoryAnalysis, tags=["analysis"]
    )
    @router.get(
        "/projects/{project_id}/story-analysis", response_model=StoryAnalysis, tags=["analysis"]
    )
    def get_project_analysis(
        project_id: str,
        project_repo: ProjectRepository = Depends(repository),
        repo: AnalysisRepository = Depends(analysis_repository),
    ) -> dict[str, object]:
        if project_repo.get(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        analysis = repo.latest_analysis(project_id)
        if analysis is None:
            raise HTTPException(status_code=404, detail="analysis not found")
        return analysis

    @router.post("/projects/{project_id}/bible", response_model=BibleVersion, status_code=201, tags=["bible"])
    @router.post("/projects/{project_id}/bible/generate", response_model=BibleVersion, status_code=201, tags=["bible"])
    @router.post("/projects/{project_id}/bible-versions", response_model=BibleVersion, status_code=201, tags=["bible"])
    def generate_bible(project_id: str, repo: ProductionRepository = Depends(production_repository)):
        try:
            result = repo.generate_bible(project_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None: raise HTTPException(status_code=404, detail="project not found")
        return result

    @router.get("/projects/{project_id}/bible", response_model=BibleVersion, tags=["bible"])
    @router.get("/projects/{project_id}/bible-versions/latest", response_model=BibleVersion, tags=["bible"])
    def get_bible(project_id: str, repo: ProductionRepository = Depends(production_repository)):
        result = repo.get_bible(project_id)
        if result is None: raise HTTPException(status_code=404, detail="bible not found")
        return result

    @router.get("/projects/{project_id}/bible-versions/{version}", response_model=BibleVersion, tags=["bible"])
    def get_bible_version(project_id: str, version: int, repo: ProductionRepository = Depends(production_repository)):
        result = repo.get_bible(project_id, version)
        if result is None: raise HTTPException(status_code=404, detail="bible not found")
        return result

    @router.post("/projects/{project_id}/bible/approve", response_model=BibleVersion, tags=["bible"])
    def approve_latest_bible(project_id: str, repo: ProductionRepository = Depends(production_repository)):
        latest = repo.get_bible(project_id)
        if latest is None: raise HTTPException(status_code=404, detail="bible not found")
        result = repo.approve_bible(project_id, int(latest["version"]))
        if result is None: raise HTTPException(status_code=404, detail="bible not found")
        return result

    @router.post("/projects/{project_id}/bible-versions/{version}/approve", response_model=BibleVersion, tags=["bible"])
    def approve_bible(project_id: str, version: int, repo: ProductionRepository = Depends(production_repository)):
        result = repo.approve_bible(project_id, version)
        if result is None: raise HTTPException(status_code=404, detail="bible not found")
        return result

    @router.post("/projects/{project_id}/production-plan", response_model=ScriptStoryboard, status_code=201, tags=["screenplay"])
    @router.post("/projects/{project_id}/screenplay/generate", response_model=ScriptStoryboard, status_code=201, tags=["screenplay"])
    @router.post("/projects/{project_id}/script-storyboard/generate", response_model=ScriptStoryboard, status_code=201, tags=["screenplay"])
    def generate_screenplay(project_id: str, repo: ProductionRepository = Depends(production_repository)):
        try: result = repo.generate_script(project_id)
        except ValueError as exc: raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None: raise HTTPException(status_code=404, detail="project not found")
        return result

    @router.get("/projects/{project_id}/episodes", response_model=list[Episode], tags=["screenplay"])
    def get_episodes(project_id: str, repo: ProductionRepository = Depends(production_repository)):
        result = repo.get_script(project_id)
        if result is None: raise HTTPException(status_code=404, detail="screenplay not found")
        return result["episodes"]

    @router.get("/projects/{project_id}/screenplay", response_model=ScriptStoryboard, tags=["screenplay"])
    def get_screenplay(project_id: str, repo: ProductionRepository = Depends(production_repository)):
        result = repo.get_script(project_id)
        if result is None: raise HTTPException(status_code=404, detail="screenplay not found")
        return result

    @router.get("/projects/{project_id}/shots", response_model=list[Shot], tags=["storyboard"])
    def list_shots(project_id: str, project_repo: ProjectRepository = Depends(repository), repo: ProductionRepository = Depends(production_repository)):
        if project_repo.get(project_id) is None: raise HTTPException(status_code=404, detail="project not found")
        return repo.list_shots(project_id)

    @router.post("/projects/{project_id}/shots/{shot_id}/image", response_model=Task,
                 status_code=status.HTTP_201_CREATED, tags=["media"])
    def create_shot_image_task(project_id: str, shot_id: str, background_tasks: BackgroundTasks,
                               repo: AnalysisRepository = Depends(analysis_repository)):
        return start_shot_media_task("shot_image", project_id, [shot_id], background_tasks, repo)

    @router.post("/projects/{project_id}/media-images", response_model=Task,
                 status_code=status.HTTP_201_CREATED, tags=["media"])
    def create_media_images_task(project_id: str, background_tasks: BackgroundTasks,
                                 payload: dict = Body(default_factory=dict),
                                 repo: AnalysisRepository = Depends(analysis_repository)):
        shot_ids = payload.get("shot_ids")
        if shot_ids is not None and not isinstance(shot_ids, list):
            raise HTTPException(status_code=422, detail="shot_ids must be a list of shot ids")
        return start_shot_media_task("shot_image", project_id, shot_ids, background_tasks, repo)

    @router.post("/projects/{project_id}/speech", response_model=Task,
                 status_code=status.HTTP_201_CREATED, tags=["media"])
    def create_speech_task(project_id: str, background_tasks: BackgroundTasks,
                           payload: dict = Body(default_factory=dict),
                           repo: AnalysisRepository = Depends(analysis_repository)):
        shot_ids = payload.get("shot_ids")
        if shot_ids is not None and not isinstance(shot_ids, list):
            raise HTTPException(status_code=422, detail="shot_ids must be a list of shot ids")
        return start_shot_media_task("speech", project_id, shot_ids, background_tasks, repo)

    @router.post("/projects/{project_id}/shots/{shot_id}/video", response_model=Task,
                 status_code=status.HTTP_201_CREATED, tags=["media"])
    def create_shot_video_task(project_id: str, shot_id: str, background_tasks: BackgroundTasks,
                               repo: AnalysisRepository = Depends(analysis_repository)):
        return start_shot_media_task("shot_video", project_id, [shot_id], background_tasks, repo)

    @router.post("/projects/{project_id}/media-videos", response_model=Task,
                 status_code=status.HTTP_201_CREATED, tags=["media"])
    def create_media_videos_task(project_id: str, background_tasks: BackgroundTasks,
                                 payload: dict = Body(default_factory=dict),
                                 repo: AnalysisRepository = Depends(analysis_repository)):
        shot_ids = payload.get("shot_ids")
        if shot_ids is not None and not isinstance(shot_ids, list):
            raise HTTPException(status_code=422, detail="shot_ids must be a list of shot ids")
        return start_shot_media_task("shot_video", project_id, shot_ids, background_tasks, repo)

    @router.get("/shots/{shot_id}", response_model=Shot, tags=["storyboard"])
    def get_shot(shot_id: str, repo: ProductionRepository = Depends(production_repository)):
        result = repo.get_shot(shot_id)
        if result is None: raise HTTPException(status_code=404, detail="shot not found")
        return result

    @router.patch("/shots/{shot_id}", response_model=Shot, tags=["storyboard"])
    @router.put("/shots/{shot_id}", response_model=Shot, tags=["storyboard"])
    def update_shot(shot_id: str, payload: ShotUpdate, repo: ProductionRepository = Depends(production_repository)):
        try: result = repo.update_shot(shot_id, payload)
        except ValueError as exc: raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None: raise HTTPException(status_code=404, detail="shot not found")
        return result

    @router.post("/projects/{project_id}/shots/approve", response_model=list[Shot], tags=["storyboard"])
    def approve_project_shots(project_id: str, payload: dict = Body(default_factory=dict), repo: ProductionRepository = Depends(production_repository)):
        all_shots = repo.list_shots(project_id)
        if not all_shots: raise HTTPException(status_code=404, detail="shots not found")
        selected = set(payload.get("shot_ids") or [shot["id"] for shot in all_shots])
        return [approved for shot in all_shots if shot["id"] in selected and (approved := repo.approve_shot(str(shot["id"]))) is not None]

    @router.post("/shots/{shot_id}/approve", response_model=Shot, tags=["storyboard"])
    def approve_shot(shot_id: str, repo: ProductionRepository = Depends(production_repository)):
        result = repo.approve_shot(shot_id)
        if result is None: raise HTTPException(status_code=404, detail="shot not found")
        return result

    @router.post("/projects/{project_id}/demo-media", status_code=201, tags=["media"])
    def generate_demo_media(project_id: str, repo: MediaRepository = Depends(media_repository)):
        tasks = AnalysisRepository(application.state.database)
        task = tasks.create_task(project_id, "demo_media", capability="demo_media", estimated_cost=0, provider="demo", model="local-placeholder")
        if task is None: raise HTTPException(status_code=404, detail="project not found")
        claimed = tasks.claim(str(task["id"]))
        try: result = repo.generate_demo_media(project_id, source_task_id=str(task["id"]))
        except ValueError as exc:
            tasks.fail(str(task["id"]), str(exc)); raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None: tasks.fail(str(task["id"]), "project not found"); raise HTTPException(status_code=404, detail="project not found")
        tasks.complete_task(str(task["id"]), project_id, 0)
        return result

    @router.get("/projects/{project_id}/assets", tags=["media"])
    def list_assets(project_id: str, repo: MediaRepository = Depends(media_repository)):
        result = repo.list_assets(project_id)
        if result is None: raise HTTPException(status_code=404, detail="project not found")
        return result

    @router.post("/projects/{project_id}/timeline", status_code=201, tags=["timeline"])
    def create_timeline(project_id: str, repo: MediaRepository = Depends(media_repository)):
        tasks=AnalysisRepository(application.state.database); task=tasks.create_task(project_id,"timeline",capability="timeline",estimated_cost=0,provider="local",model="neutral-v1")
        if task is None: raise HTTPException(status_code=404,detail="project not found")
        tasks.claim(str(task["id"]))
        try: result=repo.create_timeline(project_id)
        except ValueError as exc: tasks.fail(str(task["id"]),str(exc)); raise HTTPException(status_code=409,detail=str(exc)) from exc
        if result is None: tasks.fail(str(task["id"]),"project not found"); raise HTTPException(status_code=404,detail="project not found")
        tasks.complete_task(str(task["id"]),str(result["id"]),0); return result

    @router.get("/projects/{project_id}/timeline", tags=["timeline"])
    def get_timeline(project_id: str, version: int | None = None, repo: MediaRepository = Depends(media_repository)):
        result = repo.get_timeline(project_id, version)
        if result is None: raise HTTPException(status_code=404, detail="timeline not found")
        return result

    @router.post("/projects/{project_id}/exports", status_code=201, tags=["export"])
    def create_export(project_id: str, payload: ExportRequest = Body(default_factory=ExportRequest),
                      repo: MediaRepository = Depends(media_repository)):
        # ``payload.format`` is validated by the schema, so an unknown value is a 422 here and never
        # reaches the repository.
        tasks=AnalysisRepository(application.state.database); task=tasks.create_task(project_id,"export",payload={"timeline_version":payload.timeline_version,"format":payload.format},capability="export",estimated_cost=0,provider="local",model="ffmpeg-or-draft")
        if task is None: raise HTTPException(status_code=404,detail="project not found")
        tasks.claim(str(task["id"]))
        try: result=repo.create_export(project_id,payload.timeline_version,format=payload.format)
        except ValueError as exc: tasks.fail(str(task["id"]),str(exc)); raise HTTPException(status_code=409,detail=str(exc)) from exc
        if result is None: tasks.fail(str(task["id"]),"project not found"); raise HTTPException(status_code=404,detail="project not found")
        if result.get("status") == "failed": tasks.fail(str(task["id"]),str(result.get("error") or "export failed"))
        else: tasks.complete_task(str(task["id"]),str(result["id"]),0)
        return result

    @router.get("/projects/{project_id}/exports", tags=["export"])
    def list_exports(project_id: str, limit: int = 20, repo: MediaRepository = Depends(media_repository)):
        """Recent exports, newest first — so a past (including failed) export can be looked up again."""
        return repo.list_exports(project_id, limit)

    @router.get("/projects/{project_id}/exports/{export_id}", tags=["export"])
    def get_export(project_id: str, export_id: str, repo: MediaRepository = Depends(media_repository)):
        result = repo.get_export(project_id, export_id)
        if result is None: raise HTTPException(status_code=404, detail="export not found")
        return result

    @router.get("/exports/{export_id}", tags=["export"])
    def get_export_by_id(export_id: str, repo: MediaRepository = Depends(media_repository)):
        with application.state.database.connect() as connection:
            row = connection.execute("SELECT project_id FROM export_jobs WHERE id=?", (export_id,)).fetchone()
        if not row: raise HTTPException(status_code=404, detail="export not found")
        return repo.get_export(row["project_id"], export_id)

    @router.get("/projects/{project_id}/artifacts/{artifact_id}/download", tags=["export"])
    def download_artifact(project_id: str, artifact_id: str, repo: MediaRepository = Depends(media_repository)):
        artifact = repo.artifact_path(project_id, artifact_id)
        if artifact is None: raise HTTPException(status_code=404, detail="artifact not found")
        path, mime_type, filename = artifact
        return FileResponse(path, media_type=mime_type, filename=filename)

    @router.get("/artifacts/{artifact_id}/download", tags=["export"])
    def download_artifact_by_id(artifact_id: str, repo: MediaRepository = Depends(media_repository)):
        with application.state.database.connect() as connection:
            row = connection.execute("""SELECT j.project_id FROM export_artifacts a
                JOIN export_jobs j ON j.id=a.export_job_id JOIN projects p ON p.id=j.project_id
                WHERE a.id=? AND p.deleted_at IS NULL""", (artifact_id,)).fetchone()
        if not row: raise HTTPException(status_code=404, detail="artifact not found")
        artifact = repo.artifact_path(str(row["project_id"]), artifact_id)
        if artifact is None: raise HTTPException(status_code=404, detail="artifact not found")
        path, mime_type, filename = artifact
        return FileResponse(path, media_type=mime_type, filename=filename)

    @router.get("/projects/{project_id}/assets/{asset_id}/versions/{version_id}/content", tags=["media"])
    def get_asset_version_content(project_id: str, asset_id: str, version_id: str,
                                  repo: MediaRepository = Depends(media_repository)):
        resolved = repo.asset_version(project_id, asset_id, version_id)
        if resolved is None:
            raise HTTPException(status_code=404, detail="asset content not found")
        path, mime_type, filename, sha256, _ = resolved
        return _asset_content_response(path, mime_type, filename, sha256)

    @router.get("/asset-versions/{version_id}/content", tags=["media"])
    def get_asset_version_content_by_id(version_id: str, repo: MediaRepository = Depends(media_repository)):
        with application.state.database.connect() as connection:
            row = connection.execute("""SELECT a.id asset_id,a.project_id FROM asset_versions av
                JOIN assets a ON a.id=av.asset_id JOIN projects p ON p.id=a.project_id
                WHERE av.id=? AND p.deleted_at IS NULL""", (version_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="asset content not found")
        resolved = repo.asset_version(str(row["project_id"]), str(row["asset_id"]), version_id)
        if resolved is None:
            raise HTTPException(status_code=404, detail="asset content not found")
        path, mime_type, filename, sha256, _ = resolved
        return _asset_content_response(path, mime_type, filename, sha256)

    @router.post("/projects/{project_id}/pipeline", response_model=Task,
                 status_code=status.HTTP_201_CREATED, tags=["pipeline"])
    def create_pipeline_task(project_id: str, background_tasks: BackgroundTasks,
                             payload: dict = Body(default_factory=dict),
                             repo: AnalysisRepository = Depends(analysis_repository)) -> dict[str, object]:
        requested = payload.get("stages")
        if requested is None:
            stages = list(PIPELINE_STAGES)
        elif not isinstance(requested, list) or not all(isinstance(item, str) for item in requested):
            raise HTTPException(status_code=422, detail="stages must be a list of stage names")
        elif not requested:
            raise HTTPException(status_code=422, detail="stages must not be empty")
        else:
            invalid = [item for item in requested if item not in PIPELINE_STAGES]
            if invalid:
                raise HTTPException(status_code=422,
                                    detail=f"unsupported pipeline stages: {', '.join(sorted(set(invalid)))}")
            # Keep the canonical execution order regardless of the request order.
            stages = [item for item in PIPELINE_STAGES if item in set(requested)]
        require_review = payload.get("require_review", True)
        if not isinstance(require_review, bool):
            raise HTTPException(status_code=422, detail="require_review must be a boolean")
        # A pipeline that calls an external text provider must reserve budget up front, otherwise the
        # project hard limit could not block the analysis stage it runs internally.
        settings = repo.get_provider_settings()
        uses_external_text = "analysis" in stages and str(settings["provider"]) != "demo"
        estimated_cost = 1.0 if uses_external_text else 0.0
        task = repo.create_task(
            project_id, "pipeline",
            payload={"stages": stages, "require_review": require_review, "stage_index": 0, "completed_stages": []},
            capability="pipeline", estimated_cost=estimated_cost,
            provider=str(settings["provider"]) if uses_external_text else "local",
            model=str(settings["model"]) if uses_external_text else "pipeline-v1",
        )
        if task is None:
            raise HTTPException(status_code=404, detail="project not found")
        task["events"] = repo.get_task(str(task["id"]))["events"]  # type: ignore[index]
        background_tasks.add_task(TaskExecutor(application.state.database).execute, str(task["id"]))
        return task

    @router.post("/tasks/{task_id}/resume", response_model=Task, tags=["tasks"])
    def resume_task(task_id: str, background_tasks: BackgroundTasks, execute: bool = False,
                    repo: AnalysisRepository = Depends(analysis_repository)) -> dict[str, object]:
        try:
            result = repo.resume(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="task not found")
        # The GUI resumes a review gate with one click, so it asks the backend to continue the run.
        if execute:
            background_tasks.add_task(TaskExecutor(application.state.database).execute, task_id)
        return result

    # --- local storage accounting: the media tree grows forever, so it can at least be seen ---

    @router.get("/system/storage", tags=["system"])
    def system_storage() -> dict[str, object]:
        """Report how much disk every project directory below the media root really uses."""
        return storage_summary(application.state.database)

    @router.post("/system/storage/cleanup", tags=["system"])
    def system_storage_cleanup(payload: dict = Body(default_factory=dict)) -> dict[str, object]:
        """Remove only files that no asset version or export artifact references.

        The body defaults to a dry run, which lists the targets without touching a single file.
        """
        dry_run = payload.get("dry_run", True)
        include_deleted = payload.get("include_deleted_projects", False)
        if not isinstance(dry_run, bool):
            raise HTTPException(status_code=422, detail="dry_run must be a boolean")
        if not isinstance(include_deleted, bool):
            raise HTTPException(status_code=422, detail="include_deleted_projects must be a boolean")
        return cleanup_storage(application.state.database, dry_run=dry_run,
                               include_deleted_projects=include_deleted)

    application.include_router(router)
    return application


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run("backend.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    run()
