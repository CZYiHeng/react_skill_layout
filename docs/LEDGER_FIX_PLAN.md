# 修复方案：EXEC 多块执行 + 银行视角记账符号

> 触发：2026-09-16 会话（`session_4b3f9a50e1ea.md`）跑「搭建银行记账系统」六步计划，
> 中途中断。复盘发现 1 个框架级缺陷、1 处验收放水、1 个设计缺陷。
> 记账视角已定：**银行视角（客户存款 = LIABILITY）**。
> 本文只给方案与改动点，**不含已实施的代码**。

---

## 0. 结论摘要

| # | 缺陷 | 级别 | 一句话 |
|---|---|---|---|
| D1 | 一个 ACT 里多个 `[EXEC: write]` 只执行第一个 | P0 | `re.search` 只取首个，其余**静默丢弃、无告警** |
| D2 | `ledger/` 包残缺 | P0 | 只有 `errors.py`、`store.py`；`invariants`/`ledger`/`__init__` 全缺 |
| D3 | OBSERVE 验收放水 | P1 | 只看一条回显就判 pass，未核对「声明文件数 vs 回显数」 |
| D4 | 借贷符号自相矛盾 | P1 | A4 示例是银行视角，`invariants` 符号是资产视角，二者只能对一个 |
| D5 | 写入载荷为一次性 JSON 长串 | P2 | 超 `max_tokens` 即半截文件，而回显只报字符数，发现不了 |
| D6 | 会话内存态，中断即失 | P2 | Step 5/6 未执行，6 步计划只保住 2 个文件 |
| D7 | 产物落在 agent 仓库根目录 | P3 | `G:\react-agent\ledger\` 与 `react/` 平级 |

---

## 1. D1：多 EXEC 块只执行第一个

### 定位

- `react/loop.py:79` —— `re.search` 只返回首个匹配
- `react/loop.py:353-362` —— `parse_exec` 只调一次，`executor.run` 只执行一次

### 复现（已实测）

```text
输入含 2 个 [EXEC: write] 块
→ parse_exec 返回: ('write', '{"path": "a.py", ...}')
→ 全文 EXEC 块数量: 2  |  实际执行: 1
```

### 会话中的实际后果

| ACT | 声明写入 | 实际落盘 | 丢失 |
|---|---|---|---|
| Step 3 | `errors.py` + `invariants.py` | `errors.py` | `invariants.py` |
| Step 4 | `store.py` + `ledger.py` + `__init__.py` | `store.py` | `ledger.py`、`__init__.py` |

### 修复：F1 新增 `parse_exec_all`，保留 `parse_exec` 兼容

> 关键点：`tests/smoke_checks.py:97` 现有断言写的是
> `parse_exec(...) != ("shell", "echo hi")`，直接改返回值会挂测试。
> 因此**新增** `parse_exec_all` 返回全部，旧函数退化为「取首个」。

```python
def parse_exec_all(raw: str) -> list[tuple[str, str]]:
    """提取 ACT 中全部 [EXEC: shell|write] 请求（按出现顺序）。"""
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"\[EXEC:\s*(shell|write)\s*\]", raw, re.IGNORECASE):
        kind = m.group(1).lower()
        tail = raw[m.end():]
        fence = _EXEC_FENCE_RE.search(tail)
        if fence:
            payload = fence.group(1).strip()
        else:
            stop = re.search(r"\[RESULT\]", tail, re.IGNORECASE)
            payload = (tail[:stop.start()] if stop else tail).strip()
        if payload:
            out.append((kind, payload))
    return out


def parse_exec(raw: str) -> tuple[str, str] | None:
    """保留：返回首个执行请求（向后兼容既有断言与调用方）。"""
    items = parse_exec_all(raw)
    return items[0] if items else None
```

**已知边界（本次不修，需记录）**：若某个 `[EXEC]` 后面没有围栏代码块，
旧逻辑会一路取到 `[RESULT]` 之前，可能把后续 EXEC 块也吞进载荷。
`parse_exec_all` 沿用了该行为。彻底修需改为「取到下一个 `[EXEC:` 或 `[RESULT]` 为止」。

### 修复：F2 loop 循环执行 + 计数告警

`react/loop.py` 第 351-362 行改为：

```python
exec_note = ""
execs = parse_exec_all(act_out.raw)
if execs:
    notes: list[str] = []
    for i, (kind, payload) in enumerate(execs, 1):
        if self.executor is None:
            notes.append(f"（{i}/{len(execs)} {kind} 未执行：未绑定执行器，仅文本产物）")
            continue
        self.render.info(f"↳ 执行 {kind} ({i}/{len(execs)}) …")
        exec_out = self.executor.run(kind, payload)
        self.context.add_user(f"[执行回显 · {kind} {i}/{len(execs)}]\n{exec_out}")
        notes.append(f"【执行回显 {i}/{len(execs)}（{kind}）】\n{exec_out}")
    if len(notes) != len(execs):
        self.render.warn(
            f"声明 {len(execs)} 个执行请求，实际产生 {len(notes)} 条回显，请核对是否漏执行"
        )
    exec_note = "\n" + "\n".join(notes)
```

**注意**：文件写入**没有事务**，第 3 个写失败时前 2 个已落盘。
应在产物规范里提示模型「按依赖顺序写，被依赖的先写」。

---

## 2. D3：OBSERVE 验收加固（F3）

现状 `obs_prompt`（`loop.py:365-370`）只喂三件套：完成标准、CHECK 自述、待核对产物。
模型因此「见到一条回显就判 pass」。

在 prompt 中补一组可比对的事实：

```python
obs_prompt = (
    "请核对待核对产物是否满足成功标准，给出判定。\n"
    f"【完成标准（计划）】{criteria or '（计划未指定）'}\n"
    f"【成功标准自述（ACT 的 CHECK）】{check or '（ACT 未声明）'}\n"
    f"【本次声明的执行请求】共 {len(execs)} 个\n"
    f"【实际执行回显】共 {len(notes)} 条\n"
    "★ 若两者数量不一致，说明产物未被完整执行，必须判定为 fail，"
    "并在 reason 中写明缺少哪几个。\n"
    f"【待核对产物】\n{result_text}{exec_note}"
)
```

可选加强：从 write 回显 `已写入 <path>（N 字符）` 中解析出路径集合，
与计划步骤里声明的文件名比对，把「缺哪个文件」直接写进 prompt。

---

## 3. D4：借贷符号 —— 银行视角规则（F4）

### 矛盾的证据

- `Step 1 / A4 示例`：客户 A 转 100 给客户 B → **借 A、贷 B**
  （客户存款是银行的**负债**：负债减少记借方，负债增加记贷方 → 示例正确）
- `invariants.py`：`_DIRECTION_SIGN = {DEBIT: 1, CREDIT: -1}`，即余额 = 借 − 贷
  → 这是**资产**账户的方向，与上面的负债账户正好相反
- `store.py`：系统现金账户为 `ASSET` 且 `allow_overdraft=1` → 印证是银行视角

### 若按现状实现会怎样

| 动作 | 分录 | 按「借+贷−」算出的余额 | 实际应为 |
|---|---|---|---|
| 存款 100 | 借 SYS-CASH / **贷 客户账户** | **−100** ❌ | +100 |
| 取款 100 | **借 客户账户** / 贷 SYS-CASH | **+100** ❌ | −100 |
| A 转 B 100 | 借 A / 贷 B | A:+100、B:−100 ❌ | A:−100、B:+100 |

后果：存款直接撞 `NegativeBalanceError`；不变量②（余额 = 分录净额）**恒定 fail**，对账永远对不平。

### 修复：净额符号按账户类型取

| 账户类型 | 增加记 | 减少记 | 净额符号（增/减） | 典型 |
|---|---|---|---|---|
| `ASSET` | 借 DEBIT | 贷 CREDIT | +1 / −1 | 现金、客户贷款 |
| `EXPENSE` | 借 DEBIT | 贷 CREDIT | +1 / −1 | 手续费支出 |
| `LIABILITY` | 贷 CREDIT | 借 DEBIT | −1(借) / +1(贷) | **客户存款** |
| `EQUITY` | 贷 CREDIT | 借 DEBIT | −1(借) / +1(贷) | 所有者权益 |
| `INCOME` | 贷 CREDIT | 借 DEBIT | −1(借) / +1(贷) | 利息收入 |

```python
#: 各账户类型的"余额增加方向"；反方向即减少
_INCREASE_SIDE = {
    "ASSET":     "DEBIT",
    "EXPENSE":   "DEBIT",
    "LIABILITY": "CREDIT",
    "EQUITY":    "CREDIT",
    "INCOME":    "CREDIT",
}


def direction_sign(account_type: str, direction: str) -> int:
    """该账户类型下，该方向对余额的贡献符号：+1 增加 / -1 减少。"""
    inc = _INCREASE_SIDE.get(account_type)
    if inc is None:
        raise ValidationError(f"未知账户类型: {account_type}")
    if direction not in VALID_DIRECTIONS:
        raise ValidationError(f"非法分录方向: {direction}")
    return 1 if direction == inc else -1
```

SQL 侧（`compute_ledger_balance`）：

```sql
SELECT COALESCE(SUM(
    CASE WHEN :inc_side = 'DEBIT'
         THEN CASE WHEN e.direction = 'DEBIT'  THEN e.amount ELSE -e.amount END
         ELSE CASE WHEN e.direction = 'CREDIT' THEN e.amount ELSE -e.amount END
    END), 0)
FROM entries e WHERE e.account_id = :account_id;
```

配套：

- `check_debit_feasible` 改为「按符号算出**变动后余额**再判非负」，
  而不是简单 `current - amount`——否则对负债账户同样会算反。
- Step 2 的对账 SQL（借贷平衡）**不受影响**：① 只看 `借合计 = 贷合计`，
  与账户类型无关，保持原样。受影响的只有 ② 余额方向和 ③ 余额非负判定。

---

## 4. D5：大文件分片写入（F5，建议）

现状 `_write`（`react/executor.py:82-104`）是覆盖式 `write_text`，
载荷靠单个 JSON 字符串承载 `\n` 转义，长文件极易被 `max_tokens` 截断。

建议给 `_write` 增加追加模式：

```python
mode = "a" if bool(data.get("append")) else "w"
with target.open(mode, encoding="utf-8") as f:
    f.write(content)
```

并在 ACT 产物规范里约定：**单文件超过约 3000 字符时，拆成多个 `[EXEC: write]`，
第一个写骨架，后续 `append: true` 续写**。（依赖 F1 落地后才可行。）

---

## 5. D2 / D6：ledger 包补完清单（F6）

按银行视角复核后需要补的文件：

| 文件 | 内容要点 | 依赖 |
|---|---|---|
| `ledger/invariants.py` | `EntryLine`、`direction_sign`、`check_balanced`、`compute_ledger_balance`、`check_balance_matches_ledger`、`check_amount`、`check_debit_feasible`、`check_non_negative_balance` | `errors.py` |
| `ledger/ledger.py` | `open_account`（默认 `LIABILITY`）、`deposit`、`withdraw`、`transfer`、`get_balance`、`get_history`、`find_account_by_no`；`BEGIN IMMEDIATE` + 条件更新 + 幂等键 | `store.py`、`invariants.py` |
| `ledger/__init__.py` | 包导出 | 上述 |
| `ledger/cli.py` | `open-account / deposit / withdraw / transfer / balance / history / reconcile` 子命令 | 上述 |
| `tests/test_ledger.py` | 六类用例：正常转账、余额不足、借贷不平、并发双花、幂等重复提交、对账一致 | 上述 |

**存款 / 取款 / 转账的标准分录（银行视角）**

| 业务 | 借方 | 贷方 |
|---|---|---|
| 存款 100 | SYS-CASH（ASSET +100） | 客户账户（LIABILITY +100） |
| 取款 100 | 客户账户（LIABILITY −100） | SYS-CASH（ASSET −100） |
| A 转 B 100 | A 账户（LIABILITY −100） | B 账户（LIABILITY +100） |

> `open_account` 默认类型必须是 `LIABILITY`，否则与 `store.py` 的
> `SYSTEM_CASH`（ASSET）构不成复式对。

---

## 6. 实施顺序与验收

| 顺序 | 改动 | 验收 |
|---|---|---|
| 1 | F1 `parse_exec_all` | 新断言：含 3 个 EXEC 的产物 → 返回长度 3；旧 `parse_exec` 断言不变 |
| 2 | F2 loop 循环执行 | mock ACT 带 2 个 EXEC → `recorder.calls` 长度 2；条数不符时 `render.warn` 被调用 |
| 3 | F3 OBSERVE prompt | 人工跑一轮多文件写入任务，确认漏写时判 fail |
| 4 | F4 符号按类型 | 单元断言：`direction_sign('LIABILITY','DEBIT') == -1`、`('ASSET','DEBIT') == 1` |
| 5 | F5 append 模式 | 追加两次后文件长度 = 两次之和 |
| 6 | F6 补完 ledger | `python -m tests.run_all` 全绿；CLI 演示脚本端到端跑通 |

**回归基线（每次改完都要跑）**

```bash
python -m tests.run_all          # mock 冒烟，EXIT=0
python -m tests.run_all --live   # 真实 API（当前用 config.deepseek.json）
```

---

## 7. 风险与不做的事

**风险**

- F1/F2 落地后，模型一次 ACT 可能产出更多 EXEC，单轮耗时变长；需观察 `max_rounds` 是否够用。
- 文件写入无事务，多块写入中途失败会留下半成品；靠「依赖顺序 + 幂等键」缓解，不做回滚。
- 改 `direction_sign` 后，任何已落库的旧数据（按旧符号算出）需重算。

**本次不做**

- 不动 `executor._write` 的路径越界保护（越出工作目录即拒绝）——这是对的，保留。
- 不引入 ORM / 第三方依赖，维持 A8「仅标准库」。
- 不把 `ledger/` 挪出仓库（D7 属建议项，等包补完再议）。
