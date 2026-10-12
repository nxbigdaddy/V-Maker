import { Archive, ArrowUpRight, Clock3, Copy, Film, MoreHorizontal, RotateCcw, Trash2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { Project } from '../types'

// 克制的封面配色：内联样式会覆盖 CSS，所以专业工具风的色调要在这里改。
const gradients = [
  'linear-gradient(160deg, #2f3a48 0%, #232c37 100%)',
  'linear-gradient(160deg, #3a3546 0%, #262230 100%)',
  'linear-gradient(160deg, #2b3d43 0%, #1f2c31 100%)',
]

type ProjectAction = 'duplicate' | 'archive' | 'restore' | 'delete'

function dateLabel(value?: string) {
  if (!value) return '刚刚更新'
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric' }).format(date)
}

export function ProjectCard({ project, index, onOpen, onAction, busy }: {
  project: Project
  index: number
  onOpen: () => void
  onAction: (action: ProjectAction) => void
  busy?: boolean
}) {
  const [open, setOpen] = useState(false)
  const menu = useRef<HTMLDivElement>(null)
  const progress = Math.max(0, Math.min(project.progress ?? 0, 100))
  const mode = { draft: '草稿', standard: '标准', professional: '专业' }[project.mode] ?? project.mode

  useEffect(() => {
    const close = (event: MouseEvent) => {
      if (!menu.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [])

  const act = (action: ProjectAction) => {
    setOpen(false)
    onAction(action)
  }

  return (
    <article
      className={`project-card ${project.status === 'archived' ? 'archived' : ''} ${open ? 'menu-open' : ''}`}
      onClick={onOpen}
      tabIndex={0}
      role="button"
      aria-label={`打开项目：${project.name}`}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget) return
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onOpen()
        }
      }}
    >
      <div
        className="project-cover"
        style={{ background: project.cover_url ? `url(${project.cover_url}) center/cover` : gradients[index % gradients.length] }}
      >
        <div className="cover-grid" />
        <span className="mode-pill">{project.status === 'archived' ? '已归档' : `${mode}模式`}</span>
        <div className="cover-monogram">{project.name.slice(0, 1)}</div>
      </div>
      {/* The menu lives next to the cover, not inside it: the cover clips its own background,
          and nesting the dropdown there cut off the last action. */}
      <div className="project-menu-wrap" ref={menu}>
        <button
          type="button"
          className="icon-button cover-menu"
          disabled={busy}
          aria-label="项目菜单"
          aria-haspopup="menu"
          aria-expanded={open}
          onClick={(event) => {
            event.stopPropagation()
            setOpen((value) => !value)
          }}
        >
          <MoreHorizontal size={18} />
        </button>
        {open && (
          <div className="project-menu" role="menu" onClick={(event) => event.stopPropagation()}>
            <button type="button" role="menuitem" onClick={() => act('duplicate')}><Copy />复制项目</button>
            {project.status === 'archived' ? (
              <button type="button" role="menuitem" onClick={() => act('restore')}><RotateCcw />恢复项目</button>
            ) : (
              <button type="button" role="menuitem" onClick={() => act('archive')}><Archive />归档项目</button>
            )}
            <button type="button" role="menuitem" className="danger" onClick={() => act('delete')}><Trash2 />删除项目</button>
          </div>
        )}
      </div>
      <div className="project-card-body">
        <div className="project-heading">
          <div>
            <h3>{project.name}</h3>
            <p>{project.current_stage || '等待开始创作'}</p>
          </div>
          <ArrowUpRight size={19} />
        </div>
        <div className="project-progress"><span style={{ width: `${progress}%` }} /></div>
        <div className="project-meta">
          <span><Film size={14} /> {project.episode_count ?? 0} 集 · {project.shot_count ?? 0} 镜</span>
          <span><Clock3 size={14} /> {dateLabel(project.updated_at)}</span>
        </div>
      </div>
    </article>
  )
}
