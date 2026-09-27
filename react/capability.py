"""能力（capability）：写齐五阶段 skill 的自包含包。

一个「能力」= 一个目录，里面是与五阶段同构的 skill 文件：

    capabilities/coding/
    ├── capability.json                    # 元数据（可选）
    └── {think,plan,act,observe,verify}/SKILL.md

它有两条设计约束，都是刻意的：

1. **自包含**：能力可以被整体搬走（`git rm -r` 或移到别的仓库），框架不留悬空引用。
   所以能力名与路径解耦——名字是标识，路径是位置，由本模块解析。
2. **不新增阶段**：`ReActLoop` 的五阶段转换与 gate 分支按阶段名硬编码
   （见 `react/loop.py` 的 `_step(...)`），所以能力只能换这五个阶段的提示词，
   不能改流程形状。要新增阶段是一次状态机重构，不是加个目录。

解析入口是 `resolve_capability()`：它是**唯一**的能力定位实现。
此前"用哪套 skill"散落在 `--skills-dir`、`REACT_AGENT_SKILLS_DIR` 与调用方默认值里，
本项目已经吃过"同一逻辑写三遍"的亏（见 config.resolve_provider 的注释），这里不再重复。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .action import ACTION_NAMES
from .config import ConfigError

#: 能力格式兼容版本（与产品版本分开，理由见 react/__init__.py）。
from . import CAPABILITY_API as DEFAULT_CAPABILITY_API

#: `capability.json` 里允许出现的键；未知键报错而不是静默忽略——
#: 拼错的 `descripton` 会让人对着一个"看起来配了却没生效"的文件排查半天。
MANIFEST_KEYS = ("name", "version", "description", "requires")

#: 保留名：指向仓库自带的通用五阶段 skill（`<base>/skills`）。
#: 注意**不含空串**——空串是"没指定"的意思，要先让配置 `active_capability` 有机会生效，
#: 不能在这里就当成 default 返回（那样配了 active_capability 也不会被读到）。
DEFAULT_CAPABILITY = "default"
_RESERVED = (DEFAULT_CAPABILITY, "builtin")

#: 能力容器目录名（新能力的默认归宿）。
CAPABILITIES_DIR = "capabilities"


@dataclass
class Capability:
    """一个能力：名字 + 目录 + 元数据 + 完整性信息。"""

    name: str
    root: Path
    version: str = ""
    description: str = ""
    #: 该能力实际提供 SKILL.md 的阶段（其余阶段运行时会回退内置默认提示词）
    provided: tuple[str, ...] = ()
    #: 来源说明，用于报错与 --check（是内置、还是某个能力目录）
    source: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """五个阶段是否都提供了 SKILL.md。缺的阶段会静默回退内置默认，故要能看出来。"""
        return set(self.provided) == set(ACTION_NAMES)

    def missing(self) -> list[str]:
        return [s for s in ACTION_NAMES if s not in self.provided]


def load_manifest(root: Path) -> tuple[dict, list[str]]:
    """读 `<root>/capability.json`，返回 (元数据, 告警)。

    没有该文件 = 匿名能力（名字取目录名），这是允许的：现有 `skills/` 就没有。
    损坏时降级为匿名能力并告警，不抛——启动不该因为一个元数据文件写坏而死。
    """
    warns: list[str] = []
    path = root / "capability.json"
    if not path.is_file():
        return {}, warns
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {}, [f"{path} 无法解析（{e}），已按匿名能力处理"]
    if not isinstance(data, dict):
        return {}, [f"{path} 顶层应为 JSON 对象，已按匿名能力处理"]
    unknown = [k for k in data if k not in MANIFEST_KEYS]
    if unknown:
        warns.append(f"{path} 含未知字段 {', '.join(sorted(unknown))}，已忽略"
                     f"（可用：{', '.join(MANIFEST_KEYS)}）")
    return data, warns


def _provided_stages(root: Path) -> tuple[str, ...]:
    return tuple(s for s in ACTION_NAMES if (root / s / "SKILL.md").is_file())


def probe(root: Path, name: str = "", source: str = "") -> Capability:
    """把一个目录读成一个能力（不校验存在性，调用方负责）。"""
    data, warns = load_manifest(root)
    # 名字的优先级：manifest.name > 显式传入的 name > 目录名。
    # `default` 能力位于 `<base>/skills`，目录名是 "skills"，但它的能力名必须叫
    # default（否则 --check 与报错里会出现一个用户从未配置过的名字）。
    return Capability(
        name=str(data.get("name") or name or root.name),
        root=root,
        version=str(data.get("version") or ""),
        description=str(data.get("description") or ""),
        provided=_provided_stages(root),
        source=source or str(root),
        warnings=warns,
    )


def _version_tuple(text: str) -> tuple:
    parts = []
    for p in str(text).split("."):
        parts.append(int(p) if p.isdigit() else 0)
    return tuple(parts) or (0,)


def check_requires(manifest: dict, current_version: str) -> str | None:
    """校验 `requires.react_agent`（只做单向区间：>= / <= / == 前缀）。

    不实现依赖求解——那是包管理器的活，本项目不需要。返回错误说明或 None。
    """
    req = manifest.get("requires")
    if not isinstance(req, dict):
        return None
    spec = str(req.get("react_agent") or "").strip()
    if not spec:
        return None
    for op in (">=", "<=", "=="):
        if spec.startswith(op):
            want = _version_tuple(spec[len(op):])
            have = _version_tuple(current_version)
            ok = {">=": have >= want, "<=": have <= want, "==": have == want}[op]
            if not ok:
                return (f"要求 react_agent {spec}，当前 {current_version}"
                        f"（见 capability.json 的 requires）")
            return None
    return f"requires.react_agent={spec!r} 格式无法识别（支持 >= / <= / ==）"


def capability_roots(cfg: dict, base_dir: Path) -> list[Path]:
    """按优先级列出能力搜索根：内置容器 → 配置列出的额外目录。

    容器目录是 `capabilities/`；另外为了兼容历史，平级的 `skills_*` 目录也算能力。
    """
    roots: list[Path] = []
    container = Path(base_dir) / CAPABILITIES_DIR
    if container.is_dir():
        roots.extend(sorted(p for p in container.iterdir() if p.is_dir()))
    # 历史平级布局（如 skills_code/）：只要目录里有任一阶段的 SKILL.md 就当能力候选
    for child in sorted(Path(base_dir).iterdir()):
        if not child.is_dir() or child.name in (CAPABILITIES_DIR, "skills"):
            continue
        if child.name.startswith("skills_") and _provided_stages(child):
            roots.append(child)
    for extra in cfg.get("capability_paths") or []:
        p = Path(str(extra))
        if not p.is_dir():
            continue
        # 一个额外根既可能"自身就是能力目录"，也可能是"能力的容器"
        if _provided_stages(p):
            roots.append(p)
        else:
            roots.extend(sorted(c for c in p.iterdir() if c.is_dir()))
    return roots


def discover(cfg: dict, base_dir: Path,
             current_version: str = "") -> tuple[dict[str, Capability], list[str]]:
    """扫描能力，返回 ({名字: Capability}, 告警)。后出现者覆盖先出现者并告警。"""
    current_version = current_version or DEFAULT_CAPABILITY_API
    warns: list[str] = []
    found: dict[str, Capability] = {}
    for root in capability_roots(cfg, base_dir):
        cap = probe(root)
        if not cap.provided:
            warns.append(f"{root} 下没有任何阶段的 SKILL.md，未作为能力载入")
            continue
        manifest, manifest_warns = load_manifest(root)
        bad = check_requires(manifest, current_version)
        if bad:
            warns.append(f"能力 '{cap.name}'（{root}）已跳过：{bad}")
            continue
        if cap.name in found:
            warns.append(
                f"能力名 '{cap.name}' 重复：{root} 覆盖了 {found[cap.name].root}"
            )
        if not cap.complete:
            manifest_warns = list(manifest_warns) + [
                f"能力 '{cap.name}' 未提供 {', '.join(cap.missing())} 阶段，"
                f"这些槽位将回退内置默认提示词"
            ]
        cap.warnings = manifest_warns
        found[cap.name] = cap
    return found, warns


def resolve_capability(cfg: dict, base_dir: Path, ref: str | Path | None = None,
                       current_version: str = "") -> Capability:
    """把「能力名 / 目录路径 / 空」解析成一个 Capability。

    - 空 / `default` / `builtin` → `<base>/skills`（通用五阶段，保持默认行为）
    - 是已存在的目录 → 直接按该路径（保留 `--skills-dir` 语义）
    - 否则当能力名 → 查 `capabilities/`、平级 `skills_*`、`capability_paths`、
      以及 `<base>/<name>` 这个平级目录（历史上 `skills_code` 这类就地目录无需配置即可解析）
    - 别名 → 先经 `capability_aliases` 映射再解析（改名后旧名仍可用）
    - 找不到 → 抛 ConfigError 并列出可用能力与来源（**不静默回退默认**：
      静默回退会让人以为在用自己的能力，实际跑的是通用档）
    """
    base_dir = Path(base_dir)
    aliases = cfg.get("capability_aliases") or {}
    text = "" if ref is None else str(ref).strip()
    if text:
        text = str(aliases.get(text, text)).strip()

    if text in _RESERVED:
        return probe(base_dir / "skills", name=DEFAULT_CAPABILITY, source="仓库内置")

    if text:
        p = Path(text)
        if p.is_dir():
            return probe(p, source=str(p))
    caps, warns = discover(cfg, base_dir, current_version)
    if not text:
        # 配置里指定了生效能力时用它；否则 default
        text = str(cfg.get("active_capability") or "").strip()
        if text:
            text = str(aliases.get(text, text)).strip()
        if not text or text in _RESERVED:
            cap = probe(base_dir / "skills", name=DEFAULT_CAPABILITY, source="仓库内置")
            cap.warnings = list(cap.warnings)
            return cap

    if text not in caps:
        # 平级目录回退：`<base>/<名字>` 或 `capabilities/<名字>`。
        # 让"目录名"也能当引用用——历史布局（`skills_code` 这类就地目录）与
        # "能力名 ≠ 目录名"的情形都不必写配置别名即可解析。
        # 注意：别名里显式写了映射时以别名为准（用户意图优先）。
        aliased = text in (cfg.get("capability_aliases") or {})
        if not aliased:
            for cand in (base_dir / text, base_dir / CAPABILITIES_DIR / text):
                if cand.is_dir() and _provided_stages(cand):
                    return probe(cand, name=text, source=str(cand))

    if text in caps:
        cap = caps[text]
        cap.warnings = list(cap.warnings) + warns
        return cap

    available = ", ".join(sorted(caps)) or "（无）"
    raise ConfigError(
        f"找不到能力 '{text}'。可用：{available}；也可直接给目录路径，"
        f"或用 default 使用仓库内置的通用五阶段 skill"
    )


def describe(caps: dict[str, Capability]) -> list[str]:
    """给 --check / 日志用的能力清单。"""
    out = []
    for name in sorted(caps):
        c = caps[name]
        tag = "完整" if c.complete else f"缺 {','.join(c.missing())}"
        ver = f" v{c.version}" if c.version else ""
        out.append(f"{name}{ver}（{tag}）· {c.root}")
    return out
