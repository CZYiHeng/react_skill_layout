// 步进控制条：对应终端的 c 继续 / s 纠偏 / q 中止。
//
// reason 由后端下发，说明「为什么停在这里」；count 是本次任务被打断的次数。
// context 是**判断依据**——缺陷原文、尝试次数、上轮纠偏、OBSERVE 给的建议修法、涉及需求。
// 没有它，用户只能看到"需要你指示"却不知道该指示什么：真实运行里 OBSERVE 判「缺陷」
// 问了两次，缺陷说明其实已经算出来了（`obs_out.parsed`），只是没接到这里。
//
// autoCountdown：auto 闸门模式下的剩余秒数（null=不显示倒计时）；倒计时归零自动继续。
import { useState } from 'react'

export default function GateBar({
  action, reason, count, context, onContinue, onSteer, onAbort,
  autoCountdown = null, onSteerFocus, onSteerBlur,
}) {
  const ctx = context || {}
  const defect = String(ctx.defect || '').trim()
  const suggestion = String(ctx.suggestion || '').trim()
  const blocked = ctx.blocked_requirements || []
  const [expanded, setExpanded] = useState(false)

  const label = reason || (action ? `${action.toUpperCase()} 之后` : '需要确认')
  const pct = autoCountdown !== null ? Math.max(0, Math.min(100, (autoCountdown / 30) * 100)) : 0

  // 缺陷全文默认折叠：前 3 行概览 + 可展开
  const defectLines = defect ? defect.split('\n') : []
  const previewLines = defectLines.slice(0, 3)
  const truncated = defectLines.length > 3

  const attempt = Number(ctx.attempt || 0)
  const attemptLimit = Number(ctx.attempt_limit || 0)

  return (
    <div className="gatebar">
      <div className="gatebar-head">
        <span className="gatebar-hint">
          暂停：{label}
          {count > 1 ? `（本次第 ${count} 次）` : ''}
        </span>
        {ctx.step ? (
          <span className="gatebar-meta">
            步骤 {ctx.step}{ctx.total_steps ? `/${ctx.total_steps}` : ''}
            {attempt && attemptLimit ? ` · 第 ${attempt}/${attemptLimit} 次修正` : ''}
          </span>
        ) : null}
      </div>

      {ctx.step_goal ? (
        <div className="gatebar-goal">本步目标：{ctx.step_goal}</div>
      ) : null}

      {defect ? (
        <div className="gatebar-defect">
          <div className="gatebar-defect-title">缺陷说明</div>
          <pre className="gatebar-defect-body">
            {(expanded ? defectLines : previewLines).join('\n')}
          </pre>
          {truncated && !expanded ? (
            <button className="linkbtn" onClick={() => setExpanded(true)}>
              展开全部 {defectLines.length} 行
            </button>
          ) : null}
          {expanded && truncated ? (
            <button className="linkbtn" onClick={() => setExpanded(false)}>收起</button>
          ) : null}
        </div>
      ) : null}

      {blocked.length ? (
        <div className="gatebar-blocked">涉及需求：{blocked.join('、')}</div>
      ) : null}

      {ctx.last_steer ? (
        <div className="gatebar-laststeer">上轮你的纠偏：{ctx.last_steer}</div>
      ) : null}

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

      {/* 三路径：每条都写清后果，避免"看标签猜行为" */}
      <div className="gatebar-paths">
        <button className="btn" onClick={() => onContinue?.()}>
          <strong>继续</strong>
          <span className="path-note">
            让模型按上面的缺陷自己修（{attempt && attemptLimit
              ? `再试 ${Math.max(0, attemptLimit - attempt)} 次就强制重规划`
              : '不额外说明'}）
          </span>
        </button>
        <button
          className="btn btn-warn"
          disabled={!suggestion}
          title={suggestion ? '' : '本次没有识别到建议修法'}
          onClick={() => onSteer?.(suggestion)}
        >
          <strong>采纳建议</strong>
          <span className="path-note">
            {suggestion ? `把「${suggestion.slice(0, 40)}${suggestion.length > 40 ? '…' : ''}」交给模型` : '本次无建议修法'}
          </span>
        </button>
        <button
          className="btn btn-warn"
          onClick={() => {
            const el = document.getElementById('steer-input')
            const v = (el?.value || '').trim()
            if (v) onSteer?.(v)
            else el?.focus()
          }}
        >
          <strong>我来纠偏</strong>
          <span className="path-note">按下面输入框的内容改写方向</span>
        </button>
        <button className="btn btn-danger" onClick={() => onAbort?.()}>
          <strong>中止</strong>
          <span className="path-note">停下本次任务，保留已有轨迹</span>
        </button>
      </div>

      <div className="gatebar-actions">
        <input
          id="steer-input"
          className="steer-input"
          placeholder="纠偏内容（点「我来纠偏」才生效）"
          onFocus={() => onSteerFocus?.()}
          onBlur={() => onSteerBlur?.()}
        />
      </div>
    </div>
  )
}
