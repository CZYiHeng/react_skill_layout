// 任务输入框。
import { useState } from 'react'

const GATE_HINT = {
  plan: '计划闸门：计划产出后确认一次',
  step: '步进闸门：每个步骤完成后暂停',
  auto: '自动闸门：仅缺陷与最终验收时暂停',
  phase: '阶段闸门：每个阶段结束后确认一次',
}

export default function ChatInput({ disabled, onSubmit, gateMode }) {
  const [text, setText] = useState('')
  const send = () => {
    const v = text.trim()
    if (!v || disabled) return
    onSubmit?.(v)
    setText('')
  }
  return (
    <div>
      <div className="chatinput">
        <textarea
          className="chat-textarea"
          rows={1}
          value={text}
          disabled={disabled}
          placeholder={disabled ? '任务进行中…' : '输入任务，回车发送（Shift+Enter 换行）'}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              send()
            }
          }}
        />
        <button className="send" disabled={disabled || !text.trim()} onClick={send} title="发送">
          ↑
        </button>
      </div>
      <div className="composer-hint">{GATE_HINT[gateMode] || ''}</div>
    </div>
  )
}
