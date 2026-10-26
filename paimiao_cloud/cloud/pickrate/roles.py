"""传奇职业（位置）的中文名与颜色。

## 为什么本地还要留一份

接口里每条传奇都带 `roleLabel`（「突击」这类中文名）和 `roleColor`（`#RRGGBB`），
所以**正常情况用接口的就行**。本地这份只在两种时候顶上：

1. 老服务器 / 缓存里的旧响应没有这两个字段（接口是逐渐补齐的）
2. 加载中、报错、离线时也要把表格骨架画出来，不然页面会跳一下

## 颜色从哪来

不是自己挑的：ApexPacks 服务端 `server/utils/legend-roles.ts` 从解包数据
`server/data/apex-legends/legend-roles.json` 读职业色（引擎 uiColor 换算而来），
这里的 5 个值与那边一致。改了那边就要跟着改这里，否则同一职业在两处颜色不同。

注意角色名用的是**上游 `config/legends.json` 的 `classes` 那一套**：
assault 突击 / skirmisher 游击 / recon 侦察 / support 支援 / controller 控制。
"""
from __future__ import annotations

# 职业 key → (中文名, 颜色)。顺序即展示顺序（与服务端 ROLE_ORDER 一致）。
ROLES = {
    "assault": ("突击", "#e0523f"),
    "skirmisher": ("游击", "#e0912f"),
    "recon": ("侦察", "#3f9fdf"),
    "support": ("支援", "#4fbf6f"),
    "controller": ("控制", "#b06ad8"),
}

# 取不到职业色时用它（服务端 FALLBACK_ROLE_COLOR 同值），比空白好看
FALLBACK_COLOR = "#94a3b8"


def label(role_key):
    """职业 key → 中文名。不认识就返回空串（模板里整格留空，不写「未知」）。"""
    pair = ROLES.get(role_key or "")
    return pair[0] if pair else ""


def color(role_key):
    """职业 key → 颜色。不认识给中性灰。"""
    pair = ROLES.get(role_key or "")
    return pair[1] if pair else FALLBACK_COLOR
