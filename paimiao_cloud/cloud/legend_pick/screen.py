"""把"选取界面"接进上游的画面判定（次要方案，见 rules.py 开头）。

## 接法

`build(ScreenDetector)` 造一个子类，只覆盖 `detect`：

    screen = super().detect(frame)          # ① 上游的判定，一个字都不改
    if screen is not None:  return screen   # ② 认出来了就是它，我们不插嘴
    是不是选取界面？                          # ③ 只有"别的画面都认不出来"时才问一句
    是 → return "legend_pick"
    否 → return None（照旧当对局中）

这样优先级天然低于游戏内所有画面：上游只要认得出任何一个（大厅、总结、淘汰、开局横幅…），
这里连一次匹配都不做，连那 1 毫秒都省了。

## 为什么不放进 in_match 里过滤

`in_match.update(screen, ...)` 对 `screen is not None` 的帧直接 return（只处理"对局中"），
所以从 `detect` 这里返回 "legend_pick" 就已经让它停手了 —— 和"大厅"帧完全一样地对待。
认不出的画面照旧是 None → 当对局中，行为不变。
"""
from __future__ import annotations

import threading

# 放在 ScreenDetector 上的标记：说明这一类已经接过
MARK = "_paimiao_legend_pick"


def build(base_cls):
    """返回继承自 base_cls 的子类。"""

    class ScreenWithLegendPick(base_cls):
        """上游 ScreenDetector + 认不出的画面再看一眼是不是选取界面。"""

        def detect(self, frame):
            screen = super().detect(frame)
            if screen is not None:
                return screen
            if _legend_pick(frame, self):
                return "legend_pick"
            return screen

    ScreenWithLegendPick.__name__ = "ScreenWithLegendPick"
    ScreenWithLegendPick.__qualname__ = "ScreenWithLegendPick"
    ScreenWithLegendPick._paimiao_legend_pick = True      # type: ignore[attr-defined]
    return ScreenWithLegendPick


# 最后一次认成"选取界面"的那一帧读到的名字（2026-10-25 用户要求：尽量取最后一张）。
# 选取界面里玩家会停十几秒，选人先后会变，**最后一张才反映最终选择**
# （队友这时已经定下来了，误差最小）。所以：
#   - 每次认成选取界面 → 用这一帧读一次名字、覆盖掉上一次的
#   - 读完就是"最终答案"，其余东西要查就用 recall()
_LATEST: dict = {}
_LATEST_LOCK = threading.Lock()


def _legend_pick(frame, detector) -> bool:
    """这一帧像不像选取界面；像的话顺手把名字读出来存着。

    任何异常都当"不像"（绝不能影响记录）。
    """
    try:
        from cloud.legend_pick import recognizer   # noqa: PLC0415

        # 把引擎传下去是为了第 4 条判据（三个人都选完、提示全消失时，
        # 靠底部三格"某格上下两行都有名字"认出来，见 recognizer.recognize）。
        engine = getattr(detector, "engine", None)
        found, _ = recognizer.recognize(frame, engine)
        if not found:
            return False
    except Exception:
        return False

    _remember(frame, detector)
    return True


def _remember(frame, detector):
    """用这一帧读一次名字，存成"最后一次"。读不出来就保留上一次的。"""
    try:
        from cloud.legend_pick import recognizer   # noqa: PLC0415

        # 识别模型是惰性加载的（原则 3）：只有真的碰到选取界面才向记录程序要引擎。
        # 要不到（没在识别、或者引擎还没建好）就先只认界面、不读名字。
        engine = getattr(detector, "engine", None)
        if engine is None:
            return
        my_name = getattr(detector, "player_name", None)
        data = recognizer.read(frame, engine, my_name=my_name)
    except Exception:
        return

    if not any(cell.get("player") or cell.get("legend") for cell in data["slots"].values()) \
            and not data["mode"] and not data["map"]:
        return                              # 什么都没读到，别把上一次的好结果冲掉
    with _LATEST_LOCK:
        _LATEST.clear()
        _LATEST.update(data)


def recall():
    """最后一次读到的选取界面内容（没读到过返回 None）。面板和调试用。

    返回的字典是副本，改它不会影响后面读到的新结果。
    """
    with _LATEST_LOCK:
        if not _LATEST:
            return None
        data = dict(_LATEST)
        data["slots"] = {k: dict(v) for k, v in _LATEST["slots"].items()}
        return data


def forget():
    """清掉上一次的结果（回大厅、换局时用，免得下一局读到旧名字）。"""
    with _LATEST_LOCK:
        _LATEST.clear()


def install(module):
    """把 ScreenDetector 换成接过的。幂等；认不出结构时静默不生效。

    0.4.0 起 `live_collect` 用 `screens.GameScreens`（多语言包装器），不再把
    `ScreenDetector` 名字引进 live_collect；`GameScreens.__init__` 用 screens
    模块的全局名 `ScreenDetector` 建各语言的 detector。所以优先 patch
    `screens.ScreenDetector`；旧版（名字直接在 live_collect 里）也兼容。
    """
    try:
        import sys

        patched = False
        for holder in (sys.modules.get("screens"), module):
            if holder is None:
                continue
            detector = getattr(holder, "ScreenDetector", None)
            if detector is None:
                continue
            if getattr(detector, MARK, False):
                patched = True
                continue
            holder.ScreenDetector = build(detector)
            patched = True
        if not patched:
            _log("[选取界面] 上游没有 ScreenDetector，这项不生效（Evo 本身不受影响）")
            return False
        label_watcher(module)      # 让日志能打出「选取界面」，见下面的说明
        return True
    except Exception as exc:
        _log(f"[选取界面] 接入失败（Evo 本身不受影响）：{exc!r}")
        return False


LABEL = "选取界面"
WATCHER_MARK = "_paimiao_legend_pick_label"


def label_watcher(module):
    """给 ScreenWatcher._label 补一条我们自己画面的名字。

    上游 `ScreenWatcher._label(screen)` 是 `screens["screens"][screen]["label"]`
    （`code/live_collect.py:298`），我们造的 `legend_pick` 不在配置里，一进这个画面
    就会 KeyError，把整个记录程序带崩。

    **不能往 screens 配置里塞一条**：`ScreenDetector.detect()` 是遍历
    `self.cfg["screens"]` 的每个名字、无条件读 `spec["roi"]`
    （`code/screens.py:99-103`，实测会 `KeyError: 'roi'`）。所以只包 `_label` 这个方法：
    先看我们的表，认不出再交回上游，别的画面一个字都不改。

    幂等；认不出结构时静默不生效（最多日志里少个名字，不影响记录）。
    """
    try:
        watcher = getattr(module, "ScreenWatcher", None)
        if watcher is None:
            return False
        if getattr(watcher, WATCHER_MARK, False):
            return True
        inner = getattr(watcher, "_label", None)
        if not callable(inner):
            return False

        def _label(self, screen):
            if screen == "legend_pick":
                return LABEL
            return inner(self, screen)

        _label.__name__ = "_label"
        watcher._label = _label
        setattr(watcher, WATCHER_MARK, True)
        return True
    except Exception:
        return False


def _log(message):
    try:
        from cloud import state as cloud_state      # noqa: PLC0415

        cloud_state.log(message)
    except Exception:
        pass
