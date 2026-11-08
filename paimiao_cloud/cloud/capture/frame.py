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
REOPEN_SECONDS = 2.0      # 打不开 / 掉线后，隔多久再试
# 不设分辨率时 CAP_DSHOW 会协商成 640×480（2026-11-07 实测：OBS 虚拟摄像头在这个分辨率下
# 甚至给黑画面）。OBS 画布通常是 1920×1080，先请求它；设备实际给多少以读到的为准。
TARGET_WIDTH = 1920
TARGET_HEIGHT = 1080

# 连续纯黑就重开设备尝试恢复（采集卡偶发掉成纯黑时，重开常能恢复；2026-11-08 用户遇到）
BLACK_MEAN = 10.0
BLACK_STD = 6.0
BLACK_REOPEN_SECONDS = 3.0


class DirectShowCamera:
    """伪装成 dxcam 的相机，实际从 DirectShow 设备取帧（OBS 虚拟摄像头）。"""

    def __init__(self, index=None, name="", log=None, mjpg=False):
        self._index = index
        self._name = name or "DirectShow"
        self._log = log or (lambda message: None)
        self._mjpg = mjpg
        self._cap = None
        self._width = 0
        self._height = 0
        self._next_try = 0.0
        self._warned = set()
        self._black_since = None
        self._last_reopen = 0.0
        self._seen_signal = False
        self._preferred = None
        self._card_down = False           # 采集卡是否处于「没信号」状态：只在状态变化时记日志
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
            self._once("grab_error", f"[直读] 「{self._name}」断开连接：读帧出错（{exc!r}），2 秒后自动重连")
            self._drop()
            self._next_try = time.monotonic() + REOPEN_SECONDS
            return None
        if not ok or frame is None:
            self._once("no_frame", f"[直读] 「{self._name}」断开连接：这一帧没拿到画面"
                                    "（采集卡没信号 / 被别的程序占用），2 秒后自动重连")
            self._drop()
            self._next_try = time.monotonic() + REOPEN_SECONDS
            return None
        # 连续纯黑 → 隔一会儿自动重开设备试一次（采集卡偶发掉黑/被二次占用时能自愈）
        try:
            small = frame[::8, ::8]
            near_black = float(small.mean()) < BLACK_MEAN and float(small.std()) < BLACK_STD
        except Exception:
            near_black = False
        now = time.monotonic()
        # 只对采集卡（mjpg=True）做「全黑就重开设备」：OBS 虚拟摄像头场景本身就是黑场/黑屏加载时会
        # 被反复重开，且日志会误标成「[采集卡]」。虚拟摄像头不走这条，交给正常的掉线重连。
        if self._mjpg and near_black:
            if self._black_since is None:
                self._black_since = now
            elif (self._seen_signal and now - self._black_since >= BLACK_REOPEN_SECONDS
                  and now - self._last_reopen >= 8.0):
                # 只有「本来有画面、后来掉成黑」才重开；从没出过画面（没信号）就别反复重连
                self._last_reopen = now
                self._notify(f"[采集卡] 「{self._name}」连续全黑约 {BLACK_REOPEN_SECONDS:.0f} 秒，正在重连设备…")
                self._drop()
                self._open()
                self._black_since = None
        else:
            self._seen_signal = True
            self._black_since = None
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
        if self._mjpg:
            return self._open_card_best()
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
        self._seen_signal = False
        try:
            if self._mjpg:
                # 采集卡用 MJPG：不少采集卡在 1080p 的默认 YUY2 下会输出纯黑（2026-11-08 实测
                # USB3 Video：YUY2 1080p 全黑，MJPG 1080p 正常）。先设 FOURCC 再设分辨率。
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc("M", "J", "P", "G"))
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

    def _open_card_best(self):
        """采集卡：默认 1920×1080@30fps；若该格式拿不到非黑画面/分辨率不对，再依次降级试。

        第一次逐个试并记住可用的组合（含帧率），之后重开直接用记住的，避免没信号时空转。
        """
        try:
            import cv2  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            self._once("no_cv2", f"[采集卡] 没有 OpenCV：{exc!r}")
            self._next_try = time.monotonic() + REOPEN_SECONDS
            return
        if self._index is None:
            self._once("no_device", "[采集卡] 没有可用的设备")
            self._next_try = time.monotonic() + REOPEN_SECONDS
            return
        if self._preferred is not None:
            variants = [self._preferred]
        else:
            variants = [("MJPG", 1920, 1080, 30), ("YUY2", 1920, 1080, 30),
                        ("MJPG", 1280, 720, 30), (None, 1280, 720, 30)]

        for fourcc, w, h, fps in variants:
            try:
                cap = cv2.VideoCapture(self._index, cv2.CAP_DSHOW)
            except Exception:
                continue
            if not cap.isOpened():
                try:
                    cap.release()
                except Exception:
                    pass
                continue
            try:
                if fourcc:
                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
                if fps:
                    cap.set(cv2.CAP_PROP_FPS, fps)
            except Exception:
                pass
            for _ in range(FLUSH_FRAMES):
                try:
                    cap.read()
                except Exception:
                    break
            frame = None
            all_black = True
            for _ in range(12):   # 读约半秒，等格式/信号稳定
                try:
                    ok, f = cap.read()
                except Exception:
                    f = None
                if f is not None:
                    frame = f
                    try:
                        sm = f[::8, ::8]
                        if not (float(sm.mean()) < BLACK_MEAN and float(sm.std()) < BLACK_STD):
                            all_black = False
                            break
                    except Exception:
                        all_black = False
                        break
            # 校验分辨率：请求 1080 时必须真拿到 ~1080，否则换下一个（避免分辨率不对）
            try:
                aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or w
                ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or h
            except Exception:
                aw, ah = w, h
            bad_size = (w >= 1900 and (aw < 1900 or ah < 1000))
            if frame is None or all_black or bad_size:
                try:
                    cap.release()
                except Exception:
                    pass
                continue
            self._cap = cap
            self._seen_signal = True
            self._card_down = False       # 有信号了：解除「没信号」状态，下次掉线才会再记一行
            self._preferred = (fourcc, w, h, fps)
            self._width, self._height = aw, ah
            afps = 0
            try:
                afps = round(float(cap.get(cv2.CAP_PROP_FPS)) or (fps or 0))
            except Exception:
                afps = fps or 0
            self._notify(f"[采集卡] 已连接「{self._name}」（设备 {self._index}）"
                         f" {aw}×{ah}@{afps} 格式 {fourcc or '默认'}")
            return

        self._cap = None
        # 记住的组合可能已失效（重插 / 重新协商后不再支持该格式）：复位，下一轮重新逐个变体探测，
        # 否则会一直只试那一个组合、永远失败，自动恢复形同失效（只能重启 Evo）。
        self._preferred = None
        # 只在「有信号 → 没信号」这一刻记一行；没信号期间每次 2 秒的重试不再刷日志
        if not self._card_down:
            self._card_down = True
            self._notify(f"[采集卡] 未拿到画面「{self._name}」（没信号/被占用？）"
                         f"{REOPEN_SECONDS:.0f} 秒后重试")
        self._next_try = time.monotonic() + REOPEN_SECONDS

    def _drop(self):
        cap, self._cap = self._cap, None
        self._width = self._height = 0
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def _notify(self, message):
        """连接/断开/重连这类事件：既给记录器日志，也写进 cloud.log（用户能在日志里看到状态）。"""
        try:
            self._log(message)
        except Exception:
            pass
        try:
            from cloud import state as cloud_state  # noqa: PLC0415
            cloud_state.log(message)
        except Exception:
            pass

    def _once(self, key, message):
        """同一类提示只说一次，别每 0.3 秒刷一行。"""
        if key in self._warned:
            return
        self._warned.add(key)
        self._notify(message)


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


def _non_obs_devices(devices):
    return [d for d in (devices or []) if "obs virtual camera" not in (d.get("name") or "").lower()]


def pick_card(devices, wanted=""):
    """挑采集卡设备：优先用户选的；否则优先名字像采集卡的（usb/video/capture/hdmi/采集），
    再不行才用第一台非 OBS 设备（避免默认选到 Integrated Camera 这种摄像头）。"""
    devices = devices or []
    if wanted:
        low = wanted.strip().lower()
        for d in devices:
            if (d.get("name") or "").lower() == low or (d.get("path") or "").lower() == low:
                return d
    cards = _non_obs_devices(devices)
    if not cards:
        return None

    def rank(d):
        name = (d.get("name") or "").lower()
        if any(k in name for k in ("usb", "video", "capture", "hdmi", "采集", "game")):
            return 0
        if "camera" in name or "webcam" in name or "摄像头" in name:
            return 2
        return 1

    return min(cards, key=rank)


def open_card(log=print, wanted=""):
    """打开采集卡（直读），返回 (相机, 说明)。找不到设备也返回一个「空相机」。"""
    try:
        devices = devices_mod.list_devices()
    except Exception as exc:  # noqa: BLE001
        log(f"[采集卡] 枚举视频设备失败：{exc!r}")
        devices = []
    target = pick_card(devices, wanted)
    if target is None:
        log("[采集卡] 没找到可用的采集卡设备（先装好采集卡/驱动）")
        # mjpg=True：让空相机走 [采集卡] 的提示文案，而不是误报「[OBS 虚拟摄像头] 没启动？」（2026-11-08 修复）
        return DirectShowCamera(None, "", log=log, mjpg=True), {"截法": "directshow", "directshow": "设备未找到", "设备": ""}
    # 采集卡用 MJPG：这块 USB3 Video 在 1920×1080 默认 YUY2 下输出纯黑，MJPG 才有画面（2026-11-08 实测）
    return (DirectShowCamera(target["index"], target["name"], log=log, mjpg=True),
            {"截法": "directshow", "directshow": None, "设备": target["name"]})


def describe_card(wanted=""):
    """给设置页用：采集卡设备能不能找到。不打开设备。"""
    try:
        devices = devices_mod.list_devices()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "device": "", "index": None, "reason": f"枚举视频设备失败：{exc}",
                "obs_running": _obs_running(), "devices": []}
    cards = _non_obs_devices(devices)
    target = pick_card(devices, wanted)
    if target is None:
        return {"available": False, "device": "", "index": None,
                "reason": "没找到采集卡设备（先装好采集卡和驱动，或在下面手动选）。",
                "obs_running": _obs_running(), "devices": cards}
    return {"available": True, "device": target["name"], "index": target["index"], "reason": "",
            "obs_running": _obs_running(), "devices": cards}


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
