"""大厅判定 + 顺手读模式：挂在记录程序每帧认画面那条路上。

对外两个东西：

    build(base_cls)      返回一个继承自 base_cls 的新类（多一个「顺手读大厅卡片」）
    handle(engine, frame) 把这一帧当大厅看一遍（判定和读数都在 feature.py 里）

## 谁用 handle()

两条路都调它，所以两边的判据、节流、日志、待用值完全一致：

    ScreenWithLobby.detect()   记录程序每帧认画面那条路（上游的答案照旧优先）
    match_mode 的截屏钩子      每张截图都过一遍（见 match_mode.py，2026-10-24 的修复）

上游 `ScreenDetector.detect()` 是**先便宜后贵**：先比外观（几毫秒），像了才跑识别。
这里在它后面补一条：卡片在不在 → 顶栏认不认得 → 像了就去读模式。

## 为什么不再要求「先认出大厅」

原来 Detect 里只有在 `screen == "lobby"` 时才读卡片，而用户打完一局常常一直停在
结算界面，那一段一帧都不会被认成大厅，模式就读不到了（2026-10-24 第 18 局就是这样丢的，
详见 feature.py 和 match_mode.py 开头的说明）。现在只要两条判据成立就当他是在准备界面：
上游说是大厅 → 替上游把读数留下来；上游没认出来 → 这里替它回答 "lobby"。

## 为什么不多截一张、不多开一个线程

全部在当前帧上顺带做掉，没有新的线程、没有新的截图、没有新的依赖。
每帧只多一次「卡片色条在不在」的结构检查（实测 0.8~1.8 毫秒，且同一帧只算一次），
顶栏识别按 NAV_INTERVAL 缓存。
"""
from __future__ import annotations

import json
import time
from typing import Any

from . import feature
from . import rules

# 多久读一次模式卡片（秒）。**一直按这个间隔重读**，不锁（2026-10-24 用户定）：
# 玩家可能在匹配中途取消、换成另一个模式再点准备，这时要以最后一次为准。
INTERVAL = 3.0

# 最近一次读数。子进程里的变量主进程看不见，所以还要落一份文件（见 _store）。
_LATEST: dict[str, Any] = {}

# 上一次记进日志的模式名。日志只在它变化时写一行，
# 不然大厅里每 3 秒一条「读到：外卡」，几个小时就把日志塞满（用户 2026-10-24 要日志）。
_LATEST_BEFORE_MODE: list = [None]

# 「这一次是刚回到大厅」——由 handle() 在「上一帧不是大厅、这一帧是」时传下去，
# 让 _read_card 即使模式名没变也记一行（打完一局回来必须能看见一条）。
#
# 为什么这个标记必须由**判定本身**维护，而不是写在 ScreenDetector.detect 里：
# 截屏钩子（match_mode._capture）也会调 handle()，它先看到大厅、把标记置上，
# 紧接着 detect() 也调一次 —— 这时 detect() 里那个「上一帧不是大厅」还成立，
# 又把标记置一遍。两句都用 `force_log or _FORCE_LOG[0]` 取用，谁先到谁赢，
# 结果是**第二帧**才写出「读到」（2026-10-24 实测，用户看日志会发现第一帧空着）。
# 现在：标记由 handle() 维护、用一次就清，谁调用都只有一条路能拿到它。
_WAS_LOBBY: list = [False]


def _log(message):
    """写一行到 Evo 的日志（data/cloud-sync/cloud.log），带「大厅模式」前缀方便搜。"""
    try:
        from cloud import state as cloud_state      # noqa: PLC0415

        cloud_state.log("[大厅模式] %s" % message)
    except Exception:
        pass        # 日志写不下绝不影响识别


def last_reading() -> dict[str, Any]:
    return dict(_LATEST)


def handle(engine, frame):
    """把这一帧当大厅看一遍：两条判据都成立就读卡片、记待用值、写日志。

    返回 "lobby" / "blank" / "other"（含义见 feature.check_lobby）。
    只有真的读通了才更新 _LATEST 和待用值；判据不成立时什么都不动。

    「刚回到大厅要强制记一行日志」这件事在这里判断（见 _WAS_LOBBY 的说明）：
    第一次认成大厅的那一帧记一条，之后停在大厅里、模式名没变就不再写。
    """
    started = time.monotonic()
    verdict, result = feature.check_lobby(engine, frame)
    if verdict == "other":
        _WAS_LOBBY[0] = False       # 离开大厅：下一次认成大厅要重新记一条
        return verdict
    just_back = not _WAS_LOBBY[0]
    _WAS_LOBBY[0] = True
    if verdict == "lobby":
        _read_card(result, just_back, started)
    else:
        _blank(result)
    return verdict


def _blank(result):
    """卡片在、顶栏在，但一个字都没读到（刚切过来画面还没稳）。"""
    # _LATEST 不动：保住上一次的有效读数给面板看。
    #
    # 但**待用的下一局模式要作废**（2026-10-24 用户选的方案 A）：
    # 回到大厅说明进入新的一轮，这一轮没读到就不该拿上一局的读数顶上 ——
    # 否则「回大厅切成外卡、但这次没读到」会被记成上一局的排位。
    #
    # 日志每 INTERVAL(3 秒) 一次会刷屏，所以只在**状态变化时**记一行。
    if _LATEST.get("found"):
        _log("没读到：%s" % (result.get("note") or "没有可识别的字"))
    try:
        from cloud.lobby_mode import match_mode      # noqa: PLC0415

        match_mode.saw_lobby_blank()
    except Exception:
        pass        # 清不掉不影响记录，只是下一局可能沿用旧值


def _read_card(result, just_back, started=None):
    """读到了内容：更新最近读数、落盘、记日志、存成待用值（节流在 feature.check_lobby 里）。"""
    now = time.monotonic()
    _LATEST.clear()
    _LATEST.update(result, at=now, last_try=now)
    _store(result)

    # 日志：模式名变了、或者刚打完一局回到大厅，各记一行。
    # 这一行就是"我到底读到没有"最直接的答案 —— 用户 2026-10-24 要的。
    mode = result.get("mode")
    if just_back or mode != _LATEST_BEFORE_MODE[0]:
        _LATEST_BEFORE_MODE[0] = mode
        # 这一次从进函数到出结果花了多久。第一个游戏识别引擎还没加载，
        # 这一次可能要好几秒（2026-10-24 实测 3810 毫秒，之后就 2 毫秒），
        # 写进日志里，用户以后看日志就能直接看出是哪种情况。
        took = "" if started is None else "，用时 %.0f 毫秒" % ((now - started) * 1000)
        if mode:
            _log("读到：%s（%s），置信度 %.2f%s" % (
                result.get("mode_raw") or "?", mode,
                result.get("mode_score") or 0.0, took))
        else:
            _log("读到了字但不是已知模式：%r（%s）%s" % (
                result.get("mode_raw"), result.get("note") or "", took))

    # 记成「下一局的模式」，等这一局真的开始时才交给上游（见 match_mode.py）。
    # 每次都覆盖：取消换模式后，这里存的就是最新的那个。
    # 同时会复位「这一局补过了」的标记，让每一局都能重新补（见 remember 的说明）。
    try:
        from cloud.lobby_mode import match_mode      # noqa: PLC0415

        match_mode.remember(result.get("mode"), result.get("mode_raw"))
    except Exception:
        pass        # 记不上不影响记录


def build(base_cls):
    """返回继承自 base_cls 的子类。"""

    class ScreenWithLobby(base_cls):
        """上游 ScreenDetector + 顺手读大厅模式卡片。

        上游 ScreenDetector 只有 __init__/similarity/looks_like/has_keywords/detect
        五个方法，这里只覆盖 detect，其余原样继承 —— 上游以后改了识别规则，
        这里自动跟着改，不用同步。
        """

        def detect(self, frame):
            # ① 上游的判定（外观 + 识别），一个字都不改
            screen = super().detect(frame)

            # 顶栏缓存不在这里清：它会拿“现在这张图”的答案去回答别的帧
            # （2026-10-24 实测：第 18 局拿结算页的旧答案判了「不是大厅」）。
            # 清理的时机交给 feature.check_lobby —— 卡片判据判不像的那一刻就是
            # 「刚离开大厅」，那才是唯一可靠的时刻（见 feature.check_lobby）。

            # ② 两条判据都成立才算认得出大厅（2026-10-24 用户定）
            #
            # 原来这里是「任一条成立」：顶栏认得出 或 卡片在。用户看完实测数据后
            # 要求收紧成「两个都认得出才判为大厅；任意一个没有，就以上游结果为准」。
            #
            # 收紧是有依据的 —— 只有卡片成立的那批帧，模式**本来就读不出来**：
            # 实测 20261024_063812 里 8 张（lobby_063841_leave、lobby_064102_leave、
            # lobby_064126_leave、lobby_064213_enter、lobby_064330_enter、
            # lobby_064502_leave、lobby_080440_enter、lobby_080443_leave）
            # 全部 found=0 / mode=None —— 都是「色条已经画出来、标题还没渲染完」的
            # 过渡帧。放它们进来说明只是白跑一次识别，本来就是留空。
            #
            # 而顶栏单独成立的情况在样本里一张都没有，所以收紧**不会丢任何数据**：
            # 420 张真大厅里 41 张两张都成立，那 41 张模式全部读得出来。
            #
            # 这里**每一张截图都会过一遍**（2026-10-24 晚改的）。
            # 原来只在「上游认出来是大厅 / 认不出画面」这两种情况下过卡片的判据，
            # 结果打到结算界面、经验值页、淘汰画面时，玩家一停就在这几页上停几十秒，
            # 期间一帧都不看 —— 第 18 局的模式就是这么丢的（见 feature.py 开头）。
            # 代价很小：卡片判据只裁左上角一个 220×56 的小块（实测 0.8~1.8 毫秒，
            # 见 feature.card_like 的缓存说明），不像就立刻返回；
            # 像了才走顶栏（3 秒一次）+ 读卡片，那才是真的识别。
            #
            # 顶栏缓存仍然按 3 秒算，所以停在大厅里不会每张截图都跑一次识别。
            verdict = "other"
            try:
                verdict = handle(self.engine, frame)
            except Exception:      # 读卡片失败绝不能影响记录
                verdict = "other"

            # 上游认不出、而这两条都成立 → 替它回答 lobby
            if screen is None and verdict != "other":
                screen = "lobby"
            return screen

    ScreenWithLobby.__name__ = "ScreenWithLobby"
    ScreenWithLobby.__qualname__ = "ScreenWithLobby"
    ScreenWithLobby._paimiao_lobby_mode = True      # type: ignore[attr-defined]
    return ScreenWithLobby


def _card_like(frame) -> bool:
    from cloud.lobby_mode import recognizer  # noqa: PLC0415

    # card_like 现在返回 1.0（像）/ 0.0（不像），判据和门槛都在 rules.CARD_PROBE 里
    return recognizer.card_like(frame) > 0.5


def _store(result):
    """把读数写进数据文件夹里的小文件，供面板页面读（子进程的变量主进程看不见）。"""
    try:
        from cloud import state as cloud_state      # noqa: PLC0415

        path = cloud_state.data_dir() / "lobby_mode.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(result)
        payload["read_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass      # 写不下也不影响记录，面板那边显示"还没读到"
