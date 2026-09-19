import { useCallback, useEffect, useReducer, useRef } from 'react'

import * as api from './api'
import AskPanel from './components/AskPanel'
import ChatInput from './components/ChatInput'
import GateBar from './components/GateBar'
import RoundDivider from './components/RoundDivider'
import StatusBar from './components/StatusBar'
import SolutionCard from './components/SolutionCard'
import StepPanel from './components/StepPanel'
import { initialState, reducer } from './store'

export default function App() {
  const [state, dispatch] = useReducer(reducer, initialState)
  const closeStreamRef = useRef(null)
  const bottomRef = useRef(null)

  // 启动即建会话
  useEffect(() => {
    let cancelled = false
    api
      .createSession()
      .then((data) => {
        if (cancelled) return
        dispatch({ type: 'session', sessionId: data.session_id,
                   config: data.config, binds: data.binds })
      })
      .catch((e) => dispatch({ type: 'error', message: `建会话失败：${e.message}` }))
    return () => {
      cancelled = true
      closeStreamRef.current?.()
    }
  }, [])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [state.items, state.live, state.awaiting])

  const sendTask = useCallback(
    async (task) => {
      if (!state.sessionId) return
      dispatch({ type: 'task_start' })
      try {
        // gate_mode 随任务下发：step=每步骤拦一次，auto=仅在需人决定时拦
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
      // 打开事件流；流结束后关闭
      closeStreamRef.current?.()
      closeStreamRef.current = api.openEventStream(
        state.sessionId,
        (ev) => {
          if (ev.type === 'token') {
            dispatch({ type: 'live_token', action: ev.action, text: ev.text })
          } else {
            dispatch({ type: 'event', event: ev })
          }
        },
        () => {},
      )
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

  return (
    <div className="app">
      <StatusBar
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
      />

      <main className="stream">
        {state.error ? <div className="banner banner-error">✘ {state.error}</div> : null}

        {state.items.map((it) => {
          if (it.kind === 'round') return <RoundDivider key={it.id} n={it.n} />
          if (it.kind === 'step')
            return (
              <StepPanel
                key={it.id}
                action={it.action}
                text={it.text}
                elapsed={it.elapsed}
                tokens={it.tokens}
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
        })}

        {state.live ? (
          <StepPanel action={state.live.action} text={state.live.text} streaming />
        ) : null}

        {state.lastResult ? (
          <div className="banner banner-done">
            ✔ 完成：{state.lastResult.status} · {state.lastResult.rounds} 轮
          </div>
        ) : null}

        <div ref={bottomRef} />
      </main>

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
  )
}
