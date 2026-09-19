"""react-agent 自测包（纯 Python，零新增依赖）。

此前全部断言塞在 main.py 的 `cmd_smoke`（221 行）里，与 CLI 职责混杂；
现按主题拆分到本包，`--smoke` 只做「调度 + 汇总」，对外 UX 与输出保持不变。

单独运行：python -m tests.run_all
"""
