import { useEffect, useState, useCallback } from 'react'
import { getTokenStats } from '../api'

/**
 * 会话级 token 统计：全部会话汇总表 + 选中会话的逐次调用明细。
 * 数据来自 GET /api/token_stats（磁盘持久化，服务重启后仍可查）。
 */
const fmt = (n) => (n ?? 0).toLocaleString()
const pct = (r) => (typeof r === 'number' ? (r * 100).toFixed(1) + '%' : '-')

function fmtTime(ts) {
  if (!ts) return '-'
  const s = String(ts)
  return s.length >= 16 ? s.slice(5, 16) : s
}

export default function TokenStats({ sessionId }) {
  const [sessions, setSessions] = useState([])
  const [detail, setDetail] = useState(null)   // 当前选中会话的明细
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const loadList = useCallback(async () => {
    setLoading(true)
    try {
      const data = await getTokenStats()
      setSessions(data.sessions || [])
    } catch (e) {
      setError(e.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  const loadDetail = useCallback(async (sid) => {
    setDetailLoading(true)
    try {
      const data = await getTokenStats(sid)
      setDetail(data)
    } catch (e) {
      setError(e.message || '加载失败')
    } finally {
      setDetailLoading(false)
    }
  }, [])

  useEffect(() => {
    loadList()
    if (sessionId) loadDetail(sessionId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 汇总卡：所有会话合计
  const total = sessions.reduce(
    (acc, s) => {
      acc.sessions += 1
      acc.calls += s.summary?.calls || 0
      acc.tokens += s.summary?.total_tokens || 0
      acc.cached += s.summary?.cached_tokens || 0
      acc.prompt += s.summary?.prompt_tokens || 0
      return acc
    },
    { sessions: 0, calls: 0, tokens: 0, cached: 0, prompt: 0 },
  )
  const hitRate = total.prompt ? (total.cached / total.prompt) * 100 : 0

  const cards = [
    { label: '会话数', value: fmt(total.sessions) },
    { label: '模型调用', value: fmt(total.calls) },
    { label: '总 Token', value: fmt(total.tokens) },
    { label: '缓存命中 Token', value: fmt(total.cached) },
    { label: '缓存命中率', value: hitRate.toFixed(1) + '%' },
  ]

  const rows = detail?.calls || []
  const dSum = detail?.summary

  return (
    <div style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: '16px 18px', boxSizing: 'border-box' }}>
      {/* 汇总卡 */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, marginBottom: 14 }}>
        {cards.map((c) => (
          <div key={c.label} style={{
            flex: '1 1 120px', background: 'var(--surface-2)', border: '1px solid var(--border)',
            borderRadius: 10, padding: '10px 14px',
          }}>
            <div style={{ fontSize: 12, color: 'var(--muted)' }}>{c.label}</div>
            <div style={{ fontSize: 20, fontWeight: 700, marginTop: 2, color: 'var(--text)' }}>{c.value}</div>
          </div>
        ))}
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
        <h3 style={{ margin: 0, fontSize: 15, color: 'var(--text)' }}>会话 Token 统计</h3>
        <button className="btn" onClick={() => { loadList(); if (sessionId) loadDetail(sessionId) }}>刷新</button>
        {loading && <span style={{ fontSize: 12, color: 'var(--muted)' }}>加载中…</span>}
      </div>

      {error && <div className="banner banner-error" style={{ marginBottom: 10 }}>✘ {error}</div>}

      {/* 会话汇总表 */}
      {sessions.length === 0 && !loading ? (
        <div style={{ padding: 24, textAlign: 'center', color: 'var(--muted)', border: '1px dashed var(--border)', borderRadius: 10 }}>
          暂无已持久化的会话统计。跑一轮任务后自动生成。
        </div>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13, tableLayout: 'fixed' }}>
          <colgroup>
            <col style={{ width: '16%' }} />
            <col style={{ width: '26%' }} />
            <col style={{ width: '8%' }} />
            <col style={{ width: '8%' }} />
            <col style={{ width: '8%' }} />
            <col style={{ width: '12%' }} />
            <col style={{ width: '10%' }} />
            <col style={{ width: '12%' }} />
          </colgroup>
          <thead>
            <tr style={{ color: 'var(--muted)', textAlign: 'left' }}>
              <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--border)' }}>会话 ID</th>
              <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--border)' }}>任务</th>
              <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--border)' }}>状态</th>
              <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--border)' }}>轮次</th>
              <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--border)' }}>调用</th>
              <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--border)' }}>总 Token</th>
              <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--border)' }}>缓存命中</th>
              <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--border)' }}>命中率</th>
            </tr>
          </thead>
          <tbody>
            {sessions.map((s) => {
              const sel = detail?.session_id === s.session_id
              return (
                <tr key={s.session_id} onClick={() => loadDetail(s.session_id)}
                    style={{ cursor: 'pointer', background: sel ? 'var(--surface-2)' : 'transparent' }}>
                  <td style={{ padding: '6px 8px', borderBottom: '1px solid var(--border)', fontFamily: 'monospace', color: 'var(--primary)' }}>{s.session_id}</td>
                  <td style={{ padding: '6px 8px', borderBottom: '1px solid var(--border)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.task || '-'}</td>
                  <td style={{ padding: '6px 8px', borderBottom: '1px solid var(--border)' }}>{s.status || '-'}</td>
                  <td style={{ padding: '6px 8px', borderBottom: '1px solid var(--border)' }}>{s.rounds ?? '-'}</td>
                  <td style={{ padding: '6px 8px', borderBottom: '1px solid var(--border)' }}>{s.summary?.calls ?? 0}</td>
                  <td style={{ padding: '6px 8px', borderBottom: '1px solid var(--border)' }}>{fmt(s.summary?.total_tokens)}</td>
                  <td style={{ padding: '6px 8px', borderBottom: '1px solid var(--border)' }}>{fmt(s.summary?.cached_tokens)}</td>
                  <td style={{ padding: '6px 8px', borderBottom: '1px solid var(--border)' }}>{pct(s.summary?.cache_hit_rate)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}

      {/* 选中会话明细 */}
      {detail && (
        <div style={{ marginTop: 18 }}>
          <h3 style={{ margin: '0 0 8px', fontSize: 14, color: 'var(--text)' }}>
            明细 · {detail.session_id} · 调用 {dSum?.calls ?? 0} 次 · 总 {fmt(dSum?.total_tokens)} · 缓存命中率 {pct(dSum?.cache_hit_rate)}
          </h3>
          {detailLoading ? (
            <div style={{ fontSize: 12, color: 'var(--muted)' }}>加载中…</div>
          ) : rows.length === 0 ? (
            <div style={{ padding: 16, color: 'var(--muted)', border: '1px dashed var(--border)', borderRadius: 10 }}>
              该会话暂无调用记录。
            </div>
          ) : (
            <div style={{ maxHeight: 320, overflow: 'auto', border: '1px solid var(--border)', borderRadius: 10 }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5, tableLayout: 'fixed' }}>
                <colgroup>
                  <col style={{ width: '10%' }} />
                  <col style={{ width: '16%' }} />
                  <col style={{ width: '14%' }} />
                  <col style={{ width: '14%' }} />
                  <col style={{ width: '14%' }} />
                  <col style={{ width: '14%' }} />
                  <col style={{ width: '10%' }} />
                  <col style={{ width: '8%' }} />
                </colgroup>
                <thead>
                  <tr style={{ color: 'var(--muted)', textAlign: 'left', background: 'var(--surface-2)' }}>
                    <th style={{ padding: '5px 8px' }}>阶段</th>
                    <th style={{ padding: '5px 8px' }}>时间</th>
                    <th style={{ padding: '5px 8px' }}>输入</th>
                    <th style={{ padding: '5px 8px' }}>输出</th>
                    <th style={{ padding: '5px 8px' }}>合计</th>
                    <th style={{ padding: '5px 8px' }}>缓存命中</th>
                    <th style={{ padding: '5px 8px' }}>命中率</th>
                    <th style={{ padding: '5px 8px' }}>耗时 s</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => {
                    const u = r.usage || {}
                    const pr = u.prompt || 0
                    const hit = pr ? ((u.cached || 0) / pr) * 100 : 0
                    return (
                      <tr key={i}>
                        <td style={{ padding: '5px 8px', borderBottom: '1px solid var(--border)' }}>
                          <span className={`slot ${r.phase}`} style={{ fontSize: 11, padding: '2px 6px' }}>{r.phase}</span>
                        </td>
                        <td style={{ padding: '5px 8px', borderBottom: '1px solid var(--border)', color: 'var(--muted)' }}>{fmtTime(r.ts)}</td>
                        <td style={{ padding: '5px 8px', borderBottom: '1px solid var(--border)' }}>{fmt(pr)}</td>
                        <td style={{ padding: '5px 8px', borderBottom: '1px solid var(--border)' }}>{fmt(u.completion)}</td>
                        <td style={{ padding: '5px 8px', borderBottom: '1px solid var(--border)' }}>{fmt(u.total)}</td>
                        <td style={{ padding: '5px 8px', borderBottom: '1px solid var(--border)' }}>{fmt(u.cached)}</td>
                        <td style={{ padding: '5px 8px', borderBottom: '1px solid var(--border)' }}>{hit.toFixed(1) + '%'}</td>
                        <td style={{ padding: '5px 8px', borderBottom: '1px solid var(--border)', color: 'var(--muted)' }}>{r.elapsed_sec ?? '-'}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
