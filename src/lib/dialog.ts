import { useEffect, useRef, type RefObject } from 'react'

export type DialogOptions = {
  /** 为 false 时完全不生效（例如受控确认框当前处于关闭状态）。默认 true。 */
  active?: boolean
  /** 为 true 时阻止 Esc 关闭（例如正在提交/保存中）。 */
  blocked?: boolean
  /** 打开时是否自动把焦点移入容器，默认 true。 */
  autoFocus?: boolean
  /** 是否圈定 Tab 焦点，默认 true。 */
  trapFocus?: boolean
  /** 关闭/卸载时是否恢复之前的焦点，默认 true。 */
  restoreFocus?: boolean
  /** 是否锁定 body 滚动，默认 true。 */
  lockScroll?: boolean
}

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'area[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'iframe',
  'audio[controls]',
  'video[controls]',
  '[contenteditable]:not([contenteditable="false"])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

function isVisible(element: HTMLElement): boolean {
  if (element.hasAttribute('hidden')) return false
  if (element.getAttribute('aria-hidden') === 'true') return false
  if (element.closest('[hidden],[aria-hidden="true"]')) return false
  return true
}

function focusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
    .filter((element) => isVisible(element))
}

/**
 * 弹层通用行为：Esc 关闭、初始聚焦、Tab 焦点圈定、关闭后恢复焦点、锁定 body 滚动。
 *
 * 用法：把返回的 ref 挂到弹层容器（带 role="dialog" 的元素）上。
 */
export function useDialog<T extends HTMLElement = HTMLDivElement>(
  onClose: () => void,
  options: DialogOptions = {},
): RefObject<T | null> {
  const { active = true, blocked = false, autoFocus = true, trapFocus = true, restoreFocus = true, lockScroll = true } = options
  const containerRef = useRef<T | null>(null)
  // 用 ref 读取最新回调，避免因 onClose/blocked 变化重跑副作用（重跑会重置焦点与滚动锁）。
  const onCloseRef = useRef(onClose)
  const blockedRef = useRef(blocked)
  onCloseRef.current = onClose
  blockedRef.current = blocked

  useEffect(() => {
    if (!active) return
    const container = containerRef.current
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const previousOverflow = document.body.style.overflow

    if (lockScroll) document.body.style.overflow = 'hidden'

    if (autoFocus && container) {
      const first = focusableElements(container)[0]
      if (first) first.focus()
      else if (!container.hasAttribute('tabindex')) {
        // 容器内没有任何可聚焦元素时，让容器自身成为焦点落点。
        container.setAttribute('tabindex', '-1')
        container.focus()
        container.removeAttribute('tabindex')
      } else {
        container.focus()
      }
    }

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        if (blockedRef.current) return
        event.preventDefault()
        onCloseRef.current()
        return
      }
      if (event.key !== 'Tab' || !trapFocus || !container) return
      const items = focusableElements(container)
      if (!items.length) {
        event.preventDefault()
        return
      }
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement
      if (event.shiftKey) {
        if (active === first || !container.contains(active)) {
          event.preventDefault()
          last.focus()
        }
      } else if (active === last || !container.contains(active)) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      if (lockScroll) document.body.style.overflow = previousOverflow
      if (
        restoreFocus
        && previouslyFocused
        && previouslyFocused.isConnected
        && typeof previouslyFocused.focus === 'function'
      ) {
        previouslyFocused.focus()
      }
    }
  }, [active, autoFocus, lockScroll, restoreFocus, trapFocus])

  return containerRef
}
