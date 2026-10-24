"""从一张大厅截图里读「模式」和「是否正在匹配」。

对外只有三个函数：

    read_lobby_mode(engine, img)   -> dict    读一次，返回这次读到的内容（读不出就是 None）
    card_like(img)                 -> float   这个位置像不像大厅那张卡（0~1，粗筛用）
    fill_color(img)                -> (红度, 青度)  色条的颜色偏向

这里不改上游任何模块，纯函数；怎么接进 Evo 的流程见 inject.py。

数据可信优先（项目原则 5）：一律"读不出就留空"，不猜、不补默认值。
"""
from __future__ import annotations

import re
from typing import Any

import numpy as np

from . import rules


def _clean(text):
    """去掉识别结果里的空白和标点：'正在匹配....' → '正在匹配'。"""
    return re.sub(r"[\s.。·、,，;；:：!！?？\-—_~]+", "", text or "")


def _read(engine, img, spec, min_score=None):
    """按一段规则裁图并识别，返回 [(文字, 置信度), ...]。

    spec 里给的是基准坐标，裁图和缩放都交给上游 ocr.crop —— 分辨率、宽高比不同的
    截图换算方式只有一份（layout.py），这里不重复实现。
    """
    import ocr

    lines = ocr.read_lines(engine, img, spec["roi"], rules.ocr_cfg(spec), spec["anchor"])
    low = spec.get("min_score", 0.0) if min_score is None else min_score
    return [(str(text), float(score)) for text, score in lines if float(score) >= low]


def _pick(lines, wanted):
    """在识别结果里找字样，返回 (命中的标准写法, 原文, 置信度) 或 None。

    wanted 是 [(字样, 值), ...]，按顺序优先 —— 越具体的排越前。
    """
    best = None
    for text, score in lines:
        cleaned = _clean(text)
        if not cleaned:
            continue
        for index, (word, value) in enumerate(wanted):
            if cleaned == word or cleaned.startswith(word) or word in cleaned:
                # 先比"排得靠前"，再比置信度：具体模式名（外卡）胜过泛称（三人赛）
                rank = (-index, -score)
                if best is None or rank > best[0]:
                    best = (rank, value, text, score)
            elif _similar(cleaned, word):
                # 引擎偶尔认错一个字（「子弹时间」读成「子弹时问」）。
                # 宁可比这个宽松一点，也不要把整局漏掉 —— 读错字时置信度本来就低，
                # 后面的色条颜色还会再做一次旁证。
                rank = (-index, -score - 0.5)
                if best is None or rank > best[0]:
                    best = (rank, value, text, score)
    if best is None:
        return None
    return best[1], best[2], best[3]


def _similar(text, word):
    """两条文字重合得够多就算像（只用来兜住个别错字）。"""
    if len(word) < 3 or len(text) < 3:
        return False    # 两三个字的短名（外卡、控制）不容错，认错一个字就是另一个模式
    same = len(set(text) & set(word))
    return same / max(len(set(word)), 1) >= 0.75


def fill_color(img):
    """卡片色条最左端那一段的 (红度, 青度)。

    红度 = R - (G+B)/2 —— 排位赛／未上榜是红底，红度约 +50；
    外卡、控制、进化版是青底，红度约 -25 ~ -60。

    注意要用**彩色**截图算（上游 load_image 给的是 BGR），转成灰度以后红和青的
    亮度几乎一样，就分不出来了。
    """
    try:
        import cv2

        import layout

        spec = rules.RANKED_FILL
        # 不能走 ocr.crop：那个函数会把小块转成灰度（config 里 grayscale=True），
        # 红和青的亮度几乎一样，转完就分不出来了。这里只借用它的坐标换算 layout.cut。
        piece = layout.cut(img, spec["roi"], rules.BASE_RESOLUTION, spec["anchor"]).astype(np.float32)
        if piece.ndim == 2:
            piece = cv2.cvtColor(piece.astype(np.uint8), cv2.COLOR_GRAY2BGR).astype(np.float32)
        b, g, r = piece[..., 0].mean(), piece[..., 1].mean(), piece[..., 2].mean()
        mid = (g + b) / 2.0
        return float(r - mid), float(mid - r)
    except Exception:
        return 0.0, 0.0


def nav_like(engine, img):
    """顶栏有没有大厅那几个词（游玩 / 挑战 / 通行证 / 传奇储物柜 / 商店）。

    这是「这是大厅」的第二条线索（2026-10-24 用户提出）。第一条是左下角那张卡片
    （card_like），但卡片在**对局结束的过渡帧**上也在、标题还没渲染完，光看卡片
    分不出「真大厅」和「刚要开始渲染的大厅」。顶栏这排字是大厅独有的。

    实测（全部历史截图）：45 张真大厅里 40 张认得出（≥3 个词），
    39 张别的画面（hud / 淘汰 / 冠军 / 结算 / 跳伞）**全部 0 个**，零误报。

    返回 (命中几个词, 那一行读到的原文)。
    """
    spec = rules.LOBBY_NAV
    lines = _read(engine, img, spec, min_score=spec["min_score"])
    text = "".join(t for t, s in lines)
    hits = sum(1 for w in spec["words"] if w in text)
    return hits, text


def nav_band(engine, img):
    """顶栏**中段那一小块**认得出几个大厅的词 —— 和 nav_like 同样的答案，快十几倍。

    用户 2026-10-24 20:10 说的：「你为什么不能只截上面那一部分和左下角那一部分？」

    对，而且这是对的做法。nav_like 读的是整条顶栏（1920 基准 × 放大 2 倍 =
    3840×120 送进 OCR，实测 450~1100 毫秒），但那条横带左边一大半是
    「186 经验值 / L3999 / 11.6K/20.0K」这些**和判大厅毫无关系**的数字，
    它们照样要被检测框出来、再逐字识别，全是白花的。

    真正有用的只有中间那一段「活动 挑战 通行证」。按图片实际宽度的比例裁
    （不是 1920 基准坐标 —— 各分辨率换算后位置并不固定，2026-10-24 20:00
    试过用基准坐标收窄，另外 4 张真大厅帧的命中数就从 [3,3,3,2] 掉到 [2,1,1,1]），
    实测 28 张不同时刻的大厅帧上，判定结果和全宽**完全一致（0 张不同）**，
    耗时从 450~1100 毫秒降到 77~130 毫秒。

    返回 (命中几个词, 读到的原文)。
    """
    spec = rules.LOBBY_NAV
    return _read_band(engine, img, spec, spec["band"])


def _read_band(engine, img, spec, band):
    """按**比例**裁一条横带再识别（img 是原图，band 是 (起,止) 两个小数）。"""
    import numpy as np                         # noqa: PLC0415
    import ocr                                 # noqa: PLC0415

    x0, x1, y0, y1 = band
    height, width = img.shape[:2]
    crop = img[int(height * y0):int(height * y1), int(width * x0):int(width * x1)]
    if crop.size == 0:
        return 0, ""
    text = "".join(str(t) for t in (getattr(engine(crop), "txts", None) or []))
    hits = sum(1 for word in spec["words"] if word in text)
    del np
    return hits, text


_NAV_OUTLINE: dict = {"key": None, "tpl": None}


def _band_crop(img, band):
    """按比例裁出顶栏那一条（灰度）—— 外观判据和 OCR 用的是同一块。"""
    import cv2                                 # noqa: PLC0415

    x0, x1, y0, y1 = band
    height, width = img.shape[:2]
    crop = img[int(height * y0):int(height * y1), int(width * x0):int(width * x1)]
    if crop.size == 0:
        return None
    return cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop


def nav_outline_like(img):
    """顶栏这一条**长得像不像**（0~1）—— 0.7 毫秒，比 OCR 便宜 170 倍。

    用户 2026-10-24 20:22：「上游似乎有进入和离开大厅的检测，看看它是怎么检测的，
    是不是可以直接复用它的检测。」

    上游 `code/screens.py` 的 `ScreenDetector` 确实是这么干的，思路值得直接照抄：

        1. 外观检查（几毫秒，不用识别模型）：检查区域里有没有和参照图
           （config/templates/）一样的**文字轮廓**。
        2. 外观像了，再用识别引擎读这一块，关键字都在才算。

    所以打游戏的大部分时间画面都不是这几种，第 1 步就排除了，不用跑识别引擎。

    **为什么不能直接复用上游的 `lobby` 画面**：上游那张 lobby 模板认的是
    「排位点数」这几个字（`config/screens.json` 里 `lobby.roi=[160,732,250,46]`），
    只对**排位大厅**成立。实测外卡大厅（左下角卡片是青底写「外卡」）相似度只有
    0.12，被判成"不是大厅"。它认的是「排位大厅」，我要的是「大厅」，不是一回事。
    上游自己的流程里，大厅判定失败时正是退到 `in_match` 那条路继续跑。

    **为什么顶栏那一条能当判据**：整条顶栏（经验值 / 三级导航 / 货币 / 齿轮）
    是短时间不变的结构，第一次出现时才 OCR 一次，把那一条**当时的文字轮廓**
    存下来当模板，之后每帧只做一次相关系数比对。

    用**窄模板 + matchTemplate 滑动**（上游同款），不是整条对齐比：实测同一台机器上
    同一批大厅帧，顶栏有的显示 5 个标签（游玩/挑战/通行证/传奇储物柜/商店），
    有的显示 6 个（多一个「活动」）—— 整条对齐比会让相似度从 0.99 掉到 0.01。
    拿其中一小段（16000 像素）去整条里滑，多一个少一个标签都不影响。
    实测：真大厅帧 0.85~1.00，过渡帧（顶栏还空着）0.03~0.08，分得很干净。

    模板来自「第一次认成大厅的那一帧」，所以换赛季导航栏改版后会自动跟上
    （只要有一次 OCR 认出来了，模板就换成新的）。

    `img` 可以是整帧，也可以是已经裁好的顶栏灰度条（见 feature.check_lobby）。
    """
    import cv2                                 # noqa: PLC0415

    spec = rules.LOBBY_NAV
    if img is None or getattr(img, "size", 0) == 0:
        return 0.0
    gray = _band_crop(img, spec["band"]) if img.ndim == 3 and img.shape[0] > 200 else img
    if gray is None or gray.size == 0:
        return 0.0
    # 文字轮廓：减去模糊后的自己，只留笔画边缘（上游 screens.outline 同款）
    fl = gray.astype(np.float32)
    outline = fl - cv2.GaussianBlur(fl, (0, 0), 3)

    tpl = _NAV_OUTLINE.get("tpl")
    if tpl is None:
        return 1.0      # 还没存过模板：当作"像"，让 OCR 去认，认到了才存
    if outline.shape[0] != tpl.shape[0] or outline.shape[1] < tpl.shape[1]:
        return 1.0      # 尺寸对不上（换了分辨率）：这一帧先交给 OCR
    try:
        score = float(np.nan_to_num(
            cv2.matchTemplate(outline, tpl, cv2.TM_CCOEFF_NORMED), nan=-1.0).max())
    except Exception:
        return 1.0
    return score


def learn_nav_outline(img):
    """把这一帧顶栏的文字轮廓存成模板（只在真的 OCR 认出大厅后调用）。"""
    import cv2                                 # noqa: PLC0415

    spec = rules.LOBBY_NAV
    gray = _band_crop(img, spec["band"])
    if gray is None or gray.size == 0:
        return
    fl = gray.astype(np.float32)
    outline = fl - cv2.GaussianBlur(fl, (0, 0), 3)
    # 只取中段一小块当模板：多一个 / 少一个导航标签都不影响匹配
    x0, x1 = spec["tpl_slice"]
    width = outline.shape[1]
    piece = outline[:, int(width * x0):int(width * x1)]
    if piece.size:
        _NAV_OUTLINE["tpl"] = piece
        _NAV_OUTLINE["key"] = (piece.shape[0], piece.shape[1])


def card_like(img):
    """这个位置像不像大厅那张卡片（0~1，1 表示最像）。
    用**结构**判断，不用参照图比像素：大厅卡片顶部一定有一条纯色横带（模式名那行，
    青底或红底），它的特征是「行内亮度很均匀、连续十几行、亮度不太暗」。
    找得到这条带就算像，返回 1.0，找不到返回 0.0。

    为什么不比参照图（2026-10-24 实测教训）：原来的办法是拿 6 张排队截图平均出一张
    参照图比相关度，但「排位赛」三个字在排队时靠右、没点准备时靠左，字位置一变相似度
    就从 0.66 掉到 0.18，未点准备的排位卡整张被当成"不是大厅"跳过 —— 那正是用户反馈
    「模式没检测到」的一半原因。参照图本质是在比文字位置，不能当判据。
    也试过窄参照图、滑窗、亮度/标准差的统计量，正例负例全都分不开（见 README 的教训）。

    它只用来**省 CPU**：不像的帧直接跳过，不送 OCR。
    返回 1.0 也不等于"确认是大厅" —— 最后仍然看 OCR 读到的字（read_lobby_mode）。
    """
    try:
        import screens as up

        spec = rules.CARD_PROBE
        gray = up.gray_crop(img, spec["roi"], rules.BASE_RESOLUTION, spec["anchor"]).astype(np.float32)
        smax = spec["row_std_max"]
        lo = spec["row_mean_min"]
        left, right = spec["inset"], gray.shape[1] - spec["inset"] * 3
        run = best = 0
        uniform = 0        # 真正"一整个横条都一个色"的行
        for i in range(gray.shape[0]):
            row = gray[i, left:right]
            if row.std() < smax and row.mean() >= lo:
                run += 1
                best = max(best, run)
                # ② 这一行是不是**整行一色**（色条的特征）。
                # 只看 std 不够：跳伞画面的左下角是游戏地形，行标准差能压到 26 以下，
                # 但色条是「一整条颜色」，行内几乎每个像素都一样。这里取排序后
                # 90 分位和 50 分位的差，等于 0 就是整行一色。
                # 2026-10-24 实测：真大厅 11~29 行，跳伞 / 冠军横幅 / 黑屏全是 0 行。
                values = np.sort(row)
                n = values.size
                if values[int(n * spec["uniform_high"])] - values[int(n * spec["uniform_low"])] <= spec["uniform_max_range"]:
                    uniform += 1
            else:
                run = 0
        if best < spec["min_rows"] or best > spec["max_rows"]:
            return 0.0
        return 1.0 if uniform >= spec["min_uniform_rows"] else 0.0
    except Exception:
        return 1.0     # 出错时当作"像"，宁可多识别一次，也不要因为省事漏掉



def read_lobby_mode(engine, img, *, with_fill=True):
    """读一次大厅卡片。

    返回 dict：
        mode         "ranked" / "pubs" / "wildcard" / "mixed_control" / "bot" / None
                     （None = 没读到，或者读出了但不确定）
        mode_raw     卡片色条上的原文（面板显示、日志用）
        mode_score   那一行的置信度
        matching     True / False / None —— True = 正在排队（点了准备）；
                     False = 在读得懂卡片的前提下，状态行是空的（已不在排队）；
                     None = 认不出来
        status_raw   状态行原文
        status_score 那一行的置信度
        accent       "red" / "cyan" / None —— 色条是红底还是青底
        fill         (红度, 青度)，给日志比对用
        note         给人看的一句话，说明为什么这么判
        found        这一帧里读到了几个字段（0 表示这张图根本没有大厅卡片）
    """
    out: dict[str, Any] = {
        "mode": None,
        "mode_raw": None,
        "mode_score": 0.0,
        "matching": None,
        "status_raw": None,
        "status_score": 0.0,
        "accent": None,
        "fill": None,
        "note": "",
        "found": 0,
    }

    # 1) 色条上的模式名
    hit = _pick(_read(engine, img, rules.CARD_MODE), rules.MODE_TITLES)
    if hit:
        mode, raw, score = hit
        out["found"] += 1
        out["mode_raw"] = _clean(raw)
        out["mode_score"] = score
        out["mode"] = mode

    # 2) 色条颜色：认得出字的时候只做记录；认不出的时候才拿它兜底
    if with_fill:
        red, cyan = fill_color(img)
        out["fill"] = (round(red, 1), round(cyan, 1))
        if red >= rules.RANKED_FILL["red_margin"]:
            out["accent"] = "red"
        elif cyan >= rules.RANKED_FILL["red_margin"]:
            out["accent"] = "cyan"

    # 3) 状态行：卡片底部那一行黄字
    #
    # 读到「正在匹配」/「正在处理」= 这一帧在排队。两个词都认：实机帧里两种写法都出现过
    # （见 rules.MATCHING_WORDS 的注释）。
    #
    # 反过来，状态行**什么都没读到**也是一种信息：点了准备之后这一行会被游戏清掉，
    # 所以「读不出来」往往表示已经不在排队了（正在加载、正在进游戏）。以前这里读不到
    # 就当"这一帧不算"，结果卡在大厅排队那一秒的场景反而记不上模式。
    status = _pick(_read(engine, img, rules.CARD_STATUS),
                   [(word, word) for word in rules.MATCHING_WORDS])
    if status:
        word, raw, score = status
        out["found"] += 1
        out["status_raw"] = _clean(raw)
        out["status_score"] = score
        out["matching"] = True
    elif out["mode"]:
        # 只有认得模式名时才敢做这个否定判断：整张卡片都读不出来时，说明这帧根本不是大厅
        stopped = _pick(_read(engine, img, rules.CARD_STATUS),
                        [(word, word) for word in rules.MATCHING_STOP])
        if stopped is None:
            out["matching"] = False

    # 4) 下结论
    #
    # 用户 2026-10-24 纠正过一条：**「未上榜」不是排位**。
    # 它是大逃杀匹配模式下还没上榜时的标题，所以它归到 pubs，不再归到 ranked。
    # 排位卡片永远写着「排位赛」三个字，认得出来；红底色条不能单独把 mode 定成排位。
    if out["mode"] is None:
        if out["accent"] == "cyan":
            # 青底色条 + 字没认出来：**不猜**（2026-10-24 改）。
            #
            # 原来这里写的是"青底一律按匹配记"，理由是活动名每个赛季都会换、认不出正常，
            # 而排位是红底且写着「排位赛」，一定认得出来。听起来成立，但拿 B 站直播回放
            # 实测就崩了：852×480 的直播画面里，**对局中**左下角队友卡片那片也偏青，
            # 359 帧里有 255 帧被这条路判成 pubs —— 而那些帧全是在打游戏，根本没有大厅。
            # 猜出来的模式会被当成"大厅读到了排位/匹配"交给下一局，比留空有害得多。
            #
            # 现在是：认不出模式名就留空（CLAUDE.md 第 5 条「读不出来就不猜」）。
            # 色条颜色只写进 note，给人看，不参与判定。
            out["note"] = "青底色条，模式名没认出来，不猜"
        elif out["accent"] == "red":
            out["note"] = "色条是红底但没读到模式名，不猜"
        else:
            out["note"] = "没读到模式"
    elif out["mode_raw"] in ("三人赛", "二人赛") and out["accent"] == "red":
        # 红底色条配「三人赛／二人赛」：实测没出现过，出现了就是读串了，不写进数据。
        # 注意不能只看 mode == "pubs"：「未上榜」也是 pubs，而它本来就是红底，属于正常情况。
        out["mode"] = None
        out["note"] = "读到「%s」但色条是红色，对不上，不写" % out["mode_raw"]
    elif out["mode"] == "ranked":
        out["note"] = "排位"
    elif out["mode"] == "pubs":
        if out["mode_raw"] == "未上榜":
            out["note"] = "匹配（未上榜）"
        else:
            out["note"] = "匹配"
    else:
        # 用卡片上的原文，不用字典里的名字：引擎偶尔把「进化版」读成「机器人」，
        # 判定没错（模糊命中），但面板上写成"机器人"会让人以为读出了别的东西。
        out["note"] = "%s（%s）" % (rules.MODE_LABELS.get(out["mode"], out["mode"]), out["mode_raw"])
    if out["matching"]:
        out["note"] += "，正在匹配"
    return out
