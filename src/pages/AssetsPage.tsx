import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, Boxes, Clapperboard, Download, FileAudio, FileText, Image, LoaderCircle, RefreshCw, Sparkles, Video } from 'lucide-react'
import { api, API_BASE_URL, latestAssetMimeType } from '../api/client'
import type { MediaAsset, Project } from '../types'

const categoryOf = (asset: MediaAsset) => String(asset.type ?? asset.kind ?? asset.asset_type ?? '').toLowerCase()
const labelOf = (asset: MediaAsset) => asset.name ?? asset.title ?? (asset.shot_id ? `镜头 ${asset.shot_id}` : `素材 ${asset.id}`)
const pathOf = (asset: MediaAsset) => asset.preview_url ?? asset.url ?? asset.file_path ?? asset.path
const iconFor = (type: string) => type.includes('video')
  ? Video
  : type.includes('audio') || type.includes('voice')
    ? FileAudio
    : type.includes('subtitle') || type.includes('caption')
      ? FileText
      : type.includes('image') ? Image : Clapperboard
const statusLabel = (status?: string) => ({
  ready: '可用', completed: '已完成', succeeded: '已完成', pending: '等待中', processing: '生成中', failed: '失败',
}[String(status).toLowerCase()] ?? status ?? '未知')
const previewUrl = (value?: string) => !value || /^[A-Za-z]:[\\/]/.test(value)
  ? undefined
  : value.startsWith('/') && !value.startsWith('/api/') ? `${API_BASE_URL.replace(/\/api$/, '')}${value}` : value

/** 素材来源：latest_version.demo 为 true 是本地占位，为 false 是真实 AI 产物；字段缺失时不做任何猜测。 */
type AssetSource = 'demo' | 'ai' | 'unknown'
const sourceOf = (asset: MediaAsset): AssetSource => {
  const demo = asset.latest_version?.demo
  if (demo === true) return 'demo'
  if (demo === false) return 'ai'
  return 'unknown'
}

function metadataText(asset: MediaAsset, keys: string[]): string | null {
  let source: unknown = asset.metadata
  if (!source || typeof source !== 'object') source = asset
  const record = source as Record<string, unknown>
  for (const key of keys) {
    const value = record[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
    if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  }
  return null
}

/**
 * 该素材是否属于视频 / 图片 / 音频（按 kind/type 与 mime_type 判断，其它类型只提供下载）。
 * 视频优先于图片判断：kind 为 video、或 mime 以 video/ 开头都算视频，否则视频会被当成图片塞进 <img>。
 */
function mediaKindOf(asset: MediaAsset): 'video' | 'image' | 'audio' | 'other' {
  const category = categoryOf(asset)
  const mime = latestAssetMimeType(asset)
  if (category.includes('video') || (mime && mime.startsWith('video/'))) return 'video'
  if ((mime && mime.startsWith('image/')) || category.includes('image') || category.includes('visual')) return 'image'
  if ((mime && mime.startsWith('audio/')) || category.includes('audio') || category.includes('voice') || category.includes('dialogue')) return 'audio'
  return 'other'
}

export function AssetsPage({ project }: { project: Project }) {
  const [assets, setAssets] = useState<MediaAsset[]>([])
  const [loading, setLoading] = useState(true)
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setAssets(await api.getAssets(project.id))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '素材读取失败。')
    } finally {
      setLoading(false)
    }
  }, [project.id])

  useEffect(() => { void load() }, [load])

  const groups = useMemo(() => {
    const sections = [
      { key: 'visual', title: '镜头画面', icon: Image, items: assets.filter((asset) => { const type = categoryOf(asset); return type.includes('image') || type.includes('video') || type.includes('visual') || type.includes('shot') }) },
      { key: 'audio', title: '对白 / 音频', icon: FileAudio, items: assets.filter((asset) => { const type = categoryOf(asset); return type.includes('audio') || type.includes('voice') || type.includes('dialogue') }) },
      { key: 'subtitle', title: '字幕', icon: FileText, items: assets.filter((asset) => { const type = categoryOf(asset); return type.includes('subtitle') || type.includes('caption') }) },
    ]
    const known = new Set(sections.flatMap((section) => section.items.map((item) => item.id)))
    const other = assets.filter((asset) => !known.has(asset.id))
    if (other.length) sections.push({ key: 'other', title: '其他素材', icon: Boxes, items: other })
    return sections
  }, [assets])

  const generate = async () => {
    setGenerating(true)
    setError(null)
    setNotice(null)
    try {
      await api.generateDemoMedia(project.id)
      setNotice('离线占位素材已生成。这些素材不是 AI 生成内容。')
      await load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Demo 素材生成失败。')
    } finally {
      setGenerating(false)
    }
  }

  return <main className="studio-page media-page">
    <section className="studio-hero">
      <div><span className="eyebrow">MEDIA LIBRARY</span><h1>素材库</h1><p>集中查看镜头画面、对白音频和字幕文件，以及每项素材的版本、来源与状态。</p></div>
      <div className="hero-actions"><button type="button" className="button ghost" onClick={() => void load()} disabled={loading}><RefreshCw size={16} />刷新</button><button type="button" className="button primary" onClick={() => void generate()} disabled={generating}>{generating ? <LoaderCircle className="spin" size={16} /> : <Sparkles size={16} />}生成 Demo 素材</button></div>
    </section>

    <div className="demo-disclosure"><AlertTriangle size={18} /><span><strong>离线占位，不是 AI</strong><small>“生成 Demo 素材”只在本地建立用于串联制作流程的占位图、占位音频与字幕，不会调用生成式模型。真实 AI 画面、配音与视频来自分镜工作台的「生成画面 / 生成配音 / 生成视频」，它们会以「AI 生成」徽标区分，并显示 provider 与 model。视频素材在这里可以直接播放预览；Demo 素材本身不含视频，但在视频能力仍是 demo 时点「生成视频」会得到本机 FFmpeg 渲染的占位片段（带 demo 徽标）。</small></span></div>
    {notice && <div className="studio-success" role="status">{notice}</div>}
    {error && <div className="studio-alert" role="alert"><AlertTriangle /><span><strong>素材操作未完成</strong><small>{error}</small></span><button type="button" onClick={() => void load()}><RefreshCw size={14} />重试</button></div>}

    {loading
      ? <div className="studio-loading"><LoaderCircle className="spin" />正在读取素材…</div>
      : assets.length === 0
        ? <section className="studio-empty"><Boxes /><h2>还没有媒体素材</h2><p>完成分镜后，可以在分镜工作台对镜头「生成画面 / 生成配音」（真实服务会以「AI 生成」标记），也可以先用「生成 Demo 素材」建立一套明确标记的离线占位素材来预览后续流程。</p><button type="button" className="button primary" onClick={() => void generate()} disabled={generating}>生成 Demo 素材</button></section>
        : <div className="asset-sections">{groups.map(({ key, title, icon: Icon, items }) => <section className="asset-section" key={key}>
          <header><span><Icon size={18} /></span><div><h2>{title}</h2><p>{items.length} 项素材</p></div></header>
          {items.length
            ? <div className="asset-grid">{items.map((asset) => {
              const type = categoryOf(asset)
              const AssetIcon = iconFor(type)
              const path = pathOf(asset)
              const assetLabel = labelOf(asset)
              const mediaKind = mediaKindOf(asset)
              const contentUrl = api.latestAssetContentUrl(project.id, asset)
              const imageUrl = mediaKind === 'image' ? contentUrl ?? previewUrl(path) : undefined
              // 视频用后端最新版本的内容地址；拿不到时退回到通用预览地址，两者都没有就明确写「暂不支持预览」。
              const videoUrl = mediaKind === 'video' ? contentUrl ?? previewUrl(path) : undefined
              const playable = mediaKind === 'audio' && Boolean(contentUrl)
              const downloadUrl = contentUrl ?? previewUrl(path)
              const source = sourceOf(asset)
              const provider = metadataText(asset, ['provider'])
              const model = metadataText(asset, ['model'])
              const sourceMeta = [
                provider ? `provider：${provider}` : null,
                model ? `model：${model}` : null,
              ].filter(Boolean).join(' · ')
              return <article className="asset-card" key={asset.id}>
                <div className="asset-preview">
                  {imageUrl
                    ? <img src={imageUrl} alt={`素材预览：${assetLabel}`} loading="lazy" />
                    : videoUrl
                      ? <video controls preload="metadata" src={videoUrl} aria-label={`视频预览：${assetLabel}`} />
                      : playable
                        ? <audio controls preload="none" src={contentUrl ?? undefined} />
                        : <div className="asset-preview-note"><AssetIcon size={31} aria-hidden="true" /><small>该素材暂不支持预览</small></div>}
                  <span>{type || 'asset'}</span>
                </div>
                <div className="asset-body">
                  <strong title={assetLabel}>{assetLabel}</strong>
                  {source === 'demo' && <span className="asset-source demo">DEMO / 本地占位 · 不是 AI 生成</span>}
                  {source === 'ai' && <span className="asset-source ai">AI 生成</span>}
                  {source === 'ai' && (sourceMeta
                    ? <p className="asset-source-meta">{sourceMeta}</p>
                    : <p className="asset-source-meta">后端未提供 provider / model 信息</p>)}
                  <div><span>版本 <b>v{asset.version ?? asset.latest_version?.version ?? 1}</b></span><em className={`asset-status ${String(asset.status ?? '').toLowerCase()}`}>{statusLabel(asset.status)}</em></div>
                  {path && <code title={path}>{path}</code>}
                  {downloadUrl && <a className="asset-download" href={downloadUrl} download><Download size={13} aria-hidden="true" />可下载</a>}
                </div>
              </article>
            })}</div>
            : <div className="section-empty">暂无此类素材</div>}
        </section>)}</div>}
  </main>
}
