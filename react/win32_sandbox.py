"""Windows OS 级沙箱：给 ACT 的 shell 子进程套内核级约束。

设计目标（对标 Codex CLI / Chrome 沙箱思路，纯 ctypes 零依赖）：
- **受限令牌**（`CreateRestrictedToken` + `DISABLE_MAX_PRIVILEGE`）：剥掉进程所有特权
  （SeDebugPrivilege / SeShutdownPrivilege / SeTakeOwnership 等），即便当前用户是管理员，
  子进程也只是"无特权的该用户"，无法做提权类危险动作。
- **作业对象（Job Object）**：把子进程（含其后代）关进一个内核作业，
  - `KILL_ON_JOB_CLOSE`：父进程（本 agent）退出时，作业内所有进程一并被杀，杜绝孤儿后台服务/监听端口；
  - 默认**禁止 breakaway**（不置 `JOB_OBJECT_LIMIT_BREAKAWAY_OK`）：`cmd /c start xxx` 之类
    想脱离作业另起炉灶的进程，仍被作业兜住、随作业同归于尽；
  - `ActiveProcessLimit`：限制进程总数，防 fork 炸弹；
  - `JobMemoryLimit`：限制作业总内存，防内存耗尽。
- **可选低完整性级别（Low IL）**：把令牌降到 Low（S-1-16-4096），使其无法写入 Medium/High
  完整性对象（系统目录、其他用户数据），只能写被显式降为 Low 的工作区。默认关闭，开启时
  会自动用 icacls 把 cwd 降为 Low IL（属持久性改动，可用 `icacls <cwd> /setintegritylevel M` 还原）。

注意边界（与 OS 沙箱定位一致）：
- 子进程**仍运行在当前用户身份下**，对本用户有权访问的文件（含 cwd 外）可读写——这是"以用户身份跑命令"
  的固有属性；真正的账号隔离需另建低权账户或容器，列为后续扩展。
- **不阻断网络**：网络隔离需 Windows 防火墙 API 或独立网络命名空间，本期未做，列为后续扩展。
- 非 Windows 平台：`sandbox_available()` 返回 False，`run_sandboxed` 抛 NotImplementedError，
  调用方应回退到普通 `subprocess.run`。

回显格式与 LocalExecutor._shell 保持一致：`exit=<code>\n<output[:4000]>`。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import time

__all__ = ["sandbox_available", "run_sandboxed"]

try:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _WIN = True
except Exception:  # pragma: no cover - 仅非 Windows 触发
    _WIN = False

try:
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
except Exception:  # pragma: no cover - 非 Windows 或精简镜像
    psapi = None


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
PROCESS_ALL_ACCESS = 0x1F0FFF
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_SUSPENDED = 0x00000004
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001
INFINITE = 0xFFFFFFFF
WAIT_TIMEOUT = 0x00000102

TOKEN_QUERY = 0x0008
TOKEN_DUPLICATE = 0x0002
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_ADJUST_DEFAULT = 0x0080

DISABLE_MAX_PRIVILEGE = 0x1
TokenIntegrityLevel = 25

JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000    # 脱离当前（外层）作业，使子进程可挂入本进程新建的作业
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800  # 外层作业需设此标志才允许内部进程 breakaway

_LOW_INTEGRITY_SID = "S-1-16-4096"  # Low Mandatory Level

# 进程快照 / 进程树枚举 / 软限制（Job 失效时的兜底，不依赖 Job 对象）
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

# 最近一次 run_sandboxed 的 Job 隔离是否有效（供状态展示；Job 失效时降级为软隔离兜底）
last_run_job_effective: bool = False


# ---------------------------------------------------------------------------
# 结构体
# ---------------------------------------------------------------------------
class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wt.DWORD),
        ("lpSecurityDescriptor", wt.LPVOID),
        ("bInheritHandle", wt.BOOL),
    ]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("lpReserved", wt.LPWSTR),
        ("lpDesktop", wt.LPWSTR),
        ("lpTitle", wt.LPWSTR),
        ("dwX", wt.DWORD),
        ("dwY", wt.DWORD),
        ("dwXSize", wt.DWORD),
        ("dwYSize", wt.DWORD),
        ("dwXCountChars", wt.DWORD),
        ("dwYCountChars", wt.DWORD),
        ("dwFillAttribute", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("wShowWindow", wt.WORD),
        ("cbReserved2", wt.WORD),
        ("lpReserved2", wt.LPBYTE),
        ("hStdInput", wt.HANDLE),
        ("hStdOutput", wt.HANDLE),
        ("hStdError", wt.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wt.HANDLE),
        ("hThread", wt.HANDLE),
        ("dwProcessId", wt.DWORD),
        ("dwThreadId", wt.DWORD),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wt.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wt.LARGE_INTEGER),
        ("LimitFlags", wt.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wt.DWORD),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", wt.ULARGE_INTEGER),
        ("WriteOperationCount", wt.ULARGE_INTEGER),
        ("OtherOperationCount", wt.ULARGE_INTEGER),
        ("ReadTransferCount", wt.ULARGE_INTEGER),
        ("WriteTransferCount", wt.ULARGE_INTEGER),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Sid", wt.LPVOID),
        ("Attributes", wt.DWORD),
    ]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [
        ("Label", SID_AND_ATTRIBUTES),
    ]


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", wt.LONG),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


# ---------------------------------------------------------------------------
# 函数签名绑定
# ---------------------------------------------------------------------------
def _bind() -> None:
    advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    advapi32.OpenProcessToken.restype = wt.BOOL

    advapi32.CreateRestrictedToken.argtypes = [
        wt.HANDLE, wt.DWORD,
        wt.DWORD, ctypes.POINTER(SID_AND_ATTRIBUTES),
        wt.DWORD, ctypes.c_void_p,
        wt.DWORD, ctypes.POINTER(SID_AND_ATTRIBUTES),
        ctypes.POINTER(wt.HANDLE),
    ]
    advapi32.CreateRestrictedToken.restype = wt.BOOL

    advapi32.SetTokenInformation.argtypes = [wt.HANDLE, wt.DWORD, wt.LPVOID, wt.DWORD]
    advapi32.SetTokenInformation.restype = wt.BOOL

    advapi32.ConvertStringSidToSidW.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.LPVOID)]
    advapi32.ConvertStringSidToSidW.restype = wt.BOOL

    advapi32.CreateProcessAsUserW.argtypes = [
        wt.HANDLE, wt.LPCWSTR, wt.LPWSTR,
        ctypes.POINTER(SECURITY_ATTRIBUTES), ctypes.POINTER(SECURITY_ATTRIBUTES),
        wt.BOOL, wt.DWORD, wt.LPVOID, wt.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION),
    ]
    advapi32.CreateProcessAsUserW.restype = wt.BOOL

    kernel32.CreateJobObjectW.argtypes = [ctypes.POINTER(SECURITY_ATTRIBUTES), wt.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wt.HANDLE

    kernel32.SetInformationJobObject.argtypes = [wt.HANDLE, wt.DWORD, wt.LPVOID, wt.DWORD]
    kernel32.SetInformationJobObject.restype = wt.BOOL

    kernel32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wt.BOOL

    kernel32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
    kernel32.TerminateJobObject.restype = wt.BOOL

    kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
    kernel32.TerminateProcess.restype = wt.BOOL

    kernel32.ResumeThread.argtypes = [wt.HANDLE]
    kernel32.ResumeThread.restype = wt.DWORD

    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wt.HANDLE), ctypes.POINTER(wt.HANDLE),
        ctypes.POINTER(SECURITY_ATTRIBUTES), wt.DWORD,
    ]
    kernel32.CreatePipe.restype = wt.BOOL

    kernel32.SetHandleInformation.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD]
    kernel32.SetHandleInformation.restype = wt.BOOL

    kernel32.ReadFile.argtypes = [wt.HANDLE, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
    kernel32.ReadFile.restype = wt.BOOL

    kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    kernel32.GetExitCodeProcess.restype = wt.BOOL

    kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel32.WaitForSingleObject.restype = wt.DWORD

    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL

    kernel32.GetOEMCP.argtypes = []
    kernel32.GetOEMCP.restype = wt.UINT

    kernel32.LocalFree.argtypes = [wt.HANDLE]
    kernel32.LocalFree.restype = wt.HANDLE

    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wt.HANDLE

    kernel32.IsProcessInJob.argtypes = [wt.HANDLE, wt.HANDLE, ctypes.POINTER(wt.BOOL)]
    kernel32.IsProcessInJob.restype = wt.BOOL

    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE

    kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE

    kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    kernel32.Process32FirstW.restype = wt.BOOL

    kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    kernel32.Process32NextW.restype = wt.BOOL

    if psapi is not None:
        psapi.GetProcessMemoryInfo.argtypes = [
            wt.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wt.DWORD]
        psapi.GetProcessMemoryInfo.restype = wt.BOOL


if _WIN:
    _bind()


def sandbox_available() -> bool:
    """当前平台是否支持 Win32 沙箱（仅 Windows 且 ctypes 可用）。"""
    return _WIN


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _raise_last(api: str) -> None:
    raise ctypes.WinError(ctypes.get_last_error(), f"{api} 失败")


def _decode(b: bytes) -> str:
    oemcp = kernel32.GetOEMCP() if _WIN else 0
    for enc in ((f"cp{oemcp}" if oemcp else None), "utf-8", "cp1252"):
        if not enc:
            continue
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


def _make_inherit_sa() -> SECURITY_ATTRIBUTES:
    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(sa)
    sa.bInheritHandle = True
    sa.lpSecurityDescriptor = None
    return sa


def _create_restricted_token(low_integrity: bool) -> wt.HANDLE:
    """打开当前进程令牌 → 剥特权受限令牌 →（可选）降完整性级别。"""
    h_proc = kernel32.GetCurrentProcess()
    h_token = wt.HANDLE()
    if not advapi32.OpenProcessToken(
        h_proc,
        TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY | TOKEN_ADJUST_DEFAULT,
        ctypes.byref(h_token),
    ):
        _raise_last("OpenProcessToken")

    h_restricted = wt.HANDLE()
    # 全部 SID/特权计数为 0 + NULL 指针，仅靠 DISABLE_MAX_PRIVILEGE 剥掉所有特权
    if not advapi32.CreateRestrictedToken(
        h_token, DISABLE_MAX_PRIVILEGE,
        0, None, 0, None, 0, None,
        ctypes.byref(h_restricted),
    ):
        _raise_last("CreateRestrictedToken")
    kernel32.CloseHandle(h_token)

    if low_integrity:
        p_sid = wt.LPVOID()
        if not advapi32.ConvertStringSidToSidW(_LOW_INTEGRITY_SID, ctypes.byref(p_sid)):
            _raise_last("ConvertStringSidToSidW")
        tml = TOKEN_MANDATORY_LABEL()
        tml.Label.Sid = p_sid
        tml.Label.Attributes = 0x00000020  # SE_GROUP_INTEGRITY
        if not advapi32.SetTokenInformation(
            h_restricted, TokenIntegrityLevel,
            ctypes.byref(tml), ctypes.sizeof(tml),
        ):
            _raise_last("SetTokenInformation(TokenIntegrityLevel)")
        kernel32.LocalFree(p_sid)
    return h_restricted


def _is_in_job() -> bool:
    """检测当前进程是否已处于某个作业对象中（嵌套作业环境的判据）。"""
    if not _WIN:
        return False
    in_job = wt.BOOL(False)
    if kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), None, ctypes.byref(in_job)):
        return bool(in_job.value)
    return False


def _enum_process_tree(root_pid: int) -> list[int]:
    """返回 root_pid 的所有后代进程 PID（含 root 自身），严格按父 PID 链判定，避免误杀。"""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return [root_pid]
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        parent_of: dict[int, int] = {}
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                parent_of[entry.th32ProcessID] = entry.th32ParentProcessID
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)
    # 从 root 出发沿父子关系收集所有后代（BFS）
    result: list[int] = [root_pid]
    stack = [root_pid]
    while stack:
        cur = stack.pop()
        for pid, ppid in parent_of.items():
            if ppid == cur and pid not in result:
                result.append(pid)
                stack.append(pid)
    return result


def kill_tree(root_pid: int) -> None:
    """递归终止 root_pid 的整个进程树（替代失效的 KILL_ON_JOB_CLOSE 防孤儿）。"""
    for pid in _enum_process_tree(root_pid):
        if pid == 0 or pid == root_pid:
            continue
        h = kernel32.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_INFORMATION, False, pid)
        if not h:
            continue
        try:
            kernel32.TerminateProcess(h, 1)
        finally:
            kernel32.CloseHandle(h)


def _tree_process_count(root_pid: int) -> int:
    """子进程树进程总数（替代失效的 ActiveProcessLimit 软限制）。"""
    return len(_enum_process_tree(root_pid))


def _tree_memory_bytes(root_pid: int) -> int:
    """子进程树工作集内存总和（替代失效的 JobMemoryLimit 软限制）。"""
    if psapi is None:
        return 0
    total = 0
    for pid in _enum_process_tree(root_pid):
        h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            continue
        try:
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(h, ctypes.byref(counters), counters.cb):
                total += counters.WorkingSetSize
        finally:
            kernel32.CloseHandle(h)
    return total


def _create_job(active_process_limit: int, job_memory_limit: int) -> wt.HANDLE:
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        _raise_last("CreateJobObjectW")
    # 核心约束：进程数上限（防 fork 炸弹）+ kill-on-close（父进程退出即清场）。
    # 注意：若本进程自身已处于某个作业中（嵌套作业，常见于被托管的运行时环境），
    # KILL_ON_JOB_CLOSE 会被内核拒绝（ERROR_INVALID_PARAMETER），此时降级为"仅进程数上限"，
    # 超时仍会直接 TerminateProcess 子进程，逃逸防护不失能。
    basic = JOBOBJECT_BASIC_LIMIT_INFORMATION()
    basic.LimitFlags = JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    basic.ActiveProcessLimit = active_process_limit
    if not kernel32.SetInformationJobObject(
        job, 2, ctypes.byref(basic), ctypes.sizeof(basic)  # 2 = JobObjectBasicLimitInformation
    ):
        # 重试：去掉 kill-on-close，仅保留进程数上限
        basic.LimitFlags = JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        if not kernel32.SetInformationJobObject(
            job, 2, ctypes.byref(basic), ctypes.sizeof(basic)
        ):
            _raise_last("SetInformationJobObject(BasicLimit)")
        sys.stderr.write(
            "[sandbox] 注意：kill-on-close 不可用（可能处于嵌套作业中），作业降级为仅进程数上限；"
            "若子进程最终未能挂入作业，Job 隔离整体失效，仅令牌降权 + 软隔离兜底生效\n"
        )
    # 可选内存上限（JobObjectExtendedLimitInformation）。部分 Windows 版本对 JOB_MEMORY 限制
    # 较挑剔（ERROR_BAD_LENGTH），故仅在显式给出正值时尝试，失败则降级忽略（不影响基本约束）。
    if job_memory_limit and job_memory_limit > 0:
        ext = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        ext.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | 0x00000200  # JOB_OBJECT_LIMIT_JOB_MEMORY
        )
        ext.BasicLimitInformation.ActiveProcessLimit = active_process_limit
        ext.JobMemoryLimit = job_memory_limit
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(ext), ctypes.sizeof(ext)
        ):
            sys.stderr.write(
                "[sandbox] 注意：作业内存上限设置失败（嵌套环境常见），已降级为无内存上限\n"
            )
    return job


def _reader(read_handle: wt.HANDLE, sink: list[bytes]) -> None:
    buf = ctypes.create_string_buffer(4096)
    chunks: list[bytes] = []
    while True:
        n = wt.DWORD(0)
        ok = kernel32.ReadFile(read_handle, buf, 4096, ctypes.byref(n), None)
        if not ok or n.value == 0:
            break
        chunks.append(buf.raw[: n.value])
    sink.append(b"".join(chunks))


def _lower_cwd_integrity(cwd: str) -> None:
    """低完整性模式：把 cwd 降为 Low IL（持久性改动，使 Low IL 进程可写入）。"""
    import subprocess

    subprocess.run(
        ["icacls", cwd, "/setintegritylevel", "L"],
        shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def run_sandboxed(
    cmd: str,
    cwd: str,
    timeout_sec: int = 30,
    low_integrity: bool = False,
    active_process_limit: int = 64,
    job_memory_limit: int = 512 * 1024 * 1024,
    backend: str = "cmd",
    bash_path: str = "",
) -> str:
    """在受限令牌 + 作业对象下执行 `cmd`，返回 `exit=<code>\n<output>`。

    参数：
      cmd                  要执行的命令
      cwd                 子进程工作目录
      timeout_sec         超时（秒），超时则终止整个作业
      low_integrity       是否降完整性级别（开启前会先把 cwd 降为 Low IL）
      backend             shell 后端：cmd=cmd.exe /c，bash=bash.exe 执行脚本
      bash_path           bash.exe 绝对路径（backend=bash 时必填）
    异常：非 Windows / ctypes 缺失 / Win32 调用失败 → 抛对应异常。
    """
    if not _WIN:
        raise NotImplementedError("win32_sandbox 仅在 Windows 平台可用")
    if low_integrity:
        _lower_cwd_integrity(cwd)

    token = _create_restricted_token(low_integrity)
    job = _create_job(active_process_limit, job_memory_limit)

    # 管道：写端可继承给子进程，读端不可继承
    sa = _make_inherit_sa()
    out_r, out_w = wt.HANDLE(), wt.HANDLE()
    err_r, err_w = wt.HANDLE(), wt.HANDLE()
    if not kernel32.CreatePipe(ctypes.byref(out_r), ctypes.byref(out_w), ctypes.byref(sa), 0):
        _raise_last("CreatePipe(stdout)")
    if not kernel32.CreatePipe(ctypes.byref(err_r), ctypes.byref(err_w), ctypes.byref(sa), 0):
        _raise_last("CreatePipe(stderr)")
    kernel32.SetHandleInformation(out_r, HANDLE_FLAG_INHERIT, 0)
    kernel32.SetHandleInformation(err_r, HANDLE_FLAG_INHERIT, 0)

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.dwFlags = STARTF_USESTDHANDLES
    si.hStdOutput = out_w
    si.hStdError = err_w
    si.hStdInput = None  # 无 stdin，命令不应期望键盘输入

    # 命令：cmd 后端用 cmd.exe /c；bash 后端把脚本写临时文件再 bash 执行（避开引号转义坑）
    tmp_script = ""
    if backend == "bash" and bash_path:
        import tempfile
        tmp_script = os.path.join(tempfile.gettempdir(), "react_agent_sandbox.sh")
        with open(tmp_script, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(cmd)
        comspec = bash_path
        cmdline = ctypes.create_unicode_buffer(f'"{bash_path}" "{tmp_script}"')
    else:
        comspec = os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
        cmdline = ctypes.create_unicode_buffer(f"/c {cmd}")
    pi = PROCESS_INFORMATION()

    base_flags = CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED
    # 尝试从外层作业 breakaway，使子进程能挂入本进程自建的作业（嵌套作业兼容）。
    # 外层作业若未设 JOB_OBJECT_LIMIT_BREAKAWAY_OK，此创建会失败（子进程仍困外层），需去除标志重试。
    created = advapi32.CreateProcessAsUserW(
        token, comspec, cmdline,
        None, None, True,
        base_flags | CREATE_BREAKAWAY_FROM_JOB,
        None, cwd,
        ctypes.byref(si), ctypes.byref(pi),
    )
    if not created:
        # 外层不允许 breakaway：回退普通创建（不破坏子进程启动，零风险）
        created = advapi32.CreateProcessAsUserW(
            token, comspec, cmdline,
            None, None, True,
            base_flags,
            None, cwd,
            ctypes.byref(si), ctypes.byref(pi),
        )
    # 关闭父进程侧的写端副本（仅子进程持有写端；子进程退出/被杀 → EOF）
    kernel32.CloseHandle(out_w)
    kernel32.CloseHandle(err_w)
    if not created:
        kernel32.CloseHandle(out_r)
        kernel32.CloseHandle(err_r)
        _raise_last("CreateProcessAsUserW")

    # 作业归属：先挂作业再唤醒，杜绝"创建即逃逸"竞态
    job_assigned = kernel32.AssignProcessToJobObject(job, pi.hProcess)
    global last_run_job_effective
    last_run_job_effective = bool(job_assigned)
    if not job_assigned:
        # 子进程继承进外层作业（嵌套限制）或 breakaway 失败，本作业未挂入 → Job 隔离整层失效，
        # 仅令牌降权 + 超时直杀 + 软隔离兜底（见下方 kill_tree / 软限制）生效。
        if _is_in_job():
            sys.stderr.write(
                "[sandbox] 注意：本进程已在作业（嵌套环境）中，子进程挂入自建作业失败，"
                "Job 隔离未生效；仅令牌降权 + 超时直杀 + 软隔离兜底生效\n"
            )
        else:
            sys.stderr.write(
                "[sandbox] 注意：子进程挂入作业失败，Job 隔离未生效；"
                "仅令牌降权 + 超时直杀 + 软隔离兜底生效\n"
            )

    kernel32.ResumeThread(pi.hThread)

    out_buf: list[bytes] = []
    err_buf: list[bytes] = []
    t_out = threading.Thread(target=_reader, args=(out_r, out_buf), daemon=True)
    t_err = threading.Thread(target=_reader, args=(err_r, err_buf), daemon=True)
    t_out.start()
    t_err.start()

    timed_out = False
    violated = False
    root_pid = pi.dwProcessId
    # 轮询式等待：支持超时清理，并在 Job 失效时用进程树软限制兜底
    deadline = time.monotonic() + timeout_sec
    poll_ms = 200
    poll_n = 0
    while True:
        wait_res = kernel32.WaitForSingleObject(pi.hProcess, poll_ms)
        if wait_res != WAIT_TIMEOUT:
            break  # 主进程已自然退出
        if time.monotonic() >= deadline:
            timed_out = True
            break
        # 仅 Job 未生效时，用进程树软限制兜底（替代失效的 ActiveProcessLimit / JobMemoryLimit）
        poll_n += 1
        if not job_assigned and poll_n % 5 == 0:
            if (_tree_process_count(root_pid) > active_process_limit
                    or _tree_memory_bytes(root_pid) > job_memory_limit):
                violated = True
                timed_out = True
                break

    if timed_out:
        # Job 未挂入时 TerminateJobObject 无效（作业内无进程），改用 kill_tree 杀整棵子树；
        # 即便 Job 生效，TerminateJobObject 仍补足（无害）。
        if not job_assigned:
            kill_tree(root_pid)
        kernel32.TerminateProcess(pi.hProcess, 1)
        kernel32.TerminateJobObject(job, 1)

    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)

    exit_code = wt.DWORD(0)
    kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))

    # 收尾句柄
    if tmp_script:
        try:
            os.unlink(tmp_script)
        except OSError:
            pass
    kernel32.CloseHandle(pi.hThread)
    kernel32.CloseHandle(pi.hProcess)
    kernel32.CloseHandle(out_r)
    kernel32.CloseHandle(err_r)
    kernel32.CloseHandle(job)
    kernel32.CloseHandle(token)

    out = _decode(b"".join(out_buf))
    err = _decode(b"".join(err_buf))
    text = out or ""
    if err:
        text += f"\n[stderr]\n{err}"

    if timed_out:
        if violated:
            return (f"（执行被终止：子进程树超出资源软限制"
                    f"（进程数>{active_process_limit} 或 内存>{job_memory_limit} 字节），已清理）")
        return f"（执行超时：超过 {timeout_sec}s 未结束，已清理）"
    # 截断长度与 LocalExecutor 共用同一常量，避免两处漂移
    from .executor import _OUTPUT_LIMIT

    return f"exit={exit_code.value}\n{text.strip()[:_OUTPUT_LIMIT]}"
