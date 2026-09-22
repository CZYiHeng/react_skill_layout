// 设置：页面上读写 config.json。
// 打开时 GET /api/config 拉当前文件值，保存时 PUT 合并写回；
// 后端每个任务开始都会重读配置，故保存后下一个任务即生效，无需重启。
// inline（标签页）模式：左侧子导航 + 右侧分组内容；弹窗模式：单页滚动。
import { useEffect, useRef, useState } from 'react'
import * as api from '../api'

const GATE_MODES = ['plan', 'step', 'auto', 'phase']
const GATE_LABELS = {
  plan: '计划：每个阶段前确认',
  step: '步进：每一步都确认',
  auto: '自动：仅缺陷与最终验收暂停',
  phase: '阶段：阶段结束时确认',
}

const SECTIONS = [
  { key: 'models', title: '模型档案', desc: '每个档案独立保存地址、Key 和模型名；左侧栏可快速切换当前档案。' },
  { key: 'loop', title: '循环与上下文', desc: '控制 ReAct 循环的闸门、轮数、超时与上下文裁剪。' },
  { key: 'executor', title: '执行器', desc: 'ACT 阶段真实执行 shell 与写文件的开关，默认关闭。' },
  { key: 'workdir', title: '工作目录', desc: 'Agent 干活的根目录与目录越界策略。' },
]

export default function SettingsModal({ onClose, onSaved, allowOutside: currentAllowOutside, inline }) {
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

  // 左侧开关变化时同步到 form（组件常驻，首次加载后 store 变化不会重新拉配置）
  useEffect(() => {
    setForm((f) => (f ? { ...f, allow_outside_work_dir: currentAllowOutside } : f))
  }, [currentAllowOutside])

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }))

  // 更新某个档案的字段
  const updateProfile = (idx, field, value) => {
    setForm((f) => {
      const profiles = [...(f.profiles || [])]
      profiles[idx] = { ...profiles[idx], [field]: value }
      return { ...f, profiles }
    })
  }

  const addProfile = () => {
    const name = prompt('新档案名称（如 deepseek / kimi / qwen）：')
    if (!name) return
    setForm((f) => ({
      ...f,
      profiles: [...(f.profiles || []), { name, base_url: '', api_key: '', model: '', timeout_sec: 120 }],
    }))
  }

  const removeProfile = (idx) => {
    const p = form.profiles[idx]
    if (!confirm(`删除档案 "${p.name}"？`)) return
    setForm((f) => {
      const profiles = (f.profiles || []).filter((_, i) => i !== idx)
      const patch = { ...f, profiles }
      if (f.active_profile === p.name) patch.active_profile = ''
      return patch
    })
  }

  const useProfile = (name) => {
    set('active_profile', name)
  }

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

  const profiles = form?.profiles || []

  // —— 各分区内容 ——
  const modelsSection = (
    <>
      {profiles.length === 0 ? (
        <small style={{ display: 'block', marginBottom: 10, color: 'var(--muted)' }}>
          还没有档案。下方的"默认模型"即当前使用的配置；点"+ 新建档案"可添加多模型。
        </small>
      ) : null}

      {profiles.map((p, i) => {
        const active = form.active_profile === p.name
        return (
          <details key={p.name} className="profile" open={active}>
            <summary className="profile-head">
              <div className="profile-title">
                {p.name}
                {active ? <span className="badge-current">当前使用</span> : null}
              </div>
              <span className="profile-chev">▾</span>
            </summary>
            <div className="profile-body">
              <label className="set-field">
                <span>API 地址</span>
                <input type="text" value={p.base_url || ''}
                  onChange={(e) => updateProfile(i, 'base_url', e.target.value)}
                  placeholder="https://api.deepseek.com" />
              </label>
              <label className="set-field">
                <span>API Key</span>
                <div className="key-row">
                  <input type={showKey ? 'text' : 'password'} value={p.api_key || ''}
                    onChange={(e) => updateProfile(i, 'api_key', e.target.value)} />
                  <button type="button" className="btn btn-sm" onClick={() => setShowKey((v) => !v)}>
                    {showKey ? '隐藏' : '显示'}
                  </button>
                </div>
              </label>
              <label className="set-field">
                <span>模型名</span>
                <input type="text" value={p.model || ''}
                  onChange={(e) => updateProfile(i, 'model', e.target.value)}
                  placeholder="deepseek-chat / kimi-k2 / qwen-plus" />
              </label>
              <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
                {!active ? (
                  <button type="button" className="btn btn-sm btn-primary" onClick={() => useProfile(p.name)}>使用此档案</button>
                ) : null}
                <button type="button" className="btn btn-sm btn-danger" onClick={() => removeProfile(i)}>删除档案</button>
              </div>
            </div>
          </details>
        )
      })}

      <button type="button" className="btn btn-sm" style={{ width: '100%', marginTop: 2 }} onClick={addProfile}>
        + 新建档案
      </button>

      <details className="profile" style={{ marginTop: 18 }} open={profiles.length === 0}>
        <summary className="profile-head">
          <div className="profile-title">默认模型（无档案时使用）</div>
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
      <label className="set-field">
        <span>闸门档位</span>
        <select value={form?.gate_mode ?? 'plan'} onChange={(e) => set('gate_mode', e.target.value)}>
          {GATE_MODES.map((m) => <option key={m} value={m}>{GATE_LABELS[m] || m}</option>)}
        </select>
      </label>
      {intField('max_rounds', '最大轮数')}
      {intField('step_timeout_sec', '单步超时(秒)')}
      {intField('max_context_messages', '上下文消息数', '超出后做窗口化裁剪')}
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
      {strField('work_dir', '工作目录', 'Agent 干活的目录。相对路径按项目内解析，留空 = 项目根目录；指向项目外须勾选下方开关并填绝对路径')}
      {boolField('allow_outside_work_dir', '允许项目外绝对路径', '勾选后工作目录可指向项目根目录之外（须已存在）')}
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
