import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, Check, CheckCircle2, ChevronDown, Clapperboard, Edit3, FileAudio, Grid2X2, Image as ImageIcon, List, LoaderCircle, RefreshCw, Sparkles, Video, X } from 'lucide-react'
import { api } from '../api/client'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { useDialog } from '../lib/dialog'
import type { Episode, Project, Scene, Shot } from '../types'

const label = (value?: string) => value?.replaceAll('_', ' ') || '未设置'
const duration = (shot: Shot) => shot.duration_sec ?? shot.duration_seconds ?? (shot.duration_ticks ? shot.duration_ticks / 25 : 0)
/** 页面级生成操作的类型：plan/approve 沿用原有逻辑，images/speech/videos 为后端真实的新增媒体生成任务。 */
type StoryboardBusy = 'plan' | 'approve' | 'images' | 'speech' | 'videos' | null
type StoryboardConfirm = 'plan' | 'approve' | 'images' | 'speech' | 'videos' | null
/** 单个镜头正在进行的生成操作：图像与视频各自独立显示 spinner，二者都会阻塞其它生成按钮。 */
type ShotGenerate = { shotId: string; kind: 'image' | 'video' } | null

export function StoryboardPage({ project, onChanged }: { project: Project; onChanged?: () => void }) {
  const [episodes, setEpisodes] = useState<Episode[]>([])
  const [selected, setSelected] = useState('')
  const [view, setView] = useState<'cards' | 'table'>('cards')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<StoryboardBusy>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [editing, setEditing] = useState<Shot | null>(null)
  const [confirming, setConfirming] = useState<StoryboardConfirm>(null)
  // 每个镜头独立的生成状态：只有被点击的那个镜头（以及那张按钮）会禁用并显示 spinner。
  const [generatingShot, setGeneratingShot] = useState<ShotGenerate>(null)
  const noticeTimer = useRef<number | null>(null)

  const flashNotice = useCallback((message: string) => {
    setNotice(message)
    if (noticeTimer.current !== null) window.clearTimeout(noticeTimer.current)
    noticeTimer.current = window.setTimeout(() => { setNotice(null); noticeTimer.current = null }, 8000)
  }, [])

  useEffect(() => () => { if (noticeTimer.current !== null) window.clearTimeout(noticeTimer.current) }, [])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await api.getEpisodes(project.id)
      setEpisodes(data)
      setSelected((old) => data.some((episode) => episode.id === old) ? old : data[0]?.id || '')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '分镜读取失败。')
    } finally {
      setLoading(false)
    }
  }, [project.id])

  useEffect(() => { void load() }, [load])

  const episode = episodes.find((item) => item.id === selected) ?? episodes[0]
  const scenes = useMemo<Scene[]>(() => episode?.scenes?.length
    ? episode.scenes
    : episode ? [{ id: `${episode.id}-all`, title: '全部镜头', shots: episode.shots ?? [] }] : [], [episode])
  const allShots = scenes.flatMap((scene) => scene.shots ?? [])
  // 只用于确认框文案：后端与剧集数据都没有镜头数时保持 undefined，界面不编造数字。
  const countedShots = episodes.reduce((sum, item) => sum + (item.shots?.length ?? 0) + (item.scenes ?? []).reduce((inner, scene) => inner + (scene.shots?.length ?? 0), 0), 0)
  const projectShotCount = project.shot_count ?? (countedShots || undefined)

  const generate = async () => {
    setConfirming(null)
    setBusy('plan')
    setError(null)
    try {
      await api.generateProductionPlan(project.id)
      await load()
      onChanged?.()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '生产计划生成失败。')
    } finally {
      setBusy(null)
    }
  }

  const approve = async () => {
    setConfirming(null)
    setBusy('approve')
    setError(null)
    try {
      await api.approveShots(project.id, allShots.map((shot) => shot.id))
      await load()
      onChanged?.()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '镜头批准失败。')
    } finally {
      setBusy(null)
    }
  }

  /** 单镜头生成画面：POST /projects/{id}/shots/{shot_id}/image，后端返回任务并后台执行。 */
  const generateImageForShot = async (shot: Shot) => {
    setGeneratingShot({ shotId: shot.id, kind: 'image' })
    setError(null)
    try {
      const task = await api.generateShotImage(project.id, shot.id)
      await load()
      onChanged?.()
      flashNotice(`镜头画面生成任务已提交（任务 ${task.id}），可在任务中心查看进度；生成完成的素材会以「AI 生成」标记出现在素材库。`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '镜头画面生成任务提交失败。')
    } finally {
      setGeneratingShot(null)
    }
  }

  /**
   * 单镜头图生视频：POST /projects/{id}/shots/{shot_id}/video。
   * 后端会用该镜头已有的图像素材作为首帧；镜头没有图像时任务会失败并给出中文原因。
   * provider 为 demo 时后端用本机 FFmpeg 渲染**占位片段**（真实可解码、标记为占位，不是 AI 生成）；
   * 没有可用的 FFmpeg 时任务失败并说明缺什么。
   * 这里只如实展示后端返回的错误，不假装成功。
   */
  const generateVideoForShot = async (shot: Shot) => {
    setGeneratingShot({ shotId: shot.id, kind: 'video' })
    setError(null)
    try {
      const task = await api.generateShotVideo(project.id, shot.id)
      await load()
      onChanged?.()
      flashNotice(`镜头视频生成任务已提交（任务 ${task.id}）。图生视频是异步轮询执行的，可能耗时数十秒到数分钟，请在任务中心查看进度；任务消息会显示「第 n/N 个镜头：生成中 xx%」，完成后到素材库查看视频。`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '镜头视频生成任务提交失败。')
    } finally {
      setGeneratingShot(null)
    }
  }

  /** 页面级：为项目全部镜头生成画面（不传 shot_ids，由后端按项目全部镜头展开）。 */
  const generateAllImages = async () => {
    setConfirming(null)
    setBusy('images')
    setError(null)
    try {
      const task = await api.generateProjectImages(project.id)
      onChanged?.()
      flashNotice(`全部镜头画面生成任务已提交（任务 ${task.id}）。任务在后台执行，可在任务中心查看进度，完成后到素材库查看结果。`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '全部镜头画面生成任务提交失败。')
    } finally {
      setBusy(null)
    }
  }

  /** 页面级：为项目全部镜头生成配音。 */
  const generateAllSpeech = async () => {
    setConfirming(null)
    setBusy('speech')
    setError(null)
    try {
      const task = await api.generateProjectSpeech(project.id)
      onChanged?.()
      flashNotice(`配音生成任务已提交（任务 ${task.id}）。任务在后台执行，可在任务中心查看进度，完成后到素材库查看结果。`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '配音生成任务提交失败。')
    } finally {
      setBusy(null)
    }
  }

  /**
   * 页面级：为项目全部镜头生成视频（不传 shot_ids，由后端按项目全部镜头逐个展开）。
   * 后端逐个镜头异步轮询执行，耗时可能是图片的数倍；视频能力为 demo 时，后端用本机 FFmpeg
   * 渲染占位片段（真实可解码、标记为占位而不是 AI 生成），没有可用 FFmpeg 时任务会失败。
   */
  const generateAllVideos = async () => {
    setConfirming(null)
    setBusy('videos')
    setError(null)
    try {
      const task = await api.generateProjectVideos(project.id)
      onChanged?.()
      flashNotice(`全部镜头视频生成任务已提交（任务 ${task.id}）。视频按镜头逐个生成，可能耗时数分钟，请在任务中心查看进度（消息会显示「第 n/N 个镜头：生成中 xx%」），完成后到素材库查看视频。`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '全部镜头视频生成任务提交失败。')
    } finally {
      setBusy(null)
    }
  }

  const shotGenerating = generatingShot !== null

  return <main className="studio-page storyboard-page">
    <section className="studio-hero">
      <div><span className="eyebrow">分镜工作台 / 生产计划</span><h1>把剧本变成可执行的镜头</h1><p>按集与场景检查节奏、机位、运镜、动作和对白，批准后进入素材生产。</p></div>
      <div className="hero-actions">
        <button type="button" className="button ghost" disabled={Boolean(busy)} onClick={() => setConfirming('plan')}>{busy === 'plan' ? <LoaderCircle className="spin" size={16} /> : <Sparkles size={16} />} {episodes.length ? '重新生成计划' : '生成生产计划'}</button>
        <button type="button" className="button ghost" disabled={!allShots.length || Boolean(busy) || shotGenerating} onClick={() => setConfirming('images')}>{busy === 'images' ? <LoaderCircle className="spin" size={16} /> : <ImageIcon size={16} />}为全部镜头生成画面</button>
        <button type="button" className="button ghost" disabled={!allShots.length || Boolean(busy) || shotGenerating} onClick={() => setConfirming('speech')}>{busy === 'speech' ? <LoaderCircle className="spin" size={16} /> : <FileAudio size={16} />}生成配音</button>
        <button type="button" className="button ghost" disabled={!allShots.length || Boolean(busy) || shotGenerating} onClick={() => setConfirming('videos')}>{busy === 'videos' ? <LoaderCircle className="spin" size={16} /> : <Video size={16} />}为全部镜头生成视频</button>
        <button type="button" className="button primary" disabled={!allShots.length || Boolean(busy)} onClick={() => setConfirming('approve')}>{busy === 'approve' ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}批准全部镜头</button>
      </div>
    </section>

    {notice && <div className="studio-success" role="status"><CheckCircle2 size={16} /> {notice}</div>}
    {error && <div className="studio-alert" role="alert"><AlertTriangle /><span><strong>分镜操作未完成</strong><small>{error}</small></span><button type="button" onClick={() => void load()}><RefreshCw size={15} />重试</button></div>}

    {loading
      ? <div className="studio-loading"><LoaderCircle className="spin" />正在读取剧集与镜头…</div>
      : !episodes.length
        ? <section className="studio-empty"><Clapperboard /><h2>还没有生产计划</h2><p>从已批准内容生成剧集、场景和镜头列表，然后逐镜调整并审核。</p><button type="button" className="button subtle" onClick={() => void generate()} disabled={Boolean(busy)}><Sparkles size={16} />生成剧本分镜</button></section>
        : <>
          <section className="shot-toolbar">
            <label>当前剧集<select value={episode?.id ?? ''} onChange={(event) => setSelected(event.target.value)}>{episodes.map((item, index) => <option key={item.id} value={item.id}>第 {item.number ?? item.order ?? index + 1} 集 · {item.title || item.name || '未命名'}</option>)}</select><ChevronDown size={15} /></label>
            <span>{scenes.length} 场 · {allShots.length} 镜头 · 约 {allShots.reduce((sum, shot) => sum + duration(shot), 0).toFixed(0)} 秒</span>
            <div role="group" aria-label="分镜视图"><button type="button" className={view === 'cards' ? 'active' : ''} aria-pressed={view === 'cards'} onClick={() => setView('cards')}><Grid2X2 size={16} />卡片</button><button type="button" className={view === 'table' ? 'active' : ''} aria-pressed={view === 'table'} onClick={() => setView('table')}><List size={16} />表格</button></div>
          </section>
          {scenes.map((scene, sceneIndex) => <section className="scene-block" key={scene.id}>
            <header><div><small>SCENE {String(scene.order ?? sceneIndex + 1).padStart(2, '0')}</small><h2>{scene.title || scene.name || scene.location || `场景 ${sceneIndex + 1}`}</h2><p>{scene.description || scene.location || '暂无场景说明'}</p></div><span>{scene.shots?.length ?? 0} 镜头</span></header>
            {view === 'cards'
              ? <div className="shot-grid">{(scene.shots ?? []).map((shot, index) => <ShotCard key={shot.id} shot={shot} index={index} onEdit={() => setEditing(shot)} onGenerateImage={() => void generateImageForShot(shot)} onGenerateVideo={() => void generateVideoForShot(shot)} generatingKind={generatingShot?.shotId === shot.id ? generatingShot.kind : null} disabled={shotGenerating || Boolean(busy)} />)}</div>
              : <ShotTable shots={scene.shots ?? []} onEdit={setEditing} onGenerateImage={(shot) => void generateImageForShot(shot)} onGenerateVideo={(shot) => void generateVideoForShot(shot)} generating={generatingShot} disabled={Boolean(busy)} />}
          </section>)}
        </>}

    {editing && <ShotEditor shot={editing} onClose={() => setEditing(null)} onSaved={(saved) => {
      setEditing(null)
      setEpisodes((old) => old.map((item) => ({
        ...item,
        scenes: item.scenes?.map((scene) => ({ ...scene, shots: scene.shots?.map((shot) => shot.id === saved.id ? saved : shot) })),
        shots: item.shots?.map((shot) => shot.id === saved.id ? saved : shot),
      })))
    }} />}

    <ConfirmDialog
      open={confirming === 'plan'}
      title={episodes.length ? '重新生成生产计划？' : '生成生产计划？'}
      description={episodes.length
        ? `将按当前已批准的故事内容重新生成剧集、场景与镜头，现有 ${episodes.length} 集的镜头列表会被新计划覆盖，你手动调整过的镜头目的、动作与时长不会保留，此操作不可撤销。`
        : '将根据已批准的故事内容生成剧集、场景与镜头列表，可能需要等待一段时间并产生模型调用费用。'}
      confirmLabel={episodes.length ? '确认重新生成' : '确认生成'}
      busy={busy === 'plan'}
      onConfirm={() => void generate()}
      onCancel={() => setConfirming(null)}
    />
    <ConfirmDialog
      open={confirming === 'approve'}
      title="批准全部镜头？"
      description={`将批准当前剧集的全部 ${allShots.length} 个镜头。批准后会锁定分镜并进入素材生产，可能触发模型调用与费用；如需再修改需要重新走审核流程，此操作不可撤销。`}
      confirmLabel={`确认批准 ${allShots.length} 个镜头`}
      busy={busy === 'approve'}
      onConfirm={() => void approve()}
      onCancel={() => setConfirming(null)}
    />
    <ConfirmDialog
      open={confirming === 'images'}
      title="为全部镜头生成画面？"
      description={`将对本项目全部镜头的图像能力发起一次后台生成任务${projectShotCount ? `（项目共约 ${projectShotCount} 个镜头，以后端实际镜头为准）` : ''}，当前剧集有 ${allShots.length} 个镜头。图像能力的 provider 不是 demo 时，这会真实调用生成式服务并产生实际费用（按你在「模型服务」里填写的单次调用预估成本预留预算）；provider 为 demo 时只会生成明确标记的本地占位图，不是 AI 生成。任务在后台执行，提交后可在任务中心查看进度。`}
      confirmLabel="确认生成全部画面"
      busy={busy === 'images'}
      onConfirm={() => void generateAllImages()}
      onCancel={() => setConfirming(null)}
    />
    <ConfirmDialog
      open={confirming === 'speech'}
      title="为全部镜头生成配音？"
      description={`将对本项目全部镜头的语音能力发起一次后台生成任务${projectShotCount ? `（项目共约 ${projectShotCount} 个镜头，以后端实际镜头为准）` : ''}，当前剧集有 ${allShots.length} 个镜头。语音能力的 provider 不是 demo 时，这会真实调用语音服务并产生实际费用（按你在「模型服务」里填写的单次调用预估成本预留预算）；provider 为 demo 时只会生成明确标记的本地占位音频，不是 AI 生成。任务在后台执行，提交后可在任务中心查看进度。`}
      confirmLabel="确认生成配音"
      busy={busy === 'speech'}
      onConfirm={() => void generateAllSpeech()}
      onCancel={() => setConfirming(null)}
    />
    <ConfirmDialog
      open={confirming === 'videos'}
      title="为全部镜头生成视频？"
      description={`视频按镜头逐个生成：将对本项目全部镜头发起一次后台生成任务${projectShotCount ? `（项目共约 ${projectShotCount} 个镜头，以后端实际镜头为准）` : ''}，当前剧集有 ${allShots.length} 个镜头。后端逐个镜头异步轮询执行，整个过程可能耗时数分钟甚至更久，期间任务消息会更新为「第 n/N 个镜头：生成中 xx%」。视频能力的 provider 不是 demo 时，这会真实调用图生视频服务，并按你填写的单次预估成本（unit_cost）对每个镜头产生实际费用——视频单价与耗时都明显高于图片，请先确认单价填对了；provider 仍是 demo 时，后端会用本机 FFmpeg 把该镜头已有的画面渲染成**占位片段**（真实可解码的 MP4，标记为占位而不是 AI 生成，也不产生费用），若本机没有可用的 FFmpeg，这次任务会失败并说明缺什么，错误原因会显示在任务中心和本页。任务在后台执行，提交后可在任务中心查看进度。`}
      confirmLabel="确认生成全部视频"
      busy={busy === 'videos'}
      onConfirm={() => void generateAllVideos()}
      onCancel={() => setConfirming(null)}
    />
  </main>
}

function ShotCard({ shot, index, onEdit, onGenerateImage, onGenerateVideo, generatingKind, disabled }: {
  shot: Shot
  index: number
  onEdit: () => void
  onGenerateImage: () => void
  onGenerateVideo: () => void
  generatingKind: 'image' | 'video' | null
  disabled: boolean
}) {
  const characters = shot.characters ?? shot.character_refs?.map((item) => item.name || item.entity_id || '角色') ?? []
  const title = shot.title || shot.purpose || `镜头 ${index + 1}`
  // 没有图像素材时不隐藏视频按钮（失败原因由后端给出并展示），但先说明建议先生成画面。
  const videoHint = shot.thumbnail_url ? '' : '（该镜头还没有图像素材；视频以已有图像为首帧，建议先生成画面）'
  return <article className="shot-card">
    <div className="shot-image">{shot.thumbnail_url ? <img src={shot.thumbnail_url} alt={`${title}预览`} /> : <Clapperboard aria-hidden="true" />}<span>{String(shot.number ?? shot.order ?? index + 1).padStart(2, '0')}</span><em>{duration(shot).toFixed(1)}s</em></div>
    <div className="shot-body"><div><strong>{title}</strong><button type="button" onClick={onEdit} aria-label={`编辑${title}`}><Edit3 size={14} /></button></div><p>{shot.action || '暂无动作描述'}</p><div className="shot-meta"><span>{label(shot.shot_size)}</span><span>{label(shot.camera_angle)}</span><span>{label(shot.camera_motion)}</span></div>{characters.length > 0 && <small>{characters.join(' · ')}</small>}<footer><span className={`shot-status ${shot.status || 'draft'}`}>{shot.status || 'draft'}</span><em>{label(shot.render_strategy)}</em></footer>
      <div className="shot-actions">
        <button type="button" onClick={onGenerateImage} disabled={disabled} aria-label={`为${title}生成画面`}>
          {generatingKind === 'image' ? <LoaderCircle className="spin" size={13} /> : <ImageIcon size={13} />}{generatingKind === 'image' ? '生成中' : '生成画面'}
        </button>
        <button type="button" onClick={onGenerateVideo} disabled={disabled} title={`为${title}生成视频${videoHint}`} aria-label={`为${title}生成视频（图生视频，以后端返回结果为准）${videoHint}`}>
          {generatingKind === 'video' ? <LoaderCircle className="spin" size={13} /> : <Video size={13} />}{generatingKind === 'video' ? '生成中' : '生成视频'}
        </button>
      </div>
    </div>
  </article>
}

function ShotTable({ shots, onEdit, onGenerateImage, onGenerateVideo, generating, disabled }: {
  shots: Shot[]
  onEdit: (shot: Shot) => void
  onGenerateImage: (shot: Shot) => void
  onGenerateVideo: (shot: Shot) => void
  generating: ShotGenerate
  disabled: boolean
}) {
  return <div className="shot-table"><table><thead><tr><th>#</th><th>镜头目的 / 动作</th><th>时长</th><th>景别</th><th>机位</th><th>运镜</th><th>状态</th><th><span className="sr-only">操作</span></th></tr></thead><tbody>{shots.map((shot, index) => {
    const title = shot.purpose || shot.title || '未命名镜头'
    const generatingKind = generating?.shotId === shot.id ? generating.kind : null
    const videoHint = shot.thumbnail_url ? '' : '（该镜头还没有图像素材；视频以已有图像为首帧，建议先生成画面）'
    return <tr key={shot.id}><td>{shot.number ?? shot.order ?? index + 1}</td><td><strong>{title}</strong><small>{shot.action || '暂无动作描述'}</small></td><td>{duration(shot).toFixed(1)}s</td><td>{label(shot.shot_size)}</td><td>{label(shot.camera_angle)}</td><td>{label(shot.camera_motion)}</td><td><span className={`shot-status ${shot.status || 'draft'}`}>{shot.status || 'draft'}</span></td><td><div className="shot-actions"><button type="button" onClick={() => onGenerateImage(shot)} disabled={disabled || generating !== null} aria-label={`为${title}生成画面`}>{generatingKind === 'image' ? <LoaderCircle className="spin" size={13} /> : <ImageIcon size={13} />}{generatingKind === 'image' ? '生成中' : '生成画面'}</button><button type="button" onClick={() => onGenerateVideo(shot)} disabled={disabled || generating !== null} title={`为${title}生成视频${videoHint}`} aria-label={`为${title}生成视频${videoHint}`}>{generatingKind === 'video' ? <LoaderCircle className="spin" size={13} /> : <Video size={13} />}{generatingKind === 'video' ? '生成中' : '生成视频'}</button><button type="button" onClick={() => onEdit(shot)} aria-label={`编辑${title}`}><Edit3 size={15} /></button></div></td></tr>
  })}</tbody></table></div>
}

function ShotEditor({ shot, onClose, onSaved }: { shot: Shot; onClose: () => void; onSaved: (shot: Shot) => void }) {
  return <ShotEditorDrawer shot={shot} onClose={onClose} onSaved={onSaved} />
}

function ShotEditorDrawer({ shot, onClose, onSaved }: { shot: Shot; onClose: () => void; onSaved: (shot: Shot) => void }) {
  const [form, setForm] = useState({ ...shot })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  // Esc 关闭 / 焦点圈定在抽屉内 / 关闭后焦点回到触发编辑的按钮。
  const dialogRef = useDialog<HTMLElement>(onClose, { blocked: saving })
  const patch = (key: keyof Shot, value: unknown) => setForm((old) => ({ ...old, [key]: value }))
  // 保存中一律不能关闭：否则请求仍在跑，界面却切到别的镜头，用户会以为改动丢了。
  const requestClose = () => { if (!saving) onClose() }

  const save = async () => {
    setSaving(true)
    setError('')
    try {
      onSaved(await api.updateShot(shot.id, {
        purpose: form.purpose,
        action: form.action,
        duration_sec: Number(form.duration_sec ?? form.duration_seconds ?? 0),
        shot_size: form.shot_size,
        camera_angle: form.camera_angle,
        camera_motion: form.camera_motion,
        render_strategy: form.render_strategy,
      }))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败。')
    } finally {
      setSaving(false)
    }
  }

  return <div className="drawer-backdrop" onMouseDown={(event) => event.target === event.currentTarget && requestClose()}>
  <aside className="shot-drawer" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="shot-editor-title">
    <header><div><small>镜头编辑</small><h2 id="shot-editor-title">{shot.title || shot.purpose || shot.id}</h2></div><button type="button" onClick={requestClose} disabled={saving} aria-label="关闭镜头编辑器"><X /></button></header>
    <div className="drawer-form">
      <label>剧情目的<input value={form.purpose || ''} onChange={(event) => patch('purpose', event.target.value)} /></label>
      <label>动作描述<textarea rows={4} value={form.action || ''} onChange={(event) => patch('action', event.target.value)} /></label>
      <div className="two-column">
        <label>时长（秒）<input type="number" min="0.1" step="0.1" value={form.duration_sec ?? form.duration_seconds ?? 0} onChange={(event) => patch('duration_sec', Number(event.target.value))} /></label>
        <label>景别<input value={form.shot_size || ''} onChange={(event) => patch('shot_size', event.target.value)} /></label>
        <label>机位<input value={form.camera_angle || ''} onChange={(event) => patch('camera_angle', event.target.value)} /></label>
        <label>运镜<input value={form.camera_motion || ''} onChange={(event) => patch('camera_motion', event.target.value)} /></label>
      </div>
      <label>制作策略<input value={form.render_strategy || ''} onChange={(event) => patch('render_strategy', event.target.value)} /></label>
      {error && <div className="form-error" role="alert">{error}</div>}
    </div>
    <footer><button type="button" className="button ghost" onClick={requestClose} disabled={saving}>取消</button><button type="button" className="button primary" onClick={() => void save()} disabled={saving}>{saving && <LoaderCircle className="spin" size={15} />}保存镜头</button></footer>
  </aside>
  </div>
}
