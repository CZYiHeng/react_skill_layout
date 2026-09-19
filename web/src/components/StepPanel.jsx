// 单步面板：五槽位分色，沿用 DESIGN.md §3.6 的配色与图标规范。
// think 蓝 ◆ / plan 黄 ◇ / act 绿 ▶ / observe 紫 ● / verify 青 ✔

const META = {
  think: { icon: '◆', label: 'THINK', color: '#3b82f6' },
  plan: { icon: '◇', label: 'PLAN', color: '#eab308' },
  act: { icon: '▶', label: 'ACT', color: '#22c55e' },
  observe: { icon: '●', label: 'OBSERVE', color: '#a855f7' },
  verify: { icon: '✔', label: 'VERIFY', color: '#06b6d4' },
}

export default function StepPanel({ action, text, elapsed, tokens, reasoning, streaming }) {
  const meta = META[action] || { icon: '·', label: (action || '').toUpperCase(), color: '#64748b' }
  const body = text || ''
  return (
    <div className="panel" style={{ borderLeftColor: meta.color }}>
      <div className="panel-head">
        <span className="panel-icon" style={{ color: meta.color }}>
          {meta.icon} {meta.label}
        </span>
        <span className="panel-meta">
          {typeof elapsed === 'number' && `${elapsed.toFixed(1)}s`}
          {typeof tokens === 'number' && ` · ${tokens}tok`}
          {streaming && <span className="live-dot"> 流式</span>}
        </span>
      </div>
      {reasoning ? <pre className="reasoning">{reasoning}</pre> : null}
      <div className="panel-body">
        {body}
        {streaming ? <span className="caret" /> : null}
      </div>
    </div>
  )
}
