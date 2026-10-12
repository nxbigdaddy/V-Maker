import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, CircleDollarSign, CreditCard, LoaderCircle, RefreshCw, ShieldCheck, TrendingUp } from 'lucide-react'
import { api } from '../api/client'
import type { Project, ProjectCosts } from '../types'

function normalizeCurrency(value: unknown): string {
  return typeof value === 'string' && /^[A-Za-z]{3}$/.test(value.trim()) ? value.trim().toUpperCase() : 'CNY'
}

function formatCurrency(value: unknown, code: string): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  try {
    return new Intl.NumberFormat('zh-CN', { style: 'currency', currency: code }).format(value)
  } catch {
    return new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(value)
  }
}

function formatDate(value: unknown): string {
  if (typeof value !== 'string' || !value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}

export function CostsPage({ project }: { project: Project }) {
  const [data, setData] = useState<ProjectCosts | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await api.getProjectCosts(project.id))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '成本数据读取失败。')
    } finally {
      setLoading(false)
    }
  }, [project.id])

  useEffect(() => { void load() }, [load])

  const budget = data?.budget ?? project.budget
  const code = normalizeCurrency(budget?.currency ?? data?.currency)
  const spent = budget?.spent ?? data?.spent
  const reserved = budget?.reserved ?? data?.reserved
  const limit = budget?.hard_limit ?? data?.hard_limit
  const committed = typeof spent === 'number' && Number.isFinite(spent) && typeof reserved === 'number' && Number.isFinite(reserved)
    ? spent + reserved
    : typeof spent === 'number' && Number.isFinite(spent)
      ? spent
      : typeof reserved === 'number' && Number.isFinite(reserved)
        ? reserved
        : undefined

  const ratio = useMemo(() => {
    if (typeof limit !== 'number' || !Number.isFinite(limit) || limit <= 0 || typeof committed !== 'number') return 0
    return Math.min(100, Math.max(0, committed / limit * 100))
  }, [limit, committed])

  const available = typeof limit === 'number' && Number.isFinite(limit) && typeof committed === 'number'
    ? Math.max(0, limit - committed)
    : undefined

  return (
    <main className="studio-page costs-page">
      <section className="studio-hero">
        <div>
          <span className="eyebrow">BUDGET</span>
          <h1>成本与预算</h1>
          <p>基于后端账本展示已花费、已预留和预算余量。</p>
        </div>
        <button type="button" className="button ghost" disabled={loading} onClick={() => void load()}>
          <RefreshCw className={loading ? 'spin' : ''} size={16} /> 刷新
        </button>
      </section>

      {error && (
        <div className="studio-alert">
          <AlertTriangle />
          <span style={{ minWidth: 0 }}><strong>无法读取成本数据</strong><small style={{ overflowWrap: 'anywhere' }}>{error}</small></span>
        </div>
      )}

      {loading ? (
        <div className="studio-loading" role="status"><LoaderCircle className="spin" /> 正在读取预算…</div>
      ) : error ? (
        <section className="studio-empty">
          <AlertTriangle />
          <h2>成本数据暂不可用</h2>
          <p>为避免把读取失败误显示为零成本，数据恢复前不会展示预算金额。</p>
          <button type="button" className="button ghost" onClick={() => void load()}>
            <RefreshCw size={16} /> 重新读取
          </button>
        </section>
      ) : (
        <>
          <section className="budget-overview">
            <header>
              <div>
                <span className="budget-icon"><CircleDollarSign /></span>
                <div>
                  <small>预算使用</small>
                  <strong>{formatCurrency(committed, code)} <em>/ {formatCurrency(limit, code)}</em></strong>
                </div>
              </div>
              <b>{typeof limit === 'number' && Number.isFinite(limit) && limit > 0 ? `${Math.round(ratio)}%` : '未设置上限'}</b>
            </header>
            <div className="budget-track">
              <i className={ratio >= 90 ? 'danger' : ratio >= 70 ? 'warning' : ''} style={{ width: `${ratio}%` }} />
            </div>
            <footer>
              <span><i className="spent" /> 已花费 {formatCurrency(spent, code)}</span>
              <span><i className="reserved" /> 已预留 {formatCurrency(reserved, code)}</span>
              <span>可用 {formatCurrency(available, code)}</span>
            </footer>
          </section>

          <div className="cost-summary">
            <article><CreditCard /><span><small>已花费</small><strong>{formatCurrency(spent, code)}</strong></span></article>
            <article><ShieldCheck /><span><small>已预留</small><strong>{formatCurrency(reserved, code)}</strong></span></article>
            <article><TrendingUp /><span><small>账单项目</small><strong>{data?.items?.length ?? 0}</strong></span></article>
          </div>

          <section className="cost-ledger">
            <header>
              <div><h2>成本明细</h2><p>仅展示后端返回的计费记录</p></div>
            </header>
            {data?.items?.length ? (
              <div className="cost-table">
                <div className="cost-table-head"><span>项目</span><span>时间</span><span>金额</span></div>
                {data.items.map((item, index) => {
                  const label = item.label || item.type || item.message || '未命名费用'
                  return (
                    <div className="cost-table-row" key={item.id || `${label}-${index}`}>
                      <span style={{ minWidth: 0, overflowWrap: 'anywhere' }}><strong>{label}</strong>{item.message && item.message !== label && <small style={{ overflowWrap: 'anywhere' }}>{item.message}</small>}</span>
                      <span style={{ overflowWrap: 'anywhere' }}>{formatDate(item.created_at)}</span>
                      <strong>{formatCurrency(item.amount, normalizeCurrency(item.currency ?? code))}</strong>
                    </div>
                  )
                })}
              </div>
            ) : (
              <div className="section-empty">暂无成本明细。这里只会显示后端返回的真实计费记录。</div>
            )}
          </section>
        </>
      )}
    </main>
  )
}
