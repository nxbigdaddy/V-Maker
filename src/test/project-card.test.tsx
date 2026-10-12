import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ProjectCard } from '../components/ProjectCard'
import type { Project } from '../types'

/**
 * 项目卡交互回归。
 *
 * 来自真实事故：卡片本身是 role="button"，菜单按钮嵌在里面，
 * 点菜单或按 Enter 时事件会冒泡，可能同时打开项目和菜单；
 * 另外删除项曾经被封面区的 overflow:hidden 裁掉（那是 CSS 问题，由 scripts/ui-probe.html 覆盖）。
 */
const base = {
  id: 'prj_1',
  name: '测试项目',
  mode: 'standard',
  aspect_ratio: '16:9',
  status: 'active',
  episode_count: 1,
  shot_count: 5,
  updated_at: '2026-01-02T03:04:05Z',
} as unknown as Project

function setup(overrides: Partial<Project> = {}) {
  const onOpen = vi.fn()
  const onAction = vi.fn()
  render(
    <ProjectCard
      project={{ ...base, ...overrides }}
      index={0}
      onOpen={onOpen}
      onAction={onAction}
    />,
  )
  return { onOpen, onAction }
}

describe('ProjectCard', () => {
  it('点击卡片打开项目', async () => {
    const { onOpen } = setup()
    await userEvent.click(screen.getByText('测试项目'))
    expect(onOpen).toHaveBeenCalledTimes(1)
  })

  it('按 Enter 打开项目', async () => {
    const { onOpen } = setup()
    screen.getByRole('button', { name: /打开项目/ }).focus()
    await userEvent.keyboard('{Enter}')
    expect(onOpen).toHaveBeenCalledTimes(1)
  })

  it('按空格也打开项目', async () => {
    const { onOpen } = setup()
    screen.getByRole('button', { name: /打开项目/ }).focus()
    await userEvent.keyboard(' ')
    expect(onOpen).toHaveBeenCalledTimes(1)
  })

  it('点菜单按钮只展开菜单，不会同时打开项目', async () => {
    const { onOpen } = setup()
    await userEvent.click(screen.getByLabelText('项目菜单'))
    expect(onOpen).not.toHaveBeenCalled()
    expect(screen.getByRole('menu')).toBeTruthy()
  })

  it('菜单包含复制、归档与删除，删除会回调对应动作', async () => {
    const { onAction, onOpen } = setup()
    await userEvent.click(screen.getByLabelText('项目菜单'))
    await userEvent.click(screen.getByText('删除项目'))
    expect(onAction).toHaveBeenCalledWith('delete')
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('已归档项目在菜单里显示“恢复项目”', async () => {
    const { onAction } = setup({ status: 'archived' } as Partial<Project>)
    await userEvent.click(screen.getByLabelText('项目菜单'))
    await userEvent.click(screen.getByText('恢复项目'))
    expect(onAction).toHaveBeenCalledWith('restore')
  })

  it('菜单展开时卡片会带上 menu-open 类，避免菜单被下一行卡片盖住', async () => {
    const { container } = { container: document.body }
    setup()
    await userEvent.click(screen.getByLabelText('项目菜单'))
    expect(container.querySelector('.project-card.menu-open')).toBeTruthy()
  })
})
