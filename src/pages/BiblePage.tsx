import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { AlertTriangle, Check, LoaderCircle, MapPin, Package, Palette, RefreshCw, Sparkles, Users } from 'lucide-react'
import { ApiError, api } from '../api/client'
import { ConfirmDialog } from '../components/ConfirmDialog'
import type { BibleEntry, Project, ProjectBible } from '../types'

function entryName(item: BibleEntry, fallback: string) {
  return item.name || item.title || fallback
}

function entryText(item: BibleEntry) {
  return item.description || item.appearance || item.personality || item.prompt || item.role || '暂无详细描述。'
}

export function BiblePage({ project, onChanged }: { project: Project; onChanged?: () => void }) {
  const [bible, setBible] = useState<ProjectBible | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<'generate' | 'approve' | null>(null)
  const [confirming, setConfirming] = useState<'generate' | 'approve' | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setBible(await api.getBible(project.id))
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 404) setBible(null)
      else setError(reason instanceof Error ? reason.message : '项目圣经读取失败。')
    } finally {
      setLoading(false)
    }
  }, [project.id])

  useEffect(() => { void load() }, [load])

  const act = async (kind: 'generate' | 'approve') => {
    setConfirming(null)
    setBusy(kind)
    setError(null)
    try {
      const next = kind === 'generate' ? await api.generateBible(project.id) : await api.approveBible(project.id)
      setBible(next)
      onChanged?.()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '操作失败，请重试。')
    } finally {
      setBusy(null)
    }
  }

  const characters = bible?.characters ?? []
  const locations = bible?.locations ?? bible?.scenes ?? []
  const props = bible?.props ?? []
  const style = bible?.style_guide ?? bible?.style
  const approved = Boolean(bible?.approved || bible?.status === 'approved')

  return <main className="studio-page bible-page">
    <section className="studio-hero">
      <div><span className="eyebrow">项目圣经 / 视觉一致性</span><h1>让每个镜头属于同一个世界</h1><p>沉淀风格、角色、场景与关键道具，批准后作为生产计划的统一参考。</p></div>
      <div className="hero-actions">
        {bible && <button type="button" className="button ghost" disabled={Boolean(busy)} onClick={() => setConfirming('generate')}><RefreshCw size={16} />{busy === 'generate' ? '生成中…' : '重新生成'}</button>}
        <button type="button" className="button primary" disabled={Boolean(busy) || approved} onClick={() => (bible ? setConfirming('approve') : void act('generate'))}>
          {busy ? <LoaderCircle className="spin" size={16} /> : approved ? <Check size={16} /> : <Sparkles size={16} />} {approved ? '已批准' : bible ? '批准圣经' : '生成项目圣经'}
        </button>
      </div>
    </section>

    {error && <div className="studio-alert" role="alert"><AlertTriangle /><span><strong>操作未完成</strong><small>{error}</small></span><button type="button" onClick={() => void load()}><RefreshCw size={15} />重试</button></div>}

    {loading
      ? <div className="studio-loading"><LoaderCircle className="spin" /> 正在读取项目圣经…</div>
      : !bible
        ? <section className="studio-empty"><Palette /><h2>还没有项目圣经</h2><p>生成第一版视觉规则、人物档案、场景和道具设定，随后可审核并批准。</p><button type="button" className="button subtle" onClick={() => void act('generate')} disabled={Boolean(busy)}><Sparkles size={16} />生成第一版</button></section>
        : <>
          <section className="bible-style"><span className="result-icon lavender"><Palette /></span><div><small>视觉风格 · 版本 {bible.version ?? 1}</small><h2>{typeof style === 'string' ? '风格指南' : entryName(style ?? {}, '风格指南')}</h2><p>{typeof style === 'string' ? style : entryText(style ?? {})}</p></div><em className={approved ? 'approved' : ''}>{approved ? '已批准' : bible.status || '待审核'}</em></section>
          <BibleSection icon={<Users />} title="角色" subtitle="外观、性格与叙事身份" items={characters} />
          <BibleSection icon={<MapPin />} title="场景" subtitle="空间、光线与氛围规则" items={locations} />
          <BibleSection icon={<Package />} title="道具" subtitle="推动剧情的关键物件" items={props} />
        </>}

    <ConfirmDialog
      open={confirming === 'generate'}
      title="重新生成项目圣经？"
      description="将根据当前故事内容重新生成角色、场景、道具与风格指南，现有版本会被新的生成结果覆盖，已手工修改的设定内容不会保留，此操作不可撤销。"
      confirmLabel="确认重新生成"
      busy={busy === 'generate'}
      onConfirm={() => void act('generate')}
      onCancel={() => setConfirming(null)}
    />
    <ConfirmDialog
      open={confirming === 'approve'}
      title="批准项目圣经？"
      description="批准后这份视觉设定将成为生产计划的统一参考，用于约束角色、场景与风格一致性。批准状态会锁定当前版本，如需调整需要重新生成并再次批准，此操作不可撤销。"
      confirmLabel="确认批准"
      busy={busy === 'approve'}
      onConfirm={() => void act('approve')}
      onCancel={() => setConfirming(null)}
    />
  </main>
}

function BibleSection({ icon, title, subtitle, items }: { icon: ReactNode; title: string; subtitle: string; items: BibleEntry[] }) {
  return <section className="bible-section">
    <header><span>{icon}</span><div><h2>{title}</h2><p>{subtitle} · {items.length} 项</p></div></header>
    {items.length
      ? <div className="bible-grid">{items.map((item, index) => <article key={item.id || `${title}-${index}`}><div className="bible-placeholder">{entryName(item, `${title} ${index + 1}`).slice(0, 1)}</div><div><strong>{entryName(item, `${title} ${index + 1}`)}</strong><p>{entryText(item)}</p>{item.status && <small>{item.status}</small>}</div></article>)}</div>
      : <div className="section-empty">这一版暂未生成{title}条目</div>}
  </section>
}
