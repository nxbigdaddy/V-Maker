import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { AlertTriangle, Archive, CheckCircle2, Circle, Download, FileVideo2, FolderOpen, History, LoaderCircle, PackageCheck, RefreshCw, Scissors } from 'lucide-react'
import { api } from '../api/client'
import { ConfirmDialog } from '../components/ConfirmDialog'
import type { ExportArtifact, ExportFormat, ExportJob, Project } from '../types'

const runningStatuses = new Set(['queued', 'pending', 'running', 'processing', 'in_progress'])
const completeStatuses = new Set(['completed', 'succeeded', 'success', 'partial'])

/** 「最近导出」列表要能一眼看出状态与请求的格式，包括失败的那次。 */
const exportStatusLabels: Record<string, string> = {
  queued: '排队中', pending: '等待中', running: '进行中', processing: '处理中', in_progress: '进行中',
  completed: '已完成', succeeded: '已完成', success: '已完成', partial: '部分完成',
  failed: '失败', canceled: '已取消', cancelled: '已取消',
}
const exportFormatLabels: Record<string, string> = {
  auto: '自动', mp4: 'MP4', portable_draft: '通用草稿包', draft_package: '通用草稿包', jianying_draft: '剪映草稿',
}
const exportStatusLabel = (status: string) => exportStatusLabels[status] ?? status
const exportFormatLabel = (raw: unknown) =>
  typeof raw === 'string' && raw.trim() ? exportFormatLabels[raw.trim()] ?? raw.trim() : '格式未记录'

type ArtifactPath = { label: string; path: string; id?: string; kind?: string }

/** 后端可能的产物 kind：'jianying_draft' / 'draft_package' / 'mp4'；顺序敏感，先判断剪映。 */
function artifactLabel(kind: unknown, fallback: string): string {
  const value = typeof kind === 'string' ? kind.toLowerCase() : ''
  if (!value) return fallback
  if (/jianying|剪映/.test(value)) return '剪映草稿'
  if (/mp4|video/.test(value)) return 'MP4 成片'
  if (/draft|portable|package|zip/.test(value)) return '通用草稿包'
  return fallback
}

function kindOf(value: unknown): string | undefined {
  if (!value || typeof value !== 'object') return undefined
  const record = value as Record<string, unknown>
  const kind = record.kind ?? record.type
  return typeof kind === 'string' && kind ? kind : undefined
}

function pathsOf(job: ExportJob): ArtifactPath[] {
  const values: ArtifactPath[] = []
  const add = (label: unknown, path: unknown, id?: unknown, kind?: unknown) => {
    if (typeof path !== 'string' || !path || values.some((item) => item.path === path)) return
    values.push({
      label: typeof label === 'string' && label ? label : '产物',
      path,
      id: typeof id === 'string' && id ? id : undefined,
      kind: typeof kind === 'string' && kind ? kind : undefined,
    })
  }

  add('MP4 成片', job.mp4_path, undefined, 'mp4')
  add('通用草稿包', job.draft_path, undefined, 'draft_package')
  add('导出目录', job.output_path)

  if (Array.isArray(job.artifact_paths)) {
    job.artifact_paths.forEach((path, index) => add(`产物 ${index + 1}`, path))
  } else if (job.artifact_paths && typeof job.artifact_paths === 'object') {
    Object.entries(job.artifact_paths).forEach(([key, path]) => add(artifactLabel(key, key), path, undefined, key))
  }

  if (Array.isArray(job.artifacts)) {
    job.artifacts.forEach((artifact, index) => {
      const kind = kindOf(artifact)
      add(artifactLabel(kind, `产物 ${index + 1}`), artifact.path, artifact.id ?? artifact.artifact_id, kind)
    })
  } else if (job.artifacts && typeof job.artifacts === 'object') {
    Object.entries(job.artifacts).forEach(([key, value]) => {
      if (typeof value === 'string') {
        add(artifactLabel(key, key), value, undefined, key)
        return
      }
      const record = value && typeof value === 'object' ? value as Record<string, unknown> : null
      const artifact = record as ExportArtifact | null
      const kind = kindOf(value) ?? key
      add(artifactLabel(kind, key), artifact?.path, artifact?.id ?? artifact?.artifact_id, kind)
    })
  }
  return values
}

function warningsOf(job: ExportJob): string[] {
  const values: unknown[] = [...(Array.isArray(job.warnings) ? job.warnings : []), job.warning]
  return values.filter((value): value is string => typeof value === 'string' && Boolean(value.trim()))
}

function progressPercent(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null
  return Math.max(0, Math.min(100, Math.round(value <= 1 ? value * 100 : value)))
}

type Delivery = { label: string; tone: 'jianying' | 'mp4' | 'draft' | 'unknown'; raw: string; detail: string }

/**
 * 把后端返回的 format 映射成界面文案。
 * 后端返回值：mp4 / draft_package / jianying_draft；请求值里的 auto、portable_draft 也一并兼容。
 * 未知值不做臆测：宁可显示「未知格式」，也不要让用户以为拿到了剪映草稿。
 */
function deliveryOf(job: ExportJob): Delivery {
  const raw = typeof job.format === 'string' ? job.format.trim() : ''
  const value = raw.toLowerCase()
  if (!raw) {
    return {
      label: '格式未知', tone: 'unknown', raw: '（后端未返回 format 字段）',
      detail: '后端没有返回 format 字段，无法确认实际交付格式。请以下方产物路径与警告为准，不要默认它是剪映草稿。',
    }
  }
  if (/jianying|剪映/.test(value)) {
    return {
      label: '剪映草稿', tone: 'jianying', raw,
      detail: '剪映原生草稿包（解压后含 draft_content.json 与 draft_meta_info.json）。草稿内素材为绝对路径引用，请勿删除项目数据目录。',
    }
  }
  if (value === 'mp4' || /mp4|video/.test(value)) {
    return { label: 'MP4', tone: 'mp4', raw, detail: 'MP4 成片。' }
  }
  if (/draft|portable|package/.test(value)) {
    return {
      label: '通用草稿包', tone: 'draft', raw,
      detail: '中立的通用草稿包（现状格式），不是剪映原生草稿；剪映不能直接打开，需要剪映草稿请改用「剪映草稿」选项。',
    }
  }
  return {
    label: '未知格式', tone: 'unknown', raw,
    detail: '后端返回了界面不认识的格式值，请以下方产物路径与警告为准，不要假设它是剪映草稿。',
  }
}

type FormatOption = { value: ExportFormat; title: string; description: string; badges: ReactNode }

/** 选择项与后端 format 取值一一对应，不在此处臆测后端行为。 */
const formatOptions: FormatOption[] = [
  {
    value: 'auto',
    title: '自动选择（推荐）',
    description: '由后端按本机环境决定产出：有 FFmpeg 时合成 MP4；没有 FFmpeg 时优先尝试剪映草稿，再退回通用草稿包。',
    badges: <><FileVideo2 size={14} /> MP4 <span>或</span> <Archive size={14} /> 通用草稿包<span>（按环境决定）</span></>,
  },
  {
    value: 'mp4',
    title: 'MP4 成片',
    description: '显式要求 MP4。没有 FFmpeg 时按原有规则失败或回退，不会悄悄改成只给草稿。',
    badges: <><FileVideo2 size={14} /> MP4 成片 <span>（需要本机 FFmpeg）</span></>,
  },
  {
    value: 'portable_draft',
    title: '通用草稿包',
    description: '现状的中立草稿包，不依赖 pyJianYingDraft，任何环境都能生成。',
    badges: <><Archive size={14} /> 通用草稿包<span>（现状格式，非剪映原生草稿）</span></>,
  },
  {
    value: 'jianying_draft',
    title: '剪映草稿（实验性，未验证）',
    description: '尝试生成剪映原生草稿（draft_content.json + draft_meta_info.json），供剪映继续精修。依赖 pyJianYingDraft，失败会自动回退成通用草稿包；我们从未在真实剪映里验证过它能否打开。',
    badges: <><Scissors size={14} /> 剪映原生草稿 <span>（未验证 · 可能回退）</span></>,
  },
]

/** 剪映草稿的限制：如实写清，不弱化措辞。 */
const jianyingCaveats: string[] = [
  '需要后端安装 pyJianYingDraft。没装或构建失败时，导出会自动回退成通用草稿包，结果区会写明回退原因。',
  '剪映不支持 SVG：默认的 demo 占位图是位图（PNG），可以直接进草稿；但如果你自己的图像 provider 返回 SVG，这些镜头会被跳过，结果区会列出被跳过的镜头与原因。',
  '草稿里的素材是绝对路径引用，指向本项目的数据目录。不要删除项目素材，否则草稿会失效。',
  '我们从未在真实剪映里验证过这份草稿能否正常打开（开发环境没有剪映）。请不要把它当成已验证可用的产出。',
  '不提供「自动导出成片」：该库的自动导出只支持剪映 6 及以下。这里只生成草稿，成片请在剪映里自行导出。',
]

const confirmDescriptions: Record<ExportFormat, string> = {
  auto: '将由后端按本机环境决定实际产物：有 FFmpeg 时合成 MP4，没有 FFmpeg 时优先尝试剪映草稿，再退回通用草稿包。没有 FFmpeg 时 MP4 不会生成；实际产出格式与警告会在结果区展示。导出会占用 CPU 与磁盘空间，可能需要等待较长时间；已发起的导出无法撤销，只能等待完成或另行取消任务。',
  mp4: '将尝试用 FFmpeg 合成 MP4 成片。没有 FFmpeg 时会按原有规则失败或回退，不会静默改成只给草稿。合成会占用 CPU 与磁盘空间，可能需要等待较长时间；已发起的导出无法撤销，只能等待完成或另行取消任务。',
  portable_draft: '将生成中立的通用草稿包（现状格式），不依赖 pyJianYingDraft，任何环境都能完成。该包不是剪映原生草稿。已发起的导出无法撤销，只能等待完成或另行取消任务。',
  jianying_draft: '将尝试生成剪映原生草稿。若后端未安装 pyJianYingDraft 或构建失败，会自动回退成通用草稿包并说明原因；剪映不支持 SVG，若你的图像 provider 返回 SVG，对应镜头会被跳过，结果区会列出被跳过的镜头与原因；草稿里是绝对路径引用，请不要删除项目素材；我们从未在真实剪映里验证过这份草稿能否正常打开；本次只生成草稿，不做自动导出，成片请在剪映里自行导出。已发起的导出无法撤销，只能等待完成或另行取消任务。',
}

export function ExportPage({ project }: { project: Project }) {
  const [job, setJob] = useState<ExportJob | null>(null)
  const [recent, setRecent] = useState<ExportJob[]>([])
  const [format, setFormat] = useState<ExportFormat>('auto')
  const [starting, setStarting] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 打开页面就把最近的导出读回来：否则上一次失败的导出在界面上不留任何痕迹，
  // 用户只能去任务中心翻原因。
  const loadRecent = useCallback(async () => {
    try {
      setRecent(await api.listExports(project.id))
    } catch {
      // 历史读不到不该挡住「发起导出」这条主路径，静默降级即可。
      setRecent([])
    }
  }, [project.id])

  useEffect(() => { void loadRecent() }, [loadRecent])

  const refresh = useCallback(async (id?: string) => {
    if (!id) return
    try {
      const result = await api.getExport(id)
      setJob(result)
      setError(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '导出状态读取失败。')
    }
  }, [])

  const status = typeof job?.status === 'string' ? job.status.toLowerCase() : 'unknown'
  const running = runningStatuses.has(status)

  useEffect(() => {
    if (!job?.id || !running) return
    const timer = window.setInterval(() => void refresh(job.id), 1500)
    return () => window.clearInterval(timer)
  }, [job?.id, running, refresh])

  const start = async () => {
    setConfirming(false)
    setStarting(true)
    setError(null)
    try {
      setJob(await api.createExport(project.id, format))
      void loadRecent()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '导出任务创建失败。')
    } finally {
      setStarting(false)
    }
  }

  const complete = completeStatuses.has(status)
  const paths = useMemo(() => job ? pathsOf(job) : [], [job])
  const warnings = useMemo(() => job ? warningsOf(job) : [], [job])
  const delivery = useMemo(() => job ? deliveryOf(job) : null, [job])
  // 任务还在跑且后端尚未写 format 时不显示「格式未知」，避免误导；跑完必须显示。
  const showDelivery = Boolean(delivery) && (!running || typeof job?.format === 'string')
  const ffmpegMissing = warnings.some((item) => /ffmpeg/i.test(item)) || /ffmpeg/i.test(job?.error ?? '')
  const hasDraft = paths.some((item) => /draft|草稿/i.test(`${item.label} ${item.kind ?? ''} ${item.path}`))
  const percent = progressPercent(job?.progress)

  return (
    <main className="studio-page export-page">
      <section className="studio-hero">
        <div>
          <span className="eyebrow">DELIVERY</span>
          <h1>导出</h1>
          <p>将当前时间线交付为 MP4 成片、通用草稿包，或实验性的剪映原生草稿。</p>
        </div>
        {job && (
          <button type="button" className="button ghost" onClick={() => void refresh(job.id)} disabled={running}>
            <RefreshCw size={16} /> 刷新状态
          </button>
        )}
      </section>

      {error && (
        <div className="studio-alert">
          <AlertTriangle />
          <span style={{ minWidth: 0 }}><strong>导出操作失败</strong><small style={{ overflowWrap: 'anywhere' }}>{error}</small></span>
          {job && <button type="button" onClick={() => void refresh(job.id)}><RefreshCw size={14} /> 重试</button>}
        </div>
      )}

      {!job ? (
        <section className="export-picker">
          <header>
            <Archive />
            <div>
              <h2>选择交付格式</h2>
              <p>选择项与后端支持的 format 一一对应。剪映草稿为实验性能力，请先读完选中项下方的说明再导出。</p>
            </div>
          </header>
          <div className="export-format-list" role="radiogroup" aria-label="交付格式">
            {formatOptions.map((option) => {
              const selected = format === option.value
              return (
                <label key={option.value} className={selected ? 'export-option selected' : 'export-option unselected'}>
                  <input
                    type="radio"
                    name="export-format"
                    value={option.value}
                    checked={selected}
                    onChange={() => setFormat(option.value)}
                  />
                  <span className="option-check">{selected ? <CheckCircle2 /> : <Circle />}</span>
                  <div>
                    <strong>{option.title}</strong>
                    <p>{option.description}</p>
                    <small>{option.badges}</small>
                  </div>
                </label>
              )
            })}
          </div>

          {format === 'jianying_draft' && (
            <div className="export-note export-note-stack">
              <AlertTriangle size={17} />
              <div>
                <strong>关于剪映草稿：以下 5 条限制请先读完</strong>
                <ol className="export-caveats">
                  {jianyingCaveats.map((item) => <li key={item}>{item}</li>)}
                </ol>
              </div>
            </div>
          )}

          <div className="export-note">
            <AlertTriangle size={17} />
            <span><strong>关于 FFmpeg</strong>如果本机未安装 FFmpeg，草稿包仍可成功生成，但 MP4 不会生成；结果区会明确展示此警告。</span>
          </div>
          <button type="button" className="button primary export-submit" onClick={() => setConfirming(true)} disabled={starting}>
            {starting ? <LoaderCircle className="spin" size={17} /> : <Download size={17} />} 发起导出
          </button>
        </section>
      ) : (
        <section className="export-result">
          <header className={complete ? 'success' : running ? 'running' : 'failed'}>
            {running ? <LoaderCircle className="spin" /> : complete ? <PackageCheck /> : <AlertTriangle />}
            <div style={{ minWidth: 0 }}>
              <span>{running ? '正在导出' : complete ? '导出已完成' : '导出未完成'}</span>
              <h2 style={{ overflowWrap: 'anywhere' }}>{running ? '正在打包项目产物…' : complete ? '交付产物已准备好' : job.error || '请检查导出结果'}</h2>
              <small style={{ overflowWrap: 'anywhere' }}>任务 ID：{job.id} · 状态：{job.status || '未知'}</small>
            </div>
            {percent !== null && <b>{percent}%</b>}
          </header>

          {running && <div className="export-progress"><i style={{ width: `${Math.max(3, percent ?? 12)}%` }} /></div>}

          {delivery && showDelivery && (
            <div className="export-delivery">
              <em className={`delivery-badge ${delivery.tone}`}>{delivery.label}</em>
              <div>
                <strong>实际交付格式</strong>
                <p style={{ overflowWrap: 'anywhere' }}>{delivery.detail}</p>
                <small>后端返回 format：<code>{delivery.raw}</code></small>
              </div>
            </div>
          )}

          {(warnings.length > 0 || ffmpegMissing) && (
            <div className="export-warning">
              <AlertTriangle />
              <div style={{ minWidth: 0 }}>
                <strong>{warnings.length > 0 ? '导出提示与警告' : 'MP4 未生成：未检测到 FFmpeg'}</strong>
                {warnings.map((warning, index) => <p key={`${warning}-${index}`} style={{ overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}>{warning}</p>)}
                {ffmpegMissing && (
                  <p>{hasDraft ? '草稿包已成功生成，可以继续交付或编辑。' : '后端仍会尝试完成草稿包，请查看下方产物路径。'} MP4 未生成。</p>
                )}
              </div>
            </div>
          )}

          {!running && paths.length === 0 ? (
            <div className="section-empty">暂无可展示的产物路径。请刷新状态或查看警告信息。</div>
          ) : paths.length > 0 && (
            <div className="artifact-list">
              <h3>产物路径</h3>
              {paths.map((item) => {
                const kind = (item.kind ?? '').toLowerCase()
                const isJianying = /jianying|剪映/.test(kind)
                const isVideo = !isJianying && /mp4/i.test(`${item.label} ${item.path}`)
                return (
                  <div key={`${item.label}-${item.path}`}>
                    <span className={isJianying ? 'jianying' : isVideo ? 'video' : 'draft'}>
                      {isJianying ? <Scissors /> : isVideo ? <FileVideo2 /> : <FolderOpen />}
                    </span>
                    <div style={{ minWidth: 0 }}>
                      <strong style={{ overflowWrap: 'anywhere' }}>{item.label}</strong>
                      <code style={{ overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}>{item.path}</code>
                      {isJianying && (
                        <small className="artifact-hint">
                          这是剪映草稿压缩包：下载后解压得到草稿目录（内含 draft_content.json 与 draft_meta_info.json），把该目录整体放进剪映的草稿目录才能打开。草稿内是绝对路径引用，请不要删除本项目的数据目录。
                        </small>
                      )}
                    </div>
                    {item.id ? (
                      <a className="artifact-download" href={api.artifactDownloadUrl(item.id)} download>
                        <Download size={14} /> {isJianying ? '下载草稿包（解压后放进剪映草稿目录）' : '安全下载'}
                      </a>
                    ) : (
                      <em title="后端未返回 artifact id，无法生成安全下载链接">仅路径</em>
                    )}
                  </div>
                )
              })}
            </div>
          )}

          {!running && (
            <button type="button" className="button ghost export-again" onClick={() => { setJob(null); setError(null) }}>
              再次导出
            </button>
          )}
        </section>
      )}

      <ConfirmDialog
        open={confirming}
        title="发起导出？"
        description={confirmDescriptions[format]}
        confirmLabel="确认发起导出"
        busy={starting}
        onConfirm={() => void start()}
        onCancel={() => setConfirming(false)}
      />

      {recent.length > 0 && (
        <section className="export-history">
          <header>
            <History size={16} />
            <div>
              <h2>最近导出</h2>
              <p>保留最近的导出记录，便于回看失败原因或重新打开某次结果。</p>
            </div>
          </header>
          <ul>
            {recent.map((entry) => {
              const entryStatus = typeof entry.status === 'string' ? entry.status.toLowerCase() : 'unknown'
              const failure = entry.error || entry.warning
              return (
                <li key={entry.id} className={entryStatus}>
                  <span className={`status-chip ${entryStatus}`}>{exportStatusLabel(entryStatus)}</span>
                  <div className="export-history-body">
                    <strong>{exportFormatLabel(entry.format)}</strong>
                    <small>任务 {entry.id}{typeof entry.created_at === 'string' && entry.created_at
                      ? ` · ${new Date(entry.created_at).toLocaleString('zh-CN')}` : ''}</small>
                    {failure && <p>{failure}</p>}
                  </div>
                  <button type="button" className="button ghost" onClick={() => setJob(entry)}>
                    查看
                  </button>
                </li>
              )
            })}
          </ul>
        </section>
      )}
    </main>
  )
}
