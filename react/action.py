"""Action registry: 5 cognitive-phase slots, bound to SKILL.md via directory convention.

每个槽位对应 ReAct 循环中的一个认知阶段。启动时扫描 skills/<槽位名>/SKILL.md，
存在则绑定（正文注入为该步骤的系统提示），不存在则用内置默认提示词。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Action:
    name: str                       # think / plan / act / observe / verify
    skill_path: Path | None = None  # 绑定的 SKILL.md 路径；None = 内置默认
    skill_body: str = ""            # 注入的系统提示正文

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
        "如需真实执行（跑命令 / 写文件），可在 [RESULT] 前追加执行块，框架在启用执行器时"
        "真实执行并把回显交给 OBSERVE：\n"
        "[EXEC: shell] + 围栏代码块（要执行的命令）；或\n"
        "[EXEC: write] + 新格式块（path: 相对路径 + ---BEGIN---/---END--- 围栏，内容无需转义；旧 JSON 格式仍兼容）。\n"
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
        """扫描 skills_root/<槽位名>/SKILL.md，构建全部槽位。"""
        for name in ACTION_NAMES:
            skill_md = skills_root / name / "SKILL.md"
            if skill_md.is_file():
                raw = skill_md.read_text(encoding="utf-8", errors="replace")
                skill_name, body = parse_skill_md(raw, fallback_name=name)
                if skill_name != name:
                    self.warnings.append(
                        f"槽位 {name}: skill 自报名称 '{skill_name}' 与目录名不一致，按目录名绑定"
                    )
                self.actions[name] = Action(
                    name=name, skill_path=skill_md, skill_body=body.strip()
                )
            else:
                self.actions[name] = Action(
                    name=name, skill_path=None, skill_body=DEFAULT_PROMPTS[name]
                )

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
        """返回 [(槽位, 绑定描述)]，用于 /binds 与启动横幅。"""
        out = []
        for name in ACTION_NAMES:
            a = self.actions[name]
            out.append((name, f"skill: {a.skill_path.parent.name}" if a.bound else "内置默认"))
        return out
