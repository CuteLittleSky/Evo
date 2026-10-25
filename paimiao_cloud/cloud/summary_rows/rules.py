"""总结页读数的规则：纯数据，改这里不用动代码。

坐标怎么量出来的、为什么上游的框不行、为什么不用「只框数字」的小框，
见 detect.py 开头那段。这里都是基准分辨率 1920×1080 的坐标。

## 每一行整行读，数字取「标签后面的那条」

实测（2560×1440 真机全屏，`captures/live/`、`captures/mode_probe/`）：
卡片的行距 86 像素，整行框 `[740, y, 260, 60]` 里同时有标签和数字，
RapidOCR 对整行读得很准（`击杀/助攻` + `6/4/7`、`造成伤害` + `1,374`）。

## 行位是「相对伤害那一行」定的

`ROW_FRAMES` 里只有 `damage` 的 y 是实测的；生存时间及以下都从 damage 往下数。
普通版式的卡片是
`击杀/助攻/击倒 → 造成伤害 → 生存时间 → 急救 → 重生`，
外卡那张多出一行「自我急救 → 最后幸存」，从生存时间那一行起整体再往下挪一行。
前两行（kda、damage）两种版式一样，所以外卡只影响后面的 `rows`。
"""
from __future__ import annotations

# 基准分辨率（和 config/summary.json 的 base_resolution 一致）
BASE_RESOLUTION = [1920, 1080]

# 什么样的状态算“读到了”。上游 read_field 的 status：
#   ok / low_score        → 已经有值（low_score 上游会保留、只标待确认）
#   not_found             → 一片空白，换下一块候选再试
#   parse_fail            → 读到字了但格式不对，换下一块（可能框到了邻近的行）
#   out_of_range          → 格式对、数值离谱，也换下一块
GOOD_STATUSES = ("ok", "low_score")

# 卡片每一行的间距（基准像素）。上面「名次 / 小队总击杀」那一角的框读不到时，
# 往下再探两行兜底（见 detect.read_with_candidates）
CARD_ROW_HEIGHT = 86

# 这些标签出现在「自己那张卡片」上 = 外卡（终极等）模式。
# 实测 `captures/samples_wildcard/总结页_外卡_2560x1440.png` 与
# `captures/live/20261024_224044/02_225753.png`：外卡卡片的「自我急救 / 最后幸存」
# 两行标签和值正好落在上面 ROW_FRAMES 的 revives / respawns 行位里，
# 所以读到这两个标签就够判断，不用另外算行数偏移。
WILDCARD_LABELS = ("最后幸存", "自我急救")

# 每一行的标签（整行框读出来、以它结尾的那条就是标签；它后面那条才是数字）
LABELS = {
    "kda": "击倒",
    "damage": "造成伤害",
    "survival_time": "生存时间",
    "revives": "自我急救",
    "respawns": "最后幸存",
}

# 标签可以只读出一半（`击杀/助攻` 是 `击杀/助攻/击倒` 少认了一个字）。这时标签后面
# 那几条是**没读全的值**（实测普通卡片上读成 `5/6`，而小队总击杀是 16 的那种局，
# 真值是 `5/16`），所以整块用不了，要按标签位置把框往下挪开再读 —— 见 detect.read_row。
# 标签只对上前半截时，值也是半截，给低分让用户确认一下。
# 上游把低于 `min_score` 的读数留下来、整局标「待确认」—— 宁可让人看一眼，不悄悄写错。
PARTIAL_SCORE = 0.4


def label_state(name, text):
    """这条文字是不是这一行的标签：`hit` 整段对上、`part` 只对上前半截、`None` 不是。"""
    label = LABELS.get(name, "")
    if label and text.endswith(label):
        return "hit"
    if label and text.endswith(label[:-1]):      # 「击杀/助攻」是「击杀/助攻/击倒」少认了一个字
        return "part"
    return None


def value_state(name, values):
    """标签后面那几条里的数字，和 `LABEL_VALUES` 比：全对上，还是有一条对不上。

    `None` = 还没读到数字。这条数不可信的两种情形：
      - 标签只对上前半截：那条数也可能是半截（`5` 是 `5/16` 少读的）
      - 标签整段对上了，后面却有对不上的数：说明框又骑到下一行去了
    """
    if not values:
        return None
    own = LABEL_VALUES.get(name) or ()
    return "full" if all(value in own for value in values) else "partial"


def row_marker(name, text):
    """这条文字是不是这一行（随便哪种行）的标签。"""
    return any(label_state(field, text) for field in LABELS)


# 标签的下沿在整行框里大概落在多少像素高（框是 `ROW_FRAMES` 那 76 像素高的）。
# 实测（四张真机图，框读出来的文字块）：
#   行正好卡在框里（普通卡片上半截、外卡整张卡片）→ 标签 18–24、下沿 74–80
#   行整行往下挪了一截（控制模式的卡片）→ 标签 0、下沿 39–44
#   行整行往上挪了一截（生存时间、自我急救那几行）→ 标签在框外面，框里只剩数字
# 差得多就用「量到的位置」把框往下挪回去再读一次（detect._read_row）。
LINE_TOP = {
    "kda": 22,
    "damage": 20,
    "survival_time": 17,
    "revives": 15,
    "respawns": 15,
}

# 量到的位置和上面差多少才认为「这一行挪位了」
SHIFT_TOLERANCE = 10

# 每一行的「整行框」：标签 + 数字都在里面（基准坐标 [x, y, 宽, 高]）。
# 实测（control_summary / 01_233314 / 02_225753 / 外卡样本），这四块在四张图上都能读到东西：
#   kda           `3,354 评分 1625` / `1,127 降级近战攻击 0` / `2,715 生存时间 11:15`
#   damage        `已占领区域 5` / `最后幸存 2 自我急救` / `0 a 给予 0`
#   survival_time `905`（普通版式）/ `最后幸存 2 自我急救`（外卡）
#   revives       `2` / `1`（普通版式）/ 桌面的「邀请加入群」（外卡，见下）
#   respawns      `ID：2160456899`（卡片底部的 ID，读出来格式不对 → 空着）
#
# 左边从 740 挪到 738、下面多给几像素，是因为框正好卡在两行之间时 OCR 才肯出字，
# 卡得太紧反而整块拒绝（实测见 detect.py 开头「为什么不能只框数字」）。
ROW_FRAMES = {
    "kda": [738, 372, 300, 76],
    "damage": [738, 444, 300, 76],
    "survival_time": [738, 530, 300, 76],
    # 下面两行只给 240 宽：右边再往右就是传奇立绘，250~300 宽的位置上会混进它的杂字
    # （实测 2026-10-25：`自我急救` 那一行右边读出一个 `a`，拼成 `0ia`）。
    # 起点按外卡卡片实测（两张样本一致，`captures/mode_probe/probe_bottom_rows.py`）：
    #   最后幸存 标签 y 595..618、数字 y 620..649
    #   自我急救 标签 y 666..688、数字 y 690..721
    # 起点给 590 / 660：框顶落在标签上方几像素，整行「标签 + 数字」都在框里，
    # 不会像旧坐标（702 / 616）那样只切到标签下沿、把上一行的数字当成这一行的。
    "revives": [738, 660, 240, 76],
    "respawns": [738, 590, 240, 76],
}

# 整行框读不到标签时，往下多试几档（(往下挪几像素, 高度)）。
# 每一档都要读到标签才算数；标签在、数字不在的档也留着（那个数字是邻行的，只当兜底）。
# 为什么不一律用同一个高度：框越高越容易把「上一行的数字」也读进来，
# 那种情形只能靠位置检查（LABEL_VALUES）保底 —— 所以从「往下一点、矮一点」开始试，
# 框骑在行中间、行下沿两种情形都能读到干净的一行。
#
# 20~28 那几档是给「框顶撞进上一行」用的（外卡 `自我急救` 那一行，实测 2026-10-25）：
# 上一行的 `0` 在框顶 8 像素处赖着，挪 16 像素时它和本行的 `2` 一起进框、
# 被 OCR 拼成 `U`，挪到 20~28 才干净（`['自我急救', '2']`）。再往下就开始撞下一行了。
ROW_PROBES = ((0, 76), (8, 76), (16, 76), (20, 68), (24, 68), (28, 68),
              (0, 68), (8, 68), (16, 68))

# 单行读数里，标签后面的数字最多往后找几条（防止一行里数字特别多时瞎找）
TAIL_LIMIT = 4

# 外卡卡片上这两行的标签（`自我急救` / `最后幸存`）。这两行的**行位**和普通卡片的
# 「急救」「重生」完全一样，所以读法不变，只是标签不同、而且只在那一行真的带着
# `最后幸存` 时才把这一局算作外卡 —— 判据统一放在 `wildcard/detect.py`
# （`_WILDCARD_LABELS`），这里不重复一份，免得两处说法打架。
FOREIGN_LABELS = ("self_revive", "last_survive")

# 每一行「自己那个数字」长什么样（用来判断后面那条是不是本行的值，也用来打分）。
# 实测（`captures/mode_probe/control_summary.png`、`captures/live/.../01_233314.png`、
# `captures/samples_wildcard/总结页_外卡_*.png`）：
#   kda 行读出来是 `['击杀/助攻', '3/10']`、`['击杀/助攻', '5/6']`、
#       `['击杀/助攻', '6/4/7']`；而 `['击杀/助攻', '3,334']` 那种是框往上多带了上一行的伤害
#   survival_time 行读出来是 `11:18` / `2:59`（普通）或 `最后幸存 2`（外卡）
#   revives 就是单个小数（`1` / `2` / `0`）
#
# 外卡那两行有个坑（2026-10-25 真机截图 `总结页_外卡_第4名`）：框顶先撞上的是
# **上一行**的数字（`自我急救` 那一行，框顶 8 像素处是 `最后幸存` 的 `0`），而
# 「先碰到谁就取谁」会把这个 0 当成自我急救的次数（真值是 2）。所以：
#   revives 这一行允许 `0` 也当合理值（它确实可能是 0 次），但**记一笔「可能是上一行的」**，
#   打分降低 → 整局标「待确认」，用户在面板上看一眼；
#   respawns 没有「标签在上、值在下」的情形（它是卡片最下面一行），照旧严格。
LABEL_VALUES = {
    "kda": ("3/10", "5/6", "6/4/7"),
    "damage": ("1,374", "1,681"),
    "survival_time": ("11:18", "2:59"),
    "revives": ("0", "1", "2"),
    "respawns": ("1", "2", "8"),
}

# 标签**上面**那条数字算不算这一行的值（只在标签这一类行上允许，见上面的注释）
ALLOW_ABOVE = {
    "revives": ("0", "1", "2"),
}

# 上一行的数字被当成这一行的值（标签在上、值在下）时给的分数：低分 → 整局「待确认」
ABOVE_SCORE = 0.3

# 只用来「看看这行在不在」的字段；它们自己不算问题：
#   kda 上游的 KEY_FIELDS 会拿它的 status 报问题，所以固定给它 ok（不参与报错）
#   revives / respawns / survival_time 普通模式的卡片上根本没有这两行，读不到是正常的
LOCATOR_FIELDS = ("kda", "survival_time", "revives", "respawns")


def row_frame(field):
    """某个字段的整行框。"""
    x, y, w, h = ROW_FRAMES.get(field) or [738, 372, 300, 76]
    return [x, y, w, h]


def row_candidates(field):
    """整行框的候选，按顺序试（同一列，往下挪几像素、换高度）。"""
    x, y, w, h = row_frame(field)
    return [[x, y + dy, w, hh] for dy, hh in ROW_PROBES]


def row_candidates_by_frame(first):
    """从某一档框往下接着试的候选（第一档可以不是列表里的第一档）。"""
    x, y, w, _ = first
    rest = [[x, y + dy, w, hh] for dy, hh in ROW_PROBES]
    return [frame for frame in rest if frame[1] >= y]


def patch_config(cfg):
    """把上游 summary.json 的配置复制一份（字段表原样留着）。

    - 不改上游那份 dict（它的调用方可能正拿着用）
    - 顶部两格（名次、小队总击杀）还是上游的框，只是读的时候多往下探一行兜底
    - 认不出的配置（没有 fields）返回 None，调用方走原路
    """
    if not isinstance(cfg, dict):
        return None
    fields = cfg.get("fields")
    if not isinstance(fields, dict):
        return None
    fixed = dict(cfg)
    fixed["fields"] = {name: dict(spec) for name, spec in fields.items()}
    return fixed
