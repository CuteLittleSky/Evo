"""「榜单」页：把我们自己的传奇选取率接口渲染成一张表。

## 为什么是独立一页，而不是总览页插一张卡

1. **上游的「数据」页永远是「我的/队友/全体玩家」**，键在服务器上。往里塞
   「全体玩家」的数据就得给一张表做两套列、两套排序、两套口径说明，
   而这里 8 个段位档 × 3 个可选列，本来就该占满一页。
2. 总览页是「这一局/今天」的仪表盘，选取率是「赛季大盘」，刷新节奏差一个量级
   （30 分钟 vs 每局），混在一起会让总览页每次打开都多等一次网络。
3. 独立页 = 独立 Blueprint，挂不上就整块消失，上游一行都不受影响。

## 复用上游样式（不新增一行 CSS 到 style.css）

表格直接沿用 `panel/static/style.css` 里「数据页」那一套现成零件：
`table.stats-table`、`thead th`、`tbody tr` 分隔线、`td.right`、`.legend-icon`
（`_legend.html` 的 `legend_icon()` 宏）、`.share/.bar/.share-fill`（横条 + 数字）。
本页只在自己的 `<style>` 里补三样最小的东西（职业标签的底色、走势箭头颜色、
段位条折行），因为上游没有「职业标签」这个零件。

## 渲染期取数

数据一律在 `app_context_processor` 里算（不是 preprocess）：Jinja 会缓存编译结果，
`preprocess` 一辈子只跑一次，而这里每次打开都要拿最新的。
"""
from __future__ import annotations

import time

from flask import Blueprint, current_app, make_response, render_template, request

from cloud import state as cloud_state  # noqa: PLC0415
from cloud.pickrate import fetch, roles

bp = Blueprint("pickrate", __name__, url_prefix="/pickrate")

# 导航标签（用户 2026-10-25 定）
NAV_LABEL = "榜单"

# 导航里的位置：插在「设置」前面。找不到「设置」就接在最后（照 `panel.py` 的 nav 套路）。
_ANCHOR_ENDPOINT = "settings"


def _log(message):
    try:
        cloud_state.log("[榜单] %s" % message)
    except Exception:
        pass


# ---------------------------------------------------------------- 数据整理


def _fmt_pct(value):
    """选取率 → 「12.3%」。None / 非数字 → 空串（**不能当 0**）。"""
    if not isinstance(value, (int, float)):
        return ""
    return "%.1f%%" % value


def _fmt_num(value):
    if not isinstance(value, (int, float)):
        return ""
    return "%.1f" % value


def _fmt_int(value):
    """平均段位：接口给的是字符串（''、'15'、'15000' 都可能），原样显示，空就是空。"""
    text = "" if value is None else str(value).strip()
    return text


def _arrow(trend_dir, trend):
    """走势：↑(+0.4) / ↓(-0.3) / —（平的、或者没有这个数）。"""
    if trend_dir == "up":
        mark = "↑"
    elif trend_dir == "down":
        mark = "↓"
    else:
        mark = "—"
    if mark != "—" and isinstance(trend, (int, float)):
        return "%s %.2f" % (mark, trend)
    return mark


def _rows(legends):
    """接口的 legends[] → 模板要的形状。只读我们真的用到的字段。"""
    rows = []
    top = 0.0
    for item in legends:
        rate = item.get("pickRate")
        if isinstance(rate, (int, float)) and rate > top:
            top = rate
    for item in legends:
        rate = item.get("pickRate")
        row = {
            "rank": item.get("rank") or "",
            "name": item.get("nameZh") or item.get("name") or "",
            "en": item.get("name") or "",
            "icon": None,
            "pick": _fmt_pct(rate),
            # 横条长度按「最多那个占满」算，和上游「数据」页的 `.pick-bar` 一个口径
            "bar": round(float(rate) / top * 100, 1) if top and isinstance(rate, (int, float)) else 0,
            "trend": _arrow(item.get("trendDir"), item.get("trend")),
            "trend_dir": item.get("trendDir") or "flat",
            "avg_rank": _fmt_int(item.get("avgRank")),
            "role_label": item.get("roleLabel") or roles.label(item.get("roleKey")),
            "role_key": item.get("roleKey") or "",
            "role_color": item.get("roleColor") or roles.color(item.get("roleKey")),
            "win": _fmt_pct(item.get("winRate")),
            "kpm": _fmt_num(item.get("kpm")),
        }
        rows.append(row)
    return rows


def _icons(app_module):
    """中文名 → static 里的 SVG 路径。直接借上游的字典（`panel_app.LEGEND_ICONS`）。"""
    table = getattr(app_module, "LEGEND_ICONS", None)
    return table if isinstance(table, dict) else {}


def _column_present(rows, key):
    """这一列有没有值。整列都是空就隐藏该列（新版窄屏也一样）。"""
    return any(row.get(key) for row in rows)


def _stamp(millis):
    """时间戳（毫秒）→ 「10-25 19:07」。接口和本地缓存都给毫秒；没有就空。"""
    if not isinstance(millis, (int, float)) or millis <= 0:
        return ""
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(millis / 1000.0))
    except Exception:
        return ""


# ---------------------------------------------------------------- 视图


# ⚠ 只注册 ""：url_for 会生成 `/pickrate?tier=…`（与上游 `/stats` 的写法一致）。
# 再加一条 `/` 会让 url_for 选到 `/pickrate/?tier=…`，多一个斜杠还会让 Flask 走 308 跳转。
@bp.route("")
def page():
    """渲染页面。

    ⚠ 必须用 `flask.render_template` 而不是 `jinja_env.get_template(...).render()`：
    前者才会带上 app / Blueprint 的上下文处理器（`base.html` 里的 `account_view`、
    `nav`、`token` 全在 `panel_app.template_globals()` 里出的）。手搓 Jinja 渲染
    会在 `base.html:65` 炸 `UndefinedError`，而且**只在真实请求里炸**，
    单测里直接 render 模板是看不出来的。
    """
    tier = (request.args.get("tier") or fetch.DEFAULT_TIER).strip()
    if tier not in fetch.TIER_KEYS:
        tier = fetch.DEFAULT_TIER
    resp = make_response(render_template("pickrate_page.html"))
    # 榜单是「半小时才变一次」的东西，别让浏览器缓存出交叉数据
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.app_context_processor
def inject():
    """每次渲染都算一遍（含切换段位）。出错一律降级成一句话，绝不让面板白屏。"""
    module = getattr(current_app, "_paimiao_panel_module", None)
    if module is None:
        module = _module_from_blueprint()
    icon_map = _icons(module)

    tier = (request.args.get("tier") or fetch.DEFAULT_TIER).strip()
    if tier not in fetch.TIER_KEYS:
        tier = fetch.DEFAULT_TIER

    tab_tier = [
        {"key": key, "label": label, "on": key == tier} for key, label in fetch.TIERS
    ]

    ctx = {
        "pick_tier": tier,
        "pick_tabs": tab_tier,
        "pick_rows": [],
        "pick_error": None,
        "pick_has_win": False,
        "pick_has_kpm": False,
        "pick_has_avg": False,
        "pick_stale": False,
        "pick_updated": "",
    }

    reason = fetch.unavailable_reason()
    if reason:
        ctx["pick_error"] = reason
        return ctx

    try:
        data = fetch.snapshot(tier)
    except fetch.Unavailable as exc:
        ctx["pick_error"] = str(exc)
        return ctx
    except Exception as exc:  # 注入层自己的 bug，也不能让页面挂
        _log("取数异常：%r" % exc)
        ctx["pick_error"] = "取数时出错：%s" % exc
        return ctx

    rows = _rows(data["legends"])
    for row in rows:
        row["icon"] = icon_map.get(row["name"])

    ctx["pick_rows"] = rows
    ctx["pick_stale"] = bool(data.get("stale"))
    ctx["pick_updated"] = _stamp(data.get("updated_at") or data.get("fetched_at"))
    ctx["pick_has_win"] = _column_present(rows, "win")
    ctx["pick_has_kpm"] = _column_present(rows, "kpm")
    ctx["pick_has_avg"] = _column_present(rows, "avg_rank")
    if data.get("stale") and data.get("error"):
        ctx["pick_error"] = "这次没取到最新数据（%s），下面是 %s 的" % (
            data["error"],
            ctx["pick_updated"] or "上一次",
        )
    return ctx


def _module_from_blueprint():
    """上游的 `panel_app` 模块。`bootstrap.attach()` 会把它记在 Blueprint 上。"""
    return getattr(bp, "_paimiao_module", None)


def register(app):
    """注册 Blueprint（幂等）+ 记下上游模块，好取 `LEGEND_ICONS`。"""
    module = getattr(app, "_paimiao_panel_module", None)
    if module is not None:
        bp._paimiao_module = module  # noqa: SLF001  注入层内部约定
    if app.blueprints.get(bp.name):
        return
    app.register_blueprint(bp)
    _log("已挂上「%s」页：/pickrate" % NAV_LABEL)
