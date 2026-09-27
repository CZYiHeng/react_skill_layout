# ReAct Agent（react-agent）

> 显式 ReAct 协议的开源 Agent 框架：推理框架（THINK → PLAN → ACT → OBSERVE → VERIFY 状态机）是框架的，skill 通过**目录约定**绑定到认知阶段槽位——随时换文件即换行为。一套核心循环驱动 **CLI / Web / MCP** 三种形态。

[![Python >= 3.10](https://img.shields.io/badge/python-%3E%3D3.10-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)]()
[![Dependencies](https://img.shields.io/badge/deps-stdlib%2B3-green)]()
[![Agent Skills](https://img.shields.io/badge/Agent%20Skills-compatible-8A2BE2)](https://agentskills.io)

## 特性

- **显式五阶段状态机**：THINK → PLAN → ACT → OBSERVE → VERIFY，模型每轮必须通过 `decide_next_step` 给出六值决策（PLAN / ACT / DONE / VERIFY / ASK / ESCALATE），解析失败默认移交人工（fail-safe），绝不盲目推进。
- **人工闸门 `gate_mode`**：`plan` / `step` / `auto` / `phase` 四档控制被拦频率；「模型提问 / 判定缺陷 / 最终验收」在任何档位下都必拦。
- **Skill 槽位化**：`skills/<槽位>/SKILL.md` 换文件即换行为，遵循 [Agent Skills 开放标准](https://agentskills.io)（frontmatter + 正文）。
- **三入口同一核心**：终端 REPL（rich 分色面板）、Web 对话（React + SSE）、MCP 工具（WorkBuddy 自动化）。
- **安全执行**：`[EXEC: shell|write]` 默认关闭；工作目录越界拒绝；Windows 提供 OS 级沙箱（受限令牌 + 作业对象，纯 ctypes 零依赖）。
- **上下文窗口化**：消息窗口 + 历史摘要，长任务不爆上下文。
- **缓存友好的前缀（省 token）**：发给模型的消息前缀**只追加、不回改**——稳定 system、冻结的历史摘要、历史原文一律按序追加，利用率最高的开头部分始终能被 provider 的前缀缓存命中（DeepSeek 自动硬盘缓存 / Kimi context caching）。`max_context_tokens` 是压缩触发阈值（按实测 usage 预估下一次请求的压力），调大则命中率更高、单次请求更大；实测离线探针（`tools/cache_probe.py`）纯追加负载逐字复用率 **12% → 78%**。
- **双通道控制信号**：原生工具调用为主、正则文本解析兜底；歧义时自我修正最多 2 次，随后走安全默认。

## 快速开始

```bash
# 1. 安装依赖（uv 管理，自动创建 .venv 并锁定版本）
uv sync

# 2. 配置模型接入：复制模板后在 providers 里填 key（config.json 已被 .gitignore 排除）
cp config.example.json config.json
#    多家端点并存看 config.providers.example.json（用 active_provider 选生效的那家）

# 3. 启动 REPL
uv run python main.py
```

> 支持任何 **OpenAI 兼容端点**（deepseek / kimi / Moonshot / 通义 / GLM 等），一家一个
> `providers` 条目（`base_url` + `api_key` + `model`），用 `active_provider` 指定生效的那家，
> Web 端可在设置里随时切换。**key 只从配置文件读**（不读环境变量），详见
> [docs/DESIGN.md](docs/DESIGN.md) §5。

## 使用示例

```
react> 帮我设计一个 Excel 转 Markdown 的脚本方案
◆ THINK · 1.2s · 843tok      ← 分析现状，决策下一步
▶ ACT · 2.4s · 1.9ktok       ← 产出方案（标注"请人工取用"）
◈ 方案确认 · 为什么这么做     ← 有选择空间时自证：目的/约束/方案/预期/对比
● OBSERVE · 0.8s · 550tok    ← 核对产物
[c]继续 · s <纠偏> · q 中止   ← 需要时才拦你
```

**人工闸门档位**（`gate_mode`，写在配置文件里）：

| 档位 | 何时拦你 | 6 步任务点击数 |
|---|---|---|
| `auto`（默认） | 只在缺陷或最终验收时——闸门回到「需要人决策」的本意 | ≈ 1–2 |
| `plan` | 计划产出后额外确认一次方向，之后同上 | ≈ 2–3 |
| `step` | 每个步骤收尾 + 最终验收 | ≈ 7 |
| `phase` | 每个阶段都拦（旧行为回滚开关） | ≈ 18–20 |

> 为什么默认 `auto` 而不是 `plan`：闸门的目的是「在需要人决定时介入」，`plan` 档却让**每个任务**
> 都无条件停一次——问答、只读、批处理类任务被无差别打断，人一慢就卡在那儿。而「计划跑偏」
> 这件事框架已有独立兜底（连续 3 次未通过强制重出计划、`max_rounds`、判定歧义默认不通过/移交），
> 不靠人肉审批也不掉质量。长任务想强制先看计划再开跑，把它显式设为 `plan` 即可。

**想主动介入不用等闸门**：运行时点「暂停」，循环在下一个阶段边界停下（可继续 / 纠偏 / 中止）；也可以直接下发纠偏，不打断执行、从下一步生效。

## 双模型（计划 / 执行分离）

默认所有阶段共用同一个 `model`。若你希望**计划阶段用推理模型、其余阶段用快模型**（更合理也更省 token），可单独配置 `plan_model`：

```json
{
  "model": "deepseek-chat",          // 执行 / 思考 / 观察 / 验证：快而稳的 chat
  "plan_model": "deepseek-reasoner", // 仅 PLAN 阶段：慢但深的推理模型
  "plan_timeout_sec": 300            // 计划模型超时（推理模型更慢，默认 300，可改）
}
```

- `plan_model` 留空（`""`）或不配置 → 计划阶段自动回落到 `model`，行为与旧版一致。
- 配置后，循环仅在 `action_name == "plan"` 时调用 `plan_model` 客户端，其余（`think` / `act` / `observe` / `verify`）一律用 `model`。
- 推理模型的 `reasoning_content` 仅用于流式展示（`show_reasoning`），**不会进入上下文账本**，因此不会污染消息窗口、不会撑大上下文。
- 计划模型同样写在配置文件里（`plan_model`），没有对应的环境变量。

## 工作原理

```
┌────────────── 入口层 ──────────────┐
│  main.py (CLI) · webapi.py (Web)  │
│  mcp_server.py (MCP)              │
└──────────────┬────────────────────┘
┌──────────────▼─────── 服务层 ───────┐
│  ReactService：统一构造 runtime     │
│  Renderer×3 (Rich/Event/Null)      │
│  Control×3 (Cli/Queue/Auto)        │
└──────────────┬─────────────────────┘
┌──────────────▼─────── 核心层 ───────┐
│  loop.py      五阶段状态机         │
│  action.py    槽位注册+SKILL.md 解析│
│  model.py     OpenAI 兼容客户端    │
│  context.py   消息账本/窗口化       │
│  executor.py  安全执行器           │
│  win32_sandbox.py  Windows 沙箱    │
└────────────────────────────────────┘
```

- **双通道控制信号**：模型先走原生工具调用（`decide_next_step` / `submit_verdict`，结构化参数抗格式漂移），正则文本解析兜底。
- **fail-safe**：决策解析不到 → 默认 ESCALATE（移交人工）；判定歧义 → 默认不通过；绝不静默乐观通过。
- **防打转**：ASK 独立上限 6 次；OBSERVE 连续 3 次非通过 → 强制重出 PLAN；验收不通过 → 反馈回炉，不得直接 DONE。

## Skill 体系

5 个认知阶段槽位：`think` / `plan` / `act` / `observe` / `verify`，各绑一个 `SKILL.md`：

```
skills/think/SKILL.md      ← THINK：怎么决策下一步（六值）
skills/plan/SKILL.md       ← PLAN：怎么拆步骤（带可核对完成标准）
skills/act/SKILL.md        ← ACT：怎么产出产物（[RESULT]/[方案]/[EXEC]）
skills/observe/SKILL.md    ← OBSERVE：怎么核对（pass/defect/retry）
skills/verify/SKILL.md     ← VERIFY：怎么终检（pass/fail）
```

- 槽位目录为空时回退内置默认提示词。
- **默认绑定的是仓库自带的 `skills/`**（通用推理/分析档）；`skills_code/` 是**并列的另一套**，
  **不设 `--skills-dir` 就不会被加载**——这点最容易误解。
- 把 `~/.claude/skills/` 里的 skill 绑到槽位：`python main.py --bind plan dev-flow`，或直接复制目录到 `skills/plan/`。
- 仓库附带一套**写代码专用档案** `skills_code/`（8 字段函数头、req-to-code 增量骨架、solution-review 三维度等），用 `--skills-dir skills_code` 或 `REACT_AGENT_SKILLS_DIR` 切换——框架绑定机制不变，换的只是"写代码行为"。
- 查当前到底绑了什么：`python main.py --check`（打印 skill 根目录与五个槽位各自的变体）。
- 槽位内还能放多个 skill（变体）：`skills/<槽位>/<变体名>/SKILL.md` + 配置 `skill_variants` 选用，只影响该槽位。

## Web 对话前端

React + Vite 网页界面（五槽位分色面板、流式吐字、步进控制、ASK 回答、方案卡片折叠展示）。

**Windows 一键启动**：双击 `start.bat`（自动校验 Python/配置、缺 `web\dist` 时自动构建前端、拉起后端并打开浏览器）。

```bat
start.bat           :: 启动 Web 对话界面（后端 8000 同时托管前端）
start.bat dev       :: 开发模式：后端 8000 + Vite 热更新 5173
start.bat cli       :: 命令行 REPL
start.bat build     :: 仅构建前端产物 web\dist
start.bat stop      :: 停止后台服务
```

手动启动：

```bash
# 后端（starlette + SSE，监听 8000）
uv run python -m react.webapi

# 前端开发服务器（5173，/api 代理到 8000）
cd web && npm install && npm run dev
```

## 作为 MCP 工具（WorkBuddy 等）

```bash
uv run python mcp_server.py   # stdio MCP server
```

在 MCP 客户端注册（示例，路径换成你的安装目录）：

```json
{
  "mcpServers": {
    "react-agent": {
      "command": "<项目绝对路径>/.venv/Scripts/python.exe",
      "args": ["<项目绝对路径>/mcp_server.py"]
    }
  }
}
```

工具 `run_react_agent(task, skills_dir?, max_rounds=10, allow_exec=False)` 返回完整轨迹 JSON（`status` / `rounds` / `final_text` / `messages` 账本）。MCP 模式下 ACT 真实执行默认关闭（`allow_exec=False`），stdout 静默，仅走 stdio JSON-RPC。

## 执行能力与安全（默认关闭）

ACT 产物可附带 `[EXEC: shell|write]` 块请求本地执行。出于安全默认，需显式开启：

```json
{ "enable_shell_exec": true, "enable_file_write": true }
```

> 只需打开开关：`exec_timeout_sec`（默认 120 秒）与 `shell_backend`（默认 `auto`，
> 探测 Git Bash、找不到回退 `cmd`）的默认值已按"执行真能干活"设定，不必手填。
> 若显式写成保守值（超时 < 60 或 `cmd`），启动会告警提示。

- `shell`：在 `cwd` 内运行命令；`write`：写入文件，目标必须落在 `cwd` 内（越界拒绝）
- 未开启时 `[EXEC]` 仅作文本产物呈现，agent 不会触碰环境

**Windows OS 级沙箱**（可选，对标 Codex CLI / Chrome 沙箱）：`sandbox_shell: true` 时给 shell 子进程套内核级隔离——受限令牌剥特权 + 作业对象（`KILL_ON_JOB_CLOSE` 防孤儿进程、`ActiveProcessLimit` 防 fork 炸弹）。纯 ctypes 零依赖，非 Windows 自动回退普通执行。

## 验证

```bash
uv run python main.py --smoke         # Mock 模型，零 API 消耗：断言循环结构/解析/执行/窗口化
uv run python main.py --smoke-live    # 真实 kimi API：断言循环终止 + 原生工具调用兼容性
python -m tests.run_all               # 独立测试入口
uv run python tools/cache_probe.py    # 离线缓存探针：改造前/后逐字复用率 A/B 对比
```

## 已知边界

这些是**有意为之的设计取舍**，不是待修 bug——写在这里是为了让你在踩到之前就知道：

- **每个任务都是全新会话（不是连续对话）**。`ReActLoop.run()` 第一行就 `context.reset()`，
  所以 Web 里输入「再改一下」= 全新任务、零上下文，尽管它紧跟在上一轮结果下面。
  需要接续上文时：Web 端在请求里带 `continue_session: true`，CLI 用 `/continue` 开关
  （提示符会变成 `react(接续)>`）——开启后下个任务会带上**上一个任务的结论摘要**
  （≤约 400 token，不重放账本）。
- **SSE 断线期间的事件不补发**。后端事件队列取走即移除，前端重连后不补放断线期间的
  事件（[web/src/api.js](web/src/api.js) 有说明）。若断线时恰好错过 `done`，界面会停在
  `running`——刷新页面即可（会走 `/api/state` 重建）。
- **默认闸门档位是 `auto`**：只在 OBSERVE 判缺陷/不通过、最终验收时拦你，计划不再无条件打断。
  需要「先看计划再开跑」就把 `gate_mode` 设为 `plan`（Web 端在侧栏或设置里切换，CLI 走配置或
  配置文件里的 `gate_mode`）。注意 `auto` 档下 Web 端闸门弹出后仍有 30s 倒计时自动放行。
- **`[EXEC:]` 默认不执行**：`enable_shell_exec` / `enable_file_write` 默认关闭，
  开启后 agent 才能真正跑命令、写文件（写盘范围受 `work_dir` 约束）。
- **压缩看 token 预算，不看条数**：`max_context_tokens`（默认 100000）是压缩阈值。
  每次调用后用 provider 的实测 usage 校准"下一次请求大概多少 token"，超阈值才压缩一次。
  早先的 `max_context_messages`（条数预算）**已删除**：它与 token 预算并列摆放，容易被
  当成"上下文大小"旋钮一直留着（12 条会让压缩频繁触发、把前缀缓存反复打断）。
  条数只剩一个内部护栏（400 条）防病态长尾。
  **判据：看 `/api/token_stats` 的两个数**——`cache_hit_rate` 长期低于 40% 说明压缩
  太频繁（该上调阈值）；而单次 prompt 接近或撞上阈值、`prompt` 总量居高不下，说明
  阈值偏大（该下调）：每次调用都要重发全部历史，单次上限越大，二次增长的代价越高。
  默认 100k 就是被实测修正过的：一度调到 200k，结果长任务单次 prompt 涨到 199,371、
  10 轮累计 10.87M。窗口更小的端点按「窗口 × 0.6」下调（128k 窗口 → 约 76000）。
- **槽位内可放多个 skill（变体）**：`skills/<槽位>/SKILL.md` 是该槽位默认行为，
  `skills/<槽位>/<变体名>/SKILL.md` 是可独立抽出的额外 skill；用配置
  `"skill_variants": {"act": "strict-code"}` 选用其一，**只影响该槽位**，五个槽位各自独立。
  每步只注入当前槽位那一份，所以加变体不增加单次请求 token。
  未知变体名会降级为默认并告警（不阻断启动）。注意 `--bind` 是**整目录替换**，
  会连带清掉该槽位下的变体。
- **Windows 沙箱不阻断网络**，且子进程仍以当前用户身份运行（详见
  [docs/DESIGN.md](docs/DESIGN.md) 安全模型一节）。

## 项目结构

```
react-agent/
├── main.py                  # CLI 入口（REPL + --bind/--smoke/--smoke-live）
├── mcp_server.py            # MCP server
├── config.example.json      # 配置模板（单家接入）
├── config.providers.example.json  # 配置模板（多家接入 + active_provider）
├── react/                   # 核心包
│   ├── loop.py              # ReActLoop 五阶段状态机
│   ├── model.py             # OpenAI 兼容客户端 + Mock
│   ├── action.py            # 5 槽位注册 + SKILL.md 解析
│   ├── context.py           # 消息账本 / 计划状态 / 窗口化
│   ├── executor.py          # shell/write 安全执行器
│   ├── service.py           # 服务层：Renderer×3 / Control×3
│   ├── render.py            # Renderer 协议 + rich 渲染
│   ├── display.py           # 产物展示模板
│   ├── webapi.py            # Web API（SSE + REST）
│   └── win32_sandbox.py     # Windows 沙箱（纯 ctypes）
├── skills/                  # 5 槽位默认 skill（槽位内可再放变体子目录）
│   └── act/SKILL.md         #   默认；act/<变体名>/SKILL.md = 可抽出的额外 skill
├── skills_code/             # 写代码专用 skill 档案（同上结构）
├── web/                     # React + Vite 前端
├── tests/                   # 冒烟测试
├── tools/                   # 离线工具（cache_probe.py 缓存前缀 A/B 探针）
├── docs/                    # DESIGN.md 等设计文档
└── start.bat                # Windows 一键启动
```

## 文档

- [docs/DESIGN.md](docs/DESIGN.md) —— 完整设计文档（状态机、协议、安全模型）
- [docs/demo-brief.md](docs/demo-brief.md) —— 演示说明

## 依赖

- Python ≥ 3.10 + [uv](https://docs.astral.sh/uv/)
- 运行时依赖仅 3 个：`rich`（终端渲染）、`openai`（模型 API）、`markdown-it-py`（产物 Markdown 渲染，带零依赖兜底渲染器）
- 核心循环本身零第三方依赖（纯标准库）；前端 React + Vite

## License

MIT（按需替换为你选择的许可证；本仓库 LICENSE 文件由你在发布前确认添加）。
