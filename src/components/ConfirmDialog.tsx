import { useEffect, useRef } from 'react'
import { LoaderCircle } from 'lucide-react'
import { useDialog } from '../lib/dialog'

export type ConfirmDialogProps = {
  open: boolean
  title: string
  description: string
  confirmLabel?: string
  cancelLabel?: string
  tone?: 'default' | 'danger'
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
}

/**
 * 受控确认框：用于高影响、不可撤销的操作。
 * 打开时聚焦取消按钮，Esc（非 busy 时）等同于取消。
 */
export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  cancelLabel,
  tone = 'default',
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const cancelRef = useRef<HTMLButtonElement | null>(null)
  const danger = tone === 'danger'
  // busy 时不响应 Esc：正在进行的高影响操作不应被键盘中断。
  // active: open —— 关闭时完全不锁定滚动、不接管焦点。
  const containerRef = useDialog<HTMLDivElement>(onCancel, { blocked: busy, active: open })

  useEffect(() => {
    if (open) cancelRef.current?.focus()
  }, [open])

  if (!open) return null

  return <div
    className="modal-backdrop confirm-backdrop"
    ref={containerRef}
    role="dialog"
    aria-modal="true"
    aria-labelledby="confirm-dialog-title"
    aria-describedby="confirm-dialog-description"
    onMouseDown={(event) => event.target === event.currentTarget && !busy && onCancel()}
  >
    <div className="confirm-dialog">
      <h2 id="confirm-dialog-title" className="confirm-title">{title}</h2>
      <p id="confirm-dialog-description" className="confirm-description">{description}</p>
      <footer className="confirm-actions">
        <button type="button" className="button ghost" ref={cancelRef} onClick={onCancel} disabled={busy}>{cancelLabel ?? '取消'}</button>
        <button
          type="button"
          className={danger ? 'button danger' : 'button primary'}
          onClick={onConfirm}
          disabled={busy}
        >
          {busy && <LoaderCircle className="spin" size={16} />}
          {confirmLabel ?? (danger ? '确认删除' : '确认')}
        </button>
      </footer>
    </div>
  </div>
}
