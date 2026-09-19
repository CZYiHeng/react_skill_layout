"""统一配置加载：文件 + 环境变量覆盖 + 默认值 + 安全告警。

设计要点：
- **单一真相源**：CLI（main.py）、MCP（mcp_server.py）、Web（webapi.py）共用本模块，
  消除此前各写一份的重复实现（其中 main.py 版本直接 `SystemExit(1)` 杀进程，属库行为越界）。
- **错误语义收敛**：一切加载失败抛 `ConfigError`，由调用方决定转成退出码（CLI）还是工具错误（MCP/Web），
  库本身不再决定进程生死。
- **占位符判定放宽**：模板里 api_key 的占位写法有多种（`<在此填入你的 key>` / `<在此填入你的 api_key>`），
  统一用"形如 <...> 或含 <在此填入"判定，避免占位符漏网被当成真 key 发给服务商。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# 环境变量覆盖（优先级高于文件）
ENV_MAP = {
    "api_key": "REACT_AGENT_API_KEY",
    "base_url": "REACT_AGENT_BASE_URL",
    "model": "REACT_AGENT_MODEL",
    "gate_mode": "REACT_AGENT_GATE_MODE",
    "work_dir": "REACT_AGENT_WORK_DIR",
    "allow_outside_work_dir": "REACT_AGENT_ALLOW_OUTSIDE_WORK_DIR",
}

# 必填字段（缺一即报错）
REQUIRED_KEYS = ("base_url", "api_key", "model")

# 默认值（config 未显式给出时补齐）
DEFAULTS: dict = {
    "max_rounds": 10,
    "step_timeout_sec": 120,
    "show_reasoning": True,
    "max_context_messages": 12,
    "enable_shell_exec": False,
    "enable_file_write": False,
    "exec_timeout_sec": 30,
    "sandbox_shell": False,
    "sandbox_integrity_low": False,
    "gate_mode": "plan",
    "work_dir": "",
    "allow_outside_work_dir": False,
}

#: 人工闸门档位。plan=计划批准一次后放行（默认，推荐）
#: step=每步骤一次 / auto=连计划也不拦 / phase=每阶段一次（旧行为，回滚开关）
GATE_MODES = ("plan", "step", "auto", "phase")

_PLACEHOLDER_HINT = "<在此填入"


class ConfigError(Exception):
    """配置缺失/损坏/字段不全。由调用方决定如何处理（退出码 / 工具错误）。"""


def is_placeholder(value: object) -> bool:
    """判定是否为模板占位符（未填真值）。"""
    if not isinstance(value, str):
        return False
    v = value.strip()
    return _PLACEHOLDER_HINT in v or (v.startswith("<") and v.endswith(">"))


def resolve_config_path(base_dir: Path) -> Path:
    """config 路径：默认 base_dir/config.json，可由环境变量 REACT_AGENT_CONFIG 覆盖。"""
    return Path(os.environ.get("REACT_AGENT_CONFIG", str(base_dir / "config.json")))


def security_warnings(cfg: dict, path: Path) -> list[str]:
    """明文密钥等安全提示（供调用方打印，不在此处直接输出）。"""
    warns: list[str] = []
    if not is_placeholder(cfg.get("api_key")) and not os.environ.get(ENV_MAP["api_key"]):
        warns.append(
            f"api_key 来自 {path.name} 明文。建议改用环境变量 REACT_AGENT_API_KEY，"
            f"并将该配置文件加入 .gitignore；若曾提交/共享过该文件，请到服务商处轮换 key。"
        )
    return warns


@dataclass
class LoadedConfig:
    """配置与其元信息：值字典 + 来源路径 + 安全提示。"""

    values: dict
    path: Path
    warnings: list[str] = field(default_factory=list)

    def __getitem__(self, key: str):
        return self.values[key]

    def get(self, key: str, default=None):
        return self.values.get(key, default)


def load_config(path: Path) -> LoadedConfig:
    """加载配置：读文件 → 环境变量覆盖 → 校验必填 → 补齐默认值。

    异常：文件缺失/损坏/必填字段为空或占位符 → 抛 ConfigError。
    """
    if not path.is_file():
        raise ConfigError(
            f"{path.name} 缺失：请复制 config.example.json 为 {path.name}，"
            f"或用环境变量 {', '.join(ENV_MAP.values())} 提供"
        )
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path.name} 损坏：{e}") from e
    if not isinstance(cfg, dict):
        raise ConfigError(f"{path.name} 损坏：顶层应为 JSON 对象")

    # 环境变量覆盖：允许不落地明文 key（优先级高于文件）
    for cfg_key, env_key in ENV_MAP.items():
        val = os.environ.get(env_key)
        if val:
            cfg[cfg_key] = val

    for key in REQUIRED_KEYS:
        if not cfg.get(key) or is_placeholder(cfg.get(key)):
            raise ConfigError(
                f"{path.name} 字段缺失：{key}"
                f"（可设环境变量 {ENV_MAP[key]} 或填写 {path.name}）"
            )

    for key, default in DEFAULTS.items():
        cfg.setdefault(key, default)

    warns = security_warnings(cfg, path)
    mode = str(cfg.get("gate_mode", DEFAULTS["gate_mode"])).strip().lower()
    if mode not in GATE_MODES:
        warns.append(
            f"gate_mode={cfg.get('gate_mode')!r} 非法（应为 {'/'.join(GATE_MODES)}），"
            f"已回退为 {DEFAULTS['gate_mode']}"
        )
        mode = DEFAULTS["gate_mode"]
    cfg["gate_mode"] = mode

    return LoadedConfig(values=cfg, path=path, warnings=warns)
