import { useMemo, useState } from 'react'
import { ArrowLeft, ArrowRight, Check, Film, LoaderCircle, Sparkles, X } from 'lucide-react'
import { useDialog } from '../lib/dialog'
import type { CreateProjectInput, ProjectMode } from '../types'

const initialForm: CreateProjectInput = {
  name: '',
  story: '',
  creative_intent: '',
  episode_strategy: 'single',
  target_duration_sec: 60,
  aspect_ratio: '16:9',
  visual_style: '电影感国风',
  audience: '全年龄',
  narration: 'balanced',
  mode: 'standard',
  budget_limit: 100,
  outputs: ['mp4'],
  language: 'zh-CN',
}

const steps = ['故事内容', '成片目标', '生产策略', '确认计划']
const stepDescriptions = [
  '粘贴故事与创作意图',
  '定义画幅、时长与风格',
  '平衡质量、速度与成本',
  '预览制作计划',
]

const modes: [ProjectMode, string, string][] = [
  ['draft', '草稿模式', '低成本快速验证，优先自动完成'],
  ['standard', '标准模式', '在大纲、角色、动态分镜和成片处确认'],
  ['professional', '专业模式', '逐项控制模型、参数与资产版本'],
]

export function CreateProjectModal({ onClose, onCreate, creating, error }: {
  onClose: () => void
  onCreate: (value: CreateProjectInput) => Promise<void>
  creating: boolean
  error: string | null
}) {
  const [step, setStep] = useState(0)
  const [form, setForm] = useState(initialForm)
  const [validation, setValidation] = useState('')
  // Esc 关闭 / 焦点圈定 / 关闭后恢复触发按钮焦点；创建中禁止关闭。
  const dialogRef = useDialog<HTMLElement>(onClose, { blocked: creating })
  const estimate = useMemo(() => ({
    scenes: Math.max(3, Math.round(form.target_duration_sec / 12)),
    shots: Math.max(6, Math.round(form.target_duration_sec / 5)),
    minutes: Math.max(8, Math.round(form.target_duration_sec * 0.45)),
  }), [form.target_duration_sec])

  const patch = <K extends keyof CreateProjectInput>(key: K, value: CreateProjectInput[K]) => {
    setForm((old) => ({ ...old, [key]: value }))
  }

  const next = () => {
    if (step === 0 && !form.name.trim()) {
      setValidation('请先填写项目名称。')
      return
    }
    if (step === 0 && !form.story.trim()) {
      setValidation('请先输入故事内容；一两句话也可以，后续可继续完善。')
      return
    }
    if (step === 2 && form.outputs.length === 0) {
      setValidation('请至少选择一种交付内容。')
      return
    }
    setValidation('')
    setStep((current) => Math.min(3, current + 1))
  }

  const previous = () => {
    setValidation('')
    if (step === 0) onClose()
    else setStep((current) => Math.max(0, current - 1))
  }

  /**
   * 键盘也能前进：输入框里按 Enter 继续，多行文本里用 Ctrl+Enter（避免误吞换行）。
   *
   * 这不只是便利——底部按钮若因任何窗口尺寸/缩放问题被挤出可视区域，键盘仍能走完流程，
   * 用户不至于卡在第一步。
   */
  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (event.key !== 'Enter' || creating) return
    const target = event.target as HTMLElement
    const tag = target.tagName
    if (tag === 'TEXTAREA') {
      if (!(event.ctrlKey || event.metaKey)) return
    } else if (tag !== 'INPUT') {
      return
    }
    event.preventDefault()
    if (step < 3) next()
    else void onCreate(form)
  }

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onMouseDown={(event) => event.target === event.currentTarget && !creating && onClose()}
    >
      <section className="create-modal" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="create-title">
        <aside className="wizard-sidebar">
          <div className="wizard-symbol"><Sparkles size={22} /></div>
          <div>
            <span className="eyebrow">新建作品</span>
            <h2 id="create-title">让故事开始流动</h2>
            <p>四步创建项目，所有设置稍后都可以调整。</p>
          </div>
          <ol className="step-list">
            {steps.map((label, index) => (
              <li className={index === step ? 'active' : index < step ? 'done' : ''} key={label}>
                <span>{index < step ? <Check size={14} /> : index + 1}</span>
                <div>
                  <strong>{label}</strong>
                  <small>{stepDescriptions[index]}</small>
                </div>
              </li>
            ))}
          </ol>
          <div className="wizard-note">
            <Film size={18} />
            <span>V-Maker 会在高成本操作前向你确认，不会自动批准生成内容。</span>
          </div>
        </aside>

        <div className="wizard-content">
          <button type="button" className="icon-button modal-close" onClick={onClose} disabled={creating} aria-label="关闭">
            <X size={20} />
          </button>
          <div className="wizard-page" onKeyDown={handleKeyDown}>
            {step === 0 && (
              <>
                <span className="eyebrow">01 / 故事</span>
                <h3>你想讲一个怎样的故事？</h3>
                <p className="page-intro">可以是一段灵感、完整剧本或分集大纲。</p>
                <label>
                  项目名称
                  <input value={form.name} onChange={(event) => patch('name', event.target.value)} placeholder="例如：云端邮差" />
                </label>
                <label>
                  故事内容 <em>{form.story.length} 字</em>
                  <textarea
                    value={form.story}
                    onChange={(event) => patch('story', event.target.value)}
                    placeholder="粘贴你的故事。描述人物、冲突和你期待的结局……"
                    rows={8}
                  />
                </label>
                <div className="two-column">
                  <label>
                    组织方式
                    <select value={form.episode_strategy} onChange={(event) => patch('episode_strategy', event.target.value as CreateProjectInput['episode_strategy'])}>
                      <option value="single">单集短片</option>
                      <option value="ai_split">由 AI 建议分集</option>
                    </select>
                  </label>
                  <label>
                    创作意图（可选）
                    <input value={form.creative_intent} onChange={(event) => patch('creative_intent', event.target.value)} placeholder="温暖、克制、有余韵" />
                  </label>
                </div>
              </>
            )}

            {step === 1 && (
              <>
                <span className="eyebrow">02 / 成片目标</span>
                <h3>定义作品的观看体验</h3>
                <p className="page-intro">设置会影响分镜节奏、构图和制作计划。</p>
                <label>
                  画幅比例
                  <div className="choice-row">
                    {(['16:9', '9:16', '1:1'] as const).map((ratio) => (
                      <button
                        type="button"
                        className={`choice-card ${form.aspect_ratio === ratio ? 'selected' : ''}`}
                        onClick={() => patch('aspect_ratio', ratio)}
                        aria-pressed={form.aspect_ratio === ratio}
                        key={ratio}
                      >
                        <span className={`ratio-shape ratio-${ratio.replace(':', '')}`} />
                        <strong>{ratio}</strong>
                        <small>{ratio === '16:9' ? '横屏影片' : ratio === '9:16' ? '竖屏短片' : '方形内容'}</small>
                      </button>
                    ))}
                  </div>
                </label>
                <div className="two-column">
                  <label>
                    目标时长
                    <select value={form.target_duration_sec} onChange={(event) => patch('target_duration_sec', Number(event.target.value))}>
                      <option value={30}>约 30 秒</option>
                      <option value={60}>约 1 分钟</option>
                      <option value={90}>约 90 秒</option>
                      <option value={180}>约 3 分钟</option>
                      <option value={300}>约 5 分钟</option>
                    </select>
                  </label>
                  <label>
                    目标观众
                    <select value={form.audience} onChange={(event) => patch('audience', event.target.value)}>
                      <option>全年龄</option>
                      <option>儿童与家庭</option>
                      <option>青少年</option>
                      <option>成年观众</option>
                    </select>
                  </label>
                </div>
                <div className="two-column">
                  <label>
                    视觉风格
                    <select value={form.visual_style} onChange={(event) => patch('visual_style', event.target.value)}>
                      <option>电影感国风</option>
                      <option>清新二维动画</option>
                      <option>绘本水彩</option>
                      <option>赛博科幻</option>
                      <option>黏土定格</option>
                    </select>
                  </label>
                  <label>
                    声音倾向
                    <select value={form.narration} onChange={(event) => patch('narration', event.target.value as CreateProjectInput['narration'])}>
                      <option value="balanced">对白与旁白平衡</option>
                      <option value="dialogue">对白驱动</option>
                      <option value="narration">旁白驱动</option>
                    </select>
                  </label>
                </div>
              </>
            )}

            {step === 2 && (
              <>
                <span className="eyebrow">03 / 生产策略</span>
                <h3>选择与你匹配的创作方式</h3>
                <p className="page-intro">标准模式适合大多数项目，关键节点由你确认。</p>
                <div className="mode-grid">
                  {modes.map(([value, title, description]) => (
                    <button
                      type="button"
                      key={value}
                      className={`mode-card ${form.mode === value ? 'selected' : ''}`}
                      onClick={() => patch('mode', value)}
                      aria-pressed={form.mode === value}
                    >
                      <span>{value === 'standard' ? '推荐' : ' '}</span>
                      <strong>{title}</strong>
                      <small>{description}</small>
                    </button>
                  ))}
                </div>
                <label className="range-label">
                  <span>项目预算硬上限 <strong>¥ {form.budget_limit}</strong></span>
                  <input type="range" min="20" max="500" step="10" value={form.budget_limit} onChange={(event) => patch('budget_limit', Number(event.target.value))} />
                  <small>达到上限后制作将暂停，不会继续产生费用。</small>
                </label>
                <label>
                  交付内容
                  <div className="check-row">
                    {([['mp4', '合成 MP4'], ['jianying', '剪映草稿']] as const).map(([value, label]) => {
                      const selected = form.outputs.includes(value)
                      return (
                        <button
                          type="button"
                          key={value}
                          className={selected ? 'check-card selected' : 'check-card'}
                          onClick={() => patch('outputs', selected ? form.outputs.filter((item) => item !== value) : [...form.outputs, value])}
                          aria-pressed={selected}
                        >
                          <span>{selected && <Check size={14} />}</span>
                          {label}
                        </button>
                      )
                    })}
                  </div>
                </label>
              </>
            )}

            {step === 3 && (
              <>
                <span className="eyebrow">04 / 确认计划</span>
                <h3>准备好开拍了</h3>
                <p className="page-intro">创建后先进入项目工作台，生成任务不会立即扣费。</p>
                <div className="plan-card">
                  <div className="plan-title">
                    <div className="plan-mark">{form.name.slice(0, 1) || 'V'}</div>
                    <div>
                      <strong>{form.name}</strong>
                      <small>{form.visual_style} · {form.aspect_ratio} · 约 {form.target_duration_sec} 秒</small>
                    </div>
                  </div>
                  <div className="estimate-grid">
                    <div><strong>1</strong><small>预计集数</small></div>
                    <div><strong>{estimate.scenes}</strong><small>预计场景</small></div>
                    <div><strong>{estimate.shots}</strong><small>预计镜头</small></div>
                    <div><strong>{estimate.minutes}–{estimate.minutes + 12}</strong><small>分钟分析</small></div>
                  </div>
                </div>
                <div className="summary-list">
                  <div><span>制作模式</span><strong>{{ draft: '草稿', standard: '标准', professional: '专业' }[form.mode]}模式</strong></div>
                  <div><span>预算上限</span><strong>¥ {form.budget_limit}</strong></div>
                  <div><span>审核节点</span><strong>{form.mode === 'draft' ? '最终成片' : form.mode === 'standard' ? '4 个关键节点' : '逐场景 / 镜头'}</strong></div>
                  <div><span>输出</span><strong>{form.outputs.map((item) => item === 'mp4' ? 'MP4' : '剪映草稿').join('、') || '暂不选择'}</strong></div>
                </div>
              </>
            )}
            {(validation || error) && <div className="form-error">{validation || error}</div>}
          </div>
          <footer className="wizard-footer">
            <span className="wizard-progress">第 {step + 1} / 4 步</span>
            <div className="wizard-actions">
            <button type="button" className="button ghost" onClick={previous} disabled={creating}>
              <ArrowLeft size={17} /> {step === 0 ? '取消' : '上一步'}
            </button>
            {step < 3 ? (
              <button type="button" className="button primary wizard-next" onClick={next} disabled={creating}>
                下一步 <ArrowRight size={17} />
              </button>
            ) : (
              <button type="button" className="button primary wizard-next" onClick={() => void onCreate(form)} disabled={creating}>
                {creating ? <><LoaderCircle className="spin" size={17} /> 正在创建</> : <><Sparkles size={17} /> 创建项目</>}
              </button>
            )}
            </div>
            <p className="wizard-hint">
              {step < 3 ? '输入框里按 Enter 也可以继续（多行文本框用 Ctrl+Enter）。' : '确认无误后按 Enter 或点「创建项目」。'}
            </p>
          </footer>
        </div>
      </section>
    </div>
  )
}
