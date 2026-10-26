"""地图轮换：独立一页（导航「地图轮换」，挂在「榜单」后面）+ 网页取数口。

用户 2026-10-26 定的形态：**和「派喵云同步」一样是单独一页**，不加到总览页。
所以这里不再往任何上游模板里插卡片，只做三件事：

  ① Blueprint `/maprotation`：页面本身（模板 `cloud/templates/map_rotation.html`）
  ② 导航项：改上游 `panel_app.NAV`，插在 `pickrate.page`（榜单）后面
     —— 和 `cloud/pickrate/inject.py` 同一个套路，不碰 `base.html`
  ③ 数据接口 `/maprotation/api.json`：页面里的 JS 定时来拿。上游 API 没有 CORS 头，
     浏览器直连会被拦，所以由 `cloud/map_rotation.py` 的后台线程去拉，这里只读内存。

上游 `panel/` 一个字节不动；挂不上就整页消失（导航里没有、路径 404），Evo 照常起。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template

from cloud import map_rotation

bp = Blueprint("maprotation", __name__, url_prefix="/maprotation")

# 导航标签（和「榜单」「派喵云同步」一样短）
NAV_LABEL = "地图轮换"

# 导航里插在谁后面：我们自己的「榜单」页。找不到就接在最后。
_AFTER_ENDPOINT = "pickrate.page"


@bp.app_context_processor
def inject():
    return {"mr": _safe_view()}


def _safe_view() -> dict:
    """渲染期炸了整个页面就白屏了：任何异常都降级成「取不到」。"""
    try:
        return map_rotation.view()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": "地图轮换读不出来", "detail": repr(exc),
                "modes": [], "season": None, "age_mins": 0, "page_url": map_rotation.PAGE_URL}


@bp.get("")
def page():
    return render_template("map_rotation.html", title="地图轮换")


@bp.get("/api.json")
def api():
    """网页侧的取数口。不缓存（轮换随时会变），失败也回 200 + available:false。"""
    response = jsonify(_safe_view())
    response.headers["Cache-Control"] = "no-store"
    return response


def _patch_nav(module) -> bool:
    """把「地图轮换」插进上游 NAV（在「榜单」后面）。

    上游 NAV 是元组 `(("overview", "总览"), ..., ("settings", "设置"))`，
    这里**换成一个新元组**赋回去（元组不能原地改），每一项内容保持原样。
    """
    nav = getattr(module, "NAV", None)
    if not nav:
        return False
    try:
        items = [tuple(pair) for pair in nav]
    except Exception:  # noqa: BLE001
        return False
    endpoint = "%s.page" % bp.name
    if any(pair and pair[0] == endpoint for pair in items):
        return False
    index = len(items)
    for pos, pair in enumerate(items):
        if pair and pair[0] == _AFTER_ENDPOINT:
            index = pos + 1
            break
    # 「榜单」没挂上时接在最后，但别落到「设置」后面去（设置习惯压轴）
    for pos, pair in enumerate(items):
        if pair and pair[0] == "settings" and index > pos:
            index = pos
            break
    items.insert(index, (endpoint, NAV_LABEL))
    module.NAV = tuple(items)
    return True


def register(app, module=None) -> None:
    """幂等：重复调用不会重复注册蓝图 / 重复插导航。"""
    try:
        from cloud.state import log
    except Exception:  # noqa: BLE001
        log = lambda _m: None  # noqa: E731

    if module is None:
        module = getattr(app, "_paimiao_panel_module", None)
    if module is not None:
        app._paimiao_panel_module = module

    if not app.blueprints.get(bp.name):
        app.register_blueprint(bp)
        log("[地图轮换] 已挂上页面：/maprotation")

    # 模板目录：正常启动时 bootstrap 已经把它拼进 jinja_loader 了；单独注册（自检、
    # 别的入口）时这里补一次 —— 幂等，重复拼不会套两层。
    try:
        from cloud.templates import splice_loader
        import os
        ours = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
        loader = app.jinja_loader
        dirs = list(getattr(loader, "searchpath", []) or [])
        if ours not in dirs:
            app.jinja_loader = splice_loader(loader, ours)
    except Exception as exc:  # noqa: BLE001
        log("[地图轮换] 模板目录没拼上（页面可能打不开）：%r" % exc)

    # daemon 线程，Evo 关了就走；起不来只影响这一页的数据，不影响开页
    try:
        map_rotation.start()
    except Exception:  # noqa: BLE001
        log("[地图轮换] 后台取数线程没起来（页面照常能开）")

    if module is None:
        log("[地图轮换] 拿不到上游 panel_app，导航项没加上（页面本身还在）")
        return
    try:
        if _patch_nav(module):
            log("[地图轮换] 导航已加「%s」（在「榜单」后面）" % NAV_LABEL)
    except Exception as exc:  # noqa: BLE001
        log("[地图轮换] 导航项没加上（页面本身还在）：%r" % exc)
