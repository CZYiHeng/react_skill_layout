// 左侧控制台：模型 / 闸门 / 目录 / 槽位绑定 / 执行器 / 操作按钮。
// 由原 StatusBar 的顶栏内容重构而来，布局改为竖排。
const SLOTS = ['think', 'plan', 'act', 'observe', 'verify']

const GATE_MODES = [
  { value: 'plan', label: '计划', hint: '计划出来后确认一次，之后自动跑到异常或验收' },
  { value: 'step', label: '步进', hint: '每个步骤完成后暂停一次' },
  { value: 'auto', label: '自动', hint: '只在发现缺陷或最终验收时暂停' },
]

export default function Sidebar({
  config, binds, status, gateMode, onGateMode, workDir, onWorkDir,
  allowOutside, onAllowOutside, onPause, onAbort, onReset, onSave, onSettings, totalTokens, onSwitchModel,
}) {
  const running = status === 'running'
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">R</div>
        <div>
          <h1>ReAct Agent</h1>
          <small>显式五阶段协议</small>
        </div>
      </div>

      <div>
        <div className="group-label">当前状态</div>
        <div className="status-pill">
          <span className={running ? 'pulse' : ''} />
          {status}
          {config ? ` · ${config.model}` : ''}
        </div>
        {totalTokens > 0 ? (
          <div className="token-total">累计 {totalTokens.toLocaleString()} tok</div>
        ) : null}
        {config?.profiles?.length > 0 ? (
          <div className="field" style={{ marginTop: 8 }}>
            <label>模型</label>
            <select value={config.active_profile || ''} onChange={(e) => onSwitchModel?.(e.target.value)}>
              <option value="">默认（{config.model}）</option>
              {config.profiles.map((p) => (
                <option key={p.name} value={p.name}>{p.name}</option>
              ))}
            </select>
          </div>
        ) : null}
      </div>

      <div>
        <div className="group-label">闸门与目录</div>
        <div className="field">
          <label>闸门档位</label>
          <select value={gateMode} onChange={(e) => onGateMode?.(e.target.value)}>
            {GATE_MODES.map((m) => (
              <option key={m.value} value={m.value} title={m.hint}>{m.label}</option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>工作目录</label>
          <input
            type="text"
            value={workDir || ''}
            placeholder="项目根目录"
            onChange={(e) => onWorkDir?.(e.target.value)}
          />
        </div>
        <label
          className={`toggle${allowOutside ? ' on' : ''}`}
          title="默认只认项目根目录内的子目录。勾选后可用绝对路径指向项目外目录（须已存在）。"
        >
          <input
            type="checkbox"
            checked={!!allowOutside}
            onChange={(e) => onAllowOutside?.(e.target.checked)}
          />
          项目外目录放行
        </label>
      </div>

      <div>
        <div className="group-label">槽位绑定</div>
        <div className="slot-row">
          {binds
            ? SLOTS.map((s) => (
                <span key={s} className={binds[s] ? 'slot on' : 'slot'}>
                  {s} {binds[s] ? '✓' : '✗'}
                </span>
              ))
            : <span className="slot">— 未加载 —</span>}
        </div>
      </div>

      <div>
        <div className="group-label">执行器</div>
        {config ? (
          <div className="exec">
            shell <b>{config.shell ? '开' : '关'}</b>
            {' · '}写入 <b>{config.file_write ? '开' : '关'}</b>
            <br />
            沙箱 <b>{config.sandbox ? 'OS 级' : '关'}</b>
          </div>
        ) : null}
      </div>

      <div className="side-btns">
        <button className="btn" onClick={() => onSettings?.()}>⚙ 设置</button>
        {running ? (
          <>
            <button className="btn" title="在下一个阶段边界停下" onClick={() => onPause?.()}>暂停</button>
            <button className="btn btn-danger" onClick={() => onAbort?.()}>中止</button>
          </>
        ) : null}
        <button className="btn" onClick={() => onSave?.()}>导出纪要</button>
        <button className="btn" onClick={() => onReset?.()}>清空会话</button>
      </div>
    </aside>
  )
}
