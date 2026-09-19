"""输出显示模板拦截层：ACT 产物按内容类型选择展示模板。

检测顺序（先显式声明，后自动识别）：
  1. [FORMAT: json|table|plan|diff|code|md] 显式标签（最高优先级）
  2. 整体合法 JSON
  3. 整体围栏代码块（带语言）
  4. diff 行模式
  5. 纯 Markdown 表格
  6. 编号计划清单
  7. 兜底 Markdown 渲染
"""

from __future__ import annotations

import json
import re
from typing import Any

from rich.console import Group, RenderableType
try:
    from rich.markdown import Markdown
    _HAS_MD = True
except ModuleNotFoundError:  # markdown-it-py 未安装时用内置迷你渲染器
    Markdown = None
    _HAS_MD = False
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

KIND_LABELS = {
    "json": "json",
    "code": "code",
    "table": "table",
    "plan": "plan",
    "diff": "diff",
    "markdown": "md",
}

_FORMAT_RE = re.compile(r"\[FORMAT:\s*(json|table|plan|diff|code|md|markdown)\s*\]", re.IGNORECASE)
_WHOLE_FENCE_RE = re.compile(r"\A\s*```(\w*)[ \t]*\n(.*?)```[ \t]*\Z", re.DOTALL)
_NUM_ITEM_RE = re.compile(r"^\s*\d+[.、)]\s*(.+?)\s*$", re.MULTILINE)


# ---------------------------------------------------------------------------
# 检测
# ---------------------------------------------------------------------------

def classify(content: str) -> tuple[str, dict[str, Any]]:
    """返回 (kind, meta)。meta 含 cleaned（去掉 FORMAT 标签后的正文）与模板元数据。"""
    # ① 显式标签（优先级最高，覆盖一切自动识别）
    m = _FORMAT_RE.search(content)
    if m:
        kind = m.group(1).lower()
        kind = "markdown" if kind == "md" else kind
        cleaned = _FORMAT_RE.sub("", content).strip()
        return kind, {"declared": True, "cleaned": cleaned}

    text = content.strip()

    # ② 整体合法 JSON
    if _is_json(text):
        return "json", {"declared": False, "cleaned": text}

    # ③ 整体围栏代码块
    fence = _WHOLE_FENCE_RE.match(text)
    if fence:
        lang = fence.group(1).lower()
        inner = fence.group(2).rstrip("\n")
        if lang == "json" and _is_json(inner):
            return "json", {"declared": False, "cleaned": inner}
        return "code", {"declared": False, "cleaned": inner, "lang": lang or "text"}

    # ④ diff 行模式
    if _looks_like_diff(text):
        return "diff", {"declared": False, "cleaned": text}

    # ⑤ 纯 Markdown 表格
    blocks = _table_blocks(text)
    if blocks and not _non_table_lines(text, blocks):
        return "table", {"declared": False, "cleaned": text, "blocks": blocks}

    # ⑥ 编号计划清单
    items = _plan_items(text)
    if items:
        return "plan", {"declared": False, "cleaned": text, "items": items}

    # ⑦ 兜底
    return "markdown", {"declared": False, "cleaned": text}


def _is_json(text: str) -> bool:
    if not text or text[0] not in "{[":
        return False
    try:
        json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    return True


def _looks_like_diff(text: str) -> bool:
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    if any(l.startswith(("diff --git", "@@", "+++ ", "--- ", "index ")) for l in lines):
        return True

    def _is_change(line: str) -> bool:
        # 排除 markdown 列表符（"- 项" / "* 项"），只认 diff 的增删行
        if line.startswith(("- ", "* ")):
            return False
        return line.startswith(("+", "-")) and not line.startswith(("+++", "---"))

    changed = sum(1 for l in lines if _is_change(l))
    has_plus = any(l.startswith("+") and not l.startswith(("+++", "+ ")) for l in lines)
    has_minus = any(l.startswith("-") and not l.startswith(("---", "- ")) for l in lines)
    # 必须同时存在增/删两侧，防止纯列表文档误判
    return has_plus and has_minus and changed / len(lines) >= 0.3


def _table_blocks(text: str) -> list[list[str]]:
    """提取连续管道表格块；每个块至少含表头+分隔行。"""
    blocks: list[list[str]] = []
    cur: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and s.count("|") >= 2:
            cur.append(s)
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    valid = []
    for b in blocks:
        if len(b) >= 2 and re.fullmatch(r"\|[\s:\-|]+\|", b[1]):
            valid.append(b)
    return valid


def _non_table_lines(text: str, blocks: list[list[str]]) -> list[str]:
    """正文里不属于任何表格块的非空行（用于判断是否为'纯表格'）。"""
    table_lines: set[str] = set()
    for b in blocks:
        table_lines.update(b)
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s and s not in table_lines:
            out.append(s)
    return out


def _plan_items(text: str) -> list[str]:
    """编号清单：≥2 项且覆盖 ≥60% 非空行才判为 plan，防误判。"""
    items = _NUM_ITEM_RE.findall(text)
    nonempty = [l for l in text.splitlines() if l.strip()]
    if len(items) >= 2 and nonempty and len(items) / len(nonempty) >= 0.6:
        return [re.sub(r"\*\*(.+?)\*\*", r"\1", it) for it in items]
    return []


# ---------------------------------------------------------------------------
# 模板构建
# ---------------------------------------------------------------------------

def _markdown(text: str) -> RenderableType:
    """Markdown 渲染：装了 markdown-it-py 用 rich.Markdown，否则用内置迷你渲染器。"""
    if _HAS_MD:
        return Markdown(text)
    return _mini_markdown(text)


def _md_inline(s: str) -> str:
    """行内样式转 rich markup（先转义 [ 再应用 **粗体** / `代码`）。"""
    s = s.replace("[", r"\[")
    s = re.sub(r"\*\*(.+?)\*\*", r"[bold]\1[/bold]", s)
    s = re.sub(r"`([^`]+)`", r"[dark_cyan]\1[/]", s)
    return s


def _mini_markdown(text: str) -> RenderableType:
    """零依赖迷你 Markdown：围栏代码/标题/列表/粗体，够展示产物用。"""
    parts: list[RenderableType] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            parts.append(Text("\n".join(buf)))
            buf.clear()

    in_fence = False
    fence_lang = ""
    fence_buf: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("```"):
            if not in_fence:
                flush()
                in_fence, fence_lang, fence_buf = True, s[3:].strip(), []
            else:
                parts.append(Syntax("\n".join(fence_buf), fence_lang or "text",
                                    theme="monokai", word_wrap=True))
                in_fence = False
            continue
        if in_fence:
            fence_buf.append(line)
            continue
        if s.startswith("### "):
            flush()
            parts.append(Text(s[4:], style="bold cyan"))
        elif s.startswith("## "):
            flush()
            parts.append(Text(s[3:], style="bold green"))
        elif s.startswith("# "):
            flush()
            parts.append(Text(s[2:], style="bold white"))
        elif re.match(r"^[-*] ", s):
            buf.append("  • " + _md_inline(s[2:]))
        elif _NUM_ITEM_RE.match(line):
            buf.append("  " + _md_inline(s))
        elif s:
            buf.append(_md_inline(s))
        else:
            buf.append("")
    flush()
    if in_fence and fence_buf:
        parts.append(Syntax("\n".join(fence_buf), fence_lang or "text", theme="monokai"))
    return Group(*parts) if parts else Text("")


def build_renderable(kind: str, meta: dict[str, Any]) -> RenderableType:
    """按内容类型构建 rich 可渲染对象；识别失败安全回退 Markdown。"""
    cleaned = meta.get("cleaned", "")

    if kind == "json":
        try:
            obj = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            return _markdown(cleaned)
        pretty = json.dumps(obj, indent=2, ensure_ascii=False)
        return Syntax(pretty, "json", theme="monokai", word_wrap=True)

    if kind == "code":
        return Syntax(cleaned, meta.get("lang") or "text",
                      theme="monokai", word_wrap=True)

    if kind == "table":
        blocks = meta.get("blocks", [])
        if not blocks:  # 声明了表格但解析不出（内容不是表格）→ 回退 Markdown
            return _markdown(cleaned)
        return Group(*(_build_table(b) for b in blocks))

    if kind == "plan":
        body = Text()
        for i, item in enumerate(meta.get("items", []), 1):
            body.append(f"☐ ", style="bold green")
            body.append(f"{i}. {item}\n")
        return body

    if kind == "diff":
        return Syntax(cleaned, "diff", theme="monokai", word_wrap=True)

    return _markdown(cleaned)


def _build_table(block: list[str]) -> Table:
    rows = [[c.strip() for c in line.strip("|").split("|")] for line in block]
    header, data = rows[0], rows[2:]  # 第 2 行是 |---| 分隔行
    table = Table(box=None, pad_edge=False, expand=False, show_edge=False)
    for col, h in enumerate(header):
        col_data = [r[col] for r in data if col < len(r)]
        table.add_column(_clean_cell(h), style="bold cyan",
                         justify="left" if any(len(c) > 12 for c in col_data) else "center")
    for r in data:
        table.add_row(*(_clean_cell(c) for c in r))
    return table


def _clean_cell(cell: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", cell).strip()
