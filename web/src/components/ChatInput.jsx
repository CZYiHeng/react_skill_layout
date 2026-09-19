// 任务输入框。
import { useState } from 'react'

export default function ChatInput({ disabled, onSubmit }) {
  const [text, setText] = useState('')
  const send = () => {
    const v = text.trim()
    if (!v || disabled) return
    onSubmit?.(v)
    setText('')
  }
  return (
    <div className="chatinput">
      <textarea
        className="chat-textarea"
        rows={2}
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
      <button className="btn btn-primary" disabled={disabled || !text.trim()} onClick={send}>
        发送
      </button>
    </div>
  )
}
