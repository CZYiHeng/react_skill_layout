"""ReAct Agent CLI — 入口：REPL + 子命令（--bind / --smoke / --smoke-live）。"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

# Windows 终端 UTF-8 防护（DESIGN.md §4 风险点）
for _stream in (sys.stdout, sys.stderr, sys.stdin):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError, ValueError):
        pass

from rich.console import Console

from react.action import ACTION_NAMES, ActionRegistry
from react.config import ConfigError, load_config as load_config_module, resolve_config_path
from react.render import RichRenderer
from react.service import CliControl, ReactService
from tests.run_all import run_smoke

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SKILLS_DIR = BASE_DIR / "skills"
DEFAULT_CLAUDE_SKILLS = Path.home() / ".claude" / "skills"

HELP_TEXT = """指令：
  /help          显示本帮助与当前绑定状态
  /binds         列出 5 个动作槽位的绑定状态
  /reset         清空会话上下文
  /save [文件]   导出会话全文为 Markdown（缺省自动时间戳命名）
  /quit          退出

循环步进控制（每步执行后询问）：
  c / 回车       继续下一步
  s <纠偏内容>   人工纠偏，注入下一轮 THINK
  q              中止当前循环，回到指令输入
"""


def _config_path() -> Path:
    """config 路径：默认 BASE_DIR/config.json，可由环境变量 REACT_AGENT_CONFIG 覆盖。"""
    return resolve_config_path(BASE_DIR)


def load_config(path: Path, console: Console) -> dict:
    """加载配置；失败时打印原因并以退出码 1 结束（CLI 边界，UX 保持不变）。

    配置加载本身统一在 react.config 里完成；此处只负责把 ConfigError 翻译成命令行行为。
    """
    try:
        loaded = load_config_module(path)
    except ConfigError as e:
        console.print(f"[red]配置错误[/red]：{e}")
        raise SystemExit(1)
    for w in loaded.warnings:
        console.print(f"[yellow]⚠ 安全提示[/yellow]：{w}")
    return loaded.values


def cmd_bind(action: str, skill: str, skills_dir: Path, console: Console) -> None:
    if action not in ACTION_NAMES:
        console.print(f"[red]未知动作槽位: {action}[/red]，可选: {', '.join(ACTION_NAMES)}")
        raise SystemExit(1)
    src = DEFAULT_CLAUDE_SKILLS / skill
    registry = ActionRegistry()
    try:
        target = registry.bind(action, src, skills_dir)
    except FileNotFoundError as e:
        console.print(f"[red]绑定失败[/red]：{e}（从 {DEFAULT_CLAUDE_SKILLS} 查找）")
        raise SystemExit(1)
    console.print(f"[green]✔ 已绑定[/green]：{skill} → 槽位 {action}（{target}）")


def cmd_smoke(live: bool, skills_dir: Path, console: Console) -> None:
    """冒烟测试：mock 验证结构（零消耗）；live 验证真实 API 兼容性。

    搭环境与全部断言已迁至 tests/ 包（此前 221 行断言塞在本函数里）；
    本函数只负责调度与汇总，对外输出与退出码保持不变。
    """
    render = RichRenderer(console)
    failures, live_stats = run_smoke(live=live, skills_dir=skills_dir,
                                     base_dir=BASE_DIR, console=console,
                                     config_path=_config_path())

    # live 专属断言：验证真实 API 兼容性与工具回写（缺口 ⑤ / ⑧）
    if live:
        if live_stats:
            console.print(f"[dim]{live_stats}[/dim]")
        if failures:
            for f in failures:
                render.error(f"live 断言失败: {f}")
            raise SystemExit(1)
        render.success("live 冒烟测试通过（真实 API 兼容性 OK）")
        return

    if failures:
        for f in failures:
            render.error(f"断言失败: {f}")
        raise SystemExit(1)
    render.success("冒烟测试通过")


def cmd_repl(skills_dir: Path, console: Console) -> None:
    cfg = load_config(_config_path(), console)
    render = RichRenderer(console, show_reasoning=cfg["show_reasoning"])

    # 运行时统一由 service 层构造：终端渲染 + REPL 人工步进（CliControl）
    service = ReactService(cfg, BASE_DIR, skills_dir)
    runtime = service.build_runtime(
        render=render, control=CliControl(console), max_rounds=cfg["max_rounds"])
    context = runtime.context
    registry = runtime.registry
    for w in registry.warnings:
        render.warn(w)

    binds = " · ".join(f"{n}{'✓' if registry.get(n).bound else '✗'}" for n in ACTION_NAMES)
    console.print(f"[bold]ReAct Agent[/bold] · {cfg['model']} · 绑定: {binds}")
    console.print(f"[dim]输入 /help 查看指令，直接输入文字开始任务[/dim]")
    console.print(f"[dim]执行器：shell={'开' if cfg['enable_shell_exec'] else '关'}"
                  f" · 文件写入={'开' if cfg['enable_file_write'] else '关'}"
                  f" · 沙箱={'OS级' if cfg['enable_shell_exec'] and cfg['sandbox_shell'] else '关'}"
                  + (f"(低完整性)" if cfg.get('sandbox_integrity_low') else "")
                  + f" · 上下文窗口={cfg['max_context_messages']} 条[/dim]")

    while True:
        try:
            user_in = input("\nreact> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]再见[/dim]")
            return
        if not user_in:
            continue
        if user_in.startswith("/"):
            parts = user_in.split(maxsplit=1)
            cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
            if cmd == "/quit":
                return
            if cmd == "/help":
                console.print(HELP_TEXT)
                for name, desc in registry.bind_status():
                    console.print(f"  {name:8s} {desc}")
            elif cmd == "/binds":
                for name, desc in registry.bind_status():
                    console.print(f"  {name:8s} {desc}")
            elif cmd == "/reset":
                context.reset()
                render.info("会话已清空")
            elif cmd == "/save":
                if arg:
                    save_path = Path(arg)
                else:
                    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
                    save_path = BASE_DIR / f"session_{stamp}.md"
                save_path.write_text(context.export_markdown(), encoding="utf-8")
                render.success(f"会话已导出: {save_path}")
            else:
                render.warn(f"未知指令: {cmd}（/help 查看全部）")
            continue

        # 任务输入 → ReAct 循环（gate/ask 已由 CliControl 接线）
        loop = runtime.loop
        try:
            result = loop.run(user_in)
        except KeyboardInterrupt:
            render.warn("已中断当前循环")
            continue
        if result.status == "escalated":
            render.warn(f"模型移交人工：{result.final_text}")
            continue
        style = "green" if result.status == "done" else "yellow"
        console.print(f"[{style}]循环结束：{result.status}（{result.rounds} 轮）[/]")


def main() -> None:
    parser = argparse.ArgumentParser(prog="react-agent",
                                     description="显式 ReAct 协议的终端 agent（skill 目录约定绑定）")
    parser.add_argument("--bind", nargs=2, metavar=("动作", "SKILL"),
                        help="从 ~/.claude/skills 绑定 skill 到动作槽位，如 --bind plan dev-flow")
    parser.add_argument("--smoke", action="store_true", help="冒烟测试（Mock 模型，零 API 消耗）")
    parser.add_argument("--smoke-live", action="store_true", help="冒烟测试（真实 kimi API）")
    parser.add_argument("--skills-dir", type=Path,
                        default=Path(os.environ.get("REACT_AGENT_SKILLS_DIR", DEFAULT_SKILLS_DIR)),
                        help=f"skill 绑定根目录（默认 {DEFAULT_SKILLS_DIR}，"
                             f"可由环境变量 REACT_AGENT_SKILLS_DIR 覆盖）")
    args = parser.parse_args()

    console = Console()
    if args.bind:
        cmd_bind(args.bind[0], args.bind[1], args.skills_dir, console)
    elif args.smoke:
        cmd_smoke(live=False, skills_dir=args.skills_dir, console=console)
    elif args.smoke_live:
        cmd_smoke(live=True, skills_dir=args.skills_dir, console=console)
    else:
        cmd_repl(args.skills_dir, console)


if __name__ == "__main__":
    main()
