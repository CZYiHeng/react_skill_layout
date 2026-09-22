// 步进控制条：对应终端的 c 继续 / s 纠偏 / q 中止。
// reason 由后端下发，说明「为什么停在这里」；count 是本次任务被打断的次数。
// autoCountdown：auto 闸门模式下的剩余秒数（null=不显示倒计时）；倒计时归零自动继续。
export default function GateBar({
  action, reason, count, onContinue, onSteer, onAbort,
  autoCountdown = null, onSteerFocus, onSteerBlur,
}) {
  const label = reason || (action ? `${action.toUpperCase()} 之后` : '需要确认')
  const pct = autoCountdown !== null ? Math.max(0, Math.min(100, (autoCountdown / 30) * 100)) : 0
  return (
    <div className="gatebar">
      <span className="gatebar-hint">
        暂停：{label}
        {count > 1 ? `（本次第 ${count} 次）` : ''}
      </span>
      {autoCountdown !== null ? (
        <div className="gatebar-countdown">
          <div className="gatebar-countdown-bar">
            <div className="gatebar-countdown-fill" style={{ width: `${pct}%` }} />
          </div>
          <span className="gatebar-countdown-text">
            {autoCountdown > 0 ? `${autoCountdown}s 后自动继续` : '正在继续…'}
          </span>
        </div>
      ) : null}
      <div className="gatebar-actions">
        <input
          id="steer-input"
          className="steer-input"
          placeholder="纠偏内容（可选，填后点纠偏）"
          onFocus={() => onSteerFocus?.()}
          onBlur={() => onSteerBlur?.()}
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
