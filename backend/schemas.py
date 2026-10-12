"""Pydantic request and response schemas."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

ProjectMode = Literal["draft", "standard", "professional"]
ProjectStatus = Literal["draft", "active", "paused", "completed", "archived"]
StageStatus = Literal["pending", "in_progress", "completed", "blocked", "failed"]
ApprovalStatus = Literal["draft", "approved"]


class ProjectFields(BaseModel):
    language: str = Field(default="zh-CN", min_length=1, max_length=40)
    creative_intent: str = Field(default="", max_length=10_000)
    episode_strategy: Literal["single", "ai_split"] = "single"
    visual_style: str = Field(default="", max_length=500)
    audience: str = Field(default="", max_length=500)
    narration: Literal["balanced", "dialogue", "narration"] = "balanced"
    budget_limit: float = Field(default=0, ge=0)
    outputs: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("language")
    @classmethod
    def language_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("outputs")
    @classmethod
    def normalize_outputs(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        return list(dict.fromkeys(normalized))


class ProjectCreate(ProjectFields):
    name: str = Field(min_length=1, max_length=200)
    story: str = Field(default="", max_length=100_000)
    mode: ProjectMode = "standard"
    aspect_ratio: str = Field(default="16:9", max_length=20)
    target_duration_sec: int = Field(default=60, ge=1, le=86_400)
    status: ProjectStatus = "draft"

    @model_validator(mode="before")
    @classmethod
    def accept_duration_alias(cls, value: Any) -> Any:
        if isinstance(value, dict) and "target_duration_sec" not in value and "duration_seconds" in value:
            value = dict(value)
            value["target_duration_sec"] = value["duration_seconds"]
        return value

    @field_validator("name", "aspect_ratio")
    @classmethod
    def strip_non_empty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    story: str | None = Field(default=None, max_length=100_000)
    mode: ProjectMode | None = None
    aspect_ratio: str | None = Field(default=None, max_length=20)
    target_duration_sec: Annotated[int, Field(ge=1, le=86_400)] | None = None
    status: ProjectStatus | None = None
    language: str | None = Field(default=None, min_length=1, max_length=40)
    creative_intent: str | None = Field(default=None, max_length=10_000)
    episode_strategy: Literal["single", "ai_split"] | None = None
    visual_style: str | None = Field(default=None, max_length=500)
    audience: str | None = Field(default=None, max_length=500)
    narration: Literal["balanced", "dialogue", "narration"] | None = None
    budget_limit: float | None = Field(default=None, ge=0)
    outputs: list[str] | None = Field(default=None, max_length=20)

    @model_validator(mode="before")
    @classmethod
    def accept_duration_alias(cls, value: Any) -> Any:
        if isinstance(value, dict) and "target_duration_sec" not in value and "duration_seconds" in value:
            value = dict(value)
            value["target_duration_sec"] = value["duration_seconds"]
        return value

    @field_validator("name", "aspect_ratio", "language")
    @classmethod
    def strip_optional_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class Project(ProjectFields):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    story: str
    mode: ProjectMode
    aspect_ratio: str
    target_duration_sec: int
    status: ProjectStatus
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    deleted_at: datetime | None = None
    copied_from_id: str | None = None


class WorkflowStage(BaseModel):
    id: str; project_id: str; stage_key: str; name: str; position: int; status: StageStatus
    created_at: datetime; updated_at: datetime


class BudgetSummary(BaseModel):
    limit: float
    settled: float
    reserved: float
    available: float | None


class ProjectDashboard(BaseModel):
    project: Project
    workflow_stages: list[WorkflowStage]
    workflow_summary: dict[StageStatus, int]
    budget: BudgetSummary
    task_summary: dict[str, int]


class ProviderSettingsUpdate(BaseModel):
    provider: Literal["demo", "openai", "deepseek"]
    endpoint: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_key_env: str = Field(default="OPENAI_API_KEY", pattern=r"^[A-Z][A-Z0-9_]*$")

    @field_validator("endpoint", "model")
    @classmethod
    def strip_provider_text(cls, value: str) -> str:
        value = value.strip()
        if not value: raise ValueError("must not be blank")
        return value


class ProviderSettings(ProviderSettingsUpdate):
    configured: bool
    updated_at: datetime


CAPABILITIES = ("text", "image", "speech", "video")


class CapabilitySettings(BaseModel):
    """One row of ``capability_settings``: how a single capability is produced."""

    capability: str
    provider: str
    endpoint: str
    model: str
    api_key_env: str
    unit_cost: float
    configured: bool
    updated_at: datetime | None = None


class CapabilitySettingsUpdate(BaseModel):
    provider: str = Field(min_length=1, max_length=100)
    endpoint: str = Field(default="", max_length=500)
    model: str = Field(min_length=1, max_length=200)
    # Only the demo provider may run without a credential, so an empty name is allowed there.
    api_key_env: str = Field(default="", max_length=200)
    unit_cost: float = Field(default=0, ge=0)

    @field_validator("provider", "model", "endpoint")
    @classmethod
    def strip_capability_text(cls, value: str, info: ValidationInfo) -> str:
        value = value.strip()
        # endpoint may legitimately be empty (a demo provider has no service to call).
        if not value and info.field_name in ("provider", "model"):
            raise ValueError("must not be blank")
        return value

    @field_validator("api_key_env")
    @classmethod
    def validate_api_key_env(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
            raise ValueError("must match ^[A-Z][A-Z0-9_]*$")
        return value

    @model_validator(mode="after")
    def require_values_for_external_provider(self) -> "CapabilitySettingsUpdate":
        if not self.provider.strip():
            raise ValueError("provider must not be blank")
        if not self.model.strip():
            raise ValueError("model must not be blank")
        if not self.api_key_env and self.provider.strip() != "demo":
            raise ValueError("api_key_env is required unless the provider is demo")
        return self


class CapabilityTestRequest(BaseModel):
    """Body of ``POST /api/capability-settings/{capability}/test``.

    Every field is optional: an omitted field tests whatever the capability already has saved,
    so the user can probe the stored configuration, a configuration they have not saved yet,
    or a mixture of both.
    """

    live: bool = False
    provider: str | None = Field(default=None, max_length=100)
    endpoint: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    api_key_env: str | None = Field(default=None, max_length=200)


class CapabilityTestResult(BaseModel):
    """The honest outcome of one connection test.

    ``configured`` and ``reachable`` describe what was checked without spending money, and
    ``live`` records whether a real (billable) call was actually made. ``ok`` is true only when
    everything that applies to this capability succeeded.
    """

    capability: str
    provider: str
    model: str
    live: bool
    configured: bool
    reachable: bool | None
    ok: bool
    message: str
    latency_ms: int


ExportFormat = Literal["auto", "mp4", "portable_draft", "jianying_draft"]


class ExportRequest(BaseModel):
    """Body of ``POST /api/projects/{id}/exports``.

    ``auto`` prefers MP4, then a real Jianying draft, then the portable draft package. The other
    three are explicit: see ``MediaRepository.create_export``. An unknown value is a 422, never a
    silent downgrade.
    """

    timeline_version: int | None = Field(default=None, ge=1)
    format: ExportFormat = "auto"


class AnalysisCharacter(BaseModel):
    name: str; role: str; description: str
class AnalysisScene(BaseModel):
    title: str; setting: str; description: str
class AnalysisBeat(BaseModel):
    order: int = Field(ge=1); title: str; description: str
class StoryAnalysisResult(BaseModel):
    summary: str; genres: list[str]; audience: list[str]; characters: list[AnalysisCharacter]
    scenes: list[AnalysisScene]; beats: list[AnalysisBeat]; risks: list[str]; questions: list[str]

TaskStatus = Literal["queued", "running", "completed", "failed", "canceled", "paused"]
class TaskEvent(BaseModel):
    id: int; task_id: str; event_type: str; message: str; created_at: datetime
class Task(BaseModel):
    id: str; project_id: str | None; task_type: str; status: TaskStatus; result_id: str | None
    error: str | None; attempts: int; progress: float; message: str; capability: str
    estimated_cost: float; actual_cost: float | None; provider: str | None; model: str | None
    created_at: datetime; updated_at: datetime
    started_at: datetime | None; finished_at: datetime | None; events: list[TaskEvent] = Field(default_factory=list)
class StoryAnalysis(BaseModel):
    id: str; project_id: str; task_id: str; provider: str; model: str; result: StoryAnalysisResult
    created_at: datetime; updated_at: datetime


class BibleEntity(BaseModel):
    id: str; kind: Literal["character", "location", "prop"]; position: int; name: str
    data: dict[str, Any]; status: ApprovalStatus
class BibleVersion(BaseModel):
    id: str; project_id: str; version: int; status: ApprovalStatus; source_analysis_id: str
    style: dict[str, Any]; characters: list[BibleEntity]; locations: list[BibleEntity]; props: list[BibleEntity]
    demo: bool; generator: str; created_at: datetime; updated_at: datetime; approved_at: datetime | None


class Scene(BaseModel):
    id: str; episode_id: str; position: int; title: str; setting: str; purpose: str; action: str
    dialogue: str; duration_sec: int; status: ApprovalStatus
class Episode(BaseModel):
    id: str; project_id: str; version: int; position: int; title: str; synopsis: str
    status: ApprovalStatus; source_analysis_id: str; demo: bool; created_at: datetime; updated_at: datetime
    scenes: list[Scene] = Field(default_factory=list)
class Shot(BaseModel):
    id: str; scene_id: str; order: int; duration: int; shot_size: str; camera: str; action: str
    narration_dialogue: str; render_strategy: str; status: ApprovalStatus; revision: int
    created_at: datetime; updated_at: datetime
class ShotUpdate(BaseModel):
    order: int | None = Field(default=None, ge=1)
    duration: int | None = Field(default=None, ge=1, le=3600)
    shot_size: str | None = Field(default=None, min_length=1, max_length=100)
    camera: str | None = Field(default=None, min_length=1, max_length=500)
    action: str | None = Field(default=None, min_length=1, max_length=10_000)
    narration_dialogue: str | None = Field(default=None, max_length=10_000)
    render_strategy: Literal["text_to_video", "image_to_video", "start_end_frame_video", "talking_head", "static_pan_zoom", "composite_effect", "imported_media"] | None = None
    status: ApprovalStatus | None = None
class ScriptStoryboard(BaseModel):
    project_id: str; version: int; demo: bool; generator: str; target_duration_sec: int
    episodes: list[Episode]; shots: list[Shot]


class HealthResponse(BaseModel):
    status: Literal["ok"]; service: str; database: Literal["ok"]
