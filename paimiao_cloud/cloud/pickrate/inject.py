"""把「榜单」页挂进上游面板：注册 Blueprint、加导航项、借传奇头像表。

## 挂什么

1. **Blueprint** `/pickrate`（`cloud/pickrate/page.py`）—— 页面本身
2. **导航项**「榜单」：改上游 `panel_app.NAV` 那个元组，插在「设置」前面。
   `cloud/panel.py` 的 `nav_items()` 已经在用 `getattr(MODULE, "NAV")`，所以只要
   把 `panel_app.NAV` 换掉，导航栏就会多一项，**不用碰 `base.html`**。
3. **传奇头像**：上游 `panel_app.LEGEND_ICONS`（中文名 → `legends/<编号>.svg`），
   接口正好给中文名，直接查表，不自己拼文件名（编号和英文名并不是一一对应）。

## 幂等

`bootstrap.attach()` 用 `app._paimiao_pickrate_installed` 守卫，这里再自己判一次：
Blueprint 已在 `app.blueprints` 里就不重复注册；`NAV` 里已经有 `pickrate.page` 就不重复插。
重复调用 `register(app, module)` 是安全的 —— 自检脚本会故意调两次。

## 失败怎么办

`bootstrap.attach()` 里整块 try/except 兜住，只写日志（`cloud/state.py` 的 `log()`）：
挂不上 = 导航里没有「榜单」、`/pickrate` 404，**其余一切照常**，上游不受影响。
"""
from __future__ import annotations

from cloud import state as cloud_state
from cloud.pickrate import page as pickrate_page

# 导航里的插入位置：上游「设置」那一项之前
_ANCHOR_ENDPOINT = "settings"


def _log(message):
    try:
        cloud_state.log("[榜单] %s" % message)
    except Exception:
        pass


def _patch_nav(module, label):
    """把「榜单」插进上游 NAV（在「设置」前面）。

    上游 NAV 是元组 `(("overview", "总览"), ..., ("settings", "设置"))`，
    这里**换成一个新元组**赋回去（元组不可原地改），每一项的内容保持原样。
    """
    nav = getattr(module, "NAV", None)
    if not nav:
        return False
    try:
        items = [tuple(pair) for pair in nav]
    except Exception:
        return False
    endpoint = "%s.page" % pickrate_page.bp.name
    if any(pair and pair[0] == endpoint for pair in items):
        return False
    item = (endpoint, label)
    index = len(items)
    for pos, pair in enumerate(items):
        if pair and pair[0] == _ANCHOR_ENDPOINT:
            index = pos
            break
    items.insert(index, item)
    module.NAV = tuple(items)
    return True


def register(app, module=None):
    """幂等挂载。`module` 是上游 `panel_app`（取 NAV 和 LEGEND_ICONS）。"""
    if module is not None:
        app._paimiao_panel_module = module
    else:
        module = getattr(app, "_paimiao_panel_module", None)

    pickrate_page.register(app)  # 自己幂等；没拿到 module 也能挂上页面

    if module is None:
        _log("拿不到上游 panel_app，导航项没加上（页面本身还在）")
        return False
    if _patch_nav(module, pickrate_page.NAV_LABEL):
        _log("导航已加「%s」" % pickrate_page.NAV_LABEL)
    return True
