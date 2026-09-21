"""ACT 真实执行器：把 ACT 产出的 [EXEC] 请求落到真实环境（shell / 写文件）。

设计原则（安全默认）：
- **默认关闭**：enable_shell_exec / enable_file_write 均为 False 时一律拒绝，不越权。
- **工作目录受限**：文件写入必须落在 cwd 之内，越界拒绝。
- **超时兜底**：shell 执行有超时，避免挂死。
- 执行回显交回 OBSERVE 作为观察对象，ACT 仍是"产出意图"，执行由框架代劳。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from react import win32_sandbox

_OUTPUT_LIMIT = 4000  # 回显截断长度，避免超长输出灌爆上下文（win32_sandbox 从此处复用）


class Executor(Protocol):
    """执行器协议：给定类型与载荷，返回可读的执行回显文本。"""

    def run(self, kind: str, payload: str) -> str: ...


@dataclass
class LocalExecutor:
    """本机执行器。shell=跑命令，write=写文件；两项分别受开关保护，默认全关。

    shell 可选套 OS 级沙箱（仅 Windows）：受限令牌（剥特权）+ 作业对象
    （kill-on-close / 禁 breakaway / 进程数上限 / 内存上限）+ 可选低完整性级别。
    见 react/win32_sandbox.py。
    """

    cwd: Path
    allow_shell: bool = False
    allow_file_write: bool = False
    timeout_sec: int = 30
    sandbox: bool = False           # shell 是否走 OS 级沙箱（仅 Windows 生效）
    low_integrity: bool = False     # 沙箱内是否降为低完整性级别（需把 cwd 降 IL，默认关）
    shell_backend: str = "cmd"      # cmd / bash / auto
    bash_path: str = ""             # 探测到的 bash.exe 路径（auto 时填充）

    def __post_init__(self):
        if self.shell_backend == "auto":
            self.bash_path = _detect_bash()
            self.shell_backend = "bash" if self.bash_path else "cmd"

    def run(self, kind: str, payload: str) -> str:
        if kind == "shell":
            return self._shell(payload)
        if kind == "write":
            return self._write(payload)
        return f"（未知执行类型：{kind}）"

    # ------------------------------------------------------------------

    def _shell(self, cmd: str) -> str:
        if not self.allow_shell:
            return "（已拒绝：shell 执行未启用，请在 config 打开 enable_shell_exec）"
        if self.sandbox and win32_sandbox.sandbox_available():
            try:
                return win32_sandbox.run_sandboxed(
                    cmd, str(self.cwd), self.timeout_sec, self.low_integrity,
                    backend=self.shell_backend, bash_path=self.bash_path)
            except Exception as e:  # noqa: BLE001
                # 沙箱异常不致命：告警并安全回退到普通执行，避免一次沙箱故障拖垮整个 agent
                return (f"（沙箱执行异常，已回退普通执行：{e}）\n"
                        + self._shell_raw(cmd))
        return self._shell_raw(cmd)

    def _shell_raw(self, cmd: str) -> str:
        """普通 shell 执行（无沙箱），作为默认与沙箱回退路径。"""
        try:
            if self.shell_backend == "bash" and self.bash_path:
                proc = subprocess.run(
                    [self.bash_path, "-c", cmd], cwd=str(self.cwd),
                    timeout=self.timeout_sec, capture_output=True,
                    text=True, encoding="utf-8", errors="replace")
            else:
                proc = subprocess.run(
                    cmd, shell=True, cwd=str(self.cwd), timeout=self.timeout_sec,
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return f"（执行超时：超过 {self.timeout_sec}s 未结束）"
        except Exception as e:  # noqa: BLE001
            return f"（执行异常：{e}）"
        out = proc.stdout or ""
        if proc.stderr:
            out += f"\n[stderr]\n{proc.stderr}"
        return f"exit={proc.returncode}\n{out.strip()[:_OUTPUT_LIMIT]}"

    def _write(self, payload: str) -> str:
        if not self.allow_file_write:
            return "（已拒绝：文件写入未启用，请在 config 打开 enable_file_write）"
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return '（写入失败：载荷不是合法 JSON，需形如 {"path": "...", "content": "..."}）'
        if not isinstance(data, dict) or not data.get("path"):
            return "（写入失败：缺少 path 字段）"
        content = str(data.get("content", ""))

        root = self.cwd.resolve()
        target = (root / str(data["path"])).resolve()
        try:
            target.relative_to(root)  # 必须落在工作目录内
        except ValueError:
            return f"（已拒绝：目标路径越出工作目录 {root}）"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as e:
            return f"（写入异常：{e}）"
        return f"已写入 {target}（{len(content)} 字符）"


def _detect_bash() -> str:
    """探测 Git Bash 路径。找不到返回空串。"""
    for candidate in (
        shutil.which("bash"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return ""
