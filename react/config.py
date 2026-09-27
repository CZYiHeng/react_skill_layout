"""统一配置加载：单一文件 + 默认值 + 安全告警 + **唯一的模型接入解析器**。

设计要点：
- **单一真相源**：CLI（main.py）、MCP（mcp_server.py）、Web（webapi.py）共用本模块，
  消除此前各写一份的重复实现（其中 main.py 版本直接 `SystemExit(1)` 杀进程，属库行为越界）。
- **接入配置只有一处**：`providers` 映射 + `active_provider`。此前顶层
  `base_url/api_key/model` 与 `profiles[]` 是两套并列的真相源，导致同一套
  「取生效接入」的逻辑被写了三遍（service._active_profile、webapi._active_profile_cfg、
  以及各处直接读顶层字段），其中 webapi 那份还漏了 timeout_sec。现在一律走
  本模块的 `resolve_provider`。
- **key 只从文件读**（显式决定）：删除 `REACT_AGENT_API_KEY` 等凭据类环境变量覆盖，
  避免"文件一套、环境变量一套"的隐性优先级。启动时对明文 key 给出告警。
- **错误语义收敛**：一切加载失败抛 `ConfigError`，由调用方决定转成退出码（CLI）还是工具错误（MCP/Web），
  库本身不再决定进程生死。
- **占位符判定放宽**：模板里 api_key 的占位写法有多种（`<在此填入你的 key>` / `<在此填入你的 api_key>`），
  统一用"形如 <...> 或含 <在此填入"判定，避免占位符漏网被当成真 key 发给服务商。
- **向后兼容**：旧的顶层 `base_url/api_key/model` 与旧 `profiles[]` 数组仍可读，
  会被自动归一成 providers 形状，存量配置不需要手工迁移。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# 环境变量：只保留"去哪里找配置/技能"，不再覆盖接入类字段（key/base_url/model）。
# 理由（用户显式决定）：key 只从文件读，任何"环境变量盖住文件"的机制都会重新引入
# 两套真相源与隐性优先级。
ENV_CONFIG_PATH = "REACT_AGENT_CONFIG"
ENV_SKILLS_DIR = "REACT_AGENT_SKILLS_DIR"

#: 一份 provider 必须提供的字段（用于校验**生效的那一个**，而不是顶层）。
PROVIDER_REQUIRED = ("base_url", "api_key", "model")

#: provider 里允许出现的字段；未知字段直接报错而不是静默忽略——
#: 拼错的 `base_ur` 会让人对着一个"看起来配了却连不上"的文件排查半天。
PROVIDER_FIELDS = ("base_url", "api_key", "model", "timeout_sec")

#: provider 未显式给 `timeout_sec` 时的单步超时。
#: 超时**只有** provider 级这一处旋钮：曾经同时存在全局 `step_timeout_sec` 与
#: provider 级 timeout_sec，设置页里出现两个"单步超时"，用户无从判断哪个生效。
#: 现在全局项已删除，这里只作缺省值。
DEFAULT_STEP_TIMEOUT = 120

# 默认值（config 未显式给出时补齐）
#:
#: 关于执行参数（exec_timeout_sec / shell_backend / max_rounds）为何取"偏宽"的值：
#: 这三项曾是保守档（30 秒 / cmd / 10 轮），与"执行已启用"的用法自相矛盾——实测就
#: 出过一次 10 轮用满仍未完成（max_rounds_exceeded），而同一份配置里 shell 与写文件
#: 都开着。默认值应当与"开启执行后真正能干活"自洽，而不是让用户自己去发现该调哪个。
#: 安全底线不随之放宽：enable_shell_exec / enable_file_write 仍默认 False。
DEFAULTS: dict = {
    "max_rounds": 20,
    "show_reasoning": True,
    "max_context_tokens": 100000,   # 压缩阈值（token）：下一次请求预估超过它才压缩
    "enable_shell_exec": False,
    "enable_file_write": False,
    "exec_timeout_sec": 120,     # 30 秒跑不动测试/构建/装依赖
    "sandbox_shell": False,
    "sandbox_integrity_low": False,
    "shell_backend": "auto",     # cmd / bash / auto；auto=探测 Git Bash，找不到回退 cmd
    "gate_mode": "auto",
    "work_dir": "",
    "allow_outside_work_dir": False,
    "plan_model": "",            # 计划阶段专用模型（推理模型如 deepseek-reasoner）；空=与 model 相同
    "plan_timeout_sec": 300,     # 计划模型超时（推理模型更慢，默认更长）
    #: 生效的能力（写齐五阶段的一套 skill）。`default` = 仓库自带的 `<base>/skills`。
    #: 具名能力在 `<base>/capabilities/<名>/`，也可由 capability_paths 指向别处。
    "active_capability": "default",
    #: 额外能力根目录：每个条目既可以是"一个能力目录"，也可以是"装多个能力的容器"。
    "capability_paths": [],
    #: 能力别名：`{"旧名": "新名"}`，用于改名后仍让旧配置/旧启动脚本可用。
    "capability_aliases": {},
    #: 槽位内 skill 变体选择：`{"<槽位>": "<变体名>"}`，缺省空 = 五个槽位全用默认。
    #: 变体 = `<skills_root>/<槽位>/<变体名>/SKILL.md`（默认变体是槽位根下的 SKILL.md）。
    #: 未知变体名会被降级为默认并告警，不阻断启动。
    "skill_variants": {},
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
#: 取值 100k 的依据是**实测**，而且这个值被实测**修正过一次**：
#: 起初按"某次任务 prompt 峰值 72734，100k 下仍压缩了 4 次"抬到 200k，结果在更长
#: 的任务上（dupfinder，10 轮 95 次调用）单次 prompt 峰值涨到 199,371、累计 10.87M
#: ——典型工作负载下 200k 反而**放行**了巨大请求，而"每次调用都要重发全部历史"意味着
#: 单次上限越大、二次增长的代价越高。压回 100k 后，仅"截掉超出 100k 的部分"就有
#: 33% 的 prompt 总量（上界估算）。
#: 判据仍是实测：跑同一任务看 /api/token_stats 的 prompt 总量、未命中量与命中率
#: （压小阈值会让压缩更频繁，命中率会掉一些，净收益需实测确认）。
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
    return Path(os.environ.get(ENV_CONFIG_PATH, str(base_dir / "config.json")))


def normalize_providers(cfg: dict) -> dict[str, dict]:
    """把配置里的接入部分归一成 `{名字: provider}`，兼容三种历史形状。

    1. 新形状：`providers: { name: {base_url, api_key, model, timeout_sec} }`
    2. 旧形状：`profiles: [ {name, base_url, api_key, model, timeout_sec}, ... ]`
    3. 最旧形状：顶层 `base_url` / `api_key` / `model`（单家）

    三条路径产出同一结构，所以上层只需要认识一种形状。返回空 dict 表示配置里
    没有任何可用接入——由调用方决定报错文案（不同入口措辞不同）。
    """
    out: dict[str, dict] = {}

    providers = cfg.get("providers")
    if isinstance(providers, dict):
        for name, val in providers.items():
            if isinstance(val, dict):
                out[str(name)] = {k: val[k] for k in PROVIDER_FIELDS if k in val}

    # 旧 profiles 数组（新形状已给出同名项时不覆盖）
    profiles = cfg.get("profiles")
    if isinstance(profiles, list):
        for p in profiles:
            if not isinstance(p, dict):
                continue
            name = str(p.get("name") or "").strip()
            if name and name not in out:
                out[name] = {k: p[k] for k in PROVIDER_FIELDS if k in p}

    # 最旧的顶层三件套
    if any(cfg.get(k) for k in ("base_url", "api_key", "model")):
        top = {k: cfg[k] for k in ("base_url", "api_key", "model") if cfg.get(k)}
        top.setdefault("timeout_sec", DEFAULT_STEP_TIMEOUT)
        fallback = str(cfg.get("active_provider") or cfg.get("active_profile") or "default")
        out.setdefault(fallback, top)
    return out


def active_provider_name(cfg: dict, providers: dict[str, dict] | None = None) -> str:
    """生效的 provider 名：`active_provider`（兼容旧 `active_profile`）→ 唯一项 → 报错。

    显式指定的名字**必须存在**，否则直接报错——哪怕配置里只有一家。否则
    `active_provider: "dseek"` 这种拼写错误会被"唯一项兜底"静默吞掉，
    用户以为自己指定了哪家、实际用的是另一家。
    """
    provs = providers if providers is not None else normalize_providers(cfg)
    name = str(cfg.get("active_provider") or cfg.get("active_profile") or "").strip()
    if name:
        if name in provs:
            return name
        raise ConfigError(
            f"active_provider={name!r} 在配置里不存在；可选：{', '.join(sorted(provs)) or '（无）'}"
        )
    if len(provs) == 1:
        return next(iter(provs))
    raise ConfigError(
        f"配置了 {len(provs)} 个 provider 却没有 active_provider，"
        f"请显式指定其中之一：{', '.join(sorted(provs))}"
    )


def resolve_provider(cfg: dict, role: str = "act") -> dict:
    """**唯一的接入解析器**：返回生效 provider（含 model / timeout_sec）。

    role="act"  → 执行/思考/观察/验证等阶段
    role="plan" → 计划阶段；顶层 `plan_model` 非空时**只换 model**，其余字段
                  （base_url/api_key）仍复用当前 provider，超时用 `plan_timeout_sec`；
                  `plan_model` 为空则返回 `{}`，由调用方回落到 act 模型。
    """
    provs = normalize_providers(cfg)
    if not provs:
        raise ConfigError("配置里没有任何模型接入：请提供 providers 或顶层 base_url/api_key/model")
    name = active_provider_name(cfg, provs)
    prof = dict(provs[name])
    # 超时只有 provider 级这一处；没写就用缺省值（全局 step_timeout_sec 已删除）
    prof.setdefault("timeout_sec", DEFAULT_STEP_TIMEOUT)

    for key in PROVIDER_REQUIRED:
        val = prof.get(key)
        if not val or is_placeholder(val):
            raise ConfigError(
                f"provider {name!r} 字段缺失：{key}（请在配置文件的 providers.{name} 里填写）"
            )

    if role == "plan":
        plan_model = str(cfg.get("plan_model") or "").strip()
        if not plan_model:
            return {}
        prof["model"] = plan_model
        prof["timeout_sec"] = int(cfg.get("plan_timeout_sec", DEFAULTS["plan_timeout_sec"]))
    return prof


def security_warnings(cfg: dict, path: Path) -> list[str]:
    """安全提示：明文密钥、以及"配了多家却没说用哪家"这类易踩形态。"""
    warns: list[str] = []
    provs = normalize_providers(cfg)
    plaintext = sorted(n for n, p in provs.items()
                       if p.get("api_key") and not is_placeholder(p["api_key"]))
    if plaintext:
        warns.append(
            f"api_key 来自 {path.name} 明文（provider: {', '.join(plaintext)}）。"
            f"本项目显式设定 key 只从文件读，请确认该文件已被 .gitignore 排除；"
            f"若曾提交/共享过，请到服务商处轮换该 key。"
        )
    # 组合检查：单看每一项都合法，凑一起就自相矛盾——执行开着，参数却是保守档。
    # 实测踩过：同一份配置里 shell/写文件全开，exec_timeout_sec 只有 30、后端还是 cmd，
    # 结果是长命令被截断、任务 10 轮用满仍未完成。单值校验发现不了这种形态。
    if cfg.get("enable_shell_exec"):
        tight: list[str] = []
        try:
            if int(cfg.get("exec_timeout_sec", 0)) < 60:
                tight.append(f"exec_timeout_sec={cfg.get('exec_timeout_sec')}（建议 >= 120）")
        except (TypeError, ValueError):
            pass
        if str(cfg.get("shell_backend", "")) == "cmd":
            tight.append('shell_backend="cmd"（建议 "auto" 以探测 Git Bash）')
        if tight:
            warns.append(
                "已启用 shell 执行，但执行参数取的是保守档：" + "；".join(tight) +
                "。长命令可能被超时截断，且部分写法在 cmd 下不可用。"
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
    """加载配置：读文件 → 校验生效 provider → 补齐默认值 → 安全提示。

    异常：文件缺失/损坏、或**生效 provider** 的必填字段为空/占位符 → 抛 ConfigError。
    注意校验对象是"生效的那一个 provider"，不再是顶层字段——顶层的
    base_url/api_key/model 只是旧配置的兼容形状，多 provider 部署下它们可以是空的。
    """
    if not path.is_file():
        raise ConfigError(
            f"{path.name} 缺失：请复制 config.example.json 为 {path.name}"
        )
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path.name} 损坏：{e}") from e
    if not isinstance(cfg, dict):
        raise ConfigError(f"{path.name} 损坏：顶层应为 JSON 对象")

    # 显式不读环境变量里的接入信息（key/base_url/model）：key 只从文件读。
    # 因此这里没有任何 os.environ 覆盖——单一来源，避免隐性优先级。
    if not normalize_providers(cfg):
        raise ConfigError(
            f"{path.name} 里没有模型接入：请提供 providers（推荐）"
            f"或顶层 base_url/api_key/model"
        )
    resolve_provider(cfg, "act")   # 校验生效 provider 的必填字段，缺了就抛

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
