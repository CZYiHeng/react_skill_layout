// 单步面板：五槽位分色，圆点由 CSS ::before 生成。
// think 蓝 / plan 琥珀 / act 绿 / observe 紫 / verify 青

const META = {
  think: { label: 'THINK', color: '#3b82f6' },
  plan: { label: 'PLAN', color: '#d97706' },
  act: { label: 'ACT', color: '#16a34a' },
  observe: { label: 'OBSERVE', color: '#a855f7' },
  verify: { label: 'VERIFY', color: '#0891b2' },
}

export default function StepPanel({ action, text, elapsed, tokens, usage, reasoning, streaming }) {
  const meta = META[action] || { label: (action || '').toUpperCase(), color: '#64748b' }
  const body = text || ''
  const tokStr = usage
    ? `${usage.total} tok（入${usage.prompt}/出${usage.completion}）`
    : (typeof tokens === 'number' ? `${tokens} tok` : '')
  return (
    <div className="panel" style={{ borderLeftColor: meta.color }}>
      <div className="panel-head">
        <span className="panel-icon" style={{ color: meta.color }}>
          {meta.label}
        </span>
        <span className="panel-meta">
          {typeof elapsed === 'number' && `${elapsed.toFixed(1)}s`}
          {tokStr && ` · ${tokStr}`}
          {streaming && <span className="live-dot"> · 流式</span>}
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
