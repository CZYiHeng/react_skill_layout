// 设置：页面上读写 config.json。
// 打开时 GET /api/config 拉当前文件值，保存时 PUT 合并写回；
// 后端每个任务开始都会重读配置，故保存后下一个任务即生效，无需重启。
// inline（标签页）模式：左侧子导航 + 右侧分组内容；弹窗模式：单页滚动。
import { useEffect, useRef, useState } from 'react'
import * as api from '../api'

const GATE_MODES = ['plan', 'step', 'auto', 'phase']
const GATE_LABELS = {
  auto: '自动：仅缺陷与最终验收暂停（默认）',
  plan: '计划：计划产出后额外确认一次',
  step: '步进：每个步骤收尾都确认',
  phase: '阶段：每个阶段都确认（旧行为）',
}

const SECTIONS = [
  { key: 'models', title: '模型档案', desc: '每个档案独立保存地址、Key 和模型名；左侧栏可快速切换当前档案。' },
  { key: 'loop', title: '循环与上下文', desc: '控制 ReAct 循环的闸门、轮数、超时与上下文裁剪。' },
  { key: 'executor', title: '执行器', desc: 'ACT 阶段真实执行 shell 与写文件的开关，默认关闭。' },
  { key: 'workdir', title: '工作目录', desc: 'Agent 干活的根目录与目录越界策略。' },
]

export default function SettingsModal({
  onClose, onSaved, allowOutside: currentAllowOutside, inline,
  workDir: currentWorkDir, gateMode: currentGateMode,
}) {
  const [form, setForm] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [showKey, setShowKey] = useState(false)
  const [section, setSection] = useState('models')
  const maskRef = useRef(null)

  useEffect(() => {
    api.getConfig()
      .then((data) => {
        const cfg = { ...(data.config || {}) }
        if (currentAllowOutside !== undefined) {
          cfg.allow_outside_work_dir = currentAllowOutside
        }
        setForm(cfg)
        setError('')
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  // 左侧栏改「闸门档位 / 工作目录 / 允许项目外」时同步到 form。
  //
  // 为什么必须同步：这三个字段在运行时**有两份值**——
  //   · store 的活状态（gateMode / workDir / allowOutside）：每个任务随 payload 下发
  //     （App.jsx 的 gate_mode、work_dir），**真正决定这次运行的行为**；
  //   · 磁盘 config.json：下次启动的初始值。
  // 本组件此前只在挂载时拉一次磁盘值，于是侧栏改了作用域/档位后，设置界面仍显示旧值——
  // 用户看到的是一个"既不等于生效值、也不等于他刚输入的值"的数字，无从判断当前设置。
  // 统一以**生效值**为准展示（`!== undefined` 时才覆盖，避免把"未传"当成"清空"）。
  useEffect(() => {
    setForm((f) => {
      if (!f) return f
      const next = { ...f }
      if (currentWorkDir !== undefined) next.work_dir = currentWorkDir
      if (currentAllowOutside !== undefined) next.allow_outside_work_dir = currentAllowOutside
      if (currentGateMode !== undefined) next.gate_mode = currentGateMode
      return next
    })
  }, [currentWorkDir, currentAllowOutside, currentGateMode])

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }))

  // 归一逻辑与后端/接口层共用一份（api.providersOf），避免三处各写一遍
  const providersOf = (f) => api.providersOf(f)

  // 更新某个 provider 的字段
  const updateProvider = (name, field, value) => {
    setForm((f) => {
      const provs = providersOf(f)
      return { ...f, providers: { ...provs, [name]: { ...provs[name], [field]: value } } }
    })
  }

  const addProvider = () => {
    const name = prompt('新 provider 名称（如 deepseek / kimi / qwen）：')
    if (!name) return
    setForm((f) => {
      const provs = providersOf(f)
      if (provs[name]) { return f }
      return {
        ...f,
        providers: { ...provs, [name]: { base_url: '', api_key: '', model: '', timeout_sec: 120 } },
        active_provider: f.active_provider || name,
      }
    })
  }

  const removeProvider = (name) => {
    if (!confirm(`删除 provider "${name}"？`)) return
    setForm((f) => {
      const provs = { ...providersOf(f) }
      delete provs[name]
      const patch = { ...f, providers: provs }
      if ((f.active_provider || f.active_profile) === name) {
        patch.active_provider = Object.keys(provs)[0] || ''
      }
      return patch
    })
  }

  const useProvider = (name) => set('active_provider', name)

  const save = async () => {
    setSaving(true)
    setError('')
    try {
      await api.saveConfig(form)
      onSaved?.()
      onClose()
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  const intField = (k, label, hint) => (
    <label className="set-field" key={k}>
      <span>{label}</span>
      <input
        type="number"
        value={form?.[k] ?? ''}
        onChange={(e) => set(k, e.target.value === '' ? '' : Number(e.target.value))}
      />
      {hint ? <small>{hint}</small> : null}
    </label>
  )

  const strField = (k, label, hint) => (
    <label className="set-field" key={k}>
      <span>{label}</span>
      <input type="text" value={form?.[k] ?? ''} onChange={(e) => set(k, e.target.value)} />
      {hint ? <small>{hint}</small> : null}
    </label>
  )

  const boolField = (k, label, hint) => (
    <label className={`set-toggle${form?.[k] ? ' on' : ''}`} key={k}>
      <input type="checkbox" checked={!!form?.[k]} onChange={(e) => set(k, e.target.checked)} />
      <span>{label}</span>
      {hint ? <small>{hint}</small> : null}
    </label>
  )

  const providers = providersOf(form)

  // —— 各分区内容 ——
  const modelsSection = (
    <>
      {Object.keys(providers).length === 0 ? (
        <small style={{ display: 'block', marginBottom: 10, color: 'var(--muted)' }}>
          还没有 provider。下方"默认接入"即当前使用的单家配置；点"+ 新建 provider"可添加多家并切换。
        </small>
      ) : null}

      {Object.entries(providers).map(([name, p]) => {
        const active = (form.active_provider || form.active_profile) === name
        return (
          <details key={name} className="profile" open={active}>
            <summary className="profile-head">
              <div className="profile-title">
                {name}
                {active ? <span className="badge-current">当前使用</span> : null}
              </div>
              <span className="profile-chev">▾</span>
            </summary>
            <div className="profile-body">
              <label className="set-field">
                <span>API 地址</span>
                <input type="text" value={p.base_url || ''}
                  onChange={(e) => updateProvider(name, 'base_url', e.target.value)}
                  placeholder="https://api.deepseek.com" />
              </label>
              <label className="set-field">
                <span>API Key</span>
                <div className="key-row">
                  <input type={showKey ? 'text' : 'password'} value={p.api_key || ''}
                    onChange={(e) => updateProvider(name, 'api_key', e.target.value)} />
                  <button type="button" className="btn btn-sm" onClick={() => setShowKey((v) => !v)}>
                    {showKey ? '隐藏' : '显示'}
                  </button>
                </div>
              </label>
              <label className="set-field">
                <span>模型名</span>
                <input type="text" value={p.model || ''}
                  onChange={(e) => updateProvider(name, 'model', e.target.value)}
                  placeholder="deepseek-chat / kimi-k2 / qwen-plus" />
              </label>
              <label className="set-field">
                <span>单步超时(秒)</span>
                <input type="number" value={p.timeout_sec ?? 120}
                  onChange={(e) => updateProvider(name, 'timeout_sec', Number(e.target.value))} />
              </label>
              <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
                {!active ? (
                  <button type="button" className="btn btn-sm btn-primary" onClick={() => useProvider(name)}>使用此 provider</button>
                ) : null}
                <button type="button" className="btn btn-sm btn-danger" onClick={() => removeProvider(name)}>删除 provider</button>
              </div>
            </div>
          </details>
        )
      })}

      <button type="button" className="btn btn-sm" style={{ width: '100%', marginTop: 2 }} onClick={addProvider}>
        + 新建 provider
      </button>

      <details className="profile" style={{ marginTop: 18 }} open={Object.keys(providers).length === 0}>
        <summary className="profile-head">
          <div className="profile-title">默认接入（没有 provider 时使用）</div>
          <span className="profile-chev">▾</span>
        </summary>
        <div className="profile-body">
          {strField('base_url', 'API 地址', 'OpenAI 兼容端点')}
          <label className="set-field">
            <span>API Key</span>
            <div className="key-row">
              <input type={showKey ? 'text' : 'password'}
                value={form?.api_key ?? ''}
                onChange={(e) => set('api_key', e.target.value)} />
              <button type="button" className="btn btn-sm" onClick={() => setShowKey((v) => !v)}>
                {showKey ? '隐藏' : '显示'}
              </button>
            </div>
          </label>
          {strField('model', '模型名')}
        </div>
      </details>

      <div style={{ marginTop: 18 }}>
        {strField('plan_model', '计划模型（可选）', '留空 = 与当前模型相同；PLAN 阶段可单独用推理模型')}
        {intField('plan_timeout_sec', '计划超时(秒)')}
      </div>
    </>
  )

  const loopSection = (
    <>
      <small style={{ display: 'block', marginBottom: 10, color: 'var(--muted)' }}>
        「闸门档位」显示的是<strong>当前生效值</strong>（与左侧栏同一个来源）；
        其余循环参数来自配置文件。
      </small>
      <label className="set-field">
        <span>闸门档位</span>
        <select value={form?.gate_mode ?? 'auto'} onChange={(e) => set('gate_mode', e.target.value)}>
          {GATE_MODES.map((m) => <option key={m} value={m}>{GATE_LABELS[m] || m}</option>)}
        </select>
      </label>
      {intField('max_rounds', '最大轮数')}
      {intField('max_context_tokens', '上下文预算(token)', '预估 prompt 超此值才压缩；调小=更省但压缩更频繁')}
      <label className="set-toggle">
        <input type="checkbox" checked={!!form?.show_reasoning} onChange={(e) => set('show_reasoning', e.target.checked)} />
        <span>显示推理过程</span>
      </label>
    </>
  )

  const executorSection = (
    <>
      {boolField('enable_shell_exec', '允许执行 shell', '开启后 ACT 可跑命令')}
      {boolField('enable_file_write', '允许写文件', '写入路径限工作目录内')}
      {intField('exec_timeout_sec', '执行超时(秒)')}
      {boolField('sandbox_shell', 'Windows OS 级沙箱', '受限令牌 + 作业对象')}
      {boolField('sandbox_integrity_low', '降低沙箱完整性级别', '需 cwd 降 IL，谨慎开启')}
    </>
  )

  const workdirSection = (
    <>
      <small style={{ display: 'block', marginBottom: 10, color: 'var(--muted)' }}>
        显示的是<strong>当前生效值</strong>（与左侧栏同一个来源）。这里保存会写进配置文件，
        作为下次启动的默认值；左侧栏只改本次运行、不落盘。
      </small>
      {strField('work_dir', '工作目录', 'Agent 干活的目录 = 工具边界。相对路径按项目内解析，留空 = 项目根目录；要点到项目外（如 G:\\one）须勾选下方开关并填绝对路径')}
      {boolField('allow_outside_work_dir', '在项目外使用工作目录',
        '打开后「工作目录」可以指向 react-agent 之外的文件夹（例如在 G:\\one 建工程）。注意它只决定工作目录的位置，不放宽工具边界——文件读写仍限制在工作目录（加上下方白名单）之内')}
      <label className="set-field">
        <span>额外允许的根目录</span>
        <textarea
          rows={3}
          value={(form?.extra_roots || []).join('\n')}
          placeholder={'每行一个绝对路径，例如：\nG:\\three'}
          onChange={(e) => set('extra_roots',
            e.target.value.split('\n').map((s) => s.trim()).filter(Boolean))}
        />
        <small style={{ color: 'var(--muted)' }}>
          需要读写「工作目录」之外的文件夹时，在这里显式列出（每行一个）。
          这是访问外部文件夹的唯一开关；留空则只能动工作目录内的文件。
        </small>
      </label>
    </>
  )

  const sectionBody = {
    models: modelsSection,
    loop: loopSection,
    executor: executorSection,
    workdir: workdirSection,
  }
  const currentMeta = SECTIONS.find((s) => s.key === section)

  // inline 标签页模式：子导航布局
  if (inline && form) {
    return (
      <div className="view-pane settings-pane">
        <nav className="set-nav">
          {SECTIONS.map((s) => (
            <button
              key={s.key}
              className={section === s.key ? 'on' : ''}
              onClick={() => setSection(s.key)}
            >
              {s.title}
            </button>
          ))}
        </nav>
        <div className="set-content">
          <h2>{currentMeta.title}</h2>
          <p className="sec-desc">{currentMeta.desc}</p>
          {sectionBody[section]}
          {error ? <div className="notice notice-error" style={{ margin: '12px 0 0' }}>{error}</div> : null}
          <div className="save-bar">
            <span className="set-tip">保存后，<b>下一个任务</b>生效（当前运行任务不受影响）</span>
            <button className="btn btn-primary" disabled={saving} onClick={save}>
              {saving ? '保存中…' : '保存'}
            </button>
          </div>
        </div>
      </div>
    )
  }

  // 弹窗模式（保留单页滚动）
  const inner = (
    <>
      <div className="modal-head">
        <h2>设置</h2>
        {!inline ? <button className="modal-close" onClick={onClose}>✕</button> : null}
      </div>

      {loading ? (
        <div className="modal-body">加载中…</div>
      ) : error && !form ? (
        <div className="modal-body notice notice-error">{error}</div>
      ) : form ? (
        <div className="modal-body">
          <div className="set-group">
            <h3>模型档案</h3>
            {modelsSection}
          </div>
          <div className="set-group">
            <h3>循环与上下文</h3>
            {loopSection}
          </div>
          <div className="set-group">
            <h3>执行器（默认关）</h3>
            {executorSection}
          </div>
          <div className="set-group">
            <h3>工作目录</h3>
            {workdirSection}
          </div>
          {error ? <div className="notice notice-error">{error}</div> : null}
        </div>
      ) : null}

      <div className="modal-foot">
        <span className="set-tip">保存后，<b>下一个任务</b>生效（当前运行任务不受影响）</span>
        <div className="modal-foot-actions">
          {!inline ? <button className="btn" onClick={onClose}>取消</button> : null}
          <button className="btn btn-primary" disabled={!form || saving} onClick={save}>
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </>
  )

  return (
    <div className="modal-mask" ref={maskRef}><div className="modal">{inner}</div></div>
  )
}
