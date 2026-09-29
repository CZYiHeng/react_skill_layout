"""工程记忆：把**证据链**沉淀成跨任务持久的实体。

设计依据：`docs/changes/2026-09-29-coding-capability-design.md` §7 第 4 项。

## 它解决什么

每个任务开始都会 `context.reset()`（[loop.py](../react/loop.py) 的既有语义），压缩还会丢掉
中段。没有落盘记忆，agent 每次都要**重新摸一遍工程**：哪些需求做过、做到什么程度、
上次验证结果是什么、关键决策为什么这么定。

## 一条关键取舍：**由证据生成，不由 agent 手写**

`work.md` 的内容来自 `requirement-set` + 覆盖表 + 回执，**不提供"模型写一段文字进来"的入口**。
理由与覆盖表同源：手写的东西可以编造——真实运行里出现过"规范注入了却被忽略、
覆盖声明一处都没有"。如果记忆是模型自称的，它就会变成另一种自证。

于是三份文件各司其职：

| 文件 | 给谁看 | 内容 |
|---|---|---|
| `coverage.md` | 人（交付物） | 本次运行的覆盖表与缺口清单 |
| `evidence.json` | 机器（可核对） | 本次运行的结构化回执 |
| `work.md` | **模型**（注入上下文） | **跨任务累计的证据链** |

## 放哪

`<work_dir>/.react-agent/work.md`——必须落在工作目录内：`work_dir` 是唯一的工具边界，
放到外面 agent 根本读不到（也读不到就没意义）。`.react-agent/` 与 `spec.json` 同处，
都是**过程产物**，不污染用户的工程文件。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1

#: 工程记忆相对工作目录的位置
MEMORY_REL = Path(".react-agent") / "work.md"
#: 证据链的机器可读侧车（便于下次追加时不必解析 Markdown）
LEDGER_REL = Path(".react-agent") / "evidence-ledger.json"
#: 注入上下文时每条需求的证据摘要长度
LEDGER_NOTE_KEEP = 120
#: 注入上下文时整块的字符上限（防记忆把预算挤掉）
BLOCK_MAX_CHARS = 2400


@dataclass
class Entry:
    """一条需求的**累计**证据。"""

    id: str
    statement: str = ""
    status: str = "not_run"          # pass / fail / not_run / error
    evidence: str = ""               # 最近一次回执摘要（命令 + exit 或判据说明）
    artifacts: list[str] = field(default_factory=list)
    last: str = ""                   # 最近一次更新的时间戳


@dataclass
class Memory:
    """跨任务的工程记忆。"""

    schema_version: int = SCHEMA_VERSION
    goal: str = ""
    capability: str = ""
    work_dir: str = ""
    updated: str = ""
    entries: list[Entry] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)

    # ---- 查询 ----
    def get(self, unit_id: str) -> Entry | None:
        for e in self.entries:
            if e.id == unit_id:
                return e
        return None

    def counts(self) -> dict:
        out = {"pass": 0, "fail": 0, "not_run": 0, "error": 0}
        for e in self.entries:
            out[e.status] = out.get(e.status, 0) + 1
        return out


def memory_path(work_dir: Path) -> Path:
    """工程记忆的规范位置。"""
    return Path(work_dir) / MEMORY_REL


def ledger_path(work_dir: Path) -> Path:
    return Path(work_dir) / LEDGER_REL


def _atomic_write(path: Path, text: str) -> None:
    """原子写：先写临时文件再替换。

    **崩溃不能留下半截文件**——记忆是跨任务事实，读到一个被截断的它能骗过后续所有任务，
    比"没有记忆"危险得多。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".swp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_memory(work_dir: Path) -> Memory:
    """读工程记忆。**文件不存在或损坏时返回空记忆，不抛**。

    为什么不抛：记忆是"锦上添花"的加速器，不是任务的前提。它坏掉不该让任务起不来——
    但**损坏必须可见**（写入 `corrupt` 标记，由调用方报出），否则就成了静默失忆。
    """
    p = ledger_path(work_dir)
    if not p.is_file():
        return Memory(work_dir=str(Path(work_dir).resolve()))
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        m = Memory(work_dir=str(Path(work_dir).resolve()))
        m.gaps = [f"（记忆文件损坏，已忽略：{p}）"]
        return m
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        m = Memory(work_dir=str(Path(work_dir).resolve()))
        m.gaps = [f"（记忆版本不匹配，已忽略：原 {data.get('schema_version')!r}）"]
        return m
    entries = []
    for raw in data.get("entries") or []:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        entries.append(Entry(
            id=str(raw["id"]),
            statement=str(raw.get("statement", "")),
            status=str(raw.get("status", "not_run")),
            evidence=str(raw.get("evidence", "")),
            artifacts=[str(a) for a in (raw.get("artifacts") or [])],
            last=str(raw.get("last", "")),
        ))
    return Memory(
        goal=str(data.get("goal", "")),
        capability=str(data.get("capability", "")),
        work_dir=str(data.get("work_dir", "")),
        updated=str(data.get("updated", "")),
        entries=entries,
        gaps=[str(g) for g in (data.get("gaps") or [])],
        out_of_scope=[str(o) for o in (data.get("out_of_scope") or [])],
    )


def save_memory(mem: Memory, work_dir: Path) -> Path:
    """落盘：Markdown（给人看）+ JSON 侧车（供下次追加）。都用原子写。"""
    wd = Path(work_dir)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "goal": mem.goal,
        "capability": mem.capability,
        "work_dir": mem.work_dir,
        "updated": mem.updated,
        "entries": [e.__dict__ for e in mem.entries],
        "gaps": mem.gaps,
        "out_of_scope": mem.out_of_scope,
    }
    _atomic_write(ledger_path(wd), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(memory_path(wd), render_memory(mem))
    return memory_path(wd)


def _icon(status: str) -> str:
    return {"pass": "✅", "fail": "❌", "not_run": "⚠️", "error": "💥"}.get(status, "?")


def render_memory(mem: Memory) -> str:
    """渲染成注入上下文/供人阅读的 Markdown。**完全由数据推导，无自由文本入口。**"""
    lines: list[str] = []
    lines.append("# 工程记忆（证据链）")
    lines.append("")
    lines.append("> 本文件由 `react/memory.py` 从 requirement-set 与执行回执生成，"
                 "**不是模型手写的自述**。")
    if mem.goal:
        lines.append(f"> 目标：{mem.goal}")
    if mem.updated:
        lines.append(f"> 更新于 {mem.updated}"
                     + (f" · 能力 {mem.capability}" if mem.capability else ""))
    c = mem.counts()
    lines.append("")
    lines.append(f"进度：✅ {c['pass']} · ❌ {c['fail']} · ⚠️ {c['not_run']} · 💥 {c['error']}")
    lines.append("")
    lines.append("| 需求 | 状态 | 证据 |")
    lines.append("|---|---|---|")
    for e in mem.entries:
        note = (e.evidence or "").replace("\n", " ")
        if len(note) > LEDGER_NOTE_KEEP:
            note = note[:LEDGER_NOTE_KEEP] + "…"
        lines.append(f"| {e.id} | {_icon(e.status)} {e.status} | {note} |")
    if not mem.entries:
        lines.append("| — | 尚未有任何需求被验收 | — |")
    lines.append("")
    lines.append("## 缺口")
    lines.append("")
    if mem.gaps:
        for g in mem.gaps:
            lines.append(f"- {g}")
    else:
        lines.append("- 无")
    if mem.out_of_scope:
        lines.append("")
        lines.append("## 明确不做")
        lines.append("")
        for o in mem.out_of_scope:
            lines.append(f"- {o}")
    return "\n".join(lines) + "\n"


def update_from_evidence(work_dir: Path, data: dict, units: list,
                         evidence: list, *, capability: str = "",
                         now: str = "") -> Memory:
    """用一次运行的 `requirement-set` + 回执**更新**（并累计）工程记忆。

    合并规则：
    - 同 id 的条目**用本次回执覆盖**（后证优先——最近一次证据最有说服力）；
    - 本次没出现的 id **保留旧值**（历史证据不因一次运行没提到就作废）；
    - `gaps` / `out_of_scope` **以本次为准**（它们描述"当前状态"，不是历史）。
    """
    mem = load_memory(work_dir)
    if capability:
        mem.capability = capability
    if data.get("goal"):
        mem.goal = str(data["goal"])
    mem.work_dir = str(Path(work_dir).resolve())
    if now:
        mem.updated = now

    by_id = {e.unit_id: e for e in evidence}
    for u in units:
        e = by_id.get(u.id)
        if e is None:
            continue
        entry = mem.get(u.id) or Entry(id=u.id)
        entry.statement = u.statement
        entry.status = e.status
        entry.evidence = e.summary() if hasattr(e, "summary") else str(e)
        entry.artifacts = list(getattr(u, "artifacts", []) or [])
        entry.last = now
        if mem.get(u.id) is None:
            mem.entries.append(entry)

    from react.acceptance import build_gaps
    mem.gaps = build_gaps(units, evidence)
    mem.out_of_scope = [str(o) for o in (data.get("out_of_scope") or [])]
    return mem


def render_for_prompt(mem: Memory) -> str:
    """渲染成**注入上下文**的块。空记忆返回空串（不注入空标题，省预算）。

    与 `render_memory` 分开的原因：注入版要**更省**——去掉给人看的说明文字，
    只留模型真需要的（进度、每条状态与证据、缺口）。记忆块会被每一步反复阅读。
    """
    if not mem.entries and not mem.gaps:
        return ""
    lines: list[str] = []
    lines.append("# 工程记忆（跨任务累计的证据链）")
    lines.append("")
    lines.append("> 由 `react/memory.py` 依 requirement-set 与执行回执生成，**不是自述**。"
                 "未在下表出现 pass 的需求，一律视为**未验收**。")
    c = mem.counts()
    lines.append(f"进度：✅ {c['pass']} · ❌ {c['fail']} · ⚠️ {c['not_run']} · 💥 {c['error']}")
    lines.append("")
    for e in mem.entries:
        note = (e.evidence or "").replace("\n", " ")
        if len(note) > LEDGER_NOTE_KEEP:
            note = note[:LEDGER_NOTE_KEEP] + "…"
        lines.append(f"- {_icon(e.status)} **{e.id}** {e.statement[:60]} — {note}")
    if mem.gaps:
        lines.append("")
        lines.append("缺口：")
        for g in mem.gaps[:12]:
            lines.append(f"- {g}")
    text = "\n".join(lines)
    if len(text) > BLOCK_MAX_CHARS:
        text = text[:BLOCK_MAX_CHARS] + "\n…（记忆过长已截断，完整内容见 .react-agent/work.md）"
    return text
