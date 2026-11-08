"""通知的 Windows 托盘发送线程（不 import Flask）。

单独成文件的原因：这个线程要在**托盘进程**里、面板被打开之前就能起来
（开机自启 `--background` 时面板可能永远不打开）。而 `cloud/notify/inject.py`
顶层 import 了 Flask，如果把线程逻辑放那儿，为了起线程就得把 Flask 带进
轻量的记录子进程（违背「轻量」原则）。本文件只依赖 fetch / store / win，
都是标准库级别的模块，没有 Flask。

启动入口（两处，都幂等）：
- `cloud/notify/boot.py`：托盘 `pystray.Icon.run` 时启动（后台模式唯一的时机）；
- `cloud/notify/inject.register()`：用户打开面板时补一次。
"""
from __future__ import annotations

import threading
import time

from cloud.notify import fetch, store, win

_worker_started = False


def fire_windows_notifications() -> bool:
    """把未发过的 Windows 通知发出去。返回 True = 本轮没剩下要发的（图标没就绪时返回 False）。"""
    pending_left = False
    sent = 0
    for item in fetch.cached_items():
        if not isinstance(item, dict) or not item.get("windowsNotify"):
            continue
        nid = item.get("id")
        if not nid or store.win_seen(nid):
            continue
        if sent >= 3:
            break
        title = item.get("title") or "Evo 通知"
        body = item.get("body") or title
        if win.notify(title, body):
            store.mark_win_seen(nid)
            sent += 1
            try:
                from cloud import state as cloud_state  # noqa: PLC0415
                cloud_state.log(f"[通知] 已发 Windows 通知：{title}")
            except Exception:
                pass
        else:
            pending_left = True
            break
    return not pending_left


def _worker():
    # 首轮：拉取 + 发 Windows 通知（托盘图标可能还没建好，给它最多 ~2 分钟）
    try:
        fetch.refresh()
    except Exception:
        pass
    for _ in range(40):
        if fire_windows_notifications():
            break
        time.sleep(3)
    # 之后每 30 分钟刷一次
    while True:
        time.sleep(30 * 60)
        try:
            fetch.refresh()
            fire_windows_notifications()
        except Exception:
            pass


def start() -> None:
    """启动发送线程（幂等：重复调用只起一次）。"""
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    try:
        threading.Thread(target=_worker, name="evo-notify", daemon=True).start()
    except Exception:
        pass
