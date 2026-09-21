// 会话审查：对照 skill 检查当前会话或上传 markdown。
import { useState } from 'react'
import * as api from '../api'

export default function ReviewModal({ onClose, sessionId, inline }) {
  const [mode, setMode] = useState('current')
  const [markdown, setMarkdown] = useState('')
  const [fileName, setFileName] = useState('')
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
      <div className="review-header">
        <h2>会话审查</h2>
        <p className="review-subtitle">对照 skills/ 下的 SKILL.md，检查会话流程是否合规</p>
      </div>

      <div className="review-modes">
        <button className={mode === 'current' ? 'review-mode active' : 'review-mode'}
          onClick={() => setMode('current')}>
          <span className="review-mode-icon">▶</span>
          <span>当前会话</span>
          <small>直接分析正在运行的对话</small>
        </button>
        <button className={mode === 'file' ? 'review-mode active' : 'review-mode'}
          onClick={() => setMode('file')}>
          <span className="review-mode-icon">📄</span>
          <span>上传 Markdown</span>
          <small>选择或粘贴历史会话文件</small>
        </button>
      </div>

      <div className="review-body">
        {mode === 'file' ? (
          <div className="review-file-area">
            <label className="file-drop">
              <input
                type="file"
                accept=".md,.markdown,.txt"
                style={{ display: 'none' }}
                onChange={(e) => {
                  const f = e.target.files?.[0]
                  if (!f) return
                  setFileName(f.name)
                  const reader = new FileReader()
                  reader.onload = () => setMarkdown(reader.result || '')
                  reader.readAsText(f)
                }}
              />
              <span className="file-drop-text">{fileName || '点击选择 .md 文件'}</span>
            </label>
            <textarea
              className="review-input"
              placeholder="或直接粘贴会话 markdown..."
              value={markdown}
              onChange={(e) => setMarkdown(e.target.value)}
            />
          </div>
        ) : (
          <div className="review-current-hint">
            <p>将分析当前会话的完整记录，包括：</p>
            <ul>
              <li>用户输入与模型输出</li>
              <li>工具调用与执行回显</li>
              <li>五阶段流程是否合规</li>
            </ul>
            <p className="review-warn">注意：当前会话需要有内容才能分析</p>
          </div>
        )}

        {loading ? <div className="notice">分析中，模型调用约需 10-30 秒…</div> : null}
        {report ? <pre className="report-output">{report}</pre> : null}
        {error ? <div className="notice notice-error">{error}</div> : null}
      </div>

      <div className="review-foot">
        {!inline ? <button className="btn" onClick={onClose}>关闭</button> : null}
        <button className="btn btn-primary" disabled={loading || (mode === 'file' && !markdown.trim())} onClick={run}>
          {loading ? '分析中…' : '开始审查'}
        </button>
      </div>
    </>
  )

  return inline ? (
    <div className="view-pane review-pane">{inner}</div>
  ) : (
    <div className="modal-mask"><div className="modal">{inner}</div></div>
  )
}
