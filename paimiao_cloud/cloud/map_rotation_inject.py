"""地图轮换：独立页面 + 导航项 + 数据接口。失败只写日志，Evo 照常起。

bootstrap.attach 里调用；幂等（模块标记位 + 蓝图判断 + NAV 里已有就不插）。
"""
from __future__ import annotations


def register(app, module=None):
    try:
        from cloud.map_rotation_ui import register as _register
        _register(app, module)
    except Exception as exc:  # noqa: BLE001
        try:
            from cloud.state import log
            log(f"[地图轮换] 注入失败（Evo 本身不受影响）：{exc!r}")
        except Exception:  # noqa: BLE001
            pass
