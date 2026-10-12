"""One-click production pipeline that chains the existing repositories stage by stage.

The pipeline never re-implements business logic: every stage delegates to the same
repository call the public REST API uses, and it only adds progress reporting,
resumable payload bookkeeping, workflow-stage bookkeeping and review gates.
"""
from __future__ import annotations

import json
from uuid import uuid4

from backend.analysis import AnalysisRepository
from backend.database import Database
from backend.media import MediaRepository
from backend.production import ProductionRepository
from backend.providers import ProviderError, build_analyzer
from backend.repository import utc_now

STAGES: tuple[str, ...] = ("analysis", "bible", "production_plan", "demo_media", "timeline", "export")
REVIEW_GATES = {"bible", "production_plan"}

STAGE_LABELS: dict[str, str] = {
    "analysis": "故事分析",
    "bible": "项目圣经",
    "production_plan": "剧本与分镜",
    # Deliberately not called "AI 素材": this stage only produces local placeholders.
    "demo_media": "本地占位素材",
    "timeline": "时间线",
    "export": "导出",
}

# Each pipeline stage maps onto the existing workflow stage keys; no new keys are invented.
STAGE_WORKFLOW_KEYS: dict[str, tuple[str, ...]] = {
    "analysis": ("story_analysis",),
    "bible": ("project_bible",),
    "production_plan": ("screenplay", "storyboard"),
    "demo_media": ("assets", "audio"),
    "timeline": ("timeline",),
    "export": ("export",),
}

# Fixed position order of the eight existing workflow stage keys.
WORKFLOW_ORDER: tuple[str, ...] = (
    "story_analysis", "project_bible", "screenplay", "storyboard",
    "assets", "audio", "timeline", "export",
)


class PipelineRunner:
    """Runs (or resumes) one queued ``pipeline`` task that has already been claimed."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.repo = AnalysisRepository(database)
        self.production = ProductionRepository(database)
        self.media = MediaRepository(database)

    def run(self, task_id: str) -> dict | None:
        task = self.repo.get_task(task_id)
        if task is None:
            return None
        project_id = str(task["project_id"])
        payload = self._payload(task_id)
        stages = self._stages(payload)
        require_review = bool(payload.get("require_review", True))
        completed = [str(item) for item in payload.get("completed_stages") or [] if str(item) in STAGES]
        try:
            stage_index = max(0, int(payload.get("stage_index") or 0))
        except (TypeError, ValueError):
            stage_index = 0
        result_id = task.get("result_id")
        total = len(stages)
        try:
            for index in range(min(stage_index, total), total):
                stage = stages[index]
                # A canceled task must stop immediately: never mark it completed afterwards.
                current = self.repo.get_task(task_id)
                if current is None or str(current["status"]) != "running":
                    return current
                self.repo.progress(task_id, (index + 1) / total, f"正在执行：{STAGE_LABELS[stage]}")
                # A review gate parked the previous stage as "in_progress"; approving it by resuming
                # is what finally completes that stage, and only then may the next one start.
                self._close_previous_review_gate(project_id, stages, index)
                self._start_workflow_stage(project_id, stage)
                value = self._run_stage(stage, task, project_id, last=(index + 1 == total))
                if value is None:
                    raise ValueError(f"阶段「{STAGE_LABELS[stage]}」缺少必要数据，无法继续")
                if isinstance(value, dict) and value.get("id"):
                    result_id = str(value["id"])
                completed.append(stage)
                self._event(task_id, "stage_completed", STAGE_LABELS[stage])
                self._write_payload(task_id, stages, index + 1, completed, require_review)
                if require_review and stage in REVIEW_GATES and index + 1 < total:
                    # The reviewed stage keeps whatever status the generator reported. Parking it back
                    # to 'in_progress' would make dashboard progress drop when a human pauses, so the
                    # review state lives on the task (status='paused') instead of rewriting history.
                    self.repo.pause(task_id, index + 1, f"「{STAGE_LABELS[stage]}」已完成，请审核后再继续制作")
                    return self.repo.get_task(task_id)
                self._finish_workflow_stage(project_id, stage)
            if total:
                self.repo.progress(task_id, 1, "流水线已完成")
            # analyze as the final stage may already have completed the task; complete_task is a no-op then.
            self.repo.complete_task(task_id, result_id, 0)
        except Exception as exc:  # noqa: BLE001 - every stage failure is reported through the task record
            message = str(exc) if isinstance(exc, (ProviderError, ValueError)) else "流水线执行失败，请稍后重试"
            self.repo.fail(task_id, message)
        return self.repo.get_task(task_id)

    # --- stage dispatch -------------------------------------------------

    def _run_stage(self, stage: str, task: dict, project_id: str, *, last: bool) -> object:
        if stage == "analysis":
            return self._run_analysis(task, project_id, last=last)
        if stage == "bible":
            return self.production.generate_bible(project_id)
        if stage == "production_plan":
            return self.production.generate_script(project_id)
        if stage == "demo_media":
            return self.media.generate_demo_media(project_id, source_task_id=str(task["id"]))
        if stage == "timeline":
            return self.media.create_timeline(project_id)
        if stage == "export":
            result = self.media.create_export(project_id)
            if isinstance(result, dict) and result.get("status") == "failed":
                raise ValueError(str(result.get("error") or "export failed"))
            return result
        raise ValueError(f"unsupported pipeline stage: {stage}")

    def _run_analysis(self, task: dict, project_id: str, *, last: bool) -> dict:
        with self.database.connect() as c:
            project = c.execute("SELECT story FROM projects WHERE id=?", (project_id,)).fetchone()
        if project is None:
            raise ProviderError("project not found")
        settings = self.repo.get_provider_settings()
        analyzer = build_analyzer(settings)
        result = analyzer.analyze(str(project["story"]))
        result_json = result.model_dump_json()
        if last:
            # Equivalent to the analysis task path, and only safe because nothing follows it.
            return {"id": self.repo.complete_analysis(task, analyzer.name, analyzer.model, result_json)}
        return {"id": self._store_analysis(task, project_id, analyzer.name, analyzer.model, result_json)}

    def _store_analysis(self, task: dict, project_id: str, provider: str, model: str, result_json: str) -> str:
        """Same persistence as AnalysisRepository.complete_analysis but leaves the task running."""
        now = utc_now()
        task_id = str(task["id"])
        with self.database.connect() as c:
            row = c.execute("SELECT id FROM story_analysis WHERE task_id=?", (task_id,)).fetchone()
            if row:
                analysis_id = str(row["id"])
                c.execute("UPDATE story_analysis SET provider=?,model=?,result_json=?,updated_at=? WHERE id=?",
                          (provider, model, result_json, now, analysis_id))
            else:
                analysis_id = f"analysis_{uuid4().hex}"
                c.execute("INSERT INTO story_analysis (id,project_id,task_id,provider,model,result_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                          (analysis_id, project_id, task_id, provider, model, result_json, now, now))
            c.execute("UPDATE workflow_stages SET status='completed',updated_at=? WHERE project_id=? AND stage_key='story_analysis'", (now, project_id))
        return analysis_id

    # --- bookkeeping ----------------------------------------------------

    def _payload(self, task_id: str) -> dict:
        with self.database.connect() as c:
            row = c.execute("SELECT payload_json FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return {}
        try:
            value = json.loads(str(row["payload_json"] or "{}"))
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _stages(payload: dict) -> list[str]:
        requested = payload.get("stages")
        if not isinstance(requested, list) or not requested:
            return list(STAGES)
        wanted = {str(item) for item in requested}
        return [stage for stage in STAGES if stage in wanted]

    def _write_payload(self, task_id: str, stages: list[str], stage_index: int,
                       completed: list[str], require_review: bool) -> None:
        payload = {"stages": list(stages), "require_review": bool(require_review),
                   "stage_index": int(stage_index), "completed_stages": list(completed)}
        with self.database.connect() as c:
            c.execute("UPDATE tasks SET payload_json=?,updated_at=? WHERE id=?",
                      (json.dumps(payload, ensure_ascii=False), utc_now(), task_id))

    def _event(self, task_id: str, kind: str, message: str) -> None:
        with self.database.connect() as c:
            c.execute("INSERT INTO task_events (task_id,event_type,message,created_at) VALUES (?,?,?,?)",
                      (task_id, kind, message, utc_now()))

    def _start_workflow_stage(self, project_id: str, stage: str) -> None:
        """Show the stage as the honest current stage; already finished stages never regress."""
        now = utc_now()
        with self.database.connect() as c:
            for key in STAGE_WORKFLOW_KEYS[stage]:
                c.execute("UPDATE workflow_stages SET status='in_progress',updated_at=? WHERE project_id=? AND stage_key=? AND status='pending'",
                          (now, project_id, key))

    def _close_previous_review_gate(self, project_id: str, stages: list[str], index: int) -> None:
        """Resuming past a review gate counts as human approval, so the parked stage completes now."""
        if index <= 0:
            return
        previous = stages[index - 1]
        if previous in REVIEW_GATES:
            self._finish_workflow_stage(project_id, previous)

    def _finish_workflow_stage(self, project_id: str, stage: str) -> None:
        """Complete the stage's keys and fill only the pending holes in front of it."""
        own = STAGE_WORKFLOW_KEYS[stage]
        last_position = max(WORKFLOW_ORDER.index(key) for key in own)
        prefix = WORKFLOW_ORDER[:last_position + 1]
        now = utc_now()
        with self.database.connect() as c:
            statuses = {str(row["stage_key"]): str(row["status"]) for row in c.execute(
                "SELECT stage_key,status FROM workflow_stages WHERE project_id=?", (project_id,)).fetchall()}
            for key in prefix:
                if key in own:
                    continue
                current = statuses.get(key)
                if current == "completed":
                    continue
                if current != "pending":
                    break  # never step over an earlier stage that is still unfinished
                c.execute("UPDATE workflow_stages SET status='completed',updated_at=? WHERE project_id=? AND stage_key=? AND status='pending'",
                          (now, project_id, key))
            for key in own:
                c.execute("UPDATE workflow_stages SET status='completed',updated_at=? WHERE project_id=? AND stage_key=? AND status!='completed'",
                          (now, project_id, key))
