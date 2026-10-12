import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, Ban, CircleDollarSign, Filter, ListChecks, LoaderCircle, PauseCircle, Play, RefreshCw, RotateCcw } from 'lucide-react'
import { api } from '../api/client'
import { ConfirmDialog } from '../components/ConfirmDialog'
import type { Project, ProjectTask } from '../types'

const runningStatuses = new Set(['queued', 'pending', 'running', 'processing', 'in_progress'])
const pausedStatuses = new Set(['paused'])
const retryableStatuses = new Set(['failed', 'cancelled', 'canceled'])
/** 需要自动刷新界面的状态：正在执行与等待人工审核。 */
const liveStatuses = new Set([...runningStatuses, ...pausedStatuses])
const POLL_INTERVAL_MS = 3000
const statusLabels: Record<string, string> = {
  queued: '排队中',
  pending: '等待中',
  running: '运行中',
  processing: '处理中',
  in_progress: '进行中',
  paused: '等待审核',
  completed: '已完成',
  succeeded: '已完成',
  success: '已完成',
  failed: '失败',
  cancelled: '已取消',
  canceled: '已取消',
  unknown: '未知',
}

/** 任务类型的中文名；未知类型保留后端原始标识，不隐藏也不编造。 */
const taskTypeLabels: Record<string, string> = {
  shot_image: '镜头画面生成',
  shot_video: '镜头视频生成',
  speech: '配音生成',
}

const taskTypeLabel = (task: ProjectTask): string => {
  const raw = task.task_type || task.type
  if (!raw) return '未标注类型'
  return taskTypeLabels[raw] ?? raw
}

function normalizeStatus(value: unknown): string {
  return typeof value === 'string' && value.trim() ? value.trim().toLowerCase() : 'unknown'
}

function progressPercent(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 0
  return Math.max(0, Math.min(100, Math.round(value <= 1 ? value * 100 : value)))
}

function formatMoney(value: unknown, currency: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  const code = typeof currency === 'string' && /^[A-Za-z]{3}$/.test(currency.trim()) ? currency.trim().toUpperCase() : 'CNY'
  try {
    return new Intl.NumberFormat('zh-CN', { style: 'currency', currency: code }).format(value)
  } catch {
    return new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(value)
  }
}

function formatDate(value: unknown): string | null {
  if (typeof value !== 'string' || !value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}

export function TasksPage({ project }: { project: Project }) {
  const [tasks, setTasks] = useState<ProjectTask[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState('all')
  const [busy, setBusy] = useState<string | null>(null)
  const [pendingCancel, setPendingCancel] = useState<ProjectTask | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setTasks(await api.getProjectTasks(project.id))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '任务读取失败。')
    } finally {
      setLoading(false)
    }
  }, [project.id])

  useEffect(() => { void load() }, [load])

  const hasLiveTasks = useMemo(() => tasks.some((task) => liveStatuses.has(normalizeStatus(task.status))), [tasks])

  // 有正在执行或等待审核的任务时每 3 秒静默刷新，任务结束后停止；卸载时清理定时器。
  useEffect(() => {
    if (!hasLiveTasks) return
    let cancelled = false
    const timer = window.setInterval(() => {
      if (cancelled) return
      void api.getProjectTasks(project.id)
        .then((next) => { if (!cancelled) setTasks(next) })
        .catch(() => { /* 轮询失败保持现有列表，避免打断操作 */ })
    }, POLL_INTERVAL_MS)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [hasLiveTasks, project.id])

  const filtered = useMemo(() => tasks.filter((task) => {
    const status = normalizeStatus(task.status)
    if (filter === 'all') return true
    if (filter === 'running') return runningStatuses.has(status)
    if (filter === 'paused') return pausedStatuses.has(status)
    return status === filter
  }), [tasks, filter])

  const performAction = async (task: ProjectTask, kind: 'cancel' | 'retry' | 'resume') => {
    setPendingCancel(null)
    setBusy(task.id)
    setError(null)
    try {
      if (kind === 'cancel') await api.cancelTask(task.id)
      else if (kind === 'retry') await api.retryTask(task.id)
      else await api.resumeTask(task.id)
      await load()
    } catch (reason) {
      const label = kind === 'cancel' ? '取消' : kind === 'retry' ? '重试' : '继续'
      setError(reason instanceof Error ? reason.message : `任务${label}失败。`)
    } finally {
      setBusy(null)
    }
  }

  return (
    <main className="studio-page tasks-page">
      <section className="studio-hero">
        <div>
          <span className="eyebrow">OPERATIONS</span>
          <h1>任务中心</h1>
          <p>查看后端实际任务状态、进度、消息、成本与执行次数。</p>
        </div>
        <button type="button" className="button ghost" onClick={() => void load()} disabled={loading}>
          <RefreshCw className={loading ? 'spin' : ''} size={16} /> 刷新
        </button>
      </section>

      <div className="task-toolbar">
        <Filter size={16} />
        <label>
          状态筛选
          <select value={filter} onChange={(event) => setFilter(event.target.value)}>
            <option value="all">全部</option>
            <option value="running">运行中</option>
            <option value="paused">等待审核</option>
            <option value="completed">已完成</option>
            <option value="succeeded">成功</option>
            <option value="failed">失败</option>
            <option value="cancelled">已取消</option>
          </select>
        </label>
        <span>{filtered.length} / {tasks.length} 个任务</span>
      </div>

      {error && (
        <div className="studio-alert">
          <AlertTriangle />
          <span style={{ minWidth: 0 }}><strong>任务操作失败</strong><small style={{ overflowWrap: 'anywhere' }}>{error}</small></span>
          <button type="button" onClick={() => void load()}><RefreshCw size={14} /> 重试</button>
        </div>
      )}

      {loading ? (
        <div className="studio-loading"><LoaderCircle className="spin" /> 正在读取任务…</div>
      ) : filtered.length === 0 ? (
        <div className="studio-empty">
          <ListChecks />
          <h2>{tasks.length ? '当前筛选没有任务' : '尚无任务记录'}</h2>
          <p>这里只展示后端返回的真实任务，不会生成占位状态。</p>
        </div>
      ) : (
        <div className="task-list">
          {filtered.map((task) => {
            const status = normalizeStatus(task.status)
            const progress = progressPercent(task.progress)
            // 失败任务常常同时带着上次的进度消息和真正的错误原因。以前只显示 message，
            // 真正的失败原因会被丢掉，所以这里两者分别显示，绝不互相覆盖。
            const message = task.message || task.error
            const failure = task.error && task.error !== message ? task.error : null
            const updatedAt = formatDate(task.updated_at)
            const isPaused = pausedStatuses.has(status)
            return (
              <article className={`task-row${isPaused ? ' task-paused' : ''}`} key={task.id}>
                <div className={`task-state ${status}`}>
                  {runningStatuses.has(status) ? <LoaderCircle className="spin" /> : isPaused ? <PauseCircle /> : <ListChecks />}
                </div>
                <div className="task-detail" style={{ minWidth: 0 }}>
                  <header>
                    <div style={{ minWidth: 0 }}>
                      <strong style={{ overflowWrap: 'anywhere' }}>{taskTypeLabel(task)}</strong>
                      <span className={`status-chip ${status}`}>{statusLabels[status] || String(task.status || '未知')}</span>
                    </div>
                    <small style={{ overflowWrap: 'anywhere' }}>任务 {task.id}</small>
                  </header>
                  {(runningStatuses.has(status) || isPaused) && <div className="task-line"><i style={{ width: `${progress}%` }} /></div>}
                  <p style={{ overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}>{message || (isPaused ? '流水线已暂停，等待你确认后继续。' : '后端未提供任务消息')}</p>
                  {failure && <p className="task-error" style={{ whiteSpace: 'pre-wrap' }}><AlertTriangle size={14} /> {failure}</p>}
                  <footer>
                    <span>进度 <b>{typeof task.progress === 'number' && Number.isFinite(task.progress) ? `${progress}%` : '—'}</b></span>
                    <span><CircleDollarSign size={13} /> 成本 <b>{formatMoney(task.cost ?? task.actual_cost ?? task.estimated_cost, task.currency)}</b></span>
                    <span>执行 <b>第 {task.attempt ?? task.attempts ?? 1} 次</b>{task.max_attempts ? ` / ${task.max_attempts}` : ''}</span>
                    {updatedAt && <time style={{ overflowWrap: 'anywhere' }}>{updatedAt}</time>}
                  </footer>
                </div>
                <div className="task-actions">
                  {isPaused && (
                    <button type="button" className="button primary" disabled={busy === task.id} onClick={() => void performAction(task, 'resume')}>
                      {busy === task.id ? <LoaderCircle className="spin" size={15} /> : <Play size={15} />} 继续
                    </button>
                  )}
                  {runningStatuses.has(status) && (
                    <button type="button" className="button ghost danger" disabled={busy === task.id} onClick={() => setPendingCancel(task)}>
                      <Ban size={15} /> 取消
                    </button>
                  )}
                  {retryableStatuses.has(status) && (
                    <button type="button" className="button subtle" disabled={busy === task.id} onClick={() => void performAction(task, 'retry')}>
                      <RotateCcw size={15} /> 重试
                    </button>
                  )}
                </div>
              </article>
            )
          })}
        </div>
      )}

      <ConfirmDialog
        open={Boolean(pendingCancel)}
        title="取消这个任务？"
        description={pendingCancel
          ? `任务 ${pendingCancel.id} 正在执行，取消会立即停止后续工作，已产生的进度与费用不会退回，未完成的产物需要重新发起任务，此操作不可撤销。`
          : ''}
        confirmLabel="确认取消任务"
        tone="danger"
        busy={Boolean(pendingCancel && busy === pendingCancel.id)}
        onConfirm={() => { if (pendingCancel) void performAction(pendingCancel, 'cancel') }}
        onCancel={() => setPendingCancel(null)}
      />
    </main>
  )
}
