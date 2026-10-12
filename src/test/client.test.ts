import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { API_BASE_URL, api } from '../api/client'

/**
 * API 客户端契约回归。
 *
 * 这里的用例来自真实事故：
 *  - retry / resume 曾经漏掉 ?execute=true，任务排进队列却永远没人执行；
 *  - cancel / retry 曾经把未 await 的 Promise 交给 unwrapRecord，信封不会被拆开。
 */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const calls: Array<{ url: string; init?: RequestInit }> = []

/** 统一的 fetch 替身：记录调用，并按给定响应返回。 */
function stubFetch(body: unknown, status = 200) {
  vi.stubGlobal('fetch', vi.fn(async (url: string | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init })
    return status === 204 ? new Response(null, { status }) : jsonResponse(body, status)
  }))
}

beforeEach(() => {
  calls.length = 0
  stubFetch([{ id: 'prj_1', name: '测试项目' }])
})

afterEach(() => {
  vi.unstubAllGlobals()
})

const lastCall = () => calls[calls.length - 1]

describe('api client', () => {
  it('列表接口支持裸数组与 {items} 两种信封', async () => {
    const list = await api.listProjects()
    expect(list[0]?.id).toBe('prj_1')

    stubFetch({ items: [{ id: 'prj_2', name: 'x' }] })
    const wrapped = await api.listProjects()
    expect(wrapped[0]?.id).toBe('prj_2')
  })

  it('剧集元数据与独立镜头接口按 scene_id 合并，不跨场景或剧集串镜头', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      calls.push({ url })
      return jsonResponse(url.endsWith('/episodes')
        ? { episodes: [
          { id: 'ep1', scenes: [{ id: 's1' }, { id: 'empty' }] },
          { id: 'ep2', scenes: [{ id: 's2' }] },
        ] }
        : { shots: [
          { id: 'shot1', scene_id: 's1', order: 1, duration: 5 },
          { id: 'shot2', scene_id: 's2', order: 1 },
          { id: 'shot3', scene_id: 's1', order: 2 },
          { id: 'old-version', scene_id: 'absent' },
        ] })
    }))
    const episodes = await api.getEpisodes('prj/1')
    expect(calls.map((call) => call.url)).toEqual([
      `${API_BASE_URL}/projects/prj%2F1/episodes`, `${API_BASE_URL}/projects/prj%2F1/shots`,
    ])
    expect(episodes[0].scenes?.[0].shots?.map((shot) => shot.id)).toEqual(['shot1', 'shot3'])
    expect(episodes[0].scenes?.[0].shots?.[0].duration).toBe(5)
    expect(episodes[0].scenes?.[1].shots).toEqual([])
    expect(episodes[1].scenes?.[0].shots?.map((shot) => shot.id)).toEqual(['shot2'])
  })

  it('已有嵌套镜头和空剧集不请求独立镜头接口', async () => {
    const nested = [{ id: 'ep', scenes: [{ id: 's', shots: [{ id: 'shot' }] }] }]
    stubFetch(nested)
    expect(await api.getEpisodes('p')).toEqual(nested)
    expect(calls).toHaveLength(1)
    calls.length = 0
    stubFetch([])
    expect(await api.getEpisodes('p')).toEqual([])
    expect(calls).toHaveLength(1)
  })

  it('兼容剧集级 shots，补充缺失场景时保留已有嵌套镜头', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => jsonResponse(url.endsWith('/episodes')
      ? [
        { id: 'ep1', scenes: [{ id: 's1', shots: [{ id: 'nested', action: 'keep' }] }, { id: 's2', shots: [] }] },
        { id: 'ep2', shots: [{ id: 'episode-shot' }] },
      ]
      : [{ id: 'flat1', scene_id: 's1' }, { id: 'flat2', scene_id: 's2' }])))
    const episodes = await api.getEpisodes('p')
    expect(episodes[0].scenes?.[0].shots).toEqual([{ id: 'nested', action: 'keep' }])
    expect(episodes[0].scenes?.[1].shots).toEqual([{ id: 'flat2', scene_id: 's2' }])
    expect(episodes[1].shots).toEqual([{ id: 'episode-shot' }])
  })

  it('镜头接口读取失败必须报错，不将失败伪装为无镜头', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => url.endsWith('/episodes')
      ? jsonResponse([{ id: 'ep', scenes: [{ id: 's' }] }])
      : jsonResponse({ detail: '镜头读取失败' }, 500)))
    await expect(api.getEpisodes('p')).rejects.toThrow('镜头读取失败')
  })

  it('204 返回 undefined 而不是解析 JSON', async () => {
    stubFetch(undefined, 204)
    await expect(api.deleteProject('prj_1')).resolves.toBeUndefined()
  })

  it('后端 detail 为字符串时作为错误消息', async () => {
    stubFetch({ detail: '项目不存在' }, 404)
    await expect(api.getProject('x')).rejects.toThrow('项目不存在')
  })

  it('后端 detail 为数组时拼接 msg', async () => {
    stubFetch({ detail: [{ msg: '名称不能为空' }, { msg: '时长非法' }] }, 422)
    await expect(api.createProject({} as never)).rejects.toThrow('名称不能为空；时长非法')
  })

  it('连接失败时给出可读的中文提示，并说清怎么启动后端', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('failed') }))
    // 光说「无法连接」没用：桌面壳不会自动拉起后端，提示必须给出可执行的下一步。
    await expect(api.listProjects()).rejects.toThrow(/无法连接本地服务/)
    await expect(api.listProjects()).rejects.toThrow(/start-vmaker\.bat/)
    await expect(api.listProjects()).rejects.toThrow(/start-vmaker\.bat backend/)
    await expect(api.listProjects()).rejects.toThrow(/uvicorn backend\.main:app/)
    // 地址来自配置，不是写死的 8765。
    await expect(api.listProjects()).rejects.toThrow(new RegExp(API_BASE_URL.replace(/\/api\/?$/, '').replace(/[.:/]/g, '\\$&')))
  })

  it('重试与恢复都必须请求后端真的执行，否则任务会永远停在排队中', async () => {
    stubFetch({ id: 'task_1', status: 'queued' })
    await api.retryTask('task_1')
    expect(lastCall().url).toContain('/tasks/task_1/retry?execute=true')

    await api.resumeTask('task_1')
    expect(lastCall().url).toContain('/tasks/task_1/resume?execute=true')
  })

  it('取消任务解析 {task} 信封', async () => {
    stubFetch({ task: { id: 'task_9', status: 'canceled' } })
    const task = await api.cancelTask('task_9')
    expect(task.id).toBe('task_9')
    expect(task.status).toBe('canceled')
  })

  it('创建项目发送 JSON 请求体', async () => {
    await api.createProject({ name: '甲', story: '乙' } as never)
    const { init } = lastCall()
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toMatchObject({ name: '甲' })
  })

  it('素材内容地址按项目作用域拼接并做 URL 编码', () => {
    expect(api.assetContentUrl('prj 1', 'asset/2', 'v 3')).toBe(
      `${API_BASE_URL}/projects/prj%201/assets/asset%2F2/versions/v%203/content`,
    )
  })

  it('产物下载地址做 URL 编码', () => {
    expect(api.artifactDownloadUrl('a b')).toBe(`${API_BASE_URL}/artifacts/a%20b/download`)
  })
})
