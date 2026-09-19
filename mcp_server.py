"""react-agent MCP server — 把显式 ReAct agent 暴露为 WorkBuddy 可调用工具。

设计要点：
- 走 stdio JSON-RPC，stdout 必须保持纯净，因此渲染层用「静默渲染器」，
  绝不往 stdout 写任何东西（日志/报错一律丢弃）。
- 配置统一走 react.config（不再本地复刻一份）；ConfigError 转 ValueError
  （FastMCP 会转成工具错误），不会像 SystemExit 那样直接杀掉整个 MCP 服务进程。
- 运行时统一走 react.service：NullRenderer（静默）+ AutoControl（全自动，
  ASK 用占位回答）→ 无 REPL 人工步进，适合自动化场景。

用法（WorkBuddy 连接器注册后会自动拉起，无需手动运行）：
    python mcp_server.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Windows 终端 UTF-8 防护（MCP 走 stdio，stdout 必须纯净，仅做编码设置不写内容）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError, ValueError):
        pass

from mcp.server.fastmcp import FastMCP

from react.config import ConfigError, load_config, resolve_config_path
from react.service import AutoControl, NullRenderer, ReactService

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SKILLS_DIR = BASE_DIR / "skills"


def _load_config() -> dict:
    """读取配置；缺字段抛 ValueError（FastMCP 会转成工具错误）。

    配置加载统一走 react.config（不再本地复刻一份），此处只做异常语义转换：
    ConfigError → ValueError，避免像 SystemExit 那样杀掉 MCP 服务进程。
    """
    try:
        return load_config(resolve_config_path(BASE_DIR)).values
    except ConfigError as e:
        raise ValueError(str(e)) from e


mcp = FastMCP("react-agent")


@mcp.tool()
def run_react_agent(
    task: str,
    skills_dir: str | None = None,
    max_rounds: int = 10,
    allow_exec: bool = False,
) -> str:
    """对给定任务运行显式 ReAct agent，返回完整轨迹 JSON 字符串。

    适合 WorkBuddy 自动化（automation）调用：全自动执行，无人工步进。
    gateway=None + ASK 占位回答，模型反复提问超过上限会安全 ESCALATE 移交人工。

    Args:
        task: 任务描述（自然语言）。必填。
        skills_dir: 可选的 skill 根目录，默认使用项目内 skills/。
        max_rounds: 最大循环轮数（默认 10，防死循环护栏）。
        allow_exec: 是否允许 ACT 真实执行 shell/文件写入（默认 False，安全兜底，
                    除非你明确信任该任务，否则保持关闭）。

    Returns:
        JSON 字符串，结构：
        {
          "status": "done|escalated|max_rounds_exceeded|aborted",
          "rounds": int,
          "final_text": str,
          "warnings": [str, ...],
          "messages": [ {role, content, tool_calls?, tool_call_id?}, ... ]  # 完整轨迹账本，含原生工具调用回写
        }
    """
    cfg = _load_config()

    skills_path = Path(skills_dir) if skills_dir else DEFAULT_SKILLS_DIR
    if not skills_path.is_dir():
        raise ValueError(f"skills_dir 不存在: {skills_path}")

    # 统一走 service 层构造：静默渲染 + 全自动控制（无人工步进）
    service = ReactService(cfg, BASE_DIR, skills_path)
    runtime = service.build_runtime(
        render=NullRenderer(),
        control=AutoControl(),
        allow_exec=bool(allow_exec),
        max_rounds=max(1, int(max_rounds)),
    )
    result = runtime.loop.run(task)

    payload = {
        "status": result.status,
        "rounds": result.rounds,
        "final_text": result.final_text,
        "warnings": list(getattr(runtime.registry, "warnings", [])),
        "messages": runtime.context.messages,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
