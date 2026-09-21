// 设置弹窗：页面上读写 config.json。
// 打开时 GET /api/config 拉当前文件值，保存时 PUT 合并写回；
// 后端每个任务开始都会重读配置，故保存后下一个任务即生效，无需重启。
import { useEffect, useRef, useState } from 'react'
import * as api from '../api'

const GATE_MODES = ['plan', 'step', 'auto', 'phase']

export default function SettingsModal({ onClose, onSaved, allowOutside: currentAllowOutside, inline }) {
  const [form, setForm] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [showKey, setShowKey] = useState(false)
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
    <label className="set-field">
      <span>{label}</span>
      <input
        type="number"
        value={form[k] ?? ''}
        onChange={(e) => set(k, e.target.value === '' ? '' : Number(e.target.value))}
      />
      {hint ? <small>{hint}</small> : null}
    </label>
  )

  const strField = (k, label, hint) => (
    <label className="set-field">
      <span>{label}</span>
      <input type="text" value={form[k] ?? ''} onChange={(e) => set(k, e.target.value)} />
      {hint ? <small>{hint}</small> : null}
    </label>
  )

  const boolField = (k, label, hint) => (
    <label className={`set-toggle${form[k] ? ' on' : ''}`}>
      <input type="checkbox" checked={!!form[k]} onChange={(e) => set(k, e.target.checked)} />
      <span>{label}</span>
      {hint ? <small>{hint}</small> : null}
    </label>
  )

  const profiles = form?.profiles || []

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
              {profiles.length === 0 ? (
                <small style={{ display: 'block', marginBottom: 8 }}>
                  还没有档案。下面的"默认模型"是兼容旧配置；点"+ 新建档案"添加多模型。
                </small>
              ) : null}

              {profiles.map((p, i) => {
                const active = form.active_profile === p.name
                return (
                  <div key={p.name} className={`profile-card${active ? ' active' : ''}`}>
                    <div className="profile-card-head">
                      <span className="profile-name">{p.name}</span>
                      <div>
                        {!active ? (
                          <button type="button" className="btn btn-sm" onClick={() => useProfile(p.name)}>使用</button>
                        ) : (
                          <span className="profile-badge">当前使用</span>
                        )}
                        <button type="button" className="btn btn-sm btn-danger" onClick={() => removeProfile(i)}>删除</button>
                      </div>
                    </div>
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
                  </div>
                )
              })}

              <button type="button" className="btn btn-sm" onClick={addProfile}>+ 新建档案</button>
            </div>

            <div className="set-group">
              <h3>默认模型（无档案时使用）</h3>
              {strField('base_url', 'API 地址', 'OpenAI 兼容端点')}
              <label className="set-field">
                <span>API Key</span>
                <div className="key-row">
                  <input type={showKey ? 'text' : 'password'}
                    value={form.api_key ?? ''}
                    onChange={(e) => set('api_key', e.target.value)} />
                  <button type="button" className="btn btn-sm" onClick={() => setShowKey((v) => !v)}>
                    {showKey ? '隐藏' : '显示'}
                  </button>
                </div>
              </label>
              {strField('model', '模型名')}
              {strField('plan_model', '计划模型', '空 = 与默认模型相同')}
              {intField('plan_timeout_sec', '计划超时(秒)')}
            </div>

            <div className="set-group">
              <h3>循环与上下文</h3>
              <label className="set-field">
                <span>闸门档位</span>
                <select value={form.gate_mode ?? 'plan'} onChange={(e) => set('gate_mode', e.target.value)}>
                  {GATE_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
              </label>
              {intField('max_rounds', '最大轮数')}
              {intField('step_timeout_sec', '单步超时(秒)')}
              {intField('max_context_messages', '上下文消息数', '超出后做窗口化裁剪')}
              <label className="set-toggle">
                <input type="checkbox" checked={!!form.show_reasoning} onChange={(e) => set('show_reasoning', e.target.checked)} />
                <span>显示推理过程</span>
              </label>
            </div>

            <div className="set-group">
              <h3>执行器（默认关）</h3>
              {boolField('enable_shell_exec', '允许执行 shell', '开启后 ACT 可跑命令')}
              {boolField('enable_file_write', '允许写文件', '写入路径限项目内')}
              {intField('exec_timeout_sec', '执行超时(秒)')}
              {boolField('sandbox_shell', 'Windows OS 级沙箱', '受限令牌 + 作业对象')}
              {boolField('sandbox_integrity_low', '降低沙箱完整性级别', '需 cwd 降 IL，谨慎')}
            </div>

            <div className="set-group">
              <h3>工作目录</h3>
              {strField('work_dir', '工作目录', 'agent 干活的目录。填子目录名（如 src），留空=项目根目录；要指项目外目录请先勾选下方开关')}
              {boolField('allow_outside_work_dir', '允许项目外绝对路径')}
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

  return inline ? (
    <div className="view-pane">{inner}</div>
  ) : (
    <div className="modal-mask" ref={maskRef}><div className="modal">{inner}</div></div>
  )
}
