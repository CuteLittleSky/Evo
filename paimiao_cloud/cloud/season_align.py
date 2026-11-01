"""赛季「数据页」录不进去：两层注入修正（2026-11-02）。

上游 0.5.1 的「录入历史赛季数据」在本机踩到几处问题，这里都用注入修，**不动上游文件**：

## 问题 1：数据页常被判成「大厅」，整页被跳过

游戏里的「数据」页和「大厅」背景都是同一套六边形深色，主识别器经常把它判成 `lobby`
（`config/screens.json` 的 min_similarity 只有 0.35；再加上 0.5.1 的「关键字块没变就不再
读关键字」缓存，判成大厅后会一直停在大厅）。而 `live_collect.SeasonWatcher.update` 开头是
`if not self.on or screen is not None or in_match: return` —— `screen=="lobby"` 直接整页跳过，
`SeasonPage` 连创建都不会（所以「挂在 season_stats 导入上」的补丁永远装不上，这是死锁）。

→ 给 `SeasonWatcher.update` 打补丁：**录入模式下遇到 `screen=="lobby"` 也放行**（当成 None 交给
上游原本的读数据页逻辑）。真正的数据页由 `SeasonPage.on_page` 自己确认（不是就返回，不读）。

## 问题 2：数据页坐标整体偏 ~60px（配置按上游机器校准）

`config/season_stats.json` 的 roi / template_box 等坐标按上游机器校准；本机数据页整体比它
下移约 60px（1920×1080 基准），外观相似度只有 0.13 < 阈值 0.5 → `on_page` 恒 False。

→ 给 `SeasonPage` 打补丁：第一次在**真正数据页**上 `on_page` 时纵向搜索最佳偏移 `dy`
（外观像 + 关键字能认出才算），之后 detect 用平移后的检测器、`_piece` 把 y 加 dy；
大厅等非数据页帧上搜不到就**不锁定**，隔 3 秒重试，直到在数据页上搜到为止。

## 问题 3：「是不是自己」被判错

个人档案页上的名字通常不带俱乐部标签（`阿宝宝宝宝宝`），而 Evo 从 HUD 记的名字带标签
（`[R99]阿宝宝宝宝宝`）。上游 `_own` 只去空格直接比 → 判成「不是自己」，整页不读。

→ 给 `SeasonWatcher._own` 打补丁：比「是不是自己」时把两边的 `[俱乐部标签]` 都去掉。

## 挂载方式（重要：走共享 importhook，绝不自挂 meta_path finder）

`cloud/importhook.py` 是一个**共享 finder**，所有注入方（大厅模式、外卡、子弹时间……）都
`on_import(模块名, 回调)` 登记；一个 finder 统一分发。**自己再插一个 sys.meta_path finder 会
把排在后面的注入挡掉**（Python 只问第一个返回 spec 的 finder）—— 实测会导致大厅模式接管
失效（`screens.ScreenDetector` 不再换成 `ScreenWithLobby`、`MODE_LABELS` 也不补），正好会
影响「识别游戏模式」。所以这里统一用 `cloud.importhook.on_import`：
- `live_collect` 导入后 → 补 `SeasonWatcher.update` / `_own`（不依赖 season_stats）；
- `season_stats` 导入后 → 补 `SeasonPage` 的纵向偏移。

只在记录子进程真正 import 时生效；托盘/面板不加载识别库。幂等；任何一步失败只 `_log`、
退回上游原样（删掉 paimiao_cloud/ 上游照常跑）。
"""

from __future__ import annotations

import copy
import re
import sys
import time

DY_MIN, DY_MAX, DY_STEP = -60, 140, 4   # 纵向偏移搜索范围/步长（1920×1080 基准像素）
_TAG_RE = re.compile(r"^\s*\[[^\]]*\]")  # 名字开头的 [俱乐部标签]

_installed = False


def _log(message):
    try:
        from cloud.state import log
        log("[赛季页] %s" % message)
    except Exception:
        pass


def install():
    """走共享 importhook 登记回调（幂等）。已经导入过的模块会立刻触发。"""
    global _installed
    if _installed:
        return
    _installed = True
    try:
        from cloud import importhook
    except Exception as exc:  # noqa: BLE001
        _log(f"拿不到共享 importhook（按上游原样）：{exc!r}")
        return
    importhook.on_import("live_collect", _after_live_collect, label="赛季页")
    importhook.on_import("season_stats", _after_season_stats, label="赛季页")


def _after_live_collect(module):
    try:
        _patch_live_collect(module)
    except Exception as exc:  # noqa: BLE001
        _log(f"live_collect 补丁失败（按上游原样）：{exc!r}")


def _after_season_stats(module):
    try:
        _patch_season_stats(module)
    except Exception as exc:  # noqa: BLE001
        _log(f"season_stats 补丁失败（按上游原样）：{exc!r}")


# ---------- live_collect：录入模式下放行「被判成大厅」的数据页 + 名字忽略俱乐部标签 ----------

def _strip_tag(name_key):
    """去掉名字开头的 [俱乐部标签]（name_key 是已去空格的写法）。"""
    return _TAG_RE.sub("", name_key or "")


def _patch_live_collect(module):
    cls = getattr(module, "SeasonWatcher", None)
    if cls is None:
        return
    _patch_update(cls)
    _patch_own(cls, module)


def _patch_update(cls):
    if getattr(cls, "_paimiao_season_update_fix", False):
        return
    orig = cls.update

    def update(self, screen, frame, now, in_match):
        # 录入模式里，数据页常被判成「大厅」；上游见到 screen 不是 None 就整页跳过。
        # 这里只放行 lobby 这一种，交给上游原本的读数据页逻辑（on_page 自己会再确认）。
        if getattr(self, "on", False) and not in_match and screen == "lobby":
            return orig(self, None, frame, now, in_match)
        return orig(self, screen, frame, now, in_match)

    cls.update = update
    cls._paimiao_season_update_fix = True
    _log("已接上：录入时数据页被判成大厅也照读")


def _patch_own(cls, module):
    if getattr(cls, "_paimiao_season_own_fix", False):
        return
    import storage  # 上游模块

    def _own(self, frame, now):
        if self.owner is None or (now - self.owner[2]).total_seconds() > self.OWNER_SECONDS:
            name = self.page.name(frame)
            mine = storage.known_names(self.conn)
            if self.post.player_name:
                mine = mine | {storage.name_key(self.post.player_name)}
            mine_norm = {_strip_tag(k) for k in mine}
            own = name is not None and (not mine_norm or _strip_tag(storage.name_key(name)) in mine_norm)
            self.owner = (name, own, now)
            if not own:
                self._say(("owner", name), now, f"数据页上是 {name or '（名字没读出来）'}，不是 Evo 认识的你的名字，不读"
                          "（只录自己的；好友资料页也是这一页）")
        else:
            self.owner = (*self.owner[:2], now)
        return self.owner[1]

    cls._own = _own
    cls._paimiao_season_own_fix = True
    _log("已接上：数据页“是不是自己”忽略俱乐部标签")


# ---------- season_stats：数据页识别纵向偏移自适应 ----------

def _shift_detect_cfg(base_detect, dy):
    d = copy.deepcopy(base_detect)
    screen = d.get("screens", {}).get("season_stats")
    if screen:
        if isinstance(screen.get("roi"), list):
            screen["roi"][1] += dy
        if isinstance(screen.get("template_box"), list):
            screen["template_box"][1] += dy
    return d


def _patch_season_stats(module):
    cls = getattr(module, "SeasonPage", None)
    if cls is None or getattr(cls, "_paimiao_season_align", False):
        return
    PAGE = getattr(module, "PAGE", "season_stats")
    ScreenDetector = getattr(module, "ScreenDetector")

    orig_init = cls.__init__
    orig_on_page = cls.on_page
    orig_piece = cls._piece

    def __init__(self, engine, cfg=None, lang=None):
        orig_init(self, engine, cfg, lang)
        self._paimiao_dy = None
        self._paimiao_next_try = 0.0   # 搜不到就隔几秒再搜（别把 dy 锁死在大厅帧上）
        self._paimiao_base_detect = copy.deepcopy(self.cfg.get("detect", {}))

    def _paimiao_search_dy(self, frame):
        base = self._paimiao_base_detect
        min_sim = base.get("min_similarity", 0.5)
        best_dy, best_sim = None, -1.0
        for dy in range(DY_MIN, DY_MAX + 1, DY_STEP):
            try:
                det = ScreenDetector(self.engine, _shift_detect_cfg(base, dy), None)
                sim = det.similarity(frame, PAGE)
            except Exception:  # noqa: BLE001
                continue
            if sim is not None and sim >= min_sim and sim > best_sim:
                best_dy, best_sim = dy, sim
        if best_dy is None:
            return None
        try:
            det = ScreenDetector(self.engine, _shift_detect_cfg(base, best_dy), None)
            if det.detect(frame) == PAGE:
                return best_dy, det, best_sim
        except Exception:  # noqa: BLE001
            pass
        return None

    def on_page(self, frame):
        if getattr(self, "_paimiao_dy", None) is None:
            # on_page 第一次被调用时可能人还在大厅（录入刚开就每帧试）：那种帧上搜不到数据页
            # 模板，**不要锁成 dy=0**，否则回到数据页又是 0。搜不到隔 3 秒再搜。
            now = time.monotonic()
            if now >= getattr(self, "_paimiao_next_try", 0.0):
                self._paimiao_next_try = now + 3.0
                try:
                    found = self._paimiao_search_dy(frame)
                except Exception as exc:  # noqa: BLE001
                    _log(f"自适应失败（按上游原样）：{exc!r}")
                    found = None
                if found is not None:
                    dy, det, sim = found
                    self._paimiao_dy = dy
                    self.detector = det
                    if dy:
                        _log(f"纵向偏移自适应：dy={dy:+d}px（相似度 {sim:.2f}）")
        return orig_on_page(self, frame)

    def _piece(self, frame, box, dx=0):
        dy = getattr(self, "_paimiao_dy", None) or 0
        if dy:
            box = [box[0], box[1] + dy, box[2], box[3]]
        return orig_piece(self, frame, box, dx)

    cls.__init__ = __init__
    cls.on_page = on_page
    cls._piece = _piece
    cls._paimiao_search_dy = _paimiao_search_dy
    cls._paimiao_season_align = True
    _log("已接上：数据页识别纵向偏移自适应")
