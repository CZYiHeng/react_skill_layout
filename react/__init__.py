"""react-agent 核心包。

`CAPABILITY_API` 是**能力格式**的兼容版本，与产品版本（pyproject 的 `version`）分开：

- 能力用 `capability.json` 的 `requires: {"react_agent": ">=x.y"}` 声明它需要的能力格式；
- 框架用它做单向区间校验（见 `react/capability.py: check_requires`）。

为什么不复用产品版本：产品加个 UI 功能不该让所有能力"变得不兼容"；而能力格式
一旦变（例如阶段集合变更、manifest 字段重命名），旧能力确实需要重新适配。
把两者绑在一起，要么校验形同虚设，要么每次发版都误报不兼容。
"""

from __future__ import annotations

#: 能力格式兼容版本：新增**可选**字段不升；阶段集合变化或必需字段变化才升。
CAPABILITY_API = "0.1.0"
