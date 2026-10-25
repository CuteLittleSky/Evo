"""从派喵的地图轮换接口查「某个时刻在打哪张图」（2026-10-24 用户要求）。

## 为什么要它

上游只有**开局画面**左上角横幅第二行才显示地图（`config/match_start.json` 的 `map`）。
那几帧一闪而过：跳得快、或者当时在看手机，这一局的 `matches.map` 就永远是空的。
用户 2026-10-24 实测：库里 9 局有 6 局 map 为空。

而地图轮换是**公开的、按时间片**的：大逃杀 90 分钟一张、混战 15 分钟一张。
只要知道开局时刻和当时在打哪个模式，就能反查出那张图 —— 不用读屏幕。

## 接口

    https://tools.skycraft.cn/apex/api/maprotation?evo=true

就是派喵官网「地图轮换」页用的那个接口（ApexPacks 仓库
`server/api/maprotation/index.get.ts`），服务端对 `evo=true` 不特殊处理，这里也照传，
将来服务端要区分来源时不用改客户端。响应里每个模式组：

    {"current": {...}, "next": {...}, "maps": [...], "upcoming": [5 条], ...}

`current` / `upcoming[i]` 都带绝对的 `start` / `end`（**Unix 秒**），
所以**任意时刻**都能查出当时在打哪张图，不需要额外的新接口。

`upcoming` 有 5 条、每条 90 分钟，再加上 `current`，大逃杀覆盖「上一片 + 现在 + 未来 5 片」
≈ 过去 1.5 小时 ~ 未来 7.5 小时，回推当天的局绰绰有余。

混战只有 15 分钟一片，5 条 `upcoming` 只够 75 分钟；2026-10-24 实测服务端给了 7 片、
总跨度 1.8 小时（06:15 ~ 08:00）。也就是说**混战只能回推最近一小时半以内的局**，
更早的局会「查不到」—— 这是数据源本身的限制，不是 bug，留空即可。

**接口只覆盖「最近 ~1 天」**：2026-10-24 实测最早一片是当天 04:00，
所以昨天、前天的老局一律查不到。「查不到就留空」正好把这种情况挡在外面。

## 模式对不上的情况（用户 2026-10-24 说明）

限时活动会**顶替外卡**。服务端 `hideWildcard` 一开（index.get.ts:484-490），
响应里连 `wildcard` 这个 key 都不出现，同时 `event` 从「空壳」变成有 `current` 的组。
所以：

    外卡（wildcard）某一刻查不到   →  退回查 event（限时活动）
    event 也没在跑                →  就是查不到，不猜

其余映射（用户 2026-10-24 明确）：未上榜 = 普通匹配 = battle_royale。

## 开关

**只有真的在用派喵云同步时才发请求**（用户 2026-10-24 要求）。
判断用的是云同步模块自己的条件：同意过 + 配对过 + 开关打开（`cloud/sync.py:70-71`）。

## 内存 / 依赖

- 只用标准库 `urllib`（和 `cloud/pairing.py` 一样），**不新增依赖**
- 响应很小（一个 JSON，几 KB），结果缓存在内存里，同一小时的局只请求一次
- 请求在网络线程里做，最多等 5 秒；失败一律当「查不到」，绝不影响记录
"""
from __future__ import annotations

import json
import time
import urllib.request

# 接口地址（相对派喵云的根）。根从 cloud/state.py 退出来（.../apex/api），
# 这样自检时把 EVO_CLOUD_SERVER 指到本机假接口，这里也跟着走。
API_PATH = "/maprotation?evo=true"
TIMEOUT = 5.0
# 同一份响应用多久（秒）。地图轮换最短 15 分钟一片，缓存 5 分钟不会读到过期数据，
# 又能让「一局结算 + 一局结算」共用一个请求。
CACHE_TTL = 300.0

# 我们的 mode → 接口里的模式组（用户 2026-10-24 定的口径）
MODE_GROUPS = {
    "ranked": ("ranked",),
    "pubs": ("battle_royale",),          # 未上榜也是普通大逃杀
    "wildcard": ("wildcard", "event"),   # 活动顶替外卡：外卡查不到就查活动
    "bot": ("battle_royale",),           # 机器人大逃杀走的是同一个大逃杀轮换
}
# 混战只有 15 分钟一片、upcoming 也只 5 条，回推的容错比大逃杀小得多，单独标出来
SHORT_CYCLE = ("mixed_control", "mixed_bullet_time", "mixed_team_deathmatch")
for _mode in SHORT_CYCLE:
    MODE_GROUPS[_mode] = ("mixtape",)

# 接口里的地图名（英文 / 带 POI）→ config/match_start.json 里那一套中文名。
# 用户 2026-10-24 决定：写进 matches.map 的值要跟「开局横幅读到的」一模一样，
# 否则面板筛选、已知地图名单、测试答案会各说各话。
_ENGLISH = {
    "world's edge": "世界尽头",
    "worlds edge": "世界尽头",
    "storm point": "风暴点",
    "e-district": "残月",          # 国服旧译名；config 的已知地图里它是「残月」
    "kings canyon": "诸王峡谷",
    "olympus": "奥林匹斯",
    "broken moon": "破碎月亮",
}
# 接口里的 code（最稳的字段，大逃杀/排位/活动/混战都带）→ 中文名。
# 2026-10-24 实测拿到的 code：olympus / broken_moon / kings_canyon / mix_s_gungame_estates，
# 另外几个按 ApexPacks 的 ALL_MAP_ASSETS 补上；以后出新图就在这里加一行。
_CODES = {
    "olympus": "奥林匹斯",
    "broken_moon": "破碎月亮",
    "kings_canyon": "诸王峡谷",
    "uhkc": "诸王峡谷",
    "worlds_edge": "世界尽头",
    "des_hu": "世界尽头",
    "storm_point": "风暴点",
    "tropic": "风暴点",
    "district": "残月",
    "e_district": "残月",
}
# 接口给的 nameI18n['zh-CN'] 形如「血之契约（诸王峡谷）」，取括号里那部分再查一次
_CJK = {
    "世界尽头": "世界尽头",
    "风暴点": "风暴点",
    "残月": "残月",
    "诸王峡谷": "诸王峡谷",
    "奥林匹斯": "奥林匹斯",
    "破碎月亮": "破碎月亮",
}
# 接口里混战那些 POI 名（Hammond Labs - Control 之类）。
# 2026-10-24 用户要求补上：混战也是能玩的模式，不该因为是 POI 就放弃。
# 中文名取自 ApexPacks 仓库 `locales/zh-CN.json` 的 `maprotation.maps`
# （那是游戏里实际显示的名字，82 条，混战 12 种组合全在里面）。
# 用户 2026-10-24 决定：**只存 POI 的中文名**，不带后面的「 - 控制」，
# 这样 map 列和大地图同一套写法（「哈蒙德实验室」），面板筛选也干净。
# 键是接口给的英文全名（小写），值是中文 POI 名。
_MIXTAPE = {
    "hammond labs - control": "哈蒙德实验室",
    "barometer - control": "晴雨表",
    "caustic treatment - control": "侵蚀实验室",
    "estates - tdm": "不动产",
    "fragment - tdm": "碎片镇",
    "skulltown - tdm": "骷髅镇",
    "monument - tdm": "纪念碑",
    "estates - gun run": "不动产",
    "fragment - gun run": "碎片镇",
    "skulltown - gun run": "骷髅镇",
    "monument - gun run": "纪念碑",
}
# 混战 code → POI 中文名。接口的 code 是 mix_s_<玩法>_<地点>，
# 有的地方带 _2 后缀（同一张图在轮换里出现两次），所以先按整串查，再退到去后缀的键。
_MIXTAPE_CODES = {
    "mix_s_control_labs": "哈蒙德实验室",
    "mix_s_control_labs_2": "哈蒙德实验室",
    "mix_s_control_barometer": "晴雨表",
    "mix_s_control_caustic": "侵蚀实验室",
    "mix_s_tdm_estates": "不动产",
    "mix_s_tdm_fragment": "碎片镇",
    "mix_s_tdm_skulltown": "骷髅镇",
    "mix_s_tdm_monument": "纪念碑",
    "mix_s_gungame_estates": "不动产",
    "mix_s_gungame_fragment": "碎片镇",
    "mix_s_gungame_fragment_2": "碎片镇",
    "mix_s_gungame_skulltown": "骷髅镇",
}
# 单看 POI 名（去掉「 - 控制」之后）也要认得出，兜底用
_MIXTAPE_POI = {
    "hammond labs": "哈蒙德实验室",
    "barometer": "晴雨表",
    "caustic treatment": "侵蚀实验室",
    "estates": "不动产",
    "fragment": "碎片镇",
    "skulltown": "骷髅镇",
    "monument": "纪念碑",
}

_cache = {"at": 0.0, "data": None}


class Unavailable(Exception):
    """查不到（没开云同步、网络不通、接口变了、时刻对不上）。一律当「不填」。"""


def enabled():
    """现在该不该联网查地图（用户 2026-10-24：只在启用派喵云同步后才用）。

    条件跟云同步线程启动时一模一样，见 cloud/sync.py:70-71。任何一步出错都当「没开」。
    """
    try:
        from cloud import state as cloud_state  # noqa: PLC0415

        snapshot = cloud_state.load()
        return bool(
            cloud_state.agreed(snapshot)
            and cloud_state.paired(snapshot)
            and snapshot.get("enabled")
        )
    except Exception:
        return False


def _api_url():
    from cloud import state as cloud_state  # noqa: PLC0415

    root = cloud_state.pair_server({})      # .../apex/api（与配对接口同一个根）
    return root.rstrip("/") + API_PATH


def _api_headers():
    """`evo=true` 是给 Evo 用的通道，要带上配对令牌（2026-10-24 用户要求）。

    和云同步走的是同一套：`Authorization: Bearer <device.token>`
    （见 `cloud/pairing.py:105-107`、`cloud/sync.py:436`，那两处也是这么带的）。
    拿不到令牌就不带 —— 服务端那边会按「未鉴权」处理，我们当查不到，不猜。
    """
    headers = {"User-Agent": "EvoTracker/map-lookup", "Accept": "application/json"}
    try:
        from cloud import state as cloud_state  # noqa: PLC0415

        token = (cloud_state.load().get("device") or {}).get("token")
        if token:
            headers["Authorization"] = "Bearer %s" % token
    except Exception:
        pass
    return headers


def fetch(force=False):
    """拿一份地图轮换数据（带缓存）。失败抛 Unavailable。"""
    now = time.monotonic()
    if not force and _cache["data"] is not None and now - _cache["at"] < CACHE_TTL:
        return _cache["data"]
    try:
        req = urllib.request.Request(
            _api_url(),
            headers=_api_headers(),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise Unavailable(str(exc)) from exc
    if not isinstance(data, dict):
        raise Unavailable("响应不是 JSON 对象")
    _cache["data"], _cache["at"] = data, now
    return data


def clear_cache():
    _cache["data"], _cache["at"] = None, 0.0


def group_of(mode):
    """我们的 mode → 要查的模式组列表（按优先级）。不认识的模式返回空元组（= 不猜）。"""
    return MODE_GROUPS.get(mode or "", ())


def slots(data, mode):
    """把某个模式组的 current + next + upcoming 摊平成 [{map, start, end, code}]，按时间排序。

    2026-10-24 起接口的 `evo=true` 通道还会多给一段 `data["evo"]["previous"]`，
    里面是每个模式组的**上一片**。回推一局时开局时刻常常落在上一次轮换里，
    只靠 current + upcoming 会差一张图，所以这里把 previous 一并摊进来。
    老版接口没有 `evo` 段时照常工作（只是少一片）。
    """
    out = []
    evo = data.get("evo") if isinstance(data.get("evo"), dict) else {}
    previous = evo.get("previous") if isinstance(evo.get("previous"), dict) else {}
    seen = set()                 # 按 (start, end) 去重，见下面注释
    for key in group_of(mode):
        block = data.get(key)
        if not isinstance(block, dict):
            continue              # 这组根本没在跑（活动没开时 event 里就没有 current）
        prev = previous.get(key) if isinstance(previous.get(key), dict) else None
        for item in [block.get("current"), block.get("next"),
                     *(block.get("upcoming") or []), prev]:
            if not isinstance(item, dict):
                continue
            try:
                start, end = int(item["start"]), int(item["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if end <= start:
                continue
            # 服务端的 `next` 就是 `upcoming[0]`，两层都摊平会重复一片。
            # 同一段时间只会有一张图，按 (start, end) 去重就够了
            # （同一段时间两个不同图名 = 服务端数据有问题，宁可只留第一条）。
            span = (start, end)
            if span in seen:
                continue
            seen.add(span)
            out.append({
                "map": map_name(item),
                "code": item.get("code"),
                "start": start,
                "end": end,
            })
    out.sort(key=lambda s: s["start"])
    return out


def map_name(item):
    """接口里的一个地图条目 → 写进 matches.map 的中文名；认不出返回 None。

    认的顺序（2026-10-24 实测定的）：code 最稳（broken_moon / olympus 这种），
    其次是英文 map 名，最后才是 nameI18n —— 因为只有活动图带 nameI18n，
    大逃杀和排位只给 `"map": "Broken Moon"`，先看 nameI18n 会让它们全部认不出。

    混战（控制 / 子弹时间 / 团队死斗）的图名带 POI，如 `Hammond Labs - Control`。
    用户 2026-10-24 决定只存 POI 的中文名（「哈蒙德实验室」），不带「 - 控制」。
    """
    code = item.get("code")
    if isinstance(code, str):
        if code in _CODES:
            return _CODES[code]
        # 混战的 code 是 mix_s_control_labs 这种，整串查不到就按 POI 部分查
        if code in _MIXTAPE_CODES:
            return _MIXTAPE_CODES[code]
    raw = item.get("map") or item.get("name") or ""
    i18n = item.get("nameI18n") or {}
    zh = i18n.get("zh-CN") if isinstance(i18n, dict) else None
    for text in (raw, zh):
        if not text:
            continue
        inner = text
        if "（" in text and "）" in text:
            inner = text.split("（", 1)[1].split("）", 1)[0]
        if inner in _CJK:
            return _CJK[inner]
        low = inner.strip().lower()
        if low in _ENGLISH:
            return _ENGLISH[low]
        # 混战的「Hammond Labs - Control」这种：先查整串，再看横杠前的 POI
        if low in _MIXTAPE:
            return _MIXTAPE[low]
        head = low.split(" - ", 1)[0]
        if head in _MIXTAPE_POI:
            return _MIXTAPE_POI[head]
        if head in _ENGLISH:
            return _ENGLISH[head]
    return None


def at(data, mode, when):
    """when（Unix 秒）那一刻，mode 这个模式在打哪张图。查不到抛 Unavailable。

    同一时刻可能对上不止一条（轮换边界前后各一条）：这里直接返回**第一条覆盖该时刻**的，
    调用方要的是「推不推」，是否跨边界由 resolve() 决定。
    """
    if not group_of(mode):
        raise Unavailable("不认识的模式：%r" % (mode,))
    for slot in slots(data, mode):
        if slot["start"] <= when < slot["end"]:
            return slot
    raise Unavailable("那一刻（%s）不在任何地图窗口里" % _fmt(when))


def resolve(mode, spans, data=None):
    """用几个候选时刻推地图。返回 {"map", "code", "when", "why"}，不确定就抛 Unavailable。

    spans：[(时刻(Unix 秒), 说明)]，按「离真实开局最近」排在前面。

    规则（用户 2026-10-24 定）：所有候选时刻都算出**同一张图**才采用；
    只要两个候选落到不同地图上（说明这一局跨了轮换边界、或者开局时刻估得不准），
    就不填 —— 宁可留空，也不写一个可能错的图。
    """
    data = fetch() if data is None else data
    seen = {}
    for when, why in spans:
        try:
            slot = at(data, mode, when)
        except Unavailable:
            continue
        if slot["map"] is None:
            raise Unavailable("%s 那一刻的地图认不出中文名（%s）" % (why, slot["code"]))
        seen.setdefault(slot["map"], (when, why, slot))
    if not seen:
        raise Unavailable("候选时刻都落在地图窗口之外")
    if len(seen) > 1:
        raise Unavailable("候选时刻跨了轮换边界：%s" % "、".join(sorted(seen)))
    when, why, slot = next(iter(seen.values()))
    return {"map": slot["map"], "code": slot["code"], "when": when, "why": why}


def _fmt(secs):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(secs))
    except Exception:
        return str(secs)
