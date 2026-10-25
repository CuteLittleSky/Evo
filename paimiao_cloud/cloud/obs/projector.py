"""找 OBS 的「源投影」窗口，并算出该截哪块屏幕的哪个矩形。

为什么用投影窗口，而不是 OBS 虚拟摄像头（2026-10-23 实测定下来的）：
    1. 虚拟摄像头会**独占**采集卡 —— OBS 自己也开不了同一个采集源，必须让 OBS
       先松手；投影窗口没有这个问题，OBS 想怎么用采集卡都行。
    2. 虚拟摄像头这条路要多一层转码（OBS 把画面喂给 DirectShow），延迟更高、
       CPU 更多；投影是 OBS 自己渲染出来的，画面就在屏幕上。
    3. 投影窗口是**无边框全屏**，铺满一块屏幕时正好 16:9，和 PC 版界面布局一致，
       上游的坐标换算（layout.py）一行都不用改。
    4. 用户不用为了 Evo 改 OBS 的输出设置，也不占一个「启动虚拟摄像机」的开关。

实测的坑（2026-10-23，OBS 32.2.2 / Windows 11）：
    - **投影窗口和 OBS 主窗口是同一个窗口类**（`Qt6111QWindowIcon`），
      类名里还带 Qt 版本号，换个 OBS 版本就变。所以不能按类名认。
    - 真正的判据是：进程名 `obs64.exe` + **是置顶窗口**（WS_EX_TOPMOST）
      + 尺寸接近铺满整块屏。OBS 主窗口不是置顶的、尺寸也小，一筛就掉。
    - 投影窗口默认就带置顶；用户如果在窗口上右键取消了「始终置顶」，
      它就会被别的窗口盖住 —— 这时**不能**截，否则截到的是压在上面的那个窗口。
      所以我们坚持要求 TOPMOST，检测不到就明确告诉用户去打开置顶。

另外：dxcam 的 output_idx 就是 Windows 的显示器序号（\\.\DISPLAY1 → 0）。
    探测时按窗口所在的显示器算出 output_idx，记录子进程直接用它建 camera。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

# OBS 的可执行文件名（小写）。OBS 32 起 64 位包就是 obs64.exe；旧版本可能是 obs.exe。
OBS_EXE_NAMES = {"obs64.exe", "obs.exe"}

# 尺寸容差：窗口比显示器小 20 像素以内都算「铺满这块屏」（DWM 有时候会留几像素）。
SIZE_TOLERANCE = 20

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GW_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
MONITORINFOF_PRIMARY = 0x00000001

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HANDLE
_user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p, wintypes.LPARAM]
_user32.EnumDisplayMonitors.restype = wintypes.BOOL

_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

MONITOR_DEFAULTTONEAREST = 2


# ---------- DPI 感知（踩过的坑，必读） ----------
#
# 2026-10-23 实测：不声明 DPI 感知时，EnumDisplayMonitors 和 GetWindowRect 返回的是
# **Windows 缩放后的逻辑像素**（这台机器上 2560x1440 的屏报成 1707x960，缩放 150%），
# 而 dxcam 的 grab 用的是**真实像素**。两者混用会让「窗口在哪儿」和「截图在哪儿」
# 对不上，多显示器时直接截错屏。
#
# Evo 自己的进程是 DPI 感知的（pywebview / 打包后的清单），所以正常运行时没问题；
# 但源码用 python 跑、或者从别的宿主里调这里时就不一定。探测函数一律先自己声明，
# 声明过头也只影响本进程的坐标口径，不会改变真实截图。
#
# 失败是正常的：进程已经声明过（返回 0 + ERROR_ACCESS_DENIED），照常往下走。

_PER_MONITOR_AWARE_V2 = -4
_dpi_done = False


def ensure_dpi_aware() -> bool:
    """把这个进程切成「每显示器 DPI 感知」。幂等，永不抛异常。"""
    global _dpi_done
    if _dpi_done:
        return True
    try:
        ctypes.WinDLL("user32").SetProcessDpiAwarenessContext(
            ctypes.c_void_p(_PER_MONITOR_AWARE_V2))
    except Exception:
        # 老系统没有这个 API（Win10 1703 以前）：退一步用 shcore 的旧接口
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
        except Exception:
            pass
    _dpi_done = True
    return True


class ProbeError(RuntimeError):
    """探测本身出了问题（API 调用失败）。调用方当「没找到」处理即可。"""


# ---------- 显示器 ----------

def monitors() -> list[dict]:
    """返回所有显示器：[{"index", "left", "top", "right", "bottom", "width", "height"}]。

    坐标是**真实像素**（先 ensure_dpi_aware()）。
    index 与 dxcam 的 output_idx 对齐，但要小心：**不能假设两边排序一致**。
    实测（2026-10-23，这台机器）：
        EnumDisplayMonitors（按 left, top 排）→ 0: (0,0) 2560x1440   1: (-2560,6) 2560x1600
        dxcam.create(output_idx=...)          → 0: 2560x1440   1: 2560x1600
    这台是碰巧一致。为了不靠运气，`find_projector()` 里还会用
    `dxcam_outputs()` 按分辨率再对一次（见那个函数的注释）。
    """
    ensure_dpi_aware()
    found: list[dict] = []

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    enum_proc_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def callback(hmonitor, hdc, lprect, lparam):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if _user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            r = info.rcMonitor
            found.append({"left": r.left, "top": r.top, "right": r.right, "bottom": r.bottom,
                          "width": r.right - r.left, "height": r.bottom - r.top,
                          # 主屏 = 屏幕设置里勾了「使此成为我的主显示器」那块，坐标是 (0, 0)
                          "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY)})
        return True

    if not _user32.EnumDisplayMonitors(None, None, enum_proc_type(callback), 0):
        raise ProbeError("EnumDisplayMonitors 失败")
    # 排序后就等于 dxcam / Windows 的序号：先左后上
    found.sort(key=lambda m: (m["left"], m["top"]))
    for i, m in enumerate(found):
        m["index"] = i
    return found


def _monitor_of(window_rect, all_monitors: list[dict]):
    """窗口落在哪块显示器上：按窗口中心点判断（投影窗口铺满时中心就在那块屏里）。"""
    cx = (window_rect[0] + window_rect[2]) // 2
    cy = (window_rect[1] + window_rect[3]) // 2
    for mon in all_monitors:
        if mon["left"] <= cx < mon["right"] and mon["top"] <= cy < mon["bottom"]:
            return mon
    return None


# ---------- 窗口 ----------

def _exe_path(pid: int) -> str:
    """进程的完整路径（小写）。打不开就当空串 —— 有些进程权限高，读不到是正常的。"""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.lower()
    finally:
        _kernel32.CloseHandle(handle)
    return ""


def obs_windows() -> list[dict]:
    """枚举所有属于 OBS 的可见窗口。返回原始信息，不判断是不是投影。

    每项：{"hwnd", "title", "class", "pid", "exe", "rect": (l,t,r,b),
           "width", "height", "topmost", "minimized"}
    """
    all_monitors = monitors()
    rows: list[dict] = []

    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, lparam):
        # 只接可见、没最小化的窗口
        if not _user32.IsWindowVisible(hwnd) or _user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _exe_path(pid.value)
        if not exe or exe.rsplit("\\", 1)[-1] not in OBS_EXE_NAMES:
            return True
        rect = wintypes.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        cls = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, cls, 256)
        title = ctypes.create_unicode_buffer(512)
        _user32.GetWindowTextW(hwnd, title, 512)
        exstyle = _user32.GetWindowLongW(hwnd, GW_EXSTYLE)
        r = (rect.left, rect.top, rect.right, rect.bottom)
        rows.append({
            "hwnd": hwnd, "title": title.value, "class": cls.value,
            "pid": pid.value, "exe": exe, "rect": r,
            "width": r[2] - r[0], "height": r[3] - r[1],
            # 注意：GetWindowLongW 返回 c_long，置顶位要按无符号位运算判断
            "topmost": bool((exstyle or 0) & WS_EX_TOPMOST),
            "minimized": bool(_user32.IsIconic(hwnd)),
            "monitor": _monitor_of(r, all_monitors),
        })
        return True

    if not _user32.EnumWindows(enum_proc_type(callback), 0):
        raise ProbeError("EnumWindows 失败")
    return rows


def dxcam_outputs() -> list[dict]:
    """问 dxcam 每个 output_idx 到底多大：[{"index", "width", "height"}]。

    为什么要问它、而不是相信 monitors() 的排序：
        dxcam 内部是 DXGI 的 EnumOutputs 顺序，和 Win32 的显示器顺序**不保证一致**
        （尤其是副屏在主屏左边、接在别的显卡上时）。两者一旦不一致，
        我们按窗口算出来的「第 2 块屏」在 dxcam 那边可能是第 1 块，截到的就是错的屏。
        按分辨率对上是最稳的判据：两块屏分辨率一样时顺带按位置再对一次。

    为什么要读工厂的元数据、而不是建一台相机去问 width/height：
        dxcam 的 create 是**按 (device, output, backend) 缓存的单例**，
        同一个组合只会存在一台 DXCamera。这里如果真的 create，
        拿回来的就是记录子进程手上正在用的那一台；我们在 finally 里 release 一下，
        那台就永久作废（再 grab 抛 "DXCamera has been released"），
        而工厂缓存里还留着它（is_released 的位置见 dxcam 的 create 实现），
        下一次再 create 又拿回同一台死相机 —— 死循环。
        2026-10-23 实测踩过：进了大厅读了段位，然后整天一帧都截不到，
        日志里每 5 秒「截屏相机已就绪」一次，全是重建。

        DXGI 的 output 描述本来就带桌面矩形（DesktopCoordinates），
        宽高直接从那里算，一台相机都不用建，也就不会碰坏别人的单例。

    打不开 dxcam（没装、DXGI 初始化失败）时返回空列表，调用方退回按顺序猜。
    """
    ensure_dpi_aware()
    try:
        import dxcam  # noqa: PLC0415  只在探测时按需导入，不拖慢启动
    except Exception:
        return []
    # 用工厂内部那两个属性名（dxcam 里 DXFactory.create 用的就是它们）。
    # 换 dxcam 大版本时这里可能失效 —— 所以整段包在 try 里，失败就退回空列表，
    # 调用方会退到「按显示器序号猜」，只是没那么准，不会崩。
    try:
        factory = getattr(dxcam, "__factory", None) or getattr(dxcam, "_factory")
        outputs = factory.outputs[0]          # 只用第一块显卡：Evo 本来就只认 device 0
    except Exception:
        return []
    out: list[dict] = []
    for index, output in enumerate(outputs):
        try:
            output.update_desc()
            rect = output.desc.DesktopCoordinates
            width, height = rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            continue
        if width > 0 and height > 0:
            out.append({"index": index, "width": int(width), "height": int(height)})
    return out


def _match_output(monitor: dict, outputs: list[dict]) -> dict:
    """把一块显示器对到 dxcam 的 output_idx 上。

    返回 {"index": dxcam 序号, "exact": 是否精确对上}。
    对不上（数量不同、分辨率不同）时按顺序猜，并标 exact=False —— 调用方据此
    决定要不要在日志里提醒用户。
    """
    if not outputs:
        return {"index": monitor["index"], "exact": False}
    for one in outputs:
        if one["width"] == monitor["width"] and one["height"] == monitor["height"]:
            return {"index": one["index"], "exact": True}
    return {"index": monitor["index"], "exact": False}


def find_projector() -> dict | None:
    """找那块「铺满一整块屏的 OBS 置顶窗口」= 源投影。

    判据（全部满足才算，宁可找不到也不要认错）：
      1. 属于 OBS 的进程；
      2. 是置顶窗口（否则会被别的窗口盖住，截到的是别人）；
      3. 尺寸接近铺满它所在的那块显示器（差 SIZE_TOLERANCE 像素以内）。

    返回：{"hwnd", "title", "monitor_index", "region": (left, top, right, bottom),
           "width", "height", "monitor": {...}, "output_index", "output_exact"}
     找不到返回 None。

    monitor_index 是「第几块屏」（从左到右数的顺序号），output_index 才是**给 dxcam 用的**
    序号 —— 两者不一定相同，别混用。output_exact 为 False 表示没能按分辨率对上，
    output_index 只是按顺序猜的。
    """
    rows = obs_windows()
    # 先把「置顶 + 铺满某块屏」的候选挑出来
    candidates = []
    for row in rows:
        if not row["topmost"]:
            continue
        mon = row["monitor"]
        if mon is None:
            continue
        if (abs(row["width"] - mon["width"]) > SIZE_TOLERANCE
                or abs(row["height"] - mon["height"]) > SIZE_TOLERANCE):
            continue
        candidates.append(row)
    if not candidates:
        return None
    # 万一有多个（两块屏各投一个）：取面积最大的那块，行为可预期
    best = max(candidates, key=lambda r: r["width"] * r["height"])
    mon = best["monitor"]
    matched = _match_output(mon, dxcam_outputs())
    return {
        "hwnd": best["hwnd"],
        "title": best["title"],
        "monitor_index": mon["index"],
        # 截显示器整块，而不是窗口矩形：窗口边缘可能比屏幕多/少几个像素，
        # 截整块屏得到的就是那块屏上真实显示的像素，比例一定对。
        "region": (mon["left"], mon["top"], mon["right"], mon["bottom"]),
        "width": mon["width"],
        "height": mon["height"],
        "monitor": mon,
        "output_index": matched["index"],
        "output_exact": matched["exact"],
    }


def describe() -> dict:
    """给设置页用：当前能不能截、为什么不能、截哪块。永不抛异常。"""
    try:
        all_monitors = monitors()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"读不到显示器信息：{exc}", "monitors": [], "projector": None,
                "topmost": False, "candidates": []}
    try:
        rows = obs_windows()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"枚举窗口失败：{exc}", "monitors": all_monitors,
                "projector": None, "topmost": False, "candidates": []}

    projector = find_projector()

    # 为什么不可用：分三种说清楚，用户照着做就行
    topmost_ones = [r for r in rows if r["topmost"] and r["monitor"] is not None
                    and abs(r["width"] - r["monitor"]["width"]) <= SIZE_TOLERANCE
                    and abs(r["height"] - r["monitor"]["height"]) <= SIZE_TOLERANCE]
    full_size = [r for r in rows if r["monitor"] is not None
                 and abs(r["width"] - r["monitor"]["width"]) <= SIZE_TOLERANCE
                 and abs(r["height"] - r["monitor"]["height"]) <= SIZE_TOLERANCE]

    reason = ""
    if projector is None:
        if not rows:
            reason = "没有找到正在运行的 OBS。请先打开 OBS。"
        elif not full_size:
            reason = ("没找到铺满整块屏幕的 OBS 窗口。请用 OBS 的「投影（全屏）」把画面投到一块屏幕上"
                      "（源上右键 → 全屏投影；或预览上右键 → 全屏投影）。")
        elif not topmost_ones:
            reason = ("找到铺满屏幕的 OBS 窗口，但它没有置顶，会被别的窗口盖住。"
                      "请在投影窗口上右键，勾选「始终置顶」。")
        else:
            reason = "没找到可用的投影窗口。"

    return {
        "available": projector is not None,
        "reason": reason,
        "monitors": all_monitors,
        "projector": projector,
        "topmost": bool(topmost_ones),
        "candidates": [{"title": r["title"], "width": r["width"], "height": r["height"],
                        "topmost": r["topmost"], "pid": r["pid"]}
                       for r in rows],
    }
