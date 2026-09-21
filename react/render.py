"""rich 终端渲染：分色面板 + 轮次分隔 + 产物高亮。

本模块同时是 **Renderer 协议的唯一定义处**（此前 loop.py 里另有一份只声明 3 个方法的
残缺副本，与 loop 实际调用的 7 个方法脱节，导致 mcp_server 得手写 `_NullRenderer` 补洞）。
三方消费者（CLI 的 RichRenderer / Web 的 EventRenderer / MCP 的 NullRenderer）统一实现本协议。
"""

from __future__ import annotations

from typing import Callable, Protocol

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from .display import KIND_LABELS, build_renderable, classify

# DESIGN.md §3.6 分色规范
_COLORS = {
    "think": "blue",
    "plan": "yellow",
    "act": "green",
    "observe": "magenta",
    "verify": "cyan",
}
_ICONS = {
    "think": "◆ THINK",
    "plan": "◇ PLAN",
    "act": "▶ ACT",
    "observe": "● OBSERVE",
    "verify": "✔ VERIFY",
}


class Renderer(Protocol):
    """渲染/输出协议：ReActLoop 只依赖本接口，不关心背后是终端、SSE 还是静默收集。"""

    def round_banner(self, round_no: int) -> None: ...
    def show(self, action: str, parsed: str, raw: str,
             elapsed_sec: float, tokens: int, reasoning: str = "",
             usage: dict | None = None) -> None: ...
    def ask_question(self, question: str) -> None: ...
    def info(self, text: str) -> None: ...
    def warn(self, text: str) -> None: ...
    def error(self, text: str) -> None: ...
    def success(self, text: str) -> None: ...

    def solution(self, text: str) -> None:
        """ACT 的方案确认卡片：目的 / 方案 / 预期 / 对比 / 优缺点。

        作用是用「留痕」替代「逐步打断人」——模型自证为什么这么做，
        人随时可回看依据，但循环不停下来等批准。
        """
        ...

    def on_token(self, action: str) -> Callable[[str], None] | None:
        """返回该步骤的流式 token 回调；返回 None 表示不启用流式。

        用于把模型逐 token 输出推给前端（此前 loop.py 从未透传 on_token，流式是死代码）。
        """
        ...


class RichRenderer:
    def __init__(self, console: Console | None = None, show_reasoning: bool = True):
        self.console = console or Console()
        self.show_reasoning = show_reasoning

    def on_token(self, action: str) -> Callable[[str], None] | None:
        """终端渲染器不启用流式（保持既有逐面板输出行为不变）。"""
        return None

    def round_banner(self, round_no: int) -> None:
        self.console.rule(f"[bold]Round {round_no}[/bold]", style="dim")

    def show(self, action: str, parsed: str, raw: str,
             elapsed_sec: float, tokens: int, reasoning: str = "") -> None:
        color = _COLORS.get(action, "white")
        title = f"{_ICONS.get(action, action.upper())} · {elapsed_sec:.1f}s · {tokens}tok"
        if action == "act":
            # ACT 产物：过显示模板拦截层（内容类型 → 展示模板），面板内保留完整原文
            kind, meta = classify(parsed)
            title = (f"{_ICONS[action]} · {KIND_LABELS.get(kind, kind)}"
                     f" · {elapsed_sec:.1f}s · {tokens}tok")
            body = Group(
                Text("【产物 · 请人工取用】", style="bold reverse"),
                build_renderable(kind, meta),
                Text(f"\n{'─' * 12} 原文（含 CHECK 标准） {'─' * 12}", style="dim"),
                Text(raw),
            )
        elif action == "think" and reasoning and self.show_reasoning:
            # THINK：先展示模型的思考过程（reasoning_content），再展示结论
            body = Group(
                Text("── 思考过程 ──", style="dim"),
                Text(reasoning, style="dim italic"),
                Text(f"\n{'─' * 12} 结论 {'─' * 12}", style="dim"),
                Text(parsed),
            )
        else:
            body = Text(parsed)
        self.console.print(Panel(body, title=title, border_style=color,
                                 box=box.ROUNDED, title_align="left"))

    def ask_question(self, question: str) -> None:
        """ASK 决策的专用提问面板：模型缺少信息，阻塞等待用户回答。"""
        self.console.print(Panel(
            Text(question, style="bold yellow"),
            title="? ASK · 模型提问", border_style="yellow",
            box=box.ROUNDED, title_align="left"))

    def solution(self, text: str) -> None:
        """方案确认面板：不阻塞，仅把决策依据显式留痕。"""
        self.console.print(Panel(
            Text(text),
            title="◈ 方案确认 · 为什么这么做", border_style="blue",
            box=box.ROUNDED, title_align="left"))

    def info(self, text: str) -> None:
        self.console.print(f"[dim]{text}[/dim]")

    def warn(self, text: str) -> None:
        self.console.print(f"[yellow]⚠ {text}[/yellow]")

    def error(self, text: str) -> None:
        self.console.print(f"[red]✘ {text}[/red]")

    def success(self, text: str) -> None:
        self.console.print(f"[green]✔ {text}[/green]")
