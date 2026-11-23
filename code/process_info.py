"""进程的内存、CPU 占用，本程序的优先级和省电限速，用 Windows 自带的功能（ctypes），不需要安装别的包。

记录程序用它把自己设成低优先级（CPU 紧张时让游戏优先），读结算画面时不让 Windows 降频（set_full_speed），
并在日志里报告内存。总览页的「Evo 占用」用 snapshot、system_times 量 Evo 全部进程（见 evo_usage.py）。

snapshot 和任务管理器一样，向 Windows 要一份所有进程的列表（NtQuerySystemInformation；game_detect.py 用的
Toolhelp32 也是从这里取的），里面就有每个进程的 CPU 时间和"专用工作集"（任务管理器默认显示的"内存"）。
不打开任何进程，也就不碰游戏（原则 1）。
"""

import ctypes
import os
from collections import namedtuple
from ctypes import wintypes

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
PROCESS_POWER_THROTTLING = 4  # SetProcessInformation 的 ProcessPowerThrottling
POWER_THROTTLING_EXECUTION_SPEED = 0x1


class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


# 用自己的 WinDLL 对象并声明参数类型，不影响别的代码对 ctypes.windll 的设置
_kernel32 = ctypes.WinDLL("kernel32")
_psapi = ctypes.WinDLL("psapi")
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_kernel32.SetProcessInformation.restype = wintypes.BOOL
_psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]


def memory_mb():
    """返回本程序当前内存占用和历史峰值（MB），和任务管理器里看到的"内存"是同一种口径。"""
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    _psapi.GetProcessMemoryInfo(_kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
    return counters.WorkingSetSize / 2**20, counters.PeakWorkingSetSize / 2**20


# ---------- 所有进程的 CPU、内存（不打开进程）----------

class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]


class _SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    """每个进程一段（只写到要用的那几项；后面还有线程的信息，靠 NextEntryOffset 跳过）。"""
    _fields_ = [
        ("NextEntryOffset", wintypes.ULONG),
        ("NumberOfThreads", wintypes.ULONG),
        ("WorkingSetPrivateSize", ctypes.c_longlong),  # 专用工作集（字节），任务管理器默认的"内存"
        ("HardFaultCount", wintypes.ULONG),
        ("NumberOfThreadsHighWatermark", wintypes.ULONG),
        ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong),  # 以下三项单位都是 100 纳秒
        ("UserTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("ImageName", _UNICODE_STRING),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),  # 父进程
    ]


SYSTEM_PROCESS_INFORMATION = 5
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004

_ntdll = ctypes.WinDLL("ntdll")
_ntdll.NtQuerySystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                            ctypes.POINTER(wintypes.ULONG)]
_ntdll.NtQuerySystemInformation.restype = ctypes.c_ulong
_kernel32.GetSystemTimes.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)] * 3

# 一个进程：进程号、父进程号、名字（小写）、启动时刻（100 纳秒）、用过的 CPU 时间（秒，所有核加起来）、内存（MB）
Proc = namedtuple("Proc", "pid parent name created cpu memory")


def snapshot():
    """现在所有进程：{进程号: Proc}。约 1–3 毫秒，读不到时返回 {}。"""
    size = wintypes.ULONG(512 * 1024)
    for _ in range(5):  # 两次调用之间新开了进程，列表变长了：放大再要一次
        buffer = ctypes.create_string_buffer(size.value + 64 * 1024)
        status = _ntdll.NtQuerySystemInformation(SYSTEM_PROCESS_INFORMATION, buffer, len(buffer), ctypes.byref(size))
        if status == 0:
            break
        if status != STATUS_INFO_LENGTH_MISMATCH:
            return {}
    else:
        return {}
    procs, offset = {}, 0
    while True:
        info = _SYSTEM_PROCESS_INFORMATION.from_buffer(buffer, offset)
        pid = info.UniqueProcessId or 0
        name = ctypes.wstring_at(info.ImageName.Buffer, info.ImageName.Length // 2) if info.ImageName.Buffer else ""
        procs[pid] = Proc(pid, info.InheritedFromUniqueProcessId or 0, name.lower(), info.CreateTime,
                          (info.UserTime + info.KernelTime) / 1e7, info.WorkingSetPrivateSize / 2**20)
        if not info.NextEntryOffset:
            return procs
        offset += info.NextEntryOffset


def descendants(procs, root):
    """root 和它的子进程、子进程的子进程……的进程号（root 不在列表里时返回空集合）。
    父进程号可能是已经退出的进程留下的、又分给了别的进程：比父进程启动得早的不算它的子进程。"""
    if root not in procs:
        return set()
    children = {}
    for p in procs.values():
        if p.pid != p.parent:
            children.setdefault(p.parent, []).append(p)
    found, todo = {root}, [procs[root]]
    while todo:
        parent = todo.pop()
        for child in children.get(parent.pid, ()):
            if child.pid not in found and child.created >= parent.created:
                found.add(child.pid)
                todo.append(child)
    return found


def system_times():
    """整台电脑开机以来（忙, 合计）的 CPU 时间（秒，所有核加起来）：两次相减，忙 ÷ 合计 = 这段时间整台电脑的 CPU 占用。"""
    idle, kernel, user = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
    if not _kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    total = kernel.value + user.value  # kernel 里含空闲的时间
    return (total - idle.value) / 1e7, total / 1e7


def logical_cpus():
    """逻辑处理器个数：任务管理器的 CPU 占用 = 用过的 CPU 时间 ÷（经过的时间 × 这个数）。"""
    return os.cpu_count() or 1


class _BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BASIC_LIMIT), ("IoInfo", ctypes.c_ulonglong * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_job = None
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


def kill_with_me(proc):
    """把子进程（subprocess.Popen）放进"本程序一退出就全部结束"的作业对象：本程序崩了、被强行结束，子进程也跟着退
    （录像用：不然 ffmpeg 会一直往硬盘写，见 recorder.py）。成功返回 True。"""
    global _job
    if _job is None:
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
                                                 ctypes.sizeof(info)):
            return False
        _job = job  # 一直不关：本程序退出时 Windows 关掉它，里面的进程跟着结束
    return bool(_kernel32.AssignProcessToJobObject(_job, int(proc._handle)))


def set_low_priority():
    """把本程序设为"低于正常"优先级，CPU 紧张时让游戏优先。"""
    _kernel32.SetPriorityClass(_kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)


def set_full_speed(on):
    """on=True：告诉 Windows 别给本程序降频（省电限速 EcoQoS：笔记本上没有窗口的后台程序可能被压低频率、挪到小核）；
    False：交还给 Windows 决定。优先级不变。成功返回 True（Windows 10 1709 以前没有这个功能，返回 False）。"""
    state = PROCESS_POWER_THROTTLING_STATE(1, POWER_THROTTLING_EXECUTION_SPEED if on else 0, 0)
    return bool(_kernel32.SetProcessInformation(_kernel32.GetCurrentProcess(), PROCESS_POWER_THROTTLING,
                                                ctypes.byref(state), ctypes.sizeof(state)))
