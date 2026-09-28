# coding 能力升级设计：从"结构化函数"到"可交付工程"

> 版本：v1.0（已定稿，待实施）
> 日期：2026-09-27
> 状态：设计已定稿。§14 的五个待决问题已全部决议（首批技术栈 = Python）。
> 本文档是后续代码与 skill 修改的依据。
> 关联：[DESIGN.md](../DESIGN.md) §3.9（coding 能力）、`capabilities/coding/`、`react/loop.py`

---

## 1. 背景与目标

### 1.1 现状

`coding` 能力（`capabilities/coding/`）把 `G:\skill` 的写码方法论适配进了五阶段协议，当前约束到**函数级**：

- 8 字段函数头（`ROLE/DEPENDS_ON/IN/OUT/OWNS_FIELDS/SIDE/ERRORS/LOG`），头是持久真实来源；
- 7 规则头-实现一致性（OBSERVE 判 pass 前必查，分 STALE / MISSING 两个方向）；
- 字段归属唯一 + `DEPENDS_ON` DAG；
- 三种模式：CREATE / MODIFY / req-to-code 增量填充。

**它能约束"每个函数带头且头与实现一致"，但约束不了"这是一个能跑起来的工程"。**

### 1.2 目标

让 `coding` 产出**能交付的工程代码**，即三个可验收物：

| # | 可验收物 | 含义 |
|---|---|---|
| E1 | **需求可追溯** | 每条需求能指出落在哪个文件/符号，且**有真实证据**证明它成立 |
| E2 | **规范可声明** | 技术栈与编码规范是**能力的一处声明**，换栈不改框架 |
| E3 | **完成有证据** | 构建/测试**实际执行过**，验收结论引用真实回执 |

### 1.3 非目标（本方案明确不做）

- **不新增阶段**：`ReActLoop` 的转换与 gate 分支按五阶段名硬编码，加阶段是状态机重构。
- **不做多智能体并行**：token 已呈二次增长（实测一个 10 轮任务 95 次调用、10.87M prompt），并行会成倍放大。
- **不把 TDD 做进状态机**：测试与实现的分离只用"可见性约束"表达，不改流程。
- **不做包管理器式的技术栈依赖求解**：`conventions` 只做声明与注入。

---

## 2. 关键发现（本方案的依据，均已核实）

| # | 发现 | 证据 | 影响 |
|---|---|---|---|
| F1 | **THINK/OBSERVE/VERIFY 及所有修复轮的工具调用不执行**，回执是模型自己的文本或字面量 `"ok"` | [loop.py:798](../../react/loop.py)（`tool_handler=None`）、[:844](../../react/loop.py)（仅非 None 才执行）、[:861-863](../../react/loop.py)（占位分支） | **第 0 层**：任何"要求引用真实证据"的设计都建在沙上 |
| F2 | ACT 是唯一真正执行工具的阶段 | [:571](../../react/loop.py) `_step("act", ..., tool_handler=handler)` | 修正 F1 需把 handler 贯通到其余阶段与修复轮 |
| F3 | OBSERVE 判 pass 只对照"声明执行 N 条 vs 实际回显 M 条"，不读磁盘 | [:663-670](../../react/loop.py) | 执行回显被截断到 4000 字符（[executor.py:27](../../react/executor.py)），超出部分无人核对 |
| F4 | VERIFY 的 prompt 只要求"给出结论"，无实测要求；`final_text` 取 ACT 的 `[RESULT]` | [:703-705](../../react/loop.py)、[:531](../../react/loop.py) | 可以纯文本谎称"测试通过" |
| F5 | 原始任务文本是账本首条，**压缩时永远保留** | [context.py:328-332](../../react/context.py) `[*history[:1], *history[self._digest_upto:]]` | 把编号台账放进 PLAN 产出即可获得"全阶段可见"，无需新机制 |
| F6 | OBSERVE/VERIFY 的工具 schema 来自 `ALL_TOOLS`，与 ACT 相同 | [:671](../../react/loop.py)、[:705](../../react/loop.py) | 工具**已声明**，只差执行接线——修正成本低 |
| F7 | `submit_verdict` 的取值是 `pass/defect/retry/fail`，映射为内部中文判定 | [:249-252](../../react/loop.py)、[:283-285](../../react/loop.py) | 台账逐条对账的结论可复用这套判定语义 |
| F8 | shell 回执形如 `exit=<code>\n<输出截断4000>` | [executor.py:162](../../react/executor.py) | 这是"真实证据"的载体，可用于强制引用 |

---

## 3. 四层方案总览

```
第 0 层  工具执行接线          —— 前提：让"引用真实证据"成为可能
第 1 层  编号需求台账          —— E1 需求可追溯（能力侧 + VERIFY 强制逐条对账）
第 2 层  conventions 声明      —— E2 规范可声明（能力侧声明 + 框架注入）
第 3 层  测试演进可见性        —— 本方案不实施（成本高；改用约定级约束 + 升级判据）
```

各层的**归属与成本**（差异很大，不要混着排期）：

| 层 | 主要归属 | 成本 | 是否本方案实施 |
|---|---|---|---|
| 0 | 框架（`react/loop.py`） | 小（贯通已有 handler） | ✅ 先做 |
| 1 | 能力（`capabilities/coding/*.md`）+ 框架（VERIFY 要求） | 中 | ✅ 做 |
| 2 | 能力（`capability.json`）+ 框架（读取与注入） | 中 | ✅ 做 |
| 3 | 纯框架（需"测试改动可见"机制） | 高 | ⏸️ 先不做，待实测确认行为后再定 |

---

## 4. 第 0 层：工具执行接线（前提）

### 4.1 问题

`_step(..., tool_handler=None)` 时，模型发出的工具调用走占位分支：

```python
else:
    for tc in resp.tool_calls:
        self.context.add_tool(tc["id"], content or "ok")
```

模型收到"回执"（自己的文本或 `"ok"`），**以为自己执行了**。这是静默失败：轨迹看起来正常，实际什么都没发生。

### 4.2 设计

| 改动 | 落点 | 内容 |
|---|---|---|
| 0-a | `_step` 调用点 | 把 `tool_handler` 贯通到 OBSERVE、VERIFY，以及 `_resolve` 的修复轮 |
| 0-b | `_step` 占位分支 | 无 handler 时回执文案改为**显式未执行**（如"（工具未执行：执行器未启用）"），**不再用 `content`/`"ok"` 伪装成功** |

### 4.3 约束

- 0-b 是**行为修正**，不是文案美化：占位回执必须让模型能区分"我跑了"与"没跑"。
- 执行器未启用（`enable_shell_exec`/`enable_file_write` 关闭）时，工具本就不该执行；此时 0-b 的文案是**唯一正确**的反馈。

---

## 5. 第 1 层：编号需求台账

### 5.1 产物契约

`plan` 阶段在步骤列表之外，额外产出**编号需求台账**。每条含四个字段：

| 字段 | 含义 | 例 |
|---|---|---|
| `R<n>` | **编号**（机械可数的钩子） | `R3` |
| 需求 | 一句话，来自用户原话，不改写含义 | 扫描指定目录，按内容分组重复文件 |
| 验收 | **可执行**的验证方式（命令 + 期望） | `pytest tests/test_scan.py::test_groups` 退出码 0 |
| 落点 | 文件 + 符号 | `src/scan.py:find_duplicates` |

落盘到 `<work_dir>/.react-agent/specs.md`（`work_dir` 未设则项目根），**同时留在 PLAN 产出的正文里**——依据 F5，正文进账本后全程可见。

### 5.2 为什么用"编号"而不是表格

编号让"漏了哪条"变成**可数检查**（`R1..Rn` 是否都被提到），而不是靠语义判断。这是本层与"写一份需求文档"的本质区别。

### 5.3 各阶段契约

| 阶段 | 契约 |
|---|---|
| think | 不涉及（决策层） |
| plan | 产出 `R1..Rn` 台账；每条必须**可核验**（写"做好"这类不可判定的验收词即视为不合格） |
| act | 声明本条实现覆盖了哪些 `R` 编号 |
| observe | 判 pass 前对照**当前步骤**的完成标准（不变），并注明覆盖的 `R` 编号 |
| verify | **逐条**对 `R1..Rn` 判 pass/fail，每条引用真实证据；**缺失编号 → 不得判 pass**（fail-safe，与 [:206-225](../../react/loop.py) 判歧义默认"不通过"同源） |

### 5.4 所需框架改动

`_step_verify`（[:703-705](../../react/loop.py)）的 prompt 增加三项要求：

1. 逐条列出 `R1..Rn` 及各自判定；
2. 每条引用真实证据（文件路径 + 关键行 / 命令 + `exit=`）；
3. 有编号未提及 → 判 `fail` 并说明缺哪条。

**不做**：框架不解析台账、不自动比对（那是第 3 层式的机制成本）。台账由模型产出与消费，框架只强制"必须逐条且引用证据"。

---

## 6. 第 2 层：`conventions` 能力声明

### 6.1 schema（首批 = Python 技术栈）

```jsonc
// capabilities/coding/capability.json
{
  "name": "coding",
  "version": "1.1.0",
  "conventions": {
    "language": "python",
    "python_version": ">=3.11",
    "layout": "src/<pkg>/ 放实现、tests/ 放测试、pyproject.toml 声明依赖与入口",
    "naming": "模块/函数/变量 snake_case；类与异常 PascalCase；常量 UPPER_SNAKE",
    "typing": "公开函数签名带类型标注；不引入未声明的第三方类型",
    "error_policy": "边界处显式抛出具体异常（不用裸 except）；内部用 None/默认值降级并记 WARNING；异常不得静默吞掉",
    "log_format": "logging 模块；`业务动作 | 阶段(IN/MAP/ERR/OUT) | key=value`；禁止记录密钥/token/完整 PII",
    "header_style": "8-field-docstring",
    "test_framework": "pytest；测试与实现分文件；测试命名 test_<行为>_<条件>",
    "definition_of_done": [
      "python -m pytest -q 全绿（exit=0）",
      "python -m ruff check . 无 error",
      "入口可跑：python -m <pkg> --help 退出码 0",
      "所有函数带 8 字段头且 7 规则 0 STALE / 0 MISSING"
    ],
    "verify_command": "python -m pytest -q",
    "forbidden": [
      "修改测试以迁就实现（如需改测试必须显式声明理由）",
      "提交密钥或 token",
      "引入未在 pyproject.toml 声明的依赖",
      "用裸 except 吞异常"
    ]
  }
}
```

> **`definition_of_done` 里的命令必须是真实可跑的**。项目没有声明入口（无 `pyproject.toml` / 无 `__main__`）时，该条应由 PLAN 改写为实际存在的验证方式，不得原样保留一条跑不通的验收项——否则 VERIFY 会永远 fail。

### 6.2 必须抽两组东西

只抽一组就不完整：

| 组 | 字段 | 缺了会怎样 |
|---|---|---|
| **约束（怎么做）** | `language`/`layout`/`naming`/`error_policy`/`log_format`/`header_style` | 能验，但同一工程内风格不统一 |
| **验收（怎么算做完）** | `definition_of_done`/`verify_command`/`forbidden` | 规范声明了，但没人验 |

### 6.3 注入方式

- `react/capability.py`：`MANIFEST_KEYS` 增 `"conventions"`；`Capability` 增 `conventions: dict`。
- `ReactService` 装配时下发到 `ActionRegistry`。
- `context.build_step_messages` 在**消息尾部 user** 追加规范段（**仅当能力声明了 `conventions`**）。

### 6.4 缓存约束（重要）

规范段位于消息尾部，**是该请求缓存前缀的一部分**——但它**每步都相同**，只在**能力切换**时变化，因此不会 per-step 打断缓存。

`system` 仍**逐字节静态**（现有 `check_cache_prefix` 必须继续全绿）。

### 6.5 边界

| 情形 | 处理 |
|---|---|
| 未声明 `conventions` | **完全不注入**，行为与今天一致（`default` 能力回归保护点） |
| `conventions` 非 dict | 告警 + 忽略，不抛（与 manifest 既有降级一致） |
| 字段值非字符串 / 列表元素非字符串 | 告警 + 忽略该字段 |
| 执行器未启用 | `verify_command` 无法执行 → 提示"未实测"，不得据此判 pass 也不得据此判 fail（避免无解死循环） |

---

## 7. 第 3 层：测试演进可见性（⏸️ 本方案不实施）

> **决议（Q4）：本方案不实施第 3 层。** 先用第 0–2 层跑 2–3 次真实任务，确认"改测试迁就实现"这一行为是否真的出现，再决定是否投入机制成本。判据见 §7.3。

### 7.1 为什么不能"互相不可修改"

测试**必须能改**——需求变更、接口变更时测试要跟着变。硬性禁止会死锁。

### 7.2 真正的约束（可见性 + 举证责任）

| 允许 | 必须 |
|---|---|
| 实现改了，测试跟着改 | 显式声明"我改了测试，因为 X" |
| 需求变更导致测试重写 | 变更可追溯到台账里那条 `R` 是否变化 |
| — | **禁止**：悄悄改测试以迁就错误实现 |

### 7.3 为什么不现在做（含升级判据）

需要"测试文件改动可见"的机制（如记录 hash 并在 OBSERVE 比对），成本高；且**是否真会出现"改测试迁就实现"的行为，目前没有实测证据**。

**升级判据**（满足任一即重新评估投入）：
1. 真实任务轨迹里出现"测试被改，且改动方向是让**错误实现**得以通过"；
2. 出现"OBSERVE 判 pass 但 pytest 实际 exit≠0"的情形；
3. §11.4 的端到端验收中，`definition_of_done` 的测试项被跳过或被改写。

**若不投入的替代措施**（已包含在第 2 层）：`conventions.forbidden` 里显式写入"修改测试以迁就实现（如需改测试必须声明理由）"，并在 OBSERVE 判 pass 前要求说明测试文件的改动情况。这是**约定级**约束，不建机制，但足以让违规行为在轨迹里可见。

---

## 8. 与五阶段协议的整合

| 阶段 | 本方案带来的变化 |
|---|---|
| THINK | 增"工程 vs 片段"判据：本次交付是完整工程还是单点改动？（决定 PLAN 要不要出骨架与台账） |
| PLAN | ① 工程骨架（目录树/入口/依赖声明/配置样例/测试布局）；② **步骤数放开**（现硬限 2–6 个，多文件工程装不下）；③ 需求→模块→文件三级映射；④ **编号需求台账** |
| ACT | ① 头/规范与工程一致性（不只逐函数各自为政）；② 依赖声明与入口必须落盘；③ 覆盖的 `R` 编号；④ 工具真执行（第 0 层）后，写完可自测一次并带回 `exit=` |
| OBSERVE | ① 真实核对（读盘/跑命令，引用证据）；② 骨架完整性；③ 7 规则不变 |
| VERIFY | ① **逐条** `R1..Rn` 对账；② 构建/测试实测并引用 `exit=`；③ 交付物清单（路径 + 作用）；④ 可复现说明（怎么跑起来）；⑤ 工具真执行（第 0 层） |

---

## 9. 公共接口 / schema / 数据流变化

| 接口 | 变化 | 兼容性 |
|---|---|---|
| `capability.json` | 增可选 `conventions`（dict） | 缺失 = 不注入，`default` 与旧 `coding` 行为不变 |
| `MANIFEST_KEYS` | 增 `"conventions"` | 未知字段告警逻辑不变 |
| `Capability` dataclass | 增 `conventions: dict = {}` | 带默认值 |
| `ActionRegistry` / `Action` | 承载并下发 `conventions` | 带默认值 |
| `context.build_step_messages` | 尾部 user 追加规范段（条件注入） | `system` 仍逐字节静态 |
| `_step` | `tool_handler` 贯通到 OBSERVE/VERIFY/修复轮；占位回执改为显式"未执行" | 行为修复 |
| `_step_verify` prompt | 增逐条对账 + 实测要求 | 文案变化 |
| OBSERVE prompt | 增"产物可能被截断（上限 4000 字符）"+ 真实核对要求 | 文案变化 |
| 磁盘 | 新增 `<work_dir>/.react-agent/specs.md` | 新增产物 |
| `DEFAULTS` / `CONFIG_FIELDS` | **不新增配置键** | 对齐断言继续绿 |

**数据流**：
`capability.json: conventions` → `resolve_capability()` → `ReactService` 装配 → `ActionRegistry` → `build_step_messages()` 尾部注入 → 模型在 PLAN/ACT/OBSERVE/VERIFY 看到同一份规范；
`plan` 产出 `R1..Rn` → 账本（全程可见）+ `specs.md` 落盘 → `verify` 逐条对账。

---

## 10. 边界情况与失败模式

| 情形 | 处理 |
|---|---|
| 执行器未启用 | 工具回执显式"未执行"；验收层降级为"声明未实测"，**既不算 pass 也不算 fail** |
| 命令超时（`exec_timeout_sec`） | 回执是"（执行超时）"→ 视为**未验证**，不得当通过 |
| 纯咨询 / 单文件小任务 | THINK 判为"片段"→ 跳过骨架与台账；不强制跑构建 |
| 台账条目过多（>10） | VERIFY 允许按模块归并展示，但**不得省略未达成项** |
| 需求中途变更 | 台账条目**只增不改**（新增 `R<n+1>`，废弃条目标 `R<k> (废弃:理由)`），保留可追溯性 |
| `work_dir` 越界 | `specs.md` 写在 `work_dir` 内，越界由既有执行器拒绝 |
| `conventions` 字段类型错 | 告警 + 忽略该字段，不阻断 |
| 能力未声明 `conventions` | 完全不注入（回归保护） |

---

## 11. 测试与验收

### 11.1 新增断言 `check_engineering_quality(base_dir)`

全程 Mock 模型、零 API 消耗：

| # | 断言 |
|---|---|
| 1 | **规范注入**：能力声明 `conventions` 时，PLAN/ACT/OBSERVE/VERIFY 的尾部 user 均含规范段 |
| 2 | **未声明时零注入**：不声明能力的输出与今天**逐字节相同**（回归保护） |
| 3 | **system 仍静态**：任何能力/阶段下 `messages[0]` 逐字节相同（复用既有 `check_cache_prefix` 断言） |
| 4 | **OBSERVE 核对要求**：obs_prompt 含"产物可能被截断"+真实核对要求；执行器未启用时该要求被降级 |
| 5 | **VERIFY 逐条对账**：verify prompt 含"逐条 `R` 编号"与"缺失编号不得 pass" |
| 6 | **工具真执行**（第 0 层）：用会记录调用的假 handler，断言 OBSERVE/VERIFY/修复轮的工具调用**真的执行了**；无 handler 时回执文案为"未执行"而非 `"ok"` |
| 7 | **manifest**：`conventions` 非 dict → 告警不抛；`MANIFEST_KEYS` 已含它 |
| 8 | `CONFIG_FIELDS` / `DEFAULTS` 对齐断言仍绿（本方案不新增配置键） |

### 11.2 反向验证（每条断言都要能红）

逐项破坏并确认断言失败：去掉规范注入、把 handler 透传改回 `None`、删掉 VERIFY 的逐条要求、把占位回执改回 `"ok"`。

### 11.3 回归基线

`main.py --smoke`、`python -m tests.run_all`、`tools/cache_probe.py` 三者 exit=0。

### 11.4 端到端人工验收（真实 API，1 次）

用 `--capability coding` 跑一个小型但完整工程（例：带 CLI 入口、读 CSV 去重、带 pytest 的 dupfinder）：

- 产物是**目录结构**而非散函数；
- `pytest` **真跑过**，`exit=0` 出现在历史账本里；
- OBSERVE 的 `reason` 引用了文件路径；
- VERIFY 逐条列出 `R` 编号；
- `data/token_stats` 记录本次消耗与命中率（用于第 3 层与阈值决策）。

---

## 12. 假设与风险

### 12.1 假设

1. 目标交付是**真实工程**（多文件、可构建、可测），不只是单文件脚本。
2. 沿用"归属已有五阶段"的约束，**不新增阶段**。
3. 默认取"强制引用真实回执"，**不让框架替用户跑命令**（`verify_command` 先只作为提示）。
4. **首批技术栈 = Python 3.11+**（`conventions` 的默认值即按此写，见 §6.1）。非 Python 项目通过新增 `coding-<lang>` 能力支持，框架不改。

### 12.2 风险

| # | 风险 | 缓解 |
|---|---|---|
| R1 | 强制真实核对**增加工具调用**，推高已很严重的 token 成本（实测 95 次调用 / 10.87M） | 只强制核对**至少一项**关键事实，不做全量核对；把截断上限写进提示，让模型知道何时必须 `read` |
| R2 | 规范段改变消息尾部 → 属缓存前缀一部分 | 规范每步相同，只在能力切换时变化；`check_cache_prefix` 守住 |
| R3 | 首批技术栈已定为 Python，但 `conventions` 的默认值（pytest/ruff/src 布局）可能与用户实际项目习惯不符 | 默认值是**可改的声明**而非硬编码；`definition_of_done` 里跑不通的命令由 PLAN 改写为实际存在的验证方式（§6.1 注）；非 Python 走 `coding-<lang>` 能力 |
| R4 | 模型仍可能谎报"跑过命令" | 回执由执行器生成并进账本，措辞与编造不同；终检 gate 永远拦人。**不宣称已杜绝** |
| R5 | 台账可能沦为形式（写成"都完成了"） | VERIFY 强制引用命令与 `exit=`；不可判定的验收词视为不合格 |
| R6 | 第 0 层修复后，工具调用真实执行会带来**额外的 token 与时间开销**（此前是零成本的假回执） | 这是必要成本——假回执比开销更贵。第 1 层只强制核对"至少一项"，不做全量核对 |

---

## 13. 实施顺序（每步独立可验收、可回滚）

| 步 | 内容 | 验收 |
|---|---|---|
| 1 | **第 0 层**：`tool_handler` 贯通 + 占位回执改显式未执行 | 断言 6；三基线 |
| 2 | **第 1 层（框架部分）**：OBSERVE 真实核对要求 + VERIFY 逐条对账要求 | 断言 4、5 |
| 3 | **第 2 层（框架部分）**：`conventions` 读取与注入 | 断言 1、2、3、7 |
| 4 | **能力侧改写**：`capabilities/coding/` 五份 SKILL.md + `capability.json` 加 `conventions` | 端到端人工验收 |
| 5 | 反向验证 + 三基线 + 真实任务复测 | 全部通过 |
| 6 | **（本次不做）第 3 层**：依 §7.3 的升级判据决定是否投入 | — |

---

## 14. 决议记录（原待决问题，已定稿）

| # | 问题 | **决议** | 落地位置 |
|---|---|---|---|
| Q1 | 台账最小条目数 | **≥1 条即可**。简单任务不必凑数；但一旦产出台账，VERIFY 就必须逐条对账 | §5.1、§5.3 |
| Q2 | 需求中途变更时台账如何演进 | **只增不改**：新增 `R<n+1>`；废弃条目标 `R<k> (废弃:理由)` 而**不删除**，保留可追溯性 | §10 |
| Q3 | `verify_command` 是否由框架强制跑并据 `exit` 自动判 fail | **否**。只作为提示注入，由模型自行决定跑与不跑并引用回执。升级路径留待第 3 层一并评估 | §6.1、§12.1-3 |
| Q4 | 是否现在就投入第 3 层 | **否**。改用约定级约束（`forbidden` 声明 + OBSERVE 要求说明测试改动），并给出 3 条升级判据 | §7.2、§7.3 |
| Q5 | 首批目标技术栈 | **Python 3.11+**。`conventions` 默认值按此写定；非 Python 走 `coding-<lang>` 新能力，框架不改 | §6.1、§12.1-4 |

**由 Q5 派生的具体约定**（已写入 §6.1 的 `conventions`）：`src/<pkg>/` + `tests/` 布局、`pyproject.toml` 声明依赖与入口、pytest + ruff 作为验收命令、公开函数带类型标注、裸 `except` 与未声明依赖列入 `forbidden`。

**下一步**：按 §13 顺序实施，第 1 步为第 0 层（工具执行接线）——它不依赖任何已决问题，是独立的缺陷修复。
