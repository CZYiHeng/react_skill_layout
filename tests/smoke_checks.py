"""冒烟断言集：按主题拆分，每个函数返回失败说明列表（空列表=通过）。

断言文案与改造前 main.py 内的版本保持一致，确保 `--smoke` 输出不变。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from react.action import Action, ActionRegistry
from react.context import SessionContext
from react.display import classify
from react.executor import LocalExecutor
from react.loop import (ReActLoop, parse_check, parse_decision, parse_exec,
                        parse_exec_all, parse_plan_steps, parse_solution,
                        parse_verdict)
from react.model import ModelError, ModelResponse, OpenAIClient, _permanent_status


def check_loop_structure(calls: list[str] | None, result, context, recorder,
                         model=None) -> list[str]:
    """循环结构断言：阶段覆盖、ASK/缺陷/验收三条回路、工具调用回写、执行器接线。"""
    failures: list[str] = []
    if calls is None:
        return failures
    for phase in ("THINK", "ACT", "OBSERVE", "VERIFY"):
        if not any(f"# 当前阶段：{phase}" in c for c in calls):
            failures.append(f"缺少阶段调用: {phase}")
    if result.status != "done":
        failures.append(f"循环未正常终止: {result.status}")
    if not any("冒烟回答" in m["content"] for m in context.messages):
        failures.append("ASK 回答未注入历史")
    if not any("下一步: ASK" in m["content"] for m in context.messages):
        failures.append("Mock 未走 ASK 分支")
    # 缺口 h：VERIFY 不通过→反馈回炉→二次验收通过
    if sum("# 当前阶段：VERIFY" in c for c in calls) != 2:
        failures.append("VERIFY 未按预期执行两次（不通过回路未走通）")
    if not any("最终验收未通过" in m["content"] for m in context.messages):
        failures.append("VERIFY 不通过的反馈未注入历史")
    # v1.3 缺陷回路：OBSERVE 判缺陷 → 反馈注入 → ACT 重跑 → 通过 → 指针只推进一次
    if sum("# 当前阶段：ACT" in c for c in calls) != 2:
        failures.append("缺陷回路未导致 ACT 重跑")
    if not any("上一版产物存在缺陷" in m["content"] for m in context.messages):
        failures.append("缺陷反馈未注入历史")
    if context.plan_index != 1:
        failures.append(f"计划指针推进异常: {context.plan_index}")
    # 原生工具调用通道：全阶段统一注入全量工具（含 decide_next_step / submit_verdict）
    seen = getattr(model, "tools_seen", [])
    if not any("decide_next_step" in t for t in seen):
        failures.append("THINK 未注入 decide_next_step 工具")
    if not any("submit_verdict" in t for t in seen):
        failures.append("OBSERVE/VERIFY 未注入 submit_verdict 工具")
    # 缺口 ⑧：工具调用须回写 assistant(tool_calls) + role=tool 回执
    if not any(m.get("tool_calls") for m in context.messages):
        failures.append("工具调用未回写 assistant(tool_calls)")
    if not any(m.get("role") == "tool" for m in context.messages):
        failures.append("工具调用未回写 role=tool 回执")
    # ACT 执行请求被执行器处理（④ 接线）
    if not any(k == "shell" for k, _ in recorder.calls):
        failures.append("ACT 的 [EXEC] 未被执行器处理")
    return failures


def check_parsers() -> list[str]:
    """解析器断言：计划步骤、CHECK、决策六值、判定同义词、EXEC 提取。"""
    failures: list[str] = []
    _steps = parse_plan_steps("[PLAN]\n1. 甲 | 完成标准：含甲\n2. 乙")
    if not _steps or _steps[0][1] != "含甲" or _steps[1] != ("乙", ""):
        failures.append("parse_plan_steps 标准解析失败")
    if parse_check("[CHECK] 标准X\n[RESULT] 产物") != "标准X":
        failures.append("parse_check 解析失败")
    if parse_check("[RESULT] 无标准产物") != "":
        failures.append("parse_check 缺省应为空串")

    for value in ("PLAN", "ACT", "DONE", "VERIFY", "ASK", "ESCALATE"):
        if parse_decision(f"下一步: {value}") != (value, True):
            failures.append(f"parse_decision 不支持决策值: {value}")
    if parse_decision("[THOUGHT] 无决策行") != ("ESCALATE", False):
        failures.append("parse_decision 歧义应安全回退 ESCALATE（非 ACT）")

    if parse_verdict("[OBSERVATION] 通过：ok") != ("通过", True):
        failures.append("parse_verdict 通过判定解析失败")
    if parse_verdict("说了些含糊的话，没给判定") != ("不通过", False):
        failures.append("parse_verdict 歧义应安全回退 不通过（非通过）")
    # 缺口 f：文本兜底同义词（修"未通过"被误判为通过的坑）
    for sample, expect in (
        ("[OBSERVATION] 验收合格", "通过"),
        ("[VERIFY] 满足要求", "通过"),
        ("结论：未通过", "不通过"),
        ("不合格，需返工", "不通过"),
        ("[OBSERVATION] 有缺陷需修正", "缺陷"),
        ("[VERIFY] 请重做", "重试"),
    ):
        if parse_verdict(sample) != (expect, True):
            failures.append(f"parse_verdict 同义词解析失败: {sample} 期望 {expect}")

    if parse_exec("[CHECK] x\n[EXEC: shell]\n```bash\necho hi\n```\n[RESULT] y") != ("shell", "echo hi"):
        failures.append("parse_exec shell 解析失败")
    if parse_exec("[CHECK] x\n[RESULT] y") is not None:
        failures.append("parse_exec 无 EXEC 应返回 None")
    return failures


def check_ask_limit(registry, render, recorder) -> list[str]:
    """缺口 g：ASK 反复提问须有独立上限，否则永不触发 max_rounds（死循环）。"""
    failures: list[str] = []

    class _AskForeverClient:
        def complete(self, messages, on_token=None, tools=None):
            return ModelResponse(
                text="[THOUGHT] 还需确认。\n下一步: ASK", tokens=0, elapsed_sec=0.0,
                tool_name="decide_next_step",
                tool_args={"decision": "ASK", "reason": "缺信息"},
                tool_calls=[{"id": "c0", "type": "function",
                             "function": {"name": "decide_next_step",
                                          "arguments": '{"decision":"ASK","reason":"缺信息"}'}}])

    _ask_ctx = SessionContext(max_rounds=10)
    _ask_loop = ReActLoop(registry, _ask_ctx, _AskForeverClient(), render,
                          gate=None, ask=lambda q: "继续", executor=recorder)
    _ask_res = _ask_loop.run("任务")
    if _ask_res.status != "escalated":
        failures.append("ASK 反复提问应触发上限并 ESCALATE（缺口 g）")
    return failures


def check_executor_defaults(base_dir: Path) -> list[str]:
    """执行器安全默认 +（Windows）OS 级沙箱自测。"""
    failures: list[str] = []
    if "拒绝" not in LocalExecutor(cwd=base_dir).run("shell", "echo hi"):
        failures.append("执行器默认应拒绝 shell")
    if sys.platform == "win32":
        try:
            from react import win32_sandbox

            if not win32_sandbox.sandbox_available():
                failures.append("Windows 下 win32_sandbox 应可用")
            else:
                _sb = win32_sandbox.run_sandboxed("echo SANDBOX_OK", str(base_dir), 15)
                if "SANDBOX_OK" not in _sb:
                    failures.append(f"沙箱基础执行异常: {_sb}")
                _to = win32_sandbox.run_sandboxed("choice /t 4 /d y >nul", str(base_dir), 1)
                if "超时" not in _to:
                    failures.append(f"沙箱超时未触发: {_to}")
        except Exception as e:  # noqa: BLE001
            failures.append(f"沙箱自测异常: {e}")
    return failures


def check_context_windowing() -> list[str]:
    """问题③：上下文窗口化——压缩旧消息、限制窗口、不动全量账本。

    触发口径现已改为 **token 压力**（`max_context_tokens`）为主、条数护栏
    （`_max_window_messages`，内部量、不再是配置键）兜底；本断言直接设护栏、
    并关掉 token 触发，以复现旧的条数路径来验证窗口化契约本身。
    """
    failures: list[str] = []
    # 关掉 token 触发，只看条数护栏这条路径（token 路径由 check_token_budget 覆盖）
    _c = SessionContext(max_context_tokens=0)
    _c._max_window_messages = 4
    for i in range(20):
        _c.add_user(f"u{i}")
        _c.add_assistant(f"a{i}")
    _msgs = _c.build_step_messages(Action(name="think", skill_body="正文"), "指令")
    if "历史摘要" not in _msgs[-1]["content"]:
        failures.append("历史摘要未生成/未放在消息尾部（应并入最后一条 user）")
    if "历史摘要" in _msgs[0]["content"]:
        failures.append("历史摘要不应在 system 中（放在中间破坏缓存前缀）")
    # system + 任务锚点 + 窗口原文 + 当前指令。
    # 窗口上限是**有效预算**（cap 小于下限时会被抬到 effective_budget），
    # 且压缩是事后触发的，故再留 1 条余量。
    _cap = 1 + 1 + _c.effective_budget + 1 + 1
    if len(_msgs) > _cap:
        failures.append(f"上下文窗口化未限制发送条数: {len(_msgs)}（上限 {_cap}）")
    if len(_c.messages) != 40:
        failures.append("窗口化不应改动全量账本")
    # P0 验收：同阶段内连续调用（如 ACT 工具循环）system 前缀必须稳定，
    # 才能吃满 DeepSeek/kimi 的自动上下文缓存（前缀命中）。
    _s1 = _c.build_step_messages(Action(name="think", skill_body="正文"), "指令")
    _c.add_assistant("新增一条历史")
    _s2 = _c.build_step_messages(Action(name="think", skill_body="正文"), "指令")
    if _s1[0]["content"] != _s2[0]["content"]:
        failures.append("同阶段 system 前缀不稳定（上下文缓存将全部失效）")
    # 方案 B 验收：跨阶段/跨轮次 system 必须完全相同（所有调用共享同一前缀）。
    _s3 = _c.build_step_messages(Action(name="act", skill_body="另一正文"), "指令2")
    if _s1[0]["content"] != _s3[0]["content"]:
        failures.append("跨阶段 system 不相同（缓存前缀断裂）")
    if "# 当前阶段：ACT" not in _s3[-1]["content"]:
        failures.append("阶段标记不在尾部 user（应在消息尾部）")
    if _s3[0]["content"].startswith("# 当前阶段"):
        failures.append("system 不应包含阶段标记")
    return failures


def check_cache_prefix() -> list[str]:
    """缓存前缀稳定性：**连续组装之间，历史原文只能追加、下沉，不能被改写。**

    provider 的前缀缓存（DeepSeek 自动硬盘缓存 / Kimi context caching）要求完整匹配
    到某个缓存前缀单元，前缀中任何一处变化都会让其后的内容全部作废。老实现每次调用
    都按「最近 N 条」重算窗口，窗口一滑动就在历史**中段**插入/删除，命中率因此长期
    只有 ~15%。本断言锁死新契约：

    - 摘要边界 `_digest_upto` 单调不减（压缩是单向的）；
    - 上一次窗口里的每条原文，本次要么**原地保留**（落在重叠区且逐字节相同），
      要么被**整段下沉**进摘要（窗口右移），绝不能被别的内容顶替；
    - 账本不变时重复组装，结果必须逐条完全相同。

    实现要点：不能拿上一次「组装结果」去比——那条历史在两次组装之间又增长过，
    会把新追加的消息误判成改写。必须像下面这样，每次组装后立刻对**当时的**账本切片，
    并在下一次比较时用「当时的**账本**」而不是当时的窗口做对齐。
    """
    failures: list[str] = []

    def snapshot():
        """记录当前压缩态、发给模型的窗口切片、以及该切片所在的**账本副本**。

        账本必须 copy：ctx.messages 是同一个 list 对象，后续 add_user 会让它继续变长，
        留着引用就等于拿「未来」的账本去对齐「过去」的下标。
        """
        msgs = list(ctx.messages)
        wins = [*msgs[:1], *msgs[ctx._digest_upto:]]
        return ctx._digest_upto, wins, msgs

    ctx = SessionContext(max_context_tokens=0)
    ctx._max_window_messages = 4
    for i in range(30):
        ctx.add_user(f"u{i}")
        ctx.add_assistant(f"a{i}")

    act = Action(name="act", skill_body="阶段正文")
    prev = ctx.build_step_messages(act, "执行步骤")
    pupto, pwins, pmsgs = snapshot()
    compressions = 0

    for i in range(40):
        ctx.add_user(f"新增 {i}")      # 纯追加：模拟循环每步新增历史
        cur = ctx.build_step_messages(act, "执行步骤")
        cupto, cwins, cmsgs = snapshot()

        # system 必须逐字节稳定（五阶段共享同一缓存前缀）
        if cur[0]["content"] != prev[0]["content"]:
            failures.append("system 前缀发生变化（缓存前缀断裂）")
        # 首条任务锚点不可被替换
        if cur[1].get("content") != prev[1].get("content"):
            failures.append("首条任务锚点被改写（缓存前缀断裂）")

        # 1) 摘要边界单调不减
        if cupto < pupto:
            failures.append(f"摘要边界回退: {pupto} → {cupto}（压缩边界必须单调）")

        # 窗口首条是**固定任务锚点**，压缩后会占住新窗口第 0 位、把旧锚点挤走，
        # 所以比较必须剥掉锚点；锚点下面单独断言它没被替换。
        pk, ck = pwins[1:], cwins[1:]      # 剥掉锚点后的窗口原文
        off = len(pk) - len(ck)

        if off < 0:
            # 窗口增长：上一次的原文必须原样成为本次原文的前缀（纯追加）
            if ck[:len(pk)] != pk:
                failures.append("账本增长时窗口历史被改写（应只追加，不回改）")
        elif off == 0:
            # 条数不变：内容必须逐条一致，不得整体替换
            if ck != pk:
                failures.append("窗口条数不变但原文被整体替换（历史中段重写，前缀缓存失效）")
        else:
            # 压缩：本次每条都必须是「本次账本」里那条原文，不被别的内容顶替
            # （cupto 就是本次窗口在账本里的起点，逐条对齐即可）
            for k, msg in enumerate(ck):
                if msg != cmsgs[cupto + k]:
                    failures.append(
                        f"窗口原文被顶替（i={i} k={k}，摘要边界 {pupto}→{cupto}，"
                        f"偏移={off}）—— 历史中段被改写，前缀缓存从该点起全部作废"
                    )
                    break
            compressions += 1

        # 窗口条数上界：正常 <= 预算；触发是事后判定的，故允许 1 条余量
        if not (1 <= len(cwins) <= ctx.effective_budget + 1):
            failures.append(
                f"窗口条数越界: {len(cwins)}（有效预算={ctx.effective_budget}，上限+1）"
            )

        # 3) 账本不变时重复组装：结果必须逐条一致
        if ctx.build_step_messages(act, "执行步骤") != cur:
            failures.append("账本未变但组装结果不同（摘要被无谓重算，前缀缓存失效）")

        prev = cur
        pupto, pwins, pmsgs = cupto, cwins, cmsgs

    if compressions == 0:
        failures.append("40 步内压缩从未触发，本断言未覆盖压缩路径")
    if not ctx._digest_text:
        failures.append("压缩后摘要为空")
    # 摘要落在最后一条 user 内（放中段会破坏缓存前缀）
    if ctx._digest_text and "历史摘要" not in prev[-1]["content"]:
        failures.append("历史摘要不在尾部 user 内")
    if not (0 < ctx._digest_upto < len(ctx.messages)):
        failures.append(f"摘要边界越界: _digest_upto={ctx._digest_upto}")
    return failures


def check_token_budget() -> list[str]:
    """**核心断言**：压缩由 token 压力触发，而不是消息条数。

    旧实现只数条数，所以"1 条 50k 字符的 tool 回执"被当成 1 条、轻松绕过预算；
    这正是长任务撞上下文上限、且缓存被频繁压缩打崩的根因。
    """
    failures: list[str] = []
    act = Action(name="act", skill_body="正文")

    # 1) 条数远未达护栏，但 token 压力超标 → 必须压缩
    # 预算要大于 system+skill+tools 的开销（否则任何组装都"超预算"，测不出东西）
    ctx = SessionContext(max_context_tokens=6000)
    for i in range(30):                     # 60 条，离 400 条护栏差得远
        ctx.add_user("x" * 400)             # 每条约 200 token
        ctx.add_assistant("y" * 400)
    before = len(ctx.messages)
    ctx.build_step_messages(act, "执行")
    if ctx._digest_upto == 0:
        failures.append("token 压力达标却未压缩（仍在按条数判断？）")
    if not ctx._digest_text:
        failures.append("token 触发的压缩未生成摘要")
    if len(ctx.messages) != before:
        failures.append("压缩不应改动全量账本")

    # 2) 反过来：条数超护栏必须压缩（护栏路径，内部量直接赋值）
    ctx2 = SessionContext(max_context_tokens=0)
    ctx2._max_window_messages = 16
    for i in range(40):
        ctx2.add_user(f"u{i}")
        ctx2.add_assistant(f"a{i}")
    ctx2.build_step_messages(act, "执行")
    if ctx2._digest_upto == 0:
        failures.append("条数超护栏却未压缩（护栏失效）")

    # 3) 两者都没超 → 一条都不许动（保证不无谓压缩、不打断前缀缓存）
    ctx3 = SessionContext(max_context_tokens=10_000_000)
    for i in range(5):
        ctx3.add_user(f"u{i}")
        ctx3.add_assistant(f"a{i}")
    msgs3 = ctx3.build_step_messages(act, "执行")
    if ctx3._digest_upto != 0 or ctx3._digest_text:
        failures.append("预算充足时不应发生任何压缩")
    if len(msgs3) != 1 + 10 + 1:            # system + 10 条原文 + 尾部指令
        failures.append(f"预算充足时窗口应完整：{len(msgs3)}")

    # 4) 回滚开关：max_context_tokens=0 时只剩条数护栏
    ctx4 = SessionContext(max_context_tokens=0)
    ctx4._max_window_messages = 4
    for i in range(20):
        ctx4.add_user(f"u{i}")
        ctx4.add_assistant(f"a{i}")
    ctx4.build_step_messages(act, "执行")
    if ctx4._digest_upto == 0:
        failures.append("max_context_tokens=0 时条数护栏未生效（回滚开关失效）")

    # 5) 默认预算的回归保护。两件事：
    #    (a) 默认值就是 100k —— 直接断言 DEFAULTS，改它必红；
    #    (b) 判定确实在按 token 走：越阈值必压、阈值以下不压。
    #    为什么默认值是 100k：实测阈值 200k 时单次 prompt 峰值到 199,371，
    #    95 次调用累计 10.87M；压到 100k 正是为了不让历史滚到 20 万才压缩。
    from react.config import DEFAULTS as _D
    if _D["max_context_tokens"] != 100_000:
        failures.append(
            f"默认 max_context_tokens 应为 100000，实际 {_D['max_context_tokens']}")

    def _pressure(big, small, chars=20_000):
        c = SessionContext(max_context_tokens=100_000)
        for i in range(big):
            c.add_user("x" * chars)
        for i in range(small):
            c.add_user("y" * 1_000)
        return c

    # (b-1) 越过阈值 → 必须判定需压缩（这才是把阈值调小的意义）
    over = _pressure(20, 0)                      # 压力 ≈ 200k
    if not over._should_compress():
        failures.append(f"压力 {over.pressure_tokens()} 超过 100k 却未判定需压缩")
    # (b-2) 阈值以下 → 不得压缩
    under = _pressure(6, 0)                      # 压力 ≈ 60k
    if under._should_compress():
        failures.append(f"压力 {under.pressure_tokens()} 未超 100k 却判定需压缩")
    # 构造有效性：两组必须真的分列阈值两侧，否则上面的判定失去意义
    if not (under.pressure_tokens() < 100_000 < over.pressure_tokens()):
        failures.append(
            f"构造失效：under={under.pressure_tokens()} over={over.pressure_tokens()} "
            "未分列 100k 两侧")
    return failures


def check_provider_config() -> list[str]:
    """模型接入只有一处解析：providers 形状、active_provider 选择、旧形状兼容、缺字段报错。

    背景：此前"取生效接入"的逻辑被写了三遍（service._active_profile、
    webapi._active_profile_cfg、以及各处直接读顶层字段），其中 webapi 那份漏了
    timeout_sec；顶层 base_url/api_key/model 与 profiles[] 又是两套并列真相源。
    本断言锁死"只有一个解析器"这一契约。
    """
    from react.config import (ConfigError, active_provider_name,
                              normalize_providers, resolve_provider)

    failures: list[str] = []

    def expect_error(cfg, label):
        try:
            resolve_provider(cfg, "act")
        except ConfigError:
            return
        failures.append(f"{label}：应当报错却通过了")

    # 1) 新形状：多家 + active_provider
    cfg = {
        "active_provider": "ds",
        "providers": {
            "ds": {"base_url": "https://a", "api_key": "ka", "model": "m-a", "timeout_sec": 11},
            "km": {"base_url": "https://b", "api_key": "kb", "model": "m-b"},
        },
    }
    eff = resolve_provider(cfg, "act")
    if eff["model"] != "m-a" or eff["base_url"] != "https://a" or eff["timeout_sec"] != 11:
        failures.append(f"active_provider 未生效：{eff}")
    if active_provider_name(cfg) != "ds":
        failures.append("active_provider_name 未返回 ds")
    # 未写 timeout_sec 的 provider 回落到缺省值（不再有全局 step_timeout_sec）
    cfg["active_provider"] = "km"
    from react.config import DEFAULT_STEP_TIMEOUT
    if resolve_provider(cfg, "act")["timeout_sec"] != DEFAULT_STEP_TIMEOUT:
        failures.append("provider 未回落到 DEFAULT_STEP_TIMEOUT")

    # 2) 多家却没指定 active_provider → 必须报错（避免"改了文件不知生效谁"）
    expect_error({"providers": {"a": {"base_url": "x", "api_key": "k", "model": "m"},
                                "b": {"base_url": "y", "api_key": "k", "model": "m"}}},
                 "多家未指定 active_provider")
    # active_provider 指向不存在的名字 → 必须报错
    expect_error({"active_provider": "nope",
                  "providers": {"a": {"base_url": "x", "api_key": "k", "model": "m"}}},
                 "active_provider 指向不存在")
    # 生效 provider 缺 api_key → 必须报错（而不是拿空 key 去请求）
    expect_error({"providers": {"a": {"base_url": "x", "model": "m"}}},
                 "生效 provider 缺 api_key")
    # 唯一 provider 时可以省略 active_provider（不必强制写）
    one = resolve_provider({"providers": {"solo": {"base_url": "x", "api_key": "k",
                                                   "model": "m"}}}, "act")
    if one["model"] != "m":
        failures.append("只有一个 provider 时不应要求 active_provider")

    # 3) 旧形状兼容：profiles 数组
    legacy_arr = {
        "active_profile": "old",
        "profiles": [{"name": "old", "base_url": "https://old", "api_key": "ko",
                      "model": "m-old", "timeout_sec": 7}],
    }
    eff = resolve_provider(legacy_arr, "act")
    if eff["model"] != "m-old" or eff["timeout_sec"] != 7:
        failures.append(f"旧 profiles 数组未兼容：{eff}")

    # 4) 最旧形状兼容：顶层三件套
    legacy_top = {"base_url": "https://top", "api_key": "kt", "model": "m-top"}
    eff = resolve_provider(legacy_top, "act")
    if eff["model"] != "m-top" or eff["base_url"] != "https://top" \
            or eff["timeout_sec"] != DEFAULT_STEP_TIMEOUT:
        failures.append(f"旧顶层三件套未兼容：{eff}")

    # 5) 新形状优先于旧的同名字段（两套并存时不许含糊）
    both = {
        "base_url": "https://legacy", "api_key": "kl", "model": "m-legacy",
        "active_provider": "n",
        "providers": {"n": {"base_url": "https://new", "api_key": "kn", "model": "m-new"}},
    }
    if resolve_provider(both, "act")["base_url"] != "https://new":
        failures.append("providers 与顶层并存时应以 providers 为准")

    # 6) 计划阶段：只换模型，端点与 key 不变
    plan = resolve_provider({**cfg, "active_provider": "ds",
                             "plan_model": "m-reasoner", "plan_timeout_sec": 321}, "plan")
    if plan["model"] != "m-reasoner" or plan["base_url"] != "https://a" \
            or plan["api_key"] != "ka" or plan["timeout_sec"] != 321:
        failures.append(f"plan 角色应只换 model：{plan}")
    # plan_model 为空 → 返回空 dict，由调用方回落到 act 模型
    if resolve_provider({**cfg, "active_provider": "ds", "plan_model": ""}, "plan"):
        failures.append("plan_model 为空时应返回空 dict（回落到 act 模型）")

    # 7) 归一函数本身：三种形状都能读出来
    if set(normalize_providers(cfg)) != {"ds", "km"}:
        failures.append("normalize_providers 未正确解析 providers map")
    if set(normalize_providers(legacy_arr)) != {"old"}:
        failures.append("normalize_providers 未正确解析 profiles 数组")
    if set(normalize_providers(legacy_top)) != {"default"}:
        failures.append("normalize_providers 未把顶层三件套归为 default")

    # 8) Web 配置白名单必须与 DEFAULTS / provider 字段完全对齐。
    #    不对齐的后果是静默的：白名单缺项 → 页面里改了不生效；白名单多项 →
    #    写进文件却没人读。这类漂移很容易在重构后悄悄出现。
    from react.config import DEFAULTS, PROVIDER_FIELDS
    from react.webapi import CONFIG_FIELDS, CONFIG_PROVIDER_FIELDS
    editable = set(CONFIG_FIELDS) | set(CONFIG_PROVIDER_FIELDS)
    missing = sorted(set(DEFAULTS) - editable)
    extra = sorted(set(CONFIG_FIELDS) - set(DEFAULTS) - set(PROVIDER_FIELDS))
    if missing:
        failures.append(f"白名单缺少 DEFAULTS 里的字段（页面改不了）：{missing}")
    if extra:
        failures.append(f"白名单含非默认字段（写了也没人读）：{extra}")

    # 9) 超时只有一处：providers.<name>.timeout_sec。曾经同时存在全局
    #    `step_timeout_sec` 与 provider 级 timeout_sec，设置页里出现两个
    #    "单步超时"，用户无从判断哪个生效——这类重复旋钮不留。
    if "step_timeout_sec" in DEFAULTS or "step_timeout_sec" in CONFIG_FIELDS:
        failures.append("step_timeout_sec 与 provider.timeout_sec 重复，应只保留后者")

    # 10) 执行参数默认值必须与"执行已启用"自洽；安全底线不得被顺手放开。
    #     实测踩过：同一份配置里 shell/写文件全开，exec_timeout_sec 只有 30、
    #     shell_backend 还是 cmd，结果 10 轮用满仍未完成（max_rounds_exceeded）。
    from react.config import security_warnings
    if DEFAULTS["exec_timeout_sec"] < 60:
        failures.append(
            f"exec_timeout_sec 默认 {DEFAULTS['exec_timeout_sec']}s 跑不动测试/构建，应 >= 120")
    if DEFAULTS["shell_backend"] == "cmd":
        failures.append('shell_backend 默认不应写死 "cmd"（应用 "auto" 探测 Git Bash）')
    if DEFAULTS["max_rounds"] < 20:
        failures.append(f"max_rounds 默认 {DEFAULTS['max_rounds']} 对执行型任务偏紧，应 >= 20")
    # 安全底线：对齐参数不等于放开开关
    if DEFAULTS["enable_shell_exec"] or DEFAULTS["enable_file_write"]:
        failures.append("执行开关不应默认开启（安全底线）")

    # 11) 组合告警：单值都合法、凑一起才矛盾的情形必须能被发现（并验证鉴别力）
    probe = Path("probe.json")
    def _warns(**over):
        cfg = dict(DEFAULTS); cfg.update(over)
        cfg["providers"] = {"x": {"base_url": "https://e", "api_key": "k", "model": "m"}}
        return [w for w in security_warnings(cfg, probe) if "保守档" in w]

    if _warns(enable_shell_exec=True):
        failures.append("shell 开 + 默认执行参数不该告警（默认值已自洽）")
    for over, label in (
        ({"enable_shell_exec": True, "exec_timeout_sec": 30}, "超时过短"),
        ({"enable_shell_exec": True, "shell_backend": "cmd"}, "后端写死 cmd"),
    ):
        if not _warns(**over):
            failures.append(f"shell 已启用但{label}时未告警")
    if _warns(enable_shell_exec=False, exec_timeout_sec=30):
        failures.append("shell 关闭时不该因超时短而告警（那是无害配置）")
    return failures


def check_skill_variants(base_dir: Path) -> list[str]:
    """槽位内多 skill：`<槽位>/<变体名>/SKILL.md` 的发现、切换、回退与降级。

    这是"把槽位内部的 skill 抽出来"的机制面：默认变体是槽位根下的 SKILL.md，
    额外 skill 放子目录，运行时按配置选用其一。**不配变体时行为必须与从前逐字节一致。**
    """
    import tempfile

    from react.action import (ACTION_NAMES, DEFAULT_VARIANT, ActionRegistry,
                              parse_skill_md)

    failures: list[str] = []

    def write_md(path: Path, name: str, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\ndescription: t\n---\n\n{body}\n",
                        encoding="utf-8")

    # 1) 默认路径逐字节不变：无子目录时，body 与直接解析 SKILL.md 的结果相同
    base = ActionRegistry()
    base.load(base_dir / "skills")
    for slot in ACTION_NAMES:
        md = base_dir / "skills" / slot / "SKILL.md"
        if not md.is_file():
            continue
        _, expect = parse_skill_md(md.read_text(encoding="utf-8"), fallback_name=slot)
        got = base.get(slot).skill_body
        if got != expect.strip():
            failures.append(f"槽位 {slot}: 无变体时 body 与直接解析 SKILL.md 不一致")
        if base.get(slot).active_variant != DEFAULT_VARIANT:
            failures.append(f"槽位 {slot}: 默认应为 {DEFAULT_VARIANT}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # 槽位 act：默认 + 一个变体；observe：只有默认（用来验证互不影响）
        write_md(root / "act" / "SKILL.md", "act", "DEFAULT-ACT")
        write_md(root / "act" / "strict-code" / "SKILL.md", "strict-code", "STRICT-ACT")
        write_md(root / "observe" / "SKILL.md", "observe", "DEFAULT-OBSERVE")
        # 缺 SKILL.md 的子目录（不应当成 skill，但要告警）
        (root / "act" / "assets").mkdir(parents=True)
        # 占用保留名 default（应告警并忽略）
        write_md(root / "act" / DEFAULT_VARIANT / "SKILL.md", "default", "SHOULD-IGNORE")

        reg = ActionRegistry()
        reg.load(root)

        # 2) 变体被发现，default 仍在（且未被保留名子目录覆盖）
        vs = set(reg.get("act").variants)
        if vs != {DEFAULT_VARIANT, "strict-code"}:
            failures.append(f"变体发现不正确：{sorted(vs)}")
        if "SHOULD-IGNORE" in reg.get("act").skill_body:
            failures.append("保留名 default 子目录不应覆盖默认变体")

        # 3) 缺 SKILL.md 的子目录 / 保留名 都要告警
        joined = "\n".join(reg.warnings)
        if "assets" not in joined:
            failures.append("缺 SKILL.md 的子目录未告警")
        if DEFAULT_VARIANT not in joined:
            failures.append("占用保留名 default 未告警")

        # 4) 切换生效，且只影响该槽位
        got = reg.set_variant("act", "strict-code")
        if got != "strict-code" or "STRICT-ACT" not in reg.get("act").skill_body:
            failures.append("切换变体未生效")
        if "DEFAULT-OBSERVE" not in reg.get("observe").skill_body:
            failures.append("切换 act 变体影响到了 observe 槽位")

        # 5) 回退：空串 / default / 下划线 都回默认
        for back in ("", DEFAULT_VARIANT, "_"):
            reg.set_variant("act", back)
            if "DEFAULT-ACT" not in reg.get("act").skill_body:
                failures.append(f"set_variant({back!r}) 未回退默认")
            if reg.get("act").active_variant != DEFAULT_VARIANT:
                failures.append(f"set_variant({back!r}) 后 active_variant 不是 default")

        # 6) 未知变体 → KeyError 且消息含可选值
        try:
            reg.set_variant("act", "nope")
            failures.append("未知变体未报错")
        except KeyError as e:
            if "strict-code" not in str(e):
                failures.append("未知变体的报错未列出可选项")

        # 7) 槽位没有 SKILL.md 时回退内置默认提示词（不是空 body）
        reg2 = ActionRegistry()
        reg2.load(root)                      # think/plan/verify 在 root 下不存在
        reg2.set_variant("think", "")
        a = reg2.get("think")
        if a.bound or not a.skill_body.strip():
            failures.append("无默认 SKILL.md 的槽位回退后应为内置默认提示词且 bound=False")

        # 8) variants_of 供展示用
        if reg.variants_of("act") != [DEFAULT_VARIANT, "strict-code"]:
            failures.append(f"variants_of 返回不正确：{reg.variants_of('act')}")

        # 9) 配置级装配（ReactService.build_registry）：正常切换 + 非法值降级不抛
        from react.service import ReactService
        for variants, expect, label in (
            ({}, DEFAULT_VARIANT, "缺省"),
            ({"act": "strict-code"}, "strict-code", "正常切换"),
            ({"act": "nope"}, DEFAULT_VARIANT, "未知变体应降级"),
            ({"nosuch": "x"}, DEFAULT_VARIANT, "未知槽位应忽略"),
        ):
            svc = ReactService({"skill_variants": variants}, base_dir, root)
            r2 = svc.build_registry()
            got = r2.get("act").active_variant
            if got != expect:
                failures.append(f"skill_variants {label}：act 期望 {expect}，实际 {got}")
            if variants and not r2.warnings and variants in ({"act": "nope"}, {"nosuch": "x"}):
                failures.append(f"skill_variants {label}：非法值未告警")
        # 整体类型错（str）也要能容忍而不是崩
        try:
            ReactService({"skill_variants": "oops"}, base_dir, root).build_registry()
        except Exception as e:  # noqa: BLE001
            failures.append(f"skill_variants 类型错时应告警而非抛异常，实际 {type(e).__name__}")
    return failures


def check_capability_model(base_dir: Path) -> list[str]:
    """能力模型：能力=自包含目录，名字与路径解耦，解析唯一。

    这是"把某个功能框架 skill 化、写完能整体分离出去"的机制面：能力靠名字被选中，
    靠目录被搬走，框架不留悬空引用；槽位缺失会回退内置默认，故必须能看出"这个槽位
    到底是谁提供的"。
    """
    import json
    import tempfile

    from react.action import ACTION_NAMES, parse_skill_md
    from react.capability import (DEFAULT_CAPABILITY, discover, probe,
                                  resolve_capability)
    from react.config import DEFAULTS, ConfigError
    from react.service import ReactService

    failures: list[str] = []

    def stages(root: Path) -> None:
        for slot in ACTION_NAMES:
            d = root / slot
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(
                f"---\nname: {slot}\n---\n\nCAP-{slot}\n", encoding="utf-8")

    # 1) 零回归：缺省解析到 <base>/skills，且五槽位 body 与直接解析 SKILL.md 逐字节一致
    svc = ReactService(dict(DEFAULTS), base_dir)
    if svc.skills_dir != base_dir / "skills":
        failures.append(f"缺省能力根应为 {base_dir / 'skills'}，实际 {svc.skills_dir}")
    if svc.capability.name != DEFAULT_CAPABILITY:
        failures.append(f"缺省能力名应为 {DEFAULT_CAPABILITY}，实际 {svc.capability.name}")
    reg = svc.build_registry()
    for slot in ACTION_NAMES:
        md = base_dir / "skills" / slot / "SKILL.md"
        if not md.is_file():
            continue
        _, expect = parse_skill_md(md.read_text(encoding="utf-8"), fallback_name=slot)
        if reg.get(slot).skill_body != expect.strip():
            failures.append(f"缺省能力下槽位 {slot} 的 body 与直接解析 SKILL.md 不一致")

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        # 仓库内置的通用档（default 能力）
        stages(base / "skills")
        # 能力容器：capabilities/coding
        stages(base / "capabilities" / "coding")
        (base / "capabilities" / "coding" / "capability.json").write_text(
            json.dumps({"name": "coding", "version": "1.0.0",
                        "description": "测试用写码能力"}), encoding="utf-8")
        # 只提供 act 的半能力（用于验证"槽位来源可判定"）
        (base / "capabilities" / "partial" / "act").mkdir(parents=True)
        (base / "capabilities" / "partial" / "act" / "SKILL.md").write_text(
            "---\nname: act\n---\n\nPARTIAL-ACT\n", encoding="utf-8")
        # 历史平级布局：skills_legacy（无 capability.json，应能按名字解析）
        stages(base / "skills_legacy")

        cfg = dict(DEFAULTS)
        caps, _ = discover(cfg, base)
        if set(caps) != {"coding", "partial", "skills_legacy"}:
            failures.append(f"能力发现不正确：{sorted(caps)}")

        # 2) 具名解析 + 3) 别名 + 4) 路径解析
        if resolve_capability(cfg, base, "coding").root != base / "capabilities" / "coding":
            failures.append("具名能力未解析到 capabilities/coding")
        alt = dict(cfg, capability_aliases={"old-code": "coding"})
        if resolve_capability(alt, base, "old-code").name != "coding":
            failures.append("能力别名未生效")
        if resolve_capability(cfg, base, str(base / "skills_legacy")).name != "skills_legacy":
            failures.append("按目录路径解析未生效")
        # 目录名也能当引用：`capabilities/<名字>` 与平级 `<base>/<名字>` 都可回退解析
        if resolve_capability(cfg, base, "capabilities").root != base / "capabilities":
            pass  # capabilities 本身不是能力，跳过
        if resolve_capability(cfg, base).name != DEFAULT_CAPABILITY:
            failures.append("空 ref 应解析为 default 能力")
        # 配置里的 active_capability 必须真的生效（"" 不得被当成 default 提前返回）
        active = dict(cfg, active_capability="skills_legacy")
        if resolve_capability(active, base).name != "skills_legacy":
            failures.append("配置 active_capability 未生效（可能被空串短路成 default）")

        # 2b) ReactService(skills_dir=None) 必须走能力解析，而不是被当成路径。
        #     实测踩过：argparse 的 --skills-dir 默认值塞了具体路径，导致用户没指定
        #     也走"按路径解析"，配置里的 active_capability 与内置别名永远不生效
        #     （--check 把 default 能力显示成了目录名 skills）。
        svc_none = ReactService(dict(cfg, active_capability="coding"), base)
        if svc_none.capability.name != "coding":
            failures.append(
                f"skills_dir=None 时应按 active_capability 解析，实际能力 {svc_none.capability.name}")
        if svc_none.capability.name == "coding" and svc_none.capability.version != "1.0.0":
            failures.append("能力版本未从 capability.json 读出")

        # 5) 未知名 → 报错且列出可用能力（不静默回退 default）
        try:
            resolve_capability(cfg, base, "nope")
            failures.append("未知能力名未报错（静默回退会让人以为在用自己的能力）")
        except ConfigError as e:
            if "coding" not in str(e):
                failures.append("未知能力的报错未列出可用能力")

        # 6) manifest 损坏 → 降级匿名能力 + 告警，不抛
        broken = base / "capabilities" / "broken"
        stages(broken)
        (broken / "capability.json").write_text("{ not json", encoding="utf-8")
        cap = probe(broken)
        if cap.name != "broken":
            failures.append(f"manifest 损坏时应用目录名做能力名，实际 {cap.name}")
        if not cap.warnings:
            failures.append("manifest 损坏未告警")
        # requires 不满足 → 跳过该能力 + 告警
        mism = base / "capabilities" / "needs-future"
        stages(mism)
        (mism / "capability.json").write_text(json.dumps(
            {"name": "needs-future", "requires": {"react_agent": ">=99.0"}}),
            encoding="utf-8")
        caps2, warns2 = discover(cfg, base)
        if "needs-future" in caps2:
            failures.append("requires 不满足的能力不应被载入")
        if not any("needs-future" in w for w in warns2):
            failures.append("requires 不满足时未告警")
        # 未知 manifest 字段 → 告警（拼错的键不该被静默忽略）
        odd = base / "capabilities" / "odd"
        stages(odd)
        (odd / "capability.json").write_text(json.dumps(
            {"name": "odd", "descripton": "拼错的键"}), encoding="utf-8")
        if not any("descripton" in w for w in probe(odd).warnings):
            failures.append("manifest 未知字段未告警")

        # 7) 同名覆盖 → 告警含来源
        dup = base / "extra" / "coding"
        stages(dup)
        over = dict(cfg, capability_paths=[str(base / "extra")])
        _, warns3 = discover(over, base)
        if not any("重复" in w and "coding" in w for w in warns3):
            failures.append("同名能力覆盖未告警")

        # 8) 槽位来源可判定：partial 只给 act，其余必须标为回退内置默认
        pcap = resolve_capability(cfg, base, "partial")
        if set(pcap.provided) != {"act"}:
            failures.append(f"partial 能力提供的阶段判定错误：{pcap.provided}")
        if pcap.complete:
            failures.append("partial 能力不应被判为完整")
        if "think" not in pcap.missing():
            failures.append("partial 的缺失阶段未列出 think")
        cfg_partial = dict(cfg, active_capability="partial")
        svc2 = ReactService(cfg_partial, base)
        reg2 = svc2.build_registry()
        if "PARTIAL-ACT" not in reg2.get("act").skill_body:
            failures.append("partial 能力未接管 act 槽位")
        # 未提供的槽位回退内置默认提示词（不是空 body）
        if "PARTIAL-ACT" in reg2.get("think").skill_body or not reg2.get("think").skill_body.strip():
            failures.append("partial 未提供的槽位应回退内置默认提示词")

    # 9) 能力名不因目录改名而漂移：probe 用 manifest 的 name 优先于目录名
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "some-dir"
        stages(root)
        (root / "capability.json").write_text(
            json.dumps({"name": "coding"}), encoding="utf-8")
        if probe(root).name != "coding":
            failures.append("manifest 的 name 应优先于目录名")

    # 10) 脚手架：一条命令造出新能力，且不污染 default 能力
    with tempfile.TemporaryDirectory() as td:
        sandbox = Path(td)
        stages(sandbox / "skills")               # 假装这是仓库的 default 能力
        before = {p: p.read_bytes() for p in (sandbox / "skills").rglob("*") if p.is_file()}
        from rich.console import Console as _Console

        import main as _main
        _main.cmd_new_capability("qa-review", _Console(), sandbox)
        dest = sandbox / "capabilities" / "qa-review"
        if not dest.is_dir():
            failures.append("脚手架未在 capabilities/<名>/ 生成能力")
        else:
            for slot in ACTION_NAMES:
                md = dest / slot / "SKILL.md"
                if not md.is_file():
                    failures.append(f"脚手架缺少 {slot}/SKILL.md")
                    continue
                head = md.read_text(encoding="utf-8").splitlines()
                if f"name: {slot}" not in head:
                    failures.append(f"脚手架 {slot} 的 frontmatter name 未改成槽位名")
                if not any("【待写】" in line for line in head):
                    failures.append(f"脚手架 {slot} 未标注待写处（会看不出还没写）")
            man = dest / "capability.json"
            if not man.is_file() or json.loads(man.read_text(encoding="utf-8"))["name"] != "qa-review":
                failures.append("脚手架未生成正确的 capability.json")
            # 生成的能力必须能被发现并解析
            if resolve_capability(dict(DEFAULTS), sandbox, "qa-review").root != dest:
                failures.append("脚手架生成的能力无法按名解析")
        # 不污染 default 能力（模板是拷出去的，不是就地改）
        after = {p: p.read_bytes() for p in (sandbox / "skills").rglob("*") if p.is_file()}
        if before != after:
            failures.append("脚手架改动了 default 能力（模板必须只读）")
        # 重名必须拒绝，不能覆盖已写内容
        try:
            _main.cmd_new_capability("qa-review", _Console(), sandbox)
            failures.append("同名能力未拒绝（会覆盖已写内容）")
        except SystemExit:
            pass
        # 非法能力名必须拒绝（不能逃出 capabilities/）
        for bad in ("../evil", "a/b", ""):
            try:
                _main.cmd_new_capability(bad, _Console(), sandbox)
                failures.append(f"非法能力名 {bad!r} 未被拒绝")
            except SystemExit:
                pass

    # 11) reload：改了 SKILL.md 后重载生效（CLI 只在启动建一次 registry，需要它）
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for slot in ACTION_NAMES:
            (root / slot).mkdir(parents=True, exist_ok=True)
            (root / slot / "SKILL.md").write_text(
                f"---\nname: {slot}\n---\n\nV1-{slot}\n", encoding="utf-8")
        reg = ActionRegistry()
        reg.load(root)
        (root / "act" / "SKILL.md").write_text(
            "---\nname: act\n---\n\nV2-act\n", encoding="utf-8")
        if "V1-act" not in reg.get("act").skill_body:
            failures.append("未 reload 前应仍是旧内容（前提不成立）")
        reg.reload(root)
        body = reg.get("act").skill_body
        if "V2-act" not in body or "V1-act" in body:
            failures.append("reload 后未读到新内容")
        if reg.warnings:
            failures.append(f"reload 后 warnings 未重置：{reg.warnings}")
    return failures


def check_pressure_estimate() -> list[str]:
    """压力计量：实测优先、新增部分靠估算、并用实测在线校准。"""
    failures: list[str] = []
    ctx = SessionContext()
    # 先给账本一点内容，再做全量估算（空账本估出 0 是正确行为，不是 bug）
    ctx.add_user("u" * 400)
    est = ctx.pressure_tokens()
    if est <= 0:
        failures.append(f"无实测值时压力估算应 > 0，实际 {est}")

    # 模拟一次真实调用：实测/估算 ≈ 4（真实端点的量级）→ 系数被抬高
    sent = [{"role": "system", "content": "s" * 100},
            {"role": "user", "content": "u" * 300}]     # 400 字符 ≈ 估算 200 token
    ctx.messages = list(sent)
    ctx.note_call(sent)
    ctx.observe_usage({"prompt": 800, "completion": 10, "total": 810})
    if ctx._last_prompt_tokens != 800:
        failures.append("实测 prompt 未被记录")
    if ctx._factor <= 1.0:
        failures.append(f"校准系数未被抬高（factor={ctx._factor}，真实比例 4）")
    if ctx._factor > 12.0:
        failures.append(f"校准系数越过上界（factor={ctx._factor}）")
    # 实测 + 新增：压力必须随新增消息单调增长
    p1 = ctx.pressure_tokens()
    ctx.add_user("z" * 4000)
    p2 = ctx.pressure_tokens()
    if p2 <= p1:
        failures.append(f"新增消息后压力未增长：{p1} → {p2}")
    if p1 < 800:
        failures.append(f"压力应至少包含实测历史 800，实际 {p1}")

    # usage 缺失时不得改变任何状态（退化路径）
    keep = (ctx._last_prompt_tokens, ctx._factor)
    ctx.observe_usage(None)
    ctx.observe_usage({})
    if (ctx._last_prompt_tokens, ctx._factor) != keep:
        failures.append("usage 缺失时不应改动计量状态")

    # reset 清掉实测值，但保留校准系数（它刻画模型分词比例，跨任务有效）
    f = ctx._factor
    ctx.reset()
    if ctx._last_prompt_tokens is not None:
        failures.append("reset 后应清空实测 prompt（否则新任务会误判已超预算）")
    if ctx._factor != f:
        failures.append("reset 不应重置校准系数")
    return failures


def check_session_memory(base_dir: Path) -> list[str]:
    """任务间记忆：默认关闭且逐字节同旧；开启时注入一条摘要且不重放账本。"""
    from react.model import MockClient
    from react.render import RichRenderer

    failures: list[str] = []
    registry = ActionRegistry()
    registry.load(base_dir / "skills")
    render = RichRenderer(None, show_reasoning=False)

    def _loop(ctx):
        return ReActLoop(registry, ctx, MockClient(), render, gate=None,
                         ask=lambda q: "冒烟回答：输入已确认")

    # 1) 默认关闭：第二个任务的账本不得含上一个任务的任何痕迹
    ctx = SessionContext(max_rounds=5)
    loop = _loop(ctx)
    loop.run("第一个任务：甲")
    first_msgs = list(ctx.messages)
    loop.run("第二个任务：乙")
    joined = "\n".join(m.get("content") or "" for m in ctx.messages)
    if "会话记忆" in joined:
        failures.append("默认（continue_session=False）不应注入会话记忆")
    if len(ctx.messages) == 0 or ctx.messages[0]["content"] != "第二个任务：乙":
        failures.append("默认路径首条应为本次任务本身（与旧行为一致）")
    if not first_msgs:
        failures.append("前置任务未产生账本，断言无效")

    # 2) 记忆已被记录
    if not ctx.session_memory.get("summary"):
        failures.append("任务结束后未记录会话记忆摘要")

    # 3) 开启接续：注入一条会话记忆，且**不重放**上个任务的账本
    ctx2 = SessionContext(max_rounds=5)
    loop2 = _loop(ctx2)
    loop2.run("第一个任务：甲", continue_session=False)
    prev_len = len(ctx2.messages)
    loop2.run("第二个任务：乙", continue_session=True)
    head = "\n".join((m.get("content") or "") for m in ctx2.messages[:2])
    if "会话记忆" not in head:
        failures.append("continue_session=True 时未注入会话记忆")
    if len(ctx2.messages) >= prev_len * 2:
        failures.append("会话记忆疑似重放了整个账本（应只注入摘要）")
    for m in ctx2.messages:
        c = m.get("content") or ""
        if "会话记忆" in c and len(c) > 1500:
            failures.append(f"会话记忆摘要过长（{len(c)} 字符），可能撑大上下文")
            break
    return failures


def check_plan_revision() -> list[str]:
    """缺口 c：计划修订保留已完成前缀。"""
    failures: list[str] = []
    _ctx = SessionContext()
    _ctx.set_plan([("甲", "含甲"), ("乙", ""), ("丙", "")])
    _ctx.advance_step()
    _ctx.advance_step()
    _ctx.set_plan([("甲", "含甲"), ("乙", ""), ("丙改", "含丙"), ("丁", "")],
                  preserve_position=True)
    if _ctx.plan_index != 2 or "丙改" not in _ctx.current_step():
        failures.append("计划修订未保留已完成前缀")
    return failures


def check_display_classify() -> list[str]:
    """显示模板拦截层：内容类型自动识别 + 显式 [FORMAT:] 覆盖。"""
    failures: list[str] = []
    display_cases = [
        ('{"a": 1}', "json"),
        ("```python\nprint(1)\n```", "code"),
        ("| 名称 | 值 |\n|---|---|\n| 甲 | 1 |", "table"),
        ("1. 步骤一\n2. 步骤二\n3. 步骤三", "plan"),
        ("@@ -1,2 +1,2 @@\n-旧\n+新", "diff"),
        ("# 标题\n\n正文段落", "markdown"),
        ("[FORMAT: table]\n{\"a\":1}", "table"),
        ("[FORMAT: md]\n{\"a\":1}", "markdown"),
        ("# 标题\n\n- 列表项一\n- 列表项二\n\n正文", "markdown"),
    ]
    for sample, expect in display_cases:
        got, _ = classify(sample)
        if got != expect:
            failures.append(f"display 分类错误: {sample[:24]!r} 期望 {expect} 实际 {got}")
    return failures


def check_model_error_policy() -> list[str]:
    """模型客户端错误策略：4xx 永久性错误立即失败，其余仍退避重试。"""
    failures: list[str] = []

    class _HttpErr(Exception):
        def __init__(self, code: int) -> None:
            super().__init__(f"Error code: {code}")
            self.status_code = code

    class AuthenticationError(Exception):  # 模拟 openai 异常类型（无 status_code 时按类名判定）
        pass

    if _permanent_status(_HttpErr(401)) != 401:
        failures.append("401 未被判定为永久性错误")
    if _permanent_status(_HttpErr(404)) != 404:
        failures.append("404 未被判定为永久性错误")
    if _permanent_status(_HttpErr(500)) is not None:
        failures.append("500 被误判为永久性错误（服务端错误应重试）")
    if _permanent_status(_HttpErr(429)) is not None:
        failures.append("429 被误判为永久性错误（限流应重试）")
    if _permanent_status(AuthenticationError("bad key")) is None:
        failures.append("AuthenticationError 未按类名判定为永久性错误")

    # 不真正 sleep，保持冒烟快速
    import react.model as M

    orig_sleep = M.time.sleep
    M.time.sleep = lambda _s: None
    try:
        client = OpenAIClient.__new__(OpenAIClient)  # 绕开真实 OpenAI() 构造
        client._model = "mock"
        counter = {"n": 0}

        def raise_with(code: int):
            def _boom(messages, on_token, tools):
                counter["n"] += 1
                raise _HttpErr(code)
            return _boom

        # 401：只调一次，且错误信息带排查提示
        counter["n"] = 0
        client._call_once = raise_with(401)
        try:
            client.complete([{"role": "user", "content": "x"}])
            failures.append("401 未抛出 ModelError")
        except ModelError as e:
            if "不重试" not in str(e):
                failures.append("401 报错未标注「不重试」")
            if "providers" not in str(e):
                failures.append("401 报错缺少排查提示（应指向配置文件的 providers）")
        if counter["n"] != 1:
            failures.append(f"401 仍被重试: 实际调用 {counter['n']} 次")

        # 503：仍重试满 3 次
        counter["n"] = 0
        client._call_once = raise_with(503)
        try:
            client.complete([{"role": "user", "content": "x"}])
            failures.append("503 未抛出 ModelError")
        except ModelError as e:
            if "已重试 2 次" not in str(e):
                failures.append("503 报错未说明重试次数")
        if counter["n"] != 3:
            failures.append(f"503 重试次数错误: {counter['n']}（期望 3）")
    finally:
        M.time.sleep = orig_sleep
    return failures


def check_exec_all() -> list[str]:
    """一个 ACT 可声明多个 [EXEC]，必须全部解析——只取首个会静默丢文件。"""
    failures: list[str] = []
    raw = (
        "[CHECK] x\n"
        '[EXEC: write]\n```json\n{"path": "a.py", "content": "1"}\n```\n'
        '[EXEC: write]\n```json\n{"path": "b.py", "content": "2"}\n```\n'
        '[EXEC: shell]\n```bash\necho hi\n```\n'
        "[RESULT] y"
    )
    items = parse_exec_all(raw)
    if len(items) != 3:
        failures.append(f"parse_exec_all 应返回 3 个，实际 {len(items)}")
    else:
        if items[0] != ("write", '{"path": "a.py", "content": "1"}'):
            failures.append(f"第 1 个解析错误: {items[0]!r}")
        if items[2] != ("shell", "echo hi"):
            failures.append(f"第 3 个解析错误: {items[2]!r}")
        if parse_exec(raw) != items[0]:
            failures.append("parse_exec 应向后兼容地取首个")

    # 无围栏时不得吞掉后续 EXEC 块（旧实现会一路取到 [RESULT] 之前）
    bare = parse_exec_all("[EXEC: write] aaa\n[EXEC: shell] bbb\n[RESULT] z")
    if bare != [("write", "aaa"), ("shell", "bbb")]:
        failures.append(f"无围栏多 EXEC 解析错误: {bare!r}")

    if parse_exec_all("[CHECK] x\n[RESULT] y"):
        failures.append("无 EXEC 时应返回空列表")

    # 正文里引用 [EXEC: write] 这种散文提及，不能被当成真的执行请求
    prose = (
        "[方案]\n```text\n方案：用 [EXEC: write] 落盘，备选是只给文本\n```\n"
        "[RESULT] 产物\n"
    )
    if parse_exec_all(prose):
        failures.append(f"散文中的 [EXEC] 提及被误判为执行请求: {parse_exec_all(prose)!r}")
    return failures


def check_solution_parse() -> list[str]:
    """方案确认块 [方案]：有围栏取围栏，无围栏截到 [RESULT]，无块时为空。"""
    failures: list[str] = []

    fenced = (
        "[CHECK] x\n"
        "[方案]\n```text\n目的：解决连接瓶颈\n方案：连接池\n预期：QPS 200→1000\n```\n"
        "[RESULT] 产物"
    )
    got = parse_solution(fenced)
    if "连接池" not in got or "QPS 200→1000" not in got:
        failures.append(f"围栏式方案块解析错误: {got!r}")
    if "[RESULT]" in got or "产物" in got:
        failures.append(f"方案块不应含 [RESULT] 之后的产物: {got!r}")

    bare = "[方案]\n目的：A\n方案：B\n[RESULT] 产物"
    if parse_solution(bare) != "目的：A\n方案：B":
        failures.append(f"无围栏方案块解析错误: {parse_solution(bare)!r}")

    # 没有方案块的普通步骤不应凭空解析出内容
    if parse_solution("[CHECK] x\n[RESULT] 产物"):
        failures.append("无 [方案] 块时应返回空串")
    return failures


def check_gate_mode() -> list[str]:
    """闸门档位：step 每步骤一次 / auto 仅在需人决定时 / phase 回滚 / gate=None 恒不拦。"""
    failures: list[str] = []
    loop = ReActLoop.__new__(ReActLoop)  # 绕过构造，只测纯判定逻辑

    loop.gate = None
    loop.gate_mode = "step"
    if loop._should_gate("observe", "通过") or loop._should_gate("verify"):
        failures.append("gate=None 时不应拦人")

    loop.gate = lambda a, r="": ("continue", None)

    # plan 档（默认）：计划拦一次，步骤通过不拦，缺陷/验收必拦
    loop.gate_mode = "plan"
    for action, verdict, expect in (
        ("act", None, False),
        ("plan", None, True),
        ("observe", "通过", False),
        ("observe", "缺陷", True),
        ("observe", "不通过", True),
        ("verify", None, True),
    ):
        got = loop._should_gate(action, verdict)
        if got != expect:
            failures.append(f"plan 档 _should_gate({action},{verdict}) 期望 {expect} 实际 {got}")

    loop.gate_mode = "step"
    for action, verdict, expect in (
        ("think", None, False), ("plan", None, False), ("act", None, False),
        ("observe", "通过", True), ("observe", "缺陷", True), ("verify", None, True),
    ):
        got = loop._should_gate(action, verdict)
        if got != expect:
            failures.append(f"step 档 _should_gate({action},{verdict}) 期望 {expect} 实际 {got}")

    loop.gate_mode = "auto"
    for action, verdict, expect in (
        ("act", None, False),
        ("observe", "通过", False),
        ("observe", "缺陷", True),
        ("observe", "不通过", True),
        ("verify", None, True),
    ):
        got = loop._should_gate(action, verdict)
        if got != expect:
            failures.append(f"auto 档 _should_gate({action},{verdict}) 期望 {expect} 实际 {got}")

    # phase 档位由 _step 内拦，集中判定不再重复拦
    loop.gate_mode = "phase"
    if loop._apply_gate_if_needed("observe", "通过") is not False:
        failures.append("phase 档 _apply_gate_if_needed 应直接跳过")

    # 拦截原因可读（前端据此显示「为什么停」）
    loop.gate_mode = "auto"
    if "缺陷" not in loop._gate_reason("observe", "缺陷"):
        failures.append("_gate_reason 未体现缺陷")
    if loop._gate_reason("verify") != "最终验收":
        failures.append("_gate_reason(verify) 文案不符")
    return failures


def check_interrupt() -> list[str]:
    """随时插手：pause 就地阻塞、steer 注入账本、abort 置中止。"""
    failures: list[str] = []

    # --- QueueControl.peek 的非阻塞语义 ---
    from react.service import QueueControl

    q = QueueControl()
    if q.peek() is not None:
        failures.append("空队列 peek 应返回 None")
    q.submit("continue")  # 运行中的「继续」无意义，应被丢弃
    if q.peek() is not None:
        failures.append("运行中的 continue 应被 peek 丢弃")
    q.submit("pause")
    if q.peek() != ("pause", None):
        failures.append(f"peek(pause) 实际 {q.peek()!r}")
    q.submit("steer", "换个方向")
    if q.peek() != ("steer", "换个方向"):
        failures.append(f"peek(steer) 实际 {q.peek()!r}")

    # --- ReActLoop._check_interrupt 三种分支 ---
    loop = ReActLoop.__new__(ReActLoop)
    loop.gate_mode = "plan"
    loop._aborted = False
    loop.context = SessionContext()
    gates: list[tuple[str, str]] = []
    loop.gate = lambda a, r="": (gates.append((a, r)), ("continue", None))[1]

    loop.interrupt = lambda: ("pause", None)
    loop._check_interrupt()
    if gates != [("interrupt", "你按了暂停")]:
        failures.append(f"pause 未就地阻塞: {gates!r}")

    loop.interrupt = lambda: ("steer", "换个方向")
    loop._check_interrupt()
    if not any("人工纠偏：换个方向" in m["content"] for m in loop.context.messages):
        failures.append("steer 未注入账本")

    loop.interrupt = lambda: ("abort", None)
    loop._check_interrupt()
    if not loop._aborted:
        failures.append("abort 未置中止标志")

    # 已中止后不再处理；未接 interrupt 通道时也不应报错
    loop._check_interrupt()
    loop._aborted = False
    loop.interrupt = None
    loop._check_interrupt()
    return failures


def check_work_dir(base_dir: Path) -> list[str]:
    """工作目录解析：只认项目内子目录，越界/不存在一律回退并告警。"""
    from react.service import resolve_work_dir

    failures: list[str] = []

    # 空值 = 项目根目录
    d, w = resolve_work_dir(base_dir, None)
    if d != Path(base_dir).resolve() or w is not None:
        failures.append(f"空值应回退项目根目录，实际 {d} / {w}")

    # 项目内已存在的子目录 → 生效
    d, w = resolve_work_dir(base_dir, "skills")
    if d != (Path(base_dir) / "skills").resolve() or w is not None:
        failures.append(f"项目内子目录应生效，实际 {d} / {w}")

    # 项目内不存在的子目录 → 回退 + 告警
    d, w = resolve_work_dir(base_dir, "no_such_dir_xyz")
    if d != Path(base_dir).resolve() or not w:
        failures.append(f"不存在的子目录应回退并告警，实际 {d} / {w}")

    # 越出项目根目录 → 回退 + 告警（安全边界，绝不放宽）
    outside = base_dir.parent / "outside_root_xyz"
    d, w = resolve_work_dir(base_dir, outside)
    if d != Path(base_dir).resolve() or not w or "越出" not in w:
        failures.append(f"越界目录应回退并告警，实际 {d} / {w}")

    # 绝对路径形式的越界也要拦住
    d, w = resolve_work_dir(base_dir, "C:\\Windows")
    if d != Path(base_dir).resolve() or not w:
        failures.append(f"绝对路径越界未拦住，实际 {d} / {w}")

    # 显式授权后才放行项目外：默认不开，开了才生效
    outside = base_dir.parent
    d, w = resolve_work_dir(base_dir, outside, allow_outside=True)
    if d != outside.resolve() or w is not None:
        failures.append(f"allow_outside=True 应放行项目外目录，实际 {d} / {w}")
    # 不开时即便传了绝对路径也不放行（默认仍是收紧的）
    d, w = resolve_work_dir(base_dir, outside, allow_outside=False)
    if d != Path(base_dir).resolve() or not w:
        failures.append(f"默认（allow_outside=False）应仍拦住项目外，实际 {d} / {w}")
    # 相对路径即使开了授权也仍按项目内解析
    d, w = resolve_work_dir(base_dir, "skills", allow_outside=True)
    if d != (Path(base_dir) / "skills").resolve():
        failures.append(f"相对路径应仍按项目内解析，实际 {d}")
    return failures


def check_tool_window() -> list[str]:
    """窗口化不得切出孤儿 tool 消息（否则 API 直接 400）。

    两处都要盯住：
    - 落到窗口外、已被摘要的 assistant(tool_calls)+tool 对：整对一起离开窗口，
      绝不留下孤零零的 tool 回执（老实现硬取 history[-keep:] 就会切出孤儿）；
    - 落在窗口**内**的工具对：宿主 assistant 与回执必须同时在场、顺序正确。
    """
    from react.action import Action

    failures: list[str] = []

    class _Act:
        name = "act"
        skill_body = "skill"

    ctx = SessionContext(max_rounds=5)
    # 索引 1-2：工具对切在窗口外（压缩后整对进摘要，绝不能只剩回执）。
    ctx.add_user("任务")                                   # 0（任务锚点，始终保留）
    ctx.add_assistant("", tool_calls=[{"id": "call_1", "type": "function",
                                       "function": {"name": "decide_next_step",
                                                    "arguments": "{}"}}])   # 1
    ctx.add_tool("call_1", "ok")                            # 2
    for i in range(8):                                      # 3..10 填充
        ctx.add_user(f"填充 {i}")
    # 索引 11-12：工具对完整落在窗口内，用来验证「配对保留」这条正常路径。
    ctx.add_assistant("读取文件", tool_calls=[{"id": "call_2", "type": "function",
                                               "function": {"name": "read",
                                                            "arguments": '{"path":"a.py"}'}}])  # 11
    ctx.add_tool("call_2", "tool-out")                      # 12
    for i in range(5):                                      # 13..17
        ctx.add_user(f"填充后 {i}")

    msgs = ctx.build_step_messages(_Act(), "执行")
    body = msgs[1:]  # 去掉 system

    for i, m in enumerate(body):
        if m.get("role") == "tool":
            prev = body[i - 1] if i > 0 else None
            if prev is None or not prev.get("tool_calls"):
                failures.append(
                    f"第 {i} 条是孤儿 tool 消息（前一条 {prev and prev.get('role')}），"
                    "窗口化把宿主 assistant 切掉了"
                )

    # 窗口内的工具对必须完整保留：宿主 assistant 与它的回执都在
    ids_in_body = {m.get("tool_call_id") for m in body if m.get("role") == "tool"}
    if "call_2" not in ids_in_body:
        failures.append("窗口内的工具回执被丢弃（本应在窗口内完整保留）")
    if not any(m.get("tool_calls") for m in body):
        failures.append("窗口内缺少 assistant(tool_calls)，但 tool 回执却在——顺序错了")
    return failures


def check_sandbox_nested() -> list[str]:
    """沙箱在嵌套作业下的行为：不抛异常、job_effective 标志可读、breakaway 重试不破坏创建。

    非 Windows（sandbox_available 为 False）跳过。Windows 下会真实起一个 echo 进程，
    验证 run_sandboxed 在嵌套环境仍能正常执行，并暴露 last_run_job_effective 防护等级标志。
    """
    failures: list[str] = []
    import react.win32_sandbox as win32_sandbox

    if not win32_sandbox.sandbox_available():
        return failures  # 非 Windows 跳过

    # _is_in_job 在嵌套环境下应返回 bool 且不崩溃
    try:
        in_job = win32_sandbox._is_in_job()
        if not isinstance(in_job, bool):
            failures.append(f"_is_in_job 返回值类型异常: {type(in_job)}")
    except Exception as e:  # noqa: BLE001
        failures.append(f"_is_in_job 抛异常: {e}")

    # 真实执行一个安全命令：验证不抛异常、返回含 exit=0、job_effective 为 bool
    try:
        out = win32_sandbox.run_sandboxed("echo sandbox_ok", os.getcwd(), 10, False)
        if "exit=0" not in out:
            failures.append(f"run_sandboxed 输出异常（应含 exit=0）: {out!r}")
        flag = win32_sandbox.last_run_job_effective
        if not isinstance(flag, bool):
            failures.append(f"last_run_job_effective 类型异常: {type(flag)}")
    except Exception as e:  # noqa: BLE001
        failures.append(f"run_sandboxed 在嵌套环境抛异常: {e}")
    return failures


def check_live(result, context) -> tuple[list[str], str]:
    """live 专属断言：真实 API 兼容性与工具回写，并产出统计行。"""
    failures: list[str] = []
    if result.status not in ("done", "escalated", "max_rounds_exceeded"):
        failures.append(f"live 循环异常终止: {result.status}")
    assistants = [m for m in context.messages if m.get("role") == "assistant"]
    if not assistants:
        failures.append("live 未产生任何 assistant 响应")
    tool_msgs = [m for m in context.messages if m.get("role") == "tool"]
    tool_calls = [m for m in assistants if m.get("tool_calls")]
    stats = (f"live 统计：轮次={result.rounds} · tool_calls={len(tool_calls)} · "
             f"role=tool 回执={len(tool_msgs)}")
    return failures, stats

def check_native_tools(base_dir: Path) -> list[str]:
    """原生文件工具（对标 Claude Code）：run_tool 各工具 + read 默认上限 +
    越界拒绝 + ACT 原生工具循环（工具调用→执行→回写→产物定型）。"""
    failures: list[str] = []
    import tempfile

    from react.executor import LocalExecutor
    from react.loop import ReActLoop

    # 1) run_tool 各工具行为
    with tempfile.TemporaryDirectory() as _td:
        td = Path(_td)
        (td / "a.py").write_text("line1\nline2\nline3\nline4\nline5", encoding="utf-8")
        ex = LocalExecutor(cwd=td, allow_file_write=True)
        out = ex.run_tool("read", {"path": "a.py"})
        if "line1" not in out or "5 行" not in out:
            failures.append("read 工具未输出文件内容/行数")
        out2 = ex.run_tool("read", {"path": "a.py", "offset": 2, "limit": 2})
        if "line2" not in out2 or "line3" not in out2 or "line1" in out2:
            failures.append("read 工具 offset/limit 翻页不正确")
        if "已拒绝" not in ex.run_tool("read", {"path": "../outside.py"}):
            failures.append("read 工具未拒绝越界路径")
        if "已写入" not in ex.run_tool("write", {"path": "b.py", "content": "x = 1\n"}):
            failures.append("write 工具写入失败")
        if "已编辑" not in ex.run_tool(
                "edit", {"path": "b.py", "old_text": "x = 1", "new_text": "x = 2"}):
            failures.append("edit 工具替换失败")
        (td / "c.py").write_text("def foo():\n    pass\n", encoding="utf-8")
        if "c.py:1" not in ex.run_tool("grep", {"pattern": "def "}):
            failures.append("grep 工具未命中")
        if "a.py" not in ex.run_tool("glob", {"pattern": "*.py"}):
            failures.append("glob 工具未列出文件")
        if "已拒绝" not in ex.run_tool("shell", {"command": "echo hi"}):
            failures.append("shell 工具未按开关拒绝")

    # 1b) 非法参数不抛异常（模型参数不可信，抛异常会导致工具回执缺失 → API 400）
    with tempfile.TemporaryDirectory() as _td:
        td = Path(_td)
        (td / "a.py").write_text("line1\nline2\nline3\nline4\nline5", encoding="utf-8")
        ex = LocalExecutor(cwd=td)
        try:
            out = ex.run_tool("read", {"path": "a.py", "offset": "abc", "limit": -3})
            if "line1" not in out:
                failures.append("非法 offset/limit 未回退默认（应正常读文件）")
        except Exception as e:  # noqa: BLE001
            failures.append(f"非法 offset/limit 抛异常: {e}")

    # 2) read 默认行数上限：3000 行只读前 2000 + 翻页提示（对齐 Claude）
    with tempfile.TemporaryDirectory() as _td:
        td = Path(_td)
        (td / "big.py").write_text("\n".join(f"l{i}" for i in range(3000)),
                                   encoding="utf-8")
        ex = LocalExecutor(cwd=td)
        out = ex.run_tool("read", {"path": "big.py"})
        if "还有 1000 行未读" not in out:
            failures.append("read 未按默认 2000 行上限截断并提示翻页")
        seg = out.split("\n")
        if any(s.startswith("2001") for s in seg):
            failures.append("read 超限后仍输出了第 2001 行")

    # 3) ACT 原生工具循环：read 工具调用 → 执行 → role=tool 回写 → 产物定型
    from react.action import ActionRegistry
    from react.context import SessionContext
    from react.model import MockClient
    from react.render import RichRenderer
    from .run_all import _RecordingExecutor

    render = RichRenderer(None, show_reasoning=False)
    registry = ActionRegistry()
    registry.load(base_dir / "skills")
    model = MockClient(emit_file_tools=True)
    recorder = _RecordingExecutor()
    ctx = SessionContext(max_rounds=5)
    loop = ReActLoop(registry, ctx, model, render, gate=None,
                     ask=lambda q: "冒烟回答：输入已确认", executor=recorder)
    result = loop.run("原生工具测试任务")
    if result.status != "done":
        failures.append(f"原生工具循环未正常完成: {result.status}")
    if not any(name == "read" for name, _ in recorder.tool_calls):
        failures.append("ACT 未调用 read 原生工具")
    if not any(m.get("role") == "tool" for m in ctx.messages):
        failures.append("原生工具循环未回写 role=tool 消息")
    if not any("tool-out" in (m.get("content") or "") for m in ctx.messages):
        failures.append("原生工具执行结果未回写历史")
    if result.final_text and "冒烟测试产物" not in result.final_text:
        failures.append("工具循环最终产物应为 [RESULT] 内容")
    if not any("read" in str(t) for t in model.tools_seen):
        failures.append("未注入 read 原生工具（全阶段全量）")

    # 4) 工具执行抛异常时回执仍完备（否则下次 API 400）
    class _ThrowingExecutor:
        def run_tool(self, name, args):
            raise RuntimeError("boom")

        def run(self, kind, payload):
            return "ok"

    model2 = MockClient(emit_file_tools=True)
    recorder2 = _RecordingExecutor()
    ctx2 = SessionContext(max_rounds=5)
    loop2 = ReActLoop(registry, ctx2, model2, render, gate=None,
                      ask=lambda q: "冒烟回答：输入已确认", executor=_ThrowingExecutor())
    result2 = loop2.run("工具异常测试任务")
    if result2.status != "done":
        failures.append(f"工具异常时循环未正常完成: {result2.status}")
    for m in ctx2.messages:
        tcs = m.get("tool_calls")
        if m.get("role") == "assistant" and tcs:
            ids = {t.get("id") for t in tcs if t.get("id")}
            follow = [x for x in ctx2.messages
                      if x.get("role") == "tool"
                      and x.get("tool_call_id") in ids]
            if ids and len(follow) < len(ids):
                failures.append("工具执行异常后 assistant(tool_calls) 回执缺失")

    # 5) context 清理：缺回执的 assistant(tool_calls) 整对丢弃（防 API 400）
    from react.context import SessionContext as SC
    from react.action import ActionRegistry as AR2
    bad = SC(max_rounds=5)
    bad.add_user("任务")
    bad.add_assistant("", tool_calls=[{"id": "x1", "type": "function",
                                       "function": {"name": "read",
                                                    "arguments": '{"path":"a.py"}'}}])
    # 故意不写 x1 的回执（模拟回执缺失）
    bad.add_assistant("[RESULT] 后续产物")
    registry2 = AR2()
    registry2.load(base_dir / "skills")
    act2 = registry2.get("act")
    msgs = bad.build_step_messages(act2, "当前步骤指令")
    body = "\n".join(m.get("content") or "" for m in msgs)
    if any(m.get("tool_calls") for m in msgs):
        failures.append("缺回执的 assistant(tool_calls) 未被清理（仍会 400）")
    return failures

def check_token_stats(base_dir: Path) -> list[str]:
    """会话级 token 统计：loop 记录、usage 含 cached、持久化与汇总。"""
    failures: list[str] = []
    import tempfile

    from react.action import ActionRegistry
    from react.context import SessionContext
    from react.model import MockClient
    from react.render import RichRenderer
    from react.token_stats import (load_session_stats, list_session_stats,
                                   save_session_stats, summarize)
    from .run_all import _RecordingExecutor

    render = RichRenderer(None, show_reasoning=False)
    registry = ActionRegistry()
    registry.load(base_dir / "skills")
    model = MockClient(emit_file_tools=True)
    recorder = _RecordingExecutor()
    ctx = SessionContext(max_rounds=5)
    loop = ReActLoop(registry, ctx, model, render, gate=None,
                     ask=lambda q: "冒烟回答：输入已确认", executor=recorder)
    loop.run("token 统计测试任务")

    if not loop.token_stats:
        failures.append("loop 未记录任何 token 调用明细")
    if not any(r.get("phase") == "act" for r in loop.token_stats):
        failures.append("token 明细缺少 ACT 阶段记录")
    if not any(r.get("usage", {}).get("cached") is not None for r in loop.token_stats):
        failures.append("usage 缺少 cached（缓存命中）字段")
    if not all(r.get("ts") for r in loop.token_stats):
        failures.append("token 明细缺少时间戳")

    # 持久化 + 汇总
    with tempfile.TemporaryDirectory() as _td:
        base = Path(_td)
        save_session_stats(base, "test_sid", {"task": "测试", "status": "done",
                                              "rounds": 2, "created": "2026-01-01 00:00:00"},
                           loop.token_stats)
        data = load_session_stats(base, "test_sid")
        if data is None or data["session_id"] != "test_sid":
            failures.append("会话统计持久化/读取失败")
        if data["task"] != "测试" or data["rounds"] != 2:
            failures.append("会话统计元数据丢失")
        sm = summarize(loop.token_stats)
        if sm["calls"] != len(loop.token_stats):
            failures.append("汇总调用次数与明细不一致")
        if sm["cached_tokens"] <= 0:
            failures.append("汇总缓存命中 token 为 0（MockClient 应提供缓存值）")
        listing = list_session_stats(base)
        if len(listing) != 1 or listing[0]["summary"]["calls"] != len(loop.token_stats):
            failures.append("会话列表汇总错误")
    return failures
