import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { TasksPage } from '../pages/TasksPage'
import type { Project, ProjectTask } from '../types'

/**
 * 任务中心各状态的按钮回归。
 *
 * 真实测试数据里只有 completed 和 paused 两种任务，运行中/失败/已取消拿不到真实样本，
 * 而为了检查去创建一个真实任务会产生副作用，所以这些状态在这里用替身数据覆盖：
 * 每个状态只应出现对应的那一个操作，不能出现两个互相矛盾的按钮。
 */
const project = { id: 'prj_1', name: '测试项目', mode: 'standard', aspect_ratio: '16:9', status: 'active' } as Project

const task = (status: string, extra: Partial<ProjectTask> = {}) => ({
  id: `task_${status}`, status, task_type: 'pipeline', progress: 0.4, message: '处理中', ...extra,
} as ProjectTask)

function respond(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

const calls: string[] = []
function stubTasks(tasks: ProjectTask[]) {
  calls.length = 0
  vi.stubGlobal('fetch', vi.fn(async (url: string | URL, init?: RequestInit) => {
    const path = String(url)
    calls.push(`${init?.method || 'GET'} ${path}`)
    if (init?.method === 'POST') return respond({ task: tasks[0] })
    return respond({ tasks })
  }))
}

afterEach(() => { vi.unstubAllGlobals() })

describe('任务中心的操作按钮', () => {
  it('运行中的任务只给取消，并且取消必须经过确认框', async () => {
    const user = userEvent.setup()
    stubTasks([task('running', { id: 'task_running_1' })])
    render(<TasksPage project={project} />)

    const cancel = await screen.findByRole('button', { name: /取消/ })
    expect(screen.queryByRole('button', { name: /重试/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /继续/ })).toBeNull()

    await user.click(cancel)
    const dialog = await screen.findByRole('dialog')
    expect(dialog.textContent).toContain('task_running_1')
    // 只是打开确认框，不应该已经发出取消请求。
    expect(calls.some((call) => call.includes('/cancel'))).toBe(false)

    // 任务行里也有一个“取消”，所以必须限定在确认框内点击。
    await user.click(within(dialog).getByRole('button', { name: '取消' }))
    await waitFor(() => { expect(screen.queryByRole('dialog')).toBeNull() })
    expect(calls.some((call) => call.includes('/cancel'))).toBe(false)
  })

  it('失败的任务只给重试，并真的调用重试接口', async () => {
    const user = userEvent.setup()
    stubTasks([task('failed', { id: 'task_failed_1', error: '上游返回 500：模型服务超时' })])
    render(<TasksPage project={project} />)

    const retry = await screen.findByRole('button', { name: /重试/ })
    expect(screen.queryByRole('button', { name: /取消/ })).toBeNull()
    await user.click(retry)
    await waitFor(() => { expect(calls.some((call) => call.includes('/retry'))).toBe(true) })
  })

  it('等待审核的任务只给继续', async () => {
    stubTasks([task('paused', { id: 'task_paused_1' })])
    render(<TasksPage project={project} />)
    expect(await screen.findByRole('button', { name: /继续/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /取消/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /重试/ })).toBeNull()
  })

  it('已完成和已取消的任务不会出现互相矛盾的操作组合', async () => {
    stubTasks([task('completed', { id: 'task_done' }), task('cancelled', { id: 'task_cancelled' })])
    render(<TasksPage project={project} />)
    // 已取消可重试、已完成无操作；两者都不该出现“取消”按钮。
    expect(await screen.findByRole('button', { name: /重试/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /取消/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /继续/ })).toBeNull()
  })

  it('失败任务同时带进度消息和错误时，两者都要显示，错误原因不能被吞掉', async () => {
    const long = `上游返回错误：${'模型服务连接被重置 '.repeat(20)}`
    stubTasks([task('failed', { id: 'task_long', message: '第 3/6 个镜头：生成中', error: long })])
    render(<TasksPage project={project} />)
    // 进度消息
    expect(await screen.findByText('第 3/6 个镜头：生成中')).toBeTruthy()
    // 真正的失败原因必须完整出现
    const node = await screen.findByText((_, element) => element?.className === 'task-error')
    expect(node.textContent).toContain('上游返回错误')
    expect(node.textContent).toContain('模型服务连接被重置 '.repeat(20).trim())
  })
})
