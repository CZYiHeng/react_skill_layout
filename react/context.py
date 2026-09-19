"""会话上下文：消息历史、计划状态、轮次控制。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .action import Action

# 全局协议说明：每个步骤的系统提示都以此开头，保证模型理解当前所处循环
GLOBAL_PROTOCOL = (
    "你处于一个显式 ReAct 循环中，每一步都是独立的推理/执行/观察阶段。"
    "你的输出将被状态机解析，必须严格遵守该阶段要求的输出格式与标签。"
    "历史消息是之前各阶段的完整轨迹，供你判断当前状态。"
)


def _summarize(msg: dict, limit: int = 140) -> str:
    """把一条历史消息压成一行摘要（用于窗口外旧消息，控制发给模型的上下文）。"""
    role = msg.get("role", "?")
    if role == "tool":
        role = "工具回执"
    elif role == "assistant":
        role = "模型"
    elif role == "user":
        role = "用户"
    elif role == "system":
        role = "系统"
    text = " ".join((msg.get("content") or "").split())
    tcs = msg.get("tool_calls")
    if tcs:
        names = ",".join((t.get("function") or {}).get("name", "") for t in tcs)
        text = (text + " " if text else "") + f"⟨工具调用: {names}⟩"
    if len(text) > limit:
        text = text[:limit] + "…"
    return f"- [{role}] {text}"


_DIGEST_MAX_LINES = 20  # 历史摘要最多保留多少行（防摘要本身随历史增长而膨胀）


@dataclass
class SessionContext:
    messages: list[dict] = field(default_factory=list)
    round_no: int = 0
    plan: list[tuple[str, str]] = field(default_factory=list)  # [(步骤, 完成标准)]
    plan_index: int = 0
    max_rounds: int = 10
    # 发给模型的最近原文条数上限；超出部分折算摘要。<=0 表示不压缩（全量发送）
    max_context_messages: int = 12

    # ---- 历史维护 ----

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, text: str, *, tool_calls: list | None = None) -> None:
        """追加一条 assistant 消息；原生工具调用时携带 tool_calls（OpenAI 规范）。"""
        msg: dict = {"role": "assistant", "content": text}
        if tool_calls is not None:
            msg["tool_calls"] = tool_calls
        self.messages.append(msg)

    def add_tool(self, tool_call_id: str, content: str) -> None:
        """追加工具回执消息（role=tool），必须紧跟在含 tool_calls 的 assistant 之后。"""
        self.messages.append({"role": "tool", "tool_call_id": tool_call_id,
                             "content": content})

    # ---- 计划状态 ----

    def set_plan(self, steps: list[tuple[str, str]], preserve_position: bool = False) -> None:
        """制定或修订计划（每步为 (步骤, 完成标准) 元组）。

        preserve_position=False（首次规划）：从头开始执行。
        preserve_position=True（中途修订）：保留已完成前缀，替换剩余步骤——
        已完成的工作不重做，修订只影响未执行部分。
        """
        if preserve_position:
            self.plan_index = min(self.plan_index, len(steps))
        else:
            self.plan_index = 0
        self.plan = steps

    @property
    def plan_done(self) -> bool:
        return self.plan_index >= len(self.plan)

    def current_step(self) -> str:
        if self.plan_done:
            return "（无计划步骤，按 THINK 决策直接执行）"
        step, criteria = self.plan[self.plan_index]
        if criteria:
            return f"步骤：{step}\n完成标准：{criteria}"
        return f"步骤：{step}"

    def current_criteria(self) -> str:
        """当前步骤的完成标准；无计划或未指定时返回空串。"""
        if self.plan_done:
            return ""
        return self.plan[self.plan_index][1]

    def advance_step(self) -> None:
        self.plan_index += 1

    def reset(self) -> None:
        self.messages.clear()
        self.round_no = 0
        self.plan.clear()
        self.plan_index = 0

    # ---- 消息组装 ----

    def _window_start(self, keep: int) -> int:
        """窗口起点：绝不把 assistant(tool_calls) 与它的 role=tool 回执切开。

        硬取 history[-keep:] 时，若宿主 assistant 被切在窗外、而它的 tool 回执
        留在窗内，就会产生孤儿 tool 消息，API 直接报 400。此处一旦窗口首条是
        tool 消息，就回退到它前面的那条 assistant。
        """
        start = max(1, len(self.messages) - keep)
        while start > 1 and self.messages[start].get("role") == "tool":
            start -= 1
        return start

    def build_step_messages(self, action: Action, step_prompt: str) -> list[dict]:
        """组装某一步的完整消息列表。

        system = 全局协议 + 该动作槽位的 skill 正文 +（可选）历史摘要 + 当前步骤指令。
        历史做**窗口化**：始终保留首条任务锚点 + 最近 max_context_messages 条原文，
        更早的消息折算成一行摘要并入 system。只影响"发给模型的"，不动 self.messages
        （/save 依旧导出全量账本）。
        """
        history = self.messages
        digest = ""
        if self.max_context_messages > 0 and len(history) > self.max_context_messages:
            keep = self.max_context_messages
            head = history[:1]                     # 首条用户任务，锚点不可丢
            tail = history[self._window_start(keep):]
            dropped = history[1:len(history) - len(tail)]
            if dropped:
                shown = dropped[-_DIGEST_MAX_LINES:]      # 只留最近若干条，防摘要膨胀
                omitted = len(dropped) - len(shown)
                lines = [_summarize(m) for m in shown]
                if omitted > 0:
                    lines.insert(0, f"- （更早 {omitted} 条已省略）")
                digest = ("# 历史摘要（较早消息已压缩，仅供定位；最新轨迹在下方消息中）\n"
                          + "\n".join(lines))
            windowed = [*head, *tail]
        else:
            windowed = list(history)

        # 兜底：丢弃没有宿主的 tool 消息。孤儿 role=tool 会让 API 直接 400
        # （"must be a response to a preceding message with 'tool_calls'"）。
        # 宁可少一条上下文，也不能让整次调用失败。
        cleaned: list[dict] = []
        for m in windowed:
            if m.get("role") == "tool" and not (cleaned and cleaned[-1].get("tool_calls")):
                continue
            cleaned.append(m)
        windowed = cleaned

        system = (
            f"{GLOBAL_PROTOCOL}\n\n"
            f"# 当前阶段：{action.name.upper()}\n\n"
            f"{action.skill_body}\n\n"
            f"{digest + chr(10) + chr(10) if digest else ''}"
            f"# 当前步骤指令\n{step_prompt}"
        )
        return [{"role": "system", "content": system}, *windowed,
                {"role": "user", "content": step_prompt}]

    def export_markdown(self) -> str:
        """导出会话全文为 Markdown（/save 使用）。"""
        lines = ["# ReAct Agent 会话记录", ""]
        for msg in self.messages:
            role = msg["role"].upper()
            lines.append(f"## {role}")
            lines.append("")
            lines.append(msg.get("content") or "")
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                lines.append("")
                lines.append(f"（工具调用：{fn.get('name', '')} 参数 {fn.get('arguments', '')}）")
            lines.append("")
        return "\n".join(lines)
