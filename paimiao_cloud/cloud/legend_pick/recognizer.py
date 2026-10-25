"""认传奇选取界面：三条提示文字里认出两条（判据和坐标全在 rules.py）。

## 往哪接

`cloud/legend_pick/screen.py` 把这里的 `recognize()` 接在上游 `ScreenDetector.detect()` 后面，
**只在它返回 None（别的画面都认不出来）时**才问一句。所以这里不做任何"排除别的画面"的事 ——
只有"这三条认得出两条吗"一个问题。

## 为什么是"次要方案"

对局中每 0.3 秒截一张（`in_match.update`），而选取界面里玩家会盯着停很久（等队友选人、看地图）。
若放任它落进"认不出画面 = 对局中"那条路，一局里会多出几十张无意义的帧，还可能把上一局的收尾冲掉。
（2026-10-25 用户的要求。）
"""
from __future__ import annotations

import pathlib
import threading
import time

import cv2
import numpy as np

from . import rules

_TEMPLATES: dict = {}
_TEMPLATE_LOCK = threading.Lock()
_TEMPLATE_DIR = pathlib.Path(__file__).parent / "templates"


def template(key):
    """取第 key 条提示的参照图（轮廓图），读不到返回 None。进程内只读一次。"""
    with _TEMPLATE_LOCK:
        if key in _TEMPLATES:
            return _TEMPLATES[key]
    path = _TEMPLATE_DIR / (rules.TEMPLATE_PREFIX + key + ".png")
    value = None
    try:
        if path.is_file():
            data = np.fromfile(str(path), dtype=np.uint8)          # 兼容中文路径
            image = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
            if image is not None and image.size:
                # 选 8 位存（png 只能存 8 位）：轮廓值本来就落在 0~105，8 位够用，
                # 存下来也不会把笔画细节抹掉（实测丢掉的是第 2 位小数：0.924 → 0.799 那种是
                # 比对时用 float 值的问题，不是这张图的问题，见下方 _score 的 dtype 对齐）
                value = image
    except Exception:
        value = None
    with _TEMPLATE_LOCK:
        _TEMPLATES[key] = value
    return value


def scores(frame):
    """每一条提示的相似度：{编号: 0~1}。裁不到、没有参照图的记 0。"""
    result = {}
    for key, roi in rules.PROMPTS.items():
        try:
            value = rules.match(frame, roi, template(key))
        except Exception:
            value = 0.0
        result[key] = round(value, 4) if value > 0 else 0.0
    return result


def recognize(frame, engine=None):
    """这一帧是不是选取界面。返回 (是不是, 每条的相似度)。

    代价：每帧裁三小块、三次匹配（1920×1080 下约 1 毫秒，实测见 rules.py）。

    ## engine 是干嘛的（第 4 条判据，2026-10-25 用户决定）

    三位玩家都选完之后，左侧三条提示会**全部消失**（用户给的第二张截图就是这样），
    只靠形状一条都认不出来。这时若给了 engine，再看一眼底部三格有没有"某格上下两行都有名字"
    （`_slot_pair`，见 rules.py 里那条判据的实测依据）。

    **只在一条提示都没认出来时才跑**，而且 engine 为 None 就跳过 ——
    这一个 OCR 约 0.7 秒，不能让每帧都跑，也不能在没碰过选取界面时把识别模型叫醒（原则 3）。

    ## 频率限制（2026-10-25 用户报「游戏内 CPU 870%」）

    光靠上面那句不够：游戏内**几乎每一帧**都是 0 条提示，于是每 0.3 秒一帧 × 每帧 0.8 秒
    横带 OCR，主循环永远追不上。现在真跑过一次后 `rules.SLOT_PAIR_INTERVAL` 秒内不再跑，
    直接返回「不是」。选取界面玩家会停十几秒，不会漏。
    """
    found = scores(frame)
    hits = sum(1 for v in found.values() if v >= rules.MIN_SIMILARITY)
    if hits >= rules.MIN_HITS:
        return True, found
    if hits == 0 and engine is not None and _slot_pair_throttled(frame, engine):
        return True, found
    return False, found


# 上一次真跑横带 OCR 的时刻（time.monotonic）。节流用，见 recognize 的注释。
_SLOT_PAIR_AT = [0.0]
_SLOT_PAIR_LOCK = threading.Lock()


def slot_bars(frame):
    """外观判据：底部横带里有几条「橙红色、长度固定」的横条。

    返回命中的行数。裁不到返回 0。任何异常都当 0（绝不能影响记录）。

    只花几毫秒（两个通道相减 + 逐行数连续段），用来挡掉明显不是选取界面的帧，
    省掉 `_slot_pair` 那 0.8 秒的 OCR。标定见 rules.PRECHECK_RUN 的注释。

    ## 2026-10-25 提速（实测每帧 3.63 毫秒 → 2.7 毫秒）

    烧时间的不是判定逻辑，是那几步多余的全图运算：

    1. `crop.astype(np.int16)` 把 24 万像素 × 3 通道整个转一遍，只为做减法。
       → 改成先 `cv2.split` 得到原 dtype 的通道，各自再转 int16（uint8 直接
       相减会回绕，所以减法两边仍要 int16，但省掉整个 crop 的三倍转换量）。
    2. `np.concatenate(([0], row, [0]))` 每行都新建一个数组。
       → 改成整张遮罩前后各补一列 0，一次 `np.diff(axis=1)` 得到所有行
       的起止边界，循环里只剩取非零。

    ## 试过但**不能**用的：跳行采样

    想隔 `PRECHECK_ROW_STRIDE` 行看一行，把 120 行降到 60 行。实测
    （captures/mode_probe/test_slot_bars.py，768 帧）判定结果变了 173 帧：
    橙红横条本身只有 **3 行高**（y=8,9,10），隔行采样后只剩 2 行，
    而 `PRECHECK_MIN_ROWS` 正好是 3 —— 门槛被悄悄抬高了。

    结论：这条判据对行数很敏感，**必须逐行看**。想再快只能从别处下手
    （比如只在疑似选取界面的帧上跑，而不是每帧都跑）。
    """
    try:
        from layout import cut          # noqa: PLC0415  上游的裁切

        crop = cut(frame, rules.SLOT_BAND, rules.BASE, rules.SLOT_ANCHOR)
        if crop is None or crop.size == 0:
            return 0
        b, g, r = cv2.split(crop)
        lo, hi = rules.PRECHECK_R_RANGE
        # 只在减法需要的地方转 int16（uint8 直接相减会回绕）
        r16 = r.astype(np.int16)
        diff_g = r16 - g.astype(np.int16)
        diff_b = r16 - b.astype(np.int16)
        mask = ((r16 >= lo) & (r16 <= hi)
                & (diff_g >= rules.PRECHECK_RG)
                & (diff_b >= rules.PRECHECK_RB)).astype(np.int8)
        run_lo, run_hi = rules.PRECHECK_RUN

        # 前后各补一列 0：一次 diff 得到全部行的「起、止」位置
        padded = np.zeros((mask.shape[0], mask.shape[1] + 2), np.int8)
        padded[:, 1:-1] = mask
        edges = np.diff(padded, axis=1)
        starts_all = edges == 1
        ends_all = edges == -1

        rows = 0
        for y in range(mask.shape[0]):
            s = np.flatnonzero(starts_all[y])
            if s.size == 0:
                continue
            e = np.flatnonzero(ends_all[y])
            if e.size != s.size:      # 理论上不会发生，保险
                continue
            lens = e - s
            if ((lens >= run_lo) & (lens <= run_hi)).any():
                rows += 1
        return rows
    except Exception:
        return 0


def _slot_pair_throttled(frame, engine):
    """`_slot_pair` 的频率限制版：先过频率限制，再过外观判据，最后才真跑 OCR。

    ## 为什么顺序是「先时间、后外观」（2026-10-25 改，用户报游戏内 CPU 高）

    原来是「先外观（slot_bars，3.8 毫秒）、后时间」。但游戏内**几乎每一帧**都会
    走到这里（一条传奇提示都没有），于是每 0.3 秒就白扔掉 3.8 毫秒 ——
    py-spy 实测 `slot_bars` 占 4.4%，全是空转。

    反过来先看时间：`rules.SLOT_PAIR_INTERVAL`（5 秒）内直接返回，
    外观判据一秒最多只跑一次，CPU 占比从 4.4% 降到可以忽略。

    **不会漏判**：节流窗口内本来就打算什么都不看，和原版行为一致 ——
    原版是「外观过了也要被时间挡掉」，所以两块判据的**联合结果**不变。
    """
    # ① 频率限制（最便宜，先挡掉绝大多数帧）
    now = time.monotonic()
    interval = getattr(rules, "SLOT_PAIR_INTERVAL", 5.0)
    with _SLOT_PAIR_LOCK:
        if now - _SLOT_PAIR_AT[0] < interval:
            return False
        # 先占住时间片：外观过了再真跑 OCR，外观不过也让这 5 秒内不再重算外观
        _SLOT_PAIR_AT[0] = now
    # ② 外观判据：明显不是选取界面的帧，连 OCR 都不用跑
    if slot_bars(frame) < rules.PRECHECK_MIN_ROWS:
        return False
    return _slot_pair(frame, engine)


# ─── 读名字（2026-10-25 用户要求：只读传奇名和玩家名，不认头像） ───
#
# 只在**已经认出是选取界面之后**才调用（那样一次要跑两次 OCR，不能让每帧都跑）。
# 读取结果按 "上次读到的是哪一帧" 缓存：同一个界面玩家会停好几十秒，
# 每帧都重读一遍没意义（一次 OCR 要 200 毫秒，见 captures/cpu_probe 的实测）。

_TEXT_CACHE: dict = {}
_TEXT_LOCK = threading.Lock()
_TEXT_CACHE_MAX = 8


def _split_slot(text):
    """把槽位里读到的一行字拆成 (是否还没选人, 文本)。"""
    if rules.PICKING_HINT in text:
        rest = text.replace(rules.PICKING_HINT, "").strip()
        return True, rest
    return False, text


def _looks_like_name(text):
    """像不像一个名字（挡掉数字、百分比、"L+R"、"15/25" 这类噪声）。

    判据（2026-10-25 实测定的，见 rules.py 第 4 条判据）：
      ① 至少 rules.NAME_MIN_LEN 个字符
      ② 含字母或汉字 —— 纯数字、纯符号都不算
    """
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < rules.NAME_MIN_LEN:
        return False
    return any(ch.isalpha() or "\u4e00" <= ch <= "\u9fff" for ch in stripped)


def _slot_cells(band_width):
    """每格在底部横带里的 x 区间（像素）：按 rules.SLOT_CUTS 的边界三等分。

    为什么不用 SLOTS 的 width：那个宽度是给"读名字"留余量的，换算到 2560 实际宽度
    有 400 像素，会把大厅右下角的枪械列表也圈进第 3 格（见 rules.SLOT_CUTS 的注释）。
    """
    first, second = rules.SLOT_CUTS
    width = float(band_width)
    return [
        (0.0, first * width),
        (first * width, second * width),
        (second * width, width),
    ]


def _slot_pair(frame, engine):
    """第 4 条判据：底部有没有**某一格**上下两行都有字（上都像名字）。

    只认右侧三条提示全都消失之后的选取界面（三人都选完了）。
    返回 True/False。任何异常都当 False（绝不能影响记录）。

    代价：一次横带 OCR，约 0.7 秒 —— 所以只在三条提示一条都没认出来时才调，
    见 `recognize()` 的注释。
    """
    try:
        rows = _read_band(engine, frame, rules.SLOT_BAND, rules.SLOT_ANCHOR)
    except Exception:
        return False
    if not rows:
        return False

    from layout import cut              # noqa: PLC0415

    try:
        crop = cut(frame, rules.SLOT_BAND, rules.BASE, rules.SLOT_ANCHOR)
        band_width = max(int(crop.shape[1]), 1)
        band_height = max(int(crop.shape[0]), 1)
    except Exception:
        return False

    band = rules.SLOT_BAND
    cells = _slot_cells(band_width)

    # 每格记下"上行有名字吗""下行有名字吗"（各记最靠近下边界/上边界的高度）
    upper = [None] * len(cells)
    lower = [None] * len(cells)
    for row in rows:
        if row["cx"] is None or row["cy"] is None:
            continue
        if not _looks_like_name(row["text"]):
            continue
        index = None
        for i, (left, right) in enumerate(cells):
            if left <= row["cx"] < right:
                index = i
                break
        if index is None:
            continue
        cy = row["cy"] / float(band_height)
        spread = row.get("spread")
        if rules.SLOT_ROW_LEGEND[0] <= cy <= rules.SLOT_ROW_LEGEND[1]:
            if upper[index] is None or cy > upper[index]:
                upper[index] = cy
        elif rules.SLOT_ROW_PLAYER[0] <= cy <= rules.SLOT_ROW_PLAYER[1]:
            # 下行是**带色**的玩家名；灰白的枪械名、图标残渣都不算（见 rules.MIN_PLAYER_SPREAD）
            if spread is None or spread < rules.MIN_PLAYER_SPREAD:
                continue
            if lower[index] is None or cy < lower[index]:
                lower[index] = cy

    # 同一格既要上行又有下行，且两行的**高度差**够大（见 rules.SLOT_ROW_MIN_GAP）
    for top, bottom in zip(upper, lower):
        if top is not None and bottom is not None and (bottom - top) >= rules.SLOT_ROW_MIN_GAP:
            return True
    return False


def read(frame, engine, my_name=None):
    """读选取界面上的名字。返回 dict，读不到的项是空字符串。

        {"mode": "子弹时间", "map": "不动产",
         "slots": {
            "left":  {"legend": "罗芭", "player": "careful",       "picking": False, "me": True},
            "mid":   {"legend": "侵蚀", "player": "[R99]阿宝宝宝宝", "picking": False, "me": False},
            "right": {"legend": "",     "player": "土豆烤肉",       "picking": True,  "me": False},
         },
         "me_slot": "left", "mate_slots": ["mid", "right"]}

    **engine 为 None 时返回空结果**（识别模型是惰性加载的：不玩的时候不该为这个把模型叫醒）。

    `my_name` 是**我们自己的玩家名**（上游 `post.player_name`，没读到就是 None）。
    三格的顺序是随机的（2026-10-25 用户确认"顺序是随机的"，头像也没有标记），
    所以"哪一格是我"只能靠名字比对：和 my_name 一样的那个格子 `me=True`，
    其余两格是队友。比不出（没名字、或三格的名字都对不上）时 `me_slot` 为 None，
    **不硬猜** —— 猜错会把队友的数据记到自己名下（原则 5）。

    结果按帧缓存：识别完一次就把本帧的答案记下来，同一帧再问直接给。
    """
    empty = _empty_result()
    if engine is None or frame is None:
        return empty

    key = (id(frame), frame.shape)
    with _TEXT_LOCK:
        hit = _TEXT_CACHE.get(key)
        if hit is not None:
            del _TEXT_CACHE[key]        # 用过就删：numpy 会复用 id，见 feature.py 的说明
            return _mark_me(hit, my_name)

    try:
        result = _read_uncached(frame, engine)
    except Exception:
        result = empty
    with _TEXT_LOCK:
        if len(_TEXT_CACHE) >= _TEXT_CACHE_MAX:
            _TEXT_CACHE.clear()
        _TEXT_CACHE[key] = result

    # 归位（我 / 队友）按本次的 my_name 现算，不缓存：名字比对很便宜，
    # 而且 my_name 可能在两次调用之间才读到（上游是先读画面、后读名字的）。
    return _mark_me(result, my_name)


def _mark_me(result, my_name):
    """按玩家名认出哪一格是我。返回一份新字典（不动缓存里的那份）。"""
    out = dict(result)
    out["slots"] = {k: dict(v) for k, v in result["slots"].items()}
    for cell in out["slots"].values():
        cell["me"] = False
    out["me_slot"] = None

    if my_name:
        key = _name_key(my_name)
        for name, cell in out["slots"].items():
            player = cell.get("player") or ""
            if player and _name_key(player) == key:
                cell["me"] = True
                out["me_slot"] = name
                break

    # 认不出"我"时 mate_slots 给空：宁可没有队友，也不能把可能是自己的那一格当队友写进库
    out["mate_slots"] = ([n for n in out["slots"] if n != out["me_slot"]]
                         if out["me_slot"] else [])
    return out


def _name_key(name):
    """比较玩家名用的写法：去掉空格（和上游 storage.name_key 同一个规则）。

    这里不 import 上游的：本文件夹要能脱离 Evo 单独跑
    （captures/legend_pick/ 下的验证脚本就是这么用的）。
    """
    return "".join((name or "").split())


def _empty_result():
    return {
        "mode": "",
        "map": "",
        "slots": {s["slot"]: {"legend": "", "player": "", "picking": False, "me": False}
                  for s in rules.SLOTS},
        "me_slot": None,
        "mate_slots": [],
    }


def _read_uncached(frame, engine):
    result = _empty_result()

    # ① 顶部一条宽块：OCR 自己分行，第一行是比赛类型、第二行是地图
    for role, text in zip(rules.HEADER_ROLES, _read_header(engine, frame)):
        result[role] = _fix_close_text(text)

    # ② 底部整条横带：一次读完，按坐标归到三格
    for cell_index, row in _read_slots(engine, frame):
        slot = rules.SLOTS[cell_index]["slot"]
        cell = result["slots"][slot]
        is_hint, text = _split_slot(row["text"])
        if is_hint:
            # 「现在请选择」占了**传奇名那一行**，说明这一格还没定传奇。
            # 这时传奇名留空，`_split_slot` 剩下的字（如果有）当玩家名。
            cell["picking"] = True
            if text and not cell["player"]:
                cell["player"] = text
            continue
        if row.get("upper"):
            # 上行且不是"现在请选择" → 这是传奇名
            if not cell["legend"]:
                cell["legend"] = text
        else:
            # 下行 → 玩家名
            if not cell["player"]:
                cell["player"] = text
    return result


def _fix_close_text(text):
    """把实测过的固定错字换成对的字（表在 rules.CLOSE_TEXT）。

    只做**整行完全匹配**的替换：局部替换会把玩家名里的字也改掉，
    而这张表是为"比赛类型"这种固定短词准备的。
    """
    text = (text or "").strip()
    if not text:
        return ""
    return rules.CLOSE_TEXT.get(text, text)


def _read_header(engine, frame):
    """读顶部：返回 [比赛类型, 地图名]（可能不足两条，读不到的就是空）。

    为什么按 y 分行而不按 roi 各切一刀：见 rules.HEADER 的说明 —— 两行贴得太近，
    切两刀会互相截断。这里一次读整块、按每个字块的 y 中心分成"上行 / 下行"。
    """
    band = rules.HEADER
    rows = _read_band(engine, frame, band["roi"], band["anchor"])
    if not rows:
        return []

    from layout import cut              # noqa: PLC0415  上游的裁切

    try:
        crop = cut(frame, band["roi"], rules.BASE, band["anchor"])
        band_height = max(int(crop.shape[0]), 1)
    except Exception:
        return []

    # 每一行的 y 中心属于上半还是下半：以整块高度的中线为界
    top, bottom = [], []
    for row in rows:
        if row["cy"] is None:
            continue
        (top if row["cy"] < band_height * 0.5 else bottom).append(row)

    def pick(group):
        if not group:
            return ""
        # 同一半里万一有多行，取最靠左的那行（正文都贴着左边，不会跑到右边去）
        group.sort(key=lambda r: (r["cx"] if r["cx"] is not None else 0))
        return group[0]["text"]

    return [pick(top), pick(bottom)]


def _read_band(engine, frame, roi, anchor):
    """裁一块读文字，返回带坐标的 [{text, score, cx, cy, spread}]。

    坐标是这块 roi 内的像素。`spread` 是字块颜色的三通道差（区分白字传奇名和
    带色的玩家名用，见 _is_legend_row）。读不出返 []。

    任何异常都吞掉 —— 读名字失败只让名字空着，绝不能影响记录（原则 5）。
    """
    from layout import cut              # noqa: PLC0415  上游的裁切

    try:
        crop = cut(frame, roi, rules.BASE, anchor)
        if crop is None or crop.size == 0:
            return []
        result = engine(crop)
    except Exception:
        return []

    rows = []
    try:
        import numpy as np               # noqa: PLC0415

        texts = getattr(result, "txts", None) or ()
        scores = getattr(result, "scores", None) or ()
        boxes = getattr(result, "boxes", None)
        for i, text in enumerate(texts):
            text = (text or "").strip()
            if not text or len(text) < rules.MIN_TEXT_LEN:
                continue
            score = float(scores[i]) if i < len(scores) else 1.0
            if score < rules.MIN_TEXT_SCORE:
                continue
            if boxes is None or i >= len(boxes):
                continue
            points = np.array(boxes[i]).reshape(-1, 2)
            x0, x1 = int(points[:, 0].min()), int(points[:, 0].max())
            y0, y1 = int(points[:, 1].min()), int(points[:, 1].max())
            rows.append({
                "text": text,
                "score": score,
                "cx": float(points[:, 0].mean()),
                "cy": float(points[:, 1].mean()),
                "spread": _color_spread(crop, y0, y1, x0, x1, np),
            })
    except Exception:
        return []
    return rows


def _color_spread(crop, y0, y1, x0, x1, np):
    """字块里"最亮那批像素"的颜色三通道差（BGR 最大减最小）。

    取最亮的 15% 而不是整块平均：整块里混着背景和头像，平均下来颜色会被冲淡。
    白字（传奇名）三通道几乎一样，差 < 25；带色的玩家名差 120~190（实测两张截图）。
    """
    try:
        height, width = crop.shape[:2]
        y0c, y1c = max(y0, 0), min(y1, height)
        x0c, x1c = max(x0, 0), min(x1, width)
        if y1c - y0c < 2 or x1c - x0c < 2:
            return 0.0
        patch = crop[y0c:y1c, x0c:x1c].reshape(-1, 3).astype("float32")
        luma = patch.mean(axis=1)
        threshold = np.percentile(luma, 85)
        bright = patch[luma >= threshold]
        if bright.size == 0:
            return 0.0
        color = bright.mean(axis=0)
        return float(color.max() - color.min())
    except Exception:
        return 0.0


def _read_slots(engine, frame):
    """读底部横带，返回 [(格序号, {text, score})]，同一格里上行在前、下行在后。

    为什么必须用坐标：OCR 是**按行**返回的，实测这张图的顺序是

        罗芭 / 侵蚀 / 现在请选择 / 2 / careful / [R99]阿宝宝宝宝 / 土豆烤肉

    也就是"三个传奇名先说完，再说三个玩家名"，中间还插了一个噪声 "2"。
    按顺序归格会把格 2 的传奇名配到格 3 的玩家名上 —— 全错位。
    所以用字块的 x 中心落在哪一格、y 中心在上半还是下半来判。

    上下行怎么分：见 rules.SLOT_SPLIT_RATIO（y）和 rules.SLOT_COLOR_SPREAD（颜色兜底）。
    """
    band = rules.SLOT_BAND
    rows = _read_band(engine, frame, band, rules.SLOT_ANCHOR)
    if not rows:
        return []

    # 横带宽度（基准像素）换算成实际裁剪宽度，用来把 x 归到哪一格
    from layout import cut              # noqa: PLC0415

    try:
        crop = cut(frame, band, rules.BASE, rules.SLOT_ANCHOR)
        band_width = max(int(crop.shape[1]), 1)
        band_height = max(int(crop.shape[0]), 1)
    except Exception:
        return []

    # 每格在横带内的 x 区间（基准像素 → 按比例换算到实际宽度）
    scale = band_width / float(band[2])
    cells = []
    for item in rules.SLOTS:
        left = (item["x"] - band[0]) * scale
        cells.append((left, left + item["width"] * scale))

    out = []
    for row in rows:
        if row["cx"] is None or row["cy"] is None:
            continue
        index = None
        for i, (left, right) in enumerate(cells):
            if left <= row["cx"] < right:
                index = i
                break
        if index is None:
            continue
        upper = _is_legend_row(row, band_height)
        if upper is None:
            continue                     # y 和颜色都说它不像正文 → 噪声，丢掉
        out.append((index, row, upper))

    # 同一格里上行排前面，保证"先传奇名后玩家名"
    out.sort(key=lambda item: (item[0], 0 if item[2] else 1,
                               item[1]["cy"] if item[1]["cy"] is not None else 0))
    return [(index, {"text": row["text"], "score": row["score"], "upper": upper})
            for index, row, upper in out]


def _is_legend_row(row, band_height):
    """这个字块是上行（传奇名）还是下行（玩家名）？是噪声返回 None。

    两级判断，先看 y、再看颜色：
      ① y 中心 < 0.48H  → 上行；> 0.48H → 下行（实测上下行分得很开，见 rules）
      ② y 落在 0.42~0.55 这一小段（噪声区）时，改用颜色：
         带色的（三通道差 > SLOT_COLOR_SPREAD）是玩家名，否则当噪声丢掉。
    """
    cy = row["cy"]
    if cy is None:
        return None
    ratio = cy / float(band_height)

    if ratio < rules.SLOT_SPLIT_RATIO - 0.06:
        return True
    if ratio > rules.SLOT_SPLIT_RATIO + 0.07:
        return False

    # 落在分界附近：只有明确带色才收下（玩家名），否则是噪声
    spread = (row.get("spread") or 0.0) if isinstance(row, dict) else 0.0
    return False if spread >= rules.SLOT_COLOR_SPREAD else None
