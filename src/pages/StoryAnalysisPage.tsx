import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AlertTriangle,
  ArrowRight,
  BookOpenText,
  CheckCircle2,
  CircleHelp,
  Clock3,
  LoaderCircle,
  MapPin,
  RefreshCw,
  Sparkles,
  Tag,
  Users,
} from 'lucide-react'
import { ApiError, api } from '../api/client'
import type { AnalysisTask, PlotBeat, Project, StoryAnalysis } from '../types'

const runningStatuses = new Set(['queued', 'pending', 'running', 'in_progress'])
const successStatuses = new Set(['completed', 'succeeded'])

function textList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => (typeof item === 'string' ? item : String(item))).filter(Boolean)
}

function normalizeAnalysis(value: StoryAnalysis): StoryAnalysis {
  const raw = value as StoryAnalysis & Record<string, unknown>
  const result = value.result
  return {
    summary: value.summary || result?.summary || String(raw.overview || ''),
    genre: textList(value.genre ?? result?.genres ?? raw.genres),
    audience: textList(value.audience ?? result?.audience ?? raw.target_audience),
    characters: Array.isArray(value.characters) ? value.characters : result?.characters ?? [],
    scenes: Array.isArray(value.scenes) ? value.scenes : result?.scenes ?? [],
    plot_beats: Array.isArray(value.plot_beats) ? value.plot_beats : (result?.beats ?? []) as PlotBeat[],
    risks: textList(value.risks ?? result?.risks),
    open_questions: textList(value.open_questions ?? result?.questions ?? raw.questions),
    updated_at: value.updated_at,
  }
}

export function StoryAnalysisPage({ project }: { project: Project }) {
  const [analysis, setAnalysis] = useState<StoryAnalysis | null>(null)
  const [task, setTask] = useState<AnalysisTask | null>(null)
  const [loading, setLoading] = useState(true)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const timer = useRef<number | null>(null)

  const clearTimer = () => {
    if (timer.current !== null) window.clearTimeout(timer.current)
    timer.current = null
  }

  const loadAnalysis = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true)
    try {
      setAnalysis(normalizeAnalysis(await api.getStoryAnalysis(project.id)))
      setError(null)
    } catch (reason) {
      if (!(reason instanceof ApiError && reason.status === 404)) {
        setError(reason instanceof Error ? reason.message : '故事分析读取失败。')
      }
    } finally {
      if (!quiet) setLoading(false)
    }
  }, [project.id])

  const pollTask = useCallback(async (taskId: string) => {
    clearTimer()
    try {
      const next = await api.getTask(taskId)
      setTask(next)
      if (runningStatuses.has(next.status)) {
        timer.current = window.setTimeout(() => void pollTask(taskId), 1800)
      } else if (successStatuses.has(next.status)) {
        await loadAnalysis(true)
      } else if (next.status === 'failed' || next.status === 'cancelled') {
        setError(next.error || next.message || '分析任务未能完成。')
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '任务状态更新失败。')
    }
  }, [loadAnalysis])

  useEffect(() => {
    void loadAnalysis()
    return clearTimer
  }, [loadAnalysis])

  const start = async () => {
    clearTimer()
    setStarting(true)
    setError(null)
    try {
      const created = await api.createAnalysisTask(project.id)
      setTask(created)
      if (successStatuses.has(created.status)) await loadAnalysis(true)
      else if (runningStatuses.has(created.status)) timer.current = window.setTimeout(() => void pollTask(created.id), 700)
      else if (created.status === 'failed') setError(created.error || created.message || '分析任务启动失败。')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法发起故事分析任务。')
    } finally {
      setStarting(false)
    }
  }

  const running = starting || Boolean(task && runningStatuses.has(task.status))
  const progress = Math.max(0, Math.min(100, task?.progress ?? (running ? 12 : 0)))

  return <main className="analysis-page">
    <section className="analysis-hero">
      <div>
        <span className="eyebrow">故事与剧本 / 智能分析</span>
        <h1>把故事拆成可制作的蓝图</h1>
        <p>识别角色、场景和情节节拍，在进入剧本阶段前暴露风险与待确认问题。</p>
      </div>
      <button type="button" className="button primary" onClick={() => void start()} disabled={running}>
        {running ? <><LoaderCircle className="spin" size={17} /> 分析进行中</> : <><Sparkles size={17} /> {analysis ? '重新分析' : '开始故事分析'}</>}
      </button>
    </section>

    {running && <section className="analysis-status running" aria-live="polite">
      <div>
        <span className="task-symbol"><LoaderCircle className="spin" /></span>
        <div><strong>{task?.message || 'AI 正在理解故事结构…'}</strong><small>任务 ID {task?.id || '正在创建'} · 可以离开页面，任务会在后台继续</small></div>
        <b>{progress}%</b>
      </div>
      <div className="task-progress" role="progressbar" aria-label="分析进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}><i style={{ width: `${Math.max(progress, 6)}%` }} /></div>
    </section>}

    {error && <section className="analysis-status failed" role="alert">
      <AlertTriangle />
      <div><strong>故事分析未完成</strong><p>{error}</p><small>请检查文本模型设置、后端环境变量和网络连接。确认后可安全重试；若任务已提交，后端应按任务 ID 避免重复执行。</small></div>
      <button type="button" className="button ghost" onClick={() => void start()} disabled={starting}><RefreshCw size={16} /> 重试</button>
    </section>}

    {loading
      ? <div className="analysis-loading"><LoaderCircle className="spin" /><span>正在读取已有分析…</span></div>
      : !analysis && !running && !error
        ? <section className="analysis-empty"><span><BookOpenText size={30} /></span><h2>还没有故事分析</h2><p>发起任务后，这里会整理故事概要、角色、场景、情节节拍与创作风险。</p><button type="button" className="button subtle" onClick={() => void start()}><Sparkles size={17} /> 生成第一版分析</button></section>
        : analysis && <AnalysisResult analysis={analysis} />}
  </main>
}

function AnalysisResult({ analysis }: { analysis: StoryAnalysis }) {
  return <div className="analysis-results">
    <section className="analysis-overview result-card">
      <div className="result-heading"><span className="result-icon lavender"><BookOpenText /></span><div><h2>故事概要</h2><small>{analysis.updated_at ? `更新于 ${new Date(analysis.updated_at).toLocaleString('zh-CN')}` : '当前分析版本'}</small></div></div>
      <p>{analysis.summary || '暂无概要。'}</p>
      <div className="tag-groups">
        <div><span><Tag size={14} /> 题材</span><p>{analysis.genre.length ? analysis.genre.map((item) => <em key={item}>{item}</em>) : <small>待确认</small>}</p></div>
        <div><span><Users size={14} /> 目标受众</span><p>{analysis.audience.length ? analysis.audience.map((item) => <em key={item}>{item}</em>) : <small>待确认</small>}</p></div>
      </div>
    </section>

    <div className="analysis-columns">
      <section className="result-card">
        <div className="result-heading"><span className="result-icon mint"><Users /></span><div><h2>角色</h2><small>{analysis.characters.length} 个已识别角色</small></div></div>
        <div className="character-list">{analysis.characters.length ? analysis.characters.map((character, index) => <article key={`${character.name}-${index}`}><span>{character.name?.slice(0, 1) || '?'}</span><div><strong>{character.name || '未命名角色'} {character.role && <em>{character.role}</em>}</strong><p>{character.description || character.goal || '暂无角色描述。'}</p>{(character.goal || character.conflict) && <small>{character.goal && `目标：${character.goal}`}{character.goal && character.conflict && ' · '}{character.conflict && `冲突：${character.conflict}`}</small>}</div></article>) : <EmptyLine />}</div>
      </section>
      <section className="result-card">
        <div className="result-heading"><span className="result-icon amber"><MapPin /></span><div><h2>场景</h2><small>{analysis.scenes.length} 个叙事空间</small></div></div>
        <div className="scene-list">{analysis.scenes.length ? analysis.scenes.map((scene, index) => <article key={`${scene.name}-${index}`}><span>{String(index + 1).padStart(2, '0')}</span><div><strong>{scene.name || scene.title || scene.location || `场景 ${index + 1}`}</strong><small>{[scene.setting, scene.location, scene.time].filter(Boolean).join(' · ')}</small><p>{scene.description || scene.purpose || '暂无场景描述。'}</p></div></article>) : <EmptyLine />}</div>
      </section>
    </div>

    <section className="result-card beat-card">
      <div className="result-heading"><span className="result-icon lavender"><Clock3 /></span><div><h2>情节节拍</h2><small>按叙事顺序梳理关键转折</small></div></div>
      <div className="beat-list">{analysis.plot_beats.length ? analysis.plot_beats.map((beat, index) => <article key={`${beat.title}-${index}`}><span>{beat.order ?? index + 1}</span><i /><div><strong>{beat.title || `节拍 ${index + 1}`}</strong><p>{beat.description}</p></div></article>) : <EmptyLine />}</div>
    </section>

    <div className="analysis-columns concerns">
      <section className="result-card"><div className="result-heading"><span className="result-icon rose"><AlertTriangle /></span><div><h2>风险</h2><small>进入制作前建议处理</small></div></div><ul>{analysis.risks.length ? analysis.risks.map((item) => <li key={item}><AlertTriangle size={15} /><span>{item}</span></li>) : <li className="positive"><CheckCircle2 size={16} /> 暂未发现明确风险</li>}</ul></section>
      <section className="result-card"><div className="result-heading"><span className="result-icon amber"><CircleHelp /></span><div><h2>待确认问题</h2><small>你的选择会影响后续剧本</small></div></div><ul>{analysis.open_questions.length ? analysis.open_questions.map((item) => <li key={item}><CircleHelp size={15} /><span>{item}</span><ArrowRight size={14} /></li>) : <li className="positive"><CheckCircle2 size={16} /> 暂无待确认问题</li>}</ul></section>
    </div>
  </div>
}

function EmptyLine() {
  return <div className="result-empty">暂无结构化内容</div>
}
