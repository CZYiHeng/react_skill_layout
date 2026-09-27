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

# ---- token 压力估算（对齐 DSH token-meter 的思路：实测 + 预估，而非纯猜） ----

#: 字符数 → token 的初猜比例。保守取 2.0（中英混排、代码各半时的中位经验值）。
#: 它只是个起点：每次真实调用后都会用 provider 返回的实测 prompt 校准（见 _factor）。
_CHARS_PER_TOKEN = 2.0
#: 校准系数的滚动平均权重。取 0.5：一次实测就明显纠偏，又不会被单次异常值主导。
_FACTOR_EMA = 0.5
#: 校准系数的夹紧区间。**夹紧而不是丢弃**：早先的写法是"比值超范围就整次不学"，
#: 结果一个合法的偏高比例（例如实测 40）会把校准永久卡在初值 1.0，等于从不校准。
#: 真实端点的 chars/token 比例大致在 1~8 之间（中文偏小、代码偏大），
#: 给到 0.5~12 足够覆盖，同时挡住明显异常的 usage。
_FACTOR_MIN, _FACTOR_MAX = 0.5, 12.0


def _msg_chars(msg: dict) -> int:
    """一条消息的字符数（含 tool_calls 的参数文本，它们是真实 token 来源）。"""
    n = len(msg.get("content") or "")
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        n += len(fn.get("name") or "") + len(fn.get("arguments") or "")
    return n


def estimate_tokens(messages: list[dict], factor: float = 1.0) -> int:
    """按字符数估算一批消息的 token 量；factor 为实测校准系数。"""
    chars = sum(_msg_chars(m) for m in messages)
    return int(chars / _CHARS_PER_TOKEN * factor)
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
    #: **窗口条数护栏**（内部量，不出现在 config 里）：条数超它就压缩一次，防极端长尾
    #: 把账本拖爆。它**不是**上下文预算的调节旋钮——那件事由 `max_context_tokens` 负责。
    #: 之所以原本的 `max_context_messages` 配置键被删掉：它和 token 预算并列摆放，
    #: 让人以为是"上下文大小"设置（实测就有人把 12 一直留着），而 12 条这个量级会
    #: 让压缩频繁触发、把 provider 的前缀缓存反复打断。留着默认 400，正常任务
    #: （20~40 条）永不触发；构造测试时可直接赋值以逼出条数路径。
    _max_window_messages: int = 400
    #: **压缩阈值（token）**：下一次请求的预估 prompt 超过它才压缩。
    #: 旧实现按「条数」触发，而条数表达不了"这次请求要花多少 prompt tokens"——
    #: 一条 tool 回执上万字符也算 1 条，于是长任务在真实负载下被频繁压缩，
    #: 而 provider 的前缀缓存要求完整匹配缓存前缀单元，压缩一次就作废其后全部缓存
    #: （实测同会话 prompt 非单调：19358→16481，OBSERVE 命中率仅 3.3%）。
    #: 默认 100k：实测 200k 时单次 prompt 峰值涨到 199,371、10 轮累计 10.87M
    #: （每次调用都要重发全部历史，单次上限越大，二次增长的代价越高）。
    #: **<=0 表示整体取消上下文预算**（只剩下面的护栏兜底），即回滚开关。
    max_context_tokens: int = 100000
    # 运行环境信息（OS/shell/cwd/工具可用性），拼进每步 system 提示
    env_info: str = ""

    # ---- 缓存友好的压缩态（前缀只追加，绝不回改已有消息） ----
    #: 摘要当前覆盖的消息下标上界（已摘要的原文不入窗口）。**只增不减**：
    #: 它一旦推进就不再回退，保证两次连续调用之间「system + 历史原文」逐字节相同，
    #: 让 provider 的前缀缓存（DeepSeek 自动硬盘缓存 / Kimi context caching）持续命中。
    _digest_upto: int = field(default=0, init=False)
    #: 已冻结的摘要文本；仅在压缩推进时重建一次，其余调用逐字节复用
    _digest_text: str = field(default="", init=False)
    #: 上一次真实调用的实测 prompt tokens（provider 返回）；None = 还没有实测值
    _last_prompt_tokens: int | None = field(default=None, init=False)
    #: 上一次真实调用组装出的消息条数（用于算"自那以后新增了多少"）
    _call_msg_count: int = field(default=0, init=False)
    #: 字符→token 的在线校准系数，由实测/估算比值的滚动平均得到
    _factor: float = field(default=1.0, init=False)
    #: 上一次调用**发出**的消息列表（估算 prompt 用，与实测同源，便于精确校准）
    _last_sent: list[dict] = field(default_factory=list, init=False)
    #: 跨任务会话记忆（summary / last_status）。**不进 messages**，避免 token 爆炸。
    session_memory: dict = field(default_factory=dict)

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
        # 压力计量随账本一起归零：否则新任务会沿用上一个任务的实测值而误判已超预算。
        # 校准系数**不**重置——它刻画的是"这个模型/端点的分词比例"，跨任务依然有效。
        self._last_prompt_tokens = None
        self._call_msg_count = 0
        self._last_sent = []

    # ---- token 压力计量（实测 + 预估 + 在线校准） ----

    def note_call(self, sent_messages: list[dict]) -> None:
        """在一次真实调用**发出前**记录其形状，供随后 observe_usage 精确校准。

        传发出前的那一份，而不是调用后再取 self.messages：模型调用期间账本又新增了
        回执，用后者会让估算与实测不同源，校准系数被系统性带偏。
        """
        self._call_msg_count = len(self.messages)
        self._last_sent = list(sent_messages)

    def observe_usage(self, usage: dict | None) -> None:
        """回灌 provider 的实测 usage，校准字符→token 系数。

        没有实测值（部分端点不返回 usage）时什么都不做，压力计量退化为纯估算——
        绝不因为拿不到 usage 就改变任何既有行为。
        """
        if not usage:
            return
        prompt = usage.get("prompt")
        if not isinstance(prompt, int) or prompt <= 0:
            return
        est = estimate_tokens(self._last_sent, 1.0) if self._last_sent else 0
        if est > 0:
            ratio = prompt / est
            # 夹紧而非丢弃：一次偏高/偏低的观测仍能推动系数，只是不会越界。
            self._factor = min(_FACTOR_MAX, max(_FACTOR_MIN,
                              (1 - _FACTOR_EMA) * self._factor + _FACTOR_EMA * ratio))
        self._last_prompt_tokens = prompt

    def pressure_tokens(self) -> int:
        """下一次请求的预估 prompt 量（token）。

        实测优先："上次实测 prompt" + "自那以后新增消息的估算"。
        这正是 DSH token-meter 的口径（pressureTokens + 新增 surface = projectedTokens）：
        用实测兜住历史，用估算补上刚追加的部分。
        没有任何实测值时退化为全量估算。
        """
        if self._last_prompt_tokens is None:
            return estimate_tokens(self.messages, self._factor)
        fresh = self.messages[self._call_msg_count:]
        return self._last_prompt_tokens + estimate_tokens(fresh, self._factor)

    # ---- 消息组装 ----

    @property
    def effective_budget(self) -> int:
        """实际生效的窗口条数护栏：小于 `_MIN_BUDGET` 时抬到 `_MIN_BUDGET`。

        小于 `_MIN_KEEP` 的护栏会让「压缩后保留量」超过护栏本身，压缩刚结束就再次
        超限，窗口失控且每步都压缩——那比不压缩还糟。0/负数表示不设条数护栏。
        """
        if self._max_window_messages <= 0:
            return 0
        return max(self._max_window_messages, _MIN_BUDGET)

    def _align_window_start(self, start: int) -> int:
        """窗口起点对齐：绝不把 assistant(tool_calls) 与它的 role=tool 回执切开。

        起点若落在 tool 消息上，就回退到它的宿主 assistant。否则产生孤儿 tool 消息，
        API 直接报 400。
        """
        while start > 1 and self.messages[start].get("role") == "tool":
            start -= 1
        return start

    def _should_compress(self) -> bool:
        """是否该压缩。**主触发是 token 压力**，条数只作硬上限兜底。

        旧实现按「最近 N 条」触发，而条数表达不了"这次请求要花多少 prompt tokens"：
        一条 tool 回执上万字符也只算 1 条。于是 12/16 这种小预算在真实负载下被反复
        触发，而 provider 的前缀缓存要求完整匹配缓存前缀单元——压缩一次就作废其后
        全部缓存（实测同会话 prompt 非单调 19358→16481，OBSERVE 命中率仅 3.3%）。

        两个条件任一成立才压缩：
        - token 压力 >= `max_context_tokens`（主路径）；
        - 条数 >= `_max_window_messages`（内部护栏，防极端长尾把账本拖爆）。

        注意护栏的代价：它**忽略单条大小**，所以一条超大消息也会触发一次压缩。
        这是护栏应有的取舍——宁可多压一次，也不让窗口无限增长。
        `max_context_tokens<=0` 时压缩**整体关闭**（回滚开关）。
        """
        if self.max_context_tokens > 0 and self.pressure_tokens() >= self.max_context_tokens:
            return True
        cap = self.effective_budget
        return cap > 0 and len(self.messages) - self._digest_upto >= cap

    def _maybe_compress(self) -> None:
        """按空间预算**单调推进**压缩边界，推进时重建一次摘要。无压缩则原地不动。

        这是整个缓存策略的关键：老实现每次调用都按「最近 N 条」重算窗口，窗口每滑动
        一条就在历史**中段**插入/删除内容，而 provider 的前缀缓存要求完整匹配到某个
        缓存前缀单元——中段一变，**其后全部作废**（实测 16k 的 prompt 只剩 ~300 token
        命中）。改为只在超预算时压缩一次：

        - 压缩时剪到 `_MIN_KEEP`（而非贴着上限剪），于是压缩后窗口还能**纯追加**地
          增长一段才再次触发——两次压缩之间前缀逐字节稳定；
        - 预算之外只剩首条任务锚点留在窗口侧，其余原文全部沉淀进摘要。

        触发口径见 `_should_compress`：**token 压力优先**，条数只作硬上限。
        """
        hist_len = len(self.messages)
        if not self._should_compress():
            return  # 未超预算：一切照旧，前缀零改动
        # 保留量：条数口径下是预算的一半（留出纯追加增长空间）；
        # token 口径下沿用 _MIN_KEEP 作为不可再少的锚（避免把小任务削成空窗口）。
        budget = self.effective_budget
        keep = max(_MIN_KEEP, budget // 2) if budget > 0 else _MIN_KEEP
        # 裁剪起点必须夹在 [max(1, 边界), hist_len-1]：
        # keep 可能**大于账本长度**（条数硬上限调大后，budget//2 很容易超过实际条数），
        # 此时 hist_len - keep 会变成负数，把 _digest_upto 推成负值——之后
        # `messages[_digest_upto:]` 会从尾部倒着切片，窗口和摘要双双错乱。
        # 下界取 max(1, _digest_upto) 同时保证：首条任务锚点留在窗口侧、边界单调不减。
        start = self._align_window_start(hist_len - keep)
        start = min(max(start, max(1, self._digest_upto)), hist_len - 1)
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
