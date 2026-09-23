"""冒烟断言集：按主题拆分，每个函数返回失败说明列表（空列表=通过）。

断言文案与改造前 main.py 内的版本保持一致，确保 `--smoke` 输出不变。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from react.action import Action
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
    # 原生工具调用通道：THINK 注入 decide_next_step，OBSERVE/VERIFY 注入 submit_verdict
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
    """问题③：上下文窗口化——压缩旧消息、限制发送条数、不动全量账本。"""
    failures: list[str] = []
    _c = SessionContext(max_context_messages=4)
    for i in range(20):
        _c.add_user(f"u{i}")
        _c.add_assistant(f"a{i}")
    _msgs = _c.build_step_messages(Action(name="think", skill_body="正文"), "指令")
    if "历史摘要" not in _msgs[0]["content"]:
        failures.append("上下文窗口化未生成历史摘要")
    if len(_msgs) > 1 + 1 + 4 + 1:  # system + 任务锚点 + 最近 4 条 + 当前指令
        failures.append(f"上下文窗口化未限制发送条数: {len(_msgs)}")
    if len(_c.messages) != 40:
        failures.append("窗口化不应改动全量账本")
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
            if "REACT_AGENT_API_KEY" not in str(e):
                failures.append("401 报错缺少排查提示")
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

    构造：assistant(tool_calls) 落在窗口外、紧跟的 role=tool 落在窗口内。
    """
    from react.action import Action

    failures: list[str] = []

    class _Act:
        name = "act"
        skill_body = "skill"

    ctx = SessionContext(max_rounds=5, max_context_messages=16)
    # 关键构造：让 assistant(tool_calls) 落在窗口边界之外，而它的 tool 回执正好是窗口首条。
    # 窗口起点 = len - keep = 18 - 16 = 2，故把 tool 放在索引 2、宿主 assistant 放在索引 1。
    ctx.add_user("任务")                                   # 0（任务锚点，始终保留）
    ctx.add_assistant("", tool_calls=[{"id": "call_1", "type": "function",
                                       "function": {"name": "decide_next_step",
                                                    "arguments": "{}"}}])   # 1 会被切掉
    ctx.add_tool("call_1", "ok")                            # 2 窗口首条 → 若不管就是孤儿
    for i in range(15):                                     # 3..17 填满窗口
        ctx.add_user(f"填充 {i}")

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

    # 正常情况：assistant(tool_calls) 与其 tool 回执必须同时存在
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

def check_native_tools() -> list[str]:
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
    registry.load(Path(r"G:\react-agent\skills"))
    model = MockClient(emit_file_tools=True)
    recorder = _RecordingExecutor()
    ctx = SessionContext(max_rounds=5, max_context_messages=12)
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
        failures.append("ACT 阶段未注入 read 原生工具")
    return failures
