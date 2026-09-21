// 探针：用 Proxy 桩提供浏览器全局，加载 bundle 并捕捉初始化阶段报错。
const noop = () => {}
function makeEl() {
  const el = {
    click: noop, style: {}, dataset: {},
    setAttribute: noop, getAttribute: () => null, appendChild: noop,
    addEventListener: noop, removeEventListener: noop,
    querySelector: () => null, querySelectorAll: () => [],
    classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
    set href(_) {}, get href() { return '' },
    set download(_) {}, set innerHTML(_) {}, get innerHTML() { return '' },
    set textContent(_) {}, get textContent() { return '' },
  }
  return el
}
const docProxy = new Proxy({}, {
  get(_t, prop) {
    if (prop === 'getElementById') return () => makeEl()
    if (prop === 'createElement' || prop === 'createElementNS') return () => makeEl()
    if (prop === 'querySelector') return () => null
    if (prop === 'querySelectorAll') return () => []
    if (prop === 'body' || prop === 'documentElement') return makeEl()
    if (prop === 'addEventListener' || prop === 'removeEventListener') return noop
    if (prop === 'readyState') return 'complete'
    return noop
  },
})
globalThis.window = globalThis
globalThis.document = docProxy
globalThis.localStorage = { getItem: () => null, setItem: noop, removeItem: noop }
globalThis.URL = globalThis.URL || { createObjectURL: () => '', revokeObjectURL: noop }
globalThis.Blob = globalThis.Blob || class { constructor() {} }
globalThis.addEventListener = noop
globalThis.removeEventListener = noop
globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0)
globalThis.cancelAnimationFrame = noop

let target = process.argv[2] || './dist/assets/index-BhmWssuo.js'
if (!/^(file:|https?:|data:)/.test(target)) {
  target = 'file:///' + target.replace(/\\/g, '/')
}
try {
  await import(target)
  console.log('PROBE_OK: bundle evaluated (no TDZ) for', target)
} catch (e) {
  console.log('PROBE_ERR_TYPE:', e && e.constructor && e.constructor.name)
  console.log('PROBE_ERR_MSG:', e && e.message)
  console.log('PROBE_ERR_STACK:\n' + (e && e.stack))
}
