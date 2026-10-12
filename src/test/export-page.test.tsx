import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ExportPage } from '../pages/ExportPage'
import type { ExportJob, Project } from '../types'

/**
 * 导出页格式选择与剪映草稿披露回归。
 *
 * 这里盯住三件事：
 *  - 选了什么就请求什么（POST /projects/{id}/exports 的 format 不能被前端改写）；
 *  - 剪映草稿的限制必须如实出现在界面上（没装库会回退、不支持 SVG、绝对路径引用、未在真实剪映验证、不做自动导出）； *  - 结果区展示的是后端真实返回的 format 与 warning 全文，而不是界面猜的。
 */
const project = { id: 'prj_1', name: '测试项目', mode: 'standard', aspect_ratio: '16:9', status: 'active' } as Project

const calls: Array<{ url: string; init?: RequestInit }> = []

function respond(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

/** 导出接口替身：POST/详情返回 job，列表路由返回 list。 */
function stubExport(job: ExportJob, list: ExportJob[] = []) {
  vi.stubGlobal('fetch', vi.fn(async (url: string | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init })
    // 列表路由是 /projects/{id}/exports（结尾没有具体 export id）。
    if ((!init?.method || init.method === 'GET') && /\/exports(\?|$)/.test(String(url))) {
      return respond({ exports: list })
    }
    return respond({ export: job })
  }))
}

const posts = () => calls.filter((call) => call.init?.method === 'POST')
const postedFormat = () => JSON.parse(String(posts()[posts().length - 1]?.init?.body)).format as string

beforeEach(() => {
  calls.length = 0
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('导出页的交付格式选择', () => {
  it('默认请求 auto，确认框说明实际产物由后端按环境决定', async () => {
    stubExport({ id: 'exp_1', status: 'completed', format: 'mp4' })
    render(<ExportPage project={project} />)

    await userEvent.click(screen.getByRole('button', { name: /发起导出/ }))
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText(/将由后端按本机环境决定实际产物/)).toBeTruthy()

    await userEvent.click(screen.getByRole('button', { name: /确认发起导出/ }))
    await waitFor(() => expect(posts().length).toBe(1))
    expect(postedFormat()).toBe('auto')
    expect(posts()[0].url).toContain('/projects/prj_1/exports')
  })

  it('选择剪映草稿后请求 jianying_draft，并如实列出 5 条限制', async () => {
    stubExport({ id: 'exp_2', status: 'completed', format: 'jianying_draft' })
    render(<ExportPage project={project} />)

    // 注意：auto 选项的说明里也提到剪映草稿，所以这里用只属于该选项的标题来定位。
    await userEvent.click(screen.getByRole('radio', { name: /（实验性，未验证）/ }))

    // 关键：这些限制不能只写在注释里，必须真的出现在界面文案中。
    expect(screen.getAllByText(/pyJianYingDraft/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/会自动回退成通用草稿包/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/剪映不支持 SVG：默认的 demo 占位图是位图/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/绝对路径引用/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/从未在真实剪映里验证过/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/自动导出只支持剪映 6 及以下/).length).toBeGreaterThan(0)

    await userEvent.click(screen.getByRole('button', { name: /发起导出/ }))
    expect(within(screen.getByRole('dialog')).getByText(/只生成草稿，不做自动导出/)).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: /确认发起导出/ }))

    await waitFor(() => expect(posts().length).toBe(1))
    expect(postedFormat()).toBe('jianying_draft')
  })

  it('结果区显示剪映草稿及其下载入口说明', async () => {
    stubExport({
      id: 'exp_3',
      status: 'completed',
      format: 'jianying_draft',
      warning: '已生成剪映草稿；跳过 2 个镜头：剪映不支持 SVG（demo 占位图像）。',
      artifacts: [{ id: 'art_1', kind: 'jianying_draft', mime_type: 'application/zip', path: 'D:/data/exports/draft.zip' }],
    })
    render(<ExportPage project={project} />)

    await userEvent.click(screen.getByRole('button', { name: /发起导出/ }))
    await userEvent.click(screen.getByRole('button', { name: /确认发起导出/ }))

    expect(await screen.findByText('剪映草稿', { selector: '.delivery-badge' })).toBeTruthy()
    expect(screen.getByText(/跳过 2 个镜头：剪映不支持 SVG（demo 占位图像）。/)).toBeTruthy()
    const link = screen.getByRole('link', { name: /解压后放进剪映草稿目录/ })
    expect(link.getAttribute('href')).toContain('/artifacts/art_1/download')
    expect(screen.getByText(/内含 draft_content.json 与 draft_meta_info.json/, { selector: '.artifact-hint' })).toBeTruthy()
  })

  it('回退时显示后端真实 format（通用草稿包）与回退原因', async () => {
    stubExport({
      id: 'exp_4',
      status: 'completed',
      format: 'draft_package',
      warning: 'pyJianYingDraft 不可用，已回退为通用草稿包；跳过 3 个镜头：剪映不支持 SVG。',
      artifacts: [{ id: 'art_2', kind: 'draft_package', path: 'D:/data/exports/draft.zip' }],
    })
    render(<ExportPage project={project} />)

    await userEvent.click(screen.getByRole('button', { name: /发起导出/ }))
    await userEvent.click(screen.getByRole('button', { name: /确认发起导出/ }))

    expect(await screen.findByText('通用草稿包', { selector: '.delivery-badge' })).toBeTruthy()
    expect(screen.getByText(/pyJianYingDraft 不可用，已回退为通用草稿包；跳过 3 个镜头：剪映不支持 SVG。/)).toBeTruthy()
    // 回退场景不能给出剪映专用的下载说明。
    expect(screen.queryByRole('link', { name: /解压后放进剪映草稿目录/ })).toBeNull()
  })

  it('打开页面就展示最近的导出，失败原因与请求格式都可见，点「查看」能重新打开该结果', async () => {
    const failed = {
      id: 'exp_failed', status: 'failed', format: 'portable_draft',
      error: '素材 assets/x/v1.png 的 SHA-256 校验不通过：文件已损坏或被改动，拒绝按完好素材导出',
      created_at: '2026-10-08T12:00:00Z',
    } as ExportJob
    const done = { id: 'exp_done', status: 'completed', format: 'mp4', created_at: '2026-10-08T11:00:00Z' } as ExportJob
    stubExport(done, [failed, done])
    render(<ExportPage project={project} />)

    // 上一次失败的导出必须在界面上留痕，而不是只存在于数据库或任务中心。
    expect(await screen.findByText('最近导出')).toBeTruthy()
    const history = within(document.querySelector('.export-history') as HTMLElement)
    expect(history.getByText(/SHA-256 校验不通过/)).toBeTruthy()
    // 请求的格式要如实显示，不能因为失败就退回「自动」。
    expect(history.getByText('通用草稿包')).toBeTruthy()
    expect(history.getByText('失败')).toBeTruthy()

    const buttons = screen.getAllByRole('button', { name: '查看' })
    await userEvent.click(buttons[0])
    // 打开后进入结果区，失败状态用真实后端错误渲染。
    const result = within(document.querySelector('.export-result') as HTMLElement)
    expect(result.getByText(/SHA-256 校验不通过/)).toBeTruthy()
    expect(document.querySelector('.export-result header.failed')).toBeTruthy()
  })
})
