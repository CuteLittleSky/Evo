"""电竞页的关注和开播通知（follows.py、follow_alerts.py、panel_esports.py 的 /esports/follow…、托盘里查开播的线程）。
不联网：赛事文件是临时文件夹里的一个文件，查开播状态换成假的。（tests/test_follow.py 是总览页的"关注我"，不是这个。）

- 谁刚开播（follow_alerts.Tracker，照 PureLive 的规则）：第一次看到只记下；没在播 → 在播提醒一次；同一场（开播时刻一样、
  断了很快重连）不提醒两遍；两次检查之间下播又开了新的一场也提醒；没查到的不算数；打游戏那段时间里开播的，关了游戏补
- 关注的名单（follows.py）：存在 settings.json，写得不对的不算，最多 MAX 位；开播通知没问过是 None
- 接口：关注 / 取消（只能关注名单里有的人）、开 / 关通知；都要带面板的口令
- 页面：电竞页"我关注的"一块和主播卡片上的星星；看直播页名单每行的星星（主舞台没有）、"关注"按钮、"我关注的"一组；
  设置页的开关
- 托盘里的线程（evo_app.start_follow_checker）：没打开通知不查；Apex 开着不查也不弹，关了马上查一次、补上

运行：python tests/test_follow_alerts.py
"""

import json
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import app_settings
import esports_data
import evo_app
import follow_alerts
import follows
import live_status
import panel_app
import panel_esports
import storage

NOW = datetime.now().astimezone().replace(microsecond=0)
PLAYERS = [
    {"id": "douyu-1", "name": "甲队1号", "team": "甲队", "platform": "douyu", "room": "1"},
    {"id": "bilibili-2", "name": "甲队2号", "team": "甲队", "platform": "bilibili", "room": "2"},
    {"id": "none-0123456789ab", "name": "<b>不播的</b>", "team": "乙队", "platform": "none", "room": ""},
    {"id": "douyin-abc", "name": "乙队2号", "team": "乙队", "platform": "douyin", "room": "abc"},
    {"id": "huya-9", "name": "丙队1号", "team": "丙队", "platform": "huya", "room": "9"},
]


def event(eid, start, teams, hours=4, main=None):
    return {"id": eid, "name": f"示例联赛 {eid}", "type": "联赛", "stage": "决赛", "start": start.isoformat(),
            "end": (start + timedelta(hours=hours)).isoformat(), "teams": list(teams), **({"main": main} if main else {})}


def serve(folder, events):
    path = Path(folder) / "esports.json"
    path.write_text(json.dumps({"version": 1, "updated": NOW.isoformat(), "players": PLAYERS, "events": events,
                                "streamers": [{"id": "douyu-901", "name": "大主播", "platform": "douyu", "room": "901"}]}),
                    encoding="utf-8")
    esports_data.URL = str(path)
    esports_data.CACHE_PATH.unlink(missing_ok=True)
    panel_esports._status.update(at=0.0, found={})


def reset():
    app_settings.set_value(follows.KEY, [])
    app_settings.set_value(follows.NOTIFY_KEY, None)


def got(state, since=None, title=""):
    return live_status.result(state, title=title, since=since)


def tracker_checks(out):
    clock = [10_000.0]
    tracker = follow_alerts.Tracker(clock=lambda: clock[0])
    first = tracker.observe({"a": got("live", 9_000), "b": got("offline")})
    clock[0] += 300
    second = tracker.observe({"a": got("live", 9_000), "b": got("live", 10_200)})
    clock[0] += 300
    third = tracker.observe({"a": got("live", 9_000), "b": got("live", 10_200)})
    out["第一次看到只记下（那时已经在播的不是新闻）；没在播 → 在播提醒一次，下一次不再提醒"] = (first, second, third) == ([], ["b"], [])

    clock[0] += 300
    missed = tracker.observe({"a": got("error"), "b": got("offline")})
    clock[0] += 300
    back = tracker.observe({"a": got("live", 9_000), "b": got("live", 10_200)})
    out["这次没查到的不算数（不会因为没查到又提醒一遍）；下播几分钟又回来、开播时刻没变是同一场，不提醒"] = (missed, back) == ([], [])

    clock[0] += 300
    tracker.observe({"a": got("live", 9_000), "b": got("offline")})
    clock[0] += 3600
    again = tracker.observe({"a": got("live", clock[0] - 200), "b": got("live", clock[0] - 100)})
    out["下播以后又开了新的一场：提醒；一直在播、但开播时刻往后挪了很多（两次检查之间下播又开播）：也提醒"] = again == ["a", "b"]

    clock[0] += 300
    tracker.observe({"a": got("offline"), "b": got("live", clock[0] - 400)})
    clock[0] += 120
    flaky = tracker.observe({"a": got("live", None), "b": got("live", clock[0] - 520)})
    clock[0] += 3600
    later = tracker.observe({"a": got("offline"), "b": got("live", clock[0] - 4100)})
    clock[0] += 3600
    fresh = tracker.observe({"a": got("live", None), "b": got("live", clock[0] - 7700)})
    out["平台没给开播时刻：下播不到 15 分钟又在播算同一场（推流断了重连），隔得久了算新的一场"] = (flaky, later, fresh) == ([], [], ["a"])

    held = follow_alerts.Tracker(clock=lambda: clock[0])
    game_start = clock[0] - 1800
    after_game = held.observe({"x": got("live", clock[0] - 600), "y": got("live", clock[0] - 7200), "z": got("live", None),
                               "w": got("offline")}, since_after=game_start)
    out["打游戏那段时间没查：关了游戏第一次查，那段时间里开播、现在还在播的补上；更早就在播的、不知道几点开播的不补"] = after_game == ["x"]
    held.observe({"x": got("live", clock[0] - 600)})
    out["取消关注了的不再记着（再关注时重新从\"第一次看到\"算）"] = sorted(held._seen) == ["x"]


def storage_checks(out, folder):
    reset()
    out["还没关注任何人：名单是空的；开播通知还没问过（None）"] = follows.ids() == [] and follows.notify() is None
    follows.set_following("douyu-1", True)
    follows.set_following("none-0123456789ab", True)
    follows.set_following("douyu-1", True)
    follows.set_following("a/../b", True)
    kept = follows.ids()
    follows.set_following("douyu-1", False)
    out["关注、取消：照关注的先后记在 settings.json，不重复；id 的样子不对的不加"] = (
        kept == ["douyu-1", "none-0123456789ab"] and follows.ids() == ["none-0123456789ab"]
        and json.loads(app_settings.SETTINGS_PATH.read_text(encoding="utf-8"))[follows.KEY] == ["none-0123456789ab"])
    app_settings.set_value(follows.KEY, ["ok-1", 5, "a b", "ok-1", None, "ok-2"])
    odd = follows.ids()
    app_settings.set_value(follows.KEY, "不是名单")
    broken = follows.ids()
    app_settings.set_value(follows.KEY, [f"p{n}" for n in range(follows.MAX)])
    full = follows.set_following("one-more", True)
    out["设置文件被改坏了：写得不对的那几项不算；满了（MAX 位）就不再加"] = (
        odd == ["ok-1", "ok-2"] and broken == [] and len(full) == follows.MAX and "one-more" not in full)
    out["开播通知：答了\"要\"是 True、\"不用\"是 False"] = (
        follows.set_notify(True) is True and follows.notify() is True and follows.set_notify(False) is False and follows.notify() is False)

    reset()
    serve(folder, [event("live", NOW - timedelta(hours=1), ["甲队", "乙队"]), event("soon", NOW + timedelta(hours=3), ["丙队"]),
                   event("later", NOW + timedelta(days=1), ["乙队"]), event("old", NOW - timedelta(days=1), ["甲队"])])
    data = esports_data.get()["data"]
    for pid in ("douyin-abc", "gone-1", "none-0123456789ab", "douyu-901"):
        follows.set_following(pid, True)
    found = follows.events(data, NOW)
    out["关注的人里现在还在名单上的（选手、另外加的主播）；只要开直播的时不算不直播的选手"] = (
        [p["id"] for p in follows.players(data)] == ["douyin-abc", "none-0123456789ab", "douyu-901"]
        and [p["id"] for p in follows.players(data, streaming_only=True)] == ["douyin-abc", "douyu-901"]
        and follows.players(None) == [])
    out["有关注的人参赛的赛事：只算正在进行、即将开始的，正在进行的在前；写着是哪几位（不直播的选手也算）"] = (
        [(e["id"], [p["id"] for p in mine]) for e, mine in found]
        == [("live", ["none-0123456789ab", "douyin-abc"]), ("later", ["none-0123456789ab", "douyin-abc"])]
        and follows.events(None) == [])


def check_checks(out, folder):
    reset()
    asked = []

    def query(players):
        asked.append([p["id"] for p in players])
        return {p["id"]: got("live", time.time() - 60, title="  深夜\n排位  ") for p in players}

    tracker = follow_alerts.Tracker()
    out["没打开开播通知、没关注人：不用在后台查"] = follow_alerts.wanted() is False
    follows.set_notify(True)
    nobody = follow_alerts.check(tracker, query=query)
    for pid in ("douyu-1", "none-0123456789ab", "gone-1"):
        follows.set_following(pid, True)
    out["打开了通知、关注了人：要查"] = follow_alerts.wanted() is True and nobody == [] and asked == []
    first = follow_alerts.check(tracker, query=query)
    out["只查关注的、开直播的、还在名单上的那几位；第一次只记下不提醒"] = asked == [["douyu-1"]] and first == []
    follows.set_following("bilibili-2", True)
    caught = follow_alerts.check(tracker, since_after=time.time() - 600, query=query)
    one = follow_alerts.text(caught)
    out["刚关了游戏的那一次：游戏开着时开播的补上；通知上写名字、平台和直播标题（标题里的换行、多余空格去掉）"] = (
        [(p["id"], found["state"]) for p, found in caught] == [("bilibili-2", "live")]
        and one == ("B站：深夜 排位\n打开 Evo 的电竞页可以看", "甲队2号 开播了"))
    many = follow_alerts.text([({"name": f"主播{n}", "platform": "douyu"}, got("live")) for n in range(6)])
    untitled = follow_alerts.text([({"name": "某某", "platform": "douyin"}, got("live"))])
    out["几位一起开播：写在一条通知里，超过 4 位写\"等 N 位\"；没有标题写\"正在某平台直播\""] = (
        many == ("主播0、主播1、主播2、主播3 等 6 位\n打开 Evo 的电竞页可以看", "关注的主播开播了")
        and untitled == ("正在抖音直播\n打开 Evo 的电竞页可以看", "某某 开播了"))
    gone = follow_alerts.check(tracker, query=query, get_data=lambda fresh=0: {"data": None})
    out["名单读不到：这次不查、不提醒"] = gone == []


def route_checks(out, folder, client):
    reset()
    token = panel_app.app.config["TOKEN"]

    def post(path, **fields):
        return client.post(path, data={"token": token, **fields})

    out["关注的接口要带面板的口令（别的网页不能替玩家关注）"] = (
        client.post("/esports/follow/douyu-1", data={"on": "1"}).status_code != 200 and follows.ids() == [])
    added = post("/esports/follow/douyu-1", on="1").get_json()
    unknown = post("/esports/follow/not-in-list", on="1").get_json()
    silent = post("/esports/follow/none-0123456789ab", on="1").get_json()
    out["关注：只能关注名单里有的人；回现在关注了谁、通知还没问过（页面据此问一次）"] = (
        added["ok"] is True and added["ids"] == ["douyu-1"] and added["notify"] is None
        and unknown["ok"] is False and unknown["ids"] == ["douyu-1"]
        and silent["ids"] == ["douyu-1", "none-0123456789ab"])
    out["回的数据里：有关注的人的赛事（先看的是开直播的那一位）、关注了但不直播的选手"] = (
        [(e["id"], e["names"], e["watch"], e["state"]) for e in silent["events"]]
        == [("live", ["甲队1号", "<b>不播的</b>"], "/esports/live/watch?p=douyu-1", "live"),
            ("later", ["<b>不播的</b>"], "/esports/later/watch", "upcoming")]
        and silent["idle"] == [{"id": "none-0123456789ab", "name": "<b>不播的</b>", "team": "乙队"}])
    follow_alerts.wake.clear()
    on = post("/esports/follow-notify", on="1").get_json()
    woke = follow_alerts.wake.is_set()
    off = post("/esports/follow-notify", on="0").get_json()
    out["开 / 关开播通知：记下来，并叫醒托盘里等着的线程"] = on["notify"] is True and off["notify"] is False and woke and follows.notify() is False
    form = post("/esports/follow-notify", on="1", next="/settings")
    out["设置页的开关：改完回设置页"] = form.status_code == 302 and form.headers["Location"].endswith("/settings") and follows.notify() is True
    gone = post("/esports/follow/douyu-1", on="0").get_json()
    stale = post("/esports/follow/gone-1", on="0").get_json()
    out["取消关注：名单里已经没有的人也能取消"] = gone["ids"] == ["none-0123456789ab"] and stale["ok"] is True

    post("/esports/follow/douyu-1", on="1")
    page = client.get("/esports").get_data(as_text=True)
    config = json.loads(re.search(r'<script type="application/json" id="follow-data">(.*?)</script>', page, re.S).group(1))
    stars = re.findall(r'<button type="button" class="fav-star es-st-star" data-follow="([^"]*)" data-name="([^"]*)"', page)
    out["电竞页：最上面有\"我关注的\"一块（开播通知的开关、关注的主播、有比赛的那几场由脚本画），排在\"正在进行\"前面"] = (
        'id="es-fav"' in page and page.find('id="es-fav"') < page.find('id="h-es-live"') and 'id="es-fav-notify"' in page
        and 'role="switch"' in page[page.find('id="es-fav-notify"') - 80:page.find('id="es-fav-notify"') + 80]
        and 'id="es-fav-events"' in page and "window.esRegroup" in page)
    out["电竞页：每张主播卡片旁边一个星星按钮（和卡片的链接并排，不在链接里面）；名字只当纯文字"] = (
        [pid for pid, _ in stars] == ["douyu-1", "bilibili-2", "douyin-abc", "huya-9", "douyu-901"]
        and "<b>不播的</b>" not in page.split('id="follow-data"')[0]
        and not re.search(r'<a class="card es-st"[^>]*>(?:(?!</a>).)*fav-star', page, re.S))
    out["给脚本的数据：关注了谁、通知开没开、两个接口的地址、面板的口令"] = (
        config["ids"] == ["none-0123456789ab", "douyu-1"] and config["notify"] is True and config["url"] == "/esports/follow/"
        and config["notify_url"] == "/esports/follow-notify" and config["token"] == token and config["max"] == follows.MAX)
    out["第一次关注时问\"开播时要不要通知\"的对话框：说清楚多久问一次平台、只问关注的、Apex 开着时不问"] = (
        'id="follow-dialog"' in page and "5</span> 分钟" in page and "只问你关注的主播" in page and "Apex 开着的时候不问也不提醒" in page
        and "cfg.notify === null && !asked" in page)

    serve(folder, [event("live", NOW - timedelta(hours=1), ["甲队", "乙队"],
                         main=[{"id": "bilibili-900", "name": "官方直播", "platform": "bilibili", "room": "900"}])])
    watch = client.get("/esports/live/watch").get_data(as_text=True)
    side = watch[watch.find('<aside class="watch-side"'):watch.find("</aside>")]
    rows = re.findall(r'<div class="watch-p-row" data-row="([^"]*)">(.*?)</div>', side, re.S)
    data = json.loads(re.search(r'<script type="application/json" id="watch-data">(.*?)</script>', watch, re.S).group(1))
    out["看直播页：名单每一行右边一个星星（不直播的选手也有，主舞台的直播间没有）；星星和选人的按钮并排"] = (
        [(pid, "fav-star watch-star" in row) for pid, row in rows]
        == [("bilibili-900", False), ("douyu-1", True), ("bilibili-2", True), ("none-0123456789ab", True), ("douyin-abc", True)]
        and all(row.count("<button") == (2 if "fav-star" in row else 1) and "</button>\n          <button" in row or "fav-star" not in row
                for _, row in rows))
    out["看直播页：名单最上面有\"我关注的\"一组（先藏着，脚本把关注的那几行抄上来）；选手那一行有\"关注\"按钮；数据里带着关注了谁"] = (
        side.find('id="watch-fav"') < side.find("data-main") and 'id="watch-fav" data-fav open hidden' in side
        and 'id="watch-follow"' in watch and "data-follow-text" in watch and "function renderFav()" in watch
        and data["follow"]["ids"] == ["none-0123456789ab", "douyu-1"])
    settings = client.get("/settings").get_data(as_text=True)
    out["设置页：有\"关注的主播开播时通知我\"的开关，写着关注了几位"] = (
        'action="/esports/follow-notify"' in settings and 'id="follow-notify-name"' in settings
        and re.search(r'现在关注了 <span class="num">2</span> 位', settings) is not None
        and 'aria-checked="true"\n                  aria-labelledby="follow-notify-name"' in settings)


def thread_checks(out):
    state = {"wanted": False, "game": False, "found": []}
    checks, notes, logs = [], [], []
    real_wanted, real_check = follow_alerts.wanted, follow_alerts.check

    def check(tracker, since_after=None):
        checks.append(since_after)
        if state["found"] == "boom":
            raise RuntimeError("示例：出错了")
        found, state["found"] = state["found"], []
        return found

    follow_alerts.wanted = lambda: state["wanted"]
    follow_alerts.check = check
    stop = threading.Event()
    try:
        evo_app.start_follow_checker(logs.append, lambda body, title: notes.append((body, title)), stop, lambda: state["game"],
                                     first_delay=0, every=0.04, game_wait=0.02)
        time.sleep(0.25)
        idle = len(checks)
        state["wanted"], state["game"] = True, True
        follow_alerts.wake.set()
        time.sleep(0.25)
        in_game = len(checks)
        started = time.time()
        state["found"] = [({"name": "甲队1号", "platform": "douyu"}, got("live", time.time(), title="排位"))]
        state["game"] = False
        time.sleep(0.25)
        out["没打开通知时不查；Apex 开着时不查也不弹"] = idle == 0 and in_game == 0
        out["关了游戏马上查一次（带着游戏是什么时候开着的），刚开播的用托盘通知提醒一次，并写进运行记录"] = (
            len(checks) >= 2 and checks[0] is not None and checks[0] <= started and all(c is None for c in checks[1:])
            and notes == [("斗鱼：排位\n打开 Evo 的电竞页可以看", "甲队1号 开播了")]
            and logs == ["关注：甲队1号 开播了，用托盘通知提醒了"])
        state["found"] = "boom"
        time.sleep(0.25)
        out["查的时候出了错：只记一笔（原因没变不重复记），线程不停，也不弹通知"] = (
            logs[1:] == ["关注：没查成谁在播（RuntimeError: 示例：出错了）"] and len(notes) == 1)
    finally:
        stop.set()
        follow_alerts.wake.set()
        follow_alerts.wanted, follow_alerts.check = real_wanted, real_check
        time.sleep(0.1)
        follow_alerts.wake.clear()


def main():
    print("电竞页的关注和开播通知")
    out = {}
    folder = tempfile.mkdtemp(prefix="evo-follow-")
    db = Path(folder) / "evo.db"
    storage.connect(db).close()
    panel_app.app.config["DB"] = str(db)
    client = panel_app.app.test_client()
    real_query = live_status.query
    live_status.query = lambda players, get=None: {p["id"]: got("offline") for p in players}
    try:
        tracker_checks(out)
        storage_checks(out, folder)
        check_checks(out, folder)
        route_checks(out, folder, client)
        thread_checks(out)
    finally:
        live_status.query = real_query
        reset()
    return report(out)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
