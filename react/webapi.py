"""Web API：把 ReAct 循环暴露为 HTTP + SSE，供前端对话使用。

设计要点：
- **零新增依赖路线**：`starlette` / `uvicorn` 已随 `mcp` 装在 .venv 里（见 pyproject 已显式声明）；
  SSE 用 `StreamingResponse` 手写，不引 `sse-starlette`。
- **绝不阻塞事件循环**：`ReActLoop.run()` 是同步阻塞的（模型调用含 time.sleep 重试），
  因此每个任务跑在**独立工作线程**里；asyncio 侧只做队列轮询与推送。
- **人工交互桥接**：`gate`（c 继续 / s 纠偏 / q 中止）与 `ask`（ASK 回答）由
  `QueueControl` 阻塞在队列上，前端通过 POST 端点喂入指令。
- **会话内存态**：`dict[session_id -> Session]`，浏览器多标签互不干扰；刷新即失，
  需留存请用 `/api/save` 导出 Markdown。

启动：uvicorn react.webapi:app --port 8000
"""

from __future__ import annotations

import asyncio
import json
import queue
import sys
import time
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse,
                                 StreamingResponse)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .action import ACTION_NAMES
from .config import ConfigError, load_config, resolve_config_path
from .service import (AgentEvent, EventRenderer, QueueControl, ReactService,
                      resolve_work_dir)
from .model import OpenAIClient

BASE_DIR = Path(__file__).resolve().parent.parent
DIST_DIR = BASE_DIR / "web" / "dist"

_SSE_POLL = 0.02  # 出向队列轮询间隔（秒）


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
@dataclass
class Session:
    """一个浏览器会话：独立上下文、独立工作线程、两条队列。"""

    id: str
    context: object                      # SessionContext
    registry: object                     # ActionRegistry
    control: QueueControl                # 入向：gate/ask 指令
    out: queue.Queue = field(default_factory=queue.Queue)  # 出向：AgentEvent
    thread: threading.Thread | None = None
    done: threading.Event = field(default_factory=threading.Event)
    status: str = "idle"                 # idle / running / done / error / aborted
    last_result: dict | None = None
    created: str = ""                    # 会话创建时间（token 统计元数据）
    token_stats: list = field(default_factory=list)  # 会话级 token 调用明细


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, context, registry, control) -> Session:
        sid = uuid.uuid4().hex[:12]
        sess = Session(id=sid, context=context, registry=registry, control=control)
        with self._lock:
            self._sessions[sid] = sess
        return sess

    def get(self, sid: str) -> Session | None:
        with self._lock:
            return self._sessions.get(sid)

    def drop(self, sid: str) -> None:
        with self._lock:
            sess = self._sessions.pop(sid, None)
        if sess:
            sess.control.close()


MANAGER = SessionManager()


# ---------------------------------------------------------------------------
# 应用装配
# ---------------------------------------------------------------------------
def _load_cfg():
    return load_config(resolve_config_path(BASE_DIR))


def _service(cfg: dict):
    return ReactService(cfg, BASE_DIR)


def _binds(registry) -> dict:
    return {n: bool(registry.get(n).bound) for n in ACTION_NAMES}


def _run_task(sess: Session, task: str, cfg: dict, allow_exec: bool | None,
              max_rounds: int | None, skills_dir: str | None,
              gate_mode: str | None = None, work_dir: str | None = None,
              allow_outside_work_dir: bool | None = None) -> None:
    """工作线程主体：构造运行时 → 跑循环 → 收尾发 done/error 事件。"""
    try:
        svc = ReactService(cfg, BASE_DIR, Path(skills_dir) if skills_dir else None)
        runtime = svc.build_runtime(
            render=EventRenderer(sess.out.put),
            control=sess.control,
            allow_exec=allow_exec,
            max_rounds=max_rounds,
            context=sess.context,
            gate_mode=gate_mode,
            work_dir=work_dir,
            allow_outside_work_dir=allow_outside_work_dir,
        )
        if svc.work_dir_warning:
            sess.out.put(AgentEvent("warn", text=svc.work_dir_warning))
        sess.status = "running"
        runtime.loop.token_stats = sess.token_stats  # 会话级 token 统计接入循环
        result = runtime.loop.run(task)
        sess.last_result = {"status": result.status, "rounds": result.rounds,
                            "final_text": result.final_text}
        sess.status = "done" if result.status == "done" else result.status
        sess.out.put(AgentEvent("done", payload=sess.last_result))
    except Exception as e:  # noqa: BLE001 - 单会话异常不影响服务与其它会话
        sess.status = "error"
        sess.out.put(AgentEvent("error", text=f"{type(e).__name__}: {e}"))
    finally:
        _persist_stats(sess, task)
        sess.done.set()


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
async def api_session(request: Request) -> JSONResponse:
    """新建会话。"""
    try:
        cfg = _load_cfg().values
    except ConfigError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    svc = _service(cfg)
    registry = svc.build_registry()
    sess = MANAGER.create(svc.build_context(), registry, QueueControl())
    sess.created = time.strftime("%Y-%m-%d %H:%M:%S")
    # 阻塞等 gate 时推事件，前端据此显示「继续 / 纠偏 / 中止」步进条
    # reason 告诉前端「为什么停在这里」（步骤完成 / 发现缺陷 / 最终验收）
    sess.control.on_gate_wait = lambda action, reason="": sess.out.put(
        AgentEvent("gate", action=action, payload={"reason": reason})
    )
    return JSONResponse({
        "session_id": sess.id,
        "binds": _binds(registry),
        "warnings": list(getattr(registry, "warnings", [])),
        "config": {
            "model": cfg.get("model"),
            "max_rounds": cfg.get("max_rounds"),
            "shell": bool(cfg.get("enable_shell_exec")),
            "file_write": bool(cfg.get("enable_file_write")),
            "sandbox": bool(cfg.get("sandbox_shell")),
            "gate_mode": cfg.get("gate_mode", "plan"),
            "work_dir": str(resolve_work_dir(
                BASE_DIR, cfg.get("work_dir") or None,
                allow_outside=bool(cfg.get("allow_outside_work_dir", False)),
            )[0]),
            "allow_outside_work_dir": bool(cfg.get("allow_outside_work_dir", False)),
        },
    })


async def _json_body(request: Request) -> dict:
    """解析 JSON 请求体；空 body / 非法 JSON 一律返回空 dict（由各端点自行校验必填）。"""
    raw = await request.body()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


async def api_task(request: Request) -> JSONResponse:
    """发起任务：起工作线程跑 ReAct 循环，立即返回 202。"""
    body = await _json_body(request)
    sess = MANAGER.get(str(body.get("session_id", "")))
    if sess is None:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    task = str(body.get("task", "")).strip()
    if not task:
        return JSONResponse({"error": "task 不能为空"}, status_code=400)
    if sess.thread is not None and sess.thread.is_alive():
        return JSONResponse({"error": "该会话已有任务在运行"}, status_code=409)
    try:
        cfg = _load_cfg().values
    except ConfigError as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    sess.done.clear()
    sess.thread = threading.Thread(
        target=_run_task,
        args=(sess, task, cfg, body.get("allow_exec"), body.get("max_rounds"),
              body.get("skills_dir"), body.get("gate_mode"),
              body.get("work_dir"), body.get("allow_outside_work_dir")),
        daemon=True,
    )
    sess.thread.start()
    return JSONResponse({"ok": True, "session_id": sess.id}, status_code=202)


async def api_events(request: Request) -> StreamingResponse:
    """SSE：把工作线程产出的 AgentEvent 流式推给前端。"""
    sess = MANAGER.get(request.query_params.get("session_id", ""))
    if sess is None:
        return JSONResponse({"error": "会话不存在"}, status_code=404)

    async def gen():
        while True:
            try:
                ev: AgentEvent = sess.out.get_nowait()
            except queue.Empty:
                if sess.done.is_set():
                    break
                await asyncio.sleep(_SSE_POLL)
                continue
            payload = json.dumps(ev.to_dict(), ensure_ascii=False)
            yield f"data: {payload}\n\n"
            if ev.type in ("done", "error"):
                break
        # 兜底：done 已置位时把剩余事件排空
        while True:
            try:
                ev = sess.out.get_nowait()
            except queue.Empty:
                break
            yield f"data: {json.dumps(ev.to_dict(), ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


async def api_control(request: Request) -> JSONResponse:
    """步进控制：continue / steer / abort。"""
    body = await _json_body(request)
    sess = MANAGER.get(str(body.get("session_id", "")))
    if sess is None:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    cmd = str(body.get("cmd", "continue"))
    if cmd not in ("continue", "steer", "abort", "pause"):
        return JSONResponse({"error": f"未知指令: {cmd}"}, status_code=400)
    sess.control.submit(cmd, body.get("text"))
    return JSONResponse({"ok": True})


async def api_answer(request: Request) -> JSONResponse:
    """回答模型的 ASK 提问。"""
    body = await _json_body(request)
    sess = MANAGER.get(str(body.get("session_id", "")))
    if sess is None:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    sess.control.submit("answer", str(body.get("text", "")))
    return JSONResponse({"ok": True})


async def api_state(request: Request) -> JSONResponse:
    """当前会话状态与完整轨迹账本。"""
    sess = MANAGER.get(request.query_params.get("session_id", ""))
    if sess is None:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    return JSONResponse({
        "session_id": sess.id,
        "status": sess.status,
        "running": bool(sess.thread is not None and sess.thread.is_alive()),
        "last_result": sess.last_result,
        "binds": _binds(sess.registry),
        "messages": sess.context.messages,
    })


async def api_save(request: Request) -> JSONResponse:
    """导出会话纪要（Markdown）。"""
    sess = MANAGER.get(request.query_params.get("session_id", ""))
    if sess is None:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    return JSONResponse({"markdown": sess.context.export_markdown()})


def _persist_stats(sess: Session, task: str) -> None:
    """把会话 token 统计原子写入磁盘（重启后仍可查）。"""
    try:
        from react.token_stats import save_session_stats
        save_session_stats(BASE_DIR, sess.id, {
            "task": task,
            "status": sess.status,
            "rounds": (sess.last_result or {}).get("rounds"),
            "created": sess.created,
        }, sess.token_stats)
    except Exception:  # noqa: BLE001 - 统计写入失败不影响会话主流程
        pass


async def api_token_stats(request: Request) -> JSONResponse:
    """会话级 token 统计：?session_id=xxx 返回单会话明细+汇总；无参数返回全部会话汇总。"""
    from react.token_stats import load_session_stats, list_session_stats, summarize
    sid = request.query_params.get("session_id", "")
    if sid:
        sess = MANAGER.get(sid)
        data = load_session_stats(BASE_DIR, sid)
        if data is None and sess is None:
            return JSONResponse({"error": "会话不存在"}, status_code=404)
        if sess is not None:
            # 内存态会话优先（含尚未持久化的最新调用）
            data = data or {}
            data["session_id"] = sess.id
            data["status"] = sess.status
            data["task"] = data.get("task", "")
            data["calls"] = sess.token_stats
        data["summary"] = summarize(data.get("calls", []))
        return JSONResponse(data)
    return JSONResponse({"sessions": list_session_stats(BASE_DIR)})



async def api_review_current(request: Request) -> JSONResponse:
    """审查当前会话：对照 skill 检查合规性。"""
    body = await _json_body(request)
    sess = MANAGER.get(str(body.get("session_id", "")))
    if sess is None:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    md = sess.context.export_markdown()
    cfg = _load_cfg()
    try:
        report = _run_review(cfg, md, BASE_DIR)
    except Exception as e:
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}\n{traceback.format_exc()}"}, status_code=500)
    return JSONResponse({"report": report})


async def api_review_file(request: Request) -> JSONResponse:
    """审查上传的 markdown 文件。"""
    body = await _json_body(request)
    md = body.get("markdown", "")
    if not md.strip():
        return JSONResponse({"error": "markdown 内容为空"}, status_code=400)
    cfg = _load_cfg()
    try:
        report = _run_review(cfg, md, BASE_DIR)
    except Exception as e:
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}\n{traceback.format_exc()}"}, status_code=500)
    return JSONResponse({"report": report})


def _run_review(cfg: dict, markdown: str, base_dir: Path) -> str:
    """调模型对照 skill 分析会话。"""
    # 读取所有 SKILL.md
    skills_dir = base_dir / "skills"
    skill_texts = []
    if skills_dir.is_dir():
        for skill_dir in sorted(skills_dir.iterdir()):
            sk = skill_dir / "SKILL.md"
            if sk.is_file():
                skill_texts.append(f"### {skill_dir.name.upper()} SKILL.md\n{sk.read_text(encoding='utf-8')}")
    skills_block = "\n\n".join(skill_texts)

    client = OpenAIClient(
        cfg.get("base_url", ""), cfg.get("api_key", ""),
        cfg.get("model", ""), 300,
    )

    prompt = f"""你是 ReAct Agent 框架审查员。对照下面的 Skill 要求，审查这段会话记录是否合规。

## Skill 要求
{skills_block}

## 会话记录
{markdown[:50000]}

## 输出要求
按以下结构输出（中文，Markdown 格式）：

# 会话审查报告

## 一、Skill 要求摘要
（列出五阶段各自的核心约束，3-5 条）

## 二、会话流程概述
（从会话提取的步骤时间线，一句话一步）

## 三、逐项对照
| 检查项 | 要求 | 实际 | 结果 |
|---|---|---|---|
（至少 5 项，结果用 ✅/❌）

## 四、偏差项
1. **偏差描述**
   证据：...
   影响：...

## 五、Skill 优化建议
（哪条 SKILL.md 要怎么改）

## 六、结论
✅ 通过 / ❌ 不通过（N 项偏差）
"""
    resp = client.complete([
        {"role": "system", "content": "你是严格的框架审查员，如实判定，不讨好。"},
        {"role": "user", "content": prompt},
    ])
    return resp.text


def _active_profile_cfg(cfg: dict) -> dict:
    profiles = cfg.get("profiles") or []
    active = cfg.get("active_profile")
    for p in profiles:
        if p.get("name") == active:
            return p
    return {
        "base_url": cfg.get("base_url", ""),
        "api_key": cfg.get("api_key", ""),
        "model": cfg.get("model", ""),
    }

async def api_reset(request: Request) -> JSONResponse:
    """清空会话上下文（不销毁会话）。如果有任务在跑，先中止再清。"""
    body = await _json_body(request)
    sess = MANAGER.get(str(body.get("session_id", "")))
    if sess is None:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    # 有任务在跑：发 abort 唤醒阻塞中的线程，等其结束
    if sess.thread is not None and sess.thread.is_alive():
        sess.control.submit("abort")
        try:
            sess.done.wait(timeout=5.0)
        except Exception:
            pass
    sess.context.reset()
    sess.status = "idle"
    sess.last_result = None
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# 配置文件读写（页面上改 config.json，下个任务 _load_cfg 重读即生效）
# ---------------------------------------------------------------------------
#: 允许页面修改的字段白名单 + 类型。不在表中的字段不会被写入（防止污染）。
CONFIG_FIELDS: dict[str, type] = {
    "model": str, "base_url": str, "api_key": str,
    "plan_model": str, "plan_timeout_sec": int,
    "max_rounds": int, "step_timeout_sec": int, "show_reasoning": bool,
    "max_context_messages": int, "exec_timeout_sec": int,
    "enable_shell_exec": bool, "enable_file_write": bool,
    "sandbox_shell": bool, "sandbox_integrity_low": bool,
    "shell_backend": str,
    "gate_mode": str, "work_dir": str, "allow_outside_work_dir": bool,
    "active_profile": str,
}
CONFIG_REQUIRED = ("base_url", "api_key", "model")


async def api_config_get(request: Request) -> JSONResponse:
    """读当前配置文件（原样返回，不合并环境变量）。"""
    path = resolve_config_path(BASE_DIR)
    if not path.is_file():
        return JSONResponse({"error": f"配置文件不存在: {path.name}"}, status_code=404)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return JSONResponse({"error": f"配置文件损坏: {e}"}, status_code=500)
    return JSONResponse({"path": str(path), "config": data})


async def api_config_put(request: Request) -> JSONResponse:
    """合并写回配置文件：仅白名单字段按类型强转，保留未知字段。"""
    body = await _json_body(request)
    path = resolve_config_path(BASE_DIR)
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(current, dict):
            current = {}
    except json.JSONDecodeError:
        current = {}

    for key, typ in CONFIG_FIELDS.items():
        if key not in body:
            continue
        val = body[key]
        if typ is bool:
            current[key] = bool(val)
        elif typ is int:
            try:
                current[key] = int(val)
            except (TypeError, ValueError):
                return JSONResponse({"error": f"字段 {key} 应为整数"}, status_code=400)
        else:
            current[key] = str(val)

    # profiles 是数组，不在类型白名单里，单独保存
    if "profiles" in body:
        val = body["profiles"]
        if isinstance(val, list):
            current["profiles"] = val
        else:
            return JSONResponse({"error": "字段 profiles 应为数组"}, status_code=400)

    for key in CONFIG_REQUIRED:
        if not str(current.get(key, "")).strip():
            return JSONResponse({"error": f"{key} 不能为空"}, status_code=400)

    try:
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    except OSError as e:
        return JSONResponse({"error": f"写入失败: {e}"}, status_code=500)
    return JSONResponse({"ok": True, "path": str(path)})


async def spa_fallback(request: Request):
    """SPA 回退：未匹配的路径交给前端路由；未构建时给出明确指引。"""
    index = DIST_DIR / "index.html"
    if index.is_file():
        resp = FileResponse(index)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp
    return HTMLResponse(
        "<h3>前端尚未构建</h3><p>请先执行：</p><pre>cd web\nnpm install\nnpm run build</pre>"
        "<p>开发模式可改用：<code>npm run dev</code>（5173 端口，已配置代理到 8000）。</p>",
        status_code=503,
    )


def create_app() -> Starlette:
    routes = [
        Route("/api/session", api_session, methods=["POST"]),
        Route("/api/task", api_task, methods=["POST"]),
        Route("/api/events", api_events, methods=["GET"]),
        Route("/api/control", api_control, methods=["POST"]),
        Route("/api/answer", api_answer, methods=["POST"]),
        Route("/api/state", api_state, methods=["GET"]),
        Route("/api/save", api_save, methods=["GET"]),
        Route("/api/reset", api_reset, methods=["POST"]),
        Route("/api/config", api_config_get, methods=["GET"]),
        Route("/api/config", api_config_put, methods=["PUT", "POST"]),
        Route("/api/review/current", api_review_current, methods=["POST"]),
        Route("/api/review/file", api_review_file, methods=["POST"]),
        Route("/api/token_stats", api_token_stats, methods=["GET"]),
    ]
    if DIST_DIR.is_dir():
        routes.append(Mount("/assets", StaticFiles(directory=DIST_DIR / "assets"),
                            name="assets"))
    routes.append(Route("/{path:path}", spa_fallback))
    return Starlette(routes=routes)


app = create_app()


if __name__ == "__main__":
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
