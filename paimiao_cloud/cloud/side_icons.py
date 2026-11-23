"""侧边栏导航图标：给注入层加的导航项（派喵云同步、榜单、地图轮换……）补上图标（2026-11-23，合并上游 0.7.2 时加）。

上游 0.7.x 把顶部导航换成了左边的侧边栏（`panel/templates/_side.html`），每一项的图标写死在模板开头的
`nav_icons` 字典里，只有上游自己的几项。注入层往 `panel_app.NAV` 里加的项查不到图标：展开时只是少个图标，
收起时（只显示图标）就是一块空白。

做法（纯注入，`_side.html` 一个字节都不动）：在 `app.jinja_env` 上叠一层 preprocess，把

    {{ nav_icons[endpoint]|safe }}

换成「上游的字典里有就用上游的，没有就用 `paimiao_nav_icons`（这里的 ICONS），再没有就用一个圆点」。
图标的格式和上游一样：24×24 画布里的几段 SVG（线条，`stroke="currentColor"`，颜色跟着主题）。

降级：锚点找不到（上游改了 `_side.html`）就原样返回、只写一次日志；侧边栏照常显示，只是我们的项没图标。
"""
from __future__ import annotations

TEMPLATE = "_side.html"
ANCHOR = "{{ nav_icons[endpoint]|safe }}"
REPLACEMENT = ("{{ (nav_icons[endpoint] if endpoint in nav_icons "
               "else paimiao_nav_icons.get(endpoint, paimiao_nav_icon_default))|safe }}")
MARK = "_paimiao_side_icons_preprocess"

# endpoint → SVG 的内容（和上游 nav_icons 同样的写法）
ICONS = {
    # 派喵云同步：一朵云
    "cloud.page": '<path d="M7 18.5h10a4 4 0 0 0 .6-7.95A5.5 5.5 0 0 0 7.1 9.6 4.5 4.5 0 0 0 7 18.5Z"></path>',
    # 榜单：领奖台
    "pickrate.page": '<path d="M9 20.5V9.5h6v11"></path><path d="M3.5 20.5v-6h5.5"></path>'
                     '<path d="M15 13.5h5.5v7"></path><path d="M3 20.5h18"></path>',
    # 地图轮换：折叠的地图
    "maprotation.page": '<path d="M3.5 6.5 9 4l6 2.5 5.5-2.5v13.5L15 20l-6-2.5-5.5 2.5Z"></path>'
                        '<path d="M9 4v13.5"></path><path d="M15 6.5V20"></path>',
}
DEFAULT_ICON = '<circle cx="12" cy="12" r="3.5"></circle>'  # 以后再加的导航项：先有个圆点，不至于空着

_said = set()


def _log(message):
    if message in _said:
        return
    _said.add(message)
    try:
        from cloud.state import log
        log(f"[侧边栏图标] {message}")
    except Exception:
        pass


def patch_source(source, name):
    """只改 _side.html；找不到锚点原样返回。"""
    if not (name or "").endswith(TEMPLATE):
        return source
    if ANCHOR not in source:
        if REPLACEMENT not in source:
            _log(f"{TEMPLATE} 里没找到图标那一句，注入的导航项不会有图标（上游改了侧边栏？）")
        return source
    return source.replace(ANCHOR, REPLACEMENT, 1)


def install(env):
    """挂到 jinja_env 上。幂等：挂过就不再挂。返回这次挂上了没有。"""
    if env is None or getattr(env, MARK, False):
        return False
    original = env.preprocess

    def preprocess(source, name, filename=None):
        try:
            source = original(source, name, filename)
        except TypeError:
            source = original(source, name)
        try:
            return patch_source(source, name)
        except Exception as exc:  # noqa: BLE001  出错就用原样的模板
            _log(f"改写 {TEMPLATE} 出错：{exc!r}")
            return source

    env.preprocess = preprocess
    env.globals["paimiao_nav_icons"] = dict(ICONS)
    env.globals["paimiao_nav_icon_default"] = DEFAULT_ICON
    setattr(env, MARK, True)
    return True
