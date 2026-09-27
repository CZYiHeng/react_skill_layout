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

export function getConfig() {
  return request('/config', { method: 'GET' })
}

export function saveConfig(config) {
  return request('/config', {
    method: 'PUT',
    body: JSON.stringify(config),
  })
}

/**
 * 统一配置结构：后端两套字段名（/api/session 返回精简结构 shell/file_write/sandbox，
 * /api/config 返回文件原始结构 enable_shell_exec/enable_file_write/sandbox_shell）。
 * 合并输出，两种字段都保留——Sidebar 徽章读精简字段、store 读文件字段均可用。
 * 精简字段优先保留已有值（避免 /api/session 结构被误映射为 false）。
 */
/**
 * 把配置里的接入部分归一成 `{名字: provider}`（纯函数，不修改入参）。
 * 兼容三种历史形状：providers map / 旧 profiles 数组 / 最旧的顶层三件套。
 * 与后端 `react/config.py: normalize_providers` 同语义——两边各写一份是这个项目
 * 踩过的坑（同一套"取生效接入"的逻辑曾被写三遍且互不一致）。
 */
export function providersOf(cfg) {
  const out = { ...(cfg?.providers || {}) }
  for (const p of cfg?.profiles || []) {
    if (p?.name && !out[p.name]) {
      const { name, ...rest } = p
      out[name] = rest
    }
  }
  const legacyName = cfg?.active_provider || cfg?.active_profile || 'default'
  if (Object.keys(out).length === 0 && cfg?.base_url) {
    out[legacyName] = {
      base_url: cfg.base_url || '',
      api_key: cfg.api_key || '',
      model: cfg.model || '',
      timeout_sec: 120,
    }
  }
  return out
}

export function normalizeConfig(full) {
  const cfg = { ...(full || {}) }
  if (!('shell' in cfg)) cfg.shell = !!cfg.enable_shell_exec
  if (!('file_write' in cfg)) cfg.file_write = !!cfg.enable_file_write
  if (!('sandbox' in cfg)) cfg.sandbox = !!cfg.sandbox_shell
  cfg.providers = providersOf(cfg)
  cfg.active_provider = cfg.active_provider || cfg.active_profile || ''
  return cfg
}

export function reviewCurrent(sessionId) {
  return request('/review/current', {
    method: 'POST',
    body: JSON.stringify({ session_id: sessionId }),
  })
}

export function reviewFile(markdown) {
  return request('/review/file', {
    method: 'POST',
    body: JSON.stringify({ markdown }),
  })
}

export function getTokenStats(sessionId = '') {
  const q = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
  return request(`/token_stats${q}`, { method: 'GET' })
}

/**
 * 打开 SSE 事件流。每条事件回调 onEvent(data)；流结束回调 onEnd()。
 * 返回关闭函数。
 *
 * 重连策略：收到 done/error 事件标记正常结束并 close；异常断线不主动 close，
 * 交由原生 EventSource 自动重连（默认约 3s 间隔）；连续错误超上限后放弃。
 * 注意：后端事件队列取走即移除，重连不补放断线期间的事件（后续事件正常接收）。
 */
export function openEventStream(sessionId, onEvent, onEnd) {
  const url = `${BASE}/events?session_id=${encodeURIComponent(sessionId)}`
  const es = new EventSource(url)
  let normalEnd = false
  let errorCount = 0
  const MAX_ERRORS = 10  // 原生重连连续失败上限，超过则放弃

  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data)
      if (data.type === 'done' || data.type === 'error') {
        normalEnd = true
      }
      errorCount = 0  // 收到任何消息重置错误计数
      onEvent(data)
    } catch {
      // 忽略无法解析的帧
    }
  }
  es.onerror = () => {
    if (normalEnd) {
      es.close()
      if (onEnd) onEnd()
      return
    }
    // 异常断线：不调用 close，让原生 EventSource 自动重连
    errorCount++
    if (errorCount >= MAX_ERRORS) {
      es.close()
      if (onEnd) onEnd()
    }
    // 否则静默等待原生重连（约 3s 后自动重试）
  }
  return () => {
    normalEnd = true
    es.close()
  }
}
