// 设置弹窗：页面上读写 config.json。
// 打开时 GET /api/config 拉当前文件值，保存时 PUT 合并写回；
// 后端每个任务开始都会重读配置，故保存后下一个任务即生效，无需重启。
import { useEffect, useRef, useState } from 'react'
import * as api from '../api'

const GATE_MODES = ['plan', 'step', 'auto', 'phase']

export default function SettingsModal({ onClose, onSaved, allowOutside: currentAllowOutside }) {
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
        // 左侧栏勾选的放行开关可能还没落盘，用当前前端状态覆盖，避免保存时回写旧值
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

  const save = async () => {
    setSaving(true)
    setError('')
    try {
      // 把当前模型字段写回选中的 profile
      const profiles = [...(form.profiles || [])]
      const profData = {
        base_url: form.base_url || '',
        api_key: form.api_key || '',
        model: form.model || '',
        timeout_sec: form.step_timeout_sec || 120,
      }
      if (form.active_profile) {
        const idx = profiles.findIndex(p => p.name === form.active_profile)
        if (idx >= 0) profiles[idx] = { ...profiles[idx], ...profData }
        else profiles.push({ name: form.active_profile, ...profData })
      }
      await api.saveConfig({ ...form, profiles })
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

  return (
    <div className="modal-mask" ref={maskRef}>
      <div className="modal">
        <div className="modal-head">
          <h2>设置</h2>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>

        {loading ? (
          <div className="modal-body">加载中…</div>
        ) : error && !form ? (
          <div className="modal-body notice notice-error">{error}</div>
        ) : form ? (
          <div className="modal-body">
            <div className="set-group">
              <h3>模型</h3>
              <label className="set-field">
                <span>当前档案</span>
                <select value={form.active_profile ?? ''} onChange={(e) => {
                  const name = e.target.value
                  const prof = (form.profiles || []).find(p => p.name === name)
                  set('active_profile', name)
                  if (prof) {
                    set('base_url', prof.base_url || '')
                    set('api_key', prof.api_key || '')
                    set('model', prof.model || '')
                  }
                }}>
                  <option value="">（默认/单模型）</option>
                  {(form.profiles || []).map(p => (
                    <option key={p.name} value={p.name}>{p.name}</option>
                  ))}
                </select>
                <small>切换档案只影响新任务；保存时当前地址/Key/模型会写回该档案</small>
              </label>
              {strField('base_url', 'API 地址', 'OpenAI 兼容端点，如 https://api.deepseek.com')}
              <label className="set-field">
                <span>API Key</span>
                <div className="key-row">
                  <input
                    type={showKey ? 'text' : 'password'}
                    value={form.api_key ?? ''}
                    onChange={(e) => set('api_key', e.target.value)}
                  />
                  <button type="button" className="btn btn-sm" onClick={() => setShowKey((v) => !v)}>
                    {showKey ? '隐藏' : '显示'}
                  </button>
                </div>
              </label>
              {strField('model', '模型名', '如 deepseek-chat / kimi-k2.7')}
              {strField('plan_model', '计划模型', '空 = 与 model 相同；填推理模型可提升规划质量')}
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
            <button className="btn" onClick={onClose}>取消</button>
            <button className="btn btn-primary" disabled={!form || saving} onClick={save}>
              {saving ? '保存中…' : '保存'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
