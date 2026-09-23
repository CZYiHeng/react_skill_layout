"""模型封装：ModelClient 协议 + OpenAI 兼容客户端（原生工具调用）+ 确定性 Mock 客户端。

控制信号（THINK 决策、OBSERVE/VERIFY 判定）优先走原生工具调用（结构化参数，免正则）；
产物正文（PLAN 步骤表、ACT 的 RESULT）仍走自由文本，保持人类可读与流式可见。

注意：kimi 思考模式（reasoning）与强制 tool_choice 不兼容，只能 tool_choice="auto"，
故工具调用可能缺失——调用方须保留文本解析作为兜底（见 react/loop.py 的 _resolve）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable, Protocol

StreamCallback = Callable[[str], None]  # 每个内容增量回调


class ModelError(Exception):
    """模型调用失败（网络/限流/响应异常）。"""


class ModelClient(Protocol):
    def complete(self, messages: list[dict], on_token: StreamCallback | None = None,
                 tools: list[dict] | None = None) -> "ModelResponse": ...


@dataclass
class ModelResponse:
    text: str
    tokens: int
    elapsed_sec: float
    reasoning: str = ""        # 模型的推理过程（kimi 的 reasoning_content；可能为空）
    tool_name: str = ""        # 模型调用的工具名（无调用时为空串）
    tool_args: dict | None = None  # 工具参数（已解析 JSON；无调用时为 None）
    tool_calls: list | None = None  # 完整工具调用数组（OpenAI 规范，含 id/name/arguments）
    usage: dict | None = None  # 完整 token 用量 {prompt, completion, total}


def _safe_json(raw: str) -> dict:
    """宽松解析工具参数字符串；损坏时返回空 dict，不抛异常。"""
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {"_value": value}


# 永久性错误：重试多少次结果都一样，立即失败并给出针对性提示
_PERMANENT_STATUS = (400, 401, 403, 404, 422)
_PERMANENT_EXC_NAMES = ("AuthenticationError", "PermissionDeniedError",
                        "BadRequestError", "NotFoundError", "UnprocessableEntityError")
_STATUS_HINT = {
    400: "请求被拒绝：多为上下文超长或消息格式问题，检查 max_context_messages。",
    401: "api_key 无效或已过期：检查 config.json 的 api_key / base_url，"
         "注意环境变量 REACT_AGENT_API_KEY 会覆盖文件配置，且不同端点的 key 不通用。",
    403: "无权限访问该模型或端点，确认账号已开通对应服务。",
    404: "模型名或端点路径不存在，检查 model 与 base_url（路径后缀如 /v1 不能少）。",
    422: "请求参数不合法，检查 model 名称与工具定义。",
}


def _permanent_status(err: Exception) -> int | None:
    """判定是否为「重试无意义」的永久性错误；返回 HTTP 状态码或 None。"""
    code = getattr(err, "status_code", None)
    if isinstance(code, int) and code in _PERMANENT_STATUS:
        return code
    if type(err).__name__ in _PERMANENT_EXC_NAMES:
        return code if isinstance(code, int) else 0
    return None


class OpenAIClient:
    """OpenAI 兼容 endpoint（kimi 等）。流式输出 + 原生工具调用，失败重试 2 次（退避 1s/2s）。

    鉴权/参数类错误（4xx 中的永久性错误）不重试——重试只会让用户多等 3 秒再看到同一个 401。
    """

    def __init__(self, base_url: str, api_key: str, model: str, timeout_sec: int = 120):
        from openai import OpenAI  # 延迟导入，Mock 模式无需安装

        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_sec)
        self._model = model

    def complete(self, messages: list[dict], on_token: StreamCallback | None = None,
                 tools: list[dict] | None = None) -> ModelResponse:
        last_err: Exception | None = None
        attempt = 0
        for wait in (0, 1.0, 2.0):
            if wait:
                time.sleep(wait)
            try:
                return self._call_once(messages, on_token, tools)
            except Exception as e:  # noqa: BLE001 - 统一归一为 ModelError
                last_err = e
                attempt += 1
                code = _permanent_status(e)
                if code is not None:
                    hint = _STATUS_HINT.get(code, "")
                    suffix = (f"（不重试：永久性错误）｜{hint}" if hint
                              else "（不重试：永久性错误）")
                    raise ModelError(f"模型调用失败{suffix}: {e}")
        raise ModelError(f"模型调用失败（已重试 {attempt - 1} 次）: {last_err}")

    def _call_once(self, messages: list[dict], on_token: StreamCallback | None,
                   tools: list[dict] | None) -> ModelResponse:
        start = time.monotonic()
        parts: list[str] = []
        reasoning_parts: list[str] = []
        # index -> {id, name, args}，流式分片按 index 拼接（kimi 的 id 通常随首个分片给出）
        tool_buf: dict[int, dict] = {}

        kwargs: dict = dict(model=self._model, messages=messages, stream=True,
                            stream_options={"include_usage": True})
        if tools:
            kwargs["tools"] = tools
            # kimi 思考模式不支持强制 tool_choice，只能用 auto
            kwargs["tool_choice"] = "auto"

        stream = self._client.chat.completions.create(**kwargs)
        tokens = 0
        usage = None
        for chunk in stream:
            if getattr(chunk, "usage", None):
                u = chunk.usage
                tokens = u.completion_tokens or 0
                usage = {
                    "prompt": u.prompt_tokens or 0,
                    "completion": u.completion_tokens or 0,
                    "total": u.total_tokens or 0,
                    "cached": (getattr(getattr(u, "prompt_tokens_details", None),
                                       "cached_tokens", None) or 0),
                }
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            # kimi 等模型的深度推理放在 reasoning_content，正式输出在 content
            reason = getattr(delta, "reasoning_content", None) or ""
            if reason:
                reasoning_parts.append(reason)
            text = getattr(delta, "content", None) or ""
            if text:
                parts.append(text)
                if on_token:
                    on_token(text)
            # 原生工具调用：流式分片按 index 累加 id / name / arguments
            for tc in (getattr(delta, "tool_calls", None) or []):
                idx = getattr(tc, "index", 0) or 0
                slot = tool_buf.setdefault(idx, {"id": "", "name": "", "args": ""})
                # OpenAI 流式规范：id 在 tool_call 本体上，**不在** function 上。
                # 此前只从 function.id 取，恒为空，导致一回退到伪造的 call_N。
                fn = getattr(tc, "function", None)
                tid = getattr(tc, "id", None) or (getattr(fn, "id", None) if fn else None)
                if tid:
                    slot["id"] = tid
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] += fn.name
                    if getattr(fn, "arguments", None):
                        slot["args"] += fn.arguments

        tool_name, tool_args, tool_calls = "", None, None
        if tool_buf:
            first = tool_buf[min(tool_buf)]
            tool_name = first["name"]
            tool_args = _safe_json(first["args"])
            # 完整回写结构：供调用方按 OpenAI 规范补 assistant(tool_calls) + role=tool。
            # 必须**逐个**回写：声明 N 个 tool_call 却只补 1 条 tool 消息会被 API 判 400
            # （insufficient tool messages following tool_calls message）。
            tool_calls = [
                {
                    "id": slot.get("id") or f"call_{idx}",
                    "type": "function",
                    "function": {"name": slot["name"],
                                 "arguments": slot.get("args") or "{}"},
                }
                for idx, slot in sorted(tool_buf.items())
            ]

        return ModelResponse(text="".join(parts), tokens=tokens,
                             elapsed_sec=time.monotonic() - start,
                             reasoning="".join(reasoning_parts),
                             tool_name=tool_name, tool_args=tool_args,
                             tool_calls=tool_calls, usage=usage)


class MockClient:
    """确定性假模型：按步骤类型返回固定结构，用于 --smoke（零 API 消耗）。

    提供 tools 时，控制阶段（THINK/OBSERVE/VERIFY）同时返回对应的工具调用，
    以验证"工具调用为主 + 文本兜底"的双通道。
    """

    def __init__(self, verify_fail_once: bool = False, observe_defect_once: bool = False,
                 emit_exec: bool = False, emit_file_tools: bool = False):
        self.calls: list[str] = []         # 记录被调用的 system 提示，供断言
        self.tools_seen: list[tuple] = []  # 每次调用被注入的工具名，供断言
        self.verify_fail_once = verify_fail_once
        self._verify_failed = False
        self.observe_defect_once = observe_defect_once
        self._observed_defect = False
        self.emit_exec = emit_exec         # ACT 是否附带 [EXEC: shell] 块（验证执行器接线）
        self.emit_file_tools = emit_file_tools  # ACT 是否调用原生 read 工具（验证工具循环）
        self._file_tool_step = 0

    def complete(self, messages: list[dict], on_token: StreamCallback | None = None,
                 tools: list[dict] | None = None) -> ModelResponse:
        system = messages[0]["content"]
        self.calls.append(system)
        history = "\n".join(m["content"] for m in messages[1:])
        tool_names = {t["function"]["name"] for t in (tools or [])}
        self.tools_seen.append(tuple(sorted(tool_names)))

        text, tname, targs = "", "", None

        if "# 当前阶段：THINK" in system:
            # 完成检测以 OBSERVE 的"通过"判定为准（缺陷/重试不算完成）
            done = "[OBSERVATION] 通过" in history
            answered = "冒烟回答" in history
            if not done and not answered:
                # 首轮：走 ASK 分支验证提问→回答→继续的链路
                decision, reason = "ASK", "必须先向用户确认才能执行"
                text = ("[THOUGHT] 现状：冒烟测试任务，缺少关键输入。\n"
                        "决策理由：必须先向用户确认才能执行。\n下一步: ASK")
            else:
                decision = "DONE" if done else "ACT"
                reason = "产物已产出且通过，进入验收" if done else "输入已确认，可以执行"
                text = f"[THOUGHT] 现状：冒烟测试任务。决策理由：{reason}。\n下一步: {decision}"
            if "decide_next_step" in tool_names:
                tname, targs = "decide_next_step", {"decision": decision, "reason": reason}
        elif "# 当前阶段：PLAN" in system:
            text = ("[PLAN]\n"
                    "1. 完成冒烟步骤一 | 完成标准：包含甲内容\n"
                    "2. 完成冒烟步骤二 | 完成标准：包含乙内容")
        elif "# 当前阶段：ACT" in system:
            if self.emit_file_tools:
                # 原生工具循环：首次 ACT 调 read 工具，二次产出最终产物
                self._file_tool_step += 1
                if self._file_tool_step == 1:
                    tname, targs = "read", {"path": "src/main.py", "offset": 1, "limit": 20}
                    text = "[CHECK] 本步骤成功标准：包含甲内容\n（先读取文件确认现状）"
                else:
                    text = ("[CHECK] 本步骤成功标准：包含甲内容\n"
                            "[RESULT] 冒烟测试产物：已读取文件并产出符合预期的结果。")
            else:
                exec_block = "[EXEC: shell]\n```bash\necho 冒烟执行\n```\n" if self.emit_exec else ""
                text = ("[CHECK] 本步骤成功标准：包含甲内容\n"
                        + exec_block
                        + "[RESULT] 冒烟测试产物：步骤执行完毕，输出符合预期。")
        elif "# 当前阶段：OBSERVE" in system:
            if self.observe_defect_once and not self._observed_defect:
                self._observed_defect = True
                verdict, reason = "defect", "产物缺少关键内容 X（mock 首次核对故意判缺陷）"
                text = "[OBSERVATION] 缺陷：产物缺少关键内容 X（mock 首次核对故意判缺陷）。"
            else:
                verdict, reason = "pass", "产物完整，格式正确"
                text = "[OBSERVATION] 通过：产物完整，格式正确。"
            if "submit_verdict" in tool_names:
                tname, targs = "submit_verdict", {"verdict": verdict, "reason": reason}
        elif "# 当前阶段：VERIFY" in system:
            if self.verify_fail_once and not self._verify_failed:
                self._verify_failed = True
                verdict, reason = "fail", "产物缺少关键内容（mock 首次验收故意不通过）"
                text = "[VERIFY] 不通过：产物缺少关键内容（mock 首次验收故意不通过）。"
            else:
                verdict, reason = "pass", "任务目标已达成"
                text = "[VERIFY] 通过：任务目标已达成。"
            if "submit_verdict" in tool_names:
                tname, targs = "submit_verdict", {"verdict": verdict, "reason": reason}
        else:
            text = "[RESULT] 未知阶段"

        if on_token:
            on_token(text)
        tool_calls = None
        if tname:
            # 确定性构造工具调用结构，验证"工具调用通道 + role=tool 回写"链路
            tool_calls = [{
                "id": "call_mock_0",
                "type": "function",
                "function": {"name": tname,
                             "arguments": json.dumps(targs or {}, ensure_ascii=False)},
            }]
        _tok = len(text) // 2
        return ModelResponse(text=text, tokens=_tok, elapsed_sec=0.01,
                             tool_name=tname, tool_args=targs, tool_calls=tool_calls,
                             usage={"prompt": 200, "completion": _tok,
                                    "total": 200 + _tok, "cached": 150})
