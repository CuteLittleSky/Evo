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
- 左边：返回、赛事名、播放器、选手和三个去平台的按钮、同队视角；右边：队伍和选手，在播的可以点。
  赛程里这场赛事写了主舞台（播导播视角的直播间）时，右边最上面多一栏"主舞台"，和队伍并列（2026-11-18 用户要的）
- 页面上先把名单画好，谁在播由脚本问 GET /esports/<赛事>/status：页面开着、看得见时每 60 秒问一次
  （这里 50 秒内不重查，开播、下播后 2 分钟内页面跟着变）。顺便带上平台开关（esports.json 5 分钟内下载过就不重下）
- 能在窗口里播的平台：配置里有 embed_url 的用平台官方的嵌入播放器（现在是 B站）；配置里 player 是 flv（斗鱼，经过
  本机中转）、direct（抖音、登录了的 B站，页面直接连平台的视频服务器）的用 Evo 自己的播放器（mpegts.js）。
  别的平台（虎牙：用户 2026-11-18 定了不做）、平台开关关了的显示退路"去 X 看"
- 抖音：不登录，取地址在 watch_douyin.py（只要 H.264 的档；没选过清晰度时要码率不超过配置里那个数的最好一档）
- B站：没登录的用官方嵌入播放器（最高 720p）；玩家扫码登录了 B站（bili_login.py，登录信息加密存在本机）以后改用
  Evo 自己的播放器播原画，页面直接连 B站的视频服务器，不经过本机中转（watch_bilibili.py）。
  登录：POST /esports/login/bilibili/start 要二维码（回画好的 SVG）、…/poll 问扫了没有、…/logout 退出；都要带面板的口令
- Evo 自己的播放器：页面先问 GET /esports/play/<选手>?q=清晰度（有哪些清晰度、实际给的是哪一档、播放地址），
  再让播放器连 GET /esports/stream/<选手>（本机中转，flv_relay.py：只听 127.0.0.1、要带面板的口令、一次只播一路，
  开新的一路就停掉上一路；播放器断开就停，不在后台留着）。取地址、连视频服务器都在玩家自己的电脑上
- Apex 在运行：GET /esports/game（game_detect，只看进程名），页面每 10 秒问一次，正在播时显示黄色提示条
这个文件不加载识别用的库。规则见 docs/面板.md"赛事页""看直播页"。
"""

import logging
import re
import threading
import time
from datetime import datetime

from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for

import bili_login
import esports_data
import flv_relay
import game_detect
import live_status
import qr
import watch_bilibili
import watch_douyin
import watch_douyu

bp = Blueprint("esports", __name__)
WEEKDAYS = "一二三四五六日"
STATUS_SECONDS = 60        # 赛事页：这么久以内查过开播状态就不重查
WATCH_STATUS_SECONDS = 50  # 看直播页：页面每 60 秒问一次，每次都查新的（留 10 秒给同时开着的另一页）
WATCH_DATA_SECONDS = 300   # 看直播页开着时，这么久重新下载一次 esports.json（赛程改了、平台开关关了以后最慢这么久生效；
                           # 原来是 10 分钟，2026-11-18 用户要改成 5 分钟）
EMBED = {key: platform["embed_url"] for key, platform in live_status.PLATFORMS.items() if platform.get("embed_url")}
# 用 Evo 自己的播放器播的平台 → 取地址的模块。模块里 DIRECT 是 True 的（B站、抖音）：页面直接连平台的视频服务器；
# 别的（斗鱼）：经过本机中转（/esports/stream）
PLAYERS = {"douyu": watch_douyu, "bilibili": watch_bilibili, "douyin": watch_douyin}
PLAYERS = {key: module for key, module in PLAYERS.items() if live_status.PLATFORMS[key].get("player") in ("flv", "direct")}
# 要玩家登录了才用 Evo 自己的播放器的平台 → 管登录的模块（没登录的用平台官方的嵌入播放器）
LOGINS = {"bilibili": bili_login}
RATE_RE = re.compile(r"-?\d{1,5}|[a-z]{1,12}")  # 清晰度：斗鱼、B站是编号，抖音是档名（origin、uhd、hd……）
_playing = {"relay": None}  # 正在中转的那一路（一次只播一路）
_playing_lock = threading.Lock()
log = logging.getLogger("evo")
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
    """看直播页要的名单：[{"name": 队名, "initial": 第一个字, "logo": 队徽地址或 None, "main": 是不是主舞台,
    "players": [选手, ...]}, ...]。这场赛事有主舞台（播导播视角的直播间）时它排在最前面，"选手"是那几路直播间。
    选手：id、名字、队名、平台、房间号、直播间地址、main、off（不开直播的选手）；embed 是平台官方嵌入播放器的地址，play 是用 Evo 自己的播放器时
    问清晰度和播放地址的接口（都没有就是不能在窗口里播）。"""
    def member(player, team, main=False):
        embed = EMBED.get(player["platform"])
        off = player["platform"] == esports_data.NO_STREAM  # 不开直播的选手：照样列在队里，标"不直播"，点不开
        return {"id": player["id"], "name": player["name"], "initial": player["name"][:1].upper(),
                "team": team, "main": main, "off": off, "platform": player["platform"],
                "platform_name": esports_data.NO_STREAM_NAME if off else live_status.NAMES[player["platform"]],
                "room": player["room"],
                "url": live_status.room_url(player["platform"], player["room"]),
                "embed": embed.format(room=player["room"]) if embed else None,
                "play": url_for("esports.play", player_id=player["id"]) if player["platform"] in PLAYERS else None}

    teams = []
    main = esports_data.event_main(data, event)
    if main:
        teams.append({"key": "main", "name": esports_data.MAIN_TEAM, "initial": esports_data.MAIN_TEAM[:1], "logo": None,
                      "main": True, "players": [member(stream, esports_data.MAIN_TEAM, True) for stream in main]})
    for index, (team, players) in enumerate(esports_data.event_teams(data, event)):
        teams.append({"key": f"t{index}", "name": team, "initial": team[:1].upper(), "main": False,
                      "players": [member(player, team) for player in players],
                      "logo": esports_data.logo_url(data, team)})  # 队徽的地址，没有是 None（页面用队名第一个字）
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
    rosters = {event["id"]: esports_data.streaming(esports_data.event_players(data, event)) for event in events}  # 不开直播的不数
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
    ids = {player["id"] for team in teams for player in team["players"] if not player["off"]}  # 能选来看的人
    wanted = request.args.get("p")
    return render_template(
        "esports_watch.html", event=view(event, datetime.now().astimezone()), teams=teams,
        team_count=sum(not team["main"] for team in teams),  # "全部 N 支"只数队伍，不数主舞台
        total=len(ids), selected=wanted if wanted in ids else None,
        page_data={"players": [player for team in teams for player in team["players"]],
                   "platforms": data["platforms"], "selected": wanted if wanted in ids else None,
                   "status_url": url_for("esports.status", event_id=event["id"]), "game_url": url_for("esports.game"),
                   "mpegts_url": url_for("static", filename="vendor/mpegts.js"), "token": current_app.config["TOKEN"],
                   # 要登录才能看高画质的平台：现在登录着没有（打开页面时隔 6 小时向平台核对一次）、登录用的三个接口
                   "accounts": {key: module.account(check=True) for key, module in LOGINS.items()},
                   "login": {key: {step: url_for(f"esports.login_{step}", platform=key) for step in ("start", "poll", "logout")}
                             for key in LOGINS}})


@bp.get("/esports/<event_id>/status")
def status(event_id):
    """这场赛事名单里的选手现在在不在播：{"players": {选手 id: {"state", "loop", "title", "avatar", "since"}},
    "platforms": 平台开关, "checked": 查的时间}。没有这场赛事回 404。"""
    data = esports_data.get(fresh=WATCH_DATA_SECONDS)["data"]
    event = esports_data.find_event(data, event_id) if data else None
    if event is None:
        return jsonify(error="gone"), 404
    # 主舞台的直播间也查；不开直播的选手不查
    players = esports_data.event_main(data, event) + esports_data.streaming(esports_data.event_players(data, event))
    found = players_status(players, WATCH_STATUS_SECONDS) if players else {}
    return jsonify(checked=time.time(), platforms=data["platforms"], players={
        pid: {"state": got["state"], "loop": got["loop"], "title": got["title"], "since": got["since"],
              "avatar": safe_image(got["avatar"])} for pid, got in found.items()})


@bp.get("/esports/game")
def game():
    """Apex 有没有在运行（只看进程名，game_detect.py）：看直播页据此显示"可能让游戏掉帧"的提示条。"""
    return jsonify(running=game_detect.game_running())


def _player(player_id):
    """名单里的这位选手和现在的赛事数据；不能用 Evo 自己的播放器播（不在名单里、平台不支持、平台开关关了）返回 None。"""
    saved = esports_data.cached()
    data = saved["data"] if saved else None
    player = esports_data.find_stream(data, player_id) if data else None  # 名单里的选手，或者主舞台的一路
    if player is None or player["platform"] not in PLAYERS or data["platforms"].get(player["platform"]) is False:
        return None
    login = LOGINS.get(player["platform"])
    if login is not None and not login.account()["logged_in"]:
        return None  # 没登录：Evo 不碰这个平台的视频地址，页面用官方嵌入播放器
    return player


def _rate():
    """网址里的 q（清晰度的编号或档名）；没写、写得不对是 None（没选过：由取地址的模块定要哪一档）。"""
    wanted = request.args.get("q", "")
    return wanted if RATE_RE.fullmatch(wanted) else None


@bp.get("/esports/play/<player_id>")
def play(player_id):
    """看直播页开播前问的：{"ok": True, "qualities": [{"id", "name"}], "applied": 实际给的那一档, "stream": 播放地址}；
    取不到是 {"ok": False, "reason": "upstream", "detail": 原因}。不能在窗口里播的选手回 404。"""
    player = _player(player_id)
    if player is None:
        return jsonify(ok=False, reason="unsupported"), 404
    module = PLAYERS[player["platform"]]
    try:
        found = module.resolve(player["room"], _rate())
    except Exception as e:  # noqa: BLE001  平台那边的各种错：页面显示退路
        log.info("看直播：%s 房间 %s 没取到地址：%s", player["platform"], player["room"], e)
        return jsonify(ok=False, reason="upstream", detail=str(e))
    if getattr(module, "DIRECT", False):  # 页面直接连平台的视频服务器
        return jsonify(ok=True, qualities=found["qualities"], applied=found["rate"], stream=found["url"])
    module.remember(player["room"], found["rate"], found)  # 紧接着播放器来连时直接用，不再问一遍
    return jsonify(ok=True, qualities=found["qualities"], applied=found["rate"],
                   stream=url_for("esports.stream", player_id=player_id, q=found["rate"], t=current_app.config["TOKEN"]))


@bp.get("/esports/stream/<player_id>")
def stream(player_id):
    """本机中转：把平台的 FLV 转给页面里的播放器（flv_relay.py）。要带面板的口令（别的网页不能让 Evo 去连平台）。
    一次只播一路：开新的一路就停掉上一路。连不上、不是 H.264 回 502 和原因，页面显示退路。"""
    if request.args.get("t") != current_app.config["TOKEN"]:
        abort(403)
    player = _player(player_id)
    if player is None or getattr(PLAYERS[player["platform"]], "DIRECT", False):
        abort(404)
    module, rate, first = PLAYERS[player["platform"]], _rate(), [True]

    def open_segment():
        body, found = module.open_segment(player["room"], rate, reuse=first[0])
        first[0] = False
        return body, found["ttl"]

    relay = flv_relay.Relay(open_segment, lead=live_status.PLATFORMS[player["platform"]].get("renew_lead", 5),
                            on_event=lambda text: log.info("看直播：%s 房间 %s %s", player["platform"], player["room"], text))
    with _playing_lock:
        old, _playing["relay"] = _playing["relay"], relay
    if old is not None:
        old.close()
    try:
        relay.start()
    except flv_relay.RelayError as e:
        log.info("看直播：%s 房间 %s 播不了：%s", player["platform"], player["room"], e)
        return jsonify(ok=False, reason=e.code, detail=str(e)), 502
    log.info("看直播：开始中转 %s 房间 %s（清晰度 %s）", player["platform"], player["room"], rate)
    return Response(relay.stream(), mimetype="video/x-flv", headers={"Cache-Control": "no-store"})


@bp.post("/esports/login/<platform>/start")
def login_start(platform):
    """要一个登录二维码：{"ok": True, "key": 问状态用的, "svg": 画好的二维码}；没要到是 {"ok": False, "detail": 原因}。"""
    module = LOGINS.get(platform)
    if module is None:
        abort(404)
    try:
        found = module.start()
        return jsonify(ok=True, key=found["key"], svg=qr.svg(found["url"]))
    except Exception as e:  # noqa: BLE001  平台那边的各种错、二维码的内容太长画不下：页面说没要到
        log.info("看直播：%s 没要到登录二维码：%s", platform, e)
        return jsonify(ok=False, detail=str(e))


@bp.post("/esports/login/<platform>/poll")
def login_poll(platform):
    """扫了没有：{"state": "waiting" / "scanned" / "expired" / "ok" / "error", "name": 登录好了时的昵称}。
    登录好了由管登录的模块存下来（加密）；登录信息不回给页面，也不写进日志。"""
    module = LOGINS.get(platform)
    if module is None:
        abort(404)
    try:
        found = module.poll(request.form.get("key", ""))
    except Exception as e:  # noqa: BLE001
        return jsonify(state="error", name="", detail=str(e))
    if found["state"] == "ok":
        log.info("看直播：%s 登录好了", platform)
    return jsonify(state=found["state"], name=found["name"])


@bp.post("/esports/login/<platform>/logout")
def login_logout(platform):
    """退出登录：告诉平台让这次登录失效，删掉本机存的。"""
    module = LOGINS.get(platform)
    if module is None:
        abort(404)
    told = module.logout()
    log.info("看直播：%s 退出登录（平台那边%s）", platform, "也退出了" if told else "没告诉成，本机的已经删掉")
    return jsonify(ok=True)

