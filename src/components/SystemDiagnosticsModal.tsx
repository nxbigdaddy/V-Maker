import { useCallback, useEffect, useState } from 'react'
import { AlertCircle, CheckCircle2, FolderCog, HardDrive, HeartPulse, LoaderCircle, MonitorCog, RefreshCw, Scissors, Server, ServerCog, X, XCircle } from 'lucide-react'
import { api } from '../api/client'
import { ConfirmDialog } from './ConfirmDialog'
import { useDialog } from '../lib/dialog'
import type { DiagnosticItem, JianyingDiagnostics, SystemDiagnostics } from '../types'

const normalize = (data: SystemDiagnostics): DiagnosticItem[] => data.items ?? [
  { key: 'backend', label: '后端服务', ok: data.health?.ok ?? data.ok, message: data.health?.message || data.status },
  { key: 'ffmpeg', label: 'FFmpeg', ok: data.ffmpeg?.available, message: data.ffmpeg?.version || data.ffmpeg?.message },
  { key: 'data_dir', label: '数据目录', ok: data.data_dir?.writable, message: data.data_dir?.path },
  { key: 'desktop', label: '桌面依赖', ok: data.desktop?.available, message: data.desktop?.message },
].filter((item) => item.ok !== undefined || item.message)

/**
 * 剪映库信息：后端字段名尚未固定，逐个候选名尝试，全部用可选链取值。
 * 一个字都没读到就返回 undefined，界面显示「未知」而不是崩掉。
 */
function jianyingOf(data: SystemDiagnostics): JianyingDiagnostics | undefined {
  const candidates: Array<JianyingDiagnostics | undefined> = [data.jianying, data.jianying_draft]
  for (const candidate of candidates) if (candidate && typeof candidate === 'object') return candidate
  return undefined
}

/** 后端可能把剪映信息放在 items 里，key/label 命中时就把它当剪映那一行。 */
function isJianyingItem(item: DiagnosticItem): boolean {
  return /jianying|剪映/i.test(`${item.key ?? ''} ${item.label ?? ''}`)
}

function textOf(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

export function SystemDiagnosticsModal({ onClose }: { onClose: () => void }) {
  const [data, setData] = useState<SystemDiagnostics | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [restarting, setRestarting] = useState(false)
  const [controlMessage, setControlMessage] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<'restart' | 'stop' | null>(null)
  // Esc 关闭 / 焦点圈定 / 关闭后恢复触发按钮焦点。
  const dialogRef = useDialog<HTMLElement>(onClose)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await api.getSystemDiagnostics())
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '诊断信息读取失败。')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  /**
   * 重启后端：请求会先返回，然后管理器把当前进程停掉，界面会断开一段时间。
   * 这里轮询健康检查，等新进程起来再刷新诊断——不能请求一发出就声称"已重启"。
   */
  const restart = async () => {
    setConfirming(null)
    setRestarting(true)
    setControlMessage(null)
    try {
      const result = await api.restartBackend()
      if (result.ok === false) { setControlMessage(result.message || '重启请求被拒绝。'); return }
      setControlMessage('已请求重启，正在等待后端恢复…')
      for (let attempt = 0; attempt < 40; attempt++) {
        await new Promise((resolve) => setTimeout(resolve, 1000))
        try {
          const fresh = await api.getSystemDiagnostics()
          setData(fresh)
          setControlMessage(`后端已恢复（PID ${fresh.process?.pid ?? '未知'}）。`)
          return
        } catch { /* 还在重启，继续等 */ }
      }
      setControlMessage('等待 40 秒仍未恢复。请查看 backend/data/logs/service.log 与 backend.err.log。')
    } catch (reason) {
      setControlMessage(reason instanceof Error ? reason.message : '重启请求失败。')
    } finally {
      setRestarting(false)
    }
  }

  const stop = async () => {
    setConfirming(null)
    setControlMessage(null)
    try {
      const result = await api.stopBackend()
      setControlMessage(result.ok === false
        ? (result.message || '停止请求被拒绝。')
        : '已请求停止后端。此后界面无法再连接，需要运行 start-vmaker.bat backend 重新启动。')
    } catch (reason) {
      setControlMessage(reason instanceof Error ? reason.message : '停止请求失败。')
    }
  }

  const process = data?.process

  const allItems = data ? normalize(data) : []
  // 剪映支持单独成块展示（要写清草稿目录与未验证），所以从通用列表里摘出来。
  const jianyingItem = allItems.find(isJianyingItem)
  const items = allItems.filter((item) => !isJianyingItem(item))
  const icons = [Server, MonitorCog, HardDrive, FolderCog]
  // 这个时间戳来自当前页面真正加载到的那份前端产物；刷新后应该变化。
  const buildTime = typeof __VMAKER_BUILD_TIME__ === 'string' ? __VMAKER_BUILD_TIME__ : ''
  const buildLabel = buildTime
    ? new Date(buildTime).toLocaleString('zh-CN', { hour12: false })
    : '未知'

  const jianying = data ? jianyingOf(data) : undefined
  const jianyingAvailable = jianyingItem?.ok ?? jianying?.available ?? jianying?.installed
  const jianyingVersion = textOf(jianying?.version) ?? textOf(jianying?.library_version)
  // drafts_dir 是嵌套对象 {configured,path,writable,message}，不能当字符串读。
  const draftsDirInfo = jianying?.drafts_dir && typeof jianying.drafts_dir === 'object' ? jianying.drafts_dir : undefined
  const draftsDir = textOf(draftsDirInfo?.path) ?? textOf(jianying?.draft_dir)
  const draftsConfigured = draftsDirInfo?.configured ?? jianying?.drafts_dir_configured ?? Boolean(draftsDir)
  // 后端自己声明的限制优先展示，缺失时才用本地兜底文案——避免前后端各说一套、内容重复。
  const unverifiedText = textOf(jianying?.unverified)
  const svgText = textOf(jianying?.svg)
  const assetPathsText = textOf(jianying?.asset_paths)
  const jianyingMessage = textOf(jianyingItem?.message) ?? textOf(jianying?.message)

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="settings-modal diagnostics-modal" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="diagnostics-title">
        <header>
          <div className="settings-icon"><HeartPulse /></div>
          <div>
            <span className="eyebrow">SYSTEM</span>
            <h2 id="diagnostics-title">环境诊断</h2>
            <p>检查后端、FFmpeg、数据目录与桌面运行依赖，以及剪映草稿支持。</p>
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="关闭"><X /></button>
        </header>
        <div className="settings-body">
          {loading ? (
            <div className="settings-loading"><LoaderCircle className="spin" />正在执行诊断……</div>
          ) : error ? (
            <div className="diagnostic-error">
              <AlertCircle />
              <div>
                <strong>诊断接口不可用</strong>
                <p>{error}</p>
                <small>前端已按 <code>/api/system/diagnostics</code> 对接；请由后端返回真实环境状态。</small>
              </div>
            </div>
          ) : (
            <>
              {items.length ? (
                <div className="diagnostic-list">
                  {items.map((item, index) => {
                    const Icon = icons[index % icons.length]
                    return (
                      <article key={item.key || item.label}>
                        <span className="diagnostic-kind"><Icon /></span>
                        <div>
                          <strong>{item.label || item.key}</strong>
                          <p>{item.message || '后端未提供详情'}</p>
                        </div>
                        <em className={item.ok === true ? 'ok' : item.ok === false ? 'bad' : 'unknown'}>
                          {item.ok === true ? <><CheckCircle2 />正常</> : item.ok === false ? <><XCircle />异常</> : '未知'}
                        </em>
                      </article>
                    )
                  })}
                </div>
              ) : (
                <div className="section-empty">诊断接口未返回可展示的检查项。</div>
              )}

              {/* 后端进程：这是唯一能对后端做实际操作的地方，所以状态必须来自后端本身。 */}
              {process && (
                <article className="diagnostic-process">
                  <header>
                    <span className="diagnostic-kind"><ServerCog /></span>
                    <div>
                      <strong>后端进程</strong>
                      <p style={{ overflowWrap: 'anywhere' }}>
                        PID {process.pid ?? '未知'} · 端口 {process.port ?? '未知'} ·{' '}
                        {process.managed ? '由服务管理器启动' : '由管理器之外启动（无法用管理器停止它以外的实例）'}
                      </p>
                    </div>
                    <em className={process.managed ? 'ok' : 'unknown'}>
                      {process.managed ? <><CheckCircle2 />受管</> : '未受管'}
                    </em>
                  </header>
                  <ul>
                    <li>解释器：<code style={{ overflowWrap: 'anywhere' }}>{process.python || '未提供'}</code></li>
                    <li>PID 文件：<code style={{ overflowWrap: 'anywhere' }}>{process.pid_file || '未提供'}</code></li>
                    <li>后端日志：<code style={{ overflowWrap: 'anywhere' }}>{process.log_file || '未提供'}</code></li>
                    <li>服务日志：<code style={{ overflowWrap: 'anywhere' }}>{process.service_log_file || '未提供'}</code></li>
                    <li>{process.note || '重启与停止由 scripts/service.ps1 执行；启动必须用启动器或命令行。'}</li>
                  </ul>
                  <footer className="diagnostic-process-actions">
                    <button type="button" className="button ghost" onClick={() => setConfirming('restart')}
                            disabled={restarting || process.can_restart === false}>
                      {restarting ? <><LoaderCircle className="spin" size={16} />等待恢复…</> : <><RefreshCw size={16} />重启后端</>}
                    </button>
                    <button type="button" className="button danger" onClick={() => setConfirming('stop')}
                            disabled={process.can_restart === false}>
                      <XCircle size={16} />停止后端
                    </button>
                  </footer>
                  {controlMessage && (
                    <p className="diagnostic-process-message" role="status">{controlMessage}</p>
                  )}
                </article>
              )}

              <article className="diagnostic-jianying">
                <header>
                  <span className="diagnostic-kind"><Scissors /></span>
                  <div>
                    <strong>剪映草稿支持</strong>
                    <p>
                      {jianyingAvailable === true
                        ? `pyJianYingDraft 可用${jianyingVersion ? `（版本 ${jianyingVersion}）` : '（后端未返回版本号）'}。`
                        : jianyingAvailable === false
                          ? `pyJianYingDraft 不可用。导出剪映草稿时会自动回退为通用草稿包${jianyingVersion ? `（版本 ${jianyingVersion}）` : ''}。`
                          : '后端未返回剪映库信息，无法确认是否可用；导出剪映草稿时若库缺失会自动回退为通用草稿包。'}
                    </p>
                    {jianyingMessage && <p style={{ overflowWrap: 'anywhere' }}>{jianyingMessage}</p>}
                  </div>
                  <em className={jianyingAvailable === true ? 'ok' : jianyingAvailable === false ? 'bad' : 'unknown'}>
                    {jianyingAvailable === true ? <><CheckCircle2 />可用</> : jianyingAvailable === false ? <><XCircle />不可用</> : '未知'}
                  </em>
                </header>
                <ul>
                  <li>只生成草稿，不做自动导出：该库的自动导出只支持剪映 6 及以下，成片请在剪映里自行导出。</li>
                  <li>
                    {unverifiedText
                      ?? '未在真实剪映中验证：开发环境没有剪映，这份草稿能否正常打开尚未验证，请按实验性能力对待。'}
                  </li>
                  <li>
                    {draftsConfigured
                      ? <>草稿目录（<code>VMAKER_JIANYING_DRAFTS_DIR</code>）：<code style={{ overflowWrap: 'anywhere' }}>{draftsDir || '已配置，但后端未返回路径'}</code></>
                      : <>未配置 <code>VMAKER_JIANYING_DRAFTS_DIR</code>：草稿只写到项目导出目录，需要手动放进剪映草稿目录。</>}
                  </li>
                  <li>
                    {svgText ?? '剪映不支持 SVG；如果图像 provider 返回 SVG，对应镜头会被跳过并列出原因（默认 demo 占位图是位图，不受影响）。'}
                  </li>
                  {assetPathsText && <li>{assetPathsText}</li>}
                  {/* 未配置时上面那行已经说清楚了，再显示后端的同义提示会重复。 */}
                  {draftsConfigured && draftsDirInfo?.message && <li>{draftsDirInfo.message}</li>}
                </ul>
              </article>
            </>
          )}
          <p className="build-stamp">
            前端资源生成时间：<time dateTime={buildTime || undefined}>{buildLabel}</time>
            <small>
              开发模式下这是开发服务器的启动时间，生产构建下是构建时间。
              <strong>排查界面问题时可记录此时间；它不能单独证明浏览器缓存是否过期。</strong>
            </small>
          </p>
        </div>
        <footer>
          <button type="button" className="button ghost" onClick={() => void load()} disabled={loading}><RefreshCw size={16} />重新检测</button>
          <button type="button" className="button primary" onClick={onClose}>完成</button>
        </footer>
      </section>

      <ConfirmDialog
        open={confirming === 'restart'}
        title="重启后端？"
        description="将停止当前后端进程并立刻重新启动它。界面会在几秒内失去连接，然后自动重试。正在执行的生成/导出任务会被中断——已提交的任务会保留在任务中心，可以在重启后重试。"
        confirmLabel="确认重启"
        busy={restarting}
        onConfirm={() => void restart()}
        onCancel={() => setConfirming(null)}
      />
      <ConfirmDialog
        open={confirming === 'stop'}
        title="停止后端？"
        description="将停止当前后端进程。停止之后这个界面无法再连接，也没有任何按钮能把它启动回来——需要运行 start-vmaker.bat backend（或 scripts/service.ps1 start）。正在执行的任务会被中断。"
        confirmLabel="确认停止"
        tone="danger"
        onConfirm={() => void stop()}
        onCancel={() => setConfirming(null)}
      />
    </div>
  )
}
