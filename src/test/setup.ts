import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

// 每个用例结束后卸载已渲染的组件，避免 DOM 在用例之间互相污染。
afterEach(() => {
  cleanup()
})

// jsdom 没有实现这些浏览器 API，被测组件会用到。
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia
}
