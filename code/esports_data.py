"""赛事数据（赛事页、看直播页用）：下载 esports.json 这个公开的小文件，里面是开发者手动维护的赛程、队伍和每位选手的直播间。

只在打开赛事页时下载（get()，不在后台定时下载），60 秒内再打开不重下；请求里只带"Evo/<版本号>"，不发送任何战绩、
截图或电脑信息。文件放在 R2 上（https://dl.evotracker.cn/esports.json ，和 notice.json 一起），不经过统计服务器
（免费版每天有请求上限，原因同公告）。连不上、格式不对都当作"这次没读到"，用上次存下的（数据库旁边的 esports.json）。

esports.json 由 tools/esports.py 从两张 CSV 表生成、上传：
    {"version": 1, "updated": "2026-11-17T20:00:00+08:00",
     "platforms": {"douyu": true, "huya": true, "bilibili": true, "douyin": true},
     "players": [{"id": "douyu-520013", "name": "Kite", "team": "星火电竞", "platform": "douyu", "room": "520013",
                  "liquipedia": ""}],
     "events": [{"id": "20261117-1900-league", "name": "亚太北区职业联赛 第 3 周", "type": "联赛", "stage": "B 组对 C 组",
                 "start": "2026-11-17T19:00:00+08:00", "end": "2026-11-17T23:00:00+08:00", "teams": ["星火电竞", …]}],
     "teams": {"星火电竞": {"logo": "3fa9c1d2e4b5a6f7.webp"}}}
- platforms：远程开关。某个平台是 false，Evo 就不在窗口里播它，改成"去 X 看"。没写的平台当作 true
- players：id 只有字母、数字、- 和 _；platform 是 config/watch.json 里的四个之一；room 只能是数字，
  抖音的还可以是主播的抖音号（字母、数字、下划线；live_status.room_ok()）
- events：type 是 联赛 / 训练赛 / 杯赛；start、end 带时区，end 晚于 start；teams 是队名，对应 players 里的 team
- teams（可以没有）：队徽。键是队名，logo 是公开存储桶 esports/logos/ 下的文件名（小写字母和数字 + .png / .webp，
  协作网页上传时起的）。**只写文件名、不写网址**：Evo 自己拼成 config/watch.json 里 logo_url 那个地址（logo_url()）。
  没有队徽的队不写；名单里没有的队、文件名写得不对的那一条去掉
- "进行中 / 即将开始 / 已结束"按 start、end 和电脑上的时间算（status()），不用手动改
格式不对的那一条去掉，不影响别的；整个文件不对（不是 JSON、version 不认识）当作没读到。
存储桶的密钥就算泄露，别人也只能改赛程上的字和房间号（只会去问这四个平台的直播间），不能让 Evo 打开别的网站。

测试时用环境变量 EVO_ESPORTS_URL 换地址：可以是测试文件 https://dl.evotracker.cn/esports_test.json ，
也可以是本机的一个文件（如 E:\\Evo\\dist\\esports.json，tools/esports.py --out 生成的）。
用法：python code/esports_data.py   → 下载一次并打印
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import live_status
from paths import DB_PATH
from storage import APP_VERSION

VERSION = 1
URL = os.environ.get("EVO_ESPORTS_URL", "https://dl.evotracker.cn/esports.json")
CACHE_PATH = DB_PATH.parent / "esports.json"  # {"fetched_at": 下载的时间, "data": 清理过的内容}
TIMEOUT = 10
MAX_BYTES = 1_000_000
FRESH_SECONDS = 60     # 这么久以内下载过就不重下
RECENT_DAYS = 7        # "最近结束"只留这么多天
TYPES = ("联赛", "训练赛", "杯赛")
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
LOGO_RE = re.compile(r"[a-z0-9]{8,40}\.(png|webp)")  # 队徽的文件名：拼不出别的文件夹、别的网站
LIVE, UPCOMING, ENDED = "live", "upcoming", "ended"


class EsportsError(Exception):
    pass


def parse_time(text):
    """"2026-11-17T19:00:00+08:00" → 带时区的时间；写错了、没带时区返回 None。"""
    try:
        moment = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _text(value, limit, required=True):
    """一段文字：去掉两头空格、换行，超过 limit 个字、（required 时）是空的都算不对（返回 None）。"""
    if not isinstance(value, str):
        return None if required else ""
    value = " ".join(value.split())
    if len(value) > limit or (required and not value):
        return None
    return value


def clean_player(raw):
    if not isinstance(raw, dict):
        return None
    pid, name, team = raw.get("id"), _text(raw.get("name"), 40), _text(raw.get("team"), 40)
    platform, room = raw.get("platform"), raw.get("room")
    if not isinstance(pid, str) or not ID_RE.fullmatch(pid) or name is None or team is None:
        return None
    if not isinstance(platform, str) or not live_status.room_ok(platform, room):
        return None
    return {"id": pid, "name": name, "team": team, "platform": platform, "room": room,
            "liquipedia": _text(raw.get("liquipedia"), 100, required=False) or ""}


def clean_event(raw):
    if not isinstance(raw, dict):
        return None
    eid, name, stage = raw.get("id"), _text(raw.get("name"), 80), _text(raw.get("stage"), 80, required=False)
    start, end, teams = parse_time(raw.get("start")), parse_time(raw.get("end")), raw.get("teams")
    if not isinstance(eid, str) or not ID_RE.fullmatch(eid) or name is None or stage is None:
        return None
    if raw.get("type") not in TYPES or start is None or end is None or end <= start or not isinstance(teams, list):
        return None
    names = []
    for team in teams:
        team = _text(team, 40)
        if team is not None and team not in names:
            names.append(team)
    return {"id": eid, "name": name, "type": raw["type"], "stage": stage, "start": start.isoformat(),
            "end": end.isoformat(), "teams": names[:60]}


def clean_teams(raw, known):
    """esports.json 的 teams → {队名: {"logo": 文件名}}。known：选手、赛事里出现过的队名；别的队、写得不对的去掉。"""
    out = {}
    for name, item in (raw.items() if isinstance(raw, dict) else ()):
        name = _text(name, 40)
        logo = item.get("logo") if isinstance(item, dict) else None
        if name in known and isinstance(logo, str) and LOGO_RE.fullmatch(logo):
            out[name] = {"logo": logo}
    return out


def logo_url(data, team):
    """这支队的队徽图片地址；没有队徽返回 None。"""
    logo = ((data.get("teams") or {}).get(team) or {}).get("logo") if data else None
    return live_status.CONFIG["logo_url"].format(file=logo) if logo and LOGO_RE.fullmatch(logo) else None


def _unique(items, check):
    out, seen = [], set()
    for raw in items:
        item = check(raw)
        if item is not None and item["id"] not in seen:
            out.append(item)
            seen.add(item["id"])
    return out


def clean(data):
    """检查整个文件，返回清理过的内容；整个不对抛 EsportsError。"""
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise EsportsError("赛事文件的格式不对")
    if not isinstance(data.get("players"), list) or not isinstance(data.get("events"), list):
        raise EsportsError("赛事文件的格式不对")
    switches = data.get("platforms") if isinstance(data.get("platforms"), dict) else {}
    players, events = _unique(data["players"][:2000], clean_player), _unique(data["events"][:200], clean_event)
    known = {player["team"] for player in players} | {team for event in events for team in event["teams"]}
    return {"version": VERSION, "updated": data["updated"] if parse_time(data.get("updated")) else None,
            "platforms": {key: switches.get(key) is not False for key in live_status.PLATFORMS},
            "players": players, "events": events, "teams": clean_teams(data.get("teams"), known)}


def _address(url):
    """EVO_ESPORTS_URL 写的是本机的一个文件时，换成 file:// 地址。"""
    return url if "://" in url else Path(url).resolve().as_uri()


def fetch(url=None, timeout=TIMEOUT):
    """下载并检查 esports.json，返回清理过的内容；连不上、格式不对抛 EsportsError。"""
    request = urllib.request.Request(_address(url or URL),
                                     headers={"User-Agent": f"Evo/{APP_VERSION}", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_BYTES)
    except (OSError, ValueError) as e:
        raise EsportsError(f"连不上（{e}）") from e
    try:
        data = json.loads(body.decode("utf-8-sig"))
    except ValueError:
        raise EsportsError("赛事文件的格式不对") from None
    return clean(data)


def cached():
    """上次存下的：{"fetched_at", "data"}；没有、坏了返回 None。"""
    try:
        saved = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return {"fetched_at": float(saved["fetched_at"]), "data": clean(saved["data"])}
    except (OSError, ValueError, KeyError, TypeError, EsportsError):
        return None


def get(force=False, fresh=FRESH_SECONDS):
    """打开赛事页、看直播页时调用（fresh 秒以内下载过就不重下）。返回 {"data": 赛事数据（一次都没读到过是 None）, "ok": 是不是最新的,
    "error": 没读到的原因, "fetched_at": data 是什么时候下载的}。"""
    saved = cached()
    if saved and not force and 0 <= time.time() - saved["fetched_at"] < fresh:
        return {"data": saved["data"], "ok": True, "error": None, "fetched_at": saved["fetched_at"]}
    try:
        data = fetch()
    except EsportsError as e:
        return {"data": saved["data"] if saved else None, "ok": False, "error": str(e),
                "fetched_at": saved["fetched_at"] if saved else None}
    now = time.time()
    try:  # 先写临时文件再换过去，写到一半断电也不会留下半个文件
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = CACHE_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps({"fetched_at": now, "data": data}, ensure_ascii=False), encoding="utf-8")
        temp.replace(CACHE_PATH)
    except OSError:
        pass
    return {"data": data, "ok": True, "error": None, "fetched_at": now}


def status(event, now=None):
    """这场赛事现在是 "live" 进行中 / "upcoming" 即将开始 / "ended" 已结束。"""
    now = now or datetime.now().astimezone()
    if now < parse_time(event["start"]):
        return UPCOMING
    return LIVE if now < parse_time(event["end"]) else ENDED


def groups(data, now=None):
    """赛事页的三块：{"live": 正在进行, "upcoming": 即将开始（先开始的在前）, "recent": 最近 7 天结束的（刚结束的在前）}。"""
    now = now or datetime.now().astimezone()
    out = {LIVE: [], UPCOMING: [], "recent": []}
    for event in sorted(data["events"], key=lambda e: parse_time(e["start"])):
        state = status(event, now)
        if state != ENDED:
            out[state].append(event)
        elif now - parse_time(event["end"]) <= timedelta(days=RECENT_DAYS):
            out["recent"].append(event)
    out["recent"].sort(key=lambda e: parse_time(e["end"]), reverse=True)
    return out


def find_event(data, event_id):
    return next((event for event in data["events"] if event["id"] == event_id), None)


def event_teams(data, event):
    """这场赛事的队伍和选手：[(队名, [选手, ...]), ...]，按赛程里写的顺序。"""
    by_team = {}
    for player in data["players"]:
        by_team.setdefault(player["team"], []).append(player)
    return [(team, by_team.get(team, [])) for team in event["teams"]]


def event_players(data, event):
    return [player for _, players in event_teams(data, event) for player in players]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"赛事文件的地址：{URL}")
    try:
        found = fetch()
    except EsportsError as error:
        raise SystemExit(f"没读到：{error}") from None
    off = [live_status.NAMES[key] for key, on in found["platforms"].items() if not on]
    print(f"{len(found['events'])} 场赛事，{len(found['players'])} 位选手，更新于 {found['updated']}"
          + (f"，关掉的平台：{'、'.join(off)}" if off else ""))
    words = {LIVE: "进行中", UPCOMING: "即将开始", ENDED: "已结束"}
    for item in found["events"]:
        print(f"  · [{words[status(item)]}] {item['start'][:16].replace('T', ' ')} {item['type']} {item['name']}"
              f"（{item['stage']}）{len(item['teams'])} 支队、{len(event_players(found, item))} 位选手")
