"""Evo 窗口的"赛事"页面和看直播页（2026-11-17 用户定的：导航在"赛季"和"设置"之间）：赛事列表，点进去看选手的第一视角直播。

赛事页 /esports（esports.html）
- 打开这一页时下载一次 esports.json（esports_data.get，60 秒内不重下）；没读到时用上次存下的，并在顶部说明
- 三块：正在进行（每场一张卡片：进行中、类型、开始时间、名称、阶段和队伍数、几位选手在播、"看直播"按钮）、
  即将开始（一行一场，右边"看名单和直播"按钮：2026-11-17 用户要求没开始也能点进去，选手赛前练习、打排位也值得看）、
  最近结束（最近 7 天，灰色，右边"已结束"，不能点）。
  进行中 / 即将开始 / 已结束按电脑上的时间算，日期和钟点也按电脑的时区显示
- 几位选手在播：页面先画出来，再问 GET /esports/live（查这几场赛事名单里的直播间，live_status.query），
  60 秒内再问不重查。只在这一页开着时查，不在后台查

看直播页 /esports/<赛事>/watch（esports_watch.html，占满整个窗口，没有顶部导航）
- 左边：返回、赛事名、播放器、选手和三个去平台的按钮、同队视角；右边：队伍和选手，在播的可以点
- 页面上先把名单画好，谁在播由脚本问 GET /esports/<赛事>/status：页面开着、看得见时每 60 秒问一次
  （这里 50 秒内不重查，开播、下播后 2 分钟内页面跟着变）。顺便带上平台开关（esports.json 10 分钟内下载过就不重下）
- 能在窗口里播的平台：配置里有 embed_url 的（现在只有 B站的官方嵌入播放器）；别的平台、平台开关关了的显示退路
  "去 X 看"。斗鱼、抖音、虎牙的播放后面几步做
- Apex 在运行：GET /esports/game（game_detect，只看进程名），页面每 10 秒问一次，正在播时显示黄色提示条
这个文件不加载识别用的库。规则见 docs/面板.md"赛事页""看直播页"。
"""

import threading
import time
from datetime import datetime

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

import esports_data
import game_detect
import live_status

bp = Blueprint("esports", __name__)
WEEKDAYS = "一二三四五六日"
STATUS_SECONDS = 60        # 赛事页：这么久以内查过开播状态就不重查
WATCH_STATUS_SECONDS = 50  # 看直播页：页面每 60 秒问一次，每次都查新的（留 10 秒给同时开着的另一页）
WATCH_DATA_SECONDS = 600   # 看直播页开着时，这么久重新下载一次 esports.json（平台开关关了以后最慢这么久生效）
EMBED = {key: platform["embed_url"] for key, platform in live_status.PLATFORMS.items() if platform.get("embed_url")}
_status = {"at": 0.0, "found": {}}  # 上次查到的：{选手 id: live_status 的结果}
_status_lock = threading.Lock()  # 同时打开两次页面时只查一次


def day_text(moment, now):
    """"今天" / "明天" / "昨天"，别的写 "11-19"（按电脑的时区）。"""
    days = (moment.date() - now.date()).days
    return {0: "今天", 1: "明天", -1: "昨天"}.get(days, f"{moment:%m-%d}")


def view(event, now):
    """一场赛事在页面上要显示的。"""
    start = esports_data.parse_time(event["start"]).astimezone()
    teams = len(event["teams"])
    state = esports_data.status(event, now)
    return {"id": event["id"], "name": event["name"], "type": event["type"], "stage": event["stage"],
            "teams": teams, "sub": f"{event['stage']}，{teams} 支队伍" if event["stage"] else f"{teams} 支队伍",
            "day": day_text(start, now), "weekday": f"周{WEEKDAYS[start.weekday()]}", "time": f"{start:%H:%M}",
            "start": event["start"], "state": state,
            # 看直播页右上角的标记：没开始的写什么时候开始，不写"进行中"
            "mark": {esports_data.LIVE: "进行中", esports_data.ENDED: "已结束"}.get(
                state, f"{day_text(start, now)} {start:%H:%M} 开始")}


def players_status(players, max_age=STATUS_SECONDS):
    """这些选手现在在不在播：{选手 id: 结果}。max_age 秒内查过、而且这些人都在里面，就用上次的。"""
    with _status_lock:
        fresh = 0 <= time.time() - _status["at"] < max_age
        if not fresh or any(p["id"] not in _status["found"] for p in players):
            found = live_status.query(players)
            _status["found"] = {**(_status["found"] if fresh else {}), **found}
            _status["at"] = time.time()
        return {p["id"]: _status["found"][p["id"]] for p in players}


def safe_image(url):
    """平台给的头像地址：只要 https 的（http 的换成 https），别的不要。"""
    url = str(url or "")
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url if url.startswith("https://") and not any(c in url for c in '"<> \\') else ""


def roster(data, event):
    """看直播页要的名单：[{"name": 队名, "initial": 第一个字, "players": [选手, ...]}, ...]。
    选手：id、名字、队名、平台、房间号、直播间地址、能在窗口里播时的播放器地址。"""
    teams = []
    for index, (team, players) in enumerate(esports_data.event_teams(data, event)):
        members = []
        for player in players:
            embed = EMBED.get(player["platform"])
            members.append({"id": player["id"], "name": player["name"], "initial": player["name"][:1].upper(),
                            "team": team, "platform": player["platform"],
                            "platform_name": live_status.NAMES[player["platform"]], "room": player["room"],
                            "url": live_status.room_url(player["platform"], player["room"]),
                            "embed": embed.format(room=player["room"]) if embed else None})
        teams.append({"key": f"t{index}", "name": team, "initial": team[:1].upper(), "players": members})
    return teams


@bp.route("/esports")
def page():
    got = esports_data.get()
    now = datetime.now().astimezone()
    data = got["data"]
    found = esports_data.groups(data, now) if data else {"live": [], "upcoming": [], "recent": []}
    read_at = datetime.fromtimestamp(got["fetched_at"]).astimezone() if got["fetched_at"] else None
    return render_template(
        "esports.html", loaded=data is not None, stale=data is not None and not got["ok"],
        read_at=f"{day_text(read_at, now)} {read_at:%H:%M}" if read_at else None,
        live=[view(e, now) for e in found["live"]], upcoming=[view(e, now) for e in found["upcoming"]],
        recent=[view(e, now) for e in found["recent"]])


@bp.get("/esports/live")
def live():
    """正在进行的每场赛事有几位选手在播：{"events": {赛事 id: {"live": 在播, "total": 名单里几位, "unknown": 没查到的}}}。"""
    data = esports_data.cached()
    data = data["data"] if data else None
    events = esports_data.groups(data)["live"] if data else []
    rosters = {event["id"]: esports_data.event_players(data, event) for event in events}
    everyone = list({p["id"]: p for players in rosters.values() for p in players}.values())
    found = players_status(everyone) if everyone else {}
    out = {}
    for event_id, players in rosters.items():
        states = [found[p["id"]]["state"] for p in players]
        out[event_id] = {"live": states.count(live_status.LIVE), "total": len(players),
                         "unknown": states.count(live_status.ERROR)}
    return jsonify(events=out)


@bp.route("/esports/<event_id>/watch")
def watch(event_id):
    data = esports_data.get()["data"]
    event = esports_data.find_event(data, event_id) if data else None
    if event is None:
        flash("没有找到这场赛事，可能已经从赛程里拿掉了。", "error")
        return redirect(url_for("esports.page"))
    teams = roster(data, event)
    ids = {player["id"] for team in teams for player in team["players"]}
    wanted = request.args.get("p")
    return render_template(
        "esports_watch.html", event=view(event, datetime.now().astimezone()), teams=teams,
        total=len(ids), selected=wanted if wanted in ids else None,
        page_data={"players": [player for team in teams for player in team["players"]],
                   "platforms": data["platforms"], "selected": wanted if wanted in ids else None,
                   "status_url": url_for("esports.status", event_id=event["id"]), "game_url": url_for("esports.game")})


@bp.get("/esports/<event_id>/status")
def status(event_id):
    """这场赛事名单里的选手现在在不在播：{"players": {选手 id: {"state", "loop", "title", "avatar", "since"}},
    "platforms": 平台开关, "checked": 查的时间}。没有这场赛事回 404。"""
    data = esports_data.get(fresh=WATCH_DATA_SECONDS)["data"]
    event = esports_data.find_event(data, event_id) if data else None
    if event is None:
        return jsonify(error="gone"), 404
    players = esports_data.event_players(data, event)
    found = players_status(players, WATCH_STATUS_SECONDS) if players else {}
    return jsonify(checked=time.time(), platforms=data["platforms"], players={
        pid: {"state": got["state"], "loop": got["loop"], "title": got["title"], "since": got["since"],
              "avatar": safe_image(got["avatar"])} for pid, got in found.items()})


@bp.get("/esports/game")
def game():
    """Apex 有没有在运行（只看进程名，game_detect.py）：看直播页据此显示"可能让游戏掉帧"的提示条。"""
    return jsonify(running=game_detect.game_running())
