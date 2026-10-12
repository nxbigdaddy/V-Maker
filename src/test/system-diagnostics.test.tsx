import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SystemDiagnosticsModal } from '../components/SystemDiagnosticsModal'

/**
 * 环境诊断里的剪映草稿支持回归。
 *
 * 后端字段名与内嵌位置都还没定稿，所以这里重点验证两类兜底：
 *  - 一个字都不给（缺字段）不能崩，要显示「未知」与「未配置」；
 *  - 剪映信息放在 items 里时，不能在通用列表与剪映专块里重复显示两行。
 */
function respond(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
}

function stubDiagnostics(body: unknown) {
  vi.stubGlobal('fetch', vi.fn(async () => respond(body)))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('后端进程面板', () => {
  it('显示后端自身状态，并在重启后轮询健康检查而不是立刻宣称已重启', async () => {
    let diagnosticsCalls = 0
    vi.stubGlobal('fetch', vi.fn(async (url: string | URL) => {
      const path = String(url)
      if (path.includes('/system/backend/restart')) {
        return respond({ ok: true, action: 'restart', message: '已请求重启后端；…' })
      }
      diagnosticsCalls += 1
      // 第一次是旧进程；重启后的前两次仍然连不上，模拟真实的重启窗口。
      if (diagnosticsCalls === 2 || diagnosticsCalls === 3) throw new TypeError('backend restarting')
      const pid = diagnosticsCalls === 1 ? 111 : 222
      return respond({
        items: [{ key: 'backend', label: '后端服务', ok: true, message: '正常' }],
        process: {
          pid, port: 8765, managed: true, can_restart: true, python: 'python.exe',
          pid_file: 'D:/data/backend.pid', log_file: 'D:/data/logs/backend.out.log',
          service_log_file: 'D:/data/logs/service.log',
        },
      })
    }))

    render(<SystemDiagnosticsModal onClose={() => {}} />)
    expect(await screen.findByText('后端进程')).toBeTruthy()
    expect(screen.getByText(/PID 111/)).toBeTruthy()
    expect(screen.getByText('受管')).toBeTruthy()

    await userEvent.click(screen.getByRole('button', { name: /重启后端/ }))
    // 诊断弹窗本身也是 role=dialog，所以按标题精确定位确认框。
    const dialog = (await screen.findByText('重启后端？')).closest('[role="dialog"]') as HTMLElement
    expect(within(dialog).getByText(/界面会在几秒内失去连接/)).toBeTruthy()
    await userEvent.click(within(dialog).getByRole('button', { name: /确认重启/ }))

    // 关键：必须等到真的恢复（新 PID）才算完成，而不是请求一发出就说"已重启"。
    await waitFor(() => {
      expect(screen.getAllByText(/PID 222/).length).toBeGreaterThan(0)
    }, { timeout: 15000 })
    expect(screen.getByText(/后端已恢复/)).toBeTruthy()
  }, 20000)

  it('未受管时如实说明，且停止确认框写清之后只能靠命令行启动', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string | URL) => {
      if (String(url).includes('/system/backend/stop')) {
        return respond({ ok: true, action: 'stop', message: '已请求停止后端。' })
      }
      return respond({
        items: [{ key: 'backend', label: '后端服务', ok: true, message: '正常' }],
        process: { pid: 111, managed: false, can_restart: true },
      })
    }))

    render(<SystemDiagnosticsModal onClose={() => {}} />)
    expect(await screen.findByText('后端进程')).toBeTruthy()
    // 未受管时必须如实说明，不能让用户以为管理器能管它。
    expect(screen.getByText('未受管')).toBeTruthy()
    expect(screen.getByText(/由管理器之外启动/)).toBeTruthy()

    await userEvent.click(screen.getByRole('button', { name: /停止后端/ }))
    const dialog = (await screen.findByText('停止后端？')).closest('[role="dialog"]') as HTMLElement
    expect(within(dialog).getByText(/没有任何按钮能把它启动回来/)).toBeTruthy()
    await userEvent.click(within(dialog).getByRole('button', { name: /确认停止/ }))
    await waitFor(() => { expect(screen.getByText(/需要运行 start-vmaker\.bat backend/)).toBeTruthy() })
  })
})

describe('环境诊断的剪映草稿支持', () => {
  it('库可用时显示版本与草稿目录，并写清未验证、不做自动导出', async () => {
    stubDiagnostics({
      items: [
        { key: 'backend', label: '后端服务', ok: true, message: '正常' },
        { key: 'ffmpeg', label: 'FFmpeg', ok: true, message: '6.1' },
      ],
      // 真实后端形状：drafts_dir 是嵌套对象，另有 unverified / asset_paths / svg 三条限制说明。
      jianying: {
        library: 'pyJianYingDraft',
        available: true,
        version: '0.2.3',
        draft_generation: true,
        auto_export: false,
        drafts_dir: { configured: true, path: 'D:/JianyingPro/Drafts', writable: true, message: '草稿会直接写入该目录' },
        unverified: '未在真实剪映中验证：草稿能否被剪映打开尚未确认',
        asset_paths: '素材为绝对路径引用；删除项目素材会让草稿失效',
        svg: '剪映不支持 SVG；只有 SVG 素材的镜头会被跳过',
      },
    })

    render(<SystemDiagnosticsModal onClose={() => {}} />)

    expect(await screen.findByText('剪映草稿支持')).toBeTruthy()
    expect(screen.getByText(/pyJianYingDraft 可用（版本 0.2.3）/)).toBeTruthy()
    expect(screen.getByText(/VMAKER_JIANYING_DRAFTS_DIR/)).toBeTruthy()
    expect(screen.getByText('D:/JianyingPro/Drafts')).toBeTruthy()
    expect(screen.getByText(/只生成草稿，不做自动导出/)).toBeTruthy()
    expect(screen.getByText(/未在真实剪映中验证/)).toBeTruthy()
    // 后端原文必须被展示出来，而不是被前端硬编码文案取代。
    expect(screen.getByText(/素材为绝对路径引用/)).toBeTruthy()
    expect(screen.getByText(/草稿会直接写入该目录/)).toBeTruthy()
    // drafts_dir 是对象，绝不能渲染成 "[object Object]"。
    expect(screen.queryByText(/\[object Object\]/)).toBeNull()
  })

  it('后端没有剪映字段时不崩，显示未知并说明草稿只写到项目导出目录', async () => {
    stubDiagnostics({
      items: [
        { key: 'backend', label: '后端服务', ok: true, message: '正常' },
        { key: 'ffmpeg', label: 'FFmpeg', ok: false, message: '未安装' },
      ],
    })

    render(<SystemDiagnosticsModal onClose={() => {}} />)

    expect(await screen.findByText('剪映草稿支持')).toBeTruthy()
    expect(screen.getByText('未知')).toBeTruthy()
    expect(screen.getByText(/无法确认是否可用/)).toBeTruthy()
    expect(screen.getByText(/需要手动放进剪映草稿目录/)).toBeTruthy()
    // 通用列表仍然正常渲染，剪映那一块没有把整个弹窗带崩。
    expect(screen.getByText('FFmpeg')).toBeTruthy()
  })

  it('剪映信息放在 items 里时不重复显示两行', async () => {
    stubDiagnostics({
      items: [
        { key: 'backend', label: '后端服务', ok: true, message: '正常' },
        { key: 'jianying', label: '剪映草稿', ok: false, message: 'pyJianYingDraft 未安装' },
      ],
    })

    render(<SystemDiagnosticsModal onClose={() => {}} />)

    expect(await screen.findByText('剪映草稿支持')).toBeTruthy()
    expect(screen.getAllByText('剪映草稿支持')).toHaveLength(1)
    // items 里的那条剪映记录被摘出来放进专块，不会在通用列表里再出现一次。
    expect(screen.queryByText('剪映草稿')).toBeNull()
    expect(screen.getByText('pyJianYingDraft 未安装')).toBeTruthy()
    expect(screen.getByText(/会自动回退为通用草稿包/)).toBeTruthy()
  })
})
