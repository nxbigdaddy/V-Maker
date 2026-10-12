import { AlertCircle, ArrowRight, Clapperboard, FolderOpen, Plus, RefreshCw, Search, Settings2 } from 'lucide-react'
import { Brand } from '../components/Brand'
import { ProjectCard } from '../components/ProjectCard'
import type { Project } from '../types'

type HomePageProps = {
  projects: Project[]
  loading: boolean
  error: string | null
  onRetry: () => void
  onCreate: () => void
  onOpen: (project: Project) => void
  onSettings: () => void
  onProjectAction: (project: Project, action: 'duplicate' | 'archive' | 'restore' | 'delete') => void
  actionBusy: string | null
}

export function HomePage({
  projects,
  loading,
  error,
  onRetry,
  onCreate,
  onOpen,
  onSettings,
  onProjectAction,
  actionBusy,
}: HomePageProps) {
  return (
    <div className="home-shell">
      <header className="home-header">
        <Brand />
        <nav>
          <button type="button" className="nav-link active">
            <FolderOpen size={17} /> 项目
          </button>
          <button type="button" className="nav-link" disabled title="创作模板即将开放">
            <Clapperboard size={17} /> 创作模板
          </button>
        </nav>
        <div className="header-actions">
          {/* 仅开发模式可见：用来一眼确认浏览器加载的是不是最新代码，
              生产构建里 import.meta.env.DEV 为 false，这个标记不会出现。 */}
          {import.meta.env.DEV && <DevBuildMarker />}
          <button type="button" className="icon-button" aria-label="模型设置" onClick={onSettings}>
            <Settings2 size={19} />
          </button>
          <span className="avatar" aria-label="本地创作者">创</span>
        </div>
      </header>

      <main className="home-main">
        <section className="hero">
          <div>
            <span className="eyebrow">创作者控制台</span>
            <h1>把脑海里的故事，<br /><em>做成一部动画。</em></h1>
            <p>从故事分析、角色设定到分镜与成片，在一个清晰的工作流里掌控每个创作节点。</p>
            <button type="button" className="button primary hero-button" onClick={onCreate}>
              <Plus size={18} /> 新建动画项目
            </button>
          </div>
          <div className="hero-art" aria-hidden="true">
            <div className="orb orb-one" />
            <div className="orb orb-two" />
            <div className="film-frame frame-one"><span>SCENE 01</span></div>
            <div className="film-frame frame-two"><span>SHOT 08</span></div>
            <div className="spark">✦</div>
          </div>
        </section>

        <section className="projects-section">
          <div className="section-heading">
            <div>
              <h2>我的项目</h2>
              <p>{loading ? '正在读取项目…' : error ? '本地服务尚未连接' : `${projects.length} 个创作项目`}</p>
            </div>
            <div className="project-tools">
              <label className="search-box">
                <Search size={17} />
                <input placeholder="搜索即将开放" aria-label="搜索项目即将开放" disabled />
              </label>
              <button type="button" className="icon-button" aria-label="项目筛选即将开放" title="项目筛选即将开放" disabled>
                <Settings2 size={18} />
              </button>
            </div>
          </div>

          {error && (
            <div className="connection-error">
              <div>
                <AlertCircle size={22} />
                <span><strong>项目暂时无法载入</strong><small>{error}</small></span>
              </div>
              <button type="button" className="button ghost" onClick={onRetry}>
                <RefreshCw size={16} /> 重新连接
              </button>
            </div>
          )}

          {loading && (
            <div className="project-grid">
              {[1, 2, 3].map((item) => (
                <div className="project-card skeleton" key={item}>
                  <div className="project-cover" />
                  <div className="project-card-body"><i /><i /><i /></div>
                </div>
              ))}
            </div>
          )}

          {!loading && !error && projects.length > 0 && (
            <div className="project-grid">
              {projects.map((project, index) => (
                <ProjectCard
                  key={project.id}
                  project={project}
                  index={index}
                  onOpen={() => onOpen(project)}
                  busy={actionBusy === project.id}
                  onAction={(action) => onProjectAction(project, action)}
                />
              ))}
              <button type="button" className="new-project-card" onClick={onCreate}>
                <span><Plus size={24} /></span>
                <strong>创建新项目</strong>
                <small>从一个故事开始</small>
              </button>
            </div>
          )}

          {!loading && !error && projects.length === 0 && (
            <div className="empty-projects">
              <div className="empty-icon"><Clapperboard size={30} /></div>
              <h3>你的第一部作品，正等着开场</h3>
              <p>创建项目并输入故事，V-Maker 会帮你规划角色、场景与镜头。</p>
              <button type="button" className="button primary" onClick={onCreate}>
                开始创作 <ArrowRight size={17} />
              </button>
            </div>
          )}
        </section>
      </main>
    </div>
  )
}

/**
 * 开发模式下的构建标记。
 *
 * 我们反复遇到「界面显示的还是修复前的样子」，而截图无法区分「代码没生效」和
 * 「浏览器还在跑旧包」。这个标记只依赖当前页面真正加载到的那份 JS：
 * 刷新后时间戳变化就说明拿到的是最新代码；看不到这一行，就说明页面来自旧的打包产物。
 */
function DevBuildMarker() {
  const label = typeof __VMAKER_BUILD_TIME__ === 'string'
    ? new Date(__VMAKER_BUILD_TIME__).toLocaleTimeString('zh-CN', { hour12: false })
    : '未知'
  return (
    <span className="dev-build" title={`开发服务器启动时间 ${label}；刷新后若时间变化即为最新代码`}>
      开发版 {label}
    </span>
  )
}
