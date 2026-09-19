// 步进控制条：对应终端的 c 继续 / s 纠偏 / q 中止。
// reason 由后端下发，说明「为什么停在这里」；count 是本次任务被打断的次数。
export default function GateBar({ action, reason, count, onContinue, onSteer, onAbort }) {
  const label = reason || (action ? `${action.toUpperCase()} 之后` : '需要确认')
  return (
    <div className="gatebar">
      <span className="gatebar-hint">
        暂停：{label}
        {count > 1 ? `（本次第 ${count} 次）` : ''}
      </span>
      <div className="gatebar-actions">
        <input
          id="steer-input"
          className="steer-input"
          placeholder="纠偏内容（可选，填后点纠偏）"
        />
        <button className="btn" onClick={() => onContinue?.()}>继续 (c)</button>
        <button
          className="btn btn-warn"
          onClick={() => {
            const el = document.getElementById('steer-input')
            onSteer?.(el?.value || '')
          }}
        >
          纠偏 (s)
        </button>
        <button className="btn btn-danger" onClick={() => onAbort?.()}>中止 (q)</button>
      </div>
    </div>
  )
}
