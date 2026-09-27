"""会话上下文：消息历史、计划状态、轮次控制。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .action import Action

# 全局协议说明：每个步骤的系统提示都以此开头，保证模型理解当前所处循环
GLOBAL_PROTOCOL = (
    "你处于一个显式 ReAct 循环中，每一步都是独立的推理/执行/观察阶段。"
    "你的输出将被状态机解析，必须严格遵守该阶段要求的输出格式与标签。"
    "历史消息是之前各阶段的完整轨迹，供你判断当前状态。"
    "当前阶段的完整指令（阶段名、该阶段的规则、当前步骤）位于**最后一条 user 消息**，"
    "以其为准执行本步。"
    "模糊任务先对齐：任务刚开始且目标不明确（含'优化''改进''做好看'等模糊词、缺少明确交付物、可被多种方式解读）时，"
    "THINK 阶段必须先 ASK 问清 1-3 个关键问题，不允许直接 PLAN/ACT——避免跑偏后返工。"
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
_DIGEST_HEADER = "# 历史摘要（较早消息已压缩，仅供定位；完整轨迹见上方消息）"
#: 压缩后除任务锚点外**至少**保留的原文条数。
#: 不能简单取 cap//2：cap 很小时（如 4）cap//2=2，而循环每步会追加 2~4 条消息，
#: 于是压缩刚结束就又超预算，窗口条数失控、压缩每步触发。给一个下限即可让它
#: 稳定在 [MIN_KEEP, 有效预算] 区间内增长，压缩间隔 ≈ 有效预算 - MIN_KEEP。
_MIN_KEEP = 8
#: 有效窗口预算下限：cap 小于此值时按此值执行（cap=4 这种极端配置仍能正常工作）
_MIN_BUDGET = 16


@dataclass
class SessionContext:
    messages: list[dict] = field(default_factory=list)
    round_no: int = 0
    plan: list[tuple[str, str]] = field(default_factory=list)  # [(步骤, 完成标准)]
    plan_index: int = 0
    max_rounds: int = 10
    # 发送给模型的最近原文条数**预算**：只在超预算时压缩一次，压缩时一次剪到预算一半，
    # 于是两次压缩之间窗口纯追加地增长，前缀缓存持续命中。<=0 表示不压缩（全量发送）。
    # 调小 = 省上下文但压缩更频繁、缓存命中更低（见 react/config.py 的说明）。
    max_context_messages: int = 100
    # 运行环境信息（OS/shell/cwd/工具可用性），拼进每步 system 提示
    env_info: str = ""

    # ---- 缓存友好的压缩态（前缀只追加，绝不回改已有消息） ----
    #: 摘要当前覆盖的消息下标上界（已摘要的原文不入窗口）。**只增不减**：
    #: 它一旦推进就不再回退，保证两次连续调用之间「system + 历史原文」逐字节相同，
    #: 让 provider 的前缀缓存（DeepSeek 自动硬盘缓存 / Kimi context caching）持续命中。
    _digest_upto: int = field(default=0, init=False)
    #: 已冻结的摘要文本；仅在压缩推进时重建一次，其余调用逐字节复用
    _digest_text: str = field(default="", init=False)

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
        self._digest_upto = 0
        self._digest_text = ""

    # ---- 消息组装 ----

    @property
    def effective_budget(self) -> int:
        """实际生效的窗口预算：配置值小于 `_MIN_BUDGET` 时抬到 `_MIN_BUDGET`。

        小于 `_MIN_KEEP` 的预算会让「压缩后保留量」超过预算本身，压缩刚结束就再次
        超限，窗口失控且每步都压缩——那比不压缩还糟。0/负数仍表示「不压缩」。
        """
        if self.max_context_messages <= 0:
            return 0
        return max(self.max_context_messages, _MIN_BUDGET)

    def _align_window_start(self, start: int) -> int:
        """窗口起点对齐：绝不把 assistant(tool_calls) 与它的 role=tool 回执切开。

        起点若落在 tool 消息上，就回退到它的宿主 assistant。否则产生孤儿 tool 消息，
        API 直接报 400。
        """
        while start > 1 and self.messages[start].get("role") == "tool":
            start -= 1
        return start

    def _maybe_compress(self) -> None:
        """按空间预算**单调推进**压缩边界，推进时重建一次摘要。无压缩则原地不动。

        这是整个缓存策略的关键：老实现每次调用都按「最近 N 条」重算窗口，窗口每滑动
        一条就在历史**中段**插入/删除内容，而 provider 的前缀缓存要求完整匹配到某个
        缓存前缀单元——中段一变，**其后全部作废**（实测 16k 的 prompt 只剩 ~300 token
        命中）。改为只在超预算时压缩一次：

        - 压缩时剪到 `_MIN_KEEP`（而非贴着上限剪），于是压缩后窗口还能**纯追加**地
          增长 `预算 - MIN_KEEP` 条才再次触发——两次压缩之间前缀逐字节稳定；
        - 预算之外只剩首条任务锚点留在窗口侧，其余原文全部沉淀进摘要。
        """
        hist_len = len(self.messages)
        budget = self.effective_budget
        if budget <= 0:
            return
        # 触发条件看的是**窗口本身的大小**，不是账本总长。
        # 若写成 hist_len > budget，压缩后窗口恒为 keep 条、其后的每条新增都再次超限，
        # 于是每步都压缩、digest_upto 每步 +1，窗口条数不变而内容整体平移——
        # 完全等价于老实现的「滑动窗口」，缓存照样全灭（这个坑被 check_cache_prefix 抓到）。
        # 用 >=：压缩是"事后"发生的（当前这一步的消息已入账本），留一条余量才不会越界。
        if hist_len - self._digest_upto < budget:
            return  # 未超预算：一切照旧，前缀零改动
        keep = max(_MIN_KEEP, budget // 2)
        start = self._align_window_start(hist_len - keep)
        dropped = self.messages[self._digest_upto:start]
        if dropped:
            shown = dropped[-_DIGEST_MAX_LINES:]
            omitted = len(dropped) - len(shown)
            lines = [_summarize(m) for m in shown]
            if omitted > 0:
                lines.insert(0, f"- （更早 {omitted} 条已省略）")
            self._digest_text = _DIGEST_HEADER + "\n" + "\n".join(lines)
        self._digest_upto = start

    def build_step_messages(self, action: Action, step_prompt: str) -> list[dict]:
        """组装某一步的完整消息列表。

        system = 全局协议 + 运行环境（**完全静态**，任意阶段/轮次相同）。
        历史做**只追加式压缩**：首条任务锚点始终保留，窗口为 (摘要上界, 账本末尾]；
        摘要只在压缩推进时重建，两次压缩之间窗口内容逐字节不变。
        阶段名 / skill 正文 / 步骤指令 / 历史摘要全部放在消息**尾部** user。
        只影响"发给模型的"，不动 self.messages（/save 依旧导出全量账本）。
        """
        history = self.messages
        self._maybe_compress()

        if self.effective_budget > 0 and self._digest_upto > 0:
            # 摘要覆盖 [1, _digest_upto)，窗口从 _digest_upto 起 —— 无重叠、无遗漏
            windowed = [*history[:1], *history[self._digest_upto:]]
        else:
            windowed = list(history)

        # 兜底：清理不成对的工具消息对，防止 API 400。
        # - 孤儿 role=tool（无宿主 assistant）→ 丢弃；
        # - assistant(tool_calls) 的每个 id 缺少紧随的 tool 回执 → 整对丢弃
        #   （"insufficient tool messages following tool_calls message"）。
        # 宁可少一条上下文，也不能让整次调用失败。
        cleaned: list[dict] = []
        i = 0
        while i < len(windowed):
            m = windowed[i]
            tcs = m.get("tool_calls")
            if m.get("role") == "assistant" and tcs:
                needed = {t.get("id") for t in tcs if t.get("id")}
                j = i + 1
                got: set[str] = set()
                while j < len(windowed) and windowed[j].get("role") == "tool":
                    got.add(windowed[j].get("tool_call_id"))
                    j += 1
                if needed and got >= needed:
                    cleaned.append(m)
                    cleaned.extend(windowed[i + 1:j])
                i = j
                continue
            if m.get("role") == "tool":
                i += 1
                continue  # 孤儿 tool：丢弃
            cleaned.append(m)
            i += 1
        windowed = cleaned

        # system 完全静态化（缓存友好）：只含全局协议 + 运行环境，任意阶段/轮次调用都相同。
        # 阶段名 / skill 正文 / 步骤指令 / 历史摘要全部放消息**尾部** user——
        # 五阶段、多轮、工具循环的所有调用共享同一 system 前缀。
        env_block = f"# 运行环境\n{self.env_info}\n\n" if self.env_info else ""
        system = f"{GLOBAL_PROTOCOL}\n\n{env_block}"
        stage = (
            f"# 当前阶段：{action.name.upper()}\n\n"
            f"{action.skill_body}\n\n"
            f"# 当前步骤指令\n{step_prompt}"
        )
        # 顺序敏感：摘要跨阶段累积、内容会变，必须放在**最后**（离前缀最远）。
        # 放在 stage 之前时，摘要一变就会把后面的阶段名 + skill 正文一起作废——
        # 而 skill 正文在同一槽位内是复用的，等于把每次压缩的代价放大到整个尾部。
        tail_user = f"{stage}\n\n{self._digest_text}" if self._digest_text else stage
        return [{"role": "system", "content": system}, *windowed,
                {"role": "user", "content": tail_user}]

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
