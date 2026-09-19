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
- **双通道控制信号**：原生工具调用为主、正则文本解析兜底；歧义时自我修正最多 2 次，随后走安全默认。

## 快速开始

```bash
# 1. 安装依赖（uv 管理，自动创建 .venv 并锁定版本）
uv sync

# 2. 配置模型 key（推荐环境变量，避免明文落盘）
export REACT_AGENT_API_KEY="sk-xxx"
# 或：cp config.example.json config.json 后在文件里填 api_key（config.json 已被 .gitignore 排除）

# 3. 启动 REPL
uv run python main.py
```

> 支持任何 **OpenAI 兼容端点**（deepseek / kimi / Moonshot / 通义 / GLM 等），`base_url` + `api_key` + `model` 三件套即可。已内置 kimi 思考模式适配（reasoning 在 `reasoning_content`，思考模式仅支持 `tool_choice="auto"`）。

## 使用示例

```
react> 帮我设计一个 Excel 转 Markdown 的脚本方案
◆ THINK · 1.2s · 843tok      ← 分析现状，决策下一步
▶ ACT · 2.4s · 1.9ktok       ← 产出方案（标注"请人工取用"）
◈ 方案确认 · 为什么这么做     ← 有选择空间时自证：目的/约束/方案/预期/对比
● OBSERVE · 0.8s · 550tok    ← 核对产物
[c]继续 · s <纠偏> · q 中止   ← 需要时才拦你
```

**人工闸门档位**（`gate_mode`，可用 `REACT_AGENT_GATE_MODE` 环境变量覆盖）：

| 档位 | 何时拦你 | 6 步任务点击数 |
|---|---|---|
| `plan`（默认） | 计划出来确认一次方向；之后只在缺陷或验收时 | ≈ 2 |
| `step` | 每个步骤完成后 + 最终验收 | ≈ 7 |
| `auto` | 连计划也不拦，只在缺陷或验收时 | ≈ 1 |
| `phase` | 每个阶段都拦（旧行为回滚开关） | ≈ 18–20 |

**想主动介入不用等闸门**：运行时点「暂停」，循环在下一个阶段边界停下（可继续 / 纠偏 / 中止）；也可以直接下发纠偏，不打断执行、从下一步生效。

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
└──────────────┬────────────────────┘
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
- 把 `~/.claude/skills/` 里的 skill 绑到槽位：`python main.py --bind plan dev-flow`，或直接复制目录到 `skills/plan/`。
- 仓库附带一套**写代码专用档案** `skills_code/`（8 字段函数头、req-to-code 增量骨架、solution-review 三维度等），用 `--skills-dir` 或 `REACT_AGENT_SKILLS_DIR` 切换——框架绑定机制不变，换的只是"写代码行为"。

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
{ "enable_shell_exec": true, "enable_file_write": true, "exec_timeout_sec": 30 }
```

- `shell`：在 `cwd` 内运行命令；`write`：写入文件，目标必须落在 `cwd` 内（越界拒绝）
- 未开启时 `[EXEC]` 仅作文本产物呈现，agent 不会触碰环境

**Windows OS 级沙箱**（可选，对标 Codex CLI / Chrome 沙箱）：`sandbox_shell: true` 时给 shell 子进程套内核级隔离——受限令牌剥特权 + 作业对象（`KILL_ON_JOB_CLOSE` 防孤儿进程、`ActiveProcessLimit` 防 fork 炸弹）。纯 ctypes 零依赖，非 Windows 自动回退普通执行。

## 验证

```bash
uv run python main.py --smoke         # Mock 模型，零 API 消耗：断言循环结构/解析/执行/窗口化
uv run python main.py --smoke-live    # 真实 kimi API：断言循环终止 + 原生工具调用兼容性
python -m tests.run_all               # 独立测试入口
```

## 项目结构

```
react-agent/
├── main.py                  # CLI 入口（REPL + --bind/--smoke/--smoke-live）
├── mcp_server.py            # MCP server
├── config*.example.json     # 配置模板（真实 config*.json 被 .gitignore 排除）
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
├── skills/                  # 5 槽位默认 skill
├── skills_code/             # 写代码专用 skill 档案
├── web/                     # React + Vite 前端
├── tests/                   # 冒烟测试
├── docs/                    # DESIGN.md 等设计文档
└── start.bat                # Windows 一键启动
```

## 文档

- [docs/DESIGN.md](docs/DESIGN.md) —— 完整设计文档（状态机、协议、安全模型）
- [docs/demo-brief.md](docs/demo-brief.md) —— 演示说明
- [docs/LEDGER_FIX_PLAN.md](docs/LEDGER_FIX_PLAN.md) —— 消息账本修复计划

## 依赖

- Python ≥ 3.10 + [uv](https://docs.astral.sh/uv/)
- 运行时依赖仅 3 个：`rich`（终端渲染）、`openai`（模型 API）、`markdown-it-py`（产物 Markdown 渲染，带零依赖兜底渲染器）
- 核心循环本身零第三方依赖（纯标准库）；前端 React + Vite

## License

MIT（按需替换为你选择的许可证；本仓库 LICENSE 文件由你在发布前确认添加）。
