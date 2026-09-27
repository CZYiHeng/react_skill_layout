"""缓存前缀探针：离线测量「发给模型的消息列表」在连续调用间的可复用程度。

为什么需要它：`usage.cached` 是 provider 侧的结果，只有真实调用才有。本探针不联网，
直接把每一步的**入参消息列表**截下来，比较相邻两次调用的最长公共前缀——前缀能不能
复用是确定性的，不必等线上命中率才知道改对了没有。

用法（项目根目录）：
    .venv\\Scripts\\python.exe tools\\cache_probe.py

输出三组场景 × 两种实现的「逐字复用率」：
  · legacy  = 改造前的滑动窗口（每次调用按「最近 N 条」重算窗口 + 重算摘要）
  · 当前    = 只追加式压缩（本仓库现在的实现）

关键指标是**逐字复用率**：本步输入里有多少字符与上一次输入的前缀完全相同。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

try:  # 让中文在 GBK 控制台也能正常输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from react.action import ActionRegistry  # noqa: E402
from react.context import GLOBAL_PROTOCOL, SessionContext  # noqa: E402
from react.loop import ReActLoop  # noqa: E402
from react.model import MockClient  # noqa: E402
from react.service import NullRenderer  # noqa: E402


def _msg_key(m: dict) -> str:
    """消息的可比较指纹：正文 + 工具调用，忽略键序。"""
    return json.dumps(
        [m.get("role"), m.get("content"), m.get("tool_calls"), m.get("tool_call_id")],
        ensure_ascii=False, sort_keys=True, default=str,
    )


def _common_prefix_len(a: list[dict], b: list[dict]) -> int:
    n = 0
    while n < min(len(a), len(b)) and _msg_key(a[n]) == _msg_key(b[n]):
        n += 1
    return n


class RecordingModel:
    """包住 MockClient，记录每次调用的入参消息列表。"""

    def __init__(self, inner):
        self._inner = inner
        self.wire: list[list[dict]] = []

    def complete(self, messages, on_token=None, tools=None):
        self.wire.append([dict(m) for m in messages])
        return self._inner.complete(messages, on_token=on_token, tools=tools)


def install_legacy_window() -> None:
    """把 SessionContext 换回改造前的滑动窗口语义（用于 A/B 对比）。

    还原点有二：
    1. 每次调用都按「最近 max_context_messages 条」重算窗口起点（`_window_start`）；
    2. 摘要随窗口一起重算，而不是冻结。
    于是窗口每滑动一条，历史**中段**就被改写一次。
    """
    def _legacy_window_start(self, keep: int) -> int:
        start = max(1, len(self.messages) - keep)
        while start > 1 and self.messages[start].get("role") == "tool":
            start -= 1
        return start

    def _legacy_build(self, action, step_prompt):
        history = self.messages
        digest = ""
        if self.max_context_messages > 0 and len(history) > self.max_context_messages:
            keep = self.max_context_messages
            head = history[:1]
            tail = history[self._legacy_window_start(keep):]
            dropped = history[1:len(history) - len(tail)]
            if dropped:
                shown = dropped[-20:]
                omitted = len(dropped) - len(shown)
                lines = [f"- [{m.get('role')}] {(m.get('content') or '')[:140]}"
                         for m in shown]
                if omitted > 0:
                    lines.insert(0, f"- （更早 {omitted} 条已省略）")
                digest = ("# 历史摘要（较早消息已压缩，仅供定位；完整轨迹见上方消息）\n"
                          + "\n".join(lines))
            windowed = [*head, *tail]
        else:
            windowed = list(history)

        cleaned: list[dict] = []
        i = 0
        while i < len(windowed):
            m = windowed[i]
            tcs = m.get("tool_calls")
            if m.get("role") == "assistant" and tcs:
                needed = {t.get("id") for t in tcs if t.get("id")}
                j = i + 1
                got = set()
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
                continue
            cleaned.append(m)
            i += 1
        windowed = cleaned

        env_block = f"# 运行环境\n{self.env_info}\n\n" if self.env_info else ""
        system = f"{GLOBAL_PROTOCOL}\n\n{env_block}"
        stage = (f"# 当前阶段：{action.name.upper()}\n\n"
                 f"{action.skill_body}\n\n# 当前步骤指令\n{step_prompt}")
        tail_user = f"{stage}\n\n{digest}" if digest else stage
        return [{"role": "system", "content": system}, *windowed,
                {"role": "user", "content": tail_user}]

    SessionContext._legacy_window_start = _legacy_window_start
    SessionContext.build_step_messages = _legacy_build


def metrics(wire: list[list[dict]]) -> dict:
    """统计逐字复用率与「整份上一步列表被原样复用」的比例。"""
    total = reuse = 0
    stable = 0
    ratios = []
    for prev, cur in zip(wire, wire[1:]):
        n = _common_prefix_len(prev, cur)
        reuse_n = sum(len(_msg_key(m)) for m in cur[:n])
        t = sum(len(_msg_key(m)) for m in cur)
        reuse += reuse_n
        total += t
        stable += 1 if n == len(prev) else 0
        ratios.append(reuse_n / t if t else 0.0)
    return {
        "calls": len(wire),
        "byte_reuse": reuse / total if total else 0.0,
        "stable_prefix": stable / (len(wire) - 1) if len(wire) > 1 else 0.0,
        "ratios": ratios,
    }


def scenario_loop(budget: int, repeats: int) -> list[list[dict]]:
    """真实 agent 循环：同一个 SessionContext 连续跑多个任务。"""
    registry = ActionRegistry()
    registry.load(BASE_DIR / "skills")
    ctx = SessionContext(max_rounds=6, max_context_messages=budget)
    model = RecordingModel(MockClient(verify_fail_once=True, observe_defect_once=True))
    loop = ReActLoop(registry, ctx, model, NullRenderer(), gate=None,
                     ask=lambda q: "冒烟回答：输入已确认")
    for r in range(repeats):
        loop.run(f"缓存探针任务 {r}：走完 THINK/PLAN/ACT/OBSERVE/VERIFY 全链路")
    return model.wire


def scenario_append(budget: int, steps: int) -> list[list[dict]]:
    """合成负载：账本纯追加 N 步，每步组装一次（隔离出窗口策略本身的影响）。"""
    from react.action import Action

    ctx = SessionContext(max_context_messages=budget)
    for i in range(budget + 4):
        ctx.add_user(f"初始 {i}")
        ctx.add_assistant(f"应答 {i}")
    act = Action(name="act", skill_body="阶段正文")
    wire = []
    for i in range(steps):
        ctx.add_user(f"追加 {i}")
        wire.append([dict(m) for m in ctx.build_step_messages(act, "执行步骤")])
    return wire


def main() -> int:
    scenarios = [
        ("真实循环 cap=8 ×3 任务", lambda: scenario_loop(8, 3)),
        ("真实循环 cap=16 ×3 任务", lambda: scenario_loop(16, 3)),
        ("纯追加 cap=16 ×80 步", lambda: scenario_append(16, 80)),
        ("纯追加 cap=60 ×120 步", lambda: scenario_append(60, 120)),
    ]

    print(f"{'场景':<24}{'实现':<8}{'调用':>5}{'逐字复用率':>12}{'提升':>9}")
    print("-" * 60)
    for title, fn in scenarios:
        wire_new = fn()
        m_new = metrics(wire_new)
        # 改造前实现（同一进程内替换方法后重跑同一负载）
        original = SessionContext.build_step_messages
        install_legacy_window()
        try:
            wire_old = fn()
        finally:
            SessionContext.build_step_messages = original
        m_old = metrics(wire_old)

        delta = m_new["byte_reuse"] - m_old["byte_reuse"]
        print(f"{title:<24}{'legacy':<8}{m_old['calls']:>5}{m_old['byte_reuse']:>11.1%}{'—':>9}")
        print(f"{'':<24}{'当前':<8}{m_new['calls']:>5}{m_new['byte_reuse']:>11.1%}"
              f"{delta:>+9.1%}")
        print("-" * 60)

    print()
    print("逐字复用率 = 本步输入里与上一次输入前缀逐字节相同的字符占比（越高越省）。")
    print("尾部那条 user（阶段名 + skill 正文 + 当前指令）每步必变，所以不可能到 100%；")
    print("真实循环里阶段每步轮换，也会让尾部差异变大——那是本来就无法复用的部分。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
