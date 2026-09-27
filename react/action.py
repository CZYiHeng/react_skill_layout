"""Action registry: 5 cognitive-phase slots, bound to SKILL.md via directory convention.

每个槽位对应 ReAct 循环中的一个认知阶段。启动时扫描 skills/<槽位名>/SKILL.md，
存在则绑定（正文注入为该步骤的系统提示），不存在则用内置默认提示词。

**槽位内可放多个 skill（变体）**：`<槽位>/SKILL.md` 是该槽位的默认行为，
`<槽位>/<变体名>/SKILL.md` 是可独立抽出/替换的额外 skill，运行时按配置选用其中一个：

    skills/act/SKILL.md             ← 默认
    skills/act/strict-code/SKILL.md ← 变体，名字 = 子目录名

选中的正文会**替换**该槽位的提示，五个槽位各自独立。每步只注入当前槽位那一份，
所以加变体不会增加单次请求的 token。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

#: 默认变体的保留名（等同槽位根下的 SKILL.md）；子目录不得占用。
DEFAULT_VARIANT = "default"


@dataclass
class Action:
    name: str                       # think / plan / act / observe / verify
    skill_path: Path | None = None  # 绑定的 SKILL.md 路径；None = 内置默认
    skill_body: str = ""            # 注入的系统提示正文
    active_variant: str = DEFAULT_VARIANT   # 当前生效的变体名
    variants: dict[str, Path] = field(default_factory=dict)  # 可用变体 → SKILL.md 路径

    @property
    def bound(self) -> bool:
        return self.skill_path is not None


# ---------------------------------------------------------------------------
# SKILL.md 解析
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def parse_skill_md(raw: str, fallback_name: str) -> tuple[str, str]:
    """解析 SKILL.md，返回 (skill_name, body)。

    frontmatter 损坏时降级：全文作为正文，name 取目录名，不抛异常。
    """
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return fallback_name, raw
    name = fallback_name
    for line in m.group(1).splitlines():
        line = line.strip()
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip('"\'') or fallback_name
            break
    return name, raw[m.end():]


# ---------------------------------------------------------------------------
# 内置默认系统提示（附录 A 原则的完整版，未绑定 skill 时开箱可用）
# ---------------------------------------------------------------------------

DEFAULT_PROMPTS: dict[str, str] = {
    "think": (
        "你是 ReAct 循环中的 THINK 阶段。分析当前任务与历史轨迹，决定下一步动作。\n"
        "必须调用 decide_next_step 工具给出决策（框架已提供该工具）：\n"
        "  decision 取 PLAN | ACT | DONE | VERIFY | ASK | ESCALATE；\n"
        "  reason 写决策理由（1-2 句）；thought 写现状分析（选填）。\n"
        "思考路径（每轮按顺序，不跳步）：\n"
        "1. 回顾历史：进展到哪、已有哪些产物和判定\n"
        "2. 连问五题：缺信息?→ASK 做不动?→ESCALATE 已达成?→DONE/VERIFY "
        "可直接执行?→ACT 需多步拆解?→PLAN\n"
        "3. 暴露前提：未验证的默认假设、推进隐患\n"
        "4. 权衡候选：比较至少两个候选决策的代价，写入 reason\n"
        "5. 任务开放或模糊时追加意图检查：字面要什么？背后想解决什么？一致吗？\n"
        "只做分析决策，不要产出方案正文。"
    ),
    "plan": (
        "你是 ReAct 循环中的 PLAN 阶段。把任务拆解为编号步骤列表，每步附带可核对的完成标准。\n"
        "输出格式（严格遵守）：\n"
        "[PLAN]\n"
        "1. <步骤一句话，可独立执行> | 完成标准：<可核对的标准>\n"
        "2. ...\n"
        "要求：步骤 2-6 个，按依赖顺序；完成标准必须可核对——"
        "写'包含什么/满足什么条件/输出什么结构'，不写'做好/完善/合理'这类无法判定的词；"
        "标准是后续 ACT 自查与 OBSERVE 核对的依据。"
    ),
    "act": (
        "你是 ReAct 循环中的 ACT 阶段。产出当前步骤的完整产物。\n"
        "输出格式（严格遵守）：\n"
        "[CHECK] 本步骤成功标准：<引用当前步骤的完成标准，可精炼>\n"
        "[RESULT] <本步骤的完整产物：方案 / 代码片段 / 分析结论>\n"
        "要求：[CHECK] 必须在 [RESULT] 之前（优先引用计划标准，计划未给时自立）；"
        "宁可详尽不可残缺。\n"
        "如产物适合结构化展示，可在 [RESULT] 前加一行声明："
        "[FORMAT: table|json|code|plan|diff|md]；不声明则自动识别，不强制要求。\n"
        "需要读写文件或跑命令时，直接调用框架提供的工具（read/write/edit/grep/glob/shell），"
        "工具可连续多次调用（如 先 read → 再 edit → 再 read 核对），框架自动循环执行并把回显交给 OBSERVE。\n"
        "无法调用工具时可退回文本协议：[EXEC: read] + path 行 / [EXEC: write] + path+---BEGIN---/---END--- 围栏 / "
        "[EXEC: edit] + path+---OLD---/---NEW---/---END--- / [EXEC: grep] + pattern 行 / [EXEC: glob] + pattern 行 / [EXEC: shell] + 命令。\n"
        "执行器未启用时会被拒绝，你仍应给出 [RESULT] 供人工取用。"
    ),
    "observe": (
        "你是 ReAct 循环中的 OBSERVE 阶段。核对上一步 ACT 产物是否满足成功标准。\n"
        "依次对照【完成标准（计划）】【成功标准自述（ACT 的 CHECK）】【待核对产物】；"
        "两者冲突时以计划标准为准并指出。\n"
        "必须调用 submit_verdict 工具给出判定（框架已提供该工具）：\n"
        "  pass=满足标准（循环推进）；defect=有具体错误，reason 写清哪里错、错在哪"
        "（循环带说明回 ACT 修正）；retry=不完整/未响应步骤，reason 说明缺什么（原样重跑）。\n"
        "你的判定是建议性自检，用户可在 gate 否决——如实判定，不必讨好。"
    ),
    "verify": (
        "你是 ReAct 循环中的 VERIFY 阶段。对照任务最初目标做最终验收。\n"
        "必须调用 submit_verdict 工具给出结论（框架已提供该工具）："
        "verdict=pass 表示达成、fail 表示未达成，reason 写明验收依据。"
    ),
}

ACTION_NAMES = tuple(DEFAULT_PROMPTS.keys())


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

@dataclass
class ActionRegistry:
    actions: dict[str, Action] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def load(self, skills_root: Path) -> None:
        """扫描 skills_root/<槽位名>/SKILL.md，构建全部槽位。

        同时收集**槽位内变体** `<槽位名>/<变体名>/SKILL.md`（见模块 docstring）。
        默认变体的加载路径与结果**逐字节未变**——不配置变体时行为与从前完全一致。
        """
        for name in ACTION_NAMES:
            slot_dir = skills_root / name
            variants = self._scan_variants(name, slot_dir)
            default_md = slot_dir / "SKILL.md"
            if default_md.is_file():
                variants[DEFAULT_VARIANT] = default_md
                skill_name, body = self._read_skill(default_md, fallback_name=name)
                if skill_name != name:
                    self.warnings.append(
                        f"槽位 {name}: skill 自报名称 '{skill_name}' 与目录名不一致，按目录名绑定"
                    )
                self.actions[name] = Action(
                    name=name, skill_path=default_md, skill_body=body.strip(),
                    active_variant=DEFAULT_VARIANT, variants=variants,
                )
            else:
                self.actions[name] = Action(
                    name=name, skill_path=None, skill_body=DEFAULT_PROMPTS[name],
                    active_variant=DEFAULT_VARIANT, variants=variants,
                )

    def _scan_variants(self, slot: str, slot_dir: Path) -> dict[str, Path]:
        """收集 `<槽位>/<变体名>/SKILL.md`。缺 SKILL.md 或占用保留名 → 告警并跳过。"""
        out: dict[str, Path] = {}
        if not slot_dir.is_dir():
            return out
        for child in sorted(slot_dir.iterdir()):
            if not child.is_dir():
                continue
            if child.name == DEFAULT_VARIANT:
                self.warnings.append(
                    f"槽位 {slot}: 子目录 '{DEFAULT_VARIANT}' 是保留名（等同槽位根下的 "
                    f"SKILL.md），已忽略"
                )
                continue
            md = child / "SKILL.md"
            if not md.is_file():
                # 子目录可能是脚本/参考资料目录，不强制报错，但要让人知道它没被当成 skill
                self.warnings.append(
                    f"槽位 {slot}: 子目录 '{child.name}' 下没有 SKILL.md，未作为 skill 载入"
                )
                continue
            out[child.name] = md
        return out

    def _read_skill(self, md: Path, fallback_name: str) -> tuple[str, str]:
        raw = md.read_text(encoding="utf-8", errors="replace")
        return parse_skill_md(raw, fallback_name=fallback_name)

    def set_variant(self, slot: str, variant: str) -> str:
        """切换某槽位生效的 skill 变体，返回实际生效的变体名。

        - `""` / `"default"` / `"_"` → 回退默认（槽位根下的 SKILL.md；没有则内置默认提示词）
        - 未知变体名 → 抛 KeyError 并在消息里列出可选值（与 `get()` 的报错风格一致）

        注：默认变体是**重新从磁盘读**的，所以外部改了 SKILL.md 后调用本方法能拿到新内容；
        但它不会重扫子目录（新增变体需要重新 `load()`）。
        """
        action = self.get(slot)
        name = (variant or "").strip()
        if name in ("", DEFAULT_VARIANT, "_"):
            # 默认变体的「自报名称 ≠ 槽位名」告警已在 load() 里报过，此处不重复
            md = action.variants.get(DEFAULT_VARIANT)
            if md is not None:
                _, body = self._read_skill(md, fallback_name=slot)
                action.skill_path, action.skill_body = md, body.strip()
            else:
                action.skill_path, action.skill_body = None, DEFAULT_PROMPTS[slot]
            action.active_variant = DEFAULT_VARIANT
            return DEFAULT_VARIANT

        md = action.variants.get(name)
        if md is None:
            available = ", ".join(sorted(action.variants)) or "（无）"
            raise KeyError(f"槽位 {slot} 没有变体 '{name}'，可选: {available}")
        action.skill_path, action.skill_body = md, self._load_variant(slot, md, name)
        action.active_variant = name
        return name

    def _load_variant(self, slot: str, md: Path, expect_name: str) -> str:
        """读一个变体的正文；自报名称与目录名不符时告警（只读一次文件）。"""
        skill_name, body = self._read_skill(md, fallback_name=expect_name)
        if skill_name != expect_name:
            self.warnings.append(
                f"槽位 {slot}: skill 自报名称 '{skill_name}' 与目录名 '{expect_name}' 不一致，"
                f"按目录名绑定"
            )
        return body.strip()

    def get(self, name: str) -> Action:
        if name not in self.actions:
            raise KeyError(f"未知动作槽位: {name}，可选: {', '.join(ACTION_NAMES)}")
        return self.actions[name]

    def bind(self, action_name: str, skill_dir: Path, skills_root: Path) -> Path:
        """--bind 子命令：把 skill_dir 复制为 skills/<action_name>/（整体替换）。"""
        target = skills_root / action_name
        if not skill_dir.is_dir():
            raise FileNotFoundError(f"skill 目录不存在: {skill_dir}")
        if not (skill_dir / "SKILL.md").is_file():
            raise FileNotFoundError(f"skill 目录缺少 SKILL.md: {skill_dir}")
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(skill_dir, target)
        return target

    def bind_status(self) -> list[tuple[str, str]]:
        """返回 [(槽位, 绑定描述)]，用于 /binds 与启动横幅。

        有变体时显示 `skill: <变体名>`；同时给出可选变体数，便于发现"槽位里还有别的 skill"。
        """
        out = []
        for name in ACTION_NAMES:
            a = self.actions[name]
            if not a.bound:
                out.append((name, "内置默认"))
                continue
            extra = len([v for v in a.variants if v != DEFAULT_VARIANT])
            suffix = f"（另有 {extra} 个变体可选）" if extra else ""
            out.append((name, f"skill: {a.active_variant}{suffix}"))
        return out

    def variants_of(self, slot: str) -> list[str]:
        """某槽位可选的变体名（含 default），供前端/CLI 展示。"""
        return sorted(self.get(slot).variants)
