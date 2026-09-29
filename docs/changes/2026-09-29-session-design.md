# Session 设计：上下文即会话，会话即可重建的事实

> 版本：v1.0
> 状态：**设计稿（待评审）**——尚无实施依据效力，评审通过后改为「实施中」
> 日期：2026-09-29
> 取代：[架构设计 §1.1/§1.4/F4/F5](2026-09-29-agent-architecture.md)（该部分只给方框图，实施细节以本文为准）
> 入口：[AGENTS.md](../../AGENTS.md) · 规则：[STRUCTURE.md](../STRUCTURE.md)
> 本文档是 [2026-09-29-agent-architecture.md](2026-09-29-agent-architecture.md) 第 1 部分 §1.1 / §1.4 的**展开**——那份只给了方框图，本文给出可实施的全部细节。
> 依据：那篇清单的"会话持久化日志（可恢复 / 可回放 / 可审计）"与"状态持久化（崩溃不丢、格式有版本号与迁移）"。

---

## 1. 要解决的到底是什么问题

现在"上下文"= `SessionContext` 的内存对象。`reset()` 清掉的东西里，**有一半是"上下文的真实状态"**，而它们没有落盘：

| 字段 | 作用 | 丢失后果 |
|---|---|---|
| `messages` | 全量账本 | 任务过程完全消失 |
| `_digest_upto` | 摘要覆盖的边界 | 窗口与摘要错位（**曾出过负值 bug**） |
| `_digest_text` | 早前历史摘要 | 压缩成果全丢，等于没压 |
| `plan` / `plan_index` | 计划与当前步 | 忘了做到哪一步 |
| `_factor` | 分词校准系数 | 预算估算失准（注释明确"跨任务有效"） |
| `session_memory` | 上任务结论 | 跨任务失忆 |
| `round_no` | 轮次 | 成本归因断裂 |

**结论：session 处理方式 = 把这七项从"内存里的偶然状态"变成"可落盘、可重建的事实"。**

---

## 2. session 的定义与边界

```
session（一个可持久化实体）
├── 元信息：目标、当前能力、工作目录、创建时间、schema_version
└── task[]（时间序）
    ├── 输入：用户原话
    ├── 过程：轮次 → 阶段 → 模型调用 / 工具调用
    └── 结论：最终结果、状态、成本
```

| 概念 | 定义 | 与现有代码的对应 |
|---|---|---|
| **session** | 一串任务 + 它们共享的上下文状态 | `SessionManager` 的 `Session`（现仅内存） |
| **task** | 一次 `run()`：从用户输入到最终结果 | `LoopResult` |
| **turn** | 一次模型调用 | `token_stats` 的一条 |
| **step** | 一个阶段的一次进入 | `loop` 里的 `_step_*` |

**边界（不做什么）**：
- session 不是"多用户/多租户"的隔离单元（当前单机单用户）
- session 不负责跨会话的长期知识（那是工作记忆 `work.md` 与将来的能力包）
- session 不保存工具产生的文件内容本身（只存"谁写了哪个路径"）

---

## 3. 落盘布局

```
data/sessions/<sid>/
├── session.json      元信息 + 版本号（原子写：临时文件 + 替换）
├── journal.jsonl     追加式事件流（唯一权威来源）
└── context.json      上下文状态快照（可重建，故可丢）
```

**为什么 journal 是唯一权威**：`session.json` 和 `context.json` 都能从 journal 重新算出来。这样崩溃时只需保证"journal 的最后一行可能不完整"，其余都能恢复。

### 3.1 `session.json`

```json
{
  "schema_version": 1,
  "id": "7bea163ea979",
  "created": "2026-09-29 08:18:00",
  "updated": "2026-09-29 08:29:37",
  "goal": "实现 CSV 内容重复行去重工具（CLI + pytest）",
  "capability": { "name": "coding", "version": "1.1.0" },
  "work_dir": "G:\\one",
  "totals": { "tasks": 3, "calls": 61, "prompt": 3142741, "cached": 2828467 }
}
```

字段来源：`goal` 取首个任务的前 N 字符（可被 `--goal` 覆盖）；`totals` 从 journal 聚合，与现有 `token_stats` 同口径。

### 3.2 `journal.jsonl`（每行一个事件）

| `type` | 何时写 | 关键字段 |
|---|---|---|
| `session_start` | 建会话 | `goal`, `capability`, `work_dir`, `schema_version` |
| `task_start` | 任务开始 | `task_id`, `input` |
| `step_enter` | 进入阶段 | `task_id`, `round`, `phase` |
| `model_request` | **发请求前** | `task_id`, `phase`, **`messages`（完整）**, `tools`, `params` |
| `model_response` | 收到响应 | `usage`, `finish_reason`, `tool_calls`, `content`（截断或落盘） |
| `tool_call` | 工具执行 | `name`, `args`, `result`（超限则落盘 + `result_ref`） |
| `compaction_start` | 压缩开始 | `task_id`, `from`, `to`, `before_tokens` |
| `compaction_end` | 压缩完成 | `dropped`, `digest_len`, `after_tokens` |
| `task_end` | 任务结束 | `status`, `result`, `rounds`, `usage` |
| `session_end` | 会话关闭 | `reason` |

**三条写入规则**：

1. **先写后做**（write-ahead）：`model_request` 必须在**发请求之前**落盘。这样崩溃后能看到"发过但没回"的调用——这正是那篇清单要的"宁可留下可检测的孤儿，也不要静默损坏"。
2. **不合法行丢弃，不导致损坏**：读取时逐行 `json.loads`，失败即停止（后续行必然是崩溃残留），并记录 `truncated_at`。
3. **大内容落盘**：`messages` 里的超长工具结果、`content` 超阈值时，正文写 `data/sessions/<sid>/blobs/<hash>`，journal 里只存引用与摘要。

### 3.3 `context.json`（可重建，故可丢）

```json
{
  "schema_version": 1,
  "digest_upto": 128,
  "digest_text": "早前 128 条消息的摘要…",
  "factor": 2.34,
  "plan": ["读需求", "建台账", "实现 core.py"],
  "plan_index": 2,
  "round_no": 7,
  "message_count": 431
}
```

**崩溃后恢复流程**：优先读 `context.json`；若缺失或 `message_count` 与 journal 重放结果不符，则**从 journal 重放重建**（重放是对账，不是猜测）。

---

## 4. 上下文如何从 session 重建

这是"上下文处理成 session 方式"的核心——上下文不再是内存里生长出来的，而是**从 session 状态按固定顺序装配**：

```
build_step_messages(phase) =
  [0] system                      ← 逐字节静态（缓存契约，来自代码常量）
  [1] 台账 work.md 的"需求台账"段   ← 来自 work_dir（§5）
  [2] 历史 = 摘要 + messages[_digest_upto:]  ← 来自 session
  [3] 尾部 user：
        skill 正文（能力包）
        conventions（能力声明）
        工作记忆（工程事实板）
        当前步骤指令
```

对照现有的五层模型（架构文档 §1.3）：**第 2 层来自能力包、第 3/4 层来自 work_dir、历史来自 session**。session 负责的是"历史 + 摘要边界 + 计划状态"这一块——**恰好是现在最容易丢的部分**。

**跨任务时**（`reset()` 之后）：session 仍在，`messages` 清空，但 `session_memory`（上任务结论）与 `work.md` 提供连续性。**这就是"上下文处理成 session 方式"的落地含义：任务清空账本，但会话不清空事实。**

---

## 5. 与工作记忆（`work.md`）的分工

| | session（`data/sessions/`） | 工作记忆（`<work_dir>/.react-agent/work.md`） |
|---|---|---|
| 记录什么 | **过程**：谁在什么时候说了什么、调了哪个工具 | **事实**：工程结构、台账进度、已改文件、关键决策 |
| 给谁看 | 审计 / 回放 / 恢复 | **模型**（注入上下文） |
| 生命周期 | 跨进程，可归档 | 跨任务、跨会话，跟着工程走 |
| 体积 | 大（MB 级） | 小（KB 级） |
| 丢了怎样 | 无法复盘，但任务能继续 | **agent 失忆**，要重新摸一遍工程 |

**一句话**：session 是"录像"，work.md 是"笔记本"。录像给人复盘，笔记本给 agent 干活。

---

## 6. 压缩与 session 的关系（可检测的锁）

那篇清单要求把压缩做成"记录在案的锁操作"。用 journal 实现，**不加新机制**：

```
compaction_start  →  …生成摘要、替换窗口…  →  compaction_end
```

- 只有 `start` 没有 `end` = **孤儿锁**，恢复时**必然可检测**（对照：若用"标记已压缩"的写法，崩溃后就是一个谎称完成的 end）
- 恢复时遇到孤儿锁：**保守处理**——不信任该次压缩的产物，回到 `start` 记录里的 `from` 边界重做
- `_digest_upto` 只由 `compaction_end` 推进，保证"摘要覆盖范围"与"窗口起点"永远一致（这正是之前出负值 bug 的地方）

---

## 7. 生命周期与 API

| 操作 | CLI | Web |
|---|---|---|
| 建会话 | `main.py` 启动时隐式建 | `POST /api/session` |
| 列会话 | `--list-sessions` | `GET /api/sessions` |
| 看会话 | `--show-session <sid>`（摘要） | `GET /api/sessions/<sid>` |
| 恢复 | `--resume <sid>` | 前端"继续上次会话" |
| 回放 | `tools/replay.py --session <sid>` | — |

（现有相关入口：`tools/cache_probe.py` 是同目录下的工具脚本，可作为 `tools/replay.py` 的写法参考。）

**恢复语义**（要说清，否则会踩坑）：
- 恢复的是**会话状态**（goal、能力、工作目录、上次结论、摘要边界），**不自动重跑**任何任务
- 工作目录**以 session 里的为准**（否则恢复到一个错目录，agent 会在错误的地方继续干活）
- 能力**以 session 里的为准**；若该能力已不存在 → 报错并列出可用项（**不静默回退**，与现有 `resolve_capability` 一致）

---

## 8. 保留与体积

| 项 | 策略 |
|---|---|
| blob 去重 | 按内容哈希命名，同一文件多次读只存一份 |
| 大工具结果 | 超阈值只存引用与摘要（默认 4000 字符，与 `_OUTPUT_LIMIT` 一致） |
| `messages` 快照 | **全量存**（这是"可重建"的前提，不能省）；靠保留策略控总量 |
| 保留策略 | 最近 N=20 个 session 全量；更早的删除 `journal.jsonl` 的 `model_request.messages`（保留其余事件），会话仍可审计但不可逐字重建 |
| 上限 | 单 session 目录超 200 MB 时告警（不自动删，交用户决定） |

---

## 9. 实施顺序

| 步 | 内容 | 验收（含反向验证） |
|---|---|---|
| **S1** | 落盘骨架：`session.json` + `journal.jsonl`，接 `session_start` / `task_start` / `task_end` / `session_end` | 跑一个 Mock 会话 → 文件存在、行数正确；**截断最后一行 → 读取不抛错且标 `truncated_at`** |
| **S2** | `model_request` / `model_response` 全量落盘（含 messages 与 usage） | 从 journal **逐字节重建**出每次发送的 messages；**删掉一条 `model_request` → 重建结果与原始不符（断言必须能发现）** |
| **S3** | `context.json` + 恢复：`--resume` 重建 `_digest_upto` / `_digest_text` / `plan` / `_factor` | 恢复后 `build_step_messages` 与崩溃前**逐字节一致**；**把 `context.json` 删除 → 能从 journal 重放重建出同样的值** |
| **S4** | 压缩锁：`compaction_start` / `compaction_end`，孤儿锁检测 | 构造"只有 start"的日志 → 恢复时必须报告孤儿锁并回到 `from` 边界；**去掉 end 事件 → 断言失败** |
| **S5** | 录制回放（L2）： `tools/replay.py` 用 journal 的 messages 驱动 `MockClient`（[model.py](../../react/model.py)，已存在） | 同一 journal 回放两次结果一致；**改一处提示词 → 回放断言失败（证明它真能挡回归）** |

**每步都要有反向验证**（像前几轮那样：故意破坏 → 断言必须变红），否则断言是摆设。

---

## 10. 与架构文档的关系

| 架构文档 § | 本文档 |
|---|---|
| §1.1 总体形态（方框图） | §2 定义与边界、§3 落盘布局 |
| §1.3 上下文五层模型 | §4 上下文如何从 session 重建 |
| §1.4 记忆三类 | §5 与工作记忆的分工 |
| F4 "session 日志" | §9 S1–S3 |
| F5 "录制回放" | §9 S5 |
| （未提） | §6 压缩锁、§7 生命周期与 API、§8 保留策略 |

**权威关系（双向声明）**：本文档**取代**架构文档 §1.1 / §1.4 与 F4 / F5 的实施细节（更细、且带 schema）；
架构文档其余部分（九项关注点、编码能力契约、F1–F3、C1–C5）仍为**当前有效**。
登记见 [index.md](index.md)。

---

# 附录：本文档的自身状态

- 状态：设计稿（待评审）。评审通过后，本文件的 `状态` 改为「实施中」，并在 `index.md` 同步。
- 未决：§9 的 S1–S5 尚未开工；架构文档 F4 / F5 已改为指向本文。
