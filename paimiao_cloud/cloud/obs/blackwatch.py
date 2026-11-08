"""检测抓到的画面是不是全黑，给用户提示用（2026-11-08 用户要求）。

记录子进程每抓一帧就调 `note_frame(frame)`：连续全黑超过 HOLD_SECONDS 秒就写一份状态文件
（放 Evo 数据目录的 obs_capture_state.json）+ 写一行日志；画面恢复后再写一次。
设置页 / 托盘读 `status()` 显示提示（「最近画面全黑，检查虚拟摄像头 / 采集卡 / 游戏有没有在抓」）。

为什么放状态文件而不是直接弹窗：记录在**子进程**里跑，碰不到托盘的图标；写文件让主进程
（设置页、托盘）去看，简单可靠，也不扩大联网/权限。
"""
from __future__ import annotations

import json
import os
import threading
import time

BLACK_MEAN = 10.0      # 平均亮度低于它
BLACK_STD = 6.0        # 且标准差也低（纯色黑）
HOLD_SECONDS = 5.0     # 连续全黑这么久才提示（避免一闪而过的黑场）
RECENT_SECONDS = 60.0  # 状态超过这么久没更新就不算「最近全黑」了

_lock = threading.Lock()
_black_since = None
_warned = False
_clear_check = {"t": 0.0}
# 持续全黑时周期性刷新状态文件里 at 的时刻（见 note_frame）。
_last_refresh = {"t": 0.0}
# 持续全黑期间最多每隔这么久刷新一次 at，保证 status() 的 RECENT_SECONDS 判活不会误判。
REFRESH_SECONDS = 2.0


def _read_black() -> bool:
    path = _path()
    if not path:
        return False
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return bool(json.load(fh).get("black"))
    except (OSError, ValueError):
        return False


def _path():
    try:
        from cloud import state as cloud_state  # noqa: PLC0415
        return os.path.join(str(cloud_state.data_dir()), "obs_capture_state.json")
    except Exception:
        return None


def _write(black: bool, since, now: float) -> None:
    path = _path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"black": bool(black), "since": since, "at": now}, fh)
    except Exception:
        pass


def is_black(frame) -> bool:
    """整帧是否接近全黑。抽样计算，别为 1080p 每帧算全量。"""
    try:
        small = frame[::8, ::8]
        return float(small.mean()) < BLACK_MEAN and float(small.std()) < BLACK_STD
    except Exception:
        return False


def note_frame(frame) -> None:
    """每帧调用。连续全黑到阈值才提示一次；恢复时再记一次。"""
    global _black_since, _warned
    if frame is None:
        return
    now = time.time()
    black = is_black(frame)
    with _lock:
        if black:
            if _black_since is None:
                _black_since = now
            elif not _warned and now - _black_since >= HOLD_SECONDS:
                _warned = True
                _last_refresh["t"] = now
                _write(True, _black_since, now)
                _log("⚠ 画面已连续全黑 "
                     f"{HOLD_SECONDS:.0f} 秒：可能是 OBS 虚拟摄像机没开、采集卡没信号、或游戏没在抓。"
                     "记录会继续，但这段时间识别不到内容。")
            elif _warned and now - _last_refresh["t"] >= REFRESH_SECONDS:
                # 2026-11-08 修复：原来只在跨过 HOLD_SECONDS 那一刻写一次状态文件，
                # 文件里的 at 就冻结在那一刻；画面一直全黑约 RECENT_SECONDS(60) 秒后
                # status() 会因 now-at>60 判过期，返回 black=False —— 提示自己消失，
                # 用户以为已恢复。持续全黑期间周期性刷新 at，让「最近全黑」始终成立。
                _last_refresh["t"] = now
                _write(True, _black_since, now)
        else:
            was = _warned
            _black_since, _warned = None, False
            if was:
                _write(False, None, now)
                _log("画面恢复正常")
            elif now - _clear_check["t"] >= 2.0:
                # 记录进程重启后内存状态会丢，但文件里可能还留着 black=true；这里顺带清掉，
                # 否则设置页会一直显示“全黑”。（2026-11-08 用户遇到）
                _clear_check["t"] = now
                try:
                    if _read_black():
                        _write(False, None, now)
                except Exception:
                    pass


def status() -> dict:
    """最近一次检测结果：{"black": bool, "since": 秒, "at": 秒}。过期就当没黑。"""
    path = _path()
    if not path:
        return {"black": False}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"black": False}
    black = bool(data.get("black"))
    at = float(data.get("at") or 0)
    if black and time.time() - at > RECENT_SECONDS:
        return {"black": False}
    return {"black": black, "since": data.get("since"), "at": at}


def _log(message: str) -> None:
    try:
        from cloud import state as cloud_state  # noqa: PLC0415
        cloud_state.log(message)
    except Exception:
        try:
            print(message)
        except Exception:
            pass
