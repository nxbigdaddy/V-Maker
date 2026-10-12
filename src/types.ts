export type ProjectMode = 'draft' | 'standard' | 'professional'
export type ProjectStatus = 'draft' | 'active' | 'paused' | 'completed' | 'archived'

export interface Project {
  id: string
  name: string
  story?: string
  mode: ProjectMode
  language?: string
  aspect_ratio: string
  target_duration_sec?: number
  resolution?: [number, number]
  status: ProjectStatus
  created_at?: string
  updated_at?: string
  cover_url?: string
  progress?: number
  current_stage?: string
  episode_count?: number
  shot_count?: number
  review_count?: number
  running_tasks?: number
  budget?: ProjectBudget
}

export interface CreateProjectInput {
  name: string
  story: string
  creative_intent?: string
  episode_strategy: 'single' | 'ai_split'
  target_duration_sec: number
  aspect_ratio: '16:9' | '9:16' | '1:1'
  visual_style: string
  audience: string
  narration: 'balanced' | 'dialogue' | 'narration'
  mode: ProjectMode
  budget_limit: number
  outputs: string[]
  language: string
}

export type StageStatus = 'pending' | 'in_progress' | 'completed' | 'blocked' | 'failed'
export interface WorkflowStage { id: string; project_id: string; stage_key: string; name: string; position: number; status: StageStatus; created_at?: string; updated_at?: string }
export interface ProjectDashboard { project: Project; workflow_stages: WorkflowStage[]; workflow_summary: Record<StageStatus, number>; task_summary?: Record<string, number>; episode_count?: number; shot_count?: number; review_count?: number; running_tasks?: number; asset_count?: number; character_count?: number; budget?: ProjectBudget & { limit?: number; settled?: number; available?: number | null } }
export interface ProjectBudget { currency?: string; hard_limit?: number; spent?: number; reserved?: number }
/** 任务状态为 string，后端会持续新增状态值（例如 'paused' 表示等待人工审核），不要收窄为联合类型。 */
export type ProjectTaskStatus = 'queued' | 'pending' | 'running' | 'in_progress' | 'processing' | 'paused' | 'completed' | 'succeeded' | 'success' | 'failed' | 'cancelled' | 'canceled' | (string & {})
export interface ProjectTaskPayload { require_review?: boolean; stage_index?: number; completed_stages?: string[]; stages?: string[]; [key: string]: unknown }
export interface ProjectTask {
  id: string
  project_id?: string
  type?: string
  task_type?: string
  status: ProjectTaskStatus
  result_id?: string
  progress?: number
  message?: string
  error?: string
  capability?: string
  estimated_cost?: number
  actual_cost?: number
  provider?: string
  model?: string
  cost?: number
  currency?: string
  attempt?: number
  attempts?: number
  max_attempts?: number
  payload?: ProjectTaskPayload
  events?: unknown[]
  created_at?: string
  updated_at?: string
  started_at?: string
  finished_at?: string
}

/** 一键自动制作流水线（POST /projects/{id}/pipeline）的请求体。 */
export interface PipelineInput {
  require_review?: boolean
  stages?: string[]
}
export interface CostItem { id?: string; type?: string; label?: string; message?: string; amount?: number; currency?: string; created_at?: string }
export interface ProjectCosts { currency?: string; spent?: number; reserved?: number; hard_limit?: number; budget?: ProjectBudget; items?: CostItem[] }
export interface DiagnosticItem { key?: string; label?: string; ok?: boolean; message?: string }
/**
 * GET /api/system/diagnostics 里的剪映（pyJianYingDraft）信息。
 *
 * 后端字段名尚未最终固定，因此这里把所有可能出现的名字都标为可选，
 * 读取时一律用可选链兜底：字段缺失或改名只会显示「未知」，不会让界面崩掉。
 */
/** 后端 `GET /api/system/diagnostics` 里 `jianying` 块的真实形状（backend/jianying.py + backend/main.py）。 */
export interface JianyingDraftsDir {
  configured?: boolean
  path?: string | null
  writable?: boolean
  message?: string
}
export interface JianyingDiagnostics {
  /** 库名，例如 pyJianYingDraft。 */
  library?: string
  /** 库是否可用（能导入）。 */
  available?: boolean
  installed?: boolean
  version?: string
  library_version?: string
  /** 该库能做到什么：draft_generation / unavailable。 */
  capability?: string
  /** 只生成草稿；自动导出不提供（该库的自动导出只支持剪映 6 及以下）。 */
  draft_generation?: boolean
  auto_export?: boolean | string
  message?: string
  /** 后端如实声明的未验证项（我们从未在真实剪映中验证过草稿能否打开）。 */
  unverified?: string
  /** 素材是绝对路径引用，删除项目素材会让草稿失效。 */
  asset_paths?: string
  /** 剪映不支持 SVG，这些镜头会被跳过。 */
  svg?: string
  /** VMAKER_JIANYING_DRAFTS_DIR 的配置状态（嵌套对象，不是字符串）。 */
  drafts_dir?: JianyingDraftsDir | null
  draft_dir?: string | null
  drafts_dir_configured?: boolean
  [key: string]: unknown
}
/** 后端进程自身的信息（`GET /api/system/diagnostics` 的 `process` 块），由服务管理器维护。 */
export interface BackendProcessInfo {
  pid?: number
  port?: number
  python?: string
  /** 是否由 scripts/service.ps1 启动（PID 文件里记的就是这个进程）。 */
  managed?: boolean
  pid_file?: string
  log_file?: string
  service_log_file?: string
  manager?: string
  manager_available?: boolean
  can_restart?: boolean
  /** 后端自己写明的限制：停止之后没有 API 可以再请求「启动」。 */
  note?: string
}
export interface SystemDiagnostics { ok?: boolean; status?: string; items?: DiagnosticItem[]; health?: { ok?: boolean; message?: string }; ffmpeg?: { available?: boolean; version?: string; message?: string }; data_dir?: { path?: string; writable?: boolean }; desktop?: { available?: boolean; message?: string }; process?: BackendProcessInfo; jianying?: JianyingDiagnostics; jianying_draft?: JianyingDiagnostics }

/** 服务管理器动作的返回：ok 为 false 时必须显示 message，不能假装已经重启。 */
export interface BackendControlResult { ok?: boolean; action?: string; message?: string; service_log?: string }

export interface BibleEntry { id?: string; name?: string; title?: string; description?: string; prompt?: string; appearance?: string; personality?: string; role?: string; location?: string; status?: string; [key: string]: unknown }
export interface ProjectBible { id?: string; project_id?: string; status?: string; approved?: boolean; version?: number; style?: BibleEntry | string; style_guide?: BibleEntry | string; characters?: BibleEntry[]; scenes?: BibleEntry[]; locations?: BibleEntry[]; props?: BibleEntry[]; updated_at?: string }

export interface DialogueLine { id?: string; speaker?: string; speaker_character_id?: string; text?: string; emotion?: string }
export interface Shot { id: string; scene_id?: string; order?: number; number?: number; title?: string; purpose?: string; duration_sec?: number; duration_seconds?: number; duration_ticks?: number; shot_size?: string; camera_angle?: string; camera_motion?: string; characters?: string[]; character_refs?: Array<{entity_id?: string; name?: string}>; action?: string; dialogue?: string | DialogueLine[]; render_strategy?: string; status?: string; thumbnail_url?: string; [key: string]: unknown }
export interface Scene { id: string; title?: string; name?: string; order?: number; location?: string; description?: string; shots?: Shot[] }
export interface Episode { id: string; title?: string; name?: string; number?: number; order?: number; status?: string; scenes?: Scene[]; shots?: Shot[] }

export type TextProviderKind = 'demo' | 'openai' | 'deepseek'
export interface TextProviderSettings { provider: TextProviderKind; endpoint: string; model: string; api_key_env?: string; configured?: boolean }
export interface UpdateTextProviderSettings { provider: TextProviderKind; endpoint: string; model: string; api_key_env: string }

/**
 * 能力设置（GET/PUT /capability-settings）：文本、图像、语音、视频四种能力各自独立配置。
 * demo 之外的 provider 必须通过 api_key_env 指定的后端环境变量提供密钥；unit_cost 由用户填写，系统不猜测价格。
 * 注意：视频能力没有本地占位实现，provider 为 demo 时任何视频生成都会直接失败。
 */
export type MediaCapability = 'text' | 'image' | 'speech' | 'video'
export interface CapabilitySettings {
  capability: MediaCapability
  provider: string
  endpoint: string
  model: string
  api_key_env?: string
  unit_cost?: number
  /** provider 为 demo，或 api_key_env 指向的环境变量已存在于后端进程环境。 */
  configured?: boolean
  /** true 表示当前配置只使用本地占位产物，不会联网调用真实服务。 */
  demo?: boolean
}
export interface UpdateCapabilitySettings {
  provider: string
  endpoint: string
  model: string
  api_key_env: string
  unit_cost: number
}

/**
 * 入参（POST /capability-settings/{capability}/test）：全部可选，允许测试尚未保存的配置；
 * 缺省字段由后端回退到已保存的设置。live=false 只做配置与网络连通性检查，不调用生成服务、不产生费用。
 */
export interface CapabilityProbeInput {
  /** true 时文本/语音会发一个极小的真实请求（可能产生极少费用）；图像与视频能力不做真实生成测试。 */
  live: boolean
  provider?: string
  endpoint?: string
  model?: string
  api_key_env?: string
}

/**
 * 测试连接结果。失败同样是 HTTP 200，必须靠 ok 与 message 判断，不能只看 HTTP 状态。
 */
export interface CapabilityProbeResult {
  capability: MediaCapability
  provider: string
  model: string
  /** 本次是否真的调用了生成服务；false 表示只做了配置与网络连通性检查。 */
  live: boolean
  /** provider 是 demo，或后端进程里存在 api_key_env 指向的环境变量。 */
  configured: boolean
  /** provider 是 demo 时为 null（不联网）；否则表示能否连上 endpoint 主机。 */
  reachable: boolean | null
  ok: boolean
  /** 后端返回的中文说明：讲清测了什么、没测什么、失败原因。 */
  message: string
  latency_ms: number
}
export type AnalysisTaskStatus = 'queued' | 'pending' | 'running' | 'in_progress' | 'completed' | 'succeeded' | 'failed' | 'cancelled'
export interface AnalysisTask { id: string; project_id?: string; status: AnalysisTaskStatus; progress?: number; message?: string; error?: string; created_at?: string; updated_at?: string }
export interface StoryCharacter { name: string; role?: string; description?: string; goal?: string; conflict?: string }
export interface StoryScene { name?: string; title?: string; setting?: string; location?: string; time?: string; description?: string; purpose?: string }
export interface PlotBeat { title?: string; description: string; order?: number }
export interface StoryAnalysisResult { summary: string; genres: string[]; audience: string[]; characters: StoryCharacter[]; scenes: StoryScene[]; beats: PlotBeat[]; risks: string[]; questions: string[] }
export interface StoryAnalysis { id?: string; project_id?: string; task_id?: string; provider?: string; model?: string; result?: StoryAnalysisResult; summary: string; genre: string[]; audience: string[]; characters: StoryCharacter[]; scenes: StoryScene[]; plot_beats: PlotBeat[]; risks: string[]; open_questions: string[]; updated_at?: string }
export interface MediaAsset {
  id: string
  project_id?: string
  shot_id?: string
  type?: string
  kind?: string
  asset_type?: string
  name?: string
  title?: string
  path?: string
  file_path?: string
  url?: string
  preview_url?: string
  status?: string
  version?: number | string
  duration_sec?: number
  duration_seconds?: number
  text?: string
  metadata?: Record<string, unknown>
  /** 后端返回的最新版本信息，用于拼接素材内容预览地址。 */
  latest_version?: { id?: string; version?: number; mime_type?: string; byte_size?: number; demo?: boolean } | null
  [key: string]: unknown
}

export interface TimelineClip {
  id?: string
  asset_id?: string
  shot_id?: string
  name?: string
  title?: string
  text?: string
  start_sec?: number
  start?: number
  duration_sec?: number
  duration?: number
  end_sec?: number
  [key: string]: unknown
}
export interface TimelineTrack { id?: string; type?: string; kind?: string; name?: string; clips?: TimelineClip[]; items?: TimelineClip[]; [key: string]: unknown }
export interface ProjectTimeline {
  id?: string
  project_id?: string
  version?: number | string
  status?: string
  duration_sec?: number
  duration_seconds?: number
  total_duration_sec?: number
  tracks?: TimelineTrack[]
  video?: TimelineClip[]
  audio?: TimelineClip[]
  dialogue?: TimelineClip[]
  subtitles?: TimelineClip[]
  [key: string]: unknown
}

/**
 * 导出请求的 format 取值（POST /projects/{id}/exports）。
 * auto：有 ffmpeg 出 MP4，否则优先剪映草稿，再否则通用草稿包；
 * mp4：显式要 MP4，无 ffmpeg 时按原有规则失败/回退，不会悄悄变成草稿；
 * portable_draft：通用草稿包（现状）；
 * jianying_draft：剪映原生草稿，库缺失或构建失败会回退成通用草稿包。
 */
export type ExportFormat = 'auto' | 'mp4' | 'portable_draft' | 'jianying_draft'

/** 导出任务自身的 format 可能与请求值不同（auto 会被后端解析成具体产物）。 */
export type ExportJobFormat = 'mp4' | 'draft_package' | 'jianying_draft' | (string & {})

export interface ExportArtifact {
  id?: string
  artifact_id?: string
  /** 产物类型；剪映草稿为 'jianying_draft'（mime application/zip）。 */
  type?: string
  kind?: string
  mime_type?: string
  path?: string
  status?: string
  [key: string]: unknown
}

export interface ExportJob {
  id: string
  project_id?: string
  /** 实际产出的格式：mp4 / draft_package / jianying_draft（兼容未知字符串，不要收窄）。 */
  format?: ExportJobFormat
  status?: string
  progress?: number
  /** 后端写的导出说明：实际选了什么格式、跳过了哪些镜头及原因、回退原因。 */
  warning?: string
  warnings?: string[]
  error?: string
  output_path?: string
  mp4_path?: string
  draft_path?: string
  artifact_paths?: string[] | Record<string, string | null | undefined>
  artifacts?: ExportArtifact[] | Record<string, unknown>
  [key: string]: unknown
}

export interface ApiErrorShape { detail?: string | { msg?: string }[]; message?: string }
