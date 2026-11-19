"""把匿名统计挂到上游：包 storage.insert_match，一局入库后触发 60 秒去抖上报。"""
from __future__ import annotations

import storage

from cloud.state import log
from cloud.stats import report


def install() -> None:
    if getattr(storage, "_paimiao_stats_hooked", False):
        return
    original = storage.insert_match

    def insert_match(conn, match):
        match_id = original(conn, match)
        try:
            report.schedule(match_id)
        except Exception as exc:  # 静默降级：绝不因为它挂了影响记录主流程
            log(f"匿名统计：排程失败（{exc!r}）")
        return match_id

    storage.insert_match = insert_match
    storage._paimiao_stats_hooked = True
    log("匿名统计：已挂载（对局入库后 60 秒去抖上报）")

    # 如果已经绑定云同步：启动时报一次匿名编号，让服务端删掉之前的匿名数据（幂等）
    try:
        import threading
        threading.Thread(target=report.purge_if_bound, daemon=True).start()
    except Exception:
        pass
