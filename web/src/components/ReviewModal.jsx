// 会话审查：对照 skill 检查当前会话或上传 markdown。
import { useState } from 'react'
import * as api from '../api'

export default function ReviewModal({ onClose, sessionId, inline }) {
  const [mode, setMode] = useState('current')
  const [markdown, setMarkdown] = useState('')
  const [loading, setLoading] = useState(false)
  const [report, setReport] = useState('')
  const [error, setError] = useState('')

  const run = async () => {
    setLoading(true)
    setError('')
    setReport('')
    try {
      const body = mode === 'current'
        ? await api.reviewCurrent(sessionId)
        : await api.reviewFile(markdown)
      setReport(body.report || '(空报告)')
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const inner = (
    <>
      <div className="modal-head">
        <h2>会话审查</h2>
        {!inline ? <button className="modal-close" onClick={onClose}>✕</button> : null}
      </div>
      <div className="modal-body">
        <div className="review-tabs">
          <button className={mode === 'current' ? 'btn btn-primary' : 'btn'} onClick={() => setMode('current')}>当前会话</button>
          <button className={mode === 'file' ? 'btn btn-primary' : 'btn'} onClick={() => setMode('file')}>上传 Markdown</button>
        </div>

        {mode === 'file' ? (
          <textarea
            className="review-input"
            placeholder="粘贴会话 markdown..."
            value={markdown}
            onChange={(e) => setMarkdown(e.target.value)}
            rows={8}
          />
        ) : (
          <p style={{ fontSize: 13, color: 'var(--muted)' }}>
            审查当前会话记录，对照 skills/ 下的 SKILL.md 检查合规性。
          </p>
        )}

        {loading ? <div className="notice">分析中（模型调用约需 10-30 秒）…</div> : null}
        {report ? <pre className="report-output">{report}</pre> : null}
        {error ? <div className="notice notice-error">{error}</div> : null}
      </div>
      <div className="modal-foot">
        <div className="modal-foot-actions">
          {!inline ? <button className="btn" onClick={onClose}>关闭</button> : null}
          <button className="btn btn-primary" disabled={loading || (mode === 'file' && !markdown.trim())} onClick={run}>
            {loading ? '分析中…' : '开始审查'}
          </button>
        </div>
      </div>
    </>
  )

  return inline ? (
    <div className="view-pane">{inner}</div>
  ) : (
    <div className="modal-mask"><div className="modal">{inner}</div></div>
  )
}
