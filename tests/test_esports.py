"""赛事数据（esports_data.py、live_status.py、abogus.py、tools/esports.py）。不联网：赛事文件由本机的小服务器提供，
平台的回包是照 2026-11-17 实际问到的样子写的（名字换成了示例）。

- 赛事文件：格式不对的那一条去掉、不影响别的；整个不对当作没读到；平台开关没写的当作开；
  进行中 / 即将开始 / 已结束按时间算，"最近结束"只留 7 天
- 下载：只带 User-Agent（Evo/版本号）；60 秒内不重下；连不上、格式不对时用上次存下的
- 查开播状态：四个平台的回包读得对（在播、轮播、没开播、房间不存在）；同时最多 4 个请求；B站一次查多个；
  同一个房间只查一次；一个平台出错不影响别的；房间号不是数字的不发请求
- 抖音签名：SM3 对得上国密标准里的两个样例；固定时间下的签名和 2026-11-17 记下的一样（那天用它问通了抖音的接口）
- tools/esports.py：GBK、带 BOM 的 UTF-8 都读得出；Excel 改过的日期也认；每种写错都指出文件和行号、不生成文件；
  结束超过 7 天的赛事不放进去；选手只放用到的

运行：python tests/test_esports.py
"""

import http.server
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import abogus
import esports_data
import live_status
import storage

os.environ["no_proxy"] = "127.0.0.1,localhost"  # 电脑上设了代理时，本机的测试服务器也直接连
ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "esports.py"
NOW = datetime(2026, 11, 17, 20, 0, tzinfo=live_status.BEIJING)
DOUYIN_UA = live_status.PLATFORMS["douyin"]["user_agent"]


class Handler(http.server.BaseHTTPRequestHandler):
    reply = (404, b"not found")
    requests = []  # 收到的请求头

    def do_GET(self):  # noqa: N802
        self.requests.append(dict(self.headers))
        status, data = self.reply
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


def player(pid="douyu-1", name="示例选手", team="示例战队", platform="douyu", room="1", **more):
    return {"id": pid, "name": name, "team": team, "platform": platform, "room": room, **more}


def event(eid="e1", start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=3), teams=("示例战队",), **more):
    return {"id": eid, "name": "示例联赛", "type": "联赛", "stage": "A 组", "start": start.isoformat(),
            "end": end.isoformat(), "teams": list(teams), **more}


def file(players=None, events=None, **more):
    return {"version": 1, "updated": NOW.isoformat(), "players": players if players is not None else [player()],
            "events": events if events is not None else [event()], **more}


def format_checks(out):
    bad_players = [player(pid="有中文"), player(pid="p2", name=""), player(pid="p3", platform="kuaishou"),
                   player(pid="p4", room="abc"), player(pid="p5", room="12/../x"), player(pid="p6", team="队" * 41),
                   "不是字典", player(pid="ok"), player(pid="ok", name="重复的 id")]
    bad_events = [event(eid="e/1"), event(eid="e2", type="友谊赛"), event(eid="e3", end=NOW - timedelta(hours=2)),
                  {**event(eid="e4"), "start": "2026-11-17 19:00"}, {**event(eid="e5"), "teams": "示例战队"},
                  {**event(eid="e6"), "name": ""}, event(eid="ok")]
    cleaned = esports_data.clean(file(bad_players, bad_events))
    out["选手、赛事写错的那一条去掉（id、平台、房间号、时间、类型……），不影响别的"] = (
        [p["id"] for p in cleaned["players"]] == ["ok"] and cleaned["players"][0]["name"] == "示例选手"
        and [e["id"] for e in cleaned["events"]] == ["ok"])
    rooms = [player(pid="a", platform="douyin", room="xxfps"), player(pid="b", platform="douyin", room="NOVA_Tt1seven"),
             player(pid="c", platform="douyin", room="197427647791"), player(pid="d", platform="douyin", room="a.b"),
             player(pid="e", platform="douyin", room="a/b"), player(pid="f", platform="douyin", room="a?b=1"),
             player(pid="g", platform="douyin", room="x" * 25), player(pid="h", platform="douyin", room=""),
             player(pid="i", platform="douyu", room="xxfps"), player(pid="j", platform="huya", room="lck"),
             player(pid="k", platform="bilibili", room="6a"), player(pid="l", platform="douyin", room=123)]
    out["抖音的房间号还可以是主播的抖音号（字母、数字、下划线）；带点、斜杠、问号、太长的不收；别的平台照旧只能是数字"] = (
        [p["id"] for p in esports_data.clean(file(rooms))["players"]] == ["a", "b", "c"])
    good = "3fa9c1d2e4b5a6f7.webp"
    logos = {"示例战队": {"logo": good}, "只在赛程里的队": {"logo": "0123456789abcdef.png"}, "名单里没有的队": {"logo": good},
             "网址": {"logo": "https://example.invalid/a.png"}, "别的文件夹": {"logo": "../../latest.json"},
             "大写": {"logo": "3FA9C1D2E4B5A6F7.webp"}, "矢量图": {"logo": "3fa9c1d2e4b5a6f7.svg"}, "太短": {"logo": "a.png"},
             "不是字典": good, "没写": {}}
    roster = [player(), *[player(pid=f"t{n}", team=team) for n, team in enumerate(logos) if team not in ("示例战队", "只在赛程里的队", "名单里没有的队")]]
    with_logos = esports_data.clean(file(roster, [event(teams=("示例战队", "只在赛程里的队"))], teams=logos))
    out["队徽：只收名单里的队、写法对的文件名（网址、别的文件夹、大写、SVG、太短的都不要）；没有这一项、写得不对当作都没有队徽"] = (
        with_logos["teams"] == {"示例战队": {"logo": good}, "只在赛程里的队": {"logo": "0123456789abcdef.png"}}
        and esports_data.clean(file())["teams"] == {} and esports_data.clean(file(teams=["示例战队"]))["teams"] == {}
        and len(with_logos["players"]) == len(roster))
    out["队徽的地址由 Evo 自己拼（只会去 dl.evotracker.cn 的 esports/logos/ 下面拿）；没有队徽、存下的旧文件里没有这一项：没有地址"] = (
        esports_data.logo_url(with_logos, "示例战队") == "https://dl.evotracker.cn/esports/logos/" + good
        and esports_data.logo_url(with_logos, "网址") is None and esports_data.logo_url(with_logos, "没有这支队") is None
        and esports_data.logo_url({"players": [], "events": []}, "示例战队") is None and esports_data.logo_url(None, "示例战队") is None
        and esports_data.logo_url({"teams": {"示例战队": {"logo": "../x.png"}}}, "示例战队") is None)
    def stream(pid, name="官方直播", platform="bilibili", room="7734200"):
        return {"id": pid, "name": name, "platform": platform, "room": room}

    wanted = [stream("bilibili-7734200"), stream("douyin-xxfps", "某解说", "douyin", "xxfps")]
    odd = [*wanted, stream("x1", platform="kuaishou"), stream("x2", platform="douyu", room="abc"), stream("x3", name=""),
           stream("有中文"), "不是字典", stream("bilibili-7734200", "同一个 id 写了两次")]
    staged = esports_data.clean(file(events=[event(main=odd), event(eid="e2"), event(eid="e3", main="官方直播"),
                                             event(eid="e4", main=[stream(f"b{n}", room=str(n)) for n in range(12)])]))
    out["主舞台：每场赛事可以带几路播导播视角的直播间，照写的顺序；写得不对的那一路去掉；最多 8 路；没有主舞台的赛事不带这一项"] = (
        staged["events"][0]["main"] == wanted and "main" not in staged["events"][1] and "main" not in staged["events"][2]
        and [m["id"] for m in staged["events"][3]["main"]] == [f"b{n}" for n in range(8)] and len(staged["events"]) == 4)
    both = esports_data.clean(file([player(pid="douyu-1")], [event(main=[stream("douyu-1", platform="douyu", room="1"), wanted[0]])]))
    out["主舞台里和这场赛事的选手同一个 id 的那一路不算；按 id 找得到选手，也找得到主舞台的直播间"] = (
        esports_data.event_main(both, both["events"][0]) == [wanted[0]]
        and esports_data.event_main(staged, staged["events"][1]) == []
        and esports_data.find_stream(both, "douyu-1")["name"] == "示例选手"
        and esports_data.find_stream(both, "bilibili-7734200") == wanted[0] and esports_data.find_stream(both, "nobody") is None)
    quiet = esports_data.clean(file([player(), player(pid="none-0123456789ab", platform="none", room=""),
                                     player(pid="none-bbbbbbbbbbbb", platform="none", room="123"),
                                     player(pid="none-cccccccccccc", platform="none", room=None),
                                     player(pid="x9", platform="None", room="")]))
    out["不开直播的选手（platform 是 none、房间号是空的）照样留在名单里；写了房间号的、写法不对的不要；数开直播的人时不算他"] = (
        [(p["id"], p["platform"], p["room"]) for p in quiet["players"]] == [("douyu-1", "douyu", "1"), ("none-0123456789ab", "none", "")]
        and [p["id"] for p in esports_data.streaming(quiet["players"])] == ["douyu-1"]
        and len(esports_data.event_players(quiet, quiet["events"][0])) == 2)
    broken = 0
    for data in ([], {"version": 2, "players": [], "events": []}, {"version": 1, "players": []}, None):
        try:
            esports_data.clean(data)
        except esports_data.EsportsError:
            broken += 1
    out["整个文件不对（不是字典、版本不认识、缺了选手或赛事）当作没读到"] = broken == 4
    switches = esports_data.clean(file(platforms={"douyu": False, "huya": "false", "kuaishou": False}))["platforms"]
    out["平台开关：写了 false 的是关，没写、写错的当作开，不认识的平台不要"] = switches == {
        "douyu": False, "huya": True, "bilibili": True, "douyin": True}


def status_checks(out):
    events = [event("live"), event("soon", NOW + timedelta(hours=2), NOW + timedelta(hours=5)),
              event("later", NOW + timedelta(days=2), NOW + timedelta(days=2, hours=3)),
              event("done", NOW - timedelta(days=1, hours=4), NOW - timedelta(days=1)),
              event("done6", NOW - timedelta(days=6, hours=4), NOW - timedelta(days=6)),
              event("old", NOW - timedelta(days=8), NOW - timedelta(days=7, hours=20))]
    data = esports_data.clean(file(events=events))
    found = esports_data.groups(data, NOW)
    out["进行中、即将开始（先开始的在前）、最近结束（刚结束的在前，只留 7 天）按时间分"] = (
        [e["id"] for e in found["live"]] == ["live"] and [e["id"] for e in found["upcoming"]] == ["soon", "later"]
        and [e["id"] for e in found["recent"]] == ["done", "done6"])
    edge = esports_data.clean(file(events=[event("x", NOW, NOW + timedelta(hours=1))]))["events"][0]
    out["正好到开始时刻算进行中，正好到结束时刻算已结束"] = (
        esports_data.status(edge, NOW) == "live" and esports_data.status(edge, NOW + timedelta(hours=1)) == "ended"
        and esports_data.status(edge, NOW - timedelta(seconds=1)) == "upcoming")
    roster = [player("a1", team="甲"), player("b1", team="乙", room="2"), player("a2", team="甲", room="3")]
    data = esports_data.clean(file(roster, [event(teams=("乙", "甲", "没有人的队"))]))
    teams = esports_data.event_teams(data, data["events"][0])
    out["一场赛事的队伍按赛程里的顺序，每支队带上自己的选手"] = (
        [(team, [p["id"] for p in members]) for team, members in teams]
        == [("乙", ["b1"]), ("甲", ["a1", "a2"]), ("没有人的队", [])])


def download_checks(out, server):
    port = server.server_address[1]
    esports_data.URL = f"http://127.0.0.1:{port}/esports.json"
    Handler.reply = (200, json.dumps(file()).encode("utf-8"))
    first = esports_data.get()
    sent = Handler.requests[-1] if Handler.requests else {}
    extra = {k.lower() for k in sent} - {"host", "user-agent", "cache-control", "accept-encoding", "connection"}
    out["打开赛事页时下载，请求里只带 Evo/版本号"] = (
        first["ok"] and first["data"]["events"][0]["id"] == "e1"
        and sent.get("User-Agent") == f"Evo/{storage.APP_VERSION}" and not extra)
    count = len(Handler.requests)
    again = esports_data.get()
    out["60 秒内再打开不重新下载"] = again["ok"] and len(Handler.requests) == count
    saved = json.loads(esports_data.CACHE_PATH.read_text(encoding="utf-8"))
    saved["fetched_at"] -= 300
    esports_data.CACHE_PATH.write_text(json.dumps(saved), encoding="utf-8")
    kept = esports_data.get(fresh=600)
    kept_count = len(Handler.requests)
    esports_data.get()
    out["看直播页开着时 10 分钟内不重新下载（5 分钟前下的还算新）；赛事页过了 60 秒就重下"] = (
        kept["ok"] and kept_count == count and len(Handler.requests) == count + 1)
    Handler.reply = (200, json.dumps(file(events=[event("e2")])).encode("utf-8"))
    forced = esports_data.get(force=True)
    out["过了 60 秒（或要求重下）读到新的"] = forced["data"]["events"][0]["id"] == "e2"
    Handler.reply = (200, b"<html>not json</html>")
    garbage = esports_data.get(force=True)
    Handler.reply = (500, b"oops")
    down = esports_data.get(force=True)
    out["格式不对、连不上时用上次存下的，并说明没读到"] = all(
        not got["ok"] and got["error"] and got["data"]["events"][0]["id"] == "e2" for got in (garbage, down))
    esports_data.CACHE_PATH.write_text("坏了", encoding="utf-8")
    out["存下的文件坏了、又连不上：没有数据，不报错"] = esports_data.get(force=True)["data"] is None
    with tempfile.TemporaryDirectory() as folder:
        local = Path(folder) / "本机的.json"
        local.write_text(json.dumps(file(events=[event("local")])), encoding="utf-8")
        out["地址可以换成本机的一个文件（在本机看效果用）"] = (
            esports_data.fetch(str(local))["events"][0]["id"] == "local")


# 平台的回包：只留用到的字段，样子照 2026-11-17 实际问到的
DOUYU_LIVE = {"room": {"room_id": 100, "room_name": "示例标题", "nickname": "示例主播", "show_status": 1, "videoLoop": 0,
                       "show_time": 1794924295, "avatar_mid": "https://apic.douyucdn.cn/x_middle.jpg"}, "game": {}}
HUYA_ON = {"status": 200, "message": "", "data": {
    "liveStatus": "ON", "realLiveStatus": "ON", "stream": {}, "profileInfo": {"nick": "示例主播", "avatar180": "https://huyaimg.msstatic.com/a.jpg"},
    "liveData": {"nick": "示例主播", "roomName": "房间名", "introduction": "示例标题", "startTime": 1793092036}}}
BILI = {"code": 0, "message": "OK", "data": {"by_uids": {}, "by_room_ids": {
    "5440": {"room_id": 5440, "short_id": 1, "live_status": 2, "title": "轮播中", "uname": "示例官方", "live_time": "0000-00-00 00:00:00"},
    "7734200": {"room_id": 7734200, "short_id": 6, "live_status": 1, "title": "示例标题", "uname": "示例主播", "live_time": "2026-11-14 10:25:51"},
    "300": {"room_id": 300, "short_id": 0, "live_status": 0, "title": "", "uname": "没开播的", "live_time": "0000-00-00 00:00:00"}}}}
DOUYIN_PAGE = b'<html>...self.__pace_f.push([1,"{\\"state\\":{\\"roomStore\\":{\\"roomInfo\\":{\\"roomId\\":\\"7708905317966400307\\",\\"web_rid\\":\\"800\\"}}}}"])</html>'
DOUYIN_ROOM = {"status_code": 0, "data": {"room": {"id_str": "7708905317966400307", "status": 2, "title": "示例标题", "create_time": 1794869424,
                                                   "owner": {"nickname": "示例主播", "avatar_thumb": {"url_list": ["https://p3.douyinpic.com/a.jpeg"]}}}}}


def raw(data):
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def parse_checks(out):
    live = live_status.parse_douyu(raw(DOUYU_LIVE))
    loop = live_status.parse_douyu(raw({"room": {**DOUYU_LIVE["room"], "videoLoop": 1}}))
    off = live_status.parse_douyu(raw({"data": {"room": {**DOUYU_LIVE["room"], "show_status": 2}}}))
    gone = live_status.parse_douyu("<html><title>提示信息 -斗鱼</title>该房间目前没有开放</html>".encode("utf-8"))
    out["斗鱼：在播（主播名、标题、头像、开播时刻）、轮播不算在播、没开播、房间不存在"] = (
        live == {"state": "live", "loop": False, "name": "示例主播", "title": "示例标题",
                 "avatar": "https://apic.douyucdn.cn/x_middle.jpg", "since": 1794924295, "error": ""}
        and (loop["state"], loop["loop"], loop["since"]) == ("offline", True, None)
        and (off["state"], off["loop"]) == ("offline", False) and gone["state"] == "missing")
    on = live_status.parse_huya(raw(HUYA_ON))
    replay = live_status.parse_huya(raw({**HUYA_ON, "data": {**HUYA_ON["data"], "liveStatus": "REPLAY"}}))
    closed = live_status.parse_huya(raw({**HUYA_ON, "data": {**HUYA_ON["data"], "liveStatus": "OFF"}}))
    nobody = live_status.parse_huya(raw({"status": 422, "message": "该主播不存在！", "data": []}))
    out["虎牙：在播、轮播、没开播、主播不存在"] = (
        (on["state"], on["name"], on["title"], on["since"]) == ("live", "示例主播", "示例标题", 1793092036)
        and (replay["state"], replay["loop"]) == ("offline", True) and closed["state"] == "offline"
        and nobody["state"] == "missing")
    found = live_status.parse_bilibili(raw(BILI), ["6", "7734200", "1", "300", "999"])
    since = datetime(2026, 11, 14, 10, 25, 51, tzinfo=live_status.BEIJING).timestamp()
    out["B站：短号、真实房间号都认；在播（开播时刻按北京时间）、轮播、没开播、不在回包里的是房间不存在"] = (
        found["6"] == found["7734200"] and (found["6"]["state"], found["6"]["since"]) == ("live", since)
        and (found["1"]["state"], found["1"]["loop"]) == ("offline", True)
        and found["300"]["state"] == "offline" and found["999"]["state"] == "missing")
    room = live_status.parse_douyin_room(raw(DOUYIN_ROOM))
    ended = live_status.parse_douyin_room(raw({"data": {"room": {**DOUYIN_ROOM["data"]["room"], "status": 4}}}))
    blocked = None
    try:
        live_status.parse_douyin_page("<html>验证码中间页</html>".encode("utf-8"))
    except live_status.StatusError as e:
        blocked = str(e)
    out["抖音：网页里读出内部房间号；在播、没开播；页面里没有房间号是房间不存在；被拦说明原因"] = (
        live_status.parse_douyin_page(DOUYIN_PAGE) == "7708905317966400307"
        and live_status.parse_douyin_page(b"<html>nothing here</html>") is None
        and (room["state"], room["name"], room["avatar"]) == ("live", "示例主播", "https://p3.douyinpic.com/a.jpeg")
        and ended["state"] == "offline" and blocked is not None and "拦" in blocked)
    errors = 0
    for parser, body in ((live_status.parse_douyu, b"<html>502</html>"), (live_status.parse_huya, b"[]"),
                         (live_status.parse_huya, raw({"status": 500})), (live_status.parse_douyin_room, raw({"data": {}})),
                         (lambda b: live_status.parse_bilibili(b, ["1"]), raw({"code": -412, "message": "请求被拦截"}))):
        try:
            parser(body)
        except live_status.StatusError:
            errors += 1
    out["回了不认识的东西：当作这次没查到，不当成没开播"] = errors == 5


def query_checks(out):
    calls, lock, running, peak = [], threading.Lock(), [0], [0]
    reply = {"douyin": DOUYIN_ROOM}  # 抖音查房间的接口现在回什么
    live_status._douyin_inner.clear()

    def get(url, headers, limit=None, until=None, cookies=False):
        with lock:
            calls.append((url, headers, cookies))
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.03)
        with lock:
            running[0] -= 1
        if "douyu.com/betard/404" in url:
            raise live_status.StatusError("HTTP 404", 404)
        if "douyu.com" in url:
            return raw(DOUYU_LIVE)
        if "huya.com" in url:
            raise live_status.StatusError("连不上（超时）")
        if "bilibili.com" in url:
            return raw(BILI)
        if "live.douyin.com" in url:
            return DOUYIN_PAGE
        return raw(reply["douyin"])

    players = ([player(f"dy{i}", room=str(100 + i)) for i in range(6)] + [player("same", room="100"), player("gone", room="404")]
               + [player("hy", platform="huya", room="7")]
               + [player(f"bl{i}", platform="bilibili", room=room) for i, room in enumerate(("6", "1", "300", "999"))]
               + [player("tt", platform="douyin", room="800")]
               + [player("bad", room="1/../x"), player("ks", platform="kuaishou", room="1")])
    found = live_status.query(players, get)
    urls = [url for url, _, _ in calls]
    out["同时最多 4 个请求"] = 1 < peak[0] <= 4
    out["B站一次请求查完所有房间"] = (
        len([u for u in urls if "bilibili.com" in u]) == 1
        and all(f"room_ids={room}" in next(u for u in urls if "bilibili.com" in u) for room in ("6", "1", "300", "999")))
    out["同一个房间只查一次；房间号不是数字、不认识的平台不发请求"] = (
        len([u for u in urls if u.endswith("/betard/100")]) == 1 and not any("../" in u or "kuaishou" in u for u in urls)
        and found["bad"]["state"] == found["ks"]["state"] == "error" and found["same"] == found["dy0"])
    out["一个平台没查到只影响它自己：虎牙连不上，别的照常"] = (
        found["hy"]["state"] == "error" and "连不上" in found["hy"]["error"] and found["dy3"]["state"] == "live"
        and found["bl0"]["state"] == "live" and found["bl3"]["state"] == "missing" and found["gone"]["state"] == "missing"
        and found["tt"]["state"] == "live")
    signed = next(u for u in urls if "reflow" in u)
    cookie_use = {("douyin" in url or "amemv" in url): cookies for url, _, cookies in calls}
    agents = {headers["User-Agent"] for _, headers, _ in calls}
    out["抖音：先开网页、再带着内部房间号和签名问接口；游客 Cookie 只给抖音用；请求里没有 Evo 的标识"] = (
        "room_id=7708905317966400307" in signed and "&a_bogus=" in signed and "msToken=" in signed
        and cookie_use == {True: True, False: False} and not any("Evo" in agent for agent in agents))
    tiktok = [player("tt", platform="douyin", room="800")]

    def ask():
        start = len(calls)
        state = live_status.query(tiktok, get)["tt"]["state"]
        return state, ["网页" if "live.douyin.com" in url else "接口" for url, _, _ in calls[start:]]

    again = ask()
    reply["douyin"] = {"data": {"room": {**DOUYIN_ROOM["data"]["room"], "status": 4}}}
    ended, later = ask(), ask()
    out["抖音在播的房间下次只问接口，不再开 1 MB 的网页；这一场结束了算没开播，再下次重新开网页找新的一场"] = (
        again == ("live", ["接口"]) and ended == ("offline", ["接口"]) and later == ("offline", ["网页", "接口"]))
    live_status._douyin_inner.clear()
    out["直播间地址只按配置拼，房间号不对就没有地址"] = (
        live_status.room_url("bilibili", "6") == "https://live.bilibili.com/6"
        and live_status.room_url("douyu", "9/../x") is None and live_status.room_url("kuaishou", "1") is None
        and live_status.room_url("douyin", "Smallfishuwu") == "https://live.douyin.com/Smallfishuwu"
        and live_status.room_url("douyin", "a.b/../x") is None and live_status.room_url("douyu", "abc") is None)
    odd = live_status.query([{"id": "1", "platform": "douyin", "room": "a.b"}, {"id": "2", "platform": "huya", "room": "lck"}],
                            get=lambda *args, **more: (_ for _ in ()).throw(AssertionError("不该联网")))
    out["房间号写得不对的不去问平台"] = all(found["state"] == live_status.ERROR for found in odd.values())


def sign_checks(out):
    out["SM3 对得上国密标准里的两个样例"] = (
        abogus.sm3(b"abc").hex() == "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"
        and abogus.sm3(b"abcd" * 16).hex() == "debe9ff92275b8a138604889c18e5a4d6fdb70e5387e5765293dcba39c0c5732")
    sign = abogus.sign("type_id=0&live_id=1&room_id=7300000000000000001", DOUYIN_UA, now_ms=1794900000000)
    out["固定时间下的签名和 2026-11-17 记下的一样（改坏了会变）"] = sign == (
        "E7mhBmg6mEVNgf6X5InLfY3q61N3YIg30HViMD2fqxvwxy39HMYD9exolrzvfCWjF4/JIeYjy4hbO3xprQAjM36UHWwEUdQ2mgWkKl5Q5I0j"
        "53iruyRDntmF4vj3SFlm5XNAEOk0y75rKb70Woqe-vIlO62-zo0/9f6D=")
    other = abogus.sign("type_id=0&live_id=1&room_id=7300000000000000002", DOUYIN_UA, now_ms=1794900000000)
    url = abogus.signed_url("https://example.invalid/x/", [("b", "1 2"), ("a", "中")], DOUYIN_UA, now_ms=1794900000000)
    out["参数、时间变了签名跟着变；地址里参数的顺序不变，签名接在最后"] = (
        other != sign and abogus.sign("type_id=0", DOUYIN_UA) != abogus.sign("type_id=0", DOUYIN_UA, now_ms=1)
        and url.startswith("https://example.invalid/x/?b=1+2&a=%E4%B8%AD&a_bogus=") and url.endswith("%3D"))


def run_tool(folder, players, events, encoding="utf-8-sig", *more, event_columns="名称,类型,阶段,开始,结束,队伍"):
    """把两张表写进 folder，运行 tools/esports.py --out，返回 (退出码, 打印的字, 生成的内容或 None)。"""
    folder = Path(folder)
    (folder / "选手库.csv").write_bytes(("选手,战队,平台,房间号,Liquipedia页面名\r\n" + "\r\n".join(players) + "\r\n").encode(encoding))
    (folder / "赛程.csv").write_bytes((event_columns + "\r\n" + "\r\n".join(events) + "\r\n").encode(encoding))
    target = folder / "out.json"
    target.unlink(missing_ok=True)
    done = subprocess.run([sys.executable, str(TOOL), "--dir", str(folder), "--out", str(target), *more],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    return done.returncode, done.stdout + done.stderr, json.loads(target.read_text(encoding="utf-8")) if target.exists() else None


def tool_checks(out):
    today = datetime.now(live_status.BEIJING)
    def day(offset, hour):
        return f"{today + timedelta(days=offset):%Y-%m-%d} {hour}"

    roster = ["阿一,星火电竞,斗鱼,101,Ayi", "阿二,星火电竞,虎牙,102,", "阿三,星火电竞,B站,103,",
              "乙一,北辰,抖音,201,", "乙二,北辰,斗鱼,202,", "乙三,北辰,B站,203,",
              "丙一,没比赛的队,斗鱼,301,", "丙二,没比赛的队,斗鱼,302,", "丙三,没比赛的队,斗鱼,303,", ",,,,"]
    schedule = [f"职业联赛 第 3 周,联赛,B 组对 C 组,{day(0, '19:00')},{day(0, '23:00')},星火电竞、北辰",
                f"训练赛,训练赛,,{day(1, '20:00').replace('-', '/')},{day(1, '23:30:00').replace('-', '/')},北辰",
                f"上上周的杯赛,杯赛,决赛,{day(-9, '19:00')},{day(-9, '23:00')},星火电竞"]
    results = {}
    for encoding in ("gbk", "utf-8-sig", "utf-8"):
        with tempfile.TemporaryDirectory() as folder:
            results[encoding] = run_tool(folder, roster, schedule, encoding)
    code, text, data = results["gbk"]
    out["GBK（Excel 默认）、带 BOM 的 UTF-8、UTF-8 存的表都读得出，生成的内容一样"] = (
        code == 0 and data is not None
        and all(r[0] == 0 and r[2] and {**r[2], "updated": ""} == {**data, "updated": ""} for r in results.values()))
    data = data or {"players": [], "events": [{}]}
    out["生成的文件 Evo 读得进去：选手带平台和房间号，赛事按开始时间排、带北京时间"] = (
        esports_data.clean(data)["players"] == data["players"] and esports_data.clean(data)["events"] == data["events"]
        and data["players"][0] == {"id": "douyu-101", "name": "阿一", "team": "星火电竞", "platform": "douyu",
                                   "room": "101", "liquipedia": "Ayi"}
        and [e.get("id", "")[-6:] for e in data["events"]] == ["league", "-scrim"]
        and data["events"][0]["start"].endswith("T19:00:00+08:00") and data["events"][0]["teams"] == ["星火电竞", "北辰"])
    out["Excel 改成 2026/11/17 20:00、带秒的时间也认"] = (
        len(data["events"]) == 2 and data["events"][1].get("end", "").endswith("T23:30:00+08:00"))
    out["结束超过 7 天的赛事不放进去并提醒；选手只放这些赛事用到的队伍"] = (
        "结束超过 7 天" in text and "第 4 行" in text
        and {p["team"] for p in data["players"]} == {"星火电竞", "北辰"} and len(data["players"]) == 6)
    bad_roster = ["阿一,星火电竞,斗鱼,101,", "阿二,星火电竞,快手,102,", "阿三,星火电竞,B站,10x3,",
                  "阿一,北辰,抖音,201,", "乙二,北辰,斗鱼,101,", ",北辰,B站,203,", "丁一,两个人的队,斗鱼,401,", "丁二,两个人的队,斗鱼,402,"]
    bad_schedule = [f"联赛,联赛,,{day(0, '19:00')},{day(0, '18:00')},两个人的队",
                    f"训练赛,友谊赛,,明天晚上,{day(1, '23:00')},星火电竟、星火电竞、星火电竞",
                    f",杯赛,,{day(2, '19:00')},{day(2, '23:00')},星火电竞，北辰"]
    with tempfile.TemporaryDirectory() as folder:
        code, text, data = run_tool(folder, bad_roster, bad_schedule)
    wanted = ["选手库.csv 第 3 行：平台「快手」不认识", "选手库.csv 第 4 行：房间号「10x3」只能是数字",
              "选手库.csv 第 5 行：选手「阿一」在第 2 行已经写过", "选手库.csv 第 6 行：斗鱼 房间 101 在第 2 行已经写过",
              "选手库.csv 第 7 行：「选手」是空的", "战队「两个人的队」有 2 个人（第 8、9 行）",
              "赛程.csv 第 2 行：结束", "赛程.csv 第 3 行：类型「友谊赛」不认识", "赛程.csv 第 3 行：开始「明天晚上」要写成",
              "赛程.csv 第 3 行：队伍「星火电竞」写了 2 次", "队伍「星火电竟」不在选手库.csv里（是不是「星火电竞」？）",
              "赛程.csv 第 4 行：「名称」是空的", "赛程.csv 第 4 行：队伍要用顿号「、」隔开"]
    missing = [w for w in wanted if w not in text]
    if missing:
        print("    没指出来的：", missing)
    out["每种写错都指出是哪个文件第几行，一次全列出来，不生成文件"] = code == 1 and data is None and not missing
    letters = ["阿一,字母队,抖音,XXFPS,", "阿二,字母队,抖音,NOVA_Tt1seven,", "阿三,字母队,抖音,197427647791,"]
    one_event = [f"训练赛,训练赛,,{day(0, '19:00')},{day(0, '23:00')},字母队"]
    with tempfile.TemporaryDirectory() as folder:
        code, text, data = run_tool(folder, letters, one_event)
    out["抖音的房间号可以写主播的抖音号，生成时转成小写（抖音不分大小写）"] = (
        code == 0 and data is not None and [(p["id"], p["room"]) for p in data["players"]] == [
            ("douyin-xxfps", "xxfps"), ("douyin-nova_tt1seven", "nova_tt1seven"), ("douyin-197427647791", "197427647791")]
        and esports_data.clean(data)["players"] == data["players"])
    with tempfile.TemporaryDirectory() as folder:
        code, text, data = run_tool(folder, ["阿一,字母队,抖音,XXFPS,", "阿二,字母队,抖音,xxfps,", "阿三,字母队,抖音,a.b,",
                                             "阿四,数字队,斗鱼,xxfps,", "阿五,数字队,虎牙,1,", "阿六,数字队,B站,2,"], one_event)
    out["抖音大小写不同的同一个房间查得出来；带点的、别的平台写字母的照样不行"] = (
        code == 1 and data is None and "第 3 行：抖音 房间 xxfps 在第 2 行已经写过" in text
        and "第 4 行：房间号「a.b」只能是字母、数字、下划线" in text and "第 5 行：房间号「xxfps」只能是数字" in text)
    quiet_event = [f"训练赛,训练赛,,{day(0, '19:00')},{day(0, '23:00')},安静队"]
    with tempfile.TemporaryDirectory() as folder:
        code, text, data = run_tool(folder, ["甲一,安静队,斗鱼,11,", "甲二,安静队,不直播,,", "甲三,安静队,不直播,,Jia3"], quiet_event)
    hashed = "none-" + hashlib.sha256("甲二".encode("utf-8")).hexdigest()[:12]
    out["选手库里平台写「不直播」、房间号空着：照样算队里的人（凑满 3 个），id 是 none- 加选手名的哈希；两个不直播的不算重复"] = (
        code == 0 and data is not None and "不直播 2" in text
        and [(p["id"], p["platform"], p["room"]) for p in data["players"]][:2] == [("douyu-11", "douyu", "11"), (hashed, "none", "")]
        and data["players"][2]["id"].startswith("none-") and data["players"][2]["id"] != hashed
        and data["players"][2]["liquipedia"] == "Jia3" and esports_data.clean(data)["players"] == data["players"])
    with tempfile.TemporaryDirectory() as folder:
        code, text, data = run_tool(folder, ["甲一,安静队,斗鱼,11,", "甲二,安静队,不直播,123,", "甲三,安静队,斗鱼,,"],
                                    [quiet_event[0] + ",不直播 1 某台"], event_columns="名称,类型,阶段,开始,结束,队伍,主舞台")
    out["不直播的选手写了房间号、开直播的没写房间号、主舞台写「不直播」：都指出来，不生成文件"] = (
        code == 1 and data is None and "第 3 行：不直播的选手「房间号」要空着（现在是「123」）" in text
        and "第 4 行：「房间号」是空的" in text and "主舞台的平台「不直播」不认识，只能写 斗鱼 / 虎牙 / B站 / 抖音" in text
        and "第 3 行：「房间号」是空的" not in text)
    seven = "名称,类型,阶段,开始,结束,队伍,主舞台"
    with tempfile.TemporaryDirectory() as folder:
        code, text, data = run_tool(
            folder, letters, [one_event[0] + ",B站 7734200 官方直播、 抖音  XXFPS  某某 解说 ", one_event[0].replace("19:00", "20:00") + ","],
            event_columns=seven)
    out["赛程表可以多一列「主舞台」（平台 房间号 名字，多路用顿号隔开）：生成时拼好 id，抖音的房间号转小写；没写的赛事不带这一项"] = (
        code == 0 and data is not None and "主舞台 2 路" in text
        and data["events"][0]["main"] == [
            {"id": "bilibili-7734200", "name": "官方直播", "platform": "bilibili", "room": "7734200"},
            {"id": "douyin-xxfps", "name": "某某 解说", "platform": "douyin", "room": "xxfps"}]
        and "main" not in data["events"][1] and esports_data.clean(data)["events"] == data["events"])
    with tempfile.TemporaryDirectory() as folder:
        many = "、".join(f"B站 {n} 第{n}路" for n in range(1, 10))
        code, text, data = run_tool(folder, letters, [
            one_event[0] + ",快手 1 某台、B站 abc 官方、只有两段 1、B站 5 甲、B站 5 乙、抖音 a.b 解说、B站 6 " + "长" * 41,
            one_event[0].replace("19:00", "20:00") + "," + many], event_columns=seven)
    wanted = ["第 2 行：主舞台的平台「快手」不认识", "第 2 行：主舞台的房间号「abc」只能是数字", "第 2 行：主舞台「只有两段 1」要写成：平台 房间号 名字",
              "第 2 行：主舞台里 B站 房间 5 写了两次", "第 2 行：主舞台的房间号「a.b」只能是字母、数字、下划线",
              "第 2 行：主舞台的名字最多 40 个字", "第 3 行：主舞台最多 8 路（现在 9 路）"]
    missing = [w for w in wanted if w not in text]
    if missing:
        print("    没指出来的：", missing)
    out["主舞台写错了（平台、房间号、少了名字、同一个房间写两次、名字太长、超过 8 路）都指出是第几行，不生成文件"] = (
        code == 1 and data is None and not missing)
    sys.path.insert(0, str(TOOL.parent))
    import esports as tool  # tools/esports.py
    members = [{"line": n, "name": f"选手{n}", "team": team, "platform": "douyu", "room": str(n), "liquipedia": ""}
               for n, team in enumerate(["有队徽的队"] * 3 + ["没队徽的队"] * 3 + ["没比赛的队"] * 3, 1)]
    matches = [{"line": 2, "name": "联赛", "type": "联赛", "stage": "", "teams": ["有队徽的队", "没队徽的队"],
                "start": today + timedelta(hours=1), "end": today + timedelta(hours=4)}]
    online_teams = {"有队徽的队": {"logo": "3fa9c1d2e4b5a6f7.webp"}, "没比赛的队": {"logo": "0123456789abcdef.png"},
                    "写错的": {"logo": "x.gif"}}
    built = tool.build(members, matches, {}, [], teams=online_teams)
    plain = tool.build(members, matches, {}, [])
    out["发布脚本不管队徽：上传时照线上现在的带上，只留这次用到的队；一个都没有就不写这一项"] = (
        built["teams"] == {"有队徽的队": {"logo": "3fa9c1d2e4b5a6f7.webp"}} and "teams" not in plain
        and esports_data.clean(built)["teams"] == built["teams"]
        and {**built, "teams": None, "updated": ""} == {**plain, "teams": None, "updated": ""})
    with tempfile.TemporaryDirectory() as folder:
        code, text, data = run_tool(folder, roster, schedule)
        (Path(folder) / "赛程.csv").write_text("名字,类型\n", encoding="utf-8")
        again = subprocess.run([sys.executable, str(TOOL), "--dir", folder, "--out", str(Path(folder) / "x.json")],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        empty = subprocess.run([sys.executable, str(TOOL), "--dir", str(Path(folder) / "没有这个文件夹"), "--out", "x.json"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    out["表头不对、找不到表：说明原因，不生成文件"] = (
        again.returncode == 1 and "第 1 行（表头）要是：名称,类型,阶段,开始,结束,队伍" in again.stdout
        and empty.returncode == 1 and "找不到" in empty.stdout)


def main():
    print("赛事数据（赛事文件的格式和下载、查开播状态、抖音签名、发赛事数据的脚本）")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    out = {}
    try:
        format_checks(out)
        status_checks(out)
        download_checks(out, server)
        parse_checks(out)
        query_checks(out)
        sign_checks(out)
        tool_checks(out)
    finally:
        server.shutdown()
    return report(out)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
