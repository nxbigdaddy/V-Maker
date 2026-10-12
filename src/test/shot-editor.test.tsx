import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { StoryboardPage } from '../pages/StoryboardPage'
import type { Project, Shot } from '../types'

/**
 * 镜头抽屉「保存中」的关闭策略回归。
 *
 * 之前只有 Esc 被拦住：X、取消和点遮罩仍然能关闭抽屉，而保存请求还在跑，
 * 用户会以为改动丢了、或在另一个镜头上看到旧数据。这里盯住三个入口都不能关。
 */
const project = { id: 'prj_1', name: '测试项目', mode: 'standard', aspect_ratio: '16:9', status: 'active' } as Project
const shot = { id: 'shot_1', title: '镜头一', purpose: '目的', action: '动作', duration_sec: 5 } as Shot

function respond(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

/** 镜头保存挂起：PATCH 不返回，直到测试主动放行。 */
function stubHangingSave() {
  let release: (() => void) | null = null
  const gate = new Promise<void>((resolve) => { release = resolve })
  vi.stubGlobal('fetch', vi.fn(async (url: string | URL, init?: RequestInit) => {
    const path = String(url)
    if (init?.method === 'PATCH') { await gate; return respond({ shot }) }
    if (path.includes('/episodes')) {
      return respond({ episodes: [{ id: 'ep_1', position: 1, title: '第 1 集', scenes: [{ id: 'sc_1', position: 1, title: '场景', shots: [shot] }] }] })
    }
    if (path.includes('/projects')) return respond({ projects: [project] })
    return respond({})
  }))
  return () => release?.()
}

afterEach(() => { vi.unstubAllGlobals() })

describe('镜头编辑抽屉的保存中状态', () => {
  it('保存未完成前，X、取消和遮罩都不能关闭抽屉；完成后可以正常关闭', async () => {
    const user = userEvent.setup()
    const openGate = stubHangingSave()
    render(<StoryboardPage project={project} />)

    // 打开抽屉需要真实数据；分镜页通过 getEpisodes 拿到场景与镜头。
    const edit = await screen.findByRole('button', { name: /编辑/ })
    await user.click(edit)
    const drawer = await screen.findByRole('dialog', { name: /镜头编辑|镜头一/ })

    await user.click(screen.getByRole('button', { name: /保存镜头/ }))

    const close = screen.getByRole('button', { name: '关闭镜头编辑器' })
    const cancel = screen.getByRole('button', { name: '取消' })
    await waitFor(() => { expect(close).toHaveProperty('disabled', true) })
    expect(cancel).toHaveProperty('disabled', true)

    // 三个关闭入口都必须无效：按钮已禁用，遮罩点击也被忽略。
    await user.click(close)
    await user.click(cancel)
    const backdrop = drawer.closest('.drawer-backdrop') ?? document.querySelector('.drawer-backdrop')
    expect(backdrop).toBeTruthy()
    await user.pointer({ keys: '[MouseLeft]', target: backdrop as Element })
    expect(document.querySelector('.shot-drawer')).toBeTruthy()

    openGate()
    await waitFor(() => { expect(document.querySelector('.shot-drawer')).toBeNull() })
  })
})
