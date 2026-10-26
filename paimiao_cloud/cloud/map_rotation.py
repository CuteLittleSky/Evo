"""地图轮换：从派喵的 tools.skycraft.cn 取轮换表，显示在面板和网页里。

思路、边界、以及为什么这么做，见 docs/地图轮换方案.md。

设计要点（和 CLAUDE.md 的原则对上）：

  ① 网络只在这个模块的**后台线程**里。面板的 Flask 请求永远只读内存里的快照，
     绝不因为上游挂掉而卡住 —— 断网时显示上一次拿到的表 +「多久之前」。
  ② 上游（小站）只认自己的 JSON 形状，我们不复制它的代码；解析函数对缺字段宽容，
     季换季、字段改名时降级成「只显示拿到的部分」，不抛异常。
  ③ 时间一律用 start/end 时间戳，**不在本地推算**「90 分钟一轮」—— 上游改时长
     （事件模式、混合模式都不一样）我们不用改。时钟偏差优先用 JSON 里的 `nowSecs`
     （服务端此刻的秒数），没有再退回响应头的 `Date`。注意 Date 只有秒级、还可能被
     中间缓存过，所以只用来兜「本机系统时间差了几分钟」这种大偏差。
  ④ 接口没有 CORS 头、页面上也没有历史轮换表：所以网页要取数就走 Evo 自己的
     Flask 代理（/maprotation/api.json），不能直接 fetch 上游。
  ⑤ 用**公开接口**（不带 `?evo=true`）：带 evo 要绑定派喵云同步登录才有权限（401），
     而地图轮换是所有人都会看的东西，不该要求登录。中文地图名上游已经对公开请求也
     下发了（`nameZh` / `mapNames`），这里优先用中文名，取不到再退回英文名。
  ⑥ `rankedSchedule` 里的赛段 + 天梯战轮次（rumble.rounds）都用了：头部显示
     「S30 赛段 2 · 距结束还有 9 天」和「天梯战 · 第 9 轮」（与 ApexPacks 网页、
     小程序的算法一致：**永远从 rounds 现算**，不信可能过期的 `rumble.active`）。

还没接：网页嵌入页、rankedSchedule 的历史轮换表（见文档的路线图）。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from email.utils import parsedate_to_datetime

API_URL = "https://tools.skycraft.cn/apex/api/maprotation"
ASSET_BASE = "https://tools.skycraft.cn"        # 图片 asset 的域名
POLL_SECS = 300                                  # 5 分钟拉一次（轮换 90 分钟一轮，足够了）
TIMEOUT_SECS = 8
# 一个请求只用一次、5 分钟一次、不带自动化 UA：相当于一个人开着页面看，不是「抓取」。
# 如果以后要更保守，把 POLL_SECS 调大就行（页面打开时会自己补一次，见 map_rotation_ui）。
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Evo-Tracker/0.4"
MODE_KEYS = ("battle_royale", "ranked", "event", "mixtape")
PAGE_URL = "https://tools.skycraft.cn/apex/zh-CN/maprotation"

_lock = threading.Lock()
_snapshot: dict | None = None    # 最近一次解析成功的快照（给页面读）
_last_error = ""                 # 最近一次失败的原因（给页面的「小字」用）
_refreshed_at = 0.0              # 最近一次成功的时间（本机 monotonic）
_skew = 0.0                      # 服务器时间 - 本机时间（秒）
_thread: threading.Thread | None = None


# ---------------------------------------------------------------------------
# 解析（纯函数，方便单测：拿一段样例 JSON 就能验）
# ---------------------------------------------------------------------------

def _int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _skew_from_date(date_header: str) -> float:
    """响应头的 Date（GMT，秒级）减本机时间。取不到就是 0，宁可不校正也别校歪。"""
    try:
        server = parsedate_to_datetime(date_header).timestamp()
    except (TypeError, ValueError):
        return 0.0
    return server - time.time()


def _entry(raw: dict, names: dict) -> dict:
    """一条轮换：地图名、代码、开始/结束、时长。缺字段就留空，不猜。

    `names` 是响应里的 `mapNames`（英文名 → 中文名），用来把 "World's Edge"
    换成「世界尽头」。上游给的是 `nameZh`（按 code 查好的），两个都有就优先 nameZh。
    """
    code = str(raw.get("code", "") or "")
    name = str(raw.get("map", "") or "")
    zh = str(raw.get("nameZh", "") or "") or names.get(name, "")
    return {
        "map": zh or name,
        "map_en": name,
        "code": code,
        "start": _int(raw.get("start")),
        "end": _int(raw.get("end")),
        "mins": _int(raw.get("durationMins") or raw.get("DurationInMinutes")),
        "asset": str(raw.get("asset", "") or ""),
    }


def parse(payload: dict) -> dict:
    """上游 JSON → 我们页面要用的形状。宽容：缺某一模式就少一块，不缺就报错。"""
    payload = payload if isinstance(payload, dict) else {}
    raw_names = payload.get("mapNames")
    names = {str(k): str(v) for k, v in raw_names.items() if v} if isinstance(raw_names, dict) else {}
    modes: dict[str, dict] = {}
    for key in MODE_KEYS:
        block = payload.get(key)
        if not isinstance(block, dict):
            continue
        current = block.get("current")
        upcoming = block.get("upcoming")
        modes[key] = {
            "current": _entry(current, names) if isinstance(current, dict) else None,
            "upcoming": [_entry(x, names) for x in upcoming if isinstance(x, dict)]
                        if isinstance(upcoming, list) else [],
            "source": str(block.get("source", "") or ""),
            "source_at": str(block.get("sourceUpdatedAt", "") or ""),
        }
    schedule = payload.get("rankedSchedule")
    # 时钟偏差：服务端在响应里给了 nowSecs 就用它（精确到秒、不受中间缓存影响），
    # 没有才退回响应头的 Date（见 fetch_once）。
    now_secs = _int(payload.get("nowSecs"))
    return {"modes": modes,
            "season": _season(schedule),
            "rumble": _rumble(schedule),
            "skew": now_secs - int(time.time()) if now_secs > 0 else 0.0,
            "fetched": int(time.time())}


def _season(schedule) -> dict | None:
    """rankedSchedule 里只摘「赛段」这一小段（画时间轴和头部用）。不摘历史表，原因见文档 §2。"""
    if not isinstance(schedule, dict):
        return None
    season = schedule.get("season")
    if not isinstance(season, dict):
        return None
    split_end = _int(season.get("splitEndTimestamp"))
    target = _int(season.get("targetTimestamp"))
    return {
        "number": _int(season.get("seasonNumber")),
        "name": str(season.get("seasonName", "") or ""),
        "split": _int(season.get("currentSplit")),
        "split_name": str(season.get("splitName", "") or ""),
        "start": _int(season.get("seasonStartTimestamp")),
        "finish": _int(season.get("seasonFinishTimestamp")),
        "split_start": _int(season.get("splitStartTimestamp")),
        "split_end": split_end,
        # 该盯哪个截止时刻由 view() 按「现在」决定（见 _season_view）
        "target": target,
        "target_label": str(season.get("targetLabel", "") or ""),
    }


def _rumble(schedule) -> dict | None:
    """天梯战（Ranked Rumble）：只留「这一轮」，状态一律到 view() 里现算。

    上游 2026-10-26 起只发当前那一轮（`rumble = {round, status, targetTimestamp,
    startTimestamp, finishTimestamp, ...}`），不再发整季的 rounds 表。为了上游还没更新的
    时候也能跑，这里两种形状都认：

      · 新形状（有 startTimestamp 的单轮）→ {"round": {...}}
      · 旧形状（一季的 rounds + 可能过期的 active）→ {"rounds": [...], "fallback": ...}
    """
    if not isinstance(schedule, dict):
        return None
    rumble = schedule.get("rumble")
    if not isinstance(rumble, dict):
        return None

    def one(item: dict) -> dict | None:
        start = _int(item.get("startTimestamp"))
        finish = _int(item.get("finishTimestamp"))
        if start <= 0 or finish <= 0:
            return None
        return {"round": _int(item.get("round")), "split": _int(item.get("split")),
                "start": start, "finish": finish,
                # 上游会顺手抹掉 numMatches / totalRounds（页面上不显示它们），缺了就记 0
                "matches": _int(item.get("numMatches"))}

    rounds = []
    raw_rounds = rumble.get("rounds")
    if isinstance(raw_rounds, list):
        for item in raw_rounds:
            if isinstance(item, dict):
                row = one(item)
                if row:
                    rounds.append(row)
    if rounds:
        active = rumble.get("active")
        fallback = one(active) if isinstance(active, dict) else None
        return {"season": _int(rumble.get("season")), "total": _int(rumble.get("totalRounds")),
                "rounds": rounds, "fallback": fallback}
    # 新形状：上游已经替我们挑好了这一轮
    picked = one(rumble)
    if picked is None:
        return None
    return {"season": _int(rumble.get("season")), "total": 0, "rounds": [], "fallback": picked}


def fetch_once(url: str = API_URL, timeout: float = TIMEOUT_SECS) -> dict:
    """一次同步请求。抛异常给调用方（后台线程）记账，页面不碰这个函数。"""
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        skew = _skew_from_date(response.headers.get("Date", ""))
    data = parse(json.loads(raw.decode("utf-8")))
    if not data["skew"]:      # JSON 里有 nowSecs 就用它，没有才退回 Date 头
        data["skew"] = skew
    return data


# ---------------------------------------------------------------------------
# 后台线程
# ---------------------------------------------------------------------------

def _loop() -> None:
    global _snapshot, _last_error, _refreshed_at, _skew
    while True:
        try:
            data = fetch_once()
            with _lock:
                _snapshot = data
                _refreshed_at = time.monotonic()
                _skew = float(data.get("skew") or 0.0)
                _last_error = ""
        except Exception as exc:  # noqa: BLE001 —— 任何失败只记账，线程继续活着
            with _lock:
                _last_error = f"{type(exc).__name__}: {exc}"[:200]
        time.sleep(POLL_SECS)


def start() -> None:
    """幂等：bootstrap 调两次也只起一个线程。"""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, name="map-rotation", daemon=True)
    _thread.start()


# ---------------------------------------------------------------------------
# 页面读这里（永不抛异常）
# ---------------------------------------------------------------------------

def _now() -> int:
    """服务器时间（校正过的本机时间）。"""
    return int(time.time() + _skew)


def _iso(ts: int) -> str:
    if ts <= 0:
        return ""
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _left_text(secs: int) -> str:
    """把「还剩多少秒」说成人话：还有 9 天 / 还有 3 小时 / 还有 42 分钟。"""
    secs = max(0, int(secs))
    if secs >= 86400:
        return f"还有 {secs // 86400} 天"
    if secs >= 3600:
        hours = secs // 3600
        return f"还有 {hours} 小时" + (f" {secs % 3600 // 60} 分" if secs % 3600 >= 60 else "")
    return f"还有 {max(1, (secs + 59) // 60)} 分钟"


def _row(entry: dict | None, now: int) -> dict | None:
    """给页面用的行：把时间戳换成「还剩 42 分钟」「14:20 开始」这种人话。"""
    if not entry or not entry.get("code"):
        return None
    left = max(0, entry["end"] - now) if entry["end"] else 0
    return {
        "map": entry["map"],
        "map_en": entry.get("map_en", ""),
        "code": entry["code"],
        "left_mins": (left + 59) // 60 if entry["end"] else None,
        "progress": (min(100, max(0, round(100 * (now - entry["start"]) /
                                           max(1, entry["end"] - entry["start"]))))
                     if entry["start"] and entry["end"] else 0),
        "start": _iso(entry["start"]),
        "end": _iso(entry["end"]),
        "mins": entry["mins"],
        "image": ASSET_BASE + entry["asset"] if entry["asset"].startswith("/") else "",
        "finished": bool(entry["end"]) and entry["end"] <= now,
    }


MODE_LABELS = {
    "battle_royale": "大逃杀",
    "ranked": "排位",
    "event": "活动",
    "mixtape": "混合模式",
}


def _countdown(ts: int, now: int) -> dict:
    """一个截止时刻 → 页面要的四件套：状态、状态文字、还差多少、那个时刻。"""
    left = ts - now
    if left <= 0:
        return {"finished": True, "status_text": "已结束", "target_label": "已结束",
                "countdown": "0 分钟", "target_text": _iso(ts), "target": ts, "progress": 100}
    return {"finished": False, "status_text": "进行中", "target_label": "距结束",
            "countdown": _left_text(left), "target_text": _iso(ts), "target": ts}


def _season_view(season: dict | None, now: int) -> dict | None:
    """赛段卡：赛段还没结束就盯赛段截止，否则盯整赛季截止（与网页/小程序同一口径）。"""
    if not season:
        return None
    split_end = season.get("split_end") or 0
    use_split = split_end > now
    finish = split_end if use_split else (season.get("target") or season.get("finish") or 0)
    if finish <= 0:
        return None
    title = (season.get("split_name") or f"{season.get('name') or 'S' + str(season.get('number'))} 赛段 "
             f"{season.get('split')}") if use_split else (season.get("name") or f"S{season.get('number')}")
    start = (season.get("split_start") or season.get("start") or 0) if use_split else (season.get("start") or 0)
    return dict(season, title=title, left_days=max(0, (finish - now + 86399) // 86400),
                finish_text=_iso(finish), finish_ts=finish,
                progress=min(100, max(0, round(100 * (now - start) / max(1, finish - start))))
                         if start else None,
                **_countdown(finish, now))


def _rumble_view(rumble: dict | None, now: int) -> dict | None:
    """天梯战卡：从 rounds 现算（进行中 → 否则最早未开始 → 否则兜底那一轮）。

    顺序和 ApexPacks 网页 / 小程序完全一致，保证三处显示的是同一轮。
    """
    if not rumble:
        return None
    rounds = rumble.get("rounds") or []
    live = next((x for x in rounds if x["start"] <= now < x["finish"]), None)
    upcoming = sorted((x for x in rounds if x["start"] > now), key=lambda x: x["start"])
    picked = None
    if rounds:
        live = next((x for x in rounds if x["start"] <= now < x["finish"]), None)
        upcoming = sorted((x for x in rounds if x["start"] > now), key=lambda x: x["start"])
        picked = live or (upcoming[0] if upcoming else None)
    if picked is None:
        # 新形状（上游只发这一轮）走这里；旧形状里 rounds 全过期时也退到 fallback
        picked = rumble.get("fallback") or (rounds[-1] if rounds else None)
    if not picked:
        return None
    started = now >= picked["start"]
    if not started:
        left = picked["start"] - now
        return {"round": picked["round"], "split": picked["split"],
                "title": f"天梯战 · 第 {picked['round']} 轮", "total": rumble.get("total") or 0,
                "finished": False, "status_text": "未开放", "target_label": "距开启",
                "countdown": _left_text(left), "target_text": _iso(picked["start"]),
                "progress": 0, "start_text": _iso(picked["start"]), "end_text": _iso(picked["finish"])}
    info = _countdown(picked["finish"], now)
    info["progress"] = (min(100, max(0, round(100 * (now - picked["start"]) /
                                              max(1, picked["finish"] - picked["start"]))))
                        if not info["finished"] else 100)
    return dict(info, round=picked["round"], split=picked["split"],
                title=f"天梯战 · 第 {picked['round']} 轮", total=rumble.get("total") or 0,
                start_text=_iso(picked["start"]), end_text=_iso(picked["finish"]))


def _modes_view(snapshot: dict, now: int) -> list:
    """四种模式各自的「现在这张 / 接下来 4 张」。缺某个模式就少一块。"""
    modes = []
    for key in MODE_KEYS:
        block = snapshot["modes"].get(key)
        if not block:
            continue
        current = _row(block["current"], now)
        upcoming = [_row(x, now) for x in block["upcoming"]]
        upcoming = [_upcoming_row(x) for x in upcoming if x and not x["finished"]][:4]
        if not current and not upcoming:
            continue
        modes.append({
            "key": key,
            "label": MODE_LABELS.get(key, key),
            "current": current,
            "upcoming": upcoming,
        })
    return modes


def view() -> dict:
    """模板要的全部数据。断网 / 还没拉到 → 返回 available=False 和原因。"""
    with _lock:
        snapshot = _snapshot
        error = _last_error
        refreshed = _refreshed_at
    if snapshot is None:
        reason = "地图轮换暂时取不到" if error else "地图轮换还在加载"
        detail = error
        return {"available": False, "reason": reason, "detail": detail,
                "error_text": f"{reason}。" + (f"（{detail}）" if detail else ""),
                "modes": [], "season": None, "rumble": None, "age_mins": 0,
                "page_url": PAGE_URL}
    now = _now()
    modes = _modes_view(snapshot, now)
    season = _season_view(snapshot.get("season"), now)
    rumble = _rumble_view(snapshot.get("rumble"), now)
    age_mins = max(0, int((time.monotonic() - refreshed) // 60))
    return {
        "available": bool(modes),
        # 页面上的小字：断网但还有上次的表 → 说清楚这是旧的
        "reason": "" if modes else "上游返回的数据里没有可显示的轮换",
        "error_text": (f"这次没取到最新的（{error}），下面是 {age_mins} 分钟前拿到的表。"
                       if error else ""),
        "detail": error,
        "modes": modes,
        "season": season,
        "rumble": rumble,
        "age_mins": age_mins,
        "fetched_text": time.strftime("%H:%M", time.localtime(snapshot.get("fetched") or 0))
                        if snapshot.get("fetched") else "",
        "updated_at": snapshot.get("fetched") or 0,
        "page_url": PAGE_URL,
    }


def _upcoming_row(row: dict) -> dict:
    """接下来那几项只要「什么时候、哪张图」。"""
    return {"map": row["map"], "code": row["code"], "start": row["start"]}
