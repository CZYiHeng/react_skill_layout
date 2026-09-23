"""ReAct 主循环：THINK 驱动 → [PLAN] → ACT → OBSERVE → [VERIFY] 状态机。

循环规则（DESIGN.md §3.4）：
- THINK 是驱动槽位：每轮先跑 THINK，由其决策行（6 值）决定循环走向
- 决策词汇表：PLAN / ACT / DONE / VERIFY / ASK / ESCALATE
  - ASK：渲染提问面板 → 阻塞等用户回答 → 答案注入历史 → 不计入轮数预算 → 继续循环
  - ESCALATE：带理由移交人工，status="escalated"
- 非 ASK 的 THINK 之后才走人工 gate（c/s/q）；ASK 直接接回答输入
- OBSERVE 非"通过"累计 3 次 → 强制下一轮 THINK 重新出 PLAN（防原地打转）
- 达到 max_rounds 强制退出
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .action import ActionRegistry
from .context import SessionContext
from .executor import Executor
from .model import ModelClient
from .render import Renderer  # 协议唯一定义处（此前本文件有一份残缺副本）


# ---- 输出解析 ----

def parse_tag(raw: str, tag: str) -> str:
    """提取 [TAG] 之后的正文；标签缺失时返回全文（降级不抛异常）。"""
    m = re.search(rf"\[{tag}\]\s*", raw, re.IGNORECASE)
    return raw[m.end():].strip() if m else raw.strip()


def parse_decision(raw: str) -> tuple[str, bool]:
    """解析 THINK 的 '下一步: PLAN|ACT|DONE|VERIFY|ASK|ESCALATE'。

    返回 (决策, 是否明确命中)。歧义(未命中)时安全默认 'ESCALATE'(移交人工)，
    而非盲目 'ACT'——fail-safe：解析不到合法决策时把控制权交还人，不替人做动作。
    """
    m = re.search(r"下一步[:：]\s*(PLAN|ACT|DONE|VERIFY|ASK|ESCALATE)", raw, re.IGNORECASE)
    if m:
        return m.group(1).upper(), True
    return "ESCALATE", False


_STEP_RE = re.compile(
    r"^\s*\d+[.、)]\s*(.+?)(?:\s*[|｜]\s*完成标准\s*[:：]\s*(.+?))?\s*$",
    re.MULTILINE,
)


def parse_plan_steps(raw: str) -> list[tuple[str, str]]:
    """解析 [PLAN] 的编号步骤列表，返回 [(步骤, 完成标准)]；无标准段时标准为空串。"""
    body = parse_tag(raw, "PLAN")
    steps: list[tuple[str, str]] = []
    for m in _STEP_RE.finditer(body):
        step = m.group(1).strip()
        criteria = (m.group(2) or "").strip()
        if step:
            steps.append((step, criteria))
    return steps


def parse_check(raw: str) -> str:
    """提取 ACT 的 [CHECK] 成功标准自述；未声明返回空串（框架软兜底）。"""
    m = re.search(r"\[CHECK\]\s*(.+?)(?=\[RESULT\]|\Z)", raw, re.DOTALL)
    return m.group(1).strip() if m else ""


_EXEC_FENCE_RE = re.compile(
    r"```[a-zA-Z]*[ \t]*\n(.*?)\n```",
    re.DOTALL)

# 执行标记必须「另起一行」才算数。否则正文里引用一句
# 「方案：用 [EXEC: write] 落盘」也会被当成真的执行请求，解析出垃圾载荷。
_EXEC_RE = re.compile(r"^[ \t]*\[EXEC:\s*(shell|read|write|edit|grep|glob)\s*\]",
                      re.IGNORECASE | re.MULTILINE)
_SOLUTION_RE = re.compile(r"^[ \t]*\[\s*方案\s*\]",
                          re.IGNORECASE | re.MULTILINE)


def parse_exec_all(raw: str) -> list[tuple[str, str]]:
    """提取 ACT 中**全部**执行请求 [EXEC: shell|write]，按出现顺序返回。

    载荷优先取紧随其后的围栏代码块；无围栏则取到下一个 [EXEC 或 [RESULT] 之前（或文末）。
    一个 ACT 允许声明多个执行请求（例如一次写多个文件），调用方须逐个执行。
    """
    matches = list(_EXEC_RE.finditer(raw))
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        kind = m.group(1).lower()
        tail = raw[m.end():]
        fence = _EXEC_FENCE_RE.search(tail)
        if fence:
            payload = fence.group(1).strip()
        else:
            # 无围栏时，截到下一个 EXEC 块或 [RESULT] 之前，避免吞掉后续请求
            nxt = re.search(r"\[EXEC:|\[RESULT\]", tail, re.IGNORECASE)
            payload = (tail[:nxt.start()] if nxt else tail).strip()
        if payload:
            out.append((kind, payload))
    return out


def parse_solution(raw: str) -> str:
    """提取 ACT 的方案确认块 [方案]（solution-review 三维度）。

    块内应说明：目的 / 约束 / 方案 / 预期 / 与备选的对比及优缺点。
    仅在「本步骤存在方案选择空间」时由模型给出，因此可能为空。
    """
    m = _SOLUTION_RE.search(raw)
    if not m:
        return ""
    tail = raw[m.end():]
    fence = _EXEC_FENCE_RE.search(tail)
    if fence:
        return fence.group(1).strip()
    stop = re.search(r"\[(?:RESULT|CHECK|EXEC|FORMAT)", tail, re.IGNORECASE)
    return (tail[:stop.start()] if stop else tail).strip()


def parse_exec(raw: str) -> tuple[str, str] | None:
    """保留：取**首个**执行请求（向后兼容既有调用方与断言）。

    需要全部请求请用 parse_exec_all——否则一个 ACT 里声明的多个写入会被静默丢弃。
    """
    items = parse_exec_all(raw)
    return items[0] if items else None


# 判定文本兜底的同义词分组（工具调用已结构化，无需此表；仅文本回退时用）
# 顺序敏感：失败词必须先于通过词，否则"未通过"/"not pass"会误判为正向
# 匹配前 body 统一转小写；英文不用 "found" 形式（避免 "no issues found" 被误判），
# 不用 "error"/"issue" 等短词单用（避免 "no error" 被误判），统一用 has/with/contains 短语
_VERDICT_FAIL_WORDS = (
    # 中文
    "不通过", "未通过", "没通过", "不合格", "未达标", "不达标",
    "不符合", "未满足", "不满足", "有误", "有问题", "失败", "未达成",
    # 英文 — 否定/失败
    "fail", "failed", "failure", "fails", "failing",
    "reject", "rejected", "rejection", "rejects", "rejecting",
    "not pass", "not passed", "does not pass", "did not pass",
    "cannot pass", "can't pass", "won't pass", "would not pass",
    "invalid", "incorrect", "unmet", "unverified",
    "not met", "not satisfied", "unsatisfactory", "unacceptable",
    "does not meet", "did not meet", "fail to meet", "fails to meet",
    "failed to meet", "failing to meet",
    "does not conform", "did not conform", "non-conform", "nonconform",
    # 英文 — 存在问题（has/with/contains 短语，避免 "no error" 否定式误判；
    # detected 形式只放缺陷词组，"检测到问题"更偏向需修正而非直接不通过）
    "has error", "have error", "with error",
    "contains error",
    "has defect", "have defect", "with defect",
    "contains defect",
    "has issue", "have issue", "with issue",
    "has problem", "have problem", "with problem",
    "must fix", "need fix", "needs fix", "must correct",
)
_VERDICT_DEFECT_WORDS = (
    # 中文
    "缺陷", "有缺陷", "需修正", "有错误", "不完整",
    # 英文 — defective / 存在问题短语 / 需修正（与失败词不重叠，失败词优先匹配）
    "defective",
    "has defect", "have defect", "with defect", "defect detected",
    "contains defect", "defects detected",
    "has issue", "have issue", "with issue", "issue detected",
    "issues detected",
    "has problem", "have problem", "with problem", "problem detected",
    "problems detected",
    "incorrect", "wrong", "incomplete",
    "need fix", "needs fix", "need correction", "correction needed", "needs correction",
    "should fix", "should correct",
)
_VERDICT_RETRY_WORDS = (
    # 中文
    "重试", "重跑", "重做", "重新执行", "再跑",
    # 英文
    "retry", "retries", "retried", "retrying",
    "redo", "redid", "redoing", "re-do", "re-doing",
    "rerun", "re-run", "rerunning", "re-running",
    "try again", "run again", "execute again", "do again",
    "re-execute", "re-executing", "reexecuting",
)
_VERDICT_PASS_WORDS = (
    # 中文
    "通过", "合格", "达标", "满足", "符合", "验收合格", "通过验收", "可接受",
    # 英文
    "pass", "passed", "passing",
    "accept", "accepted", "acceptable", "accepting",
    "success", "successful", "successfully",
    "satisfy", "satisfied", "satisfies", "satisfying",
    "conform", "conforms", "conformed", "conforming",
    "verified", "validation passed", "check passed", "test passed", "review passed",
    "criteria met", "requirement met", "requirements met",
    "all criteria met", "all requirements met",
    "no issue", "no issues", "no problem", "no problems",
    "no error", "no errors", "no defect", "no defects",
    "good to go", "ready to proceed", "ready for release",
    "all good", "looks good",
)


def parse_verdict(raw: str) -> tuple[str, bool]:
    """解析 OBSERVE/VERIFY 的判定：通过 / 缺陷 / 重试 / 不通过。

    返回 (判定, 是否明确命中)。歧义(未命中任何关键字)时安全默认 '不通过'，
    而非盲目 '通过'——fail-safe：解析不到合法判定时一律当未达成，触发返工/人工，
    绝不让错误产物被静默当成通过。VERIFY 的'不通过'返回独立值，不再误标为'缺陷'。

    v1.4 扩展文本兜底同义词（缺口 f），并修正"未通过/没通过"此前被 '通过' 子串
    误判为正向的缺陷：失败词优先于通过词匹配。
    """
    body = parse_tag(raw, "OBSERVATION") if "OBSERVATION" in raw.upper() else parse_tag(raw, "VERIFY")
    body = body.lower()  # 统一小写，英文同义词匹配不区分大小写
    if any(w in body for w in _VERDICT_FAIL_WORDS):
        return "不通过", True
    if any(w in body for w in _VERDICT_DEFECT_WORDS):
        return "缺陷", True
    if any(w in body for w in _VERDICT_RETRY_WORDS):
        return "重试", True
    if any(w in body for w in _VERDICT_PASS_WORDS):
        return "通过", True
    return "不通过", False


# 自我修正提示：未拿到合法结构化结论时追加给模型，要求调用工具重新给出
_REPAIR_HINTS = {
    "think": "你尚未给出合法决策。请立即调用 decide_next_step 工具，"
             "decision 取 PLAN|ACT|DONE|VERIFY|ASK|ESCALATE 之一。",
    "observe": "你尚未给出合法判定。请立即调用 submit_verdict 工具，"
               "verdict 取 pass|defect|retry 之一，reason 写明依据。",
    "verify": "你尚未给出合法验收结论。请立即调用 submit_verdict 工具，"
              "verdict 取 pass|fail 之一，reason 写明依据。",
}
_REPAIR_MAX = 2  # 每个歧义步最多自我修正次数
_MAX_ASK_TURNS = 6  # ASK 轮次独立上限，防止模型反复提问死循环（缺口 g）
_MISS_LIMIT = 3  # OBSERVE 连续非通过达此数 → 强制下一轮 THINK 重出 PLAN（防原地打转）


# ---- 原生工具调用：控制信号结构化（免正则，抗格式漂移） ----

_DECISIONS = ("PLAN", "ACT", "DONE", "VERIFY", "ASK", "ESCALATE")

# 工具 verdict 值 → 循环内部判定词
_VERDICT_MAP = {
    "pass": "通过", "通过": "通过",
    "defect": "缺陷", "缺陷": "缺陷",
    "retry": "重试", "重试": "重试",
    "fail": "不通过", "不通过": "不通过", "未通过": "不通过",
}

DECIDE_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "decide_next_step",
        "description": "THINK 阶段必须调用：给出 ReAct 循环的下一步动作。",
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": list(_DECISIONS),
                             "description": "下一步动作"},
                "reason": {"type": "string", "description": "决策理由（1-2 句）"},
                "thought": {"type": "string", "description": "现状分析（选填）"},
            },
            "required": ["decision", "reason"],
        },
    },
}

VERDICT_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "submit_verdict",
        "description": "OBSERVE/VERIFY 阶段必须调用：给出对产物的判定。",
        "parameters": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["pass", "defect", "retry", "fail"],
                    "description": "pass=满足标准; defect=有具体缺陷需修正; "
                                   "retry=产物不完整需重跑; fail=验收不通过",
                },
                "reason": {"type": "string", "description": "判定说明（依据 / 缺陷 / 缺什么）"},
            },
            "required": ["verdict", "reason"],
        },
    },
}

# ---- 文件操作原生工具（对标 Claude Code：Read/Grep/Glob/Write/Edit/Bash） ----
# ACT 阶段注入给模型，模型直接调用，框架执行并把结果以 role=tool 回写历史。
# 文本协议 [EXEC: ...] 保留为兜底（kimi 等模型工具调用可能缺失）。

_ACT_TOOL_DESC = {
    "read": "读取文件内容（带行号，cat -n 风格）。path 相对工作目录。"
            "默认最多读 2000 行、单行截断 2000 字符；大文件用 offset/limit 翻页。"
            "修改文件前必须先 read。",
    "write": "覆盖写入文件（新文件或整体替换）。path 相对工作目录，越界拒绝。"
             "小改动优先用 edit。",
    "edit": "精确字符串替换（只替换一处）。old_text 必须与文件内容完全匹配且唯一。"
            "比 write 安全，适合小改动。",
    "grep": "正则搜索文件内容，返回 file:line:content。pattern 用 Python 正则；"
            "可选 path 限定搜索目录（默认整个工作目录）。",
    "glob": "按通配模式列出文件（如 *.py、**/*.md）。path 相对工作目录。",
    "shell": "执行 shell 命令（跑测试 / git / 安装依赖等非文件操作）。"
             "Windows 用 cmd 语法，不要用 nohup/find/heredoc。",
}

FILE_TOOLS: list[dict] = [
    {"type": "function", "function": {
        "name": name,
        "description": desc,
        "parameters": {"type": "object", "properties": props, "required": req},
    }}
    for name, desc, props, req in [
        ("read", _ACT_TOOL_DESC["read"],
         {"path": {"type": "string", "description": "文件相对路径"},
          "offset": {"type": "integer", "description": "起始行号（1 起），配合 limit 翻页"},
          "limit": {"type": "integer", "description": "读取行数上限（默认 2000）"}},
         ["path"]),
        ("write", _ACT_TOOL_DESC["write"],
         {"path": {"type": "string", "description": "文件相对路径"},
          "content": {"type": "string", "description": "完整文件内容"}},
         ["path", "content"]),
        ("edit", _ACT_TOOL_DESC["edit"],
         {"path": {"type": "string", "description": "文件相对路径"},
          "old_text": {"type": "string", "description": "要替换的旧文本（必须唯一匹配）"},
          "new_text": {"type": "string", "description": "替换后的新文本"}},
         ["path", "old_text", "new_text"]),
        ("grep", _ACT_TOOL_DESC["grep"],
         {"pattern": {"type": "string", "description": "Python 正则表达式"},
          "path": {"type": "string", "description": "搜索目录/文件（默认工作目录）"}},
         ["pattern"]),
        ("glob", _ACT_TOOL_DESC["glob"],
         {"pattern": {"type": "string", "description": "通配模式，如 *.py 或 **/*.md"}},
         ["pattern"]),
        ("shell", _ACT_TOOL_DESC["shell"],
         {"command": {"type": "string", "description": "要执行的命令"}},
         ["command"]),
    ]
]

# 工具循环防死循环上限（对标 Claude 的自动工具使用，但加护栏）
_TOOL_LOOP_MAX = 12


def render_tool_text(action: str, name: str, args: dict | None) -> str:
    """把工具调用渲染成人类可读文本（用于展示与历史账本，保持轨迹可见）。"""
    args = args or {}
    if name == "decide_next_step":
        head = args.get("thought") or args.get("reason", "")
        return f"[THOUGHT] {head}\n下一步: {args.get('decision', '')}"
    if name == "submit_verdict":
        return f"[{action.upper()}] {args.get('verdict', '')}：{args.get('reason', '')}"
    return f"[TOOL {name}] {json.dumps(args, ensure_ascii=False)}"


# ---- 渲染与人工交互的抽象接口（由调用方注入实现；协议见 render.py） ----

# gate 返回：("continue", None) | ("abort", None) | ("steer", 纠偏文本)
Gate = Callable[[str], tuple[str, str | None]]
# ask 返回用户回答；返回 None 表示提问被中断（按中止处理）
Ask = Callable[[str], str | None]


@dataclass
class StepOutput:
    action: str
    raw: str
    parsed: str
    elapsed_sec: float
    tokens: int
    reasoning: str = ""    # 思考过程（仅部分模型提供，如 kimi 的 reasoning_content）
    tool_name: str = ""    # 本步调用的工具名（无则空串）
    tool_args: dict | None = None  # 工具参数（已解析；无则 None）
    usage: dict | None = None  # {prompt, completion, total}


@dataclass
class LoopResult:
    status: str            # done / aborted / max_rounds_exceeded / escalated
    rounds: int
    final_text: str


@dataclass
class ReActLoop:
    registry: ActionRegistry
    context: SessionContext
    model: ModelClient
    render: Renderer
    plan_model: ModelClient | None = None  # 计划阶段专用模型（推理模型）；None=回落到 model
    gate: Gate | None = None          # None = 自动继续（--smoke 场景）
    ask: Ask | None = None            # None = ASK 用占位回答（自动场景）
    executor: Executor | None = None  # ACT 执行器；None = 不执行（纯文本产物）
    #: 人工闸门档位：plan=计划批准一次（默认）/ step=每步骤一次 / auto=仅必须拦时
    #: / phase=每阶段一次（旧行为，回滚开关）
    gate_mode: str = "plan"
    #: 非阻塞查看是否有待处理的人工指令（随时插手：暂停 / 纠偏 / 中止）
    interrupt: Callable[[], tuple[str, str | None] | None] | None = None
    force_plan: bool = field(default=False, init=False)
    # 修正反馈：OBSERVE 判缺陷/重试时生成，下轮 ACT 消费（空串=无待处理反馈）
    feedback: str = field(default="", init=False)

    # 连续非通过计数（OBSERVE 防打转护栏）
    _misses: int = field(default=0, init=False)
    _aborted: bool = field(default=False, init=False)
    # ASK 累计计数（独立上限护栏，缺口 g）
    _ask_count: int = field(default=0, init=False)
    # 最近一次 ACT 阶段的 [RESULT] 产物（VERIFY 通过时作为 final_text，避免取到工具回执）
    last_act_result: str = field(default="", init=False)
    # 原生工具循环标记：_step 执行了真实工具时置 True（ACT 循环据此继续）
    _tool_loop_pending: bool = field(default=False, init=False)

    # ------------------------------------------------------------------

    def run(self, user_input: str) -> LoopResult:
        self.context.reset()
        self.context.add_user(user_input)
        self._misses = 0
        self._aborted = False
        self._ask_count = 0
        self.force_plan = False
        self.feedback = ""
        self.last_act_result = ""

        while self.context.round_no < self.context.max_rounds:
            self.context.round_no += 1
            self.render.round_banner(self.context.round_no)

            # --- THINK：驱动槽位，决策在 run 层处理（ASK 需要跳过 gate） ---
            think_prompt = "请分析当前状态并决策下一步。"
            if self.force_plan:
                think_prompt += "（注意：上一阶段已连续多次未通过，你必须选择 PLAN 重新规划。）"
                self.force_plan = False
            think_out = self._step("think", think_prompt, run_gate=False,
                                   tools=[DECIDE_TOOL])
            if self._aborted:
                return LoopResult("aborted", self.context.round_no, "人工中止")
            decision = self._resolve("think", think_out, self._decision_extract,
                                     tools=[DECIDE_TOOL])

            if decision == "ASK":
                self._ask_count += 1
                if self._ask_count > _MAX_ASK_TURNS:
                    return LoopResult("escalated", self.context.round_no,
                                      f"模型反复提问超过 {_MAX_ASK_TURNS} 次，移交人工"
                                      "（请直接补充所需信息后再试）")
                # 提问面板只展示问题本体，去掉决策行
                question = re.sub(r"下一步[:：].*$", "", think_out.parsed,
                                  flags=re.MULTILINE).strip()
                self.render.ask_question(question)
                answer = self.ask(question) if self.ask is not None else "（自动回答：继续）"
                if answer is None:
                    return LoopResult("aborted", self.context.round_no, "提问被中断")
                self.context.add_user(answer)
                self.context.round_no -= 1  # ASK 轮不计入预算（对话不耗轮数）
                continue
            if decision == "ESCALATE":
                return LoopResult("escalated", self.context.round_no, think_out.parsed)

            # phase 档位（旧行为回滚）：THINK 之后也要拦，其余阶段由 _step 内部拦
            if self._is_phase_mode():
                self._apply_gate("think")
                if self._aborted:
                    return LoopResult("aborted", self.context.round_no, "人工中止")

            if decision in ("DONE", "VERIFY"):
                vout = self._step_verify()
                if self._aborted:
                    return LoopResult("aborted", self.context.round_no, "人工中止")
                verdict = self._resolve("verify", vout, self._verdict_extract,
                                        tools=[VERDICT_TOOL])
                # 终检验收必拦：人工可在此纠偏，纠偏后不许直接 done
                if self._apply_gate_if_needed("verify", verdict, reason="最终验收"):
                    verdict = "不通过"
                if self._aborted:
                    return LoopResult("aborted", self.context.round_no, "人工中止")
                if verdict == "通过":
                    final_text = self.last_act_result
                    if not final_text:
                        # 未经过 ACT 的简单任务：回退到最后一条 assistant 文本消息（跳过 role=tool 回执）
                        for _m in reversed(self.context.messages):
                            if _m.get("role") == "assistant" and _m.get("content"):
                                final_text = _m["content"]
                                break
                    return LoopResult("done", self.context.round_no, final_text)
                # 验收不通过：注入反馈回炉修正，max_rounds 兜底（缺口 h 修复）
                self.context.add_user(
                    "最终验收未通过。验收依据：" + vout.parsed +
                    "\n你必须选择 ACT 或 PLAN 修正产物，不得直接 DONE。"
                )
                continue

            # --- PLAN（可选）→ ACT → OBSERVE ---
            self._round_rest(decision)
            if self._aborted:
                return LoopResult("aborted", self.context.round_no, "人工中止")
        return LoopResult("max_rounds_exceeded", self.context.round_no,
                          f"达到最大轮数 {self.context.max_rounds}，请人工接管")

    # ------------------------------------------------------------------
    # ACT：原生工具循环（对标 Claude Code 的自由工具调用）
    # ------------------------------------------------------------------

    def _act(self, act_prompt: str) -> StepOutput:
        """ACT 阶段支持原生工具调用：模型可连续调用 read/write/edit/grep/glob/shell，
        每次执行结果以 role=tool 回写历史，直到模型产出无工具调用的最终产物。

        未绑定执行器时不注入工具（纯文本产物，等价原 _step("act")）。
        """
        handler = None
        if self.executor is not None:
            handler = lambda name, args: self.executor.run_tool(name, args)  # noqa: E731
        last = None
        for _ in range(_TOOL_LOOP_MAX):
            self._tool_loop_pending = False
            last = self._step("act", act_prompt, run_gate=False,
                              tools=FILE_TOOLS, tool_handler=handler)
            if self._aborted:
                return last
            if not self._tool_loop_pending:
                # 产物定型：phase 档位（旧行为）在 ACT 收尾拦一次
                if self._is_phase_mode():
                    self._apply_gate("act")
                return last
            # 有工具执行：循环继续，模型基于工具结果产出/继续调用
        self.render.warn(f"工具循环超过 {_TOOL_LOOP_MAX} 次上限，以最后输出为准")
        return last

    # ------------------------------------------------------------------
    # 单轮执行段：PLAN（可选）→ ACT → OBSERVE
    # ------------------------------------------------------------------

    def _round_rest(self, decision: str) -> None:
        if decision == "PLAN":
            plan_out = self._step("plan", "请为当前任务制定执行计划。")
            if self._aborted:
                return
            steps = parse_plan_steps(plan_out.raw)
            # 中途修订保留已完成前缀，只替换剩余步骤（缺口 c 修复）
            revising = bool(self.context.plan) and not self.context.plan_done
            self.context.set_plan(steps, preserve_position=revising)
            if revising:
                self.feedback = ""  # 新计划新开始，旧修正反馈作废
            # 开跑前拦一次：人在计划阶段改方向最便宜。纠偏则下一轮强制重出计划
            if self._apply_gate_if_needed("plan", reason="计划已生成，请确认方向"):
                self.force_plan = True
                return

        step_desc = self.context.current_step()
        criteria = self.context.current_criteria()
        act_prompt = f"请执行当前步骤，产出完整产物。\n{step_desc}"
        if self.feedback:
            # 修正反馈先入历史账本（可追溯），ACT 请求经历史看到它
            self.context.add_user(self.feedback)
            self.feedback = ""  # 消费后清空；若仍不通过，OBSERVE 会生成新反馈
        act_out = self._act(act_prompt)
        if self._aborted:
            return
        result_text = act_out.parsed
        self.last_act_result = result_text
        check = parse_check(act_out.raw)

        # 方案确认：本步骤若有选择空间，模型应自证「为什么这么做」并留痕，
        # 写完之后继续执行——不打断人（用留痕替代逐步确认）
        solution = parse_solution(act_out.raw)
        if solution:
            self.render.solution(solution)

        # ACT 执行请求：绑定执行器则真实执行，回显交给 OBSERVE 观察（④ 落地）
        # 一次 ACT 可声明多个请求，必须逐个执行——只跑第一个会静默丢文件
        exec_note = ""
        exec_reqs = parse_exec_all(act_out.raw)
        exec_done = 0
        if exec_reqs:
            notes: list[str] = []
            total = len(exec_reqs)
            for i, (kind, payload) in enumerate(exec_reqs, 1):
                if self.executor is None:
                    notes.append(f"【执行回显 {i}/{total}（{kind}）】\n"
                                 "（未绑定执行器，未执行；仅文本产物）")
                    continue
                self.render.info(f"↳ 执行 {kind} ({i}/{total}) …")
                exec_out = self.executor.run(kind, payload)
                self.context.add_user(f"[执行回显 · {kind} {i}/{total}]\n{exec_out}")
                notes.append(f"【执行回显 {i}/{total}（{kind}）】\n{exec_out}")
                exec_done += 1
            if exec_done != total:
                self.render.warn(
                    f"声明了 {total} 个执行请求，实际执行 {exec_done} 个，请核对是否漏执行"
                )
            exec_note = "\n" + "\n".join(notes)

        # OBSERVE：计划标准是唯一真相源，ACT 的 [CHECK] 仅在计划未给标准时兜底
        # （此前两者并列喂入，等于让标准被生产两次、消费一次）
        obs_prompt = (
            "请核对待核对产物是否满足成功标准，给出判定。\n"
            f"【完成标准】{criteria or check or '（计划与 ACT 均未指定标准）'}\n"
            f"【本次声明的执行请求】{len(exec_reqs)} 个\n"
            f"【实际执行回显】{exec_done} 条\n"
            "★ 若两者数量不一致，说明产物未被完整执行，必须判定为 fail。\n"
            f"【待核对产物】\n{result_text}{exec_note}"
        )
        obs_out = self._step("observe", obs_prompt, tools=[VERDICT_TOOL])
        if self._aborted:
            return
        verdict = self._resolve("observe", obs_out, self._verdict_extract,
                                tools=[VERDICT_TOOL])

        if verdict == "通过":
            self._misses = 0
            self.context.advance_step()
        elif verdict in ("缺陷", "不通过"):
            self._misses += 1
            self.feedback = ("上一版产物存在缺陷，请修正后重新产出完整产物。\n"
                             "缺陷说明：" + obs_out.parsed)
            if self._misses >= _MISS_LIMIT:
                self.force_plan = True
                self._misses = 0
                self.feedback = ""
        else:  # 重试：产物不完整，原样重跑（不带具体缺陷说明）
            self._misses += 1
            self.feedback = "上一版产物不完整，未真正响应步骤要求，请重新产出完整产物。"
            if self._misses >= _MISS_LIMIT:
                self.force_plan = True
                self._misses = 0
                self.feedback = ""

        # 步骤收尾闸门：step 档每步拦一次，auto 档仅在判缺陷/不通过时拦
        self._apply_gate_if_needed("observe", verdict)

    # ------------------------------------------------------------------
    # VERIFY：收尾前终检
    # ------------------------------------------------------------------

    def _step_verify(self) -> StepOutput:
        return self._step("verify", "请对照任务最初目标做最终验收，给出结论。",
                          tools=[VERDICT_TOOL])

    # ------------------------------------------------------------------
    # 人工交互
    # ------------------------------------------------------------------

    def _is_phase_mode(self) -> bool:
        """是否为旧档位（每阶段都拦）——保留为回滚开关。"""
        return self.gate_mode == "phase"

    def _needs_human(self, action: str, verdict: str | None = None) -> bool:
        """「必须拦人」的时刻，与档位无关：终检验收 + OBSERVE 判缺陷/不通过。"""
        if action == "verify":
            return True
        return action == "observe" and verdict in ("缺陷", "不通过")

    def _should_gate(self, action: str, verdict: str | None = None) -> bool:
        """当前档位下，该时点是否需要拦人。

        plan（默认）：PLAN 出来后拦一次确认方向，之后只在「必须拦」的时刻出现
        step：每个计划步骤收尾（OBSERVE）+ 最终验收
        auto：连计划也不拦，只在「必须拦」的时刻出现
        任何档位下 ASK 都另行拦（走 ask 通道，不经过这里）
        """
        if self.gate is None:
            return False
        if self.gate_mode == "step":
            return action in ("observe", "verify")
        if self.gate_mode == "plan":
            return action == "plan" or self._needs_human(action, verdict)
        return self._needs_human(action, verdict)

    def _gate_reason(self, action: str, verdict: str | None = None) -> str:
        """给前端看的拦截原因。"""
        if action == "plan":
            return "计划已生成，请确认方向"
        if action == "verify":
            return "最终验收"
        if verdict in ("缺陷", "不通过"):
            return f"OBSERVE 判定「{verdict}」，需要你指示"
        return "本步骤已完成"

    def _check_interrupt(self) -> None:
        """每个阶段边界非阻塞看一眼有没有人工指令——「随时插手」的落点。

        - pause：就地阻塞等待（复用 gate 通道），人可在 GateBar 上继续/纠偏/中止
        - steer：把纠偏注入账本，从下一阶段起生效，不打断执行
        - abort：置中止标志
        """
        if self.interrupt is None or self._aborted:
            return
        pending = self.interrupt()
        if not pending:
            return
        cmd, text = pending
        if cmd == "abort":
            self._aborted = True
        elif cmd == "steer" and text:
            self.context.add_user(f"人工纠偏：{text}")
        elif cmd == "pause":
            self._apply_gate("interrupt", reason="你按了暂停")

    def _apply_gate_if_needed(self, action: str, verdict: str | None = None,
                              reason: str = "") -> bool:
        """按档位决定是否拦人；返回 True 表示人工做了纠偏。

        phase 档位已在 _step 内拦过，此处不再重复拦。
        """
        if self.gate is None or self._is_phase_mode():
            return False
        if not self._should_gate(action, verdict):
            return False
        before = len(self.context.messages)
        self._apply_gate(action, reason or self._gate_reason(action, verdict))
        return len(self.context.messages) > before or self._aborted

    def _apply_gate(self, action_name: str, reason: str = "") -> None:
        """非 ASK 步骤后的人工步进控制：c 继续 / s 纠偏 / q 中止。"""
        if self.gate is None:
            return
        cmd, correction = self.gate(action_name, reason)
        if cmd == "abort":
            self._aborted = True
        elif cmd == "steer" and correction:
            self.context.add_user(f"人工纠偏：{correction}")

    # ------------------------------------------------------------------
    # 单步执行：组装消息 → 模型调用 → 渲染 → （可选）步进控制
    # ------------------------------------------------------------------

    def _step(self, action_name: str, step_prompt: str,
              run_gate: bool | None = None,
              tools: list[dict] | None = None,
              tool_handler=None) -> StepOutput:
        """跑一个阶段。run_gate=None 时按档位推导：仅 phase 档保留「每阶段都拦」。"""
        if run_gate is None:
            run_gate = self._is_phase_mode()
        # 随时插手：在阶段边界生效（不打断正在进行的模型调用）
        self._check_interrupt()
        if self._aborted:
            return StepOutput(action=action_name, raw="", parsed="",
                              elapsed_sec=0.0, tokens=0)
        action = self.registry.get(action_name)
        messages = self.context.build_step_messages(action, step_prompt)
        # 流式：向渲染层索取本步的 token 回调并透传（此前从未透传，model 的 on_token 是死代码）
        on_token = self.render.on_token(action_name)
        # 计划阶段用推理模型（慢但深），其余阶段用执行模型（快而稳）；plan_model 未配置则回落
        client = self.plan_model if (action_name == "plan" and self.plan_model) else self.model
        resp = client.complete(messages, tools=tools, on_token=on_token)
        parsed = parse_tag(resp.text, action_name.upper())
        # 控制阶段常无正文（决策/判定在工具参数里）：用工具渲染兜底，保展示与历史可见
        if not parsed.strip() and resp.tool_name:
            parsed = render_tool_text(action_name, resp.tool_name, resp.tool_args)
        out = StepOutput(action=action_name, raw=resp.text, parsed=parsed,
                         elapsed_sec=resp.elapsed_sec, tokens=resp.tokens,
                         reasoning=resp.reasoning,
                         tool_name=resp.tool_name, tool_args=resp.tool_args,
                         usage=resp.usage)

        # 历史账本：原生工具调用须按 OpenAI 规范回写 assistant(tool_calls)
        # + 随后的 role=tool 回执，保证后续调用历史合法（缺口 ⑧）
        if resp.tool_calls:
            content = parsed or resp.text or ""
            self.context.add_assistant(text=content, tool_calls=resp.tool_calls)
            if tool_handler is not None:
                # 原生工具循环：逐个执行真实工具，结果以 role=tool 回写（对标 Claude）
                for tc in resp.tool_calls:
                    name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = tool_handler(name, args)
                    self.context.add_tool(tc["id"], result)
                    self.render.info(f"↳ 工具 {name} → {result[:300]}")
                self._tool_loop_pending = True
            else:
                for tc in resp.tool_calls:
                    self.context.add_tool(tc["id"], content or "ok")
        elif resp.tool_name and not resp.text.strip():
            self.context.add_assistant(parsed)
        else:
            self.context.add_assistant(resp.text)
        self.render.show(action_name, parsed, resp.text or parsed, resp.elapsed_sec,
                         resp.tokens, reasoning=resp.reasoning, usage=resp.usage)

        if run_gate:
            self._apply_gate(action_name)
        return out

    # ------------------------------------------------------------------
    # 控制信号解析：工具调用为主，文本解析兜底，自修回路收尾
    # ------------------------------------------------------------------

    def _decision_extract(self, out: StepOutput) -> tuple[str, bool]:
        """从一步输出取出 THINK 决策：(值, 是否命中)。工具优先，文本兜底。"""
        if out.tool_name == "decide_next_step":
            d = str((out.tool_args or {}).get("decision", "")).upper()
            if d in _DECISIONS:
                return d, True
        return parse_decision(out.raw)

    def _verdict_extract(self, out: StepOutput) -> tuple[str, bool]:
        """从一步输出取出 OBSERVE/VERIFY 判定：(值, 是否命中)。工具优先，文本兜底。"""
        if out.tool_name == "submit_verdict":
            v = str((out.tool_args or {}).get("verdict", "")).strip().lower()
            mapped = _VERDICT_MAP.get(v)
            if mapped:
                return mapped, True
        return parse_verdict(out.raw)

    def _resolve(self, action_name: str, out: StepOutput, extract,
                 tools: list[dict] | None = None,
                 max_retry: int = _REPAIR_MAX) -> str:
        """取得合法控制信号：工具/文本命中即用；歧义则自修，仍失败回落安全默认。

        extract 返回 (值, 是否命中)；歧义时其值已是安全默认
        （think→ESCALATE 移交人工；判定→不通过），确保绝不静默乐观通过。
        修复轮不进人工 gate（run_gate=False），避免纠偏打断；每次尝试透明渲染。
        """
        value, ok = extract(out)
        if ok:
            return value
        hint = _REPAIR_HINTS.get(action_name, "请严格按当前阶段要求调用相应工具给出结论。")
        self.render.warn(f"{action_name} 未获得合法结构化结论，请求自我修正（最多 {max_retry} 次）")
        attempts = 0
        while not ok and attempts < max_retry:
            attempts += 1
            out = self._step(action_name, hint, run_gate=False, tools=tools)
            if self._aborted:
                return value
            value, ok = extract(out)
        if not ok:
            self.render.warn(f"{action_name} 自我修正 {max_retry} 次仍失败，按安全默认处理（不静默通过）")
        return value
