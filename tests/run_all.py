"""冒烟编排：搭环境 → 跑一轮循环 → 汇总全部断言。

供 main.py 的 `--smoke` / `--smoke-live` 调用，也可独立运行：
    python -m tests.run_all

返回 (failures, live_stats)：
- failures：失败说明列表（空=通过）
- live_stats：仅 live 模式的统计行（非 live 为 None）
"""

from __future__ import annotations

import sys
from pathlib import Path

from react.action import ActionRegistry
from react.context import SessionContext
from react.loop import ReActLoop
from react.model import MockClient, OpenAIClient
from react.render import RichRenderer

from . import smoke_checks as C


class _RecordingExecutor:
    """冒烟用执行器：记录调用，不产生真实副作用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.tool_calls: list[tuple[str, dict]] = []

    def run(self, kind: str, payload: str) -> str:
        self.calls.append((kind, payload))
        return "[exec-out] 冒烟执行回显"

    def run_tool(self, name: str, args: dict) -> str:
        self.tool_calls.append((name, args))
        return "[tool-out] 冒烟工具回显"


def run_smoke(live: bool, skills_dir: Path, base_dir: Path,
              console=None, config_path: Path | None = None) -> tuple[list[str], str | None]:
    """跑一轮冒烟并返回 (failures, live_stats)。"""
    show_reasoning = True
    cfg = None
    if live:
        from react.config import load_config, resolve_config_path

        cfg = load_config(config_path or resolve_config_path(base_dir)).values
        show_reasoning = cfg["show_reasoning"]
    render = RichRenderer(console, show_reasoning=show_reasoning)

    registry = ActionRegistry()
    registry.load(skills_dir)
    for w in registry.warnings:
        render.warn(w)

    if live:
        model = OpenAIClient(cfg["base_url"], cfg["api_key"], cfg["model"],
                             cfg["step_timeout_sec"])
        task = "用一句话说明 ReAct 循环中 OBSERVE 步骤的作用。"
    else:
        # 首轮 OBSERVE 故意判缺陷（验证修正回路），首轮 VERIFY 故意不通过（验证验收回路）
        # emit_exec：ACT 附 [EXEC: shell]，验证执行器接线
        model = MockClient(verify_fail_once=True, observe_defect_once=True, emit_exec=True)
        task = "冒烟测试任务"

    recorder = _RecordingExecutor()
    context = SessionContext(max_rounds=5, max_context_messages=12)
    loop = ReActLoop(registry, context, model, render, gate=None,
                     ask=lambda q: "冒烟回答：输入已确认", executor=recorder)
    result = loop.run(task)
    if console is not None:
        console.print(f"[bold]循环结束[/bold]：status={result.status} rounds={result.rounds}")

    # live 模式只做兼容性断言
    if live:
        live_failures, stats = C.check_live(result, context)
        return live_failures, stats

    failures: list[str] = []
    calls = model.calls if isinstance(model, MockClient) else None
    failures += C.check_loop_structure(calls, result, context, recorder, model)
    failures += C.check_parsers()
    failures += C.check_ask_limit(registry, render, recorder)
    failures += C.check_executor_defaults(base_dir)
    failures += C.check_context_windowing()
    failures += C.check_tool_window()
    failures += C.check_sandbox_nested()
    failures += C.check_plan_revision()
    failures += C.check_display_classify()
    failures += C.check_model_error_policy()
    failures += C.check_exec_all()
    failures += C.check_solution_parse()
    failures += C.check_gate_mode()
    failures += C.check_interrupt()
    failures += C.check_work_dir(base_dir)
    failures += C.check_native_tools()
    return failures, None


def main() -> int:
    """独立入口：python -m tests.run_all [--live]"""
    from rich.console import Console

    base = Path(__file__).resolve().parent.parent
    console = Console()
    live = "--live" in sys.argv
    failures, stats = run_smoke(live=live, skills_dir=base / "skills",
                                base_dir=base, console=console)
    if stats:
        console.print(f"[dim]{stats}[/dim]")
    if failures:
        for f in failures:
            console.print(f"[red]✘ 断言失败: {f}[/red]")
        return 1
    console.print("[green]✔ 冒烟测试通过[/green]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
