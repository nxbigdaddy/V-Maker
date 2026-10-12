import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { CreateProjectModal } from '../components/CreateProjectModal'

describe('创建向导按钮状态', () => {
  it('先校验内容，再逐步前进；返回和提交是独立操作', async () => {
    const user = userEvent.setup()
    const onCreate = vi.fn().mockResolvedValue(undefined)
    const onClose = vi.fn()
    const { container } = render(<CreateProjectModal onClose={onClose} onCreate={onCreate} creating={false} error={null} />)
    const footer = within(container.querySelector('.wizard-actions') as HTMLElement)
    await user.click(footer.getByRole('button', { name: /下一步/ }))
    expect(screen.getByText('请先填写项目名称。')).toBeTruthy()
    await user.type(screen.getByPlaceholderText('例如：云端邮差'), '按钮布局回归')
    await user.type(screen.getByPlaceholderText('粘贴你的故事。描述人物、冲突和你期待的结局……'), '一位邮差送信回家。')
    for (let i = 0; i < 3; i++) await user.click(footer.getByRole('button', { name: /下一步/ }))
    expect(footer.getByRole('button', { name: /创建项目/ })).toBeTruthy()
    expect(footer.getByRole('button', { name: /上一步/ })).toBeTruthy()
    expect(footer.queryByRole('button', { name: /下一步/ })).toBeNull()
    await user.click(footer.getByRole('button', { name: /上一步/ }))
    expect(footer.getByRole('button', { name: /下一步/ })).toBeTruthy()
    await user.click(footer.getByRole('button', { name: /下一步/ }))
    await user.click(footer.getByRole('button', { name: /创建项目/ }))
    expect(onCreate).toHaveBeenCalledTimes(1)
    expect(onClose).not.toHaveBeenCalled()
  })

  it('输入框里按 Enter 也能继续，多行文本里必须用 Ctrl+Enter', async () => {
    const user = userEvent.setup()
    const onCreate = vi.fn().mockResolvedValue(undefined)
    render(<CreateProjectModal onClose={() => {}} onCreate={onCreate} creating={false} error={null} />)

    const name = screen.getByPlaceholderText('例如：云端邮差')
    const story = screen.getByPlaceholderText('粘贴你的故事。描述人物、冲突和你期待的结局……')

    // 没填必填项时按 Enter 要给出校验提示，而不是静默无反应。
    await user.click(name)
    await user.keyboard('{Enter}')
    expect(screen.getByText('请先填写项目名称。')).toBeTruthy()

    await user.type(name, '键盘流程')
    await user.click(story)
    await user.type(story, '一只猫在雨夜回家。')
    // 多行文本框里的裸 Enter 只换行，不前进。
    await user.keyboard('{Enter}')
    expect(screen.getByText('你想讲一个怎样的故事？')).toBeTruthy()
    // Ctrl+Enter 才前进。
    await user.keyboard('{Control>}{Enter}{/Control}')
    expect(await screen.findByText('定义作品的观看体验')).toBeTruthy()
  })
})
