import { Fragment, useCallback, useEffect, useReducer, useRef, useState } from 'react'

import * as api from './api'
import AskPanel from './components/AskPanel'
import ChatInput from './components/ChatInput'
import GateBar from './components/GateBar'
import Sidebar from './components/Sidebar'
import SettingsModal from './components/SettingsModal'
import ReviewModal from './components/ReviewModal'
import SolutionCard from './components/SolutionCard'
import StepPanel from './components/StepPanel'
import { initialState, reducer } from './store'

// 本地持久化：刷新后复用同一后端会话并把界面历史重建回来。
// 服务端进程存活期间会话上下文（context.messages）不丢，故对话可继续；
// 会话若已不在（如服务重启），则回退新建。
const LS_KEY = 'react-agent:session'
function loadSession() {
  try {
    const raw = localStorage.getItem(LS_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}
function saveSession(data) {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(data))
  } catch {
    // 忽略写入失败（隐私模式 / 配额超限）：刷新持久化降级为不可用，不影响功能
  }
}

// 五阶段固定顺序与配色（与 styles.css 变量 / Sidebar 槽位一致）
const STAGES = [
  { key: 'think', label: 'THINK', color: 'var(--think)' },
  { key: 'plan', label: 'PLAN', color: 'var(--plan)' },
  { key: 'act', label: 'ACT', color: 'var(--act)' },
  { key: 'observe', label: 'OBSERVE', color: 'var(--observe)' },
  { key: 'verify', label: 'VERIFY', color: 'var(--verify)' },
]

const EXAMPLE_TASKS = [
  { title: '解析代码框架', desc: '分析一个项目目录的模块结构与调用链' },
  { title: '修复前端 Bug', desc: '定位报错原因，修改并验证构建' },
  { title: '生成需求文档', desc: '根据描述输出结构化需求与验收标准' },
]

// 把扁平 items 按「任务 + round」分组：不同任务即使 round_no 都从 1 开始也不合并。
// kind === 'task' 是任务头条目，不进任何组，只提供该任务的标题。
function groupByRound(items) {
  const groups = []
  let cur = null
  const titles = {}
  for (const it of items) {
    const t = it.task ?? 0
    if (it.kind === 'task') {
      titles[t] = it.text
      continue
    }
    if (it.kind === 'round') {
      // 同一任务内同 n 的重复 round 才合并：ASK 轮后后端 round_no 回退会重发同一 n
      if (!cur || cur.task !== t || cur.n !== it.n) {
        cur = { task: t, n: it.n, items: [] }
        groups.push(cur)
      }
    } else if (cur) {
      cur.items.push(it)
    } else {
      if (!groups[0] || !groups[0].pre) groups.unshift({ pre: true, task: t, items: [] })
      groups[0].items.push(it)
    }
  }
  for (const g of groups) g.title = titles[g.task] || ''
  return groups
}

export default function App() {
  const [state, dispatch] = useReducer(reducer, initialState)
  const closeStreamRef = useRef(null)
  const streamRef = useRef(null)
  const stickRef = useRef(true)          // 是否贴底（决定新内容是否自动跟随）
  const [showBack, setShowBack] = useState(false)  // 是否显示"回到底部"按钮
  const [view, setView] = useState('chat')  // chat | settings | review
  const [manualCollapsed, setManualCollapsed] = useState(() => new Set())  // 手动覆盖默认展开态的轮次键（task-n）

  // 打开事件流：仅贴底自动跟随由 onScroll 处理；统一在此封装便于复用
  const connectStream = useCallback((sessionId) => {
    closeStreamRef.current?.()
    closeStreamRef.current = api.openEventStream(
      sessionId,
      (ev) => {
        if (ev.type === 'token') {
          dispatch({ type: 'live_token', action: ev.action, text: ev.text })
        } else {
          dispatch({ type: 'event', event: ev })
        }
      },
      () => {},
    )
  }, [])

  // 启动：优先复用本地存储的会话（刷新不丢内容），否则新建
  useEffect(() => {
    let cancelled = false
    const saved = loadSession()
    const newSession = () => {
      if (cancelled) return
      api
        .createSession()
        .then((data) => {
          if (cancelled) return
          dispatch({ type: 'session', sessionId: data.session_id,
                     config: data.config, binds: data.binds })
        })
        .catch((e) => dispatch({ type: 'error', message: `建会话失败：${e.message}` }))
    }

    if (saved && saved.sessionId) {
      api
        .getState(saved.sessionId)
        .then(() => {
          if (cancelled) return
          // 会话仍在：复用并重建界面历史
          dispatch({ type: 'restore', sessionId: saved.sessionId,
                     snapshot: saved.snapshot || {} })
          if (saved.snapshot && saved.snapshot.status === 'running') {
            connectStream(saved.sessionId)
          }
        })
        .catch(() => newSession()) // 会话已不在（如服务重启）→ 新建
    } else {
      newSession()
    }

    return () => {
      cancelled = true
      closeStreamRef.current?.()
    }
  }, [connectStream])

  // 持久化：会话 id + 界面快照写入本地，刷新后用于重建
  useEffect(() => {
    if (!state.sessionId) return
    saveSession({
      sessionId: state.sessionId,
      snapshot: {
        items: state.items,
        status: state.status,
        lastResult: state.lastResult,
        awaiting: state.awaiting,
        gateReason: state.gateReason,
        gateCount: state.gateCount,
        config: state.config,
        binds: state.binds,
        gateMode: state.gateMode,
        workDir: state.workDir,
        allowOutside: state.allowOutside,
        taskSeq: state.taskSeq,
      },
    })
  }, [state.sessionId, state.items, state.status, state.lastResult, state.awaiting,
      state.gateReason, state.gateCount, state.config, state.binds,
      state.gateMode, state.workDir, state.allowOutside])

  // 流式更新：仅贴底时自动跟随（瞬间定位，不平滑动画堆叠）
  useEffect(() => {
    if (!stickRef.current) return
    const el = streamRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [state.items, state.live, state.awaiting])

  const onScroll = () => {
    const el = streamRef.current
    if (!el) return
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    stickRef.current = nearBottom
    setShowBack(!nearBottom)
  }

  const jumpToBottom = () => {
    const el = streamRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
    stickRef.current = true
    setShowBack(false)
  }

  // 流式更新：仅贴底自动跟随由 onScroll 处理
  const sendTask = useCallback(
    async (task) => {
      if (!state.sessionId) return
      dispatch({ type: 'task_start', task })
      setManualCollapsed(new Set())
      stickRef.current = true
      setShowBack(false)
      try {
        await api.postTask(state.sessionId, task, {
          gate_mode: state.gateMode,
          ...(state.workDir ? { work_dir: state.workDir } : {}),
          ...(state.allowOutside
            ? { allow_outside_work_dir: true }
            : {}),
        })
      } catch (e) {
        dispatch({ type: 'error', message: e.message })
        return
      }
      closeStreamRef.current?.()
      connectStream(state.sessionId)
    },
    [state.sessionId, state.gateMode, state.workDir, state.allowOutside],
  )

  const doContinue = () => {
    api.postControl(state.sessionId, 'continue').catch((e) =>
      dispatch({ type: 'error', message: e.message }))
    dispatch({ type: 'consumed' })
  }
  const doSteer = (text) => {
    api.postControl(state.sessionId, 'steer', text).catch((e) =>
      dispatch({ type: 'error', message: e.message }))
    dispatch({ type: 'consumed' })
  }
  const doPause = () => {
    api.postControl(state.sessionId, 'pause').catch((e) =>
      dispatch({ type: 'error', message: e.message }))
  }
  const doAbort = () => {
    api.postControl(state.sessionId, 'abort').catch(() => {})
    dispatch({ type: 'status', status: 'aborted' })
    dispatch({ type: 'consumed' })
  }
  const doAnswer = (text) => {
    api.postAnswer(state.sessionId, text).catch((e) =>
      dispatch({ type: 'error', message: e.message }))
    dispatch({ type: 'consumed' })
  }
  const doSkip = () => {
    api.postControl(state.sessionId, 'abort').catch(() => {})
    dispatch({ type: 'status', status: 'aborted' })
    dispatch({ type: 'consumed' })
  }

  const doReset = async () => {
    if (!state.sessionId) return
    closeStreamRef.current?.()
    await api.postReset(state.sessionId).catch(() => {})
    dispatch({ type: 'reset' })
    setManualCollapsed(new Set())
  }

  const doSave = async () => {
    if (!state.sessionId) return
    try {
      const data = await api.getSave(state.sessionId)
      const blob = new Blob([data.markdown || ''], { type: 'text/markdown;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `session_${state.sessionId}.md`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      dispatch({ type: 'error', message: e.message })
    }
  }

  const busy = state.status === 'running'
  const groups = groupByRound(state.items)
  // 最后一个非 pre 组的索引（可能跨多个任务，不能只按 round_no 判断）
  let lastGroupIdx = -1
  for (let i = groups.length - 1; i >= 0; i--) {
    if (!groups[i].pre) { lastGroupIdx = i; break }
  }
  const gkey = (g) => `${g.task}-${g.n}`
  const toggleRound = (key) => {
    setManualCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  // 渲染单个轮次分组；最新轮默认展开，其余默认折叠（用户手动切换优先）
  const renderRoundGroup = (g, idx) => {
    if (g.pre) {
      return g.items.map((it) => renderItem(it))
    }
    const isLast = idx === lastGroupIdx
    // 默认仅最新轮展开；manualCollapsed 记录「与默认相反」的手动覆盖，
    // 故旧轮可手动展开、最新轮可手动折叠
    const defaultCollapsed = !isLast
    const collapsed = manualCollapsed.has(gkey(g)) ? !defaultCollapsed : defaultCollapsed
    // 该任务的第一个轮次组：上方插任务头
    const firstOfTask = groups.findIndex((x) => !x.pre && x.task === g.task) === idx

    // 该轮已定稿的阶段 + 当前流式阶段，推导 flow 进度条状态
    const doneStages = new Set(
      g.items.filter((it) => it.kind === 'step').map((it) => it.action),
    )
    const activeStage = isLast && state.live ? state.live.action : null
    const roundElapsed = g.items
      .filter((it) => it.kind === 'step' && typeof it.elapsed === 'number')
      .reduce((sum, it) => sum + it.elapsed, 0)

    return (
      <Fragment key={`r-${g.task}-${g.n}`}>
      {firstOfTask ? (
        <div className="task-head">
          <span className="task-badge">任务 {g.task}</span>
          <span className="task-title" title={g.title}>{g.title || '未命名任务'}</span>
          <span className="task-rule" />
        </div>
      ) : null}
      <section className={`round-group${collapsed ? ' closed' : ' open'}`}>
        <div className="round-header" onClick={() => toggleRound(gkey(g))}>
          <svg className="chev" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 6 15 12 9 18"></polyline></svg>
          <span className="round-no">Round {g.n}</span>
          <div className="flow">
            {STAGES.map((s, i) => {
              const isActive = activeStage === s.key
              const isDone = !isActive && doneStages.has(s.key)
              const cls = isActive ? 'flow-step active' : isDone ? 'flow-step done' : 'flow-step'
              return (
                <span key={s.key} style={{ display: 'contents' }}>
                  <span className={cls} style={isActive || isDone ? { color: s.color } : undefined}>
                    <span
                      className="fd"
                      style={isActive || isDone ? { background: s.color, color: '#fff' } : undefined}
                    >
                      {isActive ? '▶' : isDone ? '✓' : i + 1}
                    </span>
                    {s.label}
                  </span>
                  {i < STAGES.length - 1 ? (
                    <span className={`flow-line${isDone ? ' done' : ''}`} />
                  ) : null}
                </span>
              )
            })}
          </div>
          {isLast && busy ? <span className="round-live" title="当前轮" /> : null}
          <span className="round-summary">
            {isLast && busy ? '进行中…' : `${roundElapsed.toFixed(1)}s`}
          </span>
        </div>
        <div className="round-body">
          {g.items.map((it) => renderItem(it))}
          {isLast && state.live ? (
            <StepPanel action={state.live.action} text={state.live.text} streaming />
          ) : null}
        </div>
      </section>
      </Fragment>
    )
  }

  const renderItem = (it) => {
    if (it.kind === 'step')
      return (
        <StepPanel
          key={it.id}
          action={it.action}
          text={it.text}
          elapsed={it.elapsed}
          tokens={it.tokens}
          usage={it.usage}
          reasoning={it.reasoning}
        />
      )
    if (it.kind === 'solution') return <SolutionCard key={it.id} text={it.text} />
    if (it.kind === 'ask')
      return (
        <div key={it.id} className="ask-record">
          <div className="ask-record-title">? ASK</div>
          <div>{it.text}</div>
        </div>
      )
    return (
      <div key={it.id} className={`notice notice-${it.level}`}>
        {it.text}
      </div>
    )
  }

  return (
    <div className="app">
      <Sidebar
        config={state.config}
        binds={state.binds}
        status={state.status}
        gateMode={state.gateMode}
        onGateMode={(mode) => dispatch({ type: 'gate_mode', gateMode: mode })}
        workDir={state.workDir}
        onWorkDir={(dir) => dispatch({ type: 'work_dir', workDir: dir })}
        allowOutside={state.allowOutside}
        onAllowOutside={(v) => dispatch({ type: 'allow_outside', allowOutside: v })}
        onPause={doPause}
        onAbort={doAbort}
        onReset={doReset}
        onSave={doSave}
        onSettings={() => setView('settings')}
        onReview={() => setView('review')}
        totalTokens={state.totalTokens}
        onSwitchModel={async (name) => {
          await api.saveConfig({ active_profile: name })
          const d = await api.getConfig()
          dispatch({ type: 'config_update', config: d.config || {} })
        }}
      />

      <div className="main">
        <div className="topbar">
          <div className="view-tabs">
            <button className={view === 'chat' ? 'tab on' : 'tab'} onClick={() => setView('chat')}>对话</button>
            <button className={view === 'settings' ? 'tab on' : 'tab'} onClick={() => setView('settings')}>设置</button>
            <button className={view === 'review' ? 'tab on' : 'tab'} onClick={() => setView('review')}>审查</button>
          </div>
          <div className={`status-pill status-${state.status}`}>{state.status}</div>
        </div>

        <main className={`stream${view === 'chat' ? '' : ' pane-hidden'}`} ref={streamRef} onScroll={onScroll}>
            {state.error ? <div className="banner banner-error">✘ {state.error}</div> : null}
            {state.items.length === 0 && !busy ? (
              <div className="empty">
                <div className="empty-logo">R</div>
                <h2>ReAct Agent</h2>
                <p>显式五阶段协议：思考 → 计划 → 执行 → 观察 → 验收</p>
                <div className="examples">
                  {EXAMPLE_TASKS.map((ex) => (
                    <button
                      key={ex.title}
                      className="example"
                      onClick={() => sendTask(ex.title)}
                    >
                      <b>{ex.title}</b>
                      <span>{ex.desc}</span>
                    </button>
                  ))}
                </div>
              </div>
            ) : null}
            {groups.map((g, i) => renderRoundGroup(g, i))}
            {state.lastResult ? (
              <div className="banner banner-done">
                ✔ 完成：{state.lastResult.status} · {state.lastResult.rounds} 轮
              </div>
            ) : null}
        </main>

        <div className={view === 'settings' ? 'host-pane' : 'host-pane pane-hidden'}>
          <SettingsModal inline allowOutside={state.allowOutside} onSaved={() => {}} onClose={() => {}} />
        </div>

        <div className={view === 'review' ? 'host-pane' : 'host-pane pane-hidden'}>
          <ReviewModal inline sessionId={state.sessionId} onClose={() => {}} />
        </div>

        {showBack ? (
          <div className="back-to-bottom" title="回到底部" onClick={jumpToBottom}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
          </div>
        ) : null}

        <footer className={`controls${view === 'chat' ? '' : ' pane-hidden'}`}>
          {state.awaiting === 'gate' ? (
            <GateBar
              action={state.items.filter((i) => i.kind === 'step').slice(-1)[0]?.action}
              reason={state.gateReason}
              count={state.gateCount}
              onContinue={doContinue}
              onSteer={doSteer}
              onAbort={doAbort}
            />
          ) : null}
          {state.awaiting === 'ask' ? (
            <AskPanel
              question={state.items.filter((i) => i.kind === 'ask').slice(-1)[0]?.text || ''}
              onAnswer={doAnswer}
              onSkip={doSkip}
            />
          ) : null}
          <ChatInput disabled={busy || state.awaiting !== null} onSubmit={sendTask} gateMode={state.gateMode} />
        </footer>
      </div>

      {false ? (
        <SettingsModal
          allowOutside={state.allowOutside}
          onClose={() => {}}
          onSaved={() => {
            // 配置已写回文件：只更新 config 相关字段，不动 items/live/awaiting
            api.getConfig().then((d) => {
              dispatch({ type: 'config_update', config: d.config || {} })
            }).catch(() => {})
          }}
        />
      ) : null}
    </div>
  )
}
