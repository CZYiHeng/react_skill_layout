"""ACT 真实执行器：把 ACT 的执行请求落到真实环境（shell / 文件操作）。

双入口（对标 Claude Code 的原生工具调用 + 文本协议兜底）：
- **run_tool(name, args)**：原生工具调用入口（模型直接调 read/write/edit/grep/glob/shell 工具）。
- **run(kind, payload)**：文本协议入口（[EXEC: ...] 块，kimi 等工具不稳模型的兜底），解析后转 args 调核心方法。

设计原则（安全默认）：
- **默认关闭**：enable_shell_exec / enable_file_write 均为 False 时一律拒绝，不越权。
- **工作目录受限**：文件读写/搜索必须落在 cwd 之内，越界拒绝（allow_outside 时放行绝对路径）。
- **超时兜底**：shell 执行有超时，避免挂死。
- **read 默认行数上限**（对齐 Claude Code）：默认 2000 行 + 单行截断 2000 字符，超限提示翻页。
- 执行回显交回 OBSERVE 作为观察对象，ACT 仍是"产出意图"，执行由框架代劳。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from react import win32_sandbox

_OUTPUT_LIMIT = 4000  # 回显截断长度，避免超长输出灌爆上下文（win32_sandbox 从此处复用）
_READ_DEFAULT_LINES = 2000   # read 默认最大行数（对齐 Claude Code）
_READ_MAX_LINE_CHARS = 2000  # read 单行截断长度（对齐 Claude Code）


class Executor(Protocol):
    """执行器协议：原生工具调用 + 文本协议两种入口，均返回可读执行回显。"""

    def run_tool(self, name: str, args: dict) -> str: ...
    def run(self, kind: str, payload: str) -> str: ...


@dataclass
class LocalExecutor:
    """本机执行器。shell=跑命令；read/write/edit/grep/glob=独立文件操作工具。

    shell 可选套 OS 级沙箱（仅 Windows）：受限令牌（剥特权）+ 作业对象
    （kill-on-close / 禁 breakaway / 进程数上限 / 内存上限）+ 可选低完整性级别。
    见 react/win32_sandbox.py。
    """

    cwd: Path
    allow_shell: bool = False
    allow_file_write: bool = False
    allow_outside: bool = False
    timeout_sec: int = 30
    sandbox: bool = False           # shell 是否走 OS 级沙箱（仅 Windows 生效）
    low_integrity: bool = False     # 沙箱内是否降为低完整性级别（需把 cwd 降 IL，默认关）
    shell_backend: str = "cmd"      # cmd / bash / auto
    bash_path: str = ""             # 探测到的 bash.exe 路径（auto 时填充）

    def __post_init__(self):
        if self.shell_backend == "auto":
            self.bash_path = _detect_bash()
            self.shell_backend = "bash" if self.bash_path else "cmd"

    # ------------------------------------------------------------------
    # 原生工具调用入口（主通道，对标 Claude Code）
    # ------------------------------------------------------------------

    def run_tool(self, name: str, args: dict) -> str:
        """按工具名 + 参数 dict 执行（模型原生工具调用）。"""
        args = args or {}
        if name == "shell":
            return self._shell(str(args.get("command", "")).strip())
        if name == "read":
            return self._read(args)
        if name == "write":
            return self._write(args)
        if name == "edit":
            return self._edit(args)
        if name == "grep":
            return self._grep(args)
        if name == "glob":
            return self._glob(args)
        return f"（未知工具：{name}）"

    # ------------------------------------------------------------------
    # 文本协议入口（兜底，kimi 等模型工具调用缺失时可用）
    # ------------------------------------------------------------------

    def run(self, kind: str, payload: str) -> str:
        if kind == "shell":
            return self._shell(payload)
        if kind == "read":
            return self._read(_parse_kv_payload(payload))
        if kind == "grep":
            return self._grep(_parse_kv_payload(payload))
        if kind == "glob":
            return self._glob(_parse_kv_payload(payload))
        if kind == "write":
            parsed = _parse_write_payload(payload)
            if parsed:
                return self._write({"path": parsed[0], "content": parsed[1]})
            # 回退旧 JSON 格式 {"path": ..., "content": ...}
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                return ('（写入失败：载荷格式无法识别。新格式：path: 相对路径 + '
                        '---BEGIN---/---END--- 围栏；旧格式：{"path": "...", "content": "..."}）')
            if not isinstance(data, dict) or not data.get("path"):
                return "（写入失败：缺少 path 字段）"
            return self._write({"path": str(data["path"]),
                                "content": str(data.get("content", ""))})
        if kind == "edit":
            parsed = _parse_edit_payload(payload)
            if not parsed:
                return ("（编辑失败：载荷格式应为 path: 行 + "
                        "---OLD---/---NEW---/---END--- 围栏）")
            return self._edit({"path": parsed[0], "old_text": parsed[1],
                               "new_text": parsed[2]})
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

    def _resolve_path(self, path: str):
        """解析路径并做越界检查。越界返回 None。"""
        root = self.cwd.resolve()
        target = (root / path).resolve()
        if not self.allow_outside:
            try:
                target.relative_to(root)
            except ValueError:
                return None
        return target

    def _read(self, args: dict) -> str:
        """读文件，带行号输出（cat -n 风格）。

        对齐 Claude Code：默认最多 2000 行、单行截断 2000 字符，
        支持 offset/limit 翻页。超限提示用 offset 继续读。
        """
        path = str(args.get("path", "")).strip()
        if not path:
            return "（读取失败：缺少 path 参数）"
        target = self._resolve_path(path)
        if target is None:
            return f"（已拒绝：路径越出工作目录 {self.cwd.resolve()}）"
        if not target.is_file():
            return f"（读取失败：文件不存在 {target}）"
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").split("\n")
        except OSError as e:
            return f"（读取异常：{e}）"

        total = len(lines)
        offset_raw = args.get("offset")
        offset = (max(1, int(offset_raw)) - 1) if offset_raw not in (None, "") else 0
        limit_raw = args.get("limit")
        limit = int(limit_raw) if limit_raw not in (None, "") else _READ_DEFAULT_LINES
        if limit <= 0:
            limit = _READ_DEFAULT_LINES
        # 对齐 Claude：即使显式 limit 也压到单次上限，超长提示翻页
        limit = min(limit, _READ_DEFAULT_LINES)
        segment = lines[offset:offset + limit]
        width = max(1, len(str(offset + len(segment))))
        out = []
        for i, line in enumerate(segment, start=offset + 1):
            out.append(f"{i:>{width}}\t{line[:_READ_MAX_LINE_CHARS]}")
        header = f"== {target} ({total} 行"
        if offset > 0 or limit < total:
            header += f"，显示 {offset + 1}-{offset + len(segment)}"
        truncated = any(len(line) > _READ_MAX_LINE_CHARS for line in segment)
        header += "） =="
        body = "\n".join(out)
        notes = []
        if offset + len(segment) < total:
            notes.append(f"（还有 {total - offset - len(segment)} 行未读，"
                         f"用 offset={offset + len(segment) + 1} 继续）")
        if truncated:
            notes.append(f"（部分行超 {_READ_MAX_LINE_CHARS} 字符已截断）")
        return header + "\n" + body + ("\n" + "\n".join(notes) if notes else "")

    def _write(self, args: dict) -> str:
        if not self.allow_file_write:
            return "（已拒绝：文件写入未启用，请在 config 打开 enable_file_write）"
        path = str(args.get("path", "")).strip()
        content = str(args.get("content", ""))
        if not path:
            return "（写入失败：缺少 path 参数）"
        target = self._resolve_path(path)
        if target is None:
            return f"（已拒绝：路径越出工作目录 {self.cwd.resolve()}）"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as e:
            return f"（写入异常：{e}）"
        return f"已写入 {target}（{len(content)} 字符）"

    def _edit(self, args: dict) -> str:
        """精确字符串替换（old → new），类似 Claude Code str_replace。"""
        path = str(args.get("path", "")).strip()
        old_text = str(args.get("old_text", ""))
        new_text = str(args.get("new_text", ""))
        if not path:
            return "（编辑失败：缺少 path 参数）"
        if not old_text:
            return "（编辑失败：缺少 old_text 参数）"
        target = self._resolve_path(path)
        if target is None:
            return f"（已拒绝：路径越出工作目录 {self.cwd.resolve()}）"
        if not target.is_file():
            return f"（编辑失败：文件不存在 {target}）"
        if not self.allow_file_write:
            return "（已拒绝：文件写入未启用，请在 config 打开 enable_file_write）"
        try:
            content = target.read_text(encoding="utf-8")
        except OSError as e:
            return f"（读取异常：{e}）"
        count = content.count(old_text)
        if count == 0:
            return "（编辑失败：未找到匹配的旧文本）"
        if count > 1:
            return f"（编辑失败：旧文本匹配到 {count} 处，不唯一，请缩小匹配范围）"
        new_content = content.replace(old_text, new_text, 1)
        try:
            target.write_text(new_content, encoding="utf-8")
        except OSError as e:
            return f"（写入异常：{e}）"
        return f"已编辑 {target}（1 处替换，{len(old_text)}→{len(new_text)} 字符）"

    def _grep(self, args: dict) -> str:
        """正则搜索文件内容（Python re，排除常见目录）。"""
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            return "（搜索失败：缺少 pattern 参数）"
        search_path = str(args.get("path", "")).strip() or "."
        target = self._resolve_path(search_path)
        if target is None:
            return f"（已拒绝：路径越出工作目录 {self.cwd.resolve()}）"
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"（搜索失败：正则无效：{e}）"
        exclude_dirs = {".git", ".venv", "node_modules", "__pycache__", "dist", "build"}
        results = []
        if target.is_file():
            files = [target]
        else:
            files = [p for p in target.rglob("*") if p.is_file()
                     and not any(part in exclude_dirs for part in p.parts)]
        for fp in files:
            try:
                for i, line in enumerate(
                        fp.read_text(encoding="utf-8", errors="replace").split("\n"), 1):
                    if regex.search(line):
                        try:
                            rel = str(fp.relative_to(self.cwd.resolve()))
                        except ValueError:
                            rel = str(fp)
                        results.append(f"{rel}:{i}: {line[:200]}")
                        if len(results) >= 100:
                            break
            except (OSError, UnicodeDecodeError):
                continue
            if len(results) >= 100:
                break
        if not results:
            return f"（无匹配：pattern={pattern}）"
        suffix = "\n…（结果过多，仅显示前 100 条）" if len(results) >= 100 else ""
        return f"（{len(results)} 条匹配）\n" + "\n".join(results) + suffix

    def _glob(self, args: dict) -> str:
        """文件模式匹配（fnmatch，** 递归，排除常见目录）。"""
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            return "（匹配失败：缺少 pattern 参数）"
        root = self.cwd.resolve()
        exclude_dirs = {".git", ".venv", "node_modules", "__pycache__", "dist", "build"}
        results = []
        if "**" in pattern:
            suffix = pattern.split("**/")[-1] if "**/" in pattern else pattern.lstrip("*")
            for p in root.rglob(suffix):
                if p.is_file():
                    try:
                        rel = p.relative_to(root)
                        if not any(part in exclude_dirs for part in rel.parts):
                            results.append(str(rel))
                    except ValueError:
                        pass
        else:
            for p in root.glob(pattern):
                if p.is_file():
                    try:
                        rel = p.relative_to(root)
                        if not any(part in exclude_dirs for part in rel.parts):
                            results.append(str(rel))
                    except ValueError:
                        pass
        results.sort()
        if not results:
            return f"（无匹配：pattern={pattern}）"
        return f"（{len(results)} 个文件）\n" + "\n".join(results[:200])


def _parse_kv_payload(payload: str) -> dict:
    """解析 key: value 格式载荷（read/grep/glob 用），每行一个键值对。"""
    kv = {}
    for line in payload.split("\n"):
        s = line.strip()
        if ":" in s and not s.startswith("---"):
            k, v = s.split(":", 1)
            kv[k.strip().lower()] = v.strip()
    return kv


def _parse_edit_payload(payload: str):
    """解析 edit 载荷：path: 行 + ---OLD---/---NEW---/---END--- 围栏。
    返回 (path, old_text, new_text)；格式错误返回 None。"""
    plines = payload.split("\n")
    path = None
    old_idx = new_idx = end_idx = None
    for i, line in enumerate(plines):
        s = line.strip()
        if s.lower().startswith("path:") and path is None:
            path = s[5:].strip()
        elif s == "---OLD---":
            old_idx = i
        elif s == "---NEW---":
            new_idx = i
        elif s == "---END---":
            end_idx = i
            break
    if path and old_idx is not None and new_idx is not None and end_idx is not None:
        if old_idx < new_idx < end_idx:
            old_text = "\n".join(plines[old_idx + 1:new_idx])
            new_text = "\n".join(plines[new_idx + 1:end_idx])
            return path, old_text, new_text
    return None


def _parse_write_payload(payload: str):
    """解析新格式 write 载荷：path: 行 + ---BEGIN---/---END--- 围栏。

    新格式避免了 JSON 转义（大文件内容里的引号/换行/反斜杠无需转义）。
    返回 (path, content)；非新格式返回 None（调用方回退旧 JSON 解析）。
    """
    plines = payload.split('\n')
    path = None
    begin_idx = None
    end_idx = None
    for i, line in enumerate(plines):
        s = line.strip()
        if s.lower().startswith('path:') and path is None:
            path = s[5:].strip()
        elif s == '---BEGIN---':
            begin_idx = i
        elif s == '---END---':
            end_idx = i
            break
    if path and begin_idx is not None and end_idx is not None and end_idx > begin_idx:
        content = '\n'.join(plines[begin_idx + 1:end_idx])
        return path, content
    return None


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
