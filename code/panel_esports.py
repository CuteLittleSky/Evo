"""Evo 窗口的"赛事"页面（2026-11-17 用户定的：导航在"赛季"和"设置"之间）：赛事列表，从这里进看直播页。

- 打开这一页时下载一次 esports.json（esports_data.get，60 秒内不重下）；没读到时用上次存下的，并在顶部说明
- 三块：正在进行（每场一张卡片：进行中、类型、开始时间、名称、阶段和队伍数、几位选手在播、"看直播"按钮）、
  即将开始（一行一场，右边"开始后可以看直播"）、最近结束（最近 7 天，灰色，右边"已结束"）。
  进行中 / 即将开始 / 已结束按电脑上的时间算，日期和钟点也按电脑的时区显示
- 几位选手在播：页面先画出来，再问 GET /esports/live（查这几场赛事名单里的直播间，live_status.query），
  60 秒内再问不重查。只在这一页开着时查，不在后台查
- 看直播页（/esports/<赛事>/watch）下一步做，现在只有一个占位的页面
这个文件不加载识别用的库。模板 panel/templates/esports.html，规则见 docs/面板.md"赛事页"。
"""

import threading
import time
from datetime import datetime

from flask import Blueprint, flash, jsonify, redirect, render_template, url_for

import esports_data
import live_status

bp = Blueprint("esports", __name__)
WEEKDAYS = "一二三四五六日"
STATUS_SECONDS = 60  # 这么久以内查过开播状态就不重查
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
    return {"id": event["id"], "name": event["name"], "type": event["type"], "stage": event["stage"],
            "teams": teams, "sub": f"{event['stage']}，{teams} 支队伍" if event["stage"] else f"{teams} 支队伍",
            "day": day_text(start, now), "weekday": f"周{WEEKDAYS[start.weekday()]}", "time": f"{start:%H:%M}",
            "start": event["start"]}


def players_status(players):
    """这些选手现在在不在播：{选手 id: 结果}。60 秒内查过、而且这些人都在里面，就用上次的。"""
    with _status_lock:
        fresh = 0 <= time.time() - _status["at"] < STATUS_SECONDS
        if not fresh or any(p["id"] not in _status["found"] for p in players):
            found = live_status.query(players)
            _status["found"] = {**(_status["found"] if fresh else {}), **found}
            _status["at"] = time.time()
        return {p["id"]: _status["found"][p["id"]] for p in players}


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
    data = esports_data.cached()
    event = esports_data.find_event(data["data"], event_id) if data else None
    if event is None:
        flash("没有找到这场赛事，可能已经从赛程里拿掉了。", "error")
        return redirect(url_for("esports.page"))
    return render_template("esports_watch.html", event=view(event, datetime.now().astimezone()))
