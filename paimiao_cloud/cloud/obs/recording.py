"""录像在主机模式下的适配（2026-11-23，合并上游 0.7.2 的录像时加；纯注入，上游文件一个字节都不动）。

上游的录像（`code/match_recording.py` + `code/recorder.py`）默认「只抓游戏窗口」：
`match_recording.apex_pid()` 从进程列表里找 `r5apex.exe`，`recorder.find_window(pid)` 找它最大的窗口，
交给 ffmpeg 的 `gfxcapture`（Windows Graphics Capture）。主机模式下电脑上**没有 Apex 进程**：
找不到窗口 → 上游退回「桌面复制」（`ddagrab`，录第 1 块屏）—— 录到的是电脑桌面，不是游戏。

按画面来源分三种（`cloud/obs/settings.py`）：

- **整屏**（PC 玩家，默认）：什么都不改，和上游一模一样。
- **OBS 投影**：游戏画面就在 OBS 的投影窗口里。把「Apex 的进程号」换成一个标记 `PROJECTOR`，
  `recorder.find_window(PROJECTOR)` 返回投影窗口的句柄（`projector.find_projector_window()`），
  ffmpeg 照旧用 gfxcapture 抓那个窗口：抓的是窗口自己的内容，投影被别的窗口挡住也录得到。
  投影窗口重建了（OBS 重新打开投影）：上游断了重开时会重新调 find_window，自然拿到新的句柄。
  声音：电脑上没有 Apex 进程可以按进程录。设置里选的是「只录 Apex」时改成「电脑上所有的声音」
  （OBS 投影放出来的游戏声音就在里面），日志里说一次；选了「电脑上所有的声音」、麦克风照旧。
- **OBS 虚拟摄像头 / 采集卡**：画面不在任何窗口里，Evo 是用 OpenCV（DirectShow）直接读设备的。
  录像用的精简版 ffmpeg 没有 DirectShow 输入（`tools/build_ffmpeg.py` 只开了 gfxcapture / ddagrab），
  设备也已经被记录程序占着。这两种来源**先不录**：开录时抛 RecordingUnavailable，
  上游会在日志里写一次「录像：录不了（……）」，不会每局刷屏。

挂法：`cloud/importhook.py` 等 `match_recording` 导入后包 `MatchRecorder.__init__`（`find_pid` 是构造参数的
默认值，导入后再改模块里的 apex_pid 不生效）和 `recorder.find_window`、`recorder.Recording.start`。
降级：任何一步失败只写日志，录像照上游的行为走（PC 玩家完全不受影响）。
"""
from __future__ import annotations

PROJECTOR = -0x0B5  # 「OBS 投影」的假进程号：只在 find_window 里认它，绝不会是真的进程号
MARK = "_paimiao_obs_recording"
_INSTALLED = False
_said = set()


def _log(message, log=None):
    if message in _said:
        return
    _said.add(message)
    for out in (log,):
        if out is not None:
            try:
                out(message)
                return
            except Exception:
                pass
    try:
        from cloud import state as cloud_state  # noqa: PLC0415
        cloud_state.log(message)
    except Exception:
        pass


def _source():
    try:
        from cloud.obs import settings as obs_settings  # noqa: PLC0415
        return obs_settings.source()
    except Exception:
        return "screen"


def projector_hwnd():
    """OBS 投影窗口的句柄，找不到返回 None。"""
    try:
        from cloud.obs import projector  # noqa: PLC0415
        found = projector.find_projector_window()
    except Exception:
        return None
    return (found or {}).get("hwnd")


def install():
    """登记「等 match_recording 导入后挂上」。幂等。"""
    global _INSTALLED
    if _INSTALLED:
        return False
    _INSTALLED = True
    try:
        from cloud import importhook  # noqa: PLC0415
        importhook.on_import("match_recording", _after_import, label="主机模式录像")
    except Exception as exc:  # noqa: BLE001
        _INSTALLED = False
        _log(f"[主机模式录像] 挂不上（录像照上游的行为）：{exc!r}")
        return False
    return True


def _after_import(module):
    try:
        import recorder  # noqa: PLC0415  match_recording 已经 import 过它
    except Exception as exc:  # noqa: BLE001
        _log(f"[主机模式录像] 找不到 recorder（录像照上游的行为）：{exc!r}")
        return
    ok = [_patch_find_window(recorder), _patch_start(recorder), _patch_matchrecorder(module)]
    if all(ok):
        _log("[主机模式录像] 已接管：选「OBS 投影」时录投影窗口；虚拟摄像头 / 采集卡先不录")


def _patch_find_window(recorder):
    real = getattr(recorder, "find_window", None)
    if real is None:
        _log("[主机模式录像] recorder.find_window 不见了（上游改了？），投影录不了")
        return False
    if getattr(real, MARK, False):
        return True

    def find_window(pid):
        if pid == PROJECTOR:
            return projector_hwnd()
        return real(pid)

    setattr(find_window, MARK, True)
    find_window.__wrapped__ = real  # type: ignore[attr-defined]
    recorder.find_window = find_window
    return True


def _patch_start(recorder):
    cls = getattr(recorder, "Recording", None)
    real = getattr(cls, "start", None)
    if real is None:
        _log("[主机模式录像] recorder.Recording.start 不见了（上游改了？）")
        return False
    if getattr(real, MARK, False):
        return True
    unavailable = getattr(recorder, "RecordingUnavailable", RuntimeError)

    def start(self, apex_pid=None):
        source = _source()
        if source in ("camera", "card"):
            what = "OBS 虚拟摄像头" if source == "camera" else "采集卡"
            raise unavailable(f"画面来源是{what}，录像暂时只支持整屏和 OBS 投影")
        if apex_pid == PROJECTOR:
            opts = self.options
            if getattr(opts, "source", "window") != "window":
                pass  # 设置里改成了桌面复制、测试画面：照设置来
            elif projector_hwnd() is None:
                raise unavailable("找不到 OBS 的投影窗口")
            if getattr(opts, "audio_source", "game") == "game":
                opts.audio_source = "system"
                _log("[主机模式录像] 电脑上没有 Apex 进程，声音改录电脑上所有的声音（OBS 投影的声音在里面）",
                     getattr(self, "log", None))
        return real(self, apex_pid)

    setattr(start, MARK, True)
    start.__wrapped__ = real  # type: ignore[attr-defined]
    cls.start = start
    return True


def _patch_matchrecorder(module):
    cls = getattr(module, "MatchRecorder", None)
    real_init = getattr(cls, "__init__", None)
    if real_init is None:
        _log("[主机模式录像] match_recording.MatchRecorder 不见了（上游改了？）")
        return False
    if getattr(real_init, MARK, False):
        return True

    def __init__(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        upstream_find = self.find_pid

        def find_pid():
            # PC 玩家（整屏）和 Apex 真的在这台电脑上开着时：照上游
            pid = upstream_find()
            if pid or _source() != "projector":
                return pid
            return PROJECTOR

        self.find_pid = find_pid

    setattr(__init__, MARK, True)
    __init__.__wrapped__ = real_init  # type: ignore[attr-defined]
    cls.__init__ = __init__
    return True
