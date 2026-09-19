// ASK 面板：模型缺信息时提问，阻塞等待用户回答。
import { useState } from 'react'

export default function AskPanel({ question, onAnswer, onSkip }) {
  const [text, setText] = useState('')
  const submit = () => {
    const v = text.trim()
    if (!v) return
    onAnswer?.(v)
    setText('')
  }
  return (
    <div className="askpanel">
      <div className="askpanel-title">? ASK · 模型提问</div>
      <div className="askpanel-question">{question}</div>
      <div className="askpanel-actions">
        <input
          className="ask-input"
          value={text}
          placeholder="输入回答…"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submit()
          }}
        />
        <button className="btn" onClick={submit}>回答</button>
        <button className="btn btn-danger" onClick={() => onSkip?.()}>跳过</button>
      </div>
    </div>
  )
}
