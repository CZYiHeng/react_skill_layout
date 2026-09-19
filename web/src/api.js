// 后端接口封装：REST 用 fetch，事件流用 EventSource（SSE）。
// 所有路径以 /api 开头，开发时由 Vite 代理到 Python 后端，生产时同源。

const BASE = '/api'

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  const text = await res.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = { raw: text }
  }
  if (!res.ok) {
    throw new Error(data?.error || `请求失败 ${res.status}`)
  }
  return data
}

export function createSession() {
  return request('/session', { method: 'POST' })
}

export function postTask(sessionId, task, opts = {}) {
  return request('/task', {
    method: 'POST',
    body: JSON.stringify({ session_id: sessionId, task, ...opts }),
  })
}

export function postControl(sessionId, cmd, text = '') {
  return request('/control', {
    method: 'POST',
    body: JSON.stringify({ session_id: sessionId, cmd, text }),
  })
}

export function postAnswer(sessionId, text) {
  return request('/answer', {
    method: 'POST',
    body: JSON.stringify({ session_id: sessionId, text }),
  })
}

export function getState(sessionId) {
  return request(`/state?session_id=${encodeURIComponent(sessionId)}`)
}

export function getSave(sessionId) {
  return request(`/save?session_id=${encodeURIComponent(sessionId)}`)
}

export function postReset(sessionId) {
  return request('/reset', {
    method: 'POST',
    body: JSON.stringify({ session_id: sessionId }),
  })
}

/**
 * 打开 SSE 事件流。每条事件回调 onEvent(data)；流结束回调 onEnd()。
 * 返回关闭函数。
 */
export function openEventStream(sessionId, onEvent, onEnd) {
  const url = `${BASE}/events?session_id=${encodeURIComponent(sessionId)}`
  const es = new EventSource(url)
  es.onmessage = (e) => {
    try {
      onEvent(JSON.parse(e.data))
    } catch {
      // 忽略无法解析的帧
    }
  }
  es.onerror = () => {
    // done/error 事件后服务端会关闭连接，此处统一收口
    es.close()
    if (onEnd) onEnd()
  }
  return () => es.close()
}
