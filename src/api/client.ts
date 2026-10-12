import type { AnalysisTask, ApiErrorShape, BackendControlResult, CapabilityProbeInput, CapabilityProbeResult, CapabilitySettings, CreateProjectInput, Episode, ExportFormat, ExportJob, MediaAsset, MediaCapability, PipelineInput, Project, ProjectBible, ProjectCosts, ProjectDashboard, ProjectTask, ProjectTimeline, Shot, StoryAnalysis, SystemDiagnostics, TextProviderSettings, UpdateCapabilitySettings, UpdateTextProviderSettings } from '../types'

export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8765/api'
export class ApiError extends Error { status?: number; constructor(message: string, status?: number) { super(message); this.name = 'ApiError'; this.status = status } }

/**
 * 连接失败时给出的地址必须来自实际配置，不能写死 8765——否则改了 API 地址就会指向一个错误的地方。
 * 文案还要说清「怎么把它启动起来」：桌面壳目前不会自动拉起后端，用户看到窗口却连不上时，
 * 光说"请确认后端已启动"没有用。
 */
function offlineMessage(): string {
  const target = API_BASE_URL.replace(/\/api\/?$/, '')
  return `无法连接本地服务（${target}）。请先启动 V-Maker 后端：双击项目根目录的 start-vmaker.bat` +
    `（桌面版只需要后端时用 start-vmaker.bat backend），或在该目录运行 ` +
    `python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765；` +
    `若后端已在运行，请确认地址与端口和上面的地址一致。桌面版目前不会自动启动后端。`
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try { response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } }) }
  catch { throw new ApiError(offlineMessage()) }
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`
    try { const payload = await response.json() as ApiErrorShape; if (typeof payload.detail === 'string') message = payload.detail; else if (Array.isArray(payload.detail)) message = payload.detail.map((x) => x.msg).filter(Boolean).join('；') || message; else if (payload.message) message = payload.message } catch { /* status fallback */ }
    throw new ApiError(message, response.status)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}
function unwrapList<T>(data: unknown, keys: string[]): T[] { if (Array.isArray(data)) return data as T[]; if (data && typeof data === 'object') { const r = data as Record<string, unknown>; for (const k of keys) if (Array.isArray(r[k])) return r[k] as T[] } return [] }
function unwrapRecord<T>(data: unknown, keys: string[]): T { if (data && typeof data === 'object') { const r = data as Record<string, unknown>; for (const k of keys) if (r[k] && typeof r[k] === 'object') return r[k] as T } return data as T }
const pid = (id: string) => encodeURIComponent(id)

/** 版本号排序用的数值（无法解析时返回 NaN，排序时排到最后）。 */
function versionNumber(value: unknown): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string') { const parsed = Number.parseInt(value.trim(), 10); return Number.isFinite(parsed) ? parsed : Number.NaN }
  return Number.NaN
}

function versionIdOf(entry: unknown): string | null {
  if (!entry || typeof entry !== 'object') return null
  const id = (entry as Record<string, unknown>).id
  return typeof id === 'string' && id ? id : null
}

/** 最新版本的 MIME 类型：优先 latest_version.mime_type，其次 versions 中最新一项的 mime_type。 */
export function latestAssetMimeType(asset: MediaAsset): string | undefined {
  const mimeOf = (entry: unknown): string | undefined => {
    if (!entry || typeof entry !== 'object') return undefined
    const value = (entry as Record<string, unknown>).mime_type
    return typeof value === 'string' && value.trim() ? value.trim().toLowerCase() : undefined
  }
  const direct = mimeOf(asset.latest_version)
  if (direct) return direct
  const versions = (asset as Record<string, unknown>).versions
  if (Array.isArray(versions)) for (const entry of versions) if (versionIdOf(entry) === latestAssetVersionId(asset)) { const found = mimeOf(entry); if (found) return found }
  return undefined
}

/** 取素材最新版本的 version id：优先 latest_version?.id，其次 versions 中版本号最大的一项。 */
export function latestAssetVersionId(asset: MediaAsset): string | null {
  const latest = asset.latest_version
  if (latest && typeof latest === 'object' && typeof latest.id === 'string' && latest.id) return latest.id
  const versions = (asset as Record<string, unknown>).versions
  if (Array.isArray(versions) && versions.length) {
    const sorted = [...versions].sort((a, b) => {
      const left = versionNumber(a && typeof a === 'object' ? (a as Record<string, unknown>).version : undefined)
      const right = versionNumber(b && typeof b === 'object' ? (b as Record<string, unknown>).version : undefined)
      if (Number.isNaN(left) && Number.isNaN(right)) return 0
      if (Number.isNaN(left)) return 1
      if (Number.isNaN(right)) return -1
      return right - left
    })
    return versionIdOf(sorted[0])
  }
  return null
}

export const api = {
  async listProjects(): Promise<Project[]> { return unwrapList<Project>(await request('/projects'), ['items', 'projects', 'data']) },
  getProject(id: string): Promise<Project> { return request(`/projects/${pid(id)}`) },
  createProject(input: CreateProjectInput): Promise<Project> { return request('/projects', { method: 'POST', body: JSON.stringify(input) }) },
  async duplicateProject(id: string): Promise<Project> { return unwrapRecord(await request(`/projects/${pid(id)}/duplicate`, { method: 'POST', body: JSON.stringify({}) }), ['project', 'data']) },
  async archiveProject(id: string): Promise<Project> { return unwrapRecord(await request(`/projects/${pid(id)}/archive`, { method: 'POST', body: JSON.stringify({}) }), ['project', 'data']) },
  async restoreProject(id: string): Promise<Project> { return unwrapRecord(await request(`/projects/${pid(id)}/restore`, { method: 'POST', body: JSON.stringify({}) }), ['project', 'data']) },
  deleteProject(id: string): Promise<void> { return request(`/projects/${pid(id)}`, { method: 'DELETE' }) },
  async getProjectTasks(id: string): Promise<ProjectTask[]> { return unwrapList<ProjectTask>(await request(`/projects/${pid(id)}/tasks`), ['tasks', 'items', 'data']) },
  async cancelTask(id: string): Promise<ProjectTask> { return unwrapRecord<ProjectTask>(await request(`/tasks/${pid(id)}/cancel`, { method: 'POST', body: JSON.stringify({}) }), ['task', 'data']) },
  async retryTask(id: string): Promise<ProjectTask> { return unwrapRecord<ProjectTask>(await request(`/tasks/${pid(id)}/retry?execute=true`, { method: 'POST', body: JSON.stringify({}) }), ['task', 'data']) },
  async resumeTask(id: string): Promise<ProjectTask> { return unwrapRecord<ProjectTask>(await request(`/tasks/${pid(id)}/resume?execute=true`, { method: 'POST', body: JSON.stringify({}) }), ['task', 'data']) },
  async createPipeline(projectId: string, input: PipelineInput = {}): Promise<ProjectTask> { return unwrapRecord<ProjectTask>(await request(`/projects/${pid(projectId)}/pipeline`, { method: 'POST', body: JSON.stringify({ require_review: input.require_review ?? true, ...(input.stages?.length ? { stages: input.stages } : {}) }) }), ['task', 'data']) },
  async getProjectCosts(id: string): Promise<ProjectCosts> { return unwrapRecord(await request(`/projects/${pid(id)}/costs`), ['costs', 'data']) },
  async getSystemDiagnostics(): Promise<SystemDiagnostics> { return unwrapRecord(await request('/system/diagnostics'), ['diagnostics', 'data']) },
  artifactDownloadUrl(id: string): string { return `${API_BASE_URL}/artifacts/${pid(id)}/download` },
  assetContentUrl(projectId: string, assetId: string, versionId: string): string { return `${API_BASE_URL}/projects/${pid(projectId)}/assets/${pid(assetId)}/versions/${pid(versionId)}/content` },
  latestAssetContentUrl(projectId: string, asset: MediaAsset): string | null {
    const versionId = latestAssetVersionId(asset)
    return versionId ? this.assetContentUrl(projectId, asset.id, versionId) : null
  },
  async getDashboard(id: string): Promise<ProjectDashboard> { return unwrapRecord(await request(`/projects/${pid(id)}/dashboard`), ['dashboard', 'data']) },
  async getBible(id: string): Promise<ProjectBible> { return unwrapRecord(await request(`/projects/${pid(id)}/bible`), ['bible', 'data']) },
  async generateBible(id: string): Promise<ProjectBible> { return unwrapRecord(await request(`/projects/${pid(id)}/bible`, { method: 'POST', body: JSON.stringify({}) }), ['bible', 'data']) },
  async approveBible(id: string): Promise<ProjectBible> { return unwrapRecord(await request(`/projects/${pid(id)}/bible/approve`, { method: 'POST', body: JSON.stringify({}) }), ['bible', 'data']) },
  async generateProductionPlan(id: string): Promise<unknown> { return request(`/projects/${pid(id)}/production-plan`, { method: 'POST', body: JSON.stringify({}) }) },
  async getEpisodes(id: string): Promise<Episode[]> {
    const episodes = unwrapList<Episode>(await request(`/projects/${pid(id)}/episodes`), ['episodes', 'items', 'data'])
    // The production API returns scene metadata here; shots live in a separate project endpoint.
    // Keep already-nested responses compatible and avoid hiding fetch errors as empty storyboards.
    const needsShots = episodes.some((episode) => !episode.shots?.length
      && episode.scenes?.some((scene) => !scene.shots?.length))
    if (!needsShots) return episodes
    const shots = unwrapList<Shot>(await request(`/projects/${pid(id)}/shots`), ['shots', 'items', 'data'])
    const byScene = new Map<string, Shot[]>()
    for (const shot of shots) {
      if (!shot.scene_id) continue
      const group = byScene.get(shot.scene_id) ?? []
      group.push(shot)
      byScene.set(shot.scene_id, group)
    }
    return episodes.map((episode) => ({
      ...episode,
      scenes: episode.scenes?.map((scene) => ({
        ...scene,
        shots: scene.shots?.length ? scene.shots : byScene.get(scene.id) ?? [],
      })),
    }))
  },
  async getShot(id: string): Promise<Shot> { return unwrapRecord(await request(`/shots/${pid(id)}`), ['shot', 'data']) },
  async updateShot(id: string, input: Partial<Shot>): Promise<Shot> { return unwrapRecord(await request(`/shots/${pid(id)}`, { method: 'PATCH', body: JSON.stringify(input) }), ['shot', 'data']) },
  async approveShots(id: string, shotIds?: string[]): Promise<unknown> { return request(`/projects/${pid(id)}/shots/approve`, { method: 'POST', body: JSON.stringify(shotIds?.length ? { shot_ids: shotIds } : {}) }) },
  async getTextProviderSettings(): Promise<TextProviderSettings> { return unwrapRecord(await request('/settings/text-provider'), ['settings', 'data']) },
  async updateTextProviderSettings(input: UpdateTextProviderSettings): Promise<TextProviderSettings> { return unwrapRecord(await request('/settings/text-provider', { method: 'PUT', body: JSON.stringify(input) }), ['settings', 'data']) },
  async getCapabilitySettings(): Promise<CapabilitySettings[]> { return unwrapList<CapabilitySettings>(await request('/capability-settings'), ['settings', 'capabilities', 'items', 'data']) },
  async getCapabilitySetting(capability: MediaCapability): Promise<CapabilitySettings> { return unwrapRecord(await request(`/capability-settings/${pid(capability)}`), ['setting', 'settings', 'data']) },
  async updateCapabilitySetting(capability: MediaCapability, input: UpdateCapabilitySettings): Promise<CapabilitySettings> { return unwrapRecord(await request(`/capability-settings/${pid(capability)}`, { method: 'PUT', body: JSON.stringify(input) }), ['setting', 'settings', 'data']) },
  /**
   * 测试某个能力的配置：可用表单里尚未保存的值来测，缺省字段由后端回退到已保存的设置。
   * 入参只在有值时带上对应字段，始终带上 live。失败也是 HTTP 200，靠 ok 与 message 表达。
   */
  async testCapabilitySetting(capability: MediaCapability, input: CapabilityProbeInput): Promise<CapabilityProbeResult> {
    const payload: CapabilityProbeInput = { live: input.live }
    if (input.provider) payload.provider = input.provider
    if (input.endpoint) payload.endpoint = input.endpoint
    if (input.model) payload.model = input.model
    if (input.api_key_env) payload.api_key_env = input.api_key_env
    return unwrapRecord<CapabilityProbeResult>(await request(`/capability-settings/${pid(capability)}/test`, { method: 'POST', body: JSON.stringify(payload) }), ['result', 'data'])
  },
  async generateShotImage(projectId: string, shotId: string): Promise<ProjectTask> { return unwrapRecord<ProjectTask>(await request(`/projects/${pid(projectId)}/shots/${pid(shotId)}/image`, { method: 'POST', body: JSON.stringify({}) }), ['task', 'data']) },
  async generateProjectImages(projectId: string, shotIds?: string[]): Promise<ProjectTask> { return unwrapRecord<ProjectTask>(await request(`/projects/${pid(projectId)}/media-images`, { method: 'POST', body: JSON.stringify(shotIds?.length ? { shot_ids: shotIds } : {}) }), ['task', 'data']) },
  /** 单镜头图生视频：以后端返回任务为准，后端会用该镜头已有的图像素材作首帧，后台异步轮询执行。 */
  async generateShotVideo(projectId: string, shotId: string): Promise<ProjectTask> { return unwrapRecord<ProjectTask>(await request(`/projects/${pid(projectId)}/shots/${pid(shotId)}/video`, { method: 'POST', body: JSON.stringify({}) }), ['task', 'data']) },
  /** 页面级视频生成：不传 shot_ids 时由后端展开为项目全部镜头。 */
  async generateProjectVideos(projectId: string, shotIds?: string[]): Promise<ProjectTask> { return unwrapRecord<ProjectTask>(await request(`/projects/${pid(projectId)}/media-videos`, { method: 'POST', body: JSON.stringify(shotIds?.length ? { shot_ids: shotIds } : {}) }), ['task', 'data']) },
  async generateProjectSpeech(projectId: string, shotIds?: string[]): Promise<ProjectTask> { return unwrapRecord<ProjectTask>(await request(`/projects/${pid(projectId)}/speech`, { method: 'POST', body: JSON.stringify(shotIds?.length ? { shot_ids: shotIds } : {}) }), ['task', 'data']) },
  async createAnalysisTask(projectId: string): Promise<AnalysisTask> { return unwrapRecord(await request(`/projects/${pid(projectId)}/analysis-tasks`, { method: 'POST', body: JSON.stringify({}) }), ['task', 'data']) },
  async getTask(taskId: string): Promise<AnalysisTask> { return unwrapRecord(await request(`/tasks/${pid(taskId)}`), ['task', 'data']) },
  async getStoryAnalysis(projectId: string): Promise<StoryAnalysis> { return unwrapRecord(await request(`/projects/${pid(projectId)}/story-analysis`), ['analysis', 'data']) },
  async generateDemoMedia(projectId: string): Promise<unknown> { return request(`/projects/${pid(projectId)}/demo-media`, { method: 'POST', body: JSON.stringify({}) }) },
  async getAssets(projectId: string): Promise<MediaAsset[]> { return unwrapList<MediaAsset>(await request(`/projects/${pid(projectId)}/assets`), ['assets', 'items', 'data']) },
  async generateTimeline(projectId: string): Promise<ProjectTimeline> { return unwrapRecord(await request(`/projects/${pid(projectId)}/timeline`, { method: 'POST', body: JSON.stringify({}) }), ['timeline', 'data']) },
  async getTimeline(projectId: string): Promise<ProjectTimeline> { return unwrapRecord(await request(`/projects/${pid(projectId)}/timeline`), ['timeline', 'data']) },
  /**
   * 发起导出。format 缺省为 'auto'（由后端按环境决定实际产物）；
   * 界面显式选择格式时必须原样传给后端，不要在前端做臆测。
   */
  async createExport(projectId: string, format: ExportFormat = 'auto'): Promise<ExportJob> { return unwrapRecord(await request(`/projects/${pid(projectId)}/exports`, { method: 'POST', body: JSON.stringify({ format }) }), ['export', 'job', 'data']) },
  async getExport(exportId: string): Promise<ExportJob> { return unwrapRecord(await request(`/exports/${pid(exportId)}`), ['export', 'job', 'data']) },
  /** 最近的导出（含失败）。没有它，过去的导出在界面上就再也查不回来了。 */
  async listExports(projectId: string, limit = 10): Promise<ExportJob[]> { return unwrapList<ExportJob>(await request(`/projects/${pid(projectId)}/exports?limit=${limit}`), ['exports', 'items', 'data']) },
  /** 让服务管理器重启后端。请求会先返回，随后本进程被管理器停掉，界面应当断开并重试。 */
  async restartBackend(): Promise<BackendControlResult> { return await request('/system/backend/restart', { method: 'POST', body: JSON.stringify({}) }) as BackendControlResult },
  /** 停止后端。之后没有 API 可以再请求启动，必须用启动器或命令行。 */
  async stopBackend(): Promise<BackendControlResult> { return await request('/system/backend/stop', { method: 'POST', body: JSON.stringify({}) }) as BackendControlResult },
}
