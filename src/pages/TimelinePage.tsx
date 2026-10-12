import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, Captions, Clapperboard, Clock3, Film, LoaderCircle, RefreshCw, Volume2, WandSparkles } from 'lucide-react'
import { api } from '../api/client'
import type { Project, ProjectTimeline, TimelineClip, TimelineTrack } from '../types'

const number = (...values: unknown[]) => {
  for (const value of values) {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return 0
}
const clipsOf = (track: TimelineTrack) => track.clips ?? track.items ?? []
const formatTime = (seconds: number) => {
  const safe = Math.max(0, seconds)
  const minutes = Math.floor(safe / 60)
  return `${String(minutes).padStart(2, '0')}:${String(Math.floor(safe % 60)).padStart(2, '0')}`
}
const trackKind = (track: TimelineTrack) => String(track.type ?? track.kind ?? track.name ?? '').toLowerCase()
const clipDuration = (clip: TimelineClip) => number(clip.duration_sec, clip.duration, number(clip.end_sec) - number(clip.start_sec, clip.start))

function normalizeTracks(timeline: ProjectTimeline): TimelineTrack[] {
  if (timeline.tracks?.length) return timeline.tracks
  const candidates: Array<[string, TimelineClip[] | undefined]> = [
    ['video', timeline.video],
    ['audio', timeline.audio ?? timeline.dialogue],
    ['subtitles', timeline.subtitles],
  ]
  return candidates
    .filter((entry): entry is [string, TimelineClip[]] => Boolean(entry[1]?.length))
    .map(([kind, clips]) => ({ type: kind, name: kind, clips }))
}

export function TimelinePage({ project }: { project: Project }) {
  const [timeline, setTimeline] = useState<ProjectTimeline | null>(null)
  const [loading, setLoading] = useState(true)
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setTimeline(await api.getTimeline(project.id))
    } catch (reason) {
      const status = (reason as { status?: number }).status
      if (status === 404) setTimeline(null)
      else setError(reason instanceof Error ? reason.message : '时间线读取失败。')
    } finally {
      setLoading(false)
    }
  }, [project.id])

  useEffect(() => { void load() }, [load])

  const generate = async () => {
    setGenerating(true)
    setError(null)
    try {
      setTimeline(await api.generateTimeline(project.id))
      await load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '时间线生成失败。')
    } finally {
      setGenerating(false)
    }
  }

  const tracks = useMemo(() => timeline ? normalizeTracks(timeline) : [], [timeline])
  const total = useMemo(() => {
    const declared = number(timeline?.total_duration_sec, timeline?.duration_sec, timeline?.duration_seconds)
    if (declared) return declared
    return Math.max(0, ...tracks.flatMap((track) => clipsOf(track).map((clip) => number(clip.start_sec, clip.start) + clipDuration(clip))))
  }, [timeline, tracks])
  const icon = (kind: string) => kind.includes('subtitle') || kind.includes('caption') ? Captions : kind.includes('audio') || kind.includes('dialog') || kind.includes('voice') ? Volume2 : Clapperboard
  const name = (kind: string) => kind.includes('subtitle') || kind.includes('caption') ? '字幕' : kind.includes('audio') || kind.includes('dialog') || kind.includes('voice') ? '对白 / 音频' : '视频'

  return <main className="studio-page timeline-page">
    <section className="studio-hero">
      <div><span className="eyebrow">LIGHTWEIGHT TIMELINE</span><h1>时间线</h1><p>按视频、对白或音频、字幕轨道查看自动编排结果。这是只读预览，不提供虚假的拖拽或剪辑功能。</p></div>
      <div className="hero-actions"><button type="button" className="button ghost" onClick={() => void load()} disabled={loading}><RefreshCw size={16} />刷新</button><button type="button" className="button primary" onClick={() => void generate()} disabled={generating}>{generating ? <LoaderCircle className="spin" size={16} /> : <WandSparkles size={16} />}生成 / 刷新时间线</button></div>
    </section>

    {error && <div className="studio-alert" role="alert"><AlertTriangle /><span><strong>时间线操作失败</strong><small>{error}</small></span><button type="button" onClick={() => void load()}><RefreshCw size={14} />重试</button></div>}

    {loading
      ? <div className="studio-loading"><LoaderCircle className="spin" />正在读取时间线…</div>
      : !timeline || tracks.length === 0
        ? <section className="studio-empty"><Film /><h2>尚未生成时间线</h2><p>先准备媒体素材，再由系统建立轻量、只读的轨道预览。</p><button type="button" className="button primary" onClick={() => void generate()} disabled={generating}>生成时间线</button></section>
        : <section className="timeline-board">
          <header><div><strong>时间线概览</strong><span>v{timeline.version ?? 1} · {timeline.status ?? 'ready'}</span></div><div className="timeline-duration"><Clock3 size={17} /><span>总时长</span><strong>{formatTime(total)}</strong></div></header>
          <div className="timeline-ruler" aria-hidden="true"><span /><div>{[0, 0.25, 0.5, 0.75, 1].map((ratio) => <i key={ratio} style={{ left: `${ratio * 100}%` }}>{formatTime(total * ratio)}</i>)}</div></div>
          <div className="timeline-tracks">{tracks.map((track, index) => {
            const kind = trackKind(track)
            const Icon = icon(kind)
            const clips = clipsOf(track)
            return <div className={`timeline-track track-${kind}`} key={track.id ?? `${kind}-${index}`}>
              <div className="track-label"><Icon size={17} /><span><strong>{track.name ?? name(kind)}</strong><small>{clips.length} 个片段</small></span></div>
              <div className="track-lane">{clips.map((clip, clipIndex) => {
                const start = number(clip.start_sec, clip.start)
                const duration = clipDuration(clip)
                const title = clip.title ?? clip.name ?? clip.text ?? `片段 ${clipIndex + 1}`
                return <div className="timeline-clip" key={clip.id ?? `${index}-${clipIndex}`} style={{ left: `${total ? start / total * 100 : 0}%`, width: `${Math.max(total ? duration / total * 100 : 3, 2)}%` }} title={`${title} · ${formatTime(duration)}`}><strong>{title}</strong><small>{formatTime(start)} · {formatTime(duration)}</small></div>
              })}</div>
            </div>
          })}</div>
          <footer><AlertTriangle size={15} /><span>当前为自动编排结果的只读可视化；如需调整，请回到分镜与素材阶段后重新生成。</span></footer>
        </section>}
  </main>
}
