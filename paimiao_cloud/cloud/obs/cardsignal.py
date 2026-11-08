"""采集卡「记录中」的心跳与最近画面（2026-11-08 用户决定）。

主机（Switch/PS5/Xbox）用采集卡直读时，电脑上既没有 Apex 进程，也不一定有 OBS 在跑。
记录子进程每抓一帧就调用 `mark_frame()` 写一次心跳（节流 1 秒），并每隔约 2 秒调用
`save_snapshot()` 存一张最近画面（JPEG）：

    mark_frame()     记录进程存活心跳 + 是否「有画面(非黑)」；面板据此决定复用最近帧，
                     不再去打开采集卡（避免把正在记录的画面打断）
    save_snapshot()  存最近一张画面，供面板「测试截图 / 选设备」在记录中复用
    alive_age()      面板读：记录进程最近一次抓帧到现在多少秒
    read_snapshot()  面板读：最近一张画面（JPEG 字节）

注意：Evo 开着就一直记录（`trigger._card_wanted` 恒为 True），不再按「有没有信号」启停
记录子进程；采集卡没信号/掉线由 `DirectShowCamera` 自己在内部重开重试，有画面自动恢复。
"""
from __future__ import annotations

import json
import os
import time

_throttle = {"t": 0.0}
_snap_throttle = {"t": 0.0}


def snapshot_path():
    try:
        from cloud import state as cloud_state  # noqa: PLC0415
        return os.path.join(str(cloud_state.data_dir()), "obs_card_last.jpg")
    except Exception:
        return None


def save_snapshot(frame) -> None:
    """记录子进程每隔约 2 秒存一张最近画面（JPEG），给「测试截图」在记录中复用。"""
    if frame is None:
        return
    now = time.time()
    if now - _snap_throttle["t"] < 2.0:
        return
    _snap_throttle["t"] = now
    path = snapshot_path()
    if not path:
        return
    try:
        import cv2  # noqa: PLC0415
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if ok:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(buf.tobytes())
    except Exception:
        pass


def read_snapshot():
    """读最近一张记录画面（JPEG 字节）；没有/太旧返回 None。"""
    path = snapshot_path()
    if not path:
        return None
    try:
        if time.time() - os.path.getmtime(path) > 10:
            return None
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _path():
    try:
        from cloud import state as cloud_state  # noqa: PLC0415
        return os.path.join(str(cloud_state.data_dir()), "obs_card_frame.json")
    except Exception:
        return None


def _write(now: float, last_signal) -> None:
    path = _path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"at": now, "last_signal": last_signal}, fh)
    except Exception:
        pass


def _read():
    path = _path()
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def mark_frame(frame) -> None:
    """记录子进程每抓到一帧调用：更新「记录进程存活」心跳（每秒一次）。

    不管画面黑不黑都更新 at —— 这样面板能知道「记录进程正在用采集卡」，
    从而只用最近帧、不再去打开采集卡（避免把正在记录的画面打断）。
    """
    if frame is None:
        return
    now = time.time()
    if now - _throttle["t"] < 1.0:
        return
    _throttle["t"] = now
    black = False
    try:
        from cloud.obs import blackwatch  # noqa: PLC0415
        black = blackwatch.is_black(frame)
    except Exception:
        black = False
    prev = _read().get("last_signal")
    _write(now, now if not black else prev)


def alive_age():
    """记录进程最近一次抓帧到现在多少秒（None=从没记过）。"""
    at = float(_read().get("at") or 0)
    if not at:
        return None
    return time.time() - at
