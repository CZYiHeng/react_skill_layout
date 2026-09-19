// 顶部状态条：模型、绑定、执行器与沙箱开关。
const SLOTS = ['think', 'plan', 'act', 'observe', 'verify']

const GATE_MODES = [
  { value: 'plan', label: '计划', hint: '计划出来后确认一次，之后自动跑到异常或验收' },
  { value: 'step', label: '步进', hint: '每个步骤完成后暂停一次' },
  { value: 'auto', label: '自动', hint: '只在发现缺陷或最终验收时暂停' },
]

export default function StatusBar({
  config, binds, status, gateMode, onGateMode, workDir, onWorkDir,
  allowOutside, onAllowOutside, onPause, onAbort, onReset, onSave,
}) {
  const running = status === 'running'
  return (
    <header className="statusbar">
      <div className="statusbar-left">
        <strong>ReAct Agent</strong>
        {config?.model ? <span className="chip">{config.model}</span> : null}
        {gateMode ? (
          <label className="chip chip-dim gate-mode" title="控制人工被打断的频率">
            闸门
            <select
              className="gate-select"
              value={gateMode}
              onChange={(e) => onGateMode?.(e.target.value)}
            >
              {GATE_MODES.map((m) => (
                <option key={m.value} value={m.value} title={m.hint}>{m.label}</option>
              ))}
            </select>
          </label>
        ) : null}
        <label className="chip chip-dim" title="代码写到哪（默认只认项目内的子目录）">
          目录
          <input
            className="workdir-input"
            value={workDir || ''}
            placeholder="项目根目录"
            onChange={(e) => onWorkDir?.(e.target.value)}
          />
        </label>
        <label
          className={`chip chip-dim allow-outside${allowOutside ? ' allow-outside-on' : ''}`}
          title="默认只认项目根目录内的子目录。勾选后可用绝对路径指向项目外目录（须已存在），相对路径仍按项目内解析。"
        >
          <input
            type="checkbox"
            checked={!!allowOutside}
            onChange={(e) => onAllowOutside?.(e.target.checked)}
          />
          项目外
        </label>
        {binds
          ? SLOTS.map((s) => (
              <span key={s} className={binds[s] ? 'chip chip-on' : 'chip chip-off'}>
                {s}
                {binds[s] ? '✓' : '✗'}
              </span>
            ))
          : null}
        {config ? (
          <span className="chip chip-dim">
            shell {config.shell ? '开' : '关'} · 写入 {config.file_write ? '开' : '关'} ·
            沙箱 {config.sandbox ? 'OS级' : '关'}
          </span>
        ) : null}
      </div>
      <div className="statusbar-right">
        <span className={`status status-${status}`}>{status}</span>
        {running ? (
          <>
            <button
              className="btn btn-sm"
              title="在下一个阶段边界停下，可继续 / 纠偏 / 中止"
              onClick={() => onPause?.()}
            >
              暂停
            </button>
            <button className="btn btn-sm btn-danger" onClick={() => onAbort?.()}>
              中止
            </button>
          </>
        ) : null}
        <button className="btn btn-sm" onClick={() => onSave?.()}>导出纪要</button>
        <button className="btn btn-sm" onClick={() => onReset?.()}>清空</button>
      </div>
    </header>
  )
}
