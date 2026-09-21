import { useCallback, useEffect, useReducer, useRef, useState } from 'react'

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

// 把扁平 items 按 round 事件分组：第一组为 round 之前的条目（通常为空）。
function groupByRound(items) {
  const groups = []
  let cur = null
  for (const it of items) {
    if (it.kind === 'round') {
      // 同 n 的重复 round 合并：ASK 轮后后端 round_no 回退会重发同一 n 的 round 事件
      if (!cur || cur.n !== it.n) {
        cur = { n: it.n, items: [] }
        groups.push(cur)
      }
    } else if (cur) {
      cur.items.push(it)
    } else {
      if (!groups[0] || !groups[0].pre) groups.unshift({ pre: true, items: [] })
      groups[0].items.push(it)
    }
  }
  return groups
}

export default function App() {
  const [state, dispatch] = useReducer(reducer, initialState)
  const closeStreamRef = useRef(null)
  const streamRef = useRef(null)
  const stickRef = useRef(true)          // 是否贴底（决定新内容是否自动跟随）
  const [showBack, setShowBack] = useState(false)  // 是否显示"回到底部"按钮
  const [showSettings, setShowSettings] = useState(false)  // 设置弹窗
  const [showReview, setShowReview] = useState(false)  // 会话审查
  const [manualCollapsed, setManualCollapsed] = useState(() => new Set())  // 用户手动折叠的轮次 n

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
      dispatch({ type: 'task_start' })
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
  const lastRoundN = groups.length && !groups[groups.length - 1].pre
    ? groups[groups.length - 1].n
    : null
  const toggleRound = (n) => {
    setManualCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(n)) next.delete(n)
      else next.add(n)
      return next
    })
  }

  // 渲染单个轮次分组；最新轮默认展开，其余默认折叠（用户手动切换优先）
  const renderRoundGroup = (g, idx) => {
    if (g.pre) {
      return g.items.map((it) => renderItem(it))
    }
    const isLast = g.n === lastRoundN
    const manuallyOpen = manualCollapsed.has(g.n) ? false : isLast
    const collapsed = manualCollapsed.has(g.n) ? true : !isLast
    return (
      <section key={`r-${g.n}`} className={`round-group${collapsed ? ' closed' : ' open'}`}>
        <div className="round-header" onClick={() => toggleRound(g.n)}>
          <svg className="chev" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 6 15 12 9 18"></polyline></svg>
          Round {g.n}
          {isLast ? <span className="round-live" title="当前轮" /> : null}
          <span className="round-summary">{isLast ? '进行中' : '已完成'}</span>
        </div>
        <div className="round-body">
          {g.items.map((it) => renderItem(it))}
          {isLast && state.live ? (
            <StepPanel action={state.live.action} text={state.live.text} streaming />
          ) : null}
        </div>
      </section>
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
        onSettings={() => setShowSettings(true)}
        onReview={() => setShowReview(true)}
        totalTokens={state.totalTokens}
        onSwitchModel={async (name) => {
          await api.saveConfig({ active_profile: name })
          const d = await api.getConfig()
          dispatch({ type: 'config_update', config: d.config || {} })
        }}
      />

      <div className="main">
        <div className="topbar">
          <div className="crumb">ReAct Agent 对话</div>
          <div className={`status-pill status-${state.status}`}>{state.status}</div>
        </div>

        <main className="stream" ref={streamRef} onScroll={onScroll}>
          {state.error ? <div className="banner banner-error">✘ {state.error}</div> : null}

          {groups.map((g, i) => renderRoundGroup(g, i))}

          {state.lastResult ? (
            <div className="banner banner-done">
              ✔ 完成：{state.lastResult.status} · {state.lastResult.rounds} 轮
            </div>
          ) : null}
        </main>

        {showBack ? (
          <div className="back-to-bottom" title="回到底部" onClick={jumpToBottom}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
          </div>
        ) : null}

        <footer className="controls">
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
          <ChatInput disabled={busy || state.awaiting !== null} onSubmit={sendTask} />
        </footer>
      </div>

      {showReview ? (
        <ReviewModal
          sessionId={state.sessionId}
          onClose={() => setShowReview(false)}
        />
      ) : null}

      {showSettings ? (
        <SettingsModal
          allowOutside={state.allowOutside}
          onClose={() => setShowSettings(false)}
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
