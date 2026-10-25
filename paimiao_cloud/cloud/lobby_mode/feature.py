"""大厅判定：几毫秒的便宜检查 + 只在「像是大厅」时才跑的那几行识别。

## 为什么单独成一个文件（2026-10-24 实测的 bug）

原来这两步写在 `screen.build()` 造出来的 ScreenDetector 子类里，只在**记录程序每帧认画面**
那条路上跑。可是打完一局回大厅这一段，玩家往往**一直在看结算界面**：
小队淘汰 → 排名第 3 → 名次横幅散去，画面切不成任何已知画面。

实测 2026-10-24 16:25:28 打完第 17 局之后：

    16:25:28  在总结页读到了结算（名次、伤害都对）
    （玩家没有马上离开结算界面，50 秒里画面一直是「实况 / 总结」那一页）
    下一帧突然就是 16:26 的开局横幅 —— `ScreenWatcher.update` 这时才写上一局，
    然后 `finish()` 把 `self.info` 清空

我们那条路本来要在大厅帧上「读到外卡、记成待用值」，可这一整段没有一帧被认成大厅，
所以待用值是空缺的，第 18 局的模式就是空的（maps 却照旧能回推，所以看起来「只有模式没了」）。

修法：**不要求先认出大厅**。每帧（对局中除外）先做一次几毫秒的卡片结构检查，
像了才跑顶栏那一次识别。真正的大厅帧 100% 有卡片，所以这个门槛不丢数据；
而结算界面、对局画面、横幅上都没有这条卡片色条，一次识别都不会多跑。

## 两条判据

    ① 左下角那张卡片的色条在不在（纯结构，不用 OCR；实测 0.8~1.8 毫秒，同一帧只算一次）
    ② 顶栏那排「游玩 / 挑战 / 通行证 / 传奇储物柜 / 商店」认得出几个 —— 大厅独有

两条都成立才算大厅（2026-10-24 用户定）。代价只有一个：真正的大厅里，
`check_lobby()` 每帧会多跑一次顶栏识别（几十毫秒），所以要按 NAV_INTERVAL 缓存。
"""
from __future__ import annotations

import time

from . import rules

# 顶栏那排字多久认一次（秒）。
#
# 2026-10-24 20:00 用户报「还是占用特别高」后，用 py-spy --pid <记录子进程> record
# 采样实测（停在大厅里）：**58% 的 CPU 花在顶栏这一步**，单次 450~1100 毫秒
# （顶栏 ROI 宽 1920、还要放大 2 倍，等于 3840×120 送进 OCR）。
#
# 它是两条判据里的**辅助**那一条：真正决定"这是大厅"的是卡片判据（纯结构、
# 1~2 毫秒），顶栏只用来排除"卡片在、标题还没渲染完"的过渡帧。而过渡帧总是
# 成串出现的（切场景那一两秒），所以顶栏**不需要 3 秒就跟进**。
#
# 取 15 秒：停在大厅里顶栏 OCR 从「每分钟 20 次」降到「每分钟 4 次」，
# 省下约 2/3 的 CPU；代价是刚切到大厅的最多 15 秒内可能多跑几次卡片 OCR
# （卡片 OCR 本身仍按 READ_INTERVAL=3 秒节流）。
NAV_INTERVAL = 15.0

# 最近一次顶栏识别结果（只在本进程内存里，不落盘：它只用来判断"是不是大厅"）。
_NAV: dict = {}

# 这一帧的卡片检查结果：{id(frame): True/False}
#
# 为什么要记：同一帧在一轮里会被好几处问一遍 —— 记录程序自己的 `detect()`（每张截图都走）、
# 截屏钩子（`match_mode._capture`，也看每张截图）。不记的话每张截图要重建两次 float32 数组，
# 而 `card_like` 只裁左上角一个 220×56 的方块再逐行看标准差 —— 最慢的那次是
# 「冷启动后第一次」（2026-10-24 实测 3810 毫秒 vs 1.9 毫秒），所以同一个问题只答一次。
#
# 用 id() 当键、最多留 MAX_CACHED 条：只留住最近这一两帧（新帧进来就清旧表）。
# 每次结果一算完就把它自己从表里删掉，所以同一个 id 不会被内存复用骗到。
#
# ─── 2026-10-24 19:30 修：上面那句「不会被内存复用骗到」是错的 ───
#
# 实测：连续 200 个 2560×1440 的 numpy 数组，`id()` **只有 1 个不同值**
# （2980429368464 连出 200 次）。numpy 的数组头对象很小，分配器每次都把
# 刚释放的那块地址原样还回来 —— 于是「新一帧」和「上一帧」的 id 一模一样。
#
# 后果不是少算一次，而是**拿上一帧的答案回答这一帧**：判出来「像卡片」，
# 就去跑顶栏 OCR + 读卡片；主循环 0.3 秒一帧，一帧却要 200~1000 毫秒，
# 记录程序直接卡死在识别里（19:24 那次实测：20 分钟烧掉 502 秒 CPU、
# 一张截图都没存下来、日志停在启动那一刻）。
#
# 现在不用 id() 了：记的是**帧内容**的几个采样字节 + 形状（见 _frame_sig）。
# 同一帧被问两次 → 命中；新一帧哪怕地址相同 → 签名不同，老老实实重算。
_CARD: dict = {}
MAX_CACHED = 4


def _frame_sig(frame):
    """这一帧的"指纹"：形状 + 几处采样像素。

    为什么不用 `id()`：见上面那段（实测 200 帧只有 1 个 id）。
    为什么不用完整哈希：2560×1440×3 = 11 MB，哈希一次就要几十毫秒，比读卡片还贵。

    采样点覆盖卡片关心的位置和顶栏，几处一比对就足以区分「同一张图」和「下一张图」。
    """
    try:
        h, w = frame.shape[:2]
        # 四角 + 中心 + 顶栏和卡片的几个固定位置（按比例取，换分辨率也不用改）
        picks = (
            (h // 2, w // 2), (0, 0), (h - 1, w - 1),
            (int(h * 0.15), int(w * 0.2)),      # 卡片标题附近
            (int(h * 0.09), int(w * 0.5)),      # 顶栏
            (int(h * 0.7), int(w * 0.6)),
        )
        samples = []
        for y, x in picks:
            samples.append(frame[y, x].tobytes())
        return (h, w, tuple(samples))
    except Exception:
        return None


def card_like(frame) -> float:
    """这一帧左下角像不像大厅那张卡片（0 / 1）。同一帧只算一次。"""
    from cloud.lobby_mode import recognizer  # noqa: PLC0415

    key = _frame_sig(frame)
    if key is not None:
        hit = _CARD.get(key)
        if hit is not None:
            del _CARD[key]
            return hit
    try:
        value = recognizer.card_like(frame)
    except Exception:
        value = 1.0     # 和 recognizer 的约定一样：出错时当作"像"，宁可多识别一次
    if key is not None:
        if len(_CARD) >= MAX_CACHED:
            _CARD.clear()
        _CARD[key] = value
    return value



def nav_hits(engine, frame, fresh=False) -> int:
    """顶栏认得出几个大厅的词（按 NAV_INTERVAL 缓存）。

    `fresh=True` 时不看缓存，重新认一次。

    **时间戳是唯一的开关**（2026-10-24 20:19 用户指出「进入和离开大厅一直立马
    就被加载出来了」之后改的）：以前离开大厅会 `_NAV.clear()` 把这份缓存整个抹掉，
    于是「切走 → 切回」必然重跑一次 OCR，不管距上一次才过几毫秒。用户真实的用法
    就是在大厅、对局、结算之间来回切，切一次认一次，节流等于不存在。

    现在 `_NAV` 只当一块记着「上次什么时候认的、认出了什么」的便签，谁也不清它，
    认不认只看时间够不够 NAV_INTERVAL。代价是：切走再回来，最多晚 NAV_INTERVAL
    才用上「这一刻」的答案 —— 而顶栏那几个词（游玩/活动/挑战/通行证/商店）是
    大厅的结构，十五秒内不会变，晚一点没有任何影响。
    """
    now = time.monotonic()
    if not fresh and now - _NAV.get("at", -1e9) < NAV_INTERVAL:
        return _NAV.get("hits", 0)
    from cloud.lobby_mode import recognizer  # noqa: PLC0415

    hits, text = recognizer.nav_band(engine, frame)
    _NAV.update(at=now, hits=hits, text=text)
    return hits


# 上一次真读卡片的时间点和结果。读卡片 = 两次 OCR（模式行 + 状态行），
# 实测 200~1000 毫秒，**绝不能每帧都跑**（2026-10-24 19:24 就是这么把记录程序
# 卡死的：主循环 0.3 秒一帧，一帧却要 200 毫秒以上，永远追不上，20 分钟烧掉
# 502 秒 CPU、一张截图都没存下来）。
#
# 停在大厅里时按 READ_INTERVAL 重读一次就够（用户要的是「取消换模式后以最后一次为准」，
# 3 秒一次完全够）；刚认成大厅（上一帧不是）则立刻读一次，保证第一帧就有读数。
_READ: dict = {}
READ_INTERVAL = 3.0

# 连着多少帧「卡片不像」才算真的离开了大厅（见 check_lobby 里的说明）。
MISS_LEAVE = 3
_MISS = 0

# 已经没有 `reset_nav()` 了（2026-10-24 20:20 删）。
#
# 它原本是「离开大厅时把顶栏缓存作废」的入口，是这套缓存被反复打穿的根源：
# 只要有什么东西调它一次，下一次 check_lobby 就必然重跑几百毫秒的顶栏 OCR。
# 现在 `_NAV` 只在 nav_hits 里按时间滚，任何状态变化都不清它 ——
# 想做「立刻重认」的地方，直接传 fresh=True 走 nav_hits，不要来这里清全局状态。


def check_lobby(engine, frame, fresh=False):
    """这一帧是不是准备界面。返回 (verdict, detail)：

        ("lobby", 卡片读数)  两条判据都成立（detail 是 read_lobby_mode 的结果）
        ("blank", None)      两条都成立，但卡片上的字没读出来（过渡帧）
        ("other", None)      不是准备界面

    `fresh=True`：顶栏不看缓存（截屏钩子用，说明见 nav_hits）。
    """
    from cloud.lobby_mode import recognizer  # noqa: PLC0415

    try:
        card_score = card_like(frame)
        if card_score <= 0.5:
            # 卡片「不像」**不等于**已经离开大厅：2026-10-24 19:48 用户报「把界面放到
            # 大厅上风扇就呼呼叫」，实测根因就在这里——
            # card_like 的判据原来是「整行像素完全一色」（uniform_max_range=0.0），
            # 大厅里画面一直在动（技能图标、队友、右下角动画），真实帧跟参考帧差 1 个
            # 灰度级，整行就不再"一色"，card_like 从 1.0 直接掉到 0.0。
            # 原来这里一"不像"就清 _NAV 和 _READ，于是：不像 → 清 → 下一秒像 → 两次 OCR
            # 全量重跑 → 又不像 → 又清 …… 实测 10 帧里顶栏 OCR 跑了 10 次。
            # （rules.CARD_PROBE 的容差已同时放宽到 8，这里是第二道保险。）
            # 「不像」只记一笔，连着 MISS_LEAVE 帧都不像才算真的离开大厅：真的离开会有
            # 持续几十秒的过渡/对局画面，连续 3 帧很容易满足；大厅内部的抖动只会
            # 偶发一两帧。
            global _MISS
            _MISS += 1
            # 离开大厅**不再清任何缓存**（2026-10-24 20:19 用户实测纠正）。
            #
            # 原来的写法是 `if _MISS >= MISS_LEAVE: _NAV.clear()`，看起来是在
            # 「确认离开」时作废，实际后果是：用户在大厅、对局、结算之间来回切，
            # 每切一次就清一次顶栏缓存，切回来第一帧必然重跑几百毫秒的 OCR。
            # 用户的原话是「我切页面立马一两秒就被检测出来了」—— 正是因为清了缓存
            # 就必须当场重认，缓存根本没留在手里。
            #
            # 缓存该不该重认只看时间（nav_hits 里的 NAV_INTERVAL），不看状态转移。
            # _MISS 留着只是为了别让「偶发一两帧不像」把状态误判成离开。
            return "other", None
    except Exception:
        return "other", None
    _MISS = 0

    # 第二道：顶栏**长得像不像**（0.7 毫秒，用户 20:22 提的「上游怎么检测的」）。
    #
    # 照抄上游 code/screens.py 的 ScreenDetector：先花几毫秒比外观，像了才起识别引擎。
    # 放在 OCR 之前，就是因为大多数帧根本不该跑 OCR —— 打游戏时画面五花八门，
    # 0.7 毫秒就能排除掉；真在大厅时才往下走。
    try:
        if recognizer.nav_outline_like(frame) < rules.LOBBY_NAV["outline_min"]:
            return "other", None
    except Exception:
        pass        # 外观判据出错就当"像"，交给下面的 OCR 决定，不因省事漏判

    try:
        # 顶栏现在只读**中段那一小块**（recognizer.nav_band，用户 20:10 提的
        # 「只截上面那一部分」），那块里稳定只有「活动 挑战 通行证」这 2 个词，
        # 到不了 min_hits=3。实测 28 张大厅帧上和全宽读判定完全一致，所以门槛按 band 走。
        need = rules.LOBBY_NAV["band_min_hits"]
        if nav_hits(engine, frame, fresh=fresh) < need:
            return "other", None
    except Exception:
        return "other", None

    # 到这里两条判据都成立。**这里是唯一花钱的地方**（两次 OCR，200~1000 毫秒），
    # 所以必须节流：停在大厅里时 READ_INTERVAL(3 秒) 内直接返回上一次的答案，
    # 不重新读。上一帧不是大厅（刚回来）则立刻读，保证第一帧就有读数。
    now = time.monotonic()
    prev = _READ.get("at")
    if prev is not None and (now - prev) < READ_INTERVAL:
        cached = _READ.get("result")
        if cached:
            return ("lobby" if cached.get("found") else "blank"), dict(cached)
    result = recognizer.read_lobby_mode(engine, frame)
    _READ.update(at=now, result=dict(result))
    if not result.get("found"):
        return "blank", result
    # 顶栏这一条**认下来了**，把它的文字轮廓存成模板 —— 之后每帧只花 0.7 毫秒比对，
    # 不像就直接判不是大厅，不用起 OCR（照上游 screens.ScreenDetector 的做法）。
    # 换赛季导航栏改版也不用改代码：下次真认到大厅时模板自动换成新的。
    try:
        recognizer.learn_nav_outline(frame)
    except Exception:
        pass        # 存不下不影响判大厅，只是省不了那一步
    return "lobby", result
