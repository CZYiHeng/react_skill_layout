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
    "plan_model": "REACT_AGENT_PLAN_MODEL",
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
    "max_context_tokens": 200000,   # 压缩阈值（token）：下一次请求预估超过它才压缩
    "enable_shell_exec": False,
    "enable_file_write": False,
    "exec_timeout_sec": 30,
    "sandbox_shell": False,
    "sandbox_integrity_low": False,
    "shell_backend": "cmd",      # cmd / bash / auto；auto=探测 Git Bash，找不到回退 cmd
    "gate_mode": "auto",
    "work_dir": "",
    "allow_outside_work_dir": False,
    "plan_model": "",            # 计划阶段专用模型（推理模型如 deepseek-reasoner）；空=与 model 相同
    "plan_timeout_sec": 300,     # 计划模型超时（推理模型更慢，默认更长）
}

#: 人工闸门档位。auto=只在「需要人决策」时拦（默认）
#: step=每个计划步骤收尾都拦 / plan=计划产出后额外拦一次 / phase=每阶段都拦（旧行为，回滚开关）
#:
#: 为什么默认 auto：闸门的目的是「在需要人决定时介入」，而 plan 档把它变成「每个任务
#: 无条件拦一次」——问答类、只读类任务被无差别打断，人手一慢就卡在那儿。
#: 「计划跑偏」这件事框架已有独立于闸门的兜底（连续 3 次未通过强制重出计划、max_rounds、
#: 判定歧义默认不通过/移交），所以默认不靠人肉审批也不掉质量：打断从「无差别」变「定向」。
#: 长任务想强制先看计划再开跑，把 gate_mode 显式设为 "plan" 即可。
GATE_MODES = ("plan", "step", "auto", "phase")

#: max_context_tokens 语义：**压缩的触发阈值（token）**。
#: 每次模型调用后都会回灌 provider 的实测 usage，据此预估"下一次请求的 prompt 量"
#: （实测 + 新增消息估算，对齐 DSH token-meter 的 pressureTokens 口径）；只有预估
#: 超过此阈值才压缩一次。
#:
#: 为什么用 token 而不是条数：条数表达不了"这次请求要花多少 prompt tokens"——一条
#: tool 回执上万字符也只算 1 条。旧实现按条数触发，在真实负载下被反复触发，而
#: provider 的前缀缓存要求完整匹配缓存前缀单元，压缩一次就作废其后全部缓存
#: （实测同会话 prompt 非单调 19358→16481，OBSERVE 命中率仅 3.3%）。
#: 取值 200k 的依据是**实测**而非估算：一次真实的工具型任务（dupfinder，5 轮
#: 43 次调用）prompt 峰值到 72734 token，在 100k 预算下仍触发了 4 次压缩
#: （prompt 下跌 4 次，每次都是缓存作废点）。抬到 200k 后同类任务全程不压缩；
#: DeepSeek 官方端点窗口 1M，留足输出仍有很大余量。
#: 窗口更小的端点请按"窗口 × 0.6"下调（例如 128k 窗口 → 约 76000）。
#: 设为 0 或负数 = 取消上下文预算（只剩内部条数护栏兜底），即回滚开关。
#:
#: 注：早先还有一个 `max_context_messages`（条数预算）配置键，已**删除**。
#: 它与 token 预算并列摆放，容易被当成"上下文大小"旋钮一直留着（实测就有人把
#: 12 留在配置里），而 12 条这个量级会让压缩频繁触发、把前缀缓存反复打断——
#: 正是本次要修的根因。条数护栏现在只是内部量（见 context._max_window_messages）。

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
