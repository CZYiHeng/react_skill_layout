"""需求验收：把"完成"从模型的判断变成系统的事实。

设计依据见 `docs/changes/2026-09-29-coding-capability-design.md`：

    G1  每条需求都有**可执行**的验收判据（由上游「需求」能力提供）
    G2  每条判据都**真的被执行过**，回执留存
    G3  未覆盖 / 未执行 / 失败 **必须可见**

本模块只做一件事：**读一份 requirement-set，逐条执行判据，产出覆盖表与缺口清单**。
它不认识模型、不调用模型——**判据必须是可执行的**，否则条目一律记为 `not_run` 并把
原因写进缺口清单，绝不脑补判据（判据是需求的表达，猜判据等于替需求做决定）。

为什么用 JSON 而不是 YAML：本项目 `pyyaml` 不可用，而 `json` 是标准库，
`json.loads` 本身就是一层 schema 校验。

覆盖表**由证据生成**，不接受任何模型文字输入——手写的覆盖声明可以编造
（真实运行里出现过：规范注入后被忽略、覆盖声明一处都没有），由执行结果生成的表没法编。
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1

#: 单条判据的命令超时（秒）。验收命令通常是测试，给足但不过分。
DEFAULT_TIMEOUT_SEC = 300

#: `requirement-set` 的规范位置（相对工作目录）。
#: 放在 `.react-agent/` 而不是工程根：它是**过程产物**而非工程的一部分，
#: 不该混进用户的工程文件里（与 `coverage.md` 等落在一起）。
CANONICAL_SPEC_REL = Path(".react-agent") / "spec.json"
#: 回执里保留的输出长度（避免把整份测试输出灌进交付物）
OUTPUT_KEEP = 2000
#: 覆盖表里回执摘要的长度
EVIDENCE_SUMMARY = 160

#: 状态：通过 / 失败（执行了但不满足 expect）/ 未执行（无判据或缺信息）/ 错误（命令跑不起来）
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_NOT_RUN = "not_run"
STATUS_ERROR = "error"

_ALL_STATUSES = (STATUS_PASS, STATUS_FAIL, STATUS_NOT_RUN, STATUS_ERROR)


class SpecError(ValueError):
    """requirement-set 不合法。调用方应把它当**配置错误**报给用户，而不是当成失败掩掉。"""


@dataclass
class Acceptance:
    """一条验收判据。`kind=command` 必须给 run；`kind=predicate` 必须给 predicate。

    `irreversible=True` 表示**执行这条判据会真实删除/覆盖数据且不可恢复**。
    为什么需要它：验收执行器是**真的跑**命令，不是模拟——`python -m csvdup --delete <dir>`
    会真的删掉用户的文件。声明了不可逆的条目，未经显式确认一律不执行。
    """

    kind: str
    run: str = ""
    expect: str = ""
    predicate: dict = field(default_factory=dict)
    irreversible: bool = False


@dataclass
class Unit:
    """一个需求单元。"""

    id: str
    statement: str
    acceptance: Acceptance | None = None
    artifacts: list[str] = field(default_factory=list)
    #: 判据缺失/无法验收时的原因（会进缺口清单）
    unverifiable: str = ""


@dataclass
class Clarification:
    """一条**阻塞型歧义**：需求有多种合理解读，必须由人定。

    为什么要有它：真实运行里 `coding` 能力自己识别出了歧义（"内容完全重复的行"是
    文件内还是跨文件？）并倾向 ASK——**识别这一半是对的**。但 ASK 的答案只进了对话，
    任务结束 `context.reset()` 之后就没位置了，下次还得再问一遍，而 spec 里
    **永远没有这条约束**。所以歧义必须能在 requirement-set 里落盘。

    与 ASK 的分工：**ASK 用来拿到答案，本字段用来固化答案**。
    """

    id: str
    question: str
    why: str = ""
    options: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    answer: str | None = None
    decided_at: str | None = None

    @property
    def resolved(self) -> bool:
        return bool(self.answer)


@dataclass
class Evidence:
    """一次执行的回执。**只由真实执行产生**。"""

    unit_id: str
    status: str
    command: str = ""
    exit_code: int | None = None
    output: str = ""
    detail: str = ""
    duration_ms: int = 0

    def summary(self, limit: int = EVIDENCE_SUMMARY) -> str:
        """给覆盖表用的一行摘要。"""
        if self.status == STATUS_NOT_RUN:
            return f"未执行（{self.detail}）"
        if self.status == STATUS_ERROR:
            return f"执行异常（{self.detail}）"
        head = (self.command or "").replace("\n", " ")
        if len(head) > limit // 2:
            head = head[: limit // 2] + "…"
        return f"`{head}` → exit {self.exit_code}"


def load_spec(path: Path, *, require_confirmed: bool = False,
              allow_unresolved: bool = False,
              allow_irreversible: bool = False,
              strict_acceptance: bool = False) -> tuple[dict, list[Unit]]:
    """读并校验 requirement-set。返回 (原始 dict, 单元列表)。

    校验刻意严格：**判据不完整就必须报出来**（G1），而不是让它在实现阶段被悄悄跳过。

    `require_confirmed=True` 时，`confirmed` 为假的 spec **一律拒绝**。这是设计文档
    A5 的落地："未确认时不得进入实现"——判据化只是**提议**，人认可了才算判据。
    默认不检查，是为了让 `--intake` 等工具能读自己的草稿。

    `allow_unresolved=False`（默认）时，**有未答的 `clarify` 条目一律拒绝**。
    歧义不解决就往实现走，等于让实现者替你选一个解读——而那正是"猜需求"。
    确实要先跑一次时可以 `allow_unresolved=True`，此时结论里必须标出"带着未决歧义运行"。

    `allow_irreversible=False`（默认）时，**声明 `irreversible` 的判据一律不执行**。
    验收执行器是真跑命令：`--delete` 会真的删掉用户的文件。确认方式是在 spec 里设
    `irreversible_ok: true`，或显式传 `allow_irreversible=True`。

    `strict_acceptance=True` 时，**判据写错了直接报错**（而不是降级为"无法验收"）。
    用于校验**模型产出的草稿**：模型把 `kind` 写错若被静默降级成"没判据"，
    看起来还挺合理，但那是把类型错误伪装成了缺口。人写的 spec 用默认值更宽容。
    """
    p = Path(path)
    if not p.is_file():
        raise SpecError(f"requirement-set 不存在：{p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SpecError(f"requirement-set 不是合法 JSON：{e}") from e
    if not isinstance(data, dict):
        raise SpecError("requirement-set 顶层必须是对象")

    ver = data.get("schema_version")
    if ver != SCHEMA_VERSION:
        raise SpecError(
            f"schema_version 不匹配：文件是 {ver!r}，本程序支持 {SCHEMA_VERSION}")

    if require_confirmed and not is_confirmed(data):
        raise SpecError(
            "该 requirement-set **尚未确认**，不得进入实施。"
            "请先审阅条目与判据，确认后把 `confirmed` 设为 true"
            "（CLI：`--confirm-spec <路径>`）")

    raw_units = data.get("unit")
    if not isinstance(raw_units, list) or not raw_units:
        raise SpecError("缺少 unit 列表（至少一条需求）")

    units: list[Unit] = []
    seen: set[str] = set()
    for i, ru in enumerate(raw_units, 1):
        if not isinstance(ru, dict):
            raise SpecError(f"unit[{i}] 不是对象")
        uid = str(ru.get("id", "")).strip()
        if not uid:
            raise SpecError(f"unit[{i}] 缺少 id")
        if uid in seen:
            raise SpecError(f"需求 id 重复：{uid}")
        seen.add(uid)
        stmt = str(ru.get("statement", "")).strip()
        if not stmt:
            raise SpecError(f"{uid} 缺少 statement")

        arts = ru.get("artifacts") or []
        if not isinstance(arts, list):
            raise SpecError(f"{uid} 的 artifacts 应为列表")
        art_list = [str(a) for a in arts]

        acc_raw = ru.get("acceptance")
        if acc_raw is None:
            # 只有 statement = 不算可验收（G1）。**不在这里报错**——它是"缺口"，
            # 要在交付物里可见，而不是让整次运行起不来。
            units.append(Unit(id=uid, statement=stmt, artifacts=art_list,
                              unverifiable="上游未提供 acceptance"))
            continue
        if not isinstance(acc_raw, dict):
            if strict_acceptance:
                raise SpecError(f"{uid} 的 acceptance 应为对象")
            units.append(Unit(id=uid, statement=stmt, artifacts=art_list,
                              unverifiable="acceptance 不是对象"))
            continue

        kind = str(acc_raw.get("kind", "")).strip()
        if kind == "command":
            run = str(acc_raw.get("run", "")).strip()
            expect = str(acc_raw.get("expect", "")).strip()
            irrev = bool(acc_raw.get("irreversible", False))
            missing = []
            if not run:
                missing.append("run")
            if not expect:
                missing.append("expect")
            if missing:
                units.append(Unit(
                    id=uid, statement=stmt, artifacts=art_list,
                    unverifiable=f"acceptance.kind=command 缺少 {'/'.join(missing)}"))
            else:
                units.append(Unit(id=uid, statement=stmt, artifacts=art_list,
                                  acceptance=Acceptance(kind="command", run=run,
                                                        expect=expect,
                                                        irreversible=irrev)))
        elif kind == "predicate":
            pred = acc_raw.get("predicate")
            if not isinstance(pred, dict) or not pred:
                units.append(Unit(
                    id=uid, statement=stmt, artifacts=art_list,
                    unverifiable="acceptance.kind=predicate 缺少 predicate 对象"))
            else:
                units.append(Unit(id=uid, statement=stmt, artifacts=art_list,
                                  acceptance=Acceptance(
                                      kind="predicate", predicate=pred,
                                      irreversible=bool(acc_raw.get("irreversible",
                                                                    False)))))
        else:
            # 未知 kind：对于**人手写**的 spec，降级为"无法验收"更安全（不让整次运行
            # 起不来）；但对于**模型产出的草稿**必须直接拒绝——否则模型写错 kind 会被
            # 静默降级成"没判据"，看起来还挺合理。这正是 `strict_acceptance` 的用途。
            msg = f"{uid} 的 acceptance.kind 非法：{kind!r}（支持 command / predicate）"
            if strict_acceptance:
                raise SpecError(msg)
            units.append(Unit(id=uid, statement=stmt, artifacts=art_list,
                              unverifiable=msg))

    # ---- 歧义（clarify）：schema 校验 + 阻塞规则 ----
    raw_clar = data.get("clarify") or []
    if not isinstance(raw_clar, list):
        raise SpecError("clarify 应为列表")
    unit_ids = {u.id for u in units}
    for i, rc in enumerate(raw_clar, 1):
        if not isinstance(rc, dict):
            raise SpecError(f"clarify[{i}] 不是对象")
        cid = str(rc.get("id", "")).strip()
        if not cid:
            raise SpecError(f"clarify[{i}] 缺少 id")
        q = str(rc.get("question", "")).strip()
        if not q:
            raise SpecError(f"{cid} 缺少 question")
        opts = rc.get("options") or []
        if not isinstance(opts, list):
            raise SpecError(f"{cid} 的 options 应为列表")
        blocks = rc.get("blocks") or []
        if not isinstance(blocks, list):
            raise SpecError(f"{cid} 的 blocks 应为列表")
        for b in blocks:
            if str(b) not in unit_ids:
                raise SpecError(
                    f"{cid} 的 blocks 指向不存在的需求 {b!r}（歧义必须指向真实需求）")
        ans = rc.get("answer")
        if ans is not None and not isinstance(ans, str):
            raise SpecError(f"{cid} 的 answer 应为字符串或 null")

    if require_confirmed and not allow_unresolved:
        pending = [c for c in raw_clar
                   if isinstance(c, dict) and not c.get("answer")]
        if pending:
            detail = "；".join(
                f"{c.get('id')}: {str(c.get('question', ''))[:60]}" for c in pending[:3])
            raise SpecError(
                f"有 {len(pending)} 条**未解决的歧义**，不得进入实施：{detail}。"
                "歧义不解决就往实现走，等于让实现者替你选一个解读。"
                "请用 `--resolve <ID> <选择>`（两个参数，空格分隔）落盘决定"
                "（确实要先跑一次可加 `--allow-unresolved`，结论会标出这一点）")

    # ---- 不可逆判据：未经显式确认一律不执行 ----
    # spec 里 `irreversible_ok: true` 是**项目级确认**（比每跑一次都加参数更适合
    # "这个项目的验收本来就含删改"的场景）；显式参数则适合一次性运行。
    proj_ok = bool(data.get("irreversible_ok", False))
    if require_confirmed and not (allow_irreversible or proj_ok):
        irrev = [u for u in units
                 if u.acceptance is not None and u.acceptance.irreversible]
        if irrev:
            detail = "；".join(f"{u.id}: {u.acceptance.run[:50]}" for u in irrev[:3])
            raise SpecError(
                f"有 {len(irrev)} 条判据声明为**不可逆**（会真实删除/覆盖数据），"
                f"未经确认不得执行：{detail}。"
                "验收执行器是**真的跑**命令、不是模拟——删掉的用户数据无法恢复。"
                "确认请在 spec 里设 `irreversible_ok: true`，或加 `--allow-irreversible`")

    return data, units


def irreversible_units(units: list[Unit]) -> list[Unit]:
    """会真实删改数据的条目。"""
    return [u for u in units if u.acceptance is not None and u.acceptance.irreversible]


def clarifications(data: dict) -> list[Clarification]:
    """从 spec 里取出歧义条目（不做校验，校验在 `load_spec` 里）。"""
    out: list[Clarification] = []
    for rc in data.get("clarify") or []:
        if not isinstance(rc, dict):
            continue
        out.append(Clarification(
            id=str(rc.get("id", "")),
            question=str(rc.get("question", "")),
            why=str(rc.get("why", "")),
            options=[str(o) for o in (rc.get("options") or [])],
            blocks=[str(b) for b in (rc.get("blocks") or [])],
            answer=(None if rc.get("answer") is None else str(rc["answer"])),
            decided_at=(None if rc.get("decided_at") is None else str(rc["decided_at"])),
        ))
    return out


def unresolved_clarifications(data: dict) -> list[Clarification]:
    return [c for c in clarifications(data) if not c.resolved]


def resolve_clarification(path: Path, cid: str, answer: str,
                          *, now: str = "") -> dict:
    """把一条歧义的决定**落盘**。返回改后的数据。

    这是本机制存在的理由：ASK 的答案只活在对话里，任务一结束就没了；
    写进 spec 才算**契约**，后续所有轮次与后续任务都读得到。
    """
    p = Path(path)
    if not p.is_file():
        raise SpecError(f"requirement-set 不存在：{p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SpecError(f"requirement-set 不是合法 JSON：{e}") from e
    if not isinstance(data, dict):
        raise SpecError("requirement-set 顶层必须是对象")
    # 先做结构校验：往一个坏 spec 里写决定是最糟的结果
    load_spec(p)

    target = None
    for rc in data.get("clarify") or []:
        if isinstance(rc, dict) and str(rc.get("id", "")) == cid:
            target = rc
            break
    if target is None:
        known = [str(c.get("id")) for c in (data.get("clarify") or [])
                 if isinstance(c, dict)]
        raise SpecError(f"找不到歧义 {cid!r}；现有：{known or '（无）'}")

    ans = answer.strip()
    if not ans:
        raise SpecError("决定不能为空")
    opts = [str(o) for o in (target.get("options") or [])]
    if opts and ans not in opts:
        raise SpecError(
            f"决定 {ans!r} 不在选项里；可选：{opts}（如确需自定义，请显式加进 options）")

    target["answer"] = ans
    target["decided_at"] = now or ""
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


# ---------------------------------------------------------------------------
# 输入契约：规范位置、确认门禁、判据化（把任务描述变成可验收的提议）
# ---------------------------------------------------------------------------

def canonical_spec_path(work_dir: Path) -> Path:
    """`requirement-set` 的规范位置：`<work_dir>/.react-agent/spec.json`。"""
    return Path(work_dir) / CANONICAL_SPEC_REL


def is_confirmed(data: dict) -> bool:
    """该 requirement-set 是否已被人确认。"""
    return bool(data.get("confirmed"))


def confirm_spec(path: Path) -> dict:
    """把 spec 标为已确认（A5 的显式动作）。返回改后的数据。

    刻意做成**独立动作**：确认是人审阅后的决定，不该在 `--verify` 里顺带完成——
    否则"未确认不得进入实现"这条就形同虚设。

    **未决歧义一律不许确认**（真实事故：契约声明 4 条歧义，用户只答了 2 条，
    点"确认契约并开始"就把 spec 签成了 `confirmed: true`，剩下两条由模型自己猜）。
    校验放在这里、而不是放在每个界面里——依据 DSH `user-questions` 的约定：
    "Caught at the asker, where the mistake is, rather than in each UI."
    """
    p = Path(path)
    if not p.is_file():
        raise SpecError(f"requirement-set 不存在：{p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SpecError(f"requirement-set 不是合法 JSON：{e}") from e
    if not isinstance(data, dict):
        raise SpecError("requirement-set 顶层必须是对象")
    # 先按未确认模式做一次结构校验：把明显坏掉的 spec 标成"已确认"是最糟的结果
    load_spec(p)
    # ★ 未决歧义 → 拒绝确认。只校验结构是不够的：结构合法但语义未定，
    #   实现只能靠猜，而这恰恰是"需求没确认就开始写码"的另一种形态。
    pending = unresolved_clarifications(data)
    if pending:
        ids = "、".join(c.id for c in pending)
        raise SpecError(
            f"仍有 {len(pending)} 条歧义未决定（{ids}），不能确认契约："
            + "；".join(f"{c.id} {c.question}" for c in pending[:3])
            + (f"（等 {len(pending)} 条）" if len(pending) > 3 else "")
            + "。请先逐条选定，或把它们移入 out_of_scope。")
    data["confirmed"] = True
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


#: 任务描述里可识别的"行首编号/项目符号"。据此切分需求条目。
_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)、]|\(\d+\))\s+(.+)$")
#: 描述里显式给出的可执行验收线索（`验收：xxx` / `acceptance: xxx`）
_ACCEPT_HINT_RE = re.compile(r"^\s*(?:验收|签收|acceptance)\s*[:：]\s*(.+)$", re.I)
#: 形如 `\`<命令>\`` 的反引号片段，是最可能的验收命令候选
_BACKTICK_RE = re.compile(r"`([^`]+)`")


def extract_requirements(description: str, *, default_id_prefix: str = "R") -> list[dict]:
    """把一段任务描述**机械地**拆成候选需求条目（judgement 化的第一步）。

    刻意只做**能用规则做的事**，不做语义推断：

    - 行首带编号/项目符号 → 一条需求
    - 行内出现 `验收：<命令>` → 该条带可执行判据
    - 行内反引号片段是命令的样子（以 `python`/`pytest`/`npm` 等开头，或含 `-m`）→ 判据候选
    - **其余一律没有判据**，标记为"无法验收"

    最后一条是这套设计的关键：判据化**产的是提议，不是事实**。凡是不能机械识别的，
    就诚实地留空、交给上游补或交人确认——**绝不脑补判据**（判据是需求的表达）。
    """
    lines = description.splitlines()
    items: list[str] = []
    for line in lines:
        m = _ITEM_RE.match(line)
        if m:
            items.append(m.group(1).strip())
    if not items:
        # 描述没分条：整段作为一条需求（仍然不脑补判据）
        whole = " ".join(s.strip() for s in lines if s.strip())
        if whole:
            items = [whole]

    out: list[dict] = []
    for i, item in enumerate(items, 1):
        uid = f"{default_id_prefix}{i}"
        entry: dict = {"id": uid, "statement": item}

        cmd = None
        hint = _ACCEPT_HINT_RE.match(item)
        if hint:
            # 显式给的验收文字：取其中第一个反引号片段当命令，否则整段当输出断言
            frag = _BACKTICK_RE.search(hint.group(1))
            if frag:
                cmd = frag.group(1).strip()
            else:
                text = hint.group(1).strip()
                cmd = None
                if text:
                    entry["acceptance"] = {"kind": "command", "run": text,
                                          "expect": "exit_code == 0"}
        if cmd:
            entry["acceptance"] = {"kind": "command", "run": cmd,
                                   "expect": "exit_code == 0"}
        elif "acceptance" not in entry:
            # 没识别到判据：不猜。空着就是"无法验收"，会在缺口清单里显式出现。
            pass
        out.append(entry)
    return out


def draft_spec(task: str, *, goal: str = "", out_of_scope: list[str] | None = None) -> dict:
    """由任务描述产出一份**未确认**的 requirement-set 草稿。

    `confirmed` 恒为 `false`：草稿必须经过人确认才能进入实施（A5）。
    """
    units = extract_requirements(task)
    if not units:
        raise SpecError("任务描述为空，无法产出 requirement-set 草稿")
    return {
        "schema_version": SCHEMA_VERSION,
        "confirmed": False,
        "goal": goal or (task.strip().splitlines()[0][:120] if task.strip() else ""),
        "unit": units,
        "out_of_scope": list(out_of_scope or []),
        "generated_from": "task-description",
    }


def write_draft(draft: dict, work_dir: Path) -> Path:
    """草稿落到规范位置。**不覆盖已确认的 spec**（那会悄悄丢掉人的确认）。"""
    p = canonical_spec_path(work_dir)
    if p.is_file():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        if is_confirmed(existing):
            raise SpecError(
                f"{p} 已存在且**已确认**，拒绝用草稿覆盖。"
                "先移动或删除它，或显式传另一个输出路径")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def write_spec_from_agent(raw: str | dict, work_dir: Path) -> Path:
    """把**模型产出**的 requirement-set 草稿校验后写入规范位置。

    这是"让能力自己产 spec"的落点（设计上的 B 方案）：模型在 THINK/PLAN 阶段识别到
    没有 spec 时，先把它拟出来，由框架校验并落盘，**再由人确认**——
    `confirmed` 一律强制为 false：草稿就是草稿，模型不能自己把自己确认掉。

    只做"校验 + 落盘"，不做语义修补：字段缺失/类型不对一律报错让模型改，
    因为**框架替它补一个判据就等于替需求做决定**。
    """
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SpecError(f"草稿不是合法 JSON：{e}") from e
    else:
        data = dict(raw)
    if not isinstance(data, dict):
        raise SpecError("草稿顶层必须是对象")

    preview = dict(data)
    preview.setdefault("schema_version", SCHEMA_VERSION)
    # 强制未确认：模型不许自证需求已获认可
    preview["confirmed"] = False
    preview.pop("irreversible_ok", None)   # 项目级危险确认也不能由模型自己开

    # 先校验：用临时文件走一遍 `load_spec`，避免把坏草稿写进规范位置
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "spec.json"
        probe.write_text(json.dumps(preview, ensure_ascii=False), encoding="utf-8")
        load_spec(probe, allow_unresolved=True, allow_irreversible=True,
                  strict_acceptance=True)

    p = canonical_spec_path(work_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(preview, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 判据执行
# ---------------------------------------------------------------------------

_EXIT_RE = re.compile(r"exit_code\s*==\s*(-?\d+)")
_CONTAINS_RE = re.compile(r"contains\s+(.+)$", re.S)

#: shell 报"命令本身不存在"的特征串。命中时状态应为 error（判据跑不起来），
#: 而不是 fail（判据判为不通过）——两者混在一起会让人误以为"实现有问题"，
#: 实际是"判据写错了/环境缺东西"。
_CMD_NOT_FOUND = (
    "is not recognized as an internal or external command",   # cmd.exe
    "command not found",                                      # bash/zsh
    "not found",                                              # cmd 的旧措辞
)


def check_expect(expect: str, exit_code: int | None, output: str) -> tuple[bool, str]:
    """按 `expect` 表达式判定。返回 (是否满足, 说明)。

    支持两种（刻意少而明确，避免自造表达式语言）：
      · `exit_code == N`
      · `contains <文本>`（在输出里出现）
    """
    m = _EXIT_RE.fullmatch(expect.strip())
    if m:
        want = int(m.group(1))
        return (exit_code == want), f"期望 exit_code == {want}，实际 {exit_code}"
    m = _CONTAINS_RE.match(expect.strip())
    if m:
        needle = m.group(1).strip()
        ok = needle in (output or "")
        return ok, (f"输出{'包含' if ok else '不含'} {needle!r}")
    return False, (f"无法解析 expect：{expect!r}（支持 `exit_code == N` 或 `contains <文本>`）")


def run_predicate(pred: dict, work_dir: Path) -> tuple[bool, str, int | None]:
    """执行 predicate 类判据。返回 (是否满足, 说明, exit_code)。

    内置谓词刻意只有几个：**能机械判定的才算判据**，加多了就变成自造语言。
    """
    kind = str(pred.get("kind", "")).strip()
    if kind == "file_exists":
        rel = str(pred.get("path", "")).strip()
        if not rel:
            return False, "predicate.file_exists 缺少 path", None
        ok = (work_dir / rel).exists()
        return ok, f"{rel} {'存在' if ok else '不存在'}", 0 if ok else 1
    if kind == "file_contains":
        rel = str(pred.get("path", "")).strip()
        needle = str(pred.get("text", "")).strip()
        if not rel or not needle:
            return False, "predicate.file_contains 需要 path 与 text", None
        f = work_dir / rel
        if not f.is_file():
            return False, f"{rel} 不存在", 1
        try:
            ok = needle in f.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return False, f"读取 {rel} 失败：{e}", 1
        return ok, f"{rel} {'包含' if ok else '不含'} {needle!r}", 0 if ok else 1
    return False, f"未知 predicate.kind：{kind!r}（支持 file_exists / file_contains）", None


def run_one(unit: Unit, work_dir: Path, timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> Evidence:
    """执行一个单元的判据，产出回执。

    **无判据 → `not_run`，不是 `pass`**：这条最要紧，否则"没验"会伪装成"验过了"。
    """
    if unit.acceptance is None:
        return Evidence(unit.id, STATUS_NOT_RUN, detail=unit.unverifiable or "无判据")

    acc = unit.acceptance
    import time
    t0 = time.monotonic()

    if acc.kind == "predicate":
        try:
            ok, detail, code = run_predicate(acc.predicate, work_dir)
        except Exception as e:  # noqa: BLE001 - 谓词异常必须变成可见的 error，不能吞
            return Evidence(unit.id, STATUS_ERROR, detail=f"{type(e).__name__}: {e}")
        ms = int((time.monotonic() - t0) * 1000)
        return Evidence(unit.id, STATUS_PASS if ok else STATUS_FAIL,
                        command=f"predicate:{acc.predicate.get('kind')}",
                        exit_code=code, detail=detail, duration_ms=ms)

    # kind == command
    try:
        proc = subprocess.run(acc.run, shell=True, cwd=str(work_dir),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        ms = int((time.monotonic() - t0) * 1000)
        return Evidence(unit.id, STATUS_ERROR, command=acc.run,
                        detail=f"超时（>{timeout_sec}s）", duration_ms=ms)
    except OSError as e:
        return Evidence(unit.id, STATUS_ERROR, command=acc.run, detail=f"无法执行：{e}")

    ms = int((time.monotonic() - t0) * 1000)
    output = ((proc.stdout or "") + (proc.stderr or ""))[:OUTPUT_KEEP]
    # 命令本身不存在 → error（判据跑不起来），不是 fail（判据判为不通过）
    low = output.lower()
    if proc.returncode != 0 and any(sig in low for sig in _CMD_NOT_FOUND):
        return Evidence(unit.id, STATUS_ERROR, command=acc.run,
                        exit_code=proc.returncode, output=output,
                        detail="命令无法执行（shell 报找不到该命令）", duration_ms=ms)
    ok, detail = check_expect(acc.expect, proc.returncode, output)
    return Evidence(unit.id, STATUS_PASS if ok else STATUS_FAIL, command=acc.run,
                    exit_code=proc.returncode, output=output, detail=detail, duration_ms=ms)


def run_all(units: list[Unit], work_dir: Path,
            timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> list[Evidence]:
    """逐条执行。顺序执行、不并行（判据之间可能有依赖，并行会造假象）。"""
    return [run_one(u, work_dir, timeout_sec) for u in units]


# ---------------------------------------------------------------------------
# 覆盖表与缺口清单（由证据生成，不接受模型文字）
# ---------------------------------------------------------------------------

def build_gaps(units: list[Unit], evidence: list[Evidence]) -> list[str]:
    """缺口清单：**空也要显式说明"无"**（G3）。"""
    by_id = {e.unit_id: e for e in evidence}
    gaps: list[str] = []
    for u in units:
        e = by_id.get(u.id)
        if e is None:
            gaps.append(f"{u.id}：未执行（无回执）")
            continue
        if e.status == STATUS_NOT_RUN:
            gaps.append(f"{u.id}：无法验收 —— {u.unverifiable or e.detail}")
        elif e.status == STATUS_FAIL:
            gaps.append(f"{u.id}：验收失败 —— {e.detail}")
        elif e.status == STATUS_ERROR:
            gaps.append(f"{u.id}：执行异常 —— {e.detail}")
    return gaps


def all_passed(units: list[Unit], evidence: list[Evidence]) -> bool:
    """完成判据：**∀ R: 有回执 ∧ 回执为 pass**。

    **逐条按 id 查，不靠"证据数量等于单元数量"**：数量相等是个脆弱前提——只要
    "漏执行一条"同时"少一条回执"，数量照样相等，漏掉的那条就会被算成通过。
    所以这里逐个确认，**缺失回执一律视为未通过**。

    另外 `not_run` 与 `error` 都不算通过——"没验"绝不能当成"验过了"。
    """
    by_id = {e.unit_id: e for e in evidence}
    for u in units:
        e = by_id.get(u.id)
        if e is None or e.status != STATUS_PASS:
            return False
    return bool(units)


def render_report(data: dict, units: list[Unit], evidence: list[Evidence]) -> str:
    """生成交付物用的覆盖表 + 缺口清单。**完全由证据推导**，无任何模型文字输入。"""
    by_id = {e.unit_id: e for e in evidence}
    icon = {STATUS_PASS: "✅", STATUS_FAIL: "❌",
            STATUS_NOT_RUN: "⚠️", STATUS_ERROR: "💥"}
    lines: list[str] = []
    goal = str(data.get("goal", "") or "").strip()
    lines.append("# 验收覆盖表")
    lines.append("")
    lines.append(f"> 由 `react/acceptance.py` 依**真实执行回执**生成 "
                 f"（schema_version={SCHEMA_VERSION}）")
    if goal:
        lines.append(f"> 目标：{goal}")
    passed = sum(1 for e in evidence if e.status == STATUS_PASS)
    lines.append(f"> 结果：**{passed}/{len(units)} 条通过**"
                 f"；结论：{'完成' if all_passed(units, evidence) else '未完成'}")
    lines.append("")
    lines.append("| 需求 | 状态 | 证据 |")
    lines.append("|---|---|---|")
    for u in units:
        e = by_id.get(u.id)
        st = e.status if e else STATUS_NOT_RUN
        ev = e.summary() if e else "无回执"
        lines.append(f"| {u.id} | {icon.get(st, '?')} {st} | {ev} |")
    lines.append("")

    oos = data.get("out_of_scope") or []
    lines.append("## 缺口清单")
    lines.append("")
    gaps = build_gaps(units, evidence)
    # 未决歧义排在缺口清单最前：它比"某条没验"更根本——**它意味着判据本身可能选错了**
    pending = unresolved_clarifications(data)
    if pending:
        for c in pending:
            blocked = f"（卡住 {','.join(c.blocks)}）" if c.blocks else ""
            lines.append(f"- ❓ **未解决的歧义 {c.id}**{blocked}：{c.question}")
        lines.append("")
    if gaps:
        for g in gaps:
            lines.append(f"- {g}")
    elif not pending:
        lines.append("- 无")
    resolved = [c for c in clarifications(data) if c.resolved]
    if resolved:
        lines.append("")
        lines.append("已解决的歧义（决定已落盘为契约）：")
        for c in resolved:
            lines.append(f"- {c.id}：{c.question} → **{c.answer}**")
    irrev = irreversible_units(units)
    if irrev:
        lines.append("")
        lines.append("⚠️ 本次执行含**不可逆**判据（真实删改数据）：")
        for u in irrev:
            lines.append(f"- {u.id}：`{u.acceptance.run}`")
    if oos:
        lines.append("")
        lines.append("明确不做（`out_of_scope`）：")
        for o in oos:
            lines.append(f"- {o}")
    lines.append("")
    lines.append("## 逐条陈述")
    lines.append("")
    for u in units:
        lines.append(f"- **{u.id}** {u.statement}")
    lines.append("")
    lines.append("## 失败与异常的原始输出")
    lines.append("")
    any_out = False
    for u in units:
        e = by_id.get(u.id)
        if e and e.status in (STATUS_FAIL, STATUS_ERROR) and (e.output or e.detail):
            any_out = True
            lines.append(f"### {u.id}（{e.status}）")
            lines.append("")
            lines.append(f"```\n{(e.output or e.detail)[:OUTPUT_KEEP]}\n```")
            lines.append("")
    if not any_out:
        lines.append("无。")
    return "\n".join(lines)


def run_spec(spec_path: Path, work_dir: Path,
             timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> tuple[dict, list[Unit], list[Evidence]]:
    """端到端：读 spec → 执行 → 返回 (data, units, evidence)。"""
    data, units = load_spec(spec_path)
    evidence = run_all(units, work_dir, timeout_sec)
    return data, units, evidence


def finalize(data: dict, units: list[Unit], evidence: list[Evidence],
             out_dir: Path) -> dict:
    """写出交付物：覆盖表 + 回执 + 结论。返回结论摘要。

    产出三个文件（`out_dir` 默认由 CLI 定为 `<work_dir>/.react-agent/`）：
      · `coverage.md`   —— 覆盖表与缺口清单（给人看，也是交付物）
      · `evidence.json` —— 结构化回执（机器可核对：覆盖表每行都能在此追到一次执行）
      · `verdict.json`  —— 结论（`all_passed` 由代码算出，**不接受任何模型文字**）
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    done = all_passed(units, evidence)

    (out / "coverage.md").write_text(render_report(data, units, evidence), encoding="utf-8")
    (out / "evidence.json").write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "evidence": [e.__dict__ for e in evidence],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    gaps = build_gaps(units, evidence)
    pending = unresolved_clarifications(data)
    verdict = {
        "schema_version": SCHEMA_VERSION,
        "goal": str(data.get("goal", "") or ""),
        "units_total": len(units),
        "passed": sum(1 for e in evidence if e.status == STATUS_PASS),
        "counts": {s: sum(1 for e in evidence if e.status == s) for s in _ALL_STATUSES},
        "all_passed": done,
        "gaps": gaps,
        # 未决歧义必须出现在结论里：**带着歧义跑出来的"通过"是可疑的**
        # ——它可能只是恰好满足了实现者自己选的那个解读。
        "unresolved": [{"id": c.id, "question": c.question, "blocks": c.blocks}
                       for c in pending],
        # 本条跑过的不可逆判据也要留痕：交付物要能看出"这次验收真的删过东西"
        "irreversible": [{"id": u.id, "run": u.acceptance.run}
                         for u in irreversible_units(units)],
        "resolved": [{"id": c.id, "answer": c.answer, "decided_at": c.decided_at}
                     for c in clarifications(data) if c.resolved],
    }
    if pending:
        verdict["gaps"] = list(gaps) + [
            f"{c.id}：未解决的歧义 —— {c.question}"
            + (f"（卡住 {','.join(c.blocks)}）" if c.blocks else "")
            for c in pending
        ]
    (out / "verdict.json").write_text(
        json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")

    # 顺带把**证据链**沉淀成跨任务的工程记忆（设计文档 §7 第 4 项）。
    # 放这里而不是另立命令：覆盖表与记忆是同一份证据的两个视图，分开写容易漂移。
    # 记忆**由证据生成**，因此不提供任何"模型文字"入口。
    try:
        import time as _time

        from react.memory import save_memory, update_from_evidence

        mem = update_from_evidence(
            out.parent, data, units, evidence,
            now=_time.strftime("%Y-%m-%d %H:%M:%S"))
        save_memory(mem, out.parent)
        verdict["memory"] = str(memory_path_of(out.parent))
    except Exception as e:  # noqa: BLE001 - 记忆写入失败不影响验收结论本身
        verdict["memory_error"] = f"{type(e).__name__}: {e}"

    return verdict


def memory_path_of(work_dir: Path) -> Path:
    """便捷转发（避免 acceptance 直接依赖 memory 的路径常量）。"""
    from react.memory import memory_path
    return memory_path(work_dir)
