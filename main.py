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

from react.action import ACTION_NAMES, DEFAULT_VARIANT, ActionRegistry
from react.config import (ConfigError, active_provider_name, load_config as load_config_module,
                          resolve_config_path, resolve_provider)
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
  /continue      切换任务间记忆（开=下个任务带上上一任务的结论摘要）
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


def _print_first_run_guide(path: Path, console: Console) -> None:
    """首次运行引导：配置文件缺失时告诉用户「下一步做什么」。

    只抛出 `ConfigError` 的文本，新用户看到的就是一行报错 + 退出码 1，
    既不知道 key 写在哪，也不知道模板在哪。这里把「怎么办」补上（仍是退出码 1）。
    """
    example = path.with_name("config.example.json")
    console.print(f"[red]配置缺失[/red]：找不到 {path}")
    console.print("[dim]看起来是第一次运行，需要先准备模型配置"
                  "（OpenAI 兼容端点三件套：base_url / api_key / model）。[/dim]")
    console.print("\n[bold]两步搞定：[/bold]")
    if example.is_file():
        console.print(f"  [cyan]1) 复制模板[/cyan]\n"
                      f"     copy {example.name} {path.name}")
    else:
        console.print(f"  [cyan]1) 新建 {path.name}[/cyan]（参考 config.example.json）")
    console.print(f"  [cyan]2) 编辑 providers 填 key[/cyan]\n"
                  f"     在 {path.name} 的 providers.<名字> 里填 base_url / api_key / model，\n"
                  f"     用 active_provider 指定生效的那一家（多家并存见 "
                  f"config.providers.example.json）")
    console.print(f"\n[dim]配置路径：{path}"
                  f"（可用环境变量 REACT_AGENT_CONFIG 覆盖）[/dim]")


def load_config(path: Path, console: Console, quiet: bool = False) -> dict:
    """加载配置；失败时打印原因并以退出码 1 结束（CLI 边界，UX 保持不变）。

    配置加载本身统一在 react.config 里完成；此处只负责把 ConfigError 翻译成命令行行为。
    文件不存在（首次运行的典型情形）额外补一段引导，其余错误保持原样。
    `quiet=True` 时不打印安全提示（供 `--check` 这种预检用，避免每次双击都弹噪音）。
    """
    try:
        loaded = load_config_module(path)
    except ConfigError as e:
        if not path.is_file():
            _print_first_run_guide(path, console)
        else:
            console.print(f"[red]配置错误[/red]：{e}")
        raise SystemExit(1)
    if not quiet:
        for w in loaded.warnings:
            console.print(f"[yellow]⚠ 安全提示[/yellow]：{w}")
    return loaded.values


def cmd_bind(action: str, skill: str, skills_dir: Path | None, console: Console) -> None:
    if action not in ACTION_NAMES:
        console.print(f"[red]未知动作槽位: {action}[/red]，可选: {', '.join(ACTION_NAMES)}")
        raise SystemExit(1)
    # --bind 是"把 skill 拷进某个能力的槽位目录"，所以必须有具体目录：
    # 没指定就落到 default 能力（<base>/skills），与从前一致。
    target_root = Path(skills_dir) if skills_dir else DEFAULT_SKILLS_DIR
    src = DEFAULT_CLAUDE_SKILLS / skill
    registry = ActionRegistry()
    try:
        target = registry.bind(action, src, target_root)
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
    # 横幅显示**生效 provider** 的模型与名字，而不是顶层字段——
    # 多 provider 时顶层那份可能是空占位，显示出来就是错的
    eff = resolve_provider(cfg, "act")
    console.print(f"[bold]ReAct Agent[/bold] · {eff['model']}"
                  f" · provider={active_provider_name(cfg)} · 绑定: {binds}")
    console.print(f"[dim]输入 /help 查看指令，直接输入文字开始任务[/dim]")
    console.print(f"[dim]执行器：shell={'开' if cfg['enable_shell_exec'] else '关'}"
                  f" · 文件写入={'开' if cfg['enable_file_write'] else '关'}"
                  f" · 沙箱={'OS级' if cfg['enable_shell_exec'] and cfg['sandbox_shell'] else '关'}"
                  + (f"(低完整性)" if cfg.get('sandbox_integrity_low') else "")
                  + f" · 上下文预算={cfg['max_context_tokens']} token"
                  f"（超预算才压缩）[/dim]")

    # 任务间记忆开关：默认关（新任务 = 全新会话）。追问/接续前用 /continue 打开。
    continue_session = False

    while True:
        try:
            prompt = "\nreact> " if not continue_session else "\nreact(接续)> "
            user_in = input(prompt).strip()
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
            if cmd == "/continue":
                continue_session = not continue_session
                render.info("任务间记忆：" + ("开（下个任务会带上上一任务的结论摘要）"
                                          if continue_session else "关（每个任务全新会话）"))
                continue
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
            result = loop.run(user_in, continue_session=continue_session)
        except KeyboardInterrupt:
            render.warn("已中断当前循环")
            continue
        if result.status == "escalated":
            render.warn(f"模型移交人工：{result.final_text}")
            continue
        style = "green" if result.status == "done" else "yellow"
        console.print(f"[{style}]循环结束：{result.status}（{result.rounds} 轮）[/]")


def cmd_check(console: Console, skills_dir: Path) -> None:
    """`--check`：只校验配置并打印生效接入与当前 skill 绑定，不启动循环。

    存在的理由：启动脚本（start.bat）需要"配置好了没"的判断，而它自己那份内联
    检查是按旧形状读顶层 `api_key`/`base_url` 的——接入搬进 `providers` 之后就一直
    误报。让启动脚本调本命令，校验逻辑就不再有两份、也不会再过期。
    退出码：0=可用，1=不可用（脚本据此拦下启动）。
    安全提示在这里静音：预检回答的是"能不能启动"，每次双击都弹一句"key 是明文"
    只会变成噪音（真开始时仍会提示）。
    """
    path = _config_path()
    cfg = load_config(path, console, quiet=True)
    eff = resolve_provider(cfg, "act")
    console.print(f"[green]✔ 配置可用[/green] · {path.name}")
    console.print(f"[dim]  生效 provider：{active_provider_name(cfg)}"
                  f" · 模型：{eff['model']} · 端点：{eff['base_url']}"
                  f" · 超时：{eff['timeout_sec']}s[/dim]")
    n = len(cfg.get("providers") or {})
    if n > 1:
        console.print(f"[dim]  共 {n} 个 provider，可用 active_provider 切换[/dim]")

    # 当前用的是哪个能力、五个槽位各自由谁提供 —— 这是最容易误解的一点：
    # 槽位缺 SKILL.md 会**静默回退内置默认**，不说清楚就以为自己的 skill 在生效。
    svc = ReactService(cfg, BASE_DIR, skills_dir)
    registry = svc.build_registry()
    cap = svc.capability
    console.print(f"[dim]  能力：{cap.name}"
                  + (f" v{cap.version}" if cap.version else "")
                  + f" · {cap.source}[/dim]")
    parts = []
    for slot, _desc in registry.bind_status():
        action = registry.get(slot)
        opts = registry.variants_of(slot)
        extra = [v for v in opts if v != DEFAULT_VARIANT]
        origin = f"[{cap.name}]" if action.bound else "[内置默认]"
        label = cap.name if action.bound else "内置默认"
        parts.append(f"{slot}/{label}" + (f"（可选 {', '.join(opts)}）" if extra else ""))
    console.print("[dim]  " + "  ".join(parts) + "[/dim]")
    missing = [s for s in ACTION_NAMES if not registry.get(s).bound]
    if missing:
        console.print(f"[yellow]  ⚠ 槽位 {', '.join(missing)} 未由该能力提供，"
                      f"已回退内置默认提示词[/yellow]")


def main() -> None:
    parser = argparse.ArgumentParser(prog="react-agent",
                                     description="显式 ReAct 协议的终端 agent（skill 目录约定绑定）")
    parser.add_argument("--bind", nargs=2, metavar=("动作", "SKILL"),
                        help="从 ~/.claude/skills 绑定 skill 到动作槽位，如 --bind plan dev-flow")
    parser.add_argument("--check", action="store_true",
                        help="只校验配置并打印生效接入（0=可用 / 1=不可用），不启动循环")
    parser.add_argument("--smoke", action="store_true", help="冒烟测试（Mock 模型，零 API 消耗）")
    parser.add_argument("--smoke-live", action="store_true", help="冒烟测试（真实 kimi API）")
    parser.add_argument("--capability", metavar="名字",
                        help="按名字选用能力（如 coding）。与 --skills-dir 二选一；"
                             "都没给时用配置里的 active_capability（默认 default）")
    parser.add_argument("--skills-dir", type=Path, default=None,
                        help=f"skill/能力的根目录（默认由 active_capability 决定，"
                             f"通常是 {DEFAULT_SKILLS_DIR}）；"
                             f"也可用环境变量 REACT_AGENT_SKILLS_DIR 指定")
    args = parser.parse_args()

    # 没显式指定时**不要**把默认路径塞进去：否则 `resolve_capability` 会走"按路径解析"，
    # 配置里的 active_capability / 内置别名就永远不生效（--check 曾因此把 default 显示成 skills）。
    skills_dir = args.skills_dir
    if skills_dir is None and args.capability is None:
        env_dir = os.environ.get("REACT_AGENT_SKILLS_DIR")
        skills_dir = Path(env_dir) if env_dir else None
    if args.capability:
        skills_dir = Path(args.capability)   # 名字交给 resolve_capability 解析

    console = Console()
    if args.bind:
        cmd_bind(args.bind[0], args.bind[1], skills_dir, console)
    elif args.check:
        cmd_check(console, skills_dir)
    elif args.smoke:
        cmd_smoke(live=False, skills_dir=skills_dir, console=console)
    elif args.smoke_live:
        cmd_smoke(live=True, skills_dir=skills_dir, console=console)
    else:
        cmd_repl(skills_dir, console)


if __name__ == "__main__":
    main()
