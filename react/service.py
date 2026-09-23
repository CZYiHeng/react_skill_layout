"""service 层：把 ReActLoop 与「渲染 / 人工交互」解耦，供 CLI / MCP / Web 三方复用。

设计要点：
- `ReActLoop` 只依赖 `Renderer` 协议与 `gate` / `ask` 两个回调；本层提供这些协作方的
  不同实现，让**同一个循环**既能驱动终端分色面板、也能推 SSE 事件、也能静默跑完收 JSON。
- 此前 `mcp_server.py` 手写 `_NullRenderer`（7 个空方法，且与残缺协议脱节），协议一改就
  静默漏实现；现在统一由 `NullRenderer` 承担，协议完整性由 `Renderer` 协议保证。
- 构造序列（registry/context/model/executor/loop）此前在 `main.py` 与 `mcp_server.py`
  各写一份，现收拢到 `ReactService`，避免两处漂移。

约定：`ReActLoop` 本身**不做任何 I/O 决策**——输出交给 Renderer，人工介入交给 ControlChannel。
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .action import ActionRegistry
from .context import SessionContext
from .executor import Executor, LocalExecutor
from .loop import ReActLoop
from .model import OpenAIClient
from .render import Renderer

# gate 返回值语义（与 loop.py 保持一致）
GATE_CONTINUE = "continue"
GATE_ABORT = "abort"
GATE_STEER = "steer"
GATE_PAUSE = "pause"      # 运行中按下暂停：在下一个阶段边界停下
GATE_ANSWER = "answer"

AUTO_ANSWER = "（自动回答：继续）"


# ---------------------------------------------------------------------------
# 事件模型
# ---------------------------------------------------------------------------
@dataclass
class AgentEvent:
    """结构化事件：Web(SSE) / 日志 / 测试断言的统一载体。"""

    type: str                       # round|step|token|ask|exec|info|warn|error|success|done
    action: str = ""                # think/plan/act/observe/verify
    text: str = ""
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"type": self.type, "action": self.action, "text": self.text}
        d.update(self.payload)
        return d


EventSink = Callable[[AgentEvent], None]


# ---------------------------------------------------------------------------
# Renderer 实现
# ---------------------------------------------------------------------------
class EventRenderer:
    """把渲染调用转成 AgentEvent 推给 sink；`on_token` 返回逐 token 推流闭包。"""

    def __init__(self, sink: EventSink):
        self._sink = sink

    def _emit(self, type_: str, action: str = "", text: str = "", **payload) -> None:
        self._sink(AgentEvent(type=type_, action=action, text=text, payload=payload))

    def on_token(self, action: str) -> Callable[[str], None] | None:
        return lambda t: self._emit("token", action, t)

    def round_banner(self, round_no: int) -> None:
        self._emit("round", round_no=round_no)

    def show(self, action: str, parsed: str, raw: str,
             elapsed_sec: float, tokens: int, reasoning: str = "",
             usage: dict | None = None) -> None:
        self._emit("step", action, parsed,
                   raw=raw, elapsed_sec=elapsed_sec, tokens=tokens, reasoning=reasoning,
                   usage=usage)

    def ask_question(self, question: str) -> None:
        self._emit("ask", text=question)

    def solution(self, text: str) -> None:
        self._emit("solution", "act", text)

    def info(self, text: str) -> None:
        self._emit("info", text=text)

    def warn(self, text: str) -> None:
        self._emit("warn", text=text)

    def error(self, text: str) -> None:
        self._emit("error", text=text)

    def success(self, text: str) -> None:
        self._emit("success", text=text)


class NullRenderer:
    """静默渲染器：什么都不输出（MCP / 无头场景）。可选收集事件供调用方取用。"""

    def __init__(self, collect: bool = False):
        self.events: list[AgentEvent] = []
        self._collect = collect
        if collect:
            self._inner = EventRenderer(self.events.append)
        else:
            self._inner = None

    def on_token(self, action: str) -> Callable[[str], None] | None:
        return self._inner.on_token(action) if self._inner else None

    def round_banner(self, round_no: int) -> None:
        if self._inner:
            self._inner.round_banner(round_no)

    def show(self, action: str, parsed: str, raw: str,
             elapsed_sec: float, tokens: int, reasoning: str = "",
             usage: dict | None = None) -> None:
        if self._inner:
            self._inner.show(action, parsed, raw, elapsed_sec, tokens, reasoning, usage)

    def ask_question(self, question: str) -> None:
        if self._inner:
            self._inner.ask_question(question)

    def solution(self, text: str) -> None:
        if self._inner:
            self._inner.solution(text)

    def info(self, text: str) -> None:
        if self._inner:
            self._inner.info(text)

    def warn(self, text: str) -> None:
        if self._inner:
            self._inner.warn(text)

    def error(self, text: str) -> None:
        if self._inner:
            self._inner.error(text)

    def success(self, text: str) -> None:
        if self._inner:
            self._inner.success(text)


# ---------------------------------------------------------------------------
# 人工交互通道
# ---------------------------------------------------------------------------
class ControlChannel:
    """gate / ask 的统一抽象。默认行为 = 全自动（无人工介入）。"""

    def wait_gate(self, action: str, reason: str = "") -> tuple[str, str | None]:
        return (GATE_CONTINUE, None)

    def wait_answer(self, question: str) -> str | None:
        return AUTO_ANSWER

    def peek(self) -> tuple[str, str | None] | None:
        """非阻塞查看是否有待处理的人工指令（暂停/纠偏/中止）；无则返回 None。"""
        return None

    def as_gate(self) -> Callable[..., tuple[str, str | None]]:
        return self.wait_gate

    def as_interrupt(self) -> Callable[[], tuple[str, str | None] | None]:
        return self.peek

    def as_ask(self) -> Callable[[str], str | None]:
        return self.wait_answer


class AutoControl(ControlChannel):
    """无头自动：每步直接继续，ASK 用占位回答（MCP / --smoke 场景）。"""


class CliControl(ControlChannel):
    """终端 REPL：步进控制走 input()，ASK 走 input()。"""

    def __init__(self, console=None):
        self._console = console

    def _say(self, text: str) -> None:
        if self._console is not None:
            self._console.print(text)

    def wait_gate(self, action: str, reason: str = "") -> tuple[str, str | None]:
        self._say(f"[dim][c]继续 · s <纠偏> · q 中止[/dim]"
                  + (f"  [dim]— {reason}[/dim]" if reason else ""))
        while True:
            try:
                ans = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                if self._console is not None:
                    self._console.print()
                return (GATE_ABORT, None)
            if ans in ("", "c"):
                return (GATE_CONTINUE, None)
            if ans == "q":
                return (GATE_ABORT, None)
            if ans.startswith("s ") and len(ans) > 2:
                return (GATE_STEER, ans[2:].strip())
            self._say("[dim]输入 c / s <纠偏> / q[/dim]")

    def wait_answer(self, question: str) -> str | None:
        try:
            return input("回答> ").strip()
        except (EOFError, KeyboardInterrupt):
            if self._console is not None:
                self._console.print()
            return None


class QueueControl(ControlChannel):
    """Web 场景：gate / ask 阻塞在队列上，由 HTTP 端点（POST）喂入指令。

    工作线程在 `wait_gate` / `wait_answer` 里阻塞取队列，绝不占用 asyncio 事件循环。
    `close()` 用于会话关闭时唤醒并中止阻塞中的线程，避免僵尸线程。
    """

    def __init__(self, timeout_sec: float = 600.0,
                 on_gate_wait: Callable[..., None] | None = None):
        self._q: queue.Queue = queue.Queue()
        self._timeout = timeout_sec
        self._closed = threading.Event()
        self._lock = threading.Lock()
        # 进入阻塞等待前回调：Web 侧借此推 "gate" 事件，前端才知道该显示步进控制条
        # 签名 (action, reason) —— reason 告诉前端「为什么停在这里」
        self.on_gate_wait = on_gate_wait

    def submit(self, cmd: str, text: str | None = None) -> None:
        """外部（HTTP 端点）喂入一条指令：continue / steer / abort / pause / answer。"""
        self._q.put((cmd, text))

    def peek(self) -> tuple[str, str | None] | None:
        """非阻塞取一条待处理指令，供「随时插手」在阶段边界检查。

        运行中的 continue 没有意义（没人在等），直接丢弃。
        """
        try:
            cmd, text = self._q.get_nowait()
        except queue.Empty:
            return None
        if cmd == GATE_CONTINUE:
            return None
        if cmd == GATE_STEER:
            return (GATE_STEER, text or "")
        if cmd == GATE_ABORT:
            return (GATE_ABORT, None)
        if cmd == GATE_PAUSE:
            return (GATE_PAUSE, None)
        return None

    def close(self) -> None:
        self._closed.set()
        self._q.put((GATE_ABORT, None))  # 唤醒可能阻塞中的工作线程

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def _get(self) -> tuple[str, str | None]:
        try:
            return self._q.get(timeout=self._timeout)
        except queue.Empty:
            return (GATE_ABORT, None)  # 超时按中止处理，避免线程悬挂

    def wait_gate(self, action: str, reason: str = "") -> tuple[str, str | None]:
        if self._closed.is_set():
            return (GATE_ABORT, None)
        if self.on_gate_wait is not None:
            self.on_gate_wait(action, reason)
        cmd, text = self._get()
        if cmd == GATE_STEER:
            return (GATE_STEER, text or "")
        if cmd == GATE_ABORT:
            return (GATE_ABORT, None)
        return (GATE_CONTINUE, None)

    def wait_answer(self, question: str) -> str | None:
        if self._closed.is_set():
            return None
        cmd, text = self._get()
        if cmd == GATE_ABORT:
            return None
        return text or ""


# ---------------------------------------------------------------------------
# 运行时构造
# ---------------------------------------------------------------------------
@dataclass
class Runtime:
    """一次运行所需的全部对象。"""

    loop: ReActLoop
    context: SessionContext
    registry: ActionRegistry
    executor: Executor | None = None
    control: ControlChannel | None = None


def resolve_work_dir(base_dir: Path, raw: str | Path | None,
                     allow_outside: bool = False) -> tuple[Path, str | None]:
    """解析工作目录（写盘 / 执行命令的根）。

    默认安全边界：只允许项目根目录内的子目录（`allow_outside=False`）。
    `allow_outside=True` 时放行**绝对路径**指向的项目外目录——这是显式授权，
    不走这个开关的绝对路径一律回退。目录不存在时同样回退，绝不代为创建。
    """
    base = Path(base_dir).resolve()
    if not raw:
        return base, None
    p = Path(raw)
    target = (p if p.is_absolute() else (base / p)).resolve()
    # 越界是安全相关，告警优先级高于「目录不存在」
    if not (allow_outside and p.is_absolute()):
        try:
            target.relative_to(base)
        except ValueError:
            return base, (f"工作目录 {target} 越出项目根目录 {base}，已回退为项目根目录"
                          "（如需指向项目外，请显式开启 allow_outside_work_dir）")
    if not target.is_dir():
        return base, f"工作目录 {target} 不存在，已回退为项目根目录"
    return target, None


class ReactService:
    """三方共用的运行时构造（收拢此前重复两份的构造序列）。"""

    def __init__(self, cfg: dict, base_dir: Path, skills_dir: Path | None = None):
        self.cfg = cfg
        self.base_dir = Path(base_dir)
        self.skills_dir = Path(skills_dir) if skills_dir else self.base_dir / "skills"
        #: 最近一次解析出的工作目录与可能的告警（越界/不存在时回退）
        self.work_dir: Path = self.base_dir
        self.work_dir_warning: str | None = None

    def build_registry(self) -> ActionRegistry:
        registry = ActionRegistry()
        registry.load(self.skills_dir)
        return registry

    def _build_env_info(self) -> str:
        """构造运行环境信息文本，拼进每步 system 提示，避免模型在真空中默认 Linux。"""
        import platform
        import shutil
        lines = [
            f"- 操作系统：{platform.system()} {platform.release()}",
        ]
        backend = str(self.cfg.get("shell_backend", "cmd"))
        bash_path = ""
        if backend == "auto":
            from .executor import _detect_bash
            bash_path = _detect_bash()
            backend = "bash" if bash_path else "cmd"
        elif backend == "bash":
            bash_path = self.cfg.get("bash_path", "") or r"C:\Program Files\Git\bin\bash.exe"
        if backend == "bash":
            lines.append(f"- shell 后端：bash（Git Bash，{bash_path}）")
            lines.append("- 写命令时用 bash 语法（mkdir -p、&&、管道等），但调用 Python 用 `python` 不是 `python3`")
        else:
            lines.append("- shell 后端：cmd.exe（Windows 命令行）")
            lines.append("- 写命令时用 cmd 语法：用 `&&` 连接命令，不要用 `set +e`、`nohup`、`find`、heredoc；调用 Python 用 `python` 不是 `python3`")
        cwd = self.cfg.get("work_dir") or ""
        if cwd:
            lines.append(f"- 工作目录：{cwd}")
        # 探测可用工具
        tools = [t for t in ("python", "node", "npm", "uv", "git", "pip") if shutil.which(t)]
        lines.append("- 可用工具：" + (", ".join(tools) if tools else "（未检测到常见 CLI）"))
        return "\n".join(lines)

    def build_context(self, max_rounds: int | None = None) -> SessionContext:
        return SessionContext(
            max_rounds=int(max_rounds or self.cfg.get("max_rounds", 10)),
            max_context_messages=int(self.cfg.get("max_context_messages", 50)),
            env_info=self._build_env_info(),
        )

    def build_executor(self, allow_exec: bool | None = None,
                       work_dir: str | Path | None = None,
                       allow_outside: bool | None = None) -> LocalExecutor:
        """allow_exec 为 None 时按配置开关；为 True/False 时强制覆盖（MCP 侧按需开）。

        work_dir / allow_outside 为 None 时取配置；两者都可按任务覆盖（Web 端下发）。
        """
        if allow_exec is None:
            shell = bool(self.cfg.get("enable_shell_exec", False))
            write = bool(self.cfg.get("enable_file_write", False))
        else:
            shell = write = bool(allow_exec)
        if work_dir is None:
            work_dir = self.cfg.get("work_dir") or None
        if allow_outside is None:
            allow_outside = bool(self.cfg.get("allow_outside_work_dir", False))
        self.work_dir, self.work_dir_warning = resolve_work_dir(
            self.base_dir, work_dir, allow_outside=bool(allow_outside),
        )
        return LocalExecutor(
            cwd=self.work_dir,
            allow_shell=shell,
            allow_file_write=write,
            allow_outside=bool(allow_outside),
            timeout_sec=int(self.cfg.get("exec_timeout_sec", 30)),
            sandbox=bool(self.cfg.get("sandbox_shell", False)),
            low_integrity=bool(self.cfg.get("sandbox_integrity_low", False)),
            shell_backend=str(self.cfg.get("shell_backend", "cmd")),
        )

    def _active_profile(self) -> dict:
        """返回当前生效的模型 profile。优先 active_profile 指定的档案，否则用顶层字段。"""
        profiles = self.cfg.get("profiles") or []
        active = self.cfg.get("active_profile")
        for p in profiles:
            if p.get("name") == active:
                return p
        # 兼容旧配置：没有 profiles 时用顶层字段
        return {
            "base_url": self.cfg.get("base_url", ""),
            "api_key": self.cfg.get("api_key", ""),
            "model": self.cfg.get("model", ""),
            "timeout_sec": self.cfg.get("step_timeout_sec", 120),
        }

    def build_model(self, role: str = "act") -> OpenAIClient | None:
        """构造模型客户端。

        role="act"：执行/思考/观察/验证等阶段（当前 profile，超时 step_timeout_sec）。
        role="plan"：计划阶段专用（plan_model，未配置则回退返回 None，由循环层回落到 act 模型；
                     超时 plan_timeout_sec，默认更长以容纳推理模型）。
        """
        prof = self._active_profile()
        if role == "plan":
            name = self.cfg.get("plan_model") or prof.get("model", "")
            if not self.cfg.get("plan_model"):
                return None  # 未配置计划模型 → 回落到 act 模型
            timeout = int(self.cfg.get("plan_timeout_sec", 300))
        else:
            name = prof.get("model", "")
            timeout = int(prof.get("timeout_sec") or self.cfg.get("step_timeout_sec", 120))
        return OpenAIClient(
            prof.get("base_url", ""), prof.get("api_key", ""), name, timeout,
        )

    def build_runtime(
        self,
        render: Renderer,
        control: ControlChannel | None = None,
        allow_exec: bool | None = None,
        max_rounds: int | None = None,
        model=None,
        context: SessionContext | None = None,
        gate_mode: str | None = None,
        work_dir: str | Path | None = None,
        allow_outside_work_dir: bool | None = None,
    ) -> Runtime:
        """构造一次运行所需的全部对象并接线（gate/ask 由 control 提供）。

        context 传入时复用（Web 会话需要跨多轮任务保持同一上下文），否则新建。
        gate_mode 为 None 时取配置值（默认 step）。
        """
        control = control or AutoControl()
        registry = self.build_registry()
        context = context or self.build_context(max_rounds)
        executor = self.build_executor(allow_exec, work_dir, allow_outside_work_dir)
        if model is not None:
            act_model = model          # 外部指定了单一模型 → 计划阶段回落到它
            plan_model = None
        else:
            act_model = self.build_model("act")
            plan_model = self.build_model("plan")
        mode = gate_mode or self.cfg.get("gate_mode", "plan")
        ask_fn = control.as_ask()
        if mode == "auto":
            ask_fn = lambda q: "（自动回答：继续）"
        loop = ReActLoop(
            registry, context, act_model, render,
            gate=control.as_gate(), ask=ask_fn, executor=executor,
            gate_mode=mode,
            interrupt=control.as_interrupt(), plan_model=plan_model,
        )
        return Runtime(loop=loop, context=context, registry=registry,
                       executor=executor, control=control)
