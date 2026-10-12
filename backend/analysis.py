"""Persistent generalized task queue, budget ledger, and task executor."""
from __future__ import annotations

import json
import os
import sqlite3
from uuid import uuid4

from backend.database import Database
from backend.providers import ProviderError, build_analyzer
from backend.repository import utc_now
from backend.schemas import CapabilitySettingsUpdate, ProviderSettingsUpdate

DEFAULT_PROVIDER = {"provider": "demo", "endpoint": "https://api.openai.com/v1", "model": "deterministic-v1", "api_key_env": "OPENAI_API_KEY"}
CAPABILITIES = ("text", "image", "speech", "video")
# Only used when the capability table is somehow missing a row; database initialization seeds them all.
CAPABILITY_DEFAULTS: dict[str, dict[str, str]] = {
    "text": dict(DEFAULT_PROVIDER),
    "image": {"provider": "demo", "endpoint": "", "model": "demo-bitmap-v1", "api_key_env": "OPENAI_API_KEY"},
    "speech": {"provider": "demo", "endpoint": "", "model": "demo-tone-v1", "api_key_env": "OPENAI_API_KEY"},
    # 视频的 demo 会用本地 FFmpeg 渲染一个真实可解码的占位片段（明显标注为占位，不是生成结果）；
    # 没有 FFmpeg 时它会明确报错，而不是伪造一个假视频。
    "video": {"provider": "demo", "endpoint": "", "model": "demo-ffmpeg-v1", "api_key_env": "OPENAI_API_KEY"},
}
CAPABILITY_COLUMNS = "capability,provider,endpoint,model,api_key_env,unit_cost,updated_at"
# One task type per shot-level media capability: the map is the single place the two are linked.
SHOT_MEDIA_CAPABILITY = {"shot_image": "image", "speech": "speech", "shot_video": "video"}
TASK_COLUMNS = (
    "id,project_id,task_type,status,result_id,error,attempts,progress,message,capability,"
    "estimated_cost,actual_cost,provider,model,created_at,updated_at,started_at,finished_at"
)


class BudgetExceeded(ValueError):
    pass


def capability_configured(provider: str, api_key_env: str) -> bool:
    """A capability is configured when it is local demo, or its key variable is present."""
    return provider == "demo" or bool(os.getenv(api_key_env))


class AnalysisRepository:
    def __init__(self, database: Database) -> None: self.database = database

    # --- capability settings (single source of truth for every capability) --------------

    @staticmethod
    def _capability_result(row) -> dict[str, object]:
        result = dict(row)
        result["unit_cost"] = float(result.get("unit_cost") or 0)
        result["configured"] = capability_configured(str(result["provider"]), str(result["api_key_env"]))
        return result

    def _capability_row(self, c: sqlite3.Connection, capability: str) -> dict[str, object] | None:
        row = c.execute(f"SELECT {CAPABILITY_COLUMNS} FROM capability_settings WHERE capability=?", (capability,)).fetchone()
        if row is not None:
            return dict(row)
        default = CAPABILITY_DEFAULTS.get(capability)
        if default is None:
            return None
        now = utc_now()
        c.execute("INSERT INTO capability_settings (capability,provider,endpoint,model,api_key_env,unit_cost,options_json,updated_at)"
                  " VALUES (?,?,?,?,?,0,'{}',?) ON CONFLICT(capability) DO NOTHING",
                  (capability, default["provider"], default["endpoint"], default["model"], default["api_key_env"], now))
        row = c.execute(f"SELECT {CAPABILITY_COLUMNS} FROM capability_settings WHERE capability=?", (capability,)).fetchone()
        return dict(row) if row is not None else None

    def list_capability_settings(self) -> list[dict[str, object]]:
        settings: list[dict[str, object]] = []
        with self.database.connect() as c:
            for capability in CAPABILITIES:
                row = self._capability_row(c, capability)
                if row is not None:
                    settings.append(self._capability_result(row))
        return settings

    def get_capability_settings(self, capability: str) -> dict[str, object] | None:
        if capability not in CAPABILITIES:
            return None
        with self.database.connect() as c:
            row = self._capability_row(c, capability)
        return self._capability_result(row) if row is not None else None

    def update_capability_settings(self, capability: str, payload: CapabilitySettingsUpdate) -> dict[str, object] | None:
        if capability not in CAPABILITIES:
            return None
        values, now = payload.model_dump(), utc_now()
        provider = str(values["provider"]).strip()
        endpoint = str(values["endpoint"]).strip()
        model = str(values["model"]).strip()
        api_key_env = str(values["api_key_env"]).strip()
        with self.database.connect() as c:
            self._capability_row(c, capability)
            c.execute("""UPDATE capability_settings SET provider=?,endpoint=?,model=?,api_key_env=?,unit_cost=?,updated_at=?
                WHERE capability=?""",
                (provider, endpoint, model, api_key_env, max(0.0, float(values["unit_cost"])), now, capability))
            if capability == "text":
                self._mirror_legacy_provider_row(c, provider, endpoint, model, api_key_env, now)
        return self.get_capability_settings(capability)

    @staticmethod
    def _mirror_legacy_provider_row(c: sqlite3.Connection, provider: str, endpoint: str,
                                    model: str, api_key_env: str, now: str) -> None:
        """Keep the single legacy ``provider_settings`` row aligned with the text capability.

        ``capability_settings`` is the source of truth; the old table survives only so existing
        databases, diagnostics, and the "no secret is stored" contract keep behaving as before.
        """
        c.execute("""INSERT INTO provider_settings (id,provider,endpoint,model,api_key_env,updated_at)
            VALUES (1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET provider=excluded.provider,
            endpoint=excluded.endpoint,model=excluded.model,api_key_env=excluded.api_key_env,
            updated_at=excluded.updated_at""", (provider, endpoint, model, api_key_env, now))

    def _update_text_provider(self, provider: str, endpoint: str, model: str, api_key_env: str) -> None:
        """Rewrite only the routed fields of the text row so its unit_cost survives."""
        now = utc_now()
        with self.database.connect() as c:
            self._capability_row(c, "text")
            c.execute("UPDATE capability_settings SET provider=?,endpoint=?,model=?,api_key_env=?,updated_at=? WHERE capability='text'",
                      (provider, endpoint, model, api_key_env, now))
            self._mirror_legacy_provider_row(c, provider, endpoint, model, api_key_env, now)

    # text settings ARE the capability_settings row for capability='text': one set of data, two names.

    def get_provider_settings(self) -> dict[str, object]:
        row = self.get_capability_settings("text")
        if row is None:  # pragma: no cover - the table is always seeded for 'text'
            return {**DEFAULT_PROVIDER, "unit_cost": 0.0, "configured": True, "updated_at": utc_now()}
        return row

    def update_provider_settings(self, payload: ProviderSettingsUpdate) -> dict[str, object]:
        values = payload.model_dump()
        self._update_text_provider(str(values["provider"]), str(values["endpoint"]),
                                   str(values["model"]), str(values["api_key_env"]))
        return self.get_provider_settings()

    @staticmethod
    def _event(c: sqlite3.Connection, task_id: str, kind: str, message: str, now: str) -> None:
        c.execute("INSERT INTO task_events (task_id,event_type,message,created_at) VALUES (?,?,?,?)", (task_id,kind,message,now))

    def create_task(self, project_id: str, task_type: str, *, payload: dict | None = None,
                    capability: str = "", estimated_cost: float = 0, provider: str | None = None,
                    model: str | None = None) -> dict[str, object] | None:
        task_id, now = f"task_{uuid4().hex}", utc_now(); estimated_cost = max(0.0, float(estimated_cost))
        with self.database.connect() as c:
            project = c.execute("SELECT id FROM projects WHERE id=? AND deleted_at IS NULL", (project_id,)).fetchone()
            if not project: return None
            c.execute("""INSERT INTO tasks (id,project_id,task_type,status,payload_json,attempts,progress,message,capability,estimated_cost,provider,model,created_at,updated_at)
                VALUES (?,?,?,'queued',?,0,0,'Queued',?,?,?,?,?,?)""",
                (task_id,project_id,task_type,json.dumps(payload or {},ensure_ascii=False),capability or task_type,estimated_cost,provider,model,now,now))
            self._event(c,task_id,"created",f"{task_type} task queued",now)
        return self.get_task(task_id)

    def create_analysis_task(self, project_id: str) -> dict[str, object] | None:
        settings = self.get_provider_settings()
        # Demo is explicitly zero cost; external text execution gets a conservative reservation.
        estimate = 0.0 if settings["provider"] == "demo" else 1.0
        return self.create_task(project_id,"story_analysis",capability="story_analysis",estimated_cost=estimate,
                                provider=str(settings["provider"]),model=str(settings["model"]))

    def resolve_project_shots(self, project_id: str, shot_ids: list[str] | None = None) -> list[str] | None:
        """Validate requested shots against the project's latest episode version.

        Returns every shot of the newest version when ``shot_ids`` is None, ``[]`` when none of
        the requested ids are usable, and ``None`` when the project itself does not exist.
        """
        with self.database.connect() as c:
            project = c.execute("SELECT id FROM projects WHERE id=? AND deleted_at IS NULL", (project_id,)).fetchone()
            if not project:
                return None
            rows = c.execute("""SELECT sh.id FROM shots sh JOIN scenes sc ON sc.id=sh.scene_id
                JOIN episodes e ON e.id=sc.episode_id
                WHERE e.project_id=? AND e.version=(SELECT MAX(version) FROM episodes WHERE project_id=?)
                ORDER BY e.position,sc.position,sh.position""", (project_id, project_id)).fetchall()
        available = [str(row["id"]) for row in rows]
        if shot_ids is None:
            return available
        requested = [str(item) for item in shot_ids]
        if len(set(requested)) != len(requested):
            return []
        # Duplicates or unknown ids are rejected rather than silently trimmed.
        return [shot_id for shot_id in requested if shot_id in set(available)] if all(
            shot_id in set(available) for shot_id in requested) else []

    def create_shot_media_task(self, project_id: str, task_type: str,
                               shot_ids: list[str] | None = None) -> tuple[dict[str, object] | None, str | None]:
        """Queue one ``shot_image``/``speech``/``shot_video`` task for the requested shots.

        Returns ``(task, None)`` on success and ``(None, reason)`` when the request is invalid.
        """
        if task_type not in SHOT_MEDIA_CAPABILITY:
            return None, f"unsupported shot media task type: {task_type}"
        capability = SHOT_MEDIA_CAPABILITY[task_type]
        resolved = self.resolve_project_shots(project_id, shot_ids)
        if resolved is None:
            return None, "project not found"
        if not resolved:
            return None, "no shots to process"
        settings = self.get_capability_settings(capability)
        if settings is None:  # pragma: no cover - the table is always seeded
            return None, f"{capability} capability settings not found"
        unit_cost = float(settings["unit_cost"] or 0)
        task = self.create_task(
            project_id, task_type, payload={"shot_ids": resolved}, capability=capability,
            estimated_cost=unit_cost * len(resolved), provider=str(settings["provider"]), model=str(settings["model"]))
        return task, None

    def get_task(self, task_id: str) -> dict[str, object] | None:
        with self.database.connect() as c:
            row = c.execute(f"SELECT {TASK_COLUMNS} FROM tasks WHERE id=?",(task_id,)).fetchone()
            if not row: return None
            result=dict(row); events=c.execute("SELECT id,task_id,event_type,message,created_at FROM task_events WHERE task_id=? ORDER BY id",(task_id,)).fetchall()
        result["events"]=[dict(e) for e in events]; return result

    def list_tasks(self, project_id: str) -> list[dict[str, object]] | None:
        with self.database.connect() as c:
            if not c.execute("SELECT 1 FROM projects WHERE id=? AND deleted_at IS NULL",(project_id,)).fetchone(): return None
            ids=[str(r["id"]) for r in c.execute("SELECT id FROM tasks WHERE project_id=? ORDER BY created_at DESC",(project_id,)).fetchall()]
        return [task for task_id in ids if (task := self.get_task(task_id)) is not None]

    def _budget(self, c: sqlite3.Connection, project_id: str) -> tuple[float,float,float]:
        limit=float(c.execute("SELECT budget_limit FROM projects WHERE id=?",(project_id,)).fetchone()[0])
        row=c.execute("""SELECT
          COALESCE(SUM(CASE WHEN entry_type='settle' THEN amount WHEN entry_type='refund' THEN -amount ELSE 0 END),0),
          COALESCE(SUM(CASE WHEN entry_type='reserve' THEN amount WHEN entry_type IN ('release','settle') THEN -amount ELSE 0 END),0)
          FROM cost_ledger WHERE project_id=?""",(project_id,)).fetchone()
        return limit,max(0,float(row[0])),max(0,float(row[1]))

    def claim(self, task_id: str | None = None) -> dict[str, object] | None:
        now=utc_now()
        with self.database.connect() as c:
            if task_id is None:
                row=c.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
                if not row:return None
                task_id=str(row["id"])
            row=c.execute("SELECT project_id,estimated_cost,status FROM tasks WHERE id=?",(task_id,)).fetchone()
            if not row or row["status"] != "queued": return None
            estimate=float(row["estimated_cost"]); limit,settled,reserved=self._budget(c,str(row["project_id"]))
            if limit > 0 and settled+reserved+estimate > limit + 1e-9:
                message=f"budget limit exceeded: {settled+reserved+estimate:.2f} > {limit:.2f}"
                c.execute("UPDATE tasks SET status='failed',error=?,message=?,updated_at=?,finished_at=? WHERE id=?",(message,"Budget rejected",now,now,task_id))
                self._event(c,task_id,"budget_rejected",message,now); return None
            if estimate:
                c.execute("INSERT INTO cost_ledger (project_id,task_id,entry_type,amount,provider,model,note,created_at) SELECT project_id,id,'reserve',estimated_cost,provider,model,'pre-call reservation',? FROM tasks WHERE id=?",(now,task_id))
            cur=c.execute("UPDATE tasks SET status='running',attempts=attempts+1,started_at=?,updated_at=?,error=NULL,progress=0.05,message='Running' WHERE id=? AND status='queued'",(now,now,task_id))
            if not cur.rowcount:return None
            self._event(c,task_id,"started","Task execution started",now)
            task=c.execute(f"SELECT {TASK_COLUMNS} FROM tasks WHERE id=?",(task_id,)).fetchone()
        return dict(task)

    def progress(self, task_id: str, value: float, message: str) -> None:
        now=utc_now()
        with self.database.connect() as c:
            c.execute("UPDATE tasks SET progress=?,message=?,updated_at=? WHERE id=? AND status='running'",(max(0,min(1,value)),message,now,task_id))
            self._event(c,task_id,"progress",message,now)

    def complete_task(self, task_id: str, result_id: str | None = None, actual_cost: float = 0) -> None:
        now=utc_now(); actual=max(0,float(actual_cost))
        with self.database.connect() as c:
            row=c.execute("SELECT project_id,estimated_cost,provider,model,status FROM tasks WHERE id=?",(task_id,)).fetchone()
            if not row or row["status"] != "running": return
            estimate=float(row["estimated_cost"])
            if actual: c.execute("INSERT INTO cost_ledger (project_id,task_id,entry_type,amount,provider,model,note,created_at) VALUES (?,?,'settle',?,?,?,'actual cost',?)",(row["project_id"],task_id,actual,row["provider"],row["model"],now))
            if estimate > actual: c.execute("INSERT INTO cost_ledger (project_id,task_id,entry_type,amount,provider,model,note,created_at) VALUES (?,?,'release',?,?,?,'unused reservation',?)",(row["project_id"],task_id,estimate-actual,row["provider"],row["model"],now))
            c.execute("UPDATE tasks SET status='completed',result_id=?,actual_cost=?,progress=1,message='Completed',updated_at=?,finished_at=? WHERE id=?",(result_id,actual,now,now,task_id))
            self._event(c,task_id,"completed","Task completed",now)

    def fail(self, task_id: str, message: str) -> None:
        now=utc_now(); safe=message[:1000]
        with self.database.connect() as c:
            row=c.execute("SELECT project_id,estimated_cost,status,provider,model FROM tasks WHERE id=?",(task_id,)).fetchone()
            if row and row["status"] == "running" and float(row["estimated_cost"]):
                c.execute("INSERT INTO cost_ledger (project_id,task_id,entry_type,amount,provider,model,note,created_at) VALUES (?,?,'release',?,?,?,'task failed',?)",(row["project_id"],task_id,row["estimated_cost"],row["provider"],row["model"],now))
            c.execute("UPDATE tasks SET status='failed',error=?,message='Failed',updated_at=?,finished_at=? WHERE id=?",(safe,now,now,task_id)); self._event(c,task_id,"failed",safe,now)

    def cancel(self, task_id: str) -> dict[str, object] | None:
        now=utc_now()
        with self.database.connect() as c:
            row=c.execute("SELECT project_id,estimated_cost,status,provider,model FROM tasks WHERE id=?",(task_id,)).fetchone()
            if not row:return None
            if row["status"] in ("completed","failed","canceled"): return self.get_task(task_id)
            if row["status"] == "running" and float(row["estimated_cost"]):
                c.execute("INSERT INTO cost_ledger (project_id,task_id,entry_type,amount,provider,model,note,created_at) VALUES (?,?,'release',?,?,?,'task canceled',?)",(row["project_id"],task_id,row["estimated_cost"],row["provider"],row["model"],now))
            c.execute("UPDATE tasks SET status='canceled',message='Canceled',updated_at=?,finished_at=? WHERE id=?",(now,now,task_id)); self._event(c,task_id,"canceled","Task canceled",now)
        return self.get_task(task_id)

    def retry(self, task_id: str) -> dict[str, object] | None:
        now=utc_now()
        with self.database.connect() as c:
            row=c.execute("SELECT status FROM tasks WHERE id=?",(task_id,)).fetchone()
            if not row:return None
            if row["status"] not in ("failed","canceled"): raise ValueError("only failed or canceled tasks can be retried")
            c.execute("UPDATE tasks SET status='queued',error=NULL,progress=0,message='Queued for retry',started_at=NULL,finished_at=NULL,updated_at=? WHERE id=?",(now,task_id)); self._event(c,task_id,"retried","Task queued for retry",now)
        return self.get_task(task_id)

    def pause(self, task_id: str, stage_index: int, message: str) -> dict[str, object] | None:
        """Park a running task at a review gate without losing its cost reservation history."""
        now=utc_now()
        with self.database.connect() as c:
            row=c.execute("SELECT project_id,estimated_cost,status,provider,model,payload_json FROM tasks WHERE id=?",(task_id,)).fetchone()
            if not row:return None
            if row["status"] != "running": return self.get_task(task_id)
            estimate=float(row["estimated_cost"])
            # The reservation is released, but estimated_cost is kept so resuming reserves it again exactly once.
            if estimate:
                c.execute("INSERT INTO cost_ledger (project_id,task_id,entry_type,amount,provider,model,note,created_at) VALUES (?,?,'release',?,?,?,'pipeline paused',?)",
                          (row["project_id"],task_id,estimate,row["provider"],row["model"],now))
            try: payload=json.loads(str(row["payload_json"] or "{}"))
            except ValueError: payload={}
            if not isinstance(payload,dict): payload={}
            payload["stage_index"]=int(stage_index)
            c.execute("UPDATE tasks SET status='paused',message=?,payload_json=?,finished_at=NULL,updated_at=? WHERE id=?",
                      (message[:1000],json.dumps(payload,ensure_ascii=False),now,task_id))
            self._event(c,task_id,"paused",message[:1000],now)
        return self.get_task(task_id)

    def resume(self, task_id: str) -> dict[str, object] | None:
        now=utc_now()
        with self.database.connect() as c:
            row=c.execute("SELECT status FROM tasks WHERE id=?",(task_id,)).fetchone()
            if not row:return None
            if row["status"] != "paused": raise ValueError("only paused tasks can be resumed")
            c.execute("UPDATE tasks SET status='queued',message='已恢复排队',started_at=NULL,finished_at=NULL,updated_at=? WHERE id=?",(now,task_id))
            self._event(c,task_id,"resumed","已恢复排队",now)
        return self.get_task(task_id)

    def recover_running(self) -> int:
        now=utc_now()
        with self.database.connect() as c:
            rows=c.execute("SELECT id,project_id,estimated_cost,provider,model FROM tasks WHERE status='running'").fetchall()
            for row in rows:
                if float(row["estimated_cost"]): c.execute("INSERT INTO cost_ledger (project_id,task_id,entry_type,amount,provider,model,note,created_at) VALUES (?,?,'release',?,?,?,'startup recovery',?)",(row["project_id"],row["id"],row["estimated_cost"],row["provider"],row["model"],now))
                c.execute("UPDATE tasks SET status='queued',message='Recovered after interrupted shutdown',started_at=NULL,updated_at=? WHERE id=?",(now,row["id"])); self._event(c,str(row["id"]),"recovered","Interrupted running task returned to queue",now)
        return len(rows)

    def complete_analysis(self, task: dict[str, object], provider: str, model: str, result_json: str) -> str:
        analysis_id,now=f"analysis_{uuid4().hex}",utc_now()
        with self.database.connect() as c:
            c.execute("INSERT INTO story_analysis (id,project_id,task_id,provider,model,result_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",(analysis_id,task["project_id"],task["id"],provider,model,result_json,now,now))
            c.execute("UPDATE workflow_stages SET status='completed',updated_at=? WHERE project_id=? AND stage_key='story_analysis'",(now,task["project_id"]))
        self.complete_task(str(task["id"]),analysis_id,0); return analysis_id

    def latest_analysis(self, project_id: str) -> dict[str, object] | None:
        with self.database.connect() as c: row=c.execute("SELECT id,project_id,task_id,provider,model,result_json,created_at,updated_at FROM story_analysis WHERE project_id=? ORDER BY created_at DESC LIMIT 1",(project_id,)).fetchone()
        if not row:return None
        result=dict(row); result["result"]=json.loads(str(result.pop("result_json"))); return result


def _text(value: object) -> str:
    return "" if value is None else " ".join(str(value).split())


def _label(value: object, fallback: str) -> str:
    text = _text(value)
    return text or fallback


def shot_image_prompt(project: dict | None, scene: dict | None, shot: dict | None) -> str:
    """Assemble one image prompt from the storyboard slice that produced the shot.

    Kept outside the pipeline and free of side effects so it can be unit-tested directly.
    This is the only place the image prompt is composed.
    """
    project, scene, shot = project or {}, scene or {}, shot or {}
    style = _label(project.get("visual_style"), "未指定视觉风格")
    scene_title = _label(scene.get("title"), "未命名场景")
    scene_setting = _label(scene.get("setting"), "待明确环境")
    action = _label(shot.get("action") or shot.get("narration_dialogue"), "呈现故事动作")
    shot_size = _label(shot.get("shot_size"), "medium")
    # The stored storyboard keeps a single camera description; split angle/motion are honoured too.
    angle = _label(shot.get("camera_angle") or shot.get("camera"), "平视")
    motion = _label(shot.get("camera_motion"), "固定机位")
    narration = _text(shot.get("narration_dialogue"))
    parts = [
        f"项目视觉风格：{style}",
        f"场景：{scene_title}（{scene_setting}）",
        f"镜头景别：{shot_size}",
        f"机位角度：{angle}",
        f"运镜方式：{motion}",
        f"画面内容：{action}",
    ]
    if narration:
        parts.append(f"画外音/对白：{narration}")
    return "；".join(parts) + "。单个 16:9 电影感分镜画面。"


def shot_speech_text(shot: dict | None) -> str:
    """Speech input for one shot: its narration/dialogue, falling back to the action."""
    shot = shot or {}
    return _text(shot.get("narration_dialogue")) or _text(shot.get("action"))


def shot_video_prompt(project: dict | None, scene: dict | None, shot: dict | None) -> str:
    """Assemble the motion brief for one image-to-video call.

    The still frame carries the look of the shot, so the prompt only has to describe how it
    moves. Like :func:`shot_image_prompt` it is pure and lives here so it can be asserted alone.
    """
    project, scene, shot = project or {}, scene or {}, shot or {}
    style = _label(project.get("visual_style"), "未指定视觉风格")
    scene_title = _label(scene.get("title"), "未命名场景")
    action = _label(shot.get("action"), "让画面自然呼吸")
    angle = _label(shot.get("camera_angle") or shot.get("camera"), "平视")
    motion = _label(shot.get("camera_motion"), "固定机位")
    narration = _text(shot.get("narration_dialogue"))
    parts = [
        f"以这张画面为第一帧继续拍摄：{action}",
        f"场景：{scene_title}",
        f"视觉风格：{style}",
        f"机位角度：{angle}",
        f"运镜方式：{motion}",
    ]
    if narration:
        parts.append(f"同期氛围/对白：{narration}")
    return "；".join(parts) + "。保持主体一致，动作连贯，不要切换场景。"


class TaskExecutor:
    def __init__(self,database:Database)->None:self.repo=AnalysisRepository(database);self.database=database

    def execute(self,task_id:str|None=None)->dict[str,object]|None:
        task=self.repo.claim(task_id)
        if task is None:return self.repo.get_task(task_id) if task_id else None
        try:
            task_type=str(task["task_type"]); project_id=str(task["project_id"])
            if task_type == "pipeline":
                # The pipeline owns completion/failure for every stage it runs, so it must not fall
                # through to the generic complete_task branch below.
                from backend.pipeline import PipelineRunner
                PipelineRunner(self.database).run(str(task["id"]))
                return self.repo.get_task(str(task["id"]))
            if task_type in ("shot_image","speech"):
                # Shot media owns its own completion/failure and must never fall back to demo output.
                self._execute_shot_media(task, task_type, project_id)
                return self.repo.get_task(str(task["id"]))
            if task_type == "shot_video":
                # Video is asynchronous (submit, then poll) so it has its own executor; it also owns
                # its own completion/failure and never registers a half-finished clip.
                self._execute_shot_video(task, project_id)
                return self.repo.get_task(str(task["id"]))
            self.repo.progress(str(task["id"]),.2,"Preparing task")
            if task_type == "story_analysis":
                with self.database.connect() as c: project=c.execute("SELECT story FROM projects WHERE id=?",(project_id,)).fetchone()
                if not project:raise ProviderError("project not found")
                settings=self.repo.get_provider_settings(); analyzer=build_analyzer(settings)
                self.repo.progress(str(task["id"]),.5,"Analyzing story")
                result=analyzer.analyze(str(project["story"])); self.repo.complete_analysis(task,analyzer.name,analyzer.model,result.model_dump_json())
            elif task_type in ("demo_media","timeline","export"):
                from backend.media import MediaRepository
                media=MediaRepository(self.database); self.repo.progress(str(task["id"]),.4,f"Running {task_type}")
                if task_type=="demo_media": result=media.generate_demo_media(project_id,source_task_id=str(task["id"]))
                elif task_type=="timeline": result=media.create_timeline(project_id)
                else:
                    with self.database.connect() as c: payload=json.loads(c.execute("SELECT payload_json FROM tasks WHERE id=?",(task["id"],)).fetchone()[0])
                    result=media.create_export(project_id,payload.get("timeline_version"))
                if result is None:raise ValueError("project not found")
                result_id=str(result.get("id") or result.get("project_id")); self.repo.complete_task(str(task["id"]),result_id,0)
            else: raise ValueError(f"unsupported task type: {task_type}")
        except Exception as exc:
            message=str(exc) if isinstance(exc,(ProviderError,ValueError)) else "task execution failed"; self.repo.fail(str(task["id"]),message)
        return self.repo.get_task(str(task["id"]))

    def _execute_shot_media(self, task: dict[str, object], task_type: str, project_id: str) -> None:
        """Generate one asset per shot for ``shot_image``/``speech``.

        A provider failure always fails the task: demo output is never silently substituted,
        and nothing is registered before the provider call has actually succeeded.
        """
        from backend.media import MediaRepository
        from backend.providers import (
            ProviderError, build_image_provider, build_speech_provider, scrub_secret, suffix_for_mime,
        )

        task_id = str(task["id"])
        media = MediaRepository(self.database)
        with self.database.connect() as c:
            payload_row = c.execute("SELECT payload_json FROM tasks WHERE id=?", (task_id,)).fetchone()
        try:
            payload = json.loads(str(payload_row["payload_json"])) if payload_row else {}
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        shot_ids = [str(item) for item in (payload.get("shot_ids") or [])]
        if not shot_ids:
            raise ValueError("payload 缺少 shot_ids")
        capability = "image" if task_type == "shot_image" else "speech"
        settings = self.repo.get_capability_settings(capability)
        if settings is None:  # pragma: no cover - the table is always seeded
            raise ValueError(f"{capability} capability settings not found")
        provider_name, model = str(settings["provider"]), str(settings["model"])
        api_key_env = str(settings["api_key_env"] or "")
        # Read directly instead of through the provider so failures can be scrubbed here too.
        api_key = os.getenv(api_key_env) if api_key_env else None
        is_demo = provider_name == "demo"
        builder = build_image_provider if task_type == "shot_image" else build_speech_provider
        provider = builder(settings)

        with self.database.connect() as c:
            rows = {str(row["id"]): dict(row) for row in self._shot_rows(c, project_id, shot_ids)}
            project = c.execute("SELECT visual_style,creative_intent,audience FROM projects WHERE id=?", (project_id,)).fetchone()
        project_info = dict(project) if project else {}
        total, result_ids = len(shot_ids), []
        for index, shot_id in enumerate(shot_ids):
            # Re-read status before each shot so a cancel stops the run immediately.
            if not self._still_running(task_id):
                return
            row = rows.get(shot_id)
            if row is None:
                raise ValueError(f"shot {shot_id} not found in project")
            scene = {"title": row.get("scene_title"), "setting": row.get("scene_setting"),
                     "purpose": row.get("scene_purpose")}
            shot = {"id": shot_id, "action": row.get("action"), "shot_size": row.get("shot_size"),
                    "camera": row.get("camera"), "narration_dialogue": row.get("narration_dialogue"),
                    "position": row.get("position"), "duration_sec": row.get("duration_sec"),
                    "render_strategy": row.get("render_strategy")}
            position = index + 1
            self.repo.progress(task_id, position / (total + 1),
                               f"正在生成第 {position}/{total} 个镜头的{'画面' if task_type == 'shot_image' else '语音'}")
            if task_type == "shot_image":
                prompt = shot_image_prompt(project_info, scene, shot)
                try:
                    data, mime_type = provider.generate(prompt, size="1280x720")
                except ProviderError as exc:
                    raise ProviderError(scrub_secret(f"镜头 {shot_id} 图像生成失败：{exc}", api_key)) from exc
                # The provider call may have taken a while: a cancel that arrived meanwhile must
                # discard the result instead of registering an asset for a task that is not running.
                if not self._still_running(task_id):
                    return
                with self.database.connect() as c:
                    asset = media._register_bytes(
                        c, project_id, "image", "demo" if is_demo else "import", data,
                        suffix_for_mime(mime_type), mime_type, shot_id, source_task_id=task_id, demo=is_demo,
                        metadata={
                            "provider": provider_name, "model": model, "prompt": prompt,
                            "shot_id": shot_id, "demo": is_demo,
                            # A repeat generation deliberately creates a new asset instead of a new version.
                            "notice": "本地占位画面；不是 AI 生成内容。" if is_demo
                                      else "由外部图像服务生成；每次生成都会新建 asset。",
                        })
            else:
                text = shot_speech_text(shot)
                if not text:
                    raise ValueError(f"镜头 {shot_id} 缺少可用于语音的文本")
                voice = os.getenv("VMAKER_SPEECH_VOICE", "")
                try:
                    data, mime_type = provider.synthesize(text, voice=voice)
                except ProviderError as exc:
                    raise ProviderError(scrub_secret(f"镜头 {shot_id} 语音生成失败：{exc}", api_key)) from exc
                if not self._still_running(task_id):
                    return
                with self.database.connect() as c:
                    asset = media._register_bytes(
                        c, project_id, "audio", "demo" if is_demo else "import", data,
                        suffix_for_mime(mime_type), mime_type, shot_id, source_task_id=task_id, demo=is_demo,
                        metadata={
                            "provider": provider_name, "model": model, "voice": voice,
                            "text": text, "shot_id": shot_id, "demo": is_demo,
                            "notice": "本地占位音频（提示音+静音）；不是 TTS 或 AI 语音。" if is_demo
                                      else "由外部语音服务合成；每次生成都会新建 asset。",
                        })
            result_ids.append(str(asset["id"]))
        with self.database.connect() as c:
            c.execute("UPDATE workflow_stages SET status='completed',updated_at=? WHERE project_id=? AND stage_key=?",
                      (utc_now(), project_id, "assets" if task_type == "shot_image" else "audio"))
        self.repo.complete_task(task_id, result_ids[-1] if result_ids else None, 0)

    def _still_running(self, task_id: str) -> bool:
        """True while the task is still running; a cancel or failure stops the loop."""
        current = self.repo.get_task(task_id)
        return current is not None and str(current["status"]) == "running"

    # --- image-to-video: submit one job per shot, then poll it until it settles ----------

    def _execute_shot_video(self, task: dict[str, object], project_id: str) -> None:
        """Turn each shot's newest still frame into a clip through an asynchronous provider.

        Nothing is ever registered before the provider reports success and the bytes have been
        downloaded, so a failed, timed-out, or canceled run leaves no half-finished video behind.
        """
        from backend.config import get_settings
        from backend.media import MediaRepository
        from backend.providers import (
            ProviderError, VideoRequest, build_video_provider, scrub_secret, suffix_for_mime,
        )

        task_id = str(task["id"])
        media = MediaRepository(self.database)
        payload = self._task_payload(task_id)
        shot_ids = [str(item) for item in (payload.get("shot_ids") or [])]
        if not shot_ids:
            raise ValueError("payload 缺少 shot_ids")
        settings = self.repo.get_capability_settings("video")
        if settings is None:  # pragma: no cover - the table is always seeded
            raise ValueError("video capability settings not found")
        provider_name, model = str(settings["provider"]), str(settings["model"])
        api_key_env = str(settings["api_key_env"] or "")
        # Read directly instead of through the provider so failures can be scrubbed here too.
        api_key = os.getenv(api_key_env) if api_key_env else None
        provider = build_video_provider(settings)
        runtime = get_settings()

        with self.database.connect() as c:
            rows = {str(row["id"]): dict(row) for row in self._shot_rows(c, project_id, shot_ids)}
            project = c.execute("SELECT visual_style,creative_intent,aspect_ratio,audience "
                                "FROM projects WHERE id=?", (project_id,)).fetchone()
        project_info = dict(project) if project else {}
        aspect_ratio = str(project_info.get("aspect_ratio") or "16:9")
        total, result_ids = len(shot_ids), []
        for index, shot_id in enumerate(shot_ids):
            # Re-read status before each shot so a cancel stops the run immediately.
            if not self._still_running(task_id):
                return
            row = rows.get(shot_id)
            if row is None:
                raise ValueError(f"shot {shot_id} not found in project")
            position = index + 1
            self.repo.progress(task_id, position / (total + 1), f"第 {position}/{total} 个镜头：准备生成视频")
            # A shot with no still frame has nothing to animate, so no job is ever submitted.
            source = self._latest_shot_image(media, project_id, shot_id)
            if source is None:
                raise ValueError(f"镜头 {shot_id} 还没有画面，请先生成画面再做视频")
            image_bytes, image_mime, image_asset_id = source
            scene = {"title": row.get("scene_title"), "setting": row.get("scene_setting"),
                     "purpose": row.get("scene_purpose")}
            shot = {"id": shot_id, "action": row.get("action"), "shot_size": row.get("shot_size"),
                    "camera": row.get("camera"), "narration_dialogue": row.get("narration_dialogue"),
                    "position": row.get("position"), "duration_sec": row.get("duration_sec"),
                    "render_strategy": row.get("render_strategy")}
            prompt = shot_video_prompt(project_info, scene, shot)
            duration_sec = max(1, int(row.get("duration_sec") or 5))
            request = VideoRequest(prompt=prompt, image_bytes=image_bytes, image_mime=image_mime,
                                   duration_sec=duration_sec, aspect_ratio=aspect_ratio,
                                   camera=str(row.get("camera") or "") or None)
            try:
                job = provider.submit(request)
            except ProviderError as exc:
                raise ProviderError(scrub_secret(
                    f"镜头 {shot_id} 视频提交失败：{exc}", api_key)) from exc
            status = self._await_video(task_id, provider, job, position, total, runtime)
            if status is None:
                # The task stopped being "running" while polling: register nothing at all.
                return
            if status.kind == "failed":
                raise ProviderError(scrub_secret(
                    f"镜头 {shot_id} 视频生成失败：{status.message or '服务未给出原因'}", api_key))
            if status.kind != "succeeded":
                raise ProviderError(scrub_secret(
                    f"镜头 {shot_id} 视频生成超时：等待超过 {runtime.video_timeout_seconds:g} 秒仍未完成",
                    api_key))
            try:
                data, mime_type = provider.fetch(job, status)
            except ProviderError as exc:
                raise ProviderError(scrub_secret(
                    f"镜头 {shot_id} 视频下载失败：{exc}", api_key)) from exc
            # The download may have taken a while: a cancel that arrived meanwhile must discard
            # the clip instead of registering an asset for a task that is not running.
            if not self._still_running(task_id):
                return
            with self.database.connect() as c:
                # demo 产出的是**本地占位片段**，不是外部服务结果。以前这里硬编码 demo=False，
                # 会让占位内容在界面上被当成 AI 生成素材——必须按 provider 如实标记。
                is_demo = str(provider_name) == "demo"
                asset = media._register_bytes(
                    c, project_id, "video", "import", data,
                    suffix_for_mime(mime_type), mime_type, shot_id, source_task_id=task_id, demo=is_demo,
                    metadata={
                        "provider": provider_name, "model": model, "prompt": prompt,
                        "duration_sec": duration_sec, "aspect_ratio": aspect_ratio,
                        "source_image_asset_id": image_asset_id, "shot_id": shot_id,
                        "notice": ("由本地 FFmpeg 渲染的占位片段：不是 AI 生成，也不是任何生成服务的产物。"
                                   if is_demo else "由外部图生视频服务生成；每次生成都会新建 asset。"),
                    })
            result_ids.append(str(asset["id"]))
        with self.database.connect() as c:
            c.execute("UPDATE workflow_stages SET status='completed',updated_at=? "
                      "WHERE project_id=? AND stage_key='assets'", (utc_now(), project_id))
        self.repo.complete_task(task_id, result_ids[-1] if result_ids else None, 0)

    def _await_video(self, task_id: str, provider, job, position: int, total: int,
                     runtime) -> object | None:
        """Poll one job until it settles, the deadline passes, or the task stops running.

        Returns the terminal :class:`VideoStatus`, or ``None`` when the task is no longer running.
        """
        import time

        from backend.providers import ProviderError

        deadline = time.monotonic() + max(0.0, float(runtime.video_timeout_seconds))
        while True:
            # Re-read the status *before* every poll, so a cancel is honoured before any new call.
            if not self._still_running(task_id):
                return None
            try:
                status = provider.poll(job)
            except ProviderError:
                if time.monotonic() >= deadline:
                    raise
                if not self._still_running(task_id):
                    return None
                time.sleep(max(0.0, float(runtime.video_poll_seconds)))
                continue
            if status.kind in ("succeeded", "failed"):
                return status
            percent = " 100%" if status.progress is None else f"{int(round(status.progress * 100))}%"
            detail = f"（{status.message}）" if status.message else ""
            self.repo.progress(
                task_id, min(0.95, (position - 0.5) / (total + 1)),
                f"第 {position}/{total} 个镜头：生成中{percent}{detail}")
            if time.monotonic() >= deadline:
                # The deadline is checked after a poll so the last observed state is reported.
                return status
            time.sleep(max(0.0, float(runtime.video_poll_seconds)))

    def _task_payload(self, task_id: str) -> dict:
        with self.database.connect() as c:
            row = c.execute("SELECT payload_json FROM tasks WHERE id=?", (task_id,)).fetchone()
        try:
            payload = json.loads(str(row["payload_json"])) if row else {}
        except (TypeError, ValueError):
            payload = {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _latest_shot_image(media, project_id: str, shot_id: str) -> tuple[bytes, str, str] | None:
        """Read a shot's newest image asset version, re-verifying its checksum.

        Returns ``(image_bytes, mime_type, asset_id)``, or ``None`` when the shot has no usable
        image: the video must start from a real, verified frame, never from a guess.
        """
        from backend.media import MediaRepository

        with media.database.connect() as c:
            row = c.execute("""SELECT a.id asset_id,av.id version_id
                FROM assets a JOIN asset_versions av ON av.asset_id=a.id
                WHERE a.project_id=? AND a.source_shot_id=? AND a.kind='image'
                ORDER BY av.version DESC, av.created_at DESC LIMIT 1""",
                (project_id, shot_id)).fetchone()
        if row is None:
            return None
        source = MediaRepository(media.database).asset_version(
            project_id, str(row["asset_id"]), str(row["version_id"]))
        if source is None:
            return None
        path, mime_type, _filename, _sha256, _size = source
        try:
            return path.read_bytes(), mime_type, str(row["asset_id"])
        except OSError:
            return None

    @staticmethod
    def _shot_rows(c: sqlite3.Connection, project_id: str, shot_ids: list[str]):
        """Fetch the requested shots with the scene context the prompt needs."""
        placeholders = ",".join("?" for _ in shot_ids)
        return c.execute(f"""SELECT sh.id,sh.position,sh.duration_sec,sh.shot_size,sh.camera,sh.action,
                sh.narration_dialogue,sh.render_strategy,sc.title scene_title,sc.setting scene_setting,
                sc.purpose scene_purpose,sc.position scene_position,sc.id scene_id
                FROM shots sh JOIN scenes sc ON sc.id=sh.scene_id JOIN episodes e ON e.id=sc.episode_id
                WHERE e.project_id=? AND sh.id IN ({placeholders})""", (project_id, *shot_ids)).fetchall()
