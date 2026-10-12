import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { useDialog } from '../lib/dialog'

/**
 * useDialog 的行为回归。
 *
 * 这组用例直接来自真实事故：ConfirmDialog 之前把 hook 无条件挂载，
 * 关闭状态下依然锁死 body 滚动，所以这里专门测 active 语义。
 */
function Harness({ active = true, blocked = false, onClose = () => {} }: {
  active?: boolean
  blocked?: boolean
  onClose?: () => void
}) {
  const ref = useDialog<HTMLDivElement>(onClose, { active, blocked })
  return (
    <div ref={ref} role="dialog" aria-modal="true" data-testid="dialog">
      <button type="button">第一个</button>
      <button type="button">最后一个</button>
    </div>
  )
}

describe('useDialog', () => {
  it('挂载后把焦点移进容器', async () => {
    render(<Harness />)
    expect(document.activeElement?.textContent).toBe('第一个')
  })

  it('按 Esc 触发关闭回调', async () => {
    const onClose = vi.fn()
    render(<Harness onClose={onClose} />)
    await userEvent.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('blocked 为 true 时 Esc 不关闭', async () => {
    const onClose = vi.fn()
    render(<Harness blocked onClose={onClose} />)
    await userEvent.keyboard('{Escape}')
    expect(onClose).not.toHaveBeenCalled()
  })

  it('active 为 false 时不锁定页面滚动、也不接管焦点', () => {
    document.body.style.overflow = 'auto'
    render(<Harness active={false} />)
    expect(document.body.style.overflow).toBe('auto')
    expect(document.activeElement).toBe(document.body)
  })

  it('active 为 true 时锁定滚动，卸载后恢复原值', () => {
    document.body.style.overflow = 'auto'
    const { unmount } = render(<Harness />)
    expect(document.body.style.overflow).toBe('hidden')
    unmount()
    expect(document.body.style.overflow).toBe('auto')
  })

  it('active 从 false 变为 true 时才开始生效', () => {
    function Toggle() {
      const [active, setActive] = useState(false)
      return (
        <>
          <button type="button" onClick={() => setActive(true)}>开启</button>
          <Harness active={active} />
        </>
      )
    }
    document.body.style.overflow = 'auto'
    render(<Toggle />)
    expect(document.body.style.overflow).toBe('auto')
    return userEvent.click(screen.getByText('开启')).then(() => {
      expect(document.body.style.overflow).toBe('hidden')
    })
  })

  it('Tab 焦点被圈定在容器内', async () => {
    render(<Harness />)
    const last = screen.getByText('最后一个')
    last.focus()
    await userEvent.keyboard('{Tab}')
    expect(document.activeElement?.textContent).toBe('第一个')
  })
})
