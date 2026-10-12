import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  ArrowLeft,
  BookOpenText,
  Boxes,
  Check,
  ChevronRight,
  CircleDollarSign,
  Clapperboard,
  Download,
  Film,
  LayoutDashboard,
  Library,
  ListChecks,
  LoaderCircle,
  MoreHorizontal,
  PauseCircle,
  Play,
  RefreshCw,
  Settings2,
  Sparkles,
} from 'lucide-react'
import { api } from '../api/client'
import { Brand } from '../components/Brand'
import { SystemDiagnosticsModal } from '../components/SystemDiagnosticsModal'
import { TextProviderModal } from '../components/TextProviderModal'
import type { Project, ProjectDashboard, ProjectTask } from '../types'
import { AssetsPage } from './AssetsPage'
import { BiblePage } from './BiblePage'
import { CostsPage } from './CostsPage'
import { ExportPage } from './ExportPage'
import { StoryAnalysisPage } from './StoryAnalysisPage'
import { StoryboardPage } from './StoryboardPage'
import { TasksPage } from './TasksPage'
import { TimelinePage } from './TimelinePage'

const POLL_INTERVAL_MS = 3000
const liveStatuses = new Set(['queued', 'pending', 'running', 'processing', 'in_progress', 'paused'])

function normalizeStatus(value: unknown): string {
  return typeof value === 'string' && value.trim() ? value.trim().toLowerCase() : 'unknown'
}

/** 最新一条流水线任务（后端 task_type 为 pipeline）。 */
function pipelineTaskOf(tasks: ProjectTask[]): ProjectTask | null {
  const pipelines = tasks.filter((task) => `${task.task_type ?? task.type ?? ''}`.toLowerCase().includes('pipeline'))
  if (!pipelines.length) return null
  const timeOf = (task: ProjectTask) => {
    const raw = task.updated_at ?? task.created_at
    const time = raw ? new Date(raw).getTime() : Number.NaN
    return Number.isNaN(time) ? -1 : time
  }
  return pipelines.reduce((latest, task) => (timeOf(task) >= timeOf(latest) ? task : latest))
}

/** 完成阶段的中文名，用于审核提示。 */
const stageNames: Record<string, string> = {
  analysis: '故事分析', bible: '项目圣经', production_plan: '制作计划', demo_media: 'Demo 素材', timeline: '时间线', export: '导出',
}
function completedStageLabel(task: ProjectTask): string {
  const stages = task.payload?.completed_stages
  if (!Array.isArray(stages) || !stages.length) return ''
  return stages.map((stage) => stageNames[String(stage)] ?? String(stage)).join('、')
}

const navItems = [
  ['dashboard', LayoutDashboard, '项目概览'],
  ['story', BookOpenText, '故事与剧本'],
  ['bible', Library, '项目圣经'],
  ['shots', Clapperboard, '分镜工作台'],
  ['assets', Boxes, '素材库'],
  ['timeline', Film, '时间线'],
  ['tasks', ListChecks, '任务中心'],
  ['costs', CircleDollarSign, '成本预算'],
  ['export', Download, '导出'],
] as const

type WorkspaceView = (typeof navItems)[number][0]

const titles: Record<WorkspaceView, string> = {
  dashboard: '项目概览',
  story: '故事与剧本',
  bible: '项目圣经',
  shots: '分镜工作台',
  assets: '素材库',
  timeline: '时间线',
  tasks: '任务中心',
  costs: '成本预算',
  export: '导出',
}

/** 真实的 stage_key 是 bible / storyboard / screenplay，这里按真实值决定审核跳转。 */
function reviewTargetOf(stageKey?: string): WorkspaceView {
  const key = (stageKey ?? '').toLowerCase()
  if (key.includes('bible')) return 'bible'
  if (key.includes('storyboard') || key.includes('screenplay') || key.includes('shot')) return 'shots'
  return 'story'
}

/** 审核跳转目标：优先使用任务 message（例如「等待审核：项目圣经」）里明确的阶段。 */
function reviewTargetOfTask(task: ProjectTask | null, fallback: WorkspaceView): WorkspaceView {
  const text = `${task?.message ?? ''} ${task?.task_type ?? ''}`.toLowerCase()
  if (/圣经|bible/.test(text)) return 'bible'
  if (/分镜|镜头|storyboard|screenplay|shot/.test(text)) return 'shots'
  return fallback
}

export function WorkspacePage({ project: initial, onBack }: { project: Project; onBack: () => void }) {
  const [view, setView] = useState<WorkspaceView>('dashboard')
  const [dashboard, setDashboard] = useState<ProjectDashboard | null>(null)
  const [tasks, setTasks] = useState<ProjectTask[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [pipelineError, setPipelineError] = useState<string | null>(null)
  const [pipelineBusy, setPipelineBusy] = useState(false)
  const [resumingPipeline, setResumingPipeline] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [showDiagnostics, setShowDiagnostics] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setDashboard(await api.getDashboard(initial.id))
      setTasks(await api.getProjectTasks(initial.id))
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : '工作台读取失败。')
    } finally {
      setLoading(false)
    }
  }, [initial.id])

  useEffect(() => {
    void load()
  }, [load])

  const pipelineTask = useMemo(() => pipelineTaskOf(tasks), [tasks])
  const pipelinePaused = normalizeStatus(pipelineTask?.status) === 'paused'
  const hasLiveTasks = useMemo(() => tasks.some((task) => liveStatuses.has(normalizeStatus(task.status))), [tasks])

  // 概览页也要自愈：有排队 / 运行 / 等待审核的任务时每 3 秒静默刷新，卸载时清理定时器。
  useEffect(() => {
    if (!hasLiveTasks) return
    let cancelled = false
    const timer = window.setInterval(() => {
      if (cancelled) return
      void api.getProjectTasks(initial.id)
        .then((next) => { if (!cancelled) setTasks(next) })
        .catch(() => { /* 轮询失败保留现有数据 */ })
    }, POLL_INTERVAL_MS)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [hasLiveTasks, initial.id])

  const startPipeline = async () => {
    setPipelineBusy(true)
    setPipelineError(null)
    try {
      await api.createPipeline(initial.id, { require_review: true })
      setView('tasks')
      await load()
    } catch (reason) {
      setPipelineError(reason instanceof Error ? reason.message : '一键自动制作启动失败。')
    } finally {
      setPipelineBusy(false)
    }
  }

  const resumePipeline = async () => {
    if (!pipelineTask) return
    setResumingPipeline(true)
    setPipelineError(null)
    try {
      await api.resumeTask(pipelineTask.id)
      await load()
    } catch (reason) {
      setPipelineError(reason instanceof Error ? reason.message : '流水线继续失败。')
    } finally {
      setResumingPipeline(false)
    }
  }

  const project = dashboard?.project ?? initial
  const spent = dashboard?.budget?.spent ?? dashboard?.budget?.settled ?? project.budget?.spent ?? 0
  const limit = dashboard?.budget?.hard_limit ?? dashboard?.budget?.limit ?? project.budget?.hard_limit ?? 0
  const nextView = useMemo<WorkspaceView>(() => {
    const current = dashboard?.workflow_stages.find((stage) => stage.status === 'in_progress')
      ?? dashboard?.workflow_stages.find((stage) => stage.status === 'pending')
    return reviewTargetOf(current?.stage_key)
  }, [dashboard])

  return (
    <div className="workspace-shell">
      <aside className="workspace-sidebar">
        <Brand />
        <button
          type="button"
          className="back-projects"
          onClick={onBack}
          aria-label="返回所有项目"
          title="返回所有项目"
        >
          <ArrowLeft size={15} />
          <span>返回所有项目</span>
        </button>

        <nav className="workspace-nav" aria-label="项目工作区导航">
          {navItems.map(([key, Icon, label]) => (
            <button
              type="button"
              className={view === key ? 'active' : ''}
              key={key}
              onClick={() => setView(key)}
              aria-label={label}
              title={label}
              aria-current={view === key ? 'page' : undefined}
            >
              <Icon size={18} />
              <span>{label}</span>
              {key === 'shots' && <small>{dashboard?.shot_count ?? project.shot_count ?? 0}</small>}
            </button>
          ))}
        </nav>

        <div className="sidebar-bottom">
          <button type="button" aria-label="模型设置" title="模型设置" onClick={() => setShowSettings(true)}>
            <Settings2 size={18} />
            <span>模型设置</span>
          </button>
          <div className="profile">
            <div>创</div>
            <span>
              <strong>本地创作者</strong>
              <small>个人工作区</small>
            </span>
            <MoreHorizontal size={17} />
          </div>
        </div>
      </aside>

      <div className="workspace-main">
        <header className="workspace-header">
          <div>
            <span className="project-status-dot" />
            <span className="project-title" title={project.name}>{project.name}</span>
            <small>{titles[view]}</small>
          </div>
          <div>
            <span className="saved"><Check size={14} /> 已保存</span>
            <button
              type="button"
              className="button ghost"
              onClick={() => setView('costs')}
              aria-label="查看成本预算"
            >
              <CircleDollarSign size={16} />
              ¥{spent.toFixed(2)} / ¥{limit || '—'}
            </button>
            <button
              type="button"
              className="button primary"
              onClick={() => void startPipeline()}
              disabled={pipelineBusy}
              aria-label="继续自动制作并启动一键流水线"
              title="继续自动制作并启动一键流水线"
            >
              {pipelineBusy ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}
              继续自动制作
            </button>
          </div>
        </header>

        {pipelineError && (
          <div className="studio-alert workspace-alert" role="alert">
            <AlertTriangle />
            <span style={{ minWidth: 0 }}><strong>操作未完成</strong><small style={{ overflowWrap: 'anywhere' }}>{pipelineError}</small></span>
            <button type="button" onClick={() => setPipelineError(null)}>关闭</button>
          </div>
        )}

        {view === 'dashboard' ? (
          <Dashboard
            data={dashboard}
            project={project}
            loading={loading}
            error={error}
            retry={load}
            navigate={setView}
            nextView={nextView}
            pipelineTask={pipelineTask}
            pipelinePaused={pipelinePaused}
            resumePipeline={resumePipeline}
            resumingPipeline={resumingPipeline}
          />
        ) : view === 'story' ? (
          <StoryAnalysisPage project={project} />
        ) : view === 'bible' ? (
          <BiblePage project={project} onChanged={() => void load()} />
        ) : view === 'shots' ? (
          <StoryboardPage project={project} onChanged={() => void load()} />
        ) : view === 'assets' ? (
          <AssetsPage project={project} />
        ) : view === 'timeline' ? (
          <TimelinePage project={project} />
        ) : view === 'tasks' ? (
          <TasksPage project={project} />
        ) : view === 'costs' ? (
          <CostsPage project={project} />
        ) : (
          <ExportPage project={project} />
        )}
      </div>

      {showSettings && (
        <TextProviderModal
          onClose={() => setShowSettings(false)}
          onDiagnostics={() => {
            setShowSettings(false)
            setShowDiagnostics(true)
          }}
        />
      )}
      {showDiagnostics && <SystemDiagnosticsModal onClose={() => setShowDiagnostics(false)} />}
    </div>
  )
}

interface DashboardProps {
  data: ProjectDashboard | null
  project: Project
  loading: boolean
  error: string | null
  retry: () => Promise<void>
  navigate: (view: WorkspaceView) => void
  nextView: WorkspaceView
  pipelineTask: ProjectTask | null
  pipelinePaused: boolean
  resumePipeline: () => Promise<void>
  resumingPipeline: boolean
}

function Dashboard({ data, project, loading, error, retry, navigate, nextView, pipelineTask, pipelinePaused, resumePipeline, resumingPipeline }: DashboardProps) {
  if (loading) {
    return (
      <div className="studio-loading dashboard-state">
        <LoaderCircle className="spin" />
        正在读取导演控制台……
      </div>
    )
  }

  if (error) {
    return (
      <div className="studio-empty dashboard-state">
        <AlertTriangle />
        <h2>工作台暂时无法载入</h2>
        <p>{error}</p>
        <button type="button" className="button ghost" onClick={() => void retry()}>
          <RefreshCw size={16} />
          重新连接
        </button>
      </div>
    )
  }

  const stages = data?.workflow_stages ?? []
  const completedStages = stages.filter((stage) => stage.status === 'completed').length
  const progress = stages.length ? Math.round((completedStages / stages.length) * 100) : project.progress ?? 0
  const current = stages.find((stage) => stage.status === 'in_progress')
    ?? stages.find((stage) => stage.status === 'pending')
  const reviews = data?.review_count ?? project.review_count ?? 0
  const running = data?.running_tasks ?? data?.task_summary?.running ?? project.running_tasks ?? 0
  const pausedTask = pipelinePaused ? pipelineTask : null
  const pausedStages = pausedTask ? completedStageLabel(pausedTask) : ''

  return (
    <main className="dashboard">
      <section className="welcome-row">
        <div>
          <span className="eyebrow">导演控制台</span>
          <h1>欢迎回来，创作继续。</h1>
          <p>
            项目整体进度 <strong>{progress}%</strong>，当前阶段：{current?.name || '制作已完成'}。
          </p>
        </div>
        <button type="button" className="icon-button" aria-label="更多项目操作" title="更多项目操作">
          <MoreHorizontal />
        </button>
      </section>

      <section className="stage-card">
        <div className="stage-head">
          <div>
            <span className="pulse-dot" />
            <strong>当前阶段</strong>
            <small>{current?.name || '全部阶段已完成'}</small>
          </div>
          <span>{progress}%</span>
        </div>
        <div className="stage-track"><i style={{ width: `${progress}%` }} /></div>
        <div className="stages dynamic">
          {stages.map((stage, index) => (
            <div className={stage.status} key={stage.id}>
              <span>
                {stage.status === 'completed' ? (
                  <Check size={14} />
                ) : stage.status === 'in_progress' ? (
                  <LoaderCircle className="spin" size={14} />
                ) : (
                  index + 1
                )}
              </span>
              <small>{stage.name}</small>
            </div>
          ))}
        </div>
      </section>

      {pipelinePaused && (
        <section className="pipeline-review" role="status">
          <PauseCircle size={20} aria-hidden="true" />
          <div className="panel-text">
            <strong>流水线等待你审核</strong>
            <small>{pausedTask?.message || '流水线已暂停，等待你确认后继续。'}{pausedStages ? `（已完成：${pausedStages}）` : ''}</small>
          </div>
          <div className="pipeline-review-actions">
            <button type="button" className="button primary" onClick={() => void resumePipeline()} disabled={resumingPipeline}>
              {resumingPipeline ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}
              继续
            </button>
            <button type="button" className="button ghost" onClick={() => navigate(reviewTargetOfTask(pausedTask, nextView))}>
              去审核
              <ChevronRight size={16} />
            </button>
          </div>
        </section>
      )}

      <div className="dashboard-grid">
        <section className="panel review-panel">
          <div className="panel-heading">
            <div>
              <h2>待我审核</h2>
              <p>确认后才能进入下一制作阶段</p>
            </div>
            {pipelinePaused
              ? <span className="count-badge paused">等待审核</span>
              : reviews ? <span className="count-badge count-badge-muted" title="后端 review_count 汇总值，不代表具体待审核项">汇总 {reviews}</span> : null}
          </div>
          {pipelinePaused && pipelineTask ? (
            <div className="review-item">
              <div className="panel-text">
                <strong>流水线等待你审核</strong>
                <small>{pipelineTask.message || '确认后流水线会继续执行下一阶段'}</small>
              </div>
              <button
                type="button"
                className="button subtle"
                onClick={() => navigate(reviewTargetOfTask(pipelineTask, nextView))}
              >
                去审核
                <ChevronRight size={16} />
              </button>
            </div>
          ) : reviews ? (
            <div className="review-item">
              <div className="panel-text">
                <strong>{current?.name || '制作内容'}等待确认</strong>
                <small>请进入对应工作台审核最新版本</small>
              </div>
              <button
                type="button"
                className="button subtle"
                onClick={() => navigate(reviewTargetOf(current?.stage_key))}
              >
                去审核
                <ChevronRight size={16} />
              </button>
            </div>
          ) : (
            <div className="mini-empty">
              <Check size={21} />
              <span>
                <strong>暂时没有待审核内容</strong>
                <small>新的审核项会出现在这里</small>
              </span>
            </div>
          )}
        </section>

        <section className="panel stats-panel">
          <div className="panel-heading">
            <div>
              <h2>制作数据</h2>
              <p>项目实时概览</p>
            </div>
          </div>
          <div className="stats-grid">
            <div><span className="stat-icon lavender"><Clapperboard /></span><strong>{data?.shot_count ?? 0}</strong><small>镜头总数</small></div>
            <div><span className="stat-icon mint"><Boxes /></span><strong>{data?.asset_count ?? 0}</strong><small>生成素材</small></div>
            <div><span className="stat-icon amber"><ListChecks /></span><strong>{running}</strong><small>运行任务</small></div>
            <div><span className="stat-icon rose"><Library /></span><strong>{data?.character_count ?? 0}</strong><small>角色设定</small></div>
          </div>
        </section>

        <section className="panel episode-panel">
          <div className="panel-heading">
            <div>
              <h2>剧集进度</h2>
              <p>{data?.episode_count ?? 0} 集 · 制作规划</p>
            </div>
            <button type="button" onClick={() => navigate('shots')}>
              查看全部 <ChevronRight size={15} />
            </button>
          </div>
          {(data?.episode_count ?? 0) > 0 ? (
            <div className="episode-row">
              <div className="episode-thumb"><span>EP</span><strong>01</strong></div>
              <div className="episode-info">
                <strong>剧集生产计划已建立</strong>
                <p>前往分镜工作台查看场景和镜头。</p>
              </div>
            </div>
          ) : (
            <div className="mini-empty">
              <Clapperboard />
              <span>
                <strong>暂无剧集计划</strong>
                <small>完成前置阶段后生成生产计划</small>
              </span>
            </div>
          )}
        </section>

        <section className="panel task-panel">
          <div className="panel-heading">
            <div>
              <h2>任务中心</h2>
              <p>队列与异常状态</p>
            </div>
            <button type="button" onClick={() => navigate('tasks')}>
              查看全部 <ChevronRight size={15} />
            </button>
          </div>
          {pipelinePaused ? (
            <div className="task-running task-paused">
              <PauseCircle />
              <div className="panel-text">
                <strong>流水线等待你审核</strong>
                <small>{pausedTask?.message || '确认后流水线会继续执行'}</small>
              </div>
            </div>
          ) : running ? (
            <div className="task-running">
              <LoaderCircle className="spin" />
              <div>
                <strong>正在处理 {running} 个任务</strong>
                <small>任务将在后台继续</small>
              </div>
            </div>
          ) : (
            <div className="mini-empty">
              <ListChecks />
              <span>
                <strong>队列空闲</strong>
                <small>启动制作后，状态会在这里更新</small>
              </span>
            </div>
          )}
        </section>
      </div>
    </main>
  )
}
