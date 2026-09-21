// 会话状态机：用 useReducer 消费 SSE 事件流，不引额外状态库。
//
// 事件时序（后端保证）：token*(某步骤流式) → step(该步骤定稿) → gate(等待人工步进)
// 故「流式缓冲 live」在收到 step 事件时定稿并清空。

export const initialState = {
  sessionId: null,
  config: null, // { model, max_rounds, shell, file_write, sandbox, gate_mode }
  binds: null, // { think: bool, plan: bool, ... }
  status: 'idle', // idle | running | done | error | aborted
  items: [], // 已定稿条目
  live: null, // { action, text } 正在流式输出
  awaiting: null, // null | 'gate' | 'ask'
  gateMode: 'plan', // plan=计划批准一次 | step=每步骤拦一次 | auto=仅必须拦时
  gateReason: '', // 本次拦截的原因（后端下发）
  gateCount: 0, // 本次任务被打断次数
  workDir: '', // 工作目录（项目内子目录，空=项目根目录）
  allowOutside: false, // 是否允许 work_dir 指向项目根目录之外（需绝对路径）
  error: null,
  lastResult: null,
  totalTokens: 0,
  taskSeq: 0, // 任务序号：每发一个新任务 +1，用于隔离不同任务的 Round 分组
}

function pushItem(state, item) {
  // 条目归属当前任务（item 自带 task 时除外），刷新恢复的旧条目无 task，分组时归 0
  const tagged = { ...item, id: state.items.length + 1, task: item.task ?? state.taskSeq }
  return { ...state, items: [...state.items, tagged] }
}

export function reducer(state, action) {
  switch (action.type) {
    case 'session': {
      const mode = (action.config && action.config.gate_mode) || state.gateMode
      return {
        ...state,
        sessionId: action.sessionId,
        config: action.config || null,
        binds: action.binds || null,
        gateMode: mode,
        workDir: (action.config && action.config.work_dir) || state.workDir,
        allowOutside: action.config
          ? !!action.config.allow_outside_work_dir
          : state.allowOutside,
        error: null,
      }
    }

    case 'restore': {
      // 刷新后从本地快照重建界面（历史条目 / 状态 / 配置），不触发新的实时流
      const s = action.snapshot || {}
      return {
        ...state,
        sessionId: action.sessionId || state.sessionId,
        items: Array.isArray(s.items) ? s.items : state.items,
        status: s.status || 'idle',
        lastResult: s.lastResult || null,
        awaiting: s.awaiting || null,
        gateReason: s.gateReason || '',
        gateCount: s.gateCount || 0,
        config: s.config || state.config,
        binds: s.binds || state.binds,
        gateMode: s.gateMode || state.gateMode,
        workDir: s.workDir || '',
        allowOutside: !!s.allowOutside,
        error: null,
        live: null,
        taskSeq: s.taskSeq || 0,
      }
    }

    case 'config_update': {
      // 设置弹窗保存后调用：只更新 config/目录/闸门，不动 items/live/awaiting
      const cfg = action.config || {}
      return {
        ...state,
        config: cfg,
        gateMode: cfg.gate_mode || state.gateMode,
        workDir: cfg.work_dir ?? state.workDir,
        allowOutside: 'allow_outside_work_dir' in cfg
          ? !!cfg.allow_outside_work_dir
          : state.allowOutside,
      }
    }

    case 'gate_mode':
      return { ...state, gateMode: action.gateMode }

    case 'work_dir':
      return { ...state, workDir: action.workDir }

    case 'allow_outside':
      return { ...state, allowOutside: action.allowOutside }

    case 'task_start': {
      // 保留历史 items，新任务内容追加在旧记录之后，便于回看排查
      const taskSeq = state.taskSeq + 1
      const next = { ...state, taskSeq, live: null, awaiting: null, status: 'running',
                     error: null, lastResult: null, totalTokens: 0, gateCount: 0 }
      return action.task ? pushItem(next, { kind: 'task', text: action.task }) : next
    }

    case 'reset':
      return { ...initialState, sessionId: state.sessionId, config: state.config,
               binds: state.binds, gateMode: state.gateMode, workDir: state.workDir,
               allowOutside: state.allowOutside }

    case 'live_token': {
      const a = action.action
      const prev = state.live && state.live.action === a ? state.live.text : ''
      return { ...state, live: { action: a, text: prev + action.text } }
    }

    case 'event': {
      const e = action.event
      switch (e.type) {
        case 'round':
          return pushItem(state, { kind: 'round', n: e.round_no ?? e.payload?.round_no })

        case 'step': {
          const streamed = state.live && state.live.action === e.action ? state.live.text : null
          const usage = e.usage || null
          const addTokens = usage?.total || e.tokens || 0
          return pushItem({ ...state, live: null,
                            totalTokens: state.totalTokens + addTokens }, {
            kind: 'step',
            action: e.action,
            text: e.text,
            raw: e.raw,
            streamed,
            elapsed: e.elapsed_sec,
            tokens: e.tokens,
            usage,
            reasoning: e.reasoning,
          })
        }

        case 'solution':
          return pushItem(state, { kind: 'solution', text: e.text })

        case 'ask':
          return { ...pushItem(state, { kind: 'ask', text: e.text }), awaiting: 'ask' }

        case 'gate':
          return { ...state, awaiting: 'gate', gateReason: e.reason || '',
                   gateCount: state.gateCount + 1 }

        case 'info':
        case 'warn':
        case 'error':
        case 'success':
          return pushItem(state, { kind: 'notice', level: e.type, text: e.text })

        case 'done':
          return { ...state, status: 'done', awaiting: null, live: null,
                   lastResult: { status: e.status, rounds: e.rounds, final_text: e.final_text } }

        default:
          return state
      }
    }

    case 'consumed': // 人工指令已发出（步进 / 回答），收起控制面板
      return { ...state, awaiting: null }

    case 'status':
      return { ...state, status: action.status }

    case 'error':
      return { ...state, status: 'error', error: action.message, awaiting: null, live: null }

    default:
      return state
  }
}
