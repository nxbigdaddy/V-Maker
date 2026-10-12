import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TextProviderModal } from '../components/TextProviderModal'

/**
 * 模型设置弹窗回归。
 *
 * 这个用例来自真实事故：加载 effect 把 loadingTab 写进了自己的依赖数组，
 * 于是 setLoadingTab 触发自身重跑 → cleanup 把 active 置 false →
 * then/catch/finally 全被丢弃。请求其实成功了，界面却永远停在「正在读取配置」。
 * 所以这里断言的核心是：加载状态必须结束，并且真的渲染出后端返回的值。
 */
const settings = {
  capability: 'text',
  provider: 'demo',
  endpoint: 'https://api.example.test/v1',
  model: 'deterministic-v1',
  api_key_env: 'OPENAI_API_KEY',
  unit_cost: 0,
  configured: true,
}

const calls: string[] = []

function respond(body: unknown, status = 200) {
  return status === 200
    ? new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
    : new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

beforeEach(() => {
  calls.length = 0
})

afterEach(() => {
  vi.unstubAllGlobals()
})

it('加载完成后渲染后端返回的设置，而不是一直停在「正在读取配置」', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string | URL) => {
    calls.push(String(url))
    return respond(settings)
  }))

  render(<TextProviderModal onClose={() => {}} />)

  // 关键断言：表单出现，并且带的是后端给的值。
  expect(await screen.findByDisplayValue('deterministic-v1')).toBeTruthy()
  expect(screen.queryByText(/正在读取/)).toBeNull()
  expect(calls.some((url) => url.includes('/capability-settings/text'))).toBe(true)
})

it('读取失败时给出错误与重试入口，重试后能加载成功', async () => {
  let attempt = 0
  vi.stubGlobal('fetch', vi.fn(async () => {
    attempt += 1
    return attempt === 1 ? respond({ detail: '读取失败' }, 500) : respond(settings)
  }))

  render(<TextProviderModal onClose={() => {}} />)

  expect(await screen.findByText(/读取失败/)).toBeTruthy()
  expect(screen.queryByText(/正在读取/)).toBeNull()

  await userEvent.click(screen.getByRole('button', { name: /重试/ }))

  expect(await screen.findByDisplayValue('deterministic-v1')).toBeTruthy()
  expect(attempt).toBe(2)
})
