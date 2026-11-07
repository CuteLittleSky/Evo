"""读 OBS 虚拟摄像头（DirectShow）的截屏对象。

上游 `live_collect.collect()` 通过 `capture.open_camera(log)` 建截屏对象（live_collect.py:669）。
用户选「OBS 虚拟摄像头」时，`cloud/obs/capture.py` 会在记录子进程里把 `capture.open_camera`
换成这里返回的对象，接口和 dxcam / 上游的相机一样：width / height / grab(new_frame_only) /
release / start / stop。这样识别链路一行都不用改。

为什么用 OpenCV(CAP_DSHOW)，不再装库：
- 记录子进程本来就加载了 cv2；DirectShow 后端在 cv2.pyd 里，**不依赖**打包时会删掉的
  opencv_videoio_ffmpeg*.dll（2026-11-07 实测 backend=DSHOW 能开、能抓帧）。
- 设备号来自 `devices.list_devices()`，它枚举的顺序就是 OpenCV(CAP_DSHOW) 的设备号
  （2026-11-07 实测：OBS Virtual Camera 在两边序号都是 2）。

踩过的坑（2026-11-07 实测）：
- DirectShow 刚打开时前几帧是驱动缓冲里的旧画面；只读第一帧会拿到「上一眼」的图，
  菜单都切走了还显示旧界面。所以打开后先连读几帧丢掉（FLUSH_FRAMES），再返回最新帧。
- 设备可能被别的程序占用（会议软件 / 预览），open 失败当作「这帧没有」返回 None，
  上游循环写了 `if frame is not None:` 会跳过，不会污染识别。
"""
from __future__ import annotations

import time

from cloud.capture import devices as devices_mod

FLUSH_FRAMES = 6          # 打开后先丢掉几帧旧缓冲
REOPEN_SECONDS = 5.0      # 打不开 / 掉线后，隔多久再试
# 不设分辨率时 CAP_DSHOW 会协商成 640×480（2026-11-07 实测：OBS 虚拟摄像头在这个分辨率下
# 甚至给黑画面）。OBS 画布通常是 1920×1080，先请求它；设备实际给多少以读到的为准。
TARGET_WIDTH = 1920
TARGET_HEIGHT = 1080


class DirectShowCamera:
    """伪装成 dxcam 的相机，实际从 DirectShow 设备取帧（OBS 虚拟摄像头）。"""

    def __init__(self, index=None, name="", log=None):
        self._index = index
        self._name = name or "DirectShow"
        self._log = log or (lambda message: None)
        self._cap = None
        self._width = 0
        self._height = 0
        self._next_try = 0.0
        self._warned = set()
        self._paimiao_obs_camera = True   # 和 ProjectorCamera 同一标记：探测时别被误 release
        self._open()

    # ---------- 给上游用的接口 ----------
    @property
    def width(self):
        return self._width

    @property
    def height(self):
        return self._height

    def grab(self, *args, **kwargs):
        if self._cap is None:
            if time.monotonic() >= self._next_try:
                self._open()
            if self._cap is None:
                return None
        try:
            ok, frame = self._cap.read()
        except Exception as exc:  # noqa: BLE001
            self._once("grab_error", f"[OBS 虚拟摄像头] 读帧出错，跳过这一帧：{exc!r}")
            self._drop()
            self._next_try = time.monotonic() + REOPEN_SECONDS
            return None
        if not ok or frame is None:
            self._once("no_frame", "[OBS 虚拟摄像头] 这一帧没拿到画面（虚拟摄像机没启动 / 被别的程序占用？）")
            self._drop()
            self._next_try = time.monotonic() + REOPEN_SECONDS
            return None
        return frame

    def release(self):
        self._drop()
        self._next_try = 0.0

    def start(self, *args, **kwargs):
        return None

    def stop(self):
        return None

    # ---------- 内部 ----------
    def _open(self):
        if self._index is None:
            self._once("no_device", "[OBS 虚拟摄像头] 没有可用的设备（OBS Virtual Camera 没启动？）")
            self._next_try = time.monotonic() + REOPEN_SECONDS
            return
        try:
            import cv2  # noqa: PLC0415  记录子进程里有；面板进程不建这个对象
        except Exception as exc:  # noqa: BLE001
            self._once("no_cv2", f"[OBS 虚拟摄像头] 没有 OpenCV，读不了虚拟摄像头：{exc!r}")
            self._next_try = time.monotonic() + REOPEN_SECONDS
            return
        try:
            cap = cv2.VideoCapture(self._index, cv2.CAP_DSHOW)
        except Exception as exc:  # noqa: BLE001
            self._once("open_error", f"[OBS 虚拟摄像头] 打开设备 {self._index} 失败：{exc!r}")
            self._next_try = time.monotonic() + REOPEN_SECONDS
            return
        if not cap.isOpened():
            try:
                cap.release()
            except Exception:
                pass
            self._once("open_fail", f"[OBS 虚拟摄像头] 设备 {self._index}「{self._name}」打不开"
                                    "（被别的程序占用，或虚拟摄像机没启动）")
            self._next_try = time.monotonic() + REOPEN_SECONDS
            return
        self._cap = cap
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, TARGET_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, TARGET_HEIGHT)
        except Exception:
            pass
        # 丢掉打开瞬间的旧缓冲帧（见文件顶部踩坑）
        for _ in range(FLUSH_FRAMES):
            try:
                cap.read()
            except Exception:
                break
        try:
            self._width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 0
            self._height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 0
        except Exception:
            self._width = self._height = 0
        self._log(f"[OBS 虚拟摄像头] 已打开「{self._name}」（设备 {self._index}，{self._width}×{self._height}）")

    def _drop(self):
        cap, self._cap = self._cap, None
        self._width = self._height = 0
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def _once(self, key, message):
        """同一类提示只说一次，别每 0.3 秒刷一行。"""
        if key in self._warned:
            return
        self._warned.add(key)
        self._log(message)


# ---------- 选设备 / 打开 / 给设置页用的状态 ----------

OBS_NAME_HINT = devices_mod.OBS_NAME_HINT


def pick_device(devices, wanted=""):
    """挑要用的设备：优先用户选的（按名字/设备路径），否则名字里带 OBS Virtual Camera 的。"""
    devices = devices or []
    if wanted:
        low = wanted.strip().lower()
        for d in devices:
            if (d.get("name") or "").lower() == low or (d.get("path") or "").lower() == low:
                return d
    return devices_mod.find_obs(devices)


def open_camera(log=print, wanted=""):
    """建 DirectShowCamera，返回 (相机, 说明)。找不到设备也返回一个「空相机」（grab 一直 None）。"""
    try:
        devices = devices_mod.list_devices()
    except Exception as exc:  # noqa: BLE001
        log(f"[OBS 虚拟摄像头] 枚举视频设备失败：{exc!r}")
        devices = []
    target = pick_device(devices, wanted)
    if target is None:
        log("[OBS 虚拟摄像头] 没找到这台设备：请在 OBS 里「启动虚拟摄像机」，再在设置页确认设备")
        return DirectShowCamera(None, "", log=log), {"截法": "directshow", "directshow": "设备未找到", "设备": ""}
    return (DirectShowCamera(target["index"], target["name"], log=log),
            {"截法": "directshow", "directshow": None, "设备": target["name"]})


def _obs_running():
    """OBS 进程在不在（只读进程名，和游戏检测同一套）。"""
    try:
        from game_detect import process_names  # noqa: PLC0415  上游模块，只读
        return bool(process_names() & {"obs64.exe", "obs.exe"})
    except Exception:
        return False


def describe_camera(wanted=""):
    """给设置页用：虚拟摄像头能不能找到、OBS 在不在。

    永不抛异常，也**不打开设备**（避免和正在记录的子进程抢同一个摄像头）。
    """
    try:
        devices = devices_mod.list_devices()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "device": "", "index": None, "reason": f"枚举视频设备失败：{exc}",
                "obs_running": _obs_running(), "devices": []}
    target = pick_device(devices, wanted)
    obs_run = _obs_running()
    if target is None:
        reason = ("没找到 OBS 虚拟摄像头。请在 OBS 右下角点「启动虚拟摄像机」，再回来刷新。"
                  if devices else "这台电脑上没有视频采集设备，请先装好 OBS。")
        return {"available": False, "device": "", "index": None, "reason": reason,
                "obs_running": obs_run, "devices": devices}
    return {"available": True, "device": target["name"], "index": target["index"], "reason": "",
            "obs_running": obs_run, "devices": devices}
