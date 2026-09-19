# ReAct Agent CLI 设计文档

> 版本：v1.7（已实现）  
> 日期：2026-09-16  
> 状态：核心循环 + 原生工具调用 + LocalExecutor + 上下文窗口化 均已落地；新增 skills_code 写代码专用档案（§3.9）；新增 shell OS 级沙箱（§4.1）；新增 service 层与 Web 对话前端（§3.10）

---

## 0. 变更速览（v1.0 → v1.4）

| 版本 | 关键变更 | 对应问题 |
|---|---|---|
| v1.1 | 输出显示模板拦截层（display） | — |
| v1.2 | THINK 驱动状态机完整落地、计划修订保留已完成前缀 | — |
| v1.3 | OBSERVE 三值分流 + VERIFY 不通过回炉 + 验收数据流 | — |
| **v1.4** | **① 解析失败安全默认（ESCALATE/不通过）；混合控制信号通道（原生工具调用为主 + 文本兜底）；③ 上下文窗口化；④ ACT 经 LocalExecutor 真实执行（默认关闭）；② 明文密钥治理（env 覆盖 + .gitignore）；⑧ 工具调用回写 `role=tool`；⑥ 判定文本兜底同义词（修"未通过"误判为通过的坑）；⑦ ASK 独立轮次上限（防死循环）** | ① ② ③ ④ ⑤ ⑥ ⑦ ⑧ ⑩ |
| **v1.5** | **新增 `skills_code/` 写代码专用档案：把 `G:\skill` 的 8 字段头 / 7 规则 / req-to-code / solution-review / flow-tracer / visual-digest 方法论适配进五槽位协议；不改 `load()` 与 `G:\skill`；新增 `config.code.example.json` 与两个 CLI 便捷环境变量（`REACT_AGENT_SKILLS_DIR` / `REACT_AGENT_CONFIG`）** | 写代码 agent 需求 |
| **v1.6** | **shell 执行接入 OS 级沙箱（仅 Windows）：`react/win32_sandbox.py` 用受限令牌（剥特权）+ 作业对象（kill-on-close / 禁 breakaway / 进程数上限 / 内存上限）+ 可选低完整性级别，把 agent 起的自家命令进程关进内核隔离；`sandbox_shell` 配置开关，默认关** | 市面 agent 安全范式调研后落地 |
| **v1.7** | **① service 层（`react/service.py`）：`Renderer` 协议补全并归位、新增 `on_token` 流式钩子（修 loop.py 从未透传 on_token 的死代码）、`AgentEvent`/`EventRenderer`/`NullRenderer`/`ControlChannel`/`ReactService`，让 CLI/MCP/Web 三方共用同一套驱动；② 配置统一到 `react/config.py`（`ConfigError`，不再 SystemExit）；③ 断言迁出 `cmd_smoke`（221 行）进 `tests/`；④ 新增 Web API（`react/webapi.py`，starlette + SSE）与 React+Vite 前端（`web/`）** | 前端对话需求 + 结构优化 |

> 设计哲学不变：认知阶段作为槽位 × 目录约定绑定 skill。新增的是"控制信号结构化"与"执行可控落地"，产物正文仍走人类可读的自由文本。

---

## 1. 背景与目标

### 1.1 背景

用户拥有 17 个自研 skill（位于 `~/.claude/skills/`），涵盖开发流程编排（dev-flow 链）、代码质量、测试用例转换、提交信息等场景。现状是这些 skill 依赖 Claude Code / OpenCode 的宿主环境，缺乏一个**自研的、可完全掌控的终端 agent 工具**来按自己的方式驱动它们。

### 1.2 目标

构建一个类似 OpenCode 的**终端交互式 agent 工具**，具备：

1. **显式 ReAct 推理框架**：推理（THINK）、规划（PLAN）、执行（ACT）、观察（OBSERVE）、验收（VERIFY）五步协议，每步对用户可见、可纠偏。
2. **skill 可插拔**：通过**目录约定**将任意 skill 绑定到任意动作槽位，绑定关系由用户按需填写，不写死在代码里。
3. **轻量可运行**：Python 3.11 + rich + openai SDK，模型走 kimi-k2.7，单目录工程，无外部服务依赖。

### 1.3 非目标（本期不做 / 已实现但默认关闭）

- **ACT 默认仍是纯文本产物**：真实执行（shell / 写文件）经 `LocalExecutor` 提供，**默认关闭**，需 `config.json` 显式开启 `enable_shell_exec` / `enable_file_write` 才生效（见 §4）。agent 不会在用户未授权时触碰环境。
- TUI 全屏界面（保持 REPL）
- 多模型路由、子 agent 并行调度
- 持久化记忆（跨会话记忆仅做 `/save` 导出，不做自动加载优化）

---

## 2. 总体架构

```
┌──────────────────────────────────────────────────────┐
│  REPL 终端层（main.py + render.py，rich 分色渲染）      │
│  用户输入 → 指令解析 → 触发 ReAct 循环 → 结果展示        │
├──────────────────────────────────────────────────────┤
│  ReAct 主循环（react/loop.py）                        │
│  THINK → [PLAN] → ACT → OBSERVE → [VERIFY] → 下一轮   │
│  控制信号（决策/判定）走原生工具调用，产物走自由文本      │
├──────────────────────────────────────────────────────┤
│  动作注册表（react/action.py）                         │
│  5 个动作槽位；每个槽位 = 绑定的 SKILL.md || 内置默认    │
│  Action 预留 executor 扩展点（已接 LocalExecutor）       │
├──────────────────────────────────────────────────────┤
│  Skill 绑定层：skills/<动作名>/SKILL.md 目录约定        │
├──────────────────────────────────────────────────────┤
│  会话上下文（react/context.py）：消息历史 / 轮次 / 窗口化 │
├──────────────────────────────────────────────────────┤
│  执行器层（react/executor.py，LocalExecutor，默认关闭）  │
│  ACT 的 [EXEC] 请求 → shell / 写文件（cwd 受限 + 超时） │
├──────────────────────────────────────────────────────┤
│  模型层：openai SDK → https://api.kimi.com/coding/v1   │
│  模型：kimi-k2.7（OpenAI 兼容；tool_choice="auto"）     │
└──────────────────────────────────────────────────────┘
```

### 2.1 核心设计理念：动作槽位（Action Slot）

THINK / PLAN / ACT / OBSERVE / VERIFY 是 5 个**槽位**。启动时扫描 `skills/<槽位名>/SKILL.md`：

- 存在 → 该动作的**系统提示** = SKILL.md frontmatter + 正文
- 不存在 → 使用代码内置的默认系统提示

用户对绑定的控制方式：

```
# 方式一：手动复制/软链
cp -r ~/.claude/skills/dev-flow  skills/plan/

# 方式二：命令一键绑定（从 ~/.claude/skills 按名字查找）
python main.py --bind plan dev-flow
```

**这让"每个推理和执行按 skill 要求设计"落地为数据而非代码**：换 skill 即换行为，主循环零改动。

---

## 3. 模块设计

### 3.1 目录结构

```
react-agent/
├── main.py              # 入口：REPL 主循环、指令解析、子命令
├── config.json          # 模型配置（本地文件，不入版本库）
├── config.example.json  # 配置模板（默认：文件写入关）
├── config.code.example.json  # 配置模板（写代码：enable_file_write 开、shell 关）
├── react/
│   ├── __init__.py
│   ├── loop.py          # ReAct 主循环引擎
│   ├── action.py        # 动作定义、注册表、skill 加载
│   ├── context.py       # 会话上下文
│   ├── executor.py      # LocalExecutor：shell / write 双原语（默认关闭 + 可选 OS 沙箱）
│   ├── win32_sandbox.py # Windows OS 级沙箱（ctypes，零依赖，非 Windows 自动降级）
│   ├── config.py        # 统一配置加载（ConfigError + 环境变量覆盖 + 默认值 + 安全告警）
│   ├── service.py       # service 层：AgentEvent / EventRenderer / NullRenderer /
│   │                    #   ControlChannel(Cli/Queue/Auto) / ReactService（三方共用驱动）
│   ├── webapi.py        # Web API：starlette + SSE（/api/session|task|events|control|…）
│   └── render.py        # rich 终端渲染 + Renderer 协议唯一定义处
├── tests/               # 自测包（纯 Python，零新增依赖）：smoke_checks.py / run_all.py
├── web/                 # React + Vite 前端（node_modules 与 dist 已 gitignore）
├── skills/              # ← skill 绑定区（目录约定，内置默认/通用行为）
│   ├── think/SKILL.md
│   ├── plan/SKILL.md
│   ├── act/SKILL.md
│   ├── observe/SKILL.md
│   └── verify/SKILL.md
├── skills_code/         # ← 写代码专用档案（适配器式，不改 load()/G:\skill）
│   ├── think/SKILL.md   #   CREATE/MODIFY 自检（ROLE 字段为信号）+ 编码决策
│   ├── plan/SKILL.md    #   需求分析 + DEPENDS_ON DAG + 8 字段头规划 + 增量骨架
│   ├── act/SKILL.md     #   8 字段头 + 7 规则一致 + [EXEC: write] 落盘
│   ├── observe/SKILL.md #   三件套核对 + solution-review + 7 规则表 + flow-tracer
│   └── verify/SKILL.md  #   两级验收 + flow-tracer 概览 + visual-digest 速览
├── docs/
│   └── DESIGN.md        # 本文档
└── README.md
```

### 3.2 main.py — 入口与 REPL

职责：进程入口、配置加载、指令解析、驱动循环。

关键行为：

- `python main.py`：进入 REPL
- `python main.py --bind <动作> <skill名>`：从 `~/.claude/skills/<skill名>/` 复制到 `skills/<动作>/`，完成后退出
- `python main.py --smoke`：非交互自检（见 §9）
- `python main.py --skills-dir <路径>`：指定自定义 skill 绑定根目录（默认 `./skills`）

REPL 指令集（以 `/` 开头为用户指令，其余为任务输入）：

| 指令 | 行为 |
|---|---|
| `/help` | 显示指令列表与当前绑定状态 |
| `/reset` | 清空会话上下文，重新开始 |
| `/save <文件>` | 将会话全文导出到文件 |
| `/binds` | 列出 5 个槽位的绑定状态（skill 名或"内置默认"） |
| `/quit` | 退出 |

任务输入进入 ReAct 循环；循环运行期间支持步进控制：

| 控制键 | 行为 |
|---|---|
| `c` | 继续下一步 |
| `s <修正>` | 人工纠偏：修正文本注入下一轮 THINK 的用户消息 |
| `q` | 中止当前循环，回到 REPL 等待新任务 |

### 3.3 react/action.py — 动作注册表

```python
@dataclass
class Action:
    name: str                    # 槽位名：think / plan / act / observe / verify
    skill_path: Path | None      # 绑定的 SKILL.md 路径；None = 内置默认
    skill_body: str              # 注入的系统提示正文
    executor: Executor | None    # 扩展点：未来挂工具执行器，本期恒为 None

class ActionRegistry:
    def load(self, skills_root: Path) -> None:
        """扫描 skills/<name>/SKILL.md，构建 5 个槽位。缺失槽位用内置默认。"""

    def get(self, name: str) -> Action: ...

    def bind(self, action_name: str, skill_dir: Path) -> None:
        """--bind 子命令使用：复制 skill 目录到 skills/<action_name>/。"""
```

skill 加载规则：

- 解析 SKILL.md 的 YAML frontmatter，提取 `name`、`description`
- 正文（frontmatter 之后）作为该动作的系统提示
- frontmatter 解析失败不报错：降级为"全文当正文、name 取目录名"

内置默认系统提示（每个槽位一段，见附录 A）保证未绑定 skill 时工具开箱可用。

### 3.4 react/loop.py — ReAct 主循环（v1.2：think 槽位完整落地）

状态机（THINK 为驱动槽位，每轮先跑 THINK 再按其决策行流转）：

```
            ┌──────────┐   ASK     ┌──────────────┐
            │  THINK   │ ────────▶ │ 提问→等回答   │ ──答案入历史──┐
            └────┬─────┘           │ (不计轮数)    │             │
        决策行6值 │                   └──────────────┘             │
   ┌────────┬───┴────┬──────────┬───────────┐                    │
 PLAN     ACT     DONE/      ESCALATE                         │
   │        │      VERIFY        │ (status=escalated 退出)      │
 是 ┌──┐    │        │            │                             │
   ▼    ▼   ▼        ▼            │                             │
┌─────┐ ┌─────┐ ┌──────────┐     │                             │
│PLAN │ │ ACT │ │ VERIFY 通过→结束 │     │                             │
└──┬──┘ └──┬──┘ │ 不通过→回THINK │     │                             │
   └───┬───┘    └──────────────┘     │                             │
       ▼                          │                             │
  ┌─────────┐   通过              │                             │
  │ OBSERVE │ ──→推进计划指针─────┘                             │
  └────┬────┘                                                 │
       │ 缺陷/重试(累计3次→强制THINK选PLAN)                    │
       └──────────────────────────────────────────────────────┘
```

决策词汇表（6 值，由 THINK 决策行 `下一步: X` 给出）：

| 决策 | 语义 | 框架行为 |
|---|---|---|
| `PLAN` | 任务需要多步规划 | 调 PLAN 产步骤表 → ACT → OBSERVE |
| `ACT` | 直接执行当前步骤 | 调 ACT（当前计划步骤）→ OBSERVE |
| `DONE`/`VERIFY` | 完成/需终检 | 跳过执行直接 VERIFY → 结束 |
| `ASK` | 缺少关键信息 | 提问面板（去掉决策行）→ 阻塞等回答 → 答案注入历史 → 不计轮数 → 继续 |
| `ESCALATE` | 超出能力/人工接管 | 带理由结束，status=`escalated` |

THINK 输出契约：`[THOUGHT] 现状：… | 决策理由：…`（必填），`假设：…`、`风险：…`（选填），决策行。非 ASK 的 THINK 之后才走人工 gate（c/s/q）。

THINK skill 内含**思考路径**（每轮必经，引导思考方向）：① 回顾历史 → ② 连问五题（信息/能力/达成/就绪/复杂度）→ ③ 暴露前提（假设/风险）→ ④ 权衡候选（比较至少两个决策的代价）→ ⑤ 开放任务追加意图检查 → 收敛为决策行。

循环护栏：单任务最大轮数默认 10（config `max_rounds`），超限强制退出并提示人工接管。

循环语义修正（v1.2.1 修正确性，v1.3 验收数据流）：

- **计划修订**（THINK 在计划未执行完时再次决策 PLAN）：`set_plan(preserve_position=True)` 保留已完成前缀，只替换剩余步骤——已完成的步骤不重做；万一新计划要推翻已完成部分，人在 PLAN 输出的 gate 上可否决。
- **VERIFY 回路**：VERIFY 判"不通过"时不再无条件结束——注入反馈（验收依据 + "必须 ACT/PLAN 修正，不得直接 DONE"）回到 THINK 回炉，`max_rounds` 兜底；判"通过"才 `status=done`。
- **验收数据流**（v1.3）：完成标准沿链路传递——PLAN 每步自带 `| 完成标准：…`（解析为 (步骤, 标准) 元组）；ACT 在 `[RESULT]` 前立 `[CHECK]` 段引用该标准（缺失时框架用 plan 标准兜底，再缺按步骤描述核对）；OBSERVE 收到三件套（计划标准 + CHECK 自述 + 产物）按标准核对。
- **OBSERVE 三值分流**（v1.3，此前压平）：`通过`→推进计划指针；`缺陷`→指针不动，OBSERVE 的缺陷说明作为修正反馈**注入历史账本**，下轮 ACT 修正后重产；`重试`→指针不动，原样重跑（不带缺陷说明）。任一项累计 3 次仍强制 THINK 重出 PLAN，且作废待修正反馈。

其余槽位每步输出协议（渲染层按标签解析）：

| 步骤 | 输出标签 | 内容要求 |
|---|---|---|
| PLAN | `[PLAN] 1. 步骤 \| 完成标准：…` | 编号步骤列表，每步一句话 + 可核对标准 |
| ACT | `[CHECK] …` + `[RESULT] …` | 先立标准（引用计划）后产产物；可选 `[FORMAT:]`；可附 `[EXEC: shell\|write]` 块请求本地执行（需配置开启） |
| OBSERVE | `[OBSERVATION] 通过/缺陷/重试 + 说明` | 对照标准三件套核对的结论 |
| VERIFY | `[VERIFY] 通过/不通过 + 验收说明` | 对照最初目标的终检结论 |

### 3.5 react/context.py — 会话上下文

```python
class SessionContext:
    messages: list[dict]      # OpenAI 消息格式历史
    round_no: int             # 当前轮次
    plan: list[tuple[str, str]]  # 当前计划步骤（PLAN 产出后填充）：[(步骤, 完成标准)]
    plan_index: int           # 执行到第几步
    max_rounds: int

    def add_user(self, text: str) -> None: ...
    def add_assistant(self, text: str) -> None: ...
    def build_step_messages(self, action: Action, step_prompt: str) -> list[dict]:
        """组装某一步的完整消息：系统提示 = 全局规则 + 动作 skill 正文 + 步骤指令。"""
```

要点：

- 每步调用的 system = **全局协议说明**（ReAct 规则、输出标签要求）+ **该动作绑定的 skill 正文** + **（可选）历史摘要** + **当前步骤指令**
- **全量账本始终保留**（`self.messages` 不裁剪），`/save` 导出的是完整轨迹；仅"发给模型的"做窗口化（问题③）：始终保留首条任务锚点 + 最近 `max_context_messages` 条原文，更早消息折成 ≤20 行摘要并入 system，**不改动全量账本**，故窗口化不会丢失可追溯性
- `/reset` 清空重来；`/save` 导出 Markdown（含 `tool_calls` / `role=tool` 回执）

### 3.6 react/render.py — 终端渲染

rich 分色规范（每步一个面板，面板头含步骤名 + 耗时 + token 数）：

| 步骤 | 颜色 | 面板标题示例 |
|---|---|---|
| THINK | 蓝 | `◆ THINK · 1.2s · 843tok` |
| PLAN | 黄 | `◇ PLAN · 0.8s · 512tok` |
| ACT | 绿 | `▶ ACT · 2.4s · 1.9ktok` |
| OBSERVE | 紫 | `● OBSERVE · 0.9s · 640tok` |
| VERIFY | 青 | `✔ VERIFY · 1.1s · 700tok` |

- ACT 的 `[RESULT]` 产物用独立代码块样式展示，标注"产物，请人工取用"
- 流式输出：每步逐步渲染 token
- 轮次边界打印分隔线与轮次号
- THINK 面板展示模型的思考过程（kimi `reasoning_content`，dim 斜体区块在结论上方），config `show_reasoning` 控制开关（默认开）

### 3.7 输出显示模板拦截层（v1.1 追加）

ACT 产物到达渲染层时，先经 `react/display.py` 按内容类型选择展示模板，不再一律纯文本面板。

**检测链**（先显式声明，后自动识别）：

1. 显式标签 `[FORMAT: json|table|plan|diff|code|md]`（最高优先级，覆盖一切自动识别）
2. 整体合法 JSON → 格式化 indent=2 + 语法高亮
3. 整体围栏代码块 → 按语言语法高亮
4. diff 行模式（`@@`/`diff --git` 头，或同时含增删两侧且占比 ≥30%；排除 markdown 列表防误判）
5. 纯 Markdown 表格 → rich Table（多表格/混合内容回退 Markdown 整文）
6. 编号计划清单（≥2 项且覆盖 ≥60% 非空行）→ ☐ 勾选框清单
7. 兜底 → Markdown 渲染（装了 markdown-it-py 用 rich.Markdown，否则内置零依赖迷你渲染器：标题/列表/粗体/行内代码）

**渲染规则**：

- 仅 ACT 产物走拦截层，其余 4 步骤保持纯文本面板
- 面板标题带类型标记：`▶ ACT · json · 2.4s · 56tok`
- 面板内 = 模板视图 + `── 原文 ──` 分隔 + 完整原文（rich 可复制，`/save` 不受影响）
- 声明了模板但内容解析失败（如 `[FORMAT: table]` 但无表格语法）→ 安全回退 Markdown
- `loop.py`/`context.py`/`model.py` 零改动，拦截完全在渲染层

**模型侧配合**：act 槽位默认提示词与种子 SKILL.md 说明可选 `[FORMAT:]` 声明；act 槽位被用户 skill 绑定时自动识别兜底。

---

## 3.8 控制信号通道：原生工具调用 + 文本兜底（v1.4）

决策（THINK 的"下一步"）与判定（OBSERVE/VERIFY 的 通过/缺陷/重试/不通过）是**结构性控制信号**，此前靠正则解析 `[THINK]`/`[OBSERVATION]` 文本，脆弱且易被格式漂移击穿（问题①）。v1.4 改为**双通道**：

1. **主通道：原生工具调用（function calling）**。控制阶段（THINK/OBSERVE/VERIFY）向模型注入两个工具：
   - `decide_next_step(decision, reason, thought)` — THINK 必须调用，给出下一步动作
   - `submit_verdict(verdict, reason)` — OBSERVE/VERIFY 必须调用，给出判定
   模型以结构化参数返回，框架免正则直接取用（抗格式漂移）。
2. **兜底通道：文本解析**。`kimi` 思考模式与强制 `tool_choice` 不兼容，仅支持 `tool_choice="auto"`，故工具调用可能缺失。任一控制步若未拿到合法结构化结论，框架走文本解析兜底；仍失败时触发**自我修正回路**（最多 2 次，追加提示要求重新调用工具），最终仍失败则落安全默认（决策→`ESCALATE` 移交人工；判定→`不通过`，绝不静默通过）。

**OpenAI 规范回写（问题⑧）**：模型返回 `tool_calls` 后，历史账本同时写入 `assistant(tool_calls)` 与紧随其后的 `role=tool` 回执消息，保证后续任意一次模型调用的历史都合法（否则 OpenAI / kimi 会拒绝"悬挂的 tool_call"）。该回执不影响轨迹可见性——工具参数同样以可读文本渲染到面板与 `/save` 账本。

产物正文（PLAN 步骤表、ACT 的 `[RESULT]`）**始终走自由文本**，保持人类可读与流式可见；只有"控制旋钮"结构化。

**文本兜底同义词（问题⑥）**：当模型在 `tool_choice="auto"` 下未调工具、退化为文本时，`parse_verdict` 需从中文短语识别判定。v1.4 扩展同义词分组（合格/达标/满足/符合→通过；不合格/不符合/未达成/失败→不通过；重做/重跑→重试），并**修正"未通过/没通过"曾因包含"通过"子串被误判为正向**的缺陷——失败词优先级高于通过词。歧义仍落安全默认"不通过"。

**ASK 轮次上限（问题⑦）**：ASK 不计入 `max_rounds` 预算（对话不耗轮数），但若模型反复提问永不收敛，会绕过上限死循环。v1.4 新增独立计数 `_ask_count`，超过 `_MAX_ASK_TURNS`（默认 6）即 ESCALATE 移交人工。

### 3.9 写代码专用档案（skills_code/，v1.5 新增）

通用 `skills/` 适合"方案/分析"类任务，而"写代码"需要更强的方法论约束（结构化头、头-实现一致性、增量骨架、方案评审、数据流概览）。为此新增一套**适配器式档案** `skills_code/`，把 `G:\skill` 仓库的写码方法论融进五槽位协议——**不动 `ActionRegistry.load()`（目录名=槽位名、每槽位单 SKILL.md 的硬约束原样保留），也不改 `G:\skill` 源仓库**，仅用目录约定换一套行为。

**使用方式**：

```bash
python main.py --skills-dir G:\react-agent\skills_code          # REPL（文件写入需在 config 开启）
python main.py --smoke    --skills-dir G:\react-agent\skills_code # 静态回归（零 API）
```

配套 `config.code.example.json`：`enable_file_write: true`、`enable_shell_exec: false`（写码默认落文件、不跑 shell）；CLI 便捷环境变量 `REACT_AGENT_SKILLS_DIR` / `REACT_AGENT_CONFIG` 可省去每次传参（见 §5）。

**核心契约（贯穿五槽位）**：

1. **8 字段函数头**（`ROLE / DEPENDS_ON / IN / OUT / OWNS_FIELDS / SIDE / ERRORS / LOG`）：每个待写函数必带，是"持久真实来源"（header-injector 定位）。四种语言模板（Python docstring / JS·TS JSDoc / C·C++ `/* */` / Java Javadoc）字段结构一致。
2. **7 规则头-实现一致性**（flow-tracer 权威源）：`OWNS_FIELDS_MATCH` / `DEPENDS_ON_MATCH` / `SIDE_MATCH` / `IN_MATCH` / `OUT_MATCH` / `LOG_MATCH` / `ERRORS_MATCH`，每规则区分 `STALE`（头多声明）与 `MISSING`（头少声明）两种漂移。
3. **CREATE vs MODIFY 检测**：以目标函数当前是否已有 `ROLE` 字段为唯一信号（`G:\skill` dev-flow 适配），决定走"写新码"还是"改既有码"路径。
4. **req-to-code 增量骨架**：Round 1 先产完整头 + 占位符 body（`# TODO(round-N): ...` + `raise NotImplementedError`/stub），后续按 `DEPENDS_ON` **upstream first** 顺序填充，每方案三维度评审（是什么/为什么/优点与对比）+ 批判三问。

**槽位 → G:\skill 方法论映射表**：

| 槽位 | 适配器档案职责 | 主要来源（G:\skill） | 关键适配点 |
|---|---|---|---|
| `think` | 编码模式自检（CREATE/MODIFY）+ 六值决策 + 前提四要素（数据样例/位置/目的/输出必须问） | dev-flow（think 路径）、code-formatter（补头场景） | 把"是否已有 ROLE 字段"固化为 CREATE/MODIFY 的唯一信号；ASK 优先追问四类前提 |
| `plan` | 需求分析前提表 + 架构设计（DEPENDS_ON DAG、字段唯一归属）+ 内联 8 字段头 Python 模板 + 增量骨架 | req-to-code、dev-flow-create | 步骤带可核对完成标准；每字段有且只有一个归属函数 |
| `act` | `[CHECK]`+`[RESULT]`；CREATE/MODIFY/增量填充；8 字段头；7 规则自检；`[EXEC: write]` 落盘块 | header-injector（8 字段头）、dev-flow-create/modify | 落盘块 `{path, content}` 相对 cwd、越界拒绝；日志禁止记录密钥/PII |
| `observe` | 三件套核对 + `submit_verdict` + solution-review 三维度 + 7 规则表（STALE/MISSING）+ 字段归属/影响核对 | solution-review、flow-tracer | 冲突以计划标准为准；任何 STALE/MISSING → `defect`/`retry` 回炉 |
| `verify` | `submit_verdict` 对照最初目标两级验收（需求/完整性/头一致/落盘/日志安全）+ flow-tracer 概览 + visual-digest 速览 | flow-tracer、visual-digest | 0 STALE/0 MISSING 才算通过；速览只提炼不重新生成 |

> 适配原则：五槽位的正文（skill_body）在 `build_step_messages` 中被拼进每步 system 提示；`G:\skill` 的"强制结构化头 + 一致性校验"被浓缩为 ACT 产物契约与 OBSERVE 核对表，既保留方法论约束，又不破坏 ReAct 协议的自由文本产物流。

### 3.10 service 层与 Web API（v1.7 新增）

终端 REPL 之外还要支撑 MCP 与 Web 两个入口，而 `ReActLoop` 直接耦合「渲染回调 + gate/ask 阻塞回调」，导致此前只能靠 `mcp_server.py` 手写 `_NullRenderer`（7 个空方法，与残缺协议脱节）来凑无头运行。v1.7 引入 service 层**把这些协作方抽象成可替换实现**，而不是重写主循环。

**核心抽象（`react/service.py`）**：

| 组件 | 作用 |
|---|---|
| `AgentEvent(type, action, text, payload)` | 结构化事件，Web/日志/断言的统一载体 |
| `EventRenderer` | 把渲染调用转成 `AgentEvent` 推给 sink；`on_token(action)` 返回逐 token 推流闭包 |
| `NullRenderer` | 静默渲染（MCP 无头），可选收集事件；取代手写 `_NullRenderer` |
| `ControlChannel` | 统一 `gate`/`ask` 语义，三种实现：`CliControl`（`input()`，REPL）、`QueueControl`（Web，队列阻塞等待 POST 喂入）、`AutoControl`（无头自动继续） |
| `ReactService.build_runtime()` | 收拢 registry/context/model/executor/loop 的构造序列（此前 main.py 与 mcp_server.py 各写一份） |

**协议补全**：`Renderer` 协议迁到 `react/render.py` 并补全为 7 个方法 + `on_token` 钩子（此前 loop.py 里的副本只声明 3 个，与 loop 实际调用脱节）。

**流式修复**：`loop.py:431` 此前调用 `model.complete(messages, tools=tools)` **从未传 `on_token`**，而 `model.py` 的逐 token 回调本来可用——流式链路是死代码。现改为 `on_token=self.render.on_token(action_name)`；`RichRenderer` 返回 None（终端行为不变），`EventRenderer` 返回推流闭包。

**Web API（`react/webapi.py`，starlette + SSE，零新增依赖）**：

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/session` | POST | 建会话，返回 `session_id`、绑定状态、配置摘要（不回传密钥） |
| `/api/task` | POST | 起工作线程跑循环，立即 202 |
| `/api/events` | GET（SSE） | 推 `AgentEvent`：`round`/`token`/`step`/`ask`/`gate`/`notice`/`done` |
| `/api/control` | POST | 步进控制 `continue`/`steer`/`abort`（对应终端 c/s/q） |
| `/api/answer` | POST | 回答模型 ASK |
| `/api/state` `/api/save` `/api/reset` | GET/POST | 轨迹账本 / Markdown 纪要导出 / 清空 |
| `/` 及 `/assets` | GET | 托管 `web/dist`，未匹配路径 SPA 回退到 index.html |

**关键约束**：`ReActLoop.run()` 同步阻塞（模型重试含 `time.sleep`），因此**每个任务跑在独立工作线程**，asyncio 侧只做队列轮询与推送，绝不占事件循环；`gate`/`ask` 阻塞在 `queue.Queue` 上由 HTTP 端点喂入。会话为内存态（刷新即失），多标签互不干扰。

**前端（`web/`，React + Vite）**：`useReducer` 消费 SSE 事件流，组件含 `StepPanel`（五槽位分色，沿用 §3.6 规范）、`StreamText`、`GateBar`、`AskPanel`、`RoundDivider`、`StatusBar`。dev 时 Vite(5173) 代理 `/api` → 8000；prod 时 `npm run build` 产出 `dist/` 由 starlette 托管。

## 4. ACT 执行边界与扩展点（v1.4 已实现执行器）

本期 ACT 的执行语义 = **skill 驱动的文本产物 + 可选的本地执行**：

1. ACT 的系统提示来自 act 槽位绑定的 skill（未绑定用默认）
2. 模型产出 `[RESULT]` 结构化文本（方案 / 代码片段 / 分析结论）
3. 若 ACT 在产物中附带 `[EXEC: shell|write]` 块（见 §3.4 输出协议），且配置已开启对应开关，框架经 `LocalExecutor` **真实执行**该请求，回显交给 OBSERVE 作为观察对象
4. 执行默认关闭：未开启时，`[EXEC]` 块仅作为文本产物呈现，agent 不触碰环境

**LocalExecutor（已实现，react/executor.py）**：

```python
@dataclass
class LocalExecutor:
    cwd: Path
    allow_shell: bool = False       # 默认关
    allow_file_write: bool = False  # 默认关
    timeout_sec: int = 30
    def run(self, kind: str, payload: str) -> str: ...
```

安全设计（默认全关，零越权）：

- **默认拒绝**：`allow_shell` / `allow_file_write` 均为 `False` 时，任何 `[EXEC]` 一律拒绝并说明原因，`/save` 账本与回显均可见
- **工作目录受限**：`write` 的目标路径必须解析后落在 `cwd` 之内，越界（如 `../../etc/passwd`）拒绝
- **超时兜底**：`shell` 执行受 `timeout_sec` 约束，避免挂死
- **回显截断**：执行输出截断到 4000 字符，避免超长输出灌爆上下文

### 4.1 shell 的 OS 级沙箱（v1.6，仅 Windows）

`enable_shell_exec` 开启后，agent 起的命令进程默认以**当前用户完整令牌**运行（cwd 限制只是 Python 层软约束，并非 OS 隔离）。v1.6 引入 `react/win32_sandbox.py`，在配置 `sandbox_shell: true` 时把每个 shell 子进程关进**内核级隔离**，对标 Codex CLI / Chrome 沙箱思路，纯 ctypes、零额外依赖：

1. **受限令牌**（`CreateRestrictedToken` + `DISABLE_MAX_PRIVILEGE`）：剥掉进程所有特权（SeDebug / SeShutdown / SeTakeOwnership 等）。即便当前用户是管理员，子进程也只是"无特权的该用户"，无法做提权类动作。
2. **作业对象（Job Object）**：把子进程及其后代关进一个内核作业——
   - 创建子进程时**先尝试 `CREATE_BREAKAWAY_FROM_JOB`**，使其从**外层作业**（若本进程已被托管运行时如 WorkBuddy 塞进某作业）脱离，再挂入本 agent 自建的作业——外层作业若允许 breakaway（设 `JOB_OBJECT_LIMIT_BREAKAWAY_OK`），则 Job 隔离完全生效；
   - `KILL_ON_JOB_CLOSE`：父进程（本 agent）退出时作业内所有进程一并被杀，杜绝孤儿后台服务/监听端口；
   - 本作业**禁止** breakaway（不置 `JOB_OBJECT_LIMIT_BREAKAWAY_OK`）：`cmd /c start xxx` 想脱离作业另起炉灶的进程仍被作业兜住、随作业同归于尽；
   - `ActiveProcessLimit`：限制进程总数，防 fork 炸弹；
   - 可选 `JobMemoryLimit`：限制作业总内存，防内存耗尽（部分 Windows 版本对内存上限较挑剔，设置失败则自动降级忽略）。
3. **可选低完整性级别（Low IL）**：`sandbox_integrity_low: true` 时把令牌降到 Low（S-1-16-4096），使其无法写入 Medium/High 完整性对象（系统目录、他人数据）；开启时会自动用 `icacls <cwd> /setintegritylevel L` 把工作区降为 Low IL（**持久性改动**，可用 `icacls <cwd> /setintegritylevel M` 还原），默认关闭。

**安全降级与边界**：

- 非 Windows 平台：`sandbox_available()` 返回 False，自动回退普通 `subprocess.run`，行为等价于 v1.5。
- **作业嵌套环境**（进程已被托管运行时塞进某外层作业）：先尝试 `CREATE_BREAKAWAY_FROM_JOB` 让子进程脱离外层作业；若外层作业**不允许** breakaway，子进程继承进外层作业、`AssignProcessToJobObject` 失败——此时本 agent 的 Job 隔离整层失效，降级为**软隔离兜底**：超时/退出时 `kill_tree` 递归杀整个子进程树（替代失效的 kill-on-close 防孤儿），并轮询子进程树的进程数/内存做软限制（替代失效的 `ActiveProcessLimit` / `JobMemoryLimit`）。无论哪种情况，受限令牌降权始终生效，逃逸防护不失能。另：`KILL_ON_JOB_CLOSE` 在嵌套环境下因内核限制无法设置（与是否成功 breakaway 无关），作业仅保留进程数上限；模块通过 `last_run_job_effective` 暴露本次 Job 是否真正生效，供状态展示。
- 沙箱运行期异常不致命：`run_sandboxed` 抛任何异常都会被 `LocalExecutor._shell` 捕获，告警并安全回退到普通执行，**不会因一次沙箱故障拖垮整个 agent**。
- 已知边界（与 OS 沙箱定位一致，列为后续扩展）：子进程仍运行在当前用户身份下，对本用户有权访问的文件可读写；**不阻断网络**（网络隔离需防火墙 API 或独立网络命名空间）；真正的账号/容器级隔离需另建低权账户或容器。

主循环、渲染层、绑定机制**零改动**即可接入执行器——`executor` 作为 `ReActLoop` 的可选字段注入，未注入（None）时 ACT 退化为纯文本产物。

> 设计取舍：执行器只暴露 `shell` 与 `write` 两种最小原语，不做"工具市场"。真实工程如需更丰富能力（grep、http、git 等），按同一 `Executor` 协议扩展即可，且必须复用上述安全默认。

---

### 4.5 方案确认 `[方案]`：用留痕替代逐步打断（v1.8）

**动机**：早期设计靠「每步拦人确认」来保证方向不跑偏，代价是 6 步任务要点 18–20 次。
但真正需要人拍板的只有四类时刻（计划不合理 / 信息缺失 / 产物有错 / 目标跑偏），
**ACT 执行并不在其中**——计划阶段已经定过方向，ACT 只是落实。

因此改为：ACT 在**存在方案选择空间的步骤**上，主动自证「为什么这么做」并留痕，
**写完继续执行，不停下来等批准**。人想评审时随时回看依据，不必被每一步打断。

**输出格式**（放 `[RESULT]` 之前）：

```
[方案]
```text
目的：要解决什么问题
约束：什么条件限制了我（给可验证的数字）
方案：一句话讲明白怎么做
预期：做完后能达到什么效果；不这么做的后果
对比：A 当前方案 / B 备选一 / C 备选二
  | 维度 | A | B | C | ... （统一维度对比）
  胜出原因：在给定约束下 A 为什么胜出
  什么情况下该选别的：诚实说明 A 不是最优的场景
```
```

**规则**

- **触发条件**：这一步有没有「其实也可以那样做」的余地？有才写，没有不写（避免又变成每步负担）
- 必须列出至少 1 个备选；确实无替代时写明"为什么没有其他选择"，不硬凑
- 对比用统一维度，不能拿 A 的性能比 B 的成本
- 约束给数字——"性能好"不算，"QPS 200→1000"才算
- 诚实说明适用边界

**实现**：`parse_solution()` 解析 → `Renderer.solution()` 下发。
终端渲染为独立面板；Web 渲染为**默认折叠的方案卡片**（`solution` 事件），
扫描时一眼能看到做过哪些决策，展开才读细节。

这套规范与 `~/.workbuddy/skills/solution-review` 的三维度（①方案是什么
②为什么是这个方案 ③与备选对比）同源，此处只是把它接进了 ReAct 循环。

---

## 5. 配置设计

`config.example.json`：

```json
{
  "base_url": "https://api.kimi.com/coding/v1",
  "api_key": "<在此填入你的 key>",
  "model": "kimi-k2.7",
  "max_rounds": 10,
  "step_timeout_sec": 120,
  "show_reasoning": true,
  "max_context_messages": 12,
  "enable_shell_exec": false,
  "enable_file_write": false,
  "exec_timeout_sec": 30
}
```

配置字段：

| 字段 | 默认 | 说明 |
|---|---|---|
| `base_url` / `api_key` / `model` | — | 模型接入（必填） |
| `max_rounds` | 10 | 单任务最大轮数，超限强制退出并提示人工接管 |
| `step_timeout_sec` | 120 | 单步模型调用超时（含重试） |
| `show_reasoning` | true | 是否展示模型思考过程（kimi `reasoning_content`） |
| `max_context_messages` | 12 | 发给模型的最近原文条数上限；超出部分折成摘要（问题③窗口化） |
| `enable_shell_exec` | false | 是否允许 ACT 经 `[EXEC: shell]` 真实执行命令（问题④，默认关） |
| `enable_file_write` | false | 是否允许 ACT 经 `[EXEC: write]` 真实写文件（默认关，路径限 cwd 内） |
| `exec_timeout_sec` | 30 | shell 执行超时 |
| `sandbox_shell` | false | shell 执行是否套 **OS 级沙箱**（仅 Windows 生效：剥特权令牌 + 作业对象隔离，见 §4.1）；开启 `enable_shell_exec` 才有效 |
| `sandbox_integrity_low` | false | 沙箱内是否降为低完整性级别（需把 cwd 降 IL，默认关，见 §4.1） |
| `gate_mode` | `plan` | 人工闸门档位，见 §5.1 |
| `work_dir` | `""` | 写盘 / 执行命令的工作目录，仅限项目根目录内的子目录，见 §5.3 |

- 启动时读取 `config.json`，缺失或字段不全 → 打印缺失项并退出；缺失字段可用环境变量兜底（见下）
- **环境变量覆盖（问题②）**：`REACT_AGENT_API_KEY` / `REACT_AGENT_BASE_URL` / `REACT_AGENT_MODEL` 可覆盖对应字段，**优先级高于文件**。推荐用环境变量提供 key，避免明文落盘
- **CLI 便捷环境变量（v1.5）**：`REACT_AGENT_SKILLS_DIR`（覆盖 `--skills-dir` 默认值，写码场景设 `skills_code`）、`REACT_AGENT_CONFIG`（覆盖默认配置文件路径，写码场景设 `config.code.json`）。二者仅覆盖默认值，命令行参数显式传入时优先于它们；详见 §3.9 与 `main.py`。
- **明文密钥告警**：若 `api_key` 来自 `config.json` 明文且未用环境变量遮罩，启动时打印安全提示，并建议把 `config.json` 加入 `.gitignore`；**若文件曾被提交或共享，务必到服务商处轮换 key**
- 模型调用失败（网络/限流）：重试 2 次（指数退避 1s/2s），仍失败则该步报错并允许人工 `s` 纠偏继续
  （鉴权/参数类 4xx 属永久性错误，**不重试**，立即失败并给出针对性提示）

### 5.1 人工闸门档位 `gate_mode`（v1.8）

闸门解决的是「什么时候打断人」。粒度太细会把「可干预」变成「必须干预」——
旧实现每个阶段都拦一次（think/plan/act/observe/verify），6 步任务要点 18–20 次「继续」，
真正需要人拍板的那一次反而被淹没在噪音里。

| 档位 | 何时拦人 | 6 步任务（全通过）点击数 |
|---|---|---|
| `plan`（**默认**） | 计划生成后确认一次方向；之后只在「必须拦」的时刻出现 | ≈ 2 |
| `step` | 每个计划步骤收尾（OBSERVE 之后）+ 最终验收 | ≈ 7 |
| `auto` | 连计划也不拦，只在「必须拦」的时刻出现 | ≈ 1 |
| `phase` | 每阶段一次（**旧行为**，保留作回滚开关） | ≈ 18–20 |

**恒定必拦**（不受档位影响，保证「需要人做决定的时刻一定拦」）：

- 模型主动提问 ASK（走 `ask` 通道，不经过 gate）
- OBSERVE 判「缺陷 / 不通过」
- 最终验收 VERIFY——且人工在此纠偏后**不许直接 done**，会强制回炉修正

`plan` 档的设计理由：**改方向最便宜的时机是开跑之前**。计划一旦认可，
各步骤就是机械执行，再逐步确认等于让人重复批准自己刚批准过的东西（见 §4.5）。
在 `plan` 档下人工纠偏计划 → 置 `force_plan`，下一轮强制重出计划。

### 5.3 工作目录 `work_dir`（v1.8）

默认写盘 / 执行命令的根是项目根目录（`base_dir`）。想让 agent 把代码写到某个子目录，
用 `work_dir` 指定，发任务时也可临时覆盖。

**安全边界：只允许项目根目录内的子目录。** 由 `resolve_work_dir()` 统一裁决：

| 输入 | 结果 |
|---|---|
| 空 / 未配置 | 项目根目录 |
| 项目内已存在的子目录（`workspace`） | 生效 |
| 项目内不存在的子目录 | 回退为项目根目录 + 告警（**不代为创建**，避免 agent 到处建目录） |
| 越出项目根目录（`../foo`、`C:\Windows`） | 回退为项目根目录 + 告警 |
| 越出项目根目录 **且** `allow_outside_work_dir=true` 且为绝对路径 | 生效（显式授权才放行） |

`allow_outside_work_dir` 默认 `false`——**默认仍收紧在项目内**，要指向别的工程目录
必须显式打开。这条边界是配置选择，不是代码里焊死的常量：
相对路径始终按项目内解析；绝对路径只在开关打开时才允许越界，且目录必须已存在。
越界告警的优先级高于「目录不存在」告警（越界是安全相关，先报）。可用 `REACT_AGENT_ALLOW_OUTSIDE_WORK_DIR` 覆盖。

落到此目录后，`LocalExecutor` 原有的「目标路径必须落在 cwd 内」约束继续生效——
所以模型即使写出 `../escape.py` 这类路径也逃不出去（已实测）。

可用 `REACT_AGENT_WORK_DIR` 覆盖；Web 端状态条上有「目录」输入框，随任务下发。

### 5.2 随时插手（暂停 / 纠偏 / 中止）

闸门解决「什么时候主动停」，随时插手解决「人想主动介入时怎么办」——
否则一旦不按步骤拦，运行中就完全没有介入入口。

`ControlChannel.peek()` 非阻塞查看待处理指令，loop 在**每个阶段边界**
（`_step` 开头）调 `_check_interrupt()`：

| 指令 | 行为 |
|---|---|
| `pause` | 就地阻塞等待（复用 gate 通道），前端弹出步进条，可继续 / 纠偏 / 中止 |
| `steer` | 把「人工纠偏」注入账本，从下一阶段起生效，**不打断执行** |
| `abort` | 置中止标志，循环收尾退出 |

边界而非中断：不打断正在进行的模型调用，指令在下一个阶段边界生效。
无头场景（`AutoControl`）`peek()` 恒返回 None。
Web 端 `/api/control` 接受 `pause`，前端状态条在运行中显示「暂停 / 中止」按钮。

判定集中在 `ReActLoop._should_gate(action, verdict)`（纯函数，可直接单测），
`_apply_gate_if_needed()` 负责调用并把拦截原因（`_gate_reason`）透给前端展示。
`gate is None`（`--smoke` / MCP 无头场景）时恒不拦。

可用 `REACT_AGENT_GATE_MODE` 环境变量覆盖；Web 端每次发任务时可随 `gate_mode` 字段下发，
前端状态条上有「步进 / 自动」切换。非法值回退 `step` 并告警。

---

## 6. 关键数据结构

```python
@dataclass
class StepOutput:
    action: str          # think / plan / act / observe / verify
    raw: str             # 模型原始输出
    parsed: str          # 标签之后的正文
    elapsed_sec: float
    tokens: int          # 该步 completion tokens

@dataclass
class LoopResult:
    status: str          # done / aborted / max_rounds_exceeded
    rounds: int
    final_text: str      # 最终产物或中断说明
```

---

## 7. 错误处理

| 场景 | 处理 |
|---|---|
| config.json 缺失/损坏 | 启动即报错退出，给出修复指引 |
| skill 的 SKILL.md 缺失（槽位空） | 静默使用内置默认，`/binds` 中显示"内置默认" |
| SKILL.md frontmatter 损坏 | 降级：全文作为正文，槽位可用，启动时打印一次 WARNING |
| 模型调用失败 | 重试 2 次后退化为该步报错面板，提示 `s` 纠偏或 `q` 退出 |
| OBSERVE 连续 3 次判"重试" | 强制升级：下一轮 THINK 必须输出 PLAN 重新规划，避免原地打转 |
| 超 max_rounds | 强制结束，导出当前轨迹提示人工接管 |

---

## 8. 典型使用流程

```text
$ python main.py
ReAct Agent · kimi-k2.7 · 绑定: think✓ plan✗ act✗ observe✗ verify✗
> /help
...
> 帮我设计一个 Excel 测试用例转 Markdown 的脚本方案
◆ THINK · 1.2s · 843tok
  [THOUGHT] 这是代码设计任务，需求较清晰，但需要多步产出，选定 PLAN...
◇ PLAN · 0.9s · 600tok
  [PLAN] 1. 明确输入输出结构 2. 设计转换流程 3. 给出代码骨架
▶ ACT · 2.4s · 1.9ktok        ← 第 1 步
  [RESULT] 输入：Excel（测试步骤/确认点/Display type 列）...
● OBSERVE · 0.8s · 550tok
  [OBSERVATION] 通过，输入输出定义完整
c                             ← 用户确认继续
▶ ACT · 3.1s · 2.2ktok        ← 第 2 步
...
```

---

## 9. 验证方案

1. **冒烟测试** `python main.py --smoke`：
   - 用固定输入脚本驱动一轮完整循环（不经人工步进，步进控制自动 `c`）
   - 断言：THINK/ACT/OBSERVE 三个必备步骤的输出标签存在且非空；PLAN/VERIFY 至少出现其一；循环正常终止
   - 断言：绑定扫描正确（临时目录放一个假 skill 验证绑定优先级）
2. **人工实测**：
   - REPL 跑一个真实小任务，检查分色渲染、步进控制（`c`/`s`/`q`）、`/binds`、`/reset`
   - 验证 `--bind plan dev-flow` 后 `/binds` 显示绑定、且 PLAN 行为按 dev-flow 要求变化
3. **异常路径**：故意删掉 config.json、放坏 frontmatter 的 SKILL.md，检查降级与提示

---

## 10. 实现顺序建议

1. `config` 加载 + `action.py`（槽位扫描、默认提示词、bind）
2. `context.py` + 模型调用封装（openai SDK，流式）
3. `loop.py` 状态机 + `render.py` 渲染
4. `main.py` REPL + 指令集 + `--bind` + `--smoke`
5. README + 冒烟测试通过 + 人工实测

---

## 11. 相关调研（Prior Art）

> 调研日期：2026-09-14。结论：三个核心特征各有成熟先例，但本设计的组合方式未查到现成项目。

| 本设计特征 | 互联网先例 | 一致度 |
|---|---|---|
| 显式 ReAct 文本协议（轨迹可见、人工可纠偏） | ReAct 论文（Yao et al., 2022, arXiv 2210.03629） | ✅ 同源 |
| skill 绑定到推理阶段槽位 | MetaGPT（70.4k★）`Code = SOP(Team)`，SOP 阶段化角色；但绑定在代码内、非目录约定、多 agent | ⚠️ 概念相近，机制不同 |
| 目录约定加载 SKILL.md | Agent Skills 开放标准（agentskills.io，Anthropic 发起）：40+ 产品采纳（OpenCode、Claude Code、Gemini CLI、Cursor、Copilot 等） | ✅ 行业标准，本格式天然兼容 |

关键发现：

1. **显式文本 ReAct 已被原生 function calling 取代**（LangChain `create_agent`、smolagents、OpenAI Agents SDK 均不再用文本协议）。坚持显式协议是差异化选择，代价是解析脆弱 + token 开销，由 max_rounds 与强制重规划护栏对冲。
2. **最接近的产品先例：Autohand Code CLI**（agentskills.io 客户列表），明确采用 "ReAct (Reason + Act) pattern … with your approval"，与本设计"显式协议 + 人工在环"思路高度重合，可参考其交互设计。
3. **本设计的创新组合点**："认知阶段作为槽位 × 目录约定绑定 skill"（阶段即插槽）未查到先例。经典 ReAct 动作空间开放，本设计为固定状态机 + 可插拔阶段提示。
4. **启示**：可借鉴 Agent Skills 标准的 progressive disclosure（启动只加载 description，触发时才读全文）作为后续优化方向；Fabric（44k★）证明目录约定 + 单发执行的极简路线存在，但无推理循环，与本设计定位不同。

---

## 附录 A：内置默认系统提示（摘要）

每个槽位的默认提示保证开箱可用，内容原则：

- **think**：分析当前任务与历史轨迹，决定下一步动作（plan / act / 完成 / verify），输出 `[THOUGHT]`，说明现状、决策、理由
- **plan**：把任务拆成编号步骤列表 `[PLAN]`，每步一句话、可独立执行
- **act**：产出当前步骤的完整产物 `[RESULT]`，宁可详尽不可残缺
- **observe**：核对上一步产物 `[OBSERVATION]`，结论必须是 通过 / 缺陷 / 重试 三选一并说明
- **verify**：对照任务最初目标做终检 `[VERIFY]`，输出 通过 / 不通过 + 依据

（完整默认提示词在实现时写入 `action.py` 常量。）
