import { useEffect, useRef, useState } from 'react'
import { AlertCircle, AlertTriangle, Check, FileAudio, Image, KeyRound, LoaderCircle, RefreshCw, Server, SlidersHorizontal, Sparkles, Type, Video, X } from 'lucide-react'
import { api } from '../api/client'
import { ConfirmDialog } from './ConfirmDialog'
import { useDialog } from '../lib/dialog'
import type { CapabilityProbeResult, CapabilitySettings, MediaCapability, UpdateCapabilitySettings } from '../types'

/** 表单内的数字字段用字符串保存，避免受控输入在编辑过程中被强制转成 NaN。 */
type CapabilityForm = {
  provider: string
  endpoint: string
  model: string
  api_key_env: string
  unit_cost: string
}

const capabilityTabs = [
  { key: 'text', label: '文本', icon: Type, hint: '用于故事分析、剧本规划等文本智能任务。', localDemo: '本地占位文本' },
  { key: 'image', label: '图像', icon: Image, hint: '用于分镜画面（镜头图像）生成。', localDemo: '本地占位图' },
  { key: 'speech', label: '语音', icon: FileAudio, hint: '用于对白与旁白配音生成。', localDemo: '本地占位音频' },
  { key: 'video', label: '视频', icon: Video, hint: '用于镜头图生视频：以该镜头已有的图像素材作为首帧生成视频片段。', localDemo: '（无本地占位实现）' },
] as const satisfies ReadonlyArray<{ key: MediaCapability; label: string; icon: typeof Type; hint: string; localDemo: string }>

const providerOptions = [
  { value: 'demo', title: 'demo', subtitle: '本地占位产物，不联网、不产生费用', icon: Sparkles },
  { value: 'openai', title: 'openai', subtitle: 'OpenAI 兼容接口，会真实调用并产生费用', icon: Server },
] as const

const EMPTY_FORM: CapabilityForm = { provider: 'demo', endpoint: '', model: '', api_key_env: '', unit_cost: '' }
const API_KEY_ENV_PATTERN = /^[A-Z][A-Z0-9_]*$/

/** 用后端返回的设置初始化该能力的表单；缺失字段保持空串，由用户填写，前端不编造默认模型或端点。 */
function formFromSettings(value: CapabilitySettings | undefined): CapabilityForm {
  if (!value) return { ...EMPTY_FORM }
  const cost = value.unit_cost
  return {
    provider: value.provider || 'demo',
    endpoint: value.endpoint ?? '',
    model: value.model ?? '',
    api_key_env: value.api_key_env ?? '',
    unit_cost: typeof cost === 'number' && Number.isFinite(cost) ? String(cost) : '',
  }
}

export function TextProviderModal({ onClose, onDiagnostics }: {
  onClose: () => void
  onDiagnostics?: () => void
}) {
  const [tab, setTab] = useState<MediaCapability>('text')
  const [forms, setForms] = useState<Record<MediaCapability, CapabilityForm>>({ text: { ...EMPTY_FORM }, image: { ...EMPTY_FORM }, speech: { ...EMPTY_FORM }, video: { ...EMPTY_FORM } })
  const [current, setCurrent] = useState<Partial<Record<MediaCapability, CapabilitySettings>>>({})
  const [loading, setLoading] = useState<Partial<Record<MediaCapability, boolean>>>({})
  const [reloadToken, setReloadToken] = useState(0)
  // 每个能力只请求一次；失败时清除标记以便重试。
  const requested = useRef<Partial<Record<MediaCapability, boolean>>>({})
  const [loaded, setLoaded] = useState<Partial<Record<MediaCapability, boolean>>>({})
  const [savingTab, setSavingTab] = useState<MediaCapability | null>(null)
  const [error, setError] = useState<Partial<Record<MediaCapability, string>>>({})
  const [saved, setSaved] = useState<Partial<Record<MediaCapability, boolean>>>({})
  // 测试连接的状态按能力标签页分别保存，避免把某个标签页的结果显示到另一个标签页上。
  const [probeBusy, setProbeBusy] = useState<Partial<Record<MediaCapability, boolean>>>({})
  // 记录正在进行的测试是「只测连接」还是「真实调用测试」，用于按钮上的文案与 spinner。
  const [probeRunLive, setProbeRunLive] = useState<Partial<Record<MediaCapability, boolean>>>({})
  const [probeResults, setProbeResults] = useState<Partial<Record<MediaCapability, CapabilityProbeResult>>>({})
  const [probeErrors, setProbeErrors] = useState<Partial<Record<MediaCapability, string>>>({})
  const [probeConfirmOpen, setProbeConfirmOpen] = useState(false)
  // 记录某个标签页上一次测试所依据的表单快照；表单被改动后，迟到的响应会被丢弃。
  const probeSignatureRef = useRef<Partial<Record<MediaCapability, string>>>({})
  // Esc 关闭 / 焦点圈定 / 关闭后恢复触发按钮焦点；保存中或测试中禁止关闭。
  const dialogRef = useDialog<HTMLElement>(onClose, { blocked: savingTab !== null || Boolean(probeBusy[tab]) })
  const saving = savingTab !== null

  const form = forms[tab]
  const activeMeta = capabilityTabs.find((item) => item.key === tab) ?? capabilityTabs[0]
  const tabError = error[tab] ?? null
  const probing = Boolean(probeBusy[tab])
  const probedLive = probeRunLive[tab] === true
  const probeResult = probeResults[tab] ?? null
  const probeError = probeErrors[tab] ?? null

  /**
   * 测试连接只依赖这四个字段；一旦其中任何一个（或当前标签页）发生变化，上一次的测试结果就作废。
   * unit_cost 不参与测试，因此改它不会清掉结果。
   */
  const probeSignature = `${tab}|${form.provider.trim()}|${form.endpoint.trim()}|${form.model.trim()}|${form.api_key_env.trim()}`

  /**
   * 切换标签页、修改表单字段、保存成功后清掉本次测试结果，
   * 避免把旧结果当作刚刚这次配置的结果。
   */
  useEffect(() => {
    setProbeResults((old) => (old[tab] === undefined ? old : { ...old, [tab]: undefined }))
    setProbeErrors((old) => (old[tab] === undefined ? old : { ...old, [tab]: undefined }))
  }, [tab, probeSignature])

  /**
   * 读取某个能力的设置；每个标签页独立加载，互不影响。
   *
   * 这里刻意不把 loading 写进依赖数组、也刻意不加 active 守卫：
   * 之前在 effect 内 setLoading 又把它列为依赖，导致自身重跑 → cleanup 把 active 置 false →
   * then/catch/finally 全部被丢弃，界面会永远停在「正在读取配置」。
   * 现在结果一律按 tab 写回，切换标签页也不会丢掉已经拿到的数据。
   */
  useEffect(() => {
    if (loaded[tab] || requested.current[tab]) return
    requested.current[tab] = true
    setLoading((old) => ({ ...old, [tab]: true }))
    setError((old) => ({ ...old, [tab]: undefined }))
    api.getCapabilitySetting(tab)
      .then((value) => {
        setCurrent((old) => ({ ...old, [tab]: value }))
        setForms((old) => ({ ...old, [tab]: formFromSettings(value) }))
        setLoaded((old) => ({ ...old, [tab]: true }))
      })
      .catch((reason) => {
        // 允许用户点「重试」重新发起请求，而不是永久卡在失败状态。
        requested.current[tab] = false
        setError((old) => ({ ...old, [tab]: reason instanceof Error ? reason.message : '无法读取该能力的模型设置。' }))
      })
      .finally(() => {
        setLoading((old) => ({ ...old, [tab]: false }))
      })
  }, [tab, loaded, reloadToken])

  const patch = (key: keyof CapabilityForm, value: string) => {
    setSaved((old) => ({ ...old, [tab]: false }))
    setForms((old) => ({ ...old, [tab]: { ...old[tab], [key]: value } }))
  }

  /** 切换 provider：demo 与真实服务的必填项不同，但已填写的 endpoint / model 不做任何编造或覆盖。 */
  const chooseProvider = (provider: string) => {
    setSaved((old) => ({ ...old, [tab]: false }))
    setError((old) => ({ ...old, [tab]: undefined }))
    setForms((old) => {
      const draft = old[tab]
      const next: CapabilityForm = { ...draft, provider }
      if (provider !== 'demo') {
        // 官方 OpenAI 兼容端点的环境变量名是已知事实；其余字段一律留给用户填写。
        if (!next.api_key_env.trim()) next.api_key_env = 'OPENAI_API_KEY'
      }
      return { ...old, [tab]: next }
    })
  }

  const save = async () => {
    const endpoint = form.endpoint.trim()
    const model = form.model.trim()
    const apiKeyEnv = form.api_key_env.trim()
    const rawCost = form.unit_cost.trim()
    const unitCost = rawCost === '' ? 0 : Number(rawCost)
    const tabLabel = activeMeta.label

    if (!form.provider.trim()) { setError((old) => ({ ...old, [tab]: '请选择 Provider。' })); return }
    if (form.provider.trim().length > 50) { setError((old) => ({ ...old, [tab]: 'Provider 标识过长，请使用简短的服务标识。' })); return }
    if (!model) { setError((old) => ({ ...old, [tab]: `请填写「${tabLabel}」能力的模型名（model）。` })); return }
    if (model.length > 200) { setError((old) => ({ ...old, [tab]: '模型名不能超过 200 个字符。' })); return }
    if (endpoint.length > 500) { setError((old) => ({ ...old, [tab]: 'Endpoint 不能超过 500 个字符。' })); return }
    if (apiKeyEnv.length > 100) { setError((old) => ({ ...old, [tab]: 'API Key 环境变量名不能超过 100 个字符。' })); return }
    if (form.provider !== 'demo') {
      if (!endpoint) { setError((old) => ({ ...old, [tab]: 'Provider 不是 demo 时必须填写 Endpoint。' })); return }
      if (apiKeyEnv && !API_KEY_ENV_PATTERN.test(apiKeyEnv)) { setError((old) => ({ ...old, [tab]: 'API Key 环境变量名需匹配 ^[A-Z][A-Z0-9_]*$（例如 OPENAI_API_KEY）。' })); return }
    }
    if (!Number.isFinite(unitCost) || unitCost < 0) { setError((old) => ({ ...old, [tab]: '单次调用预估成本必须是大于等于 0 的数字。' })); return }

    const payload: UpdateCapabilitySettings = { provider: form.provider.trim(), endpoint, model, api_key_env: apiKeyEnv, unit_cost: unitCost }
    setSavingTab(tab)
    setError((old) => ({ ...old, [tab]: undefined }))
    setSaved((old) => ({ ...old, [tab]: false }))
    try {
      const value = await api.updateCapabilitySetting(tab, payload)
      setCurrent((old) => ({ ...old, [tab]: value }))
      setForms((old) => ({ ...old, [tab]: { ...payload, unit_cost: String(payload.unit_cost) } }))
      setSaved((old) => ({ ...old, [tab]: true }))
    } catch (reason) {
      setError((old) => ({ ...old, [tab]: reason instanceof Error ? reason.message : '保存失败，请稍后重试。' }))
    } finally {
      setSavingTab(null)
    }
  }

  /**
   * 用当前表单里（可能尚未保存）的值测试连接。
   * live=false 只做配置与网络连通性检查；live=true 时文本/语音会发一个极小真实请求，
   * 图像能力不做真实生成测试（由后端在 message 里说明）。
   */
  const runProbe = async (live: boolean) => {
    const requestTab = tab
    const requestSignature = probeSignature
    const requestForm = form
    const runLive = live
    probeSignatureRef.current[requestTab] = requestSignature
    setProbeConfirmOpen(false)
    setProbeRunLive((old) => ({ ...old, [requestTab]: runLive }))
    setProbeBusy((old) => ({ ...old, [requestTab]: true }))
    setProbeResults((old) => ({ ...old, [requestTab]: undefined }))
    setProbeErrors((old) => ({ ...old, [requestTab]: undefined }))
    try {
      const result = await api.testCapabilitySetting(requestTab, {
        live,
        provider: requestForm.provider.trim() || undefined,
        endpoint: requestForm.endpoint.trim() || undefined,
        model: requestForm.model.trim() || undefined,
        api_key_env: requestForm.api_key_env.trim() || undefined,
      })
      // 表单在请求期间被改动或切换过标签页时，丢弃这次迟到的结果。
      if (probeSignatureRef.current[requestTab] !== requestSignature) return
      setProbeResults((old) => ({ ...old, [requestTab]: result }))
    } catch (reason) {
      if (probeSignatureRef.current[requestTab] !== requestSignature) return
      setProbeErrors((old) => ({ ...old, [requestTab]: reason instanceof Error ? reason.message : '测试连接失败，请稍后重试。' }))
    } finally {
      setProbeBusy((old) => ({ ...old, [requestTab]: false }))
    }
  }

  const probeReachability = (result: CapabilityProbeResult) => {
    if (result.reachable === null || result.reachable === undefined) return '不适用（本地占位）'
    return result.reachable ? '可连通' : '无法连通'
  }

  const activeSettings = current[tab]
  const isDemoProvider = !form.provider.trim() || form.provider.trim().toLowerCase() === 'demo'
  const knownProvider = providerOptions.some((item) => item.value === form.provider.trim())
  const providerIsDemo = activeSettings ? activeSettings.provider === 'demo' || activeSettings.demo === true : undefined
  const envName = form.api_key_env.trim() || activeSettings?.api_key_env || 'OPENAI_API_KEY'
  // 当前配置是否会调用真实服务：以已保存的后端设置为准；未保存的编辑只作为提示，不冒充已生效。
  const callsRealService = providerIsDemo === undefined ? !isDemoProvider : !providerIsDemo
  const keyMissing = callsRealService && activeSettings?.configured === false
  // 视频能力的当前（已保存）provider 是否为 demo：undefined 表示配置还没读回来，不能当作已知。
  const videoSavedProviderDemo = tab === 'video' ? (activeSettings ? activeSettings.provider === 'demo' || activeSettings.demo === true : undefined) : false
  const videoDraftProvider = form.provider.trim()
  // 视频「真实调用测试」：后端不会做真实生成，也不产生费用，因此不能沿用文本/语音的费用措辞。
  const liveProbeConfirmDescription = tab === 'video'
    ? '视频能力的「真实调用测试」不会做真实生成：后端不会调用图生视频服务、不会产出任何视频，也不会因此产生费用。它只会用当前表单里尚未保存的值检查配置，并尝试连上 endpoint，具体说明见测试结果里的 message。要真正生成视频，请到分镜工作台对镜头执行「生成视频」。'
    : tab === 'image'
      ? '真实调用测试可能产生费用，但图像能力不会做真实生成测试：后端不会调用图像生成接口、不会产出图片；它只会检查配置并尝试连上 endpoint，具体说明见测试结果里的 message。'
      : `这会向后端发起一次极小但真实的${activeMeta.label}请求，可能产生少量费用；不会保存当前表单里的设置，也不会生成正式产物。若只想确认配置与网络是否可用、不产生任何费用，请改用「测试连接」。`

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && !saving && onClose()}>
      <section className="settings-modal" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="provider-title">
        <header>
          <div className="settings-icon"><Server size={21} /></div>
          <div>
            <span className="eyebrow">全局设置</span>
            <h2 id="provider-title">模型服务</h2>
            <p>文本、图像、语音、视频四种能力各自独立配置、独立保存，互不影响。</p>
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="关闭设置"><X size={20} /></button>
        </header>

        <div className="capability-tabs" role="tablist" aria-label="模型能力">
          {capabilityTabs.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              type="button"
              role="tab"
              id={`capability-tab-${key}`}
              aria-selected={tab === key}
              aria-controls="capability-panel"
              tabIndex={tab === key ? 0 : -1}
              className={tab === key ? 'active' : ''}
              onClick={() => setTab(key)}
            >
              <Icon size={16} />{label}
            </button>
          ))}
        </div>

        <div className="settings-body" id="capability-panel" role="tabpanel" aria-labelledby={`capability-tab-${tab}`} tabIndex={-1}>
          <p className="capability-hint"><strong>{activeMeta.label}能力</strong>{activeMeta.hint}</p>

          <div className="probe-actions" role="group" aria-label={`测试「${activeMeta.label}」能力的配置`}>
            <button
              type="button"
              className="button ghost probe-button"
              onClick={() => void runProbe(false)}
              disabled={probing || saving || Boolean(loading[tab])}
            >
              {probing && !probedLive ? <><LoaderCircle className="spin" size={16} /> 测试中</> : <><SlidersHorizontal size={16} /> 测试连接</>}
            </button>
            <button
              type="button"
              className="button subtle probe-button probe-button-live"
              onClick={() => setProbeConfirmOpen(true)}
              disabled={probing || saving || Boolean(loading[tab])}
            >
              {probing && probedLive ? <><LoaderCircle className="spin" size={16} /> 真实调用测试中</> : '真实调用测试'}
            </button>
            <small className="probe-actions-note">使用当前表单里<strong>尚未保存</strong>的值测试；两种测试都不会保存设置。{tab === 'video' && '视频能力不做真实生成测试：即使点「真实调用测试」，后端也不会调用图生视频服务、不会产出视频，也不会产生费用。'}</small>
          </div>

          {/* 视频不适用这条通用提示：它既没有「本机生成」也没有「不产生费用」的占位路径
              （demo 下直接失败），而且下面已经有针对视频的准确警告，重复渲染会自相矛盾。 */}
          {tab !== 'text' && tab !== 'video' && <div className={isDemoProvider ? 'capability-notice' : 'capability-notice real'} role="note">
            <AlertTriangle size={18} />
            <span>
              {isDemoProvider
                ? <>
                  <strong>当前是本地占位素材，不是 AI 生成</strong>
                  <small>provider 为 demo 时，{tab === 'image' ? '「镜头画面生成」' : '「配音生成」'}只会在本机生成{activeMeta.localDemo}用于串联制作流程，不会调用任何生成式模型，也不会产生费用。只有把 provider 从 demo 切换为真实服务（例如 openai）并配置好密钥环境变量后，才会调用真实服务生成内容。</small>
                </>
                : <>
                  <strong>真实服务：生成{activeMeta.label}素材会产生费用</strong>
                  <small>你选择了 demo 之外的 provider（{form.provider.trim()}）。后端当前生效的 provider 是 {activeSettings?.provider ?? '未知（尚未读取到配置）'}；保存成功后，生成的{activeMeta.label}素材来自真实 AI 服务，会被标记为「AI 生成」并产生实际费用。</small>
                </>}
            </span>
          </div>}

          {tab === 'text' && <div className="settings-inline-note"><Sparkles size={15} /><span>provider 为 demo 时，文本任务使用本地确定性占位文本（不是 AI 生成内容）；切换为真实服务后才会调用模型。</span></div>}

          {tab === 'video' && <>
            <div className="capability-notice" role="note">
              <Video size={18} />
              <span>
                <strong>provider 为 demo 时，视频由本机 FFmpeg 渲染成**占位片段**（不是 AI 生成）</strong>
                <small>占位片段用该镜头已有的画面作底、加一条移动标记，产出的是真实可解码的 MP4，会被标记为「demo/占位」而不是 AI 生成素材，也不产生任何费用。它用于把视频链路（时间线、MP4 导出、剪映草稿）跑通，画面内容不是为你这个故事生成的。</small>
                <small>如果后端找不到 FFmpeg（既没装可选依赖 imageio-ffmpeg，PATH 里也没有、也没设 VMAKER_FFMPEG_PATH），生成视频会失败并说明缺什么——那时不会产出任何视频文件。</small>
              </span>
            </div>
            <div className="capability-notice" role="note">
              <AlertCircle size={18} />
              <span>
                <strong>要得到真正的画面，请切换到真实服务商并配好密钥环境变量</strong>
                <small>把 provider 从 demo 改为真实图生视频服务（填写服务商提供的 endpoint、model 与 API Key 环境变量名），在后端进程里设置好该环境变量并重启后端；前端不收集、不保存明文密钥。密钥环境变量缺失时任务同样会失败，界面不会把它显示成可用。</small>
              </span>
            </div>
            <div className="capability-notice" role="note">
              <AlertCircle size={18} />
              <span>
                <strong>视频单价与耗时都明显高于图片，请先把 unit_cost 填对</strong>
                <small>视频按镜头逐个生成，每个镜头可能耗时数十秒到数分钟（生成过程是异步轮询的，任务消息会更新为「第 n/N 个镜头：生成中 xx%」）。单次调用预估成本（unit_cost）用于预算预留：填 0 或填得过低，都会让预算看起来没有占用，而真实费用照常产生。请按服务商实际价格填写每个镜头的预估成本，不要沿用图片的价格。</small>
                {videoSavedProviderDemo === true
                  ? <small>当前后端生效的 provider 是 demo：视频由本机 FFmpeg 渲染成占位片段，不产生费用，也不是 AI 生成。</small>
                  : videoSavedProviderDemo === false
                    ? <small>当前后端生效的 provider 是「{activeSettings?.provider}」：视频生成会真实调用服务并产生费用，提交前请确认成本预估。</small>
                    : null}
                {videoDraftProvider && !isDemoProvider && videoSavedProviderDemo !== false
                  ? <small>表单里你填了「{videoDraftProvider}」，但还没有保存；保存成功后才会真正生效。生效前视频生成仍会按已保存的 provider 执行（demo 时产出的是占位片段）。</small>
                  : null}
              </span>
            </div>
          </>}

          {loading[tab]
            ? <div className="settings-loading"><LoaderCircle className="spin" /><span>正在读取「{activeMeta.label}」能力的配置……</span></div>
            : <>
              <div className="provider-options">
                {providerOptions.map(({ value, title, subtitle, icon: Icon }) => (
                  <button key={value} type="button" aria-pressed={form.provider === value} className={form.provider === value ? 'selected' : ''} onClick={() => chooseProvider(value)}>
                    <Icon size={20} /><span><strong>{title}</strong><small>{subtitle}</small></span>{form.provider === value && <Check size={17} />}
                  </button>
                ))}
                {!knownProvider && <button type="button" aria-pressed className="selected" onClick={() => chooseProvider(form.provider.trim())}>
                  <Server size={20} /><span><strong>{form.provider.trim() || '未命名 provider'}</strong><small>当前保存的自定义 Provider 标识；切换为 demo 或 openai 会覆盖它</small></span><Check size={17} />
                </button>}
              </div>

              <div className="provider-form">
                <label>Provider 标识<input value={form.provider} onChange={(event) => patch('provider', event.target.value)} placeholder="demo 或 openai" /></label>
                <label>Endpoint<input disabled={isDemoProvider} value={form.endpoint} onChange={(event) => patch('endpoint', event.target.value)} placeholder={isDemoProvider ? 'demo 模式不会联网，可留空' : '请填写服务商提供的 OpenAI 兼容端点，例如 https://api.openai.com/v1'} /></label>
                <label>Model<input value={form.model} onChange={(event) => patch('model', event.target.value)} placeholder="请填写你要使用的模型名，系统不预设" /></label>
                <label>API Key 环境变量<input value={form.api_key_env} onChange={(event) => patch('api_key_env', event.target.value.toUpperCase())} placeholder={isDemoProvider ? 'demo 模式无需密钥，可留空' : 'OPENAI_API_KEY'} /></label>
                <label>单次调用预估成本<input type="number" min="0" step="0.001" value={form.unit_cost} onChange={(event) => patch('unit_cost', event.target.value)} placeholder={tab === 'video' ? '例如 0.50（每个镜头的视频单价）' : '例如 0.02'} /><small className="field-note">{tab === 'video' ? '单次调用预估成本（由你填写，系统不猜测价格），用于预算预留。视频按镜头逐个生成，请填写每个镜头的预估成本，不要照抄图片的价格。' : '单次调用预估成本（由你填写，系统不猜测价格），用于预算预留。'}</small></label>
              </div>

              <div className="secret-notice">
                <KeyRound size={20} />
                <div>
                  <strong>API Key 仅通过后端环境变量配置</strong>
                  <p>前端不会收集、发送或保存明文密钥。请在启动后端前设置 <code>{envName}</code>。</p>
                  {!isDemoProvider && !keyMissing && <small className={activeSettings?.configured ? 'configured' : ''}>{activeSettings?.configured ? '环境变量已配置' : '保存后由后端报告密钥是否已配置'}</small>}
                </div>
              </div>

              {keyMissing && <div className="capability-warning" role="alert">
                <AlertCircle size={17} />
                <span><strong>后端尚未读到环境变量 {envName}</strong><small>该能力当前调用会失败。请在后端进程环境中设置 {envName} 并重启后端，然后重新打开本窗口确认状态；在此之前请不要把这里的配置当作已可用。</small></span>
              </div>}
            </>}

          {tabError && (
            <div className="form-error" role="alert">
              <AlertCircle size={16} />
              <span>{tabError}</span>
              <button type="button" className="button ghost form-error-retry" onClick={() => setReloadToken((value) => value + 1)} disabled={Boolean(loading[tab])}>
                <RefreshCw size={14} /> 重试
              </button>
            </div>
          )}
          {saved[tab] && <div className="settings-success"><Check size={16} /> 「{activeMeta.label}」设置已保存，新任务将使用当前 Provider。</div>}

          {probeResult && <div className={probeResult.ok ? 'settings-success probe-result' : 'form-error probe-result'} role={probeResult.ok ? 'status' : 'alert'}>
            {probeResult.ok ? <Check size={16} /> : <AlertCircle size={16} />}
            <span className="probe-result-title">{probeResult.ok ? '测试通过' : '测试未通过'}</span>
            <div className="probe-result-body">
              <p className="probe-result-message"><strong>后端返回：</strong>{probeResult.message}</p>
              <ul className="probe-result-meta">
                <li>Provider：{probeResult.provider || form.provider.trim() || '未提供'}</li>
                <li>配置（configured）：{probeResult.configured ? '已就绪（provider 为 demo，或密钥环境变量已存在）' : '未就绪（后端没有读到密钥环境变量）'}</li>
                <li>网络连通（reachable）：{probeReachability(probeResult)}</li>
                <li>本次是否真实调用（live）：{probeResult.live ? '是，已调用生成服务' : '否，只检查了配置与网络连通性'}</li>
                <li>耗时（latency_ms）：{typeof probeResult.latency_ms === 'number' ? `${probeResult.latency_ms} 毫秒` : '后端未提供'}</li>
              </ul>
              <small className="probe-result-hint">{probeResult.live ? '真实调用可能已产生少量费用；若后端未做真实生成，说明见上方 message。' : '本次不调用生成服务，不产生费用。'}</small>
            </div>
          </div>}

          {probeError && <div className="form-error probe-result" role="alert">
            <AlertCircle size={16} />
            <span className="probe-result-title">测试失败</span>
            <div className="probe-result-body">
              <p className="probe-result-message">{probeError}</p>
              <small className="probe-result-hint">前端没有拿到后端的测试结果，因此这里无法给出配置、连通性等状态；这不代表配置一定有问题。</small>
            </div>
          </div>}
        </div>

        <footer>
          {onDiagnostics && <button type="button" className="button ghost diagnostics-link" onClick={onDiagnostics}><Server size={16} /> 环境诊断</button>}
          <button type="button" className="button ghost" onClick={onClose}>取消</button>
          <button
            type="button"
            className="button primary"
            onClick={() => void save()}
            disabled={Boolean(loading[tab]) || saving}
            aria-label={`保存${activeMeta.label}能力设置`}
          >
            {savingTab === tab ? <><LoaderCircle className="spin" size={17} /> 保存中</> : `保存「${activeMeta.label}」设置`}
          </button>
        </footer>
      </section>

      <ConfirmDialog
        open={probeConfirmOpen}
        title={`对「${activeMeta.label}」能力做真实调用测试？`}
        description={liveProbeConfirmDescription}
        confirmLabel="确认真实调用测试"
        onConfirm={() => void runProbe(true)}
        onCancel={() => setProbeConfirmOpen(false)}
      />
    </div>
  )
}
