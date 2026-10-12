import { Sparkles } from 'lucide-react'

export function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <div className="brand">
      <div className="brand-mark">
        <Sparkles size={19} strokeWidth={2.2} />
      </div>
      {!compact && (
        <>
          <strong>V-Maker</strong>
          <span>AI 动画工作台</span>
        </>
      )}
    </div>
  )
}
