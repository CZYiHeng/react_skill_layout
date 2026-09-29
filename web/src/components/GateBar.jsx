// 步进控制条：对应终端的 c 继续 / s 纠偏 / q 中止。
//
// reason 由后端下发，说明「为什么停在这里」；count 是本次任务被打断的次数。
// context 是**判断依据**——缺陷原文、尝试次数、上轮纠偏、OBSERVE 给的建议修法、涉及需求。
// 没有它，用户只能看到"需要你指示"却不知道该指示什么：真实运行里 OBSERVE 判「缺陷」
// 问了两次，缺陷说明其实已经算出来了（`obs_out.parsed`），只是没接到这里。
//
// autoCountdown：auto 闸门模式下的剩余秒数（null=不显示倒计时）；倒计时归零自动继续。
import { useEffect, useState } from 'react'

export default function GateBar({
  action, reason, count, context, onContinue, onSteer, onAbort, onResolve,
  autoCountdown = null, onSteerFocus, onSteerBlur,
}) {
  const ctx = context || {}
  const defect = String(ctx.defect || '').trim()
  const suggestion = String(ctx.suggestion || '').trim()
  const blocked = ctx.blocked_requirements || []
  const [expanded, setExpanded] = useState(false)
  // 需求闸门：**本地累积选择**，全部选完再一次性提交。
  // 为什么不能"点一个就提交"：那样闸门立刻关掉，用户看不到还剩几条没定、
  // 也看不到自己刚选了什么——而这件事天然要连做几次（真实场景是 4 条）。
  const [picks, setPicks] = useState({})
  const [submitting, setSubmitting] = useState(false)

  // ★ 兜底：闸门上下文一变（后端推来新 gate 事件、或另一个闸门接管），
  //   立刻解除"正在提交…"。否则一旦某条提交路径拿不到新事件，界面就永远卡住，
  //   看起来像"没反应"——真实事故里用户点了确认，后端 spec.json 确实写好了，界面却不动。
  useEffect(() => {
    setSubmitting(false)
  }, [reason, action, context])

  const label = reason || (action ? `${action.toUpperCase()} 之后` : '需要确认')
  const pct = autoCountdown !== null ? Math.max(0, Math.min(100, (autoCountdown / 30) * 100)) : 0

  // 缺陷全文默认折叠：前 3 行概览 + 可展开
  const defectLines = defect ? defect.split('\n') : []
  const previewLines = defectLines.slice(0, 3)
  const truncated = defectLines.length > 3

  const attempt = Number(ctx.attempt || 0)
  const attemptLimit = Number(ctx.attempt_limit || 0)

  // ── 需求契约闸门：**它有自己的形状**，不能套用缺陷模板 ──
  // 此前它复用了缺陷分支，于是：依据区不渲染（context 里没有 defect）、
  // 「继续」写着"按上面的缺陷自己修"（上面没有缺陷）、还多出一个永远禁用的
  // 「采纳建议」。这些都在截图上出现过。
  const units = ctx.units || []
  const noAcc = new Set(ctx.missing_acceptance || [])
  const irrev = new Set(ctx.irreversible || [])
  const pendingClarify = ctx.clarify || []
  // 已选但未提交的排除在外——用户能看清"还有几条要定"
  const unresolvedCount = pendingClarify.filter((c) => !picks[c.id]).length
  const isRequirements = action === 'requirements'
  if (isRequirements) {
    return (
      <div className="gatebar">
        <div className="gatebar-head">
          <span className="gatebar-hint">
            暂停：{reason || '需求契约待确认'}
            {count > 1 ? `（本次第 ${count} 次）` : ''}
          </span>
          <span className="gatebar-meta">
            {ctx.confirmed ? '已确认' : '未确认'} · 契约必须人工确认，不会自动通过
          </span>
        </div>

        {!ctx.spec_exists ? (
          <div className="gatebar-defect">
            <div className="gatebar-defect-title">还没有需求契约</div>
            <div className="gatebar-defect-body">
              模型尚未产出 requirement-set。让模型先调 submit_requirements 把任务拆成
              可验收的条目，再回来确认。
            </div>
          </div>
        ) : null}

        <div className="gatebar-goal">
          需求条目（{units.length} 条
          {noAcc.size ? ` · ${noAcc.size} 条无判据` : ''}
          {irrev.size ? ` · ${irrev.size} 条不可逆` : ''}）：
        </div>
        <div className="gatebar-units">
          {units.map((u) => {
            const tag = noAcc.has(u.id) ? 'missing'
              : (irrev.has(u.id) ? 'irrev' : 'ok')
            const tip = noAcc.has(u.id) ? '无判据 → 这条永远无法验收，请补或明确接受'
              : (irrev.has(u.id) ? '不可逆 → 验收会真实删改数据，须确认后才执行' : '有可执行判据')
            return (
              <div className={`unitrow unitrow-${tag}`} key={u.id}>
                <span className="unitrow-id">{u.id}</span>
                <span className="unitrow-tag">{tip}</span>
                <span className="unitrow-stmt">{u.statement}</span>
              </div>
            )
          })}
          {units.length === 0 ? <div className="unitrow">（合同里还没有条目）</div> : null}
        </div>

        {pendingClarify.length ? (
          <div className="gatebar-clarify">
            <div className="gatebar-clarify-title">
              待你决定的歧义（{pendingClarify.length} 条，已选 {Object.keys(picks).length} 条）
              ——不定下来会阻止验收
            </div>
            {pendingClarify.map((c) => {
              const chosen = picks[c.id]
              return (
                <div className={`clarifyrow${chosen ? ' clarifyrow-done' : ''}`} key={c.id}>
                  <div className="clarifyrow-q">
                    <strong>{c.id}</strong> {c.question}
                  </div>
                  {c.why ? <div className="clarifyrow-why">为什么重要：{c.why}</div> : null}
                  <div className="clarifyrow-opts">
                    {(c.options || []).map((o) => (
                      <button
                        className={`btn btn-mini${chosen === o ? ' btn-picked' : ''}`}
                        key={o}
                        disabled={submitting}
                        onClick={() => {
                          // 只更新本地状态：闸门不关、其余条目仍在，能看清自己选了什么
                          setPicks((p) => ({ ...p, [c.id]: o }))
                        }}
                      >
                        {chosen === o ? `已选：${o}` : `选：${o}`}
                      </button>
                    ))}
                    {!(c.options || []).length ? (
                      <span className="clarifyrow-why">
                        （该歧义没有给选项——请用「中止」后手工补 options，或让模型重出草稿）
                      </span>
                    ) : null}
                  </div>
                </div>
              )
            })}
          </div>
        ) : null}

        <div className="gatebar-paths">
          <button
            className="btn"
            disabled={submitting || unresolvedCount > 0}
            title={unresolvedCount > 0
              ? `还有 ${unresolvedCount} 条歧义没选，不能确认契约`
              : '确认这份契约，之后「实现」与「验收」都按它执行'}
            onClick={() => {
              setSubmitting(true)
              const body = Object.entries(picks).map(([k, v]) => `${k}=${v}`)
              if (!body.length) {
                // 没有待定歧义：直接确认
                onContinue?.()
                return
              }
              // ★ 两步走，避免"卡在 正在提交…"：
              //   ① 先**只落盘**决定（不带 [confirm]）——后端会落盘并重开闸门；
              //   ② 再发 `continue` 真正结束闸门（onContinue 会 dispatch consumed）。
              //   为什么不能像以前那样一个批量带 `[confirm]` 发出去：那条路成功后
              //   后端**不再发新 gate 事件**（闸门直接放行、开始实现），而前端此时
              //   已经 consumed=false、submitting=true —— 于是永远停在"正在提交…"，
              //   看起来像"没反应"（真实事故：用户点了确认，后端 spec.json 确实写好了
              //   confirmed:true，但界面卡住不动）。
              onResolve?.(null, null, body.join(';;'))
              onContinue?.()
            }}
          >
            <strong>
              {submitting ? '正在提交…'
                : unresolvedCount
                  ? `先定完剩下 ${unresolvedCount} 条歧义`
                  : '确认契约并开始'}
            </strong>
            <span className="path-note">
              {unresolvedCount
                ? '未决歧义会让「实现」写错方向、「验收」也无法判定——先逐条选定才放行'
                : '确认后「实现」与「验收」都按这份契约执行'}
              {noAcc.size ? `；${noAcc.size} 条无判据的会被记为未验收` : ''}
            </span>
          </button>
          <button className="btn btn-danger" onClick={() => onAbort?.()}>
            <strong>中止</strong>
            <span className="path-note">停下本次任务，保留已有轨迹与契约草稿</span>
          </button>
        </div>
      </div>
    )
  }

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
