"""B站扫码登录和 B站原画（secret_store.py、qr.py、bili_login.py、watch_bilibili.py、panel_esports.py 的登录接口）。
不联网：B站的接口换成假的；登录信息存在临时数据文件夹里。

- 加密保存：读回来一样；文件里看不到原文；被改过、不是这里加密的读不出来（当作没有）；能删掉
- 二维码：B站登录那么长的网址（145 个字节）画得出来，用 OpenCV 的识别器扫回来一字不差
- 登录：要二维码（B站没给、给的不对说明原因）；问状态（没扫、扫了没确认、过期、别的错）；
  成功时只留登录凭证那几个 Cookie、核对昵称、加密存下来；核对不过不算登录
- 登录着没有：不联网直接看存着的；隔 6 小时向 B站核对一次，B站说没登录就删掉，连不上照旧算登录着
- 退出登录：带着 bili_jct 告诉 B站，再删掉本机的；B站那边没成也照样删
- B站取地址：只要 H.264，HLS（TS）和 FLV 都拿、优先 HLS，带 mcdn 的服务器排最后；实际给的是哪一档；
  没开播、接口报错说明原因；没选过清晰度要原画；带上登录凭证；刚取过的地址紧接着用一次；有 HLS 接 HLS、没有才接 FLV
- 面板：看直播页带着登录状态和登录的接口（不带登录凭证）；登录的接口要带口令；要二维码回画好的 SVG；
  没登录时不给 B站的播放地址（页面用官方播放器），登录了视频经过本机中转（2026-11-19 改的，原来是页面直接连）；
  页面里没播成时告诉后台记一笔

运行：python tests/test_bili_login.py
"""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import bili_login
import esports_data
import flv_relay
import hls_relay
import panel_app
import panel_esports
import qr
import secret_store
import storage
import watch_bilibili

SECRET = "SESSDATA=abc%2C1794900000%2Cdef*b1; bili_jct=csrf0123456789; DedeUserID=4321; DedeUserID__ckMd5=md5x; sid=s1d"
LOGIN_URL = ("https://account.bilibili.com/h5/account-h5/auth/scan-web?navhide=1&callback=close"
             "&qrcode_key=0123456789abcdef0123456789abcdef&from=main-fe-header")
KEY = "0123456789abcdef0123456789abcdef"


def store_checks(out):
    folder = Path(tempfile.mkdtemp(prefix="evo-secret-"))
    path = folder / "sub" / "login.bin"
    value = {"cookie": SECRET, "name": "示例用户", "saved": 1794900000.5}
    secret_store.save(path, value)
    raw = path.read_bytes()
    out["加密保存：读回来一样；文件里看不到原文"] = (
        secret_store.load(path) == value and b"SESSDATA" not in raw and b"csrf0123456789" not in raw
        and "示例用户".encode("utf-8") not in raw and not list(folder.glob("**/*.tmp")))
    path.write_bytes(raw[:-4] + b"\x00\x01\x02\x03")
    tampered = secret_store.load(path)
    path.write_bytes(b"not encrypted at all")
    plain = secret_store.load(path)
    out["被改过、不是这里加密的：读不出来，当作没有，不报错"] = tampered is None and plain is None and secret_store.load(folder / "没有.bin") is None
    secret_store.forget(path)
    secret_store.forget(path)
    out["能删掉；已经没有了再删也不报错"] = not path.exists()


def qr_checks(out):
    text = LOGIN_URL + "x" * (145 - len(LOGIN_URL.encode("utf-8")))
    cells = qr.matrix(text)
    image = np.full(((len(cells) + 8) * 8, (len(cells) + 8) * 8), 255, np.uint8)
    for r, row in enumerate(cells):
        for c, dark in enumerate(row):
            if dark:
                image[(r + 4) * 8:(r + 5) * 8, (c + 4) * 8:(c + 5) * 8] = 0
    read, _, _ = cv2.QRCodeDetector().detectAndDecode(image)
    svg = qr.svg(text)
    out["B站登录那么长的网址（145 个字节）画得出二维码，扫回来一字不差；SVG 是白底黑块"] = (
        len(text.encode("utf-8")) == 145 and read == text and svg.startswith("<svg") and 'fill="#fff"' in svg
        and "<script" not in svg)


class FakeBili:
    """假的 B站接口：照网址回事先摆好的东西，记下每次带了什么。"""

    def __init__(self):
        self.calls, self.replies = [], {}

    def __call__(self, url, cookie="", form=None):
        self.calls.append((url, cookie, form))
        for part, reply in self.replies.items():
            if part in url:
                if isinstance(reply, Exception):
                    raise reply
                return reply
        raise AssertionError(f"没想到会问 {url}")


def login_checks(out):
    bili_login.forget()
    bili = FakeBili()
    bili.replies["qrcode/generate"] = ({"code": 0, "data": {"url": LOGIN_URL, "qrcode_key": KEY}}, {})
    found = bili_login.start(bili)
    problems = 0
    for bad in ({"code": -1}, {"code": 0, "data": {"url": "http://x", "qrcode_key": KEY}},
                {"code": 0, "data": {"url": LOGIN_URL, "qrcode_key": "../x"}}, {"code": 0, "data": None}):
        bili.replies["qrcode/generate"] = (bad, {})
        try:
            bili_login.start(bili)
        except bili_login.BiliError:
            problems += 1
    out["要二维码：回二维码的内容和问状态用的 key；B站没给、给的不像样说明原因"] = (
        found == {"key": KEY, "url": LOGIN_URL} and problems == 4)

    states = []
    for code in (86101, 86090, 86038, 12345):
        bili.replies["qrcode/poll"] = ({"code": 0, "data": {"code": code}}, {})
        states.append(bili_login.poll(KEY, bili)["state"])
    bili.replies["qrcode/poll"] = ({"code": -412}, {})
    states.append(bili_login.poll(KEY, bili)["state"])
    count = len(bili.calls)
    states.append(bili_login.poll("not-a-key", bili)["state"])
    out["问状态：没扫、扫了没确认、过期，别的都算出错；key 写得不对不去问 B站；这些时候不存任何东西"] = (
        states == ["waiting", "scanned", "expired", "error", "error", "error"] and len(bili.calls) == count
        and bili_login.account() == {"logged_in": False, "name": ""} and not bili_login.STORE.exists()
        and f"&qrcode_key={KEY}" in bili.calls[-1][0])

    given = {"SESSDATA": "abc%2C1794900000%2Cdef*b1", "bili_jct": "csrf0123456789", "DedeUserID": "4321",
             "DedeUserID__ckMd5": "md5x", "sid": "s1d", "buvid3": "不要这个", "b_nut": "也不要"}
    bili.replies["qrcode/poll"] = ({"code": 0, "data": {"code": 0, "url": "https://passport.biligame.com/x?DedeUserID=4321"}}, given)
    bili.replies["web-interface/nav"] = ({"code": 0, "data": {"isLogin": True, "uname": "示例用户"}}, {})
    done = bili_login.poll(KEY, bili, now=1000.0)
    raw = bili_login.STORE.read_bytes()
    out["扫码成功：只留登录凭证那几个 Cookie，带着它核对昵称，加密存在数据库旁边；回给页面的只有状态和昵称"] = (
        done == {"state": "ok", "name": "示例用户"} and bili_login.cookie() == SECRET and bili.calls[-1][1] == SECRET
        and bili_login.STORE.parent == storage.DB_PATH.parent and b"SESSDATA" not in raw and b"csrf0123456789" not in raw
        and bili_login.account() == {"logged_in": True, "name": "示例用户"})

    bili_login.forget()
    bili.replies["web-interface/nav"] = ({"code": -101, "message": "账号未登录", "data": {"isLogin": False}}, {})
    refused = bili_login.poll(KEY, bili)
    bili.replies["qrcode/poll"] = ({"code": 0, "data": {"code": 0, "url": ""}}, {"DedeUserID": "4321"})
    partial = bili_login.poll(KEY, bili)
    out["B站说登录好了、但核对不过（或者凭证不全）：不算登录，不存"] = (
        refused["state"] == "error" and partial["state"] == "error" and not bili_login.STORE.exists())

    secret_store.save(bili_login.STORE, {"cookie": SECRET, "name": "示例用户", "saved": 1000.0, "checked": 1000.0})
    count = len(bili.calls)
    quiet = bili_login.account(check=True, ask=bili, now=1000.0 + 3600)
    out["登录着没有：直接看存着的；6 小时内核对过就不再问 B站"] = (
        quiet == {"logged_in": True, "name": "示例用户"} and len(bili.calls) == count)
    bili.replies["web-interface/nav"] = bili_login.BiliError("连不上 B站（超时）")
    offline = bili_login.account(check=True, ask=bili, now=1000.0 + 7 * 3600)
    bili.replies["web-interface/nav"] = ({"code": 0, "data": {"isLogin": True, "uname": "改了名"}}, {})
    renamed = bili_login.account(check=True, ask=bili, now=1000.0 + 7 * 3600)
    after = len(bili.calls)
    again = bili_login.account(check=True, ask=bili, now=1000.0 + 8 * 3600)
    out["隔了 6 小时核对一次：连不上照旧算登录着；连上了更新昵称，并记下核对过了"] = (
        offline == {"logged_in": True, "name": "示例用户"} and renamed == {"logged_in": True, "name": "改了名"}
        and again == renamed and len(bili.calls) == after and bili_login.cookie() == SECRET)
    bili.replies["web-interface/nav"] = ({"code": -101, "message": "账号未登录"}, {})
    expired = bili_login.account(check=True, ask=bili, now=1000.0 + 20 * 3600)
    out["B站说没登录了（过期、在别处退出了）：删掉存着的，回到没登录"] = (
        expired == {"logged_in": False, "name": ""} and not bili_login.STORE.exists() and bili_login.cookie() == "")

    secret_store.save(bili_login.STORE, {"cookie": SECRET, "name": "示例用户", "saved": 1.0, "checked": 1.0})
    bili.replies["login/exit/v2"] = ({"code": 0}, {})
    told = bili_login.logout(bili)
    last = bili.calls[-1]
    secret_store.save(bili_login.STORE, {"cookie": SECRET, "name": "示例用户", "saved": 1.0, "checked": 1.0})
    bili.replies["login/exit/v2"] = bili_login.BiliError("连不上 B站")
    failed = bili_login.logout(bili)
    count = len(bili.calls)
    nothing = bili_login.logout(bili)
    out["退出登录：带着登录凭证和里面的 bili_jct 告诉 B站，再删掉本机的；B站那边没成也照样删；本来没登录就不去问"] = (
        told is True and last[1] == SECRET and last[2] == {"biliCSRF": "csrf0123456789"} and failed is False
        and not bili_login.STORE.exists() and nothing is False and len(bili.calls) == count)


def play_reply(current=250, status=1, code=0):
    def codec(name, fmt):
        return {"codec_name": name, "current_qn": current, "accept_qn": [10000, 400, 250, 150, 250],
                "base_url": f"/live-bvc/1/live_x.{fmt}?", "url_info": [
                    {"host": "https://xy1x2x3x4xy.mcdn.bilivideo.cn:486", "extra": "sign=mcdn"},
                    {"host": "http://insecure.bilivideo.com", "extra": "sign=0"},
                    {"host": "https://a.bilivideo.com", "extra": f"expires=1&sign=aaa&codec={name}"},
                    {"host": "https://b.bilivideo.com", "extra": f"expires=1&sign=bbb&codec={name}"}]}
    return {"code": code, "message": "0", "data": {"room_id": 7734200, "live_status": status, "playurl_info": {"playurl": {
        "g_qn_desc": [{"qn": 10000, "desc": "原画"}, {"qn": 400, "desc": "蓝光"}, {"qn": 250, "desc": "超清"}, {"qn": 150, "desc": "高清"}],
        "stream": [{"protocol_name": "http_hls", "format": [{"format_name": "ts", "codec": [codec("avc", "m3u8")]}]},
                   {"protocol_name": "http_stream", "format": [{"format_name": "flv", "codec": [codec("hevc", "flv"), codec("avc", "flv")]}]}]}}}}


def play_checks(out):
    found = watch_bilibili.parse(play_reply())
    hosts = ["https://a.bilivideo.com/live-bvc/1/live_x.{}?expires=1&sign=aaa&codec=avc",
             "https://b.bilivideo.com/live-bvc/1/live_x.{}?expires=1&sign=bbb&codec=avc",
             "https://xy1x2x3x4xy.mcdn.bilivideo.cn:486/live-bvc/1/live_x.{}?sign=mcdn"]
    out["B站取地址：只要 H.264，HLS（TS）和 FLV 的地址都拿、页面按 TS 流放；只要 https 的主机，名字带 mcdn 的排最后"] = (
        found["hls"] == [h.format("m3u8") for h in hosts] and found["flv"] == [h.format("flv") for h in hosts]
        and found["kind"] == "mpegts" and found["ttl"] is None)
    out["清晰度按 B站给的顺序、带名字；实际给的是回包里的那一档"] = (
        found["rate"] == "250" and found["qualities"] == [{"id": "10000", "name": "原画"}, {"id": "400", "name": "蓝光"},
                                                           {"id": "250", "name": "超清"}, {"id": "150", "name": "高清"}])
    no_hls = play_reply()
    no_hls["data"]["playurl_info"]["playurl"]["stream"].pop(0)
    only_flv = watch_bilibili.parse(no_hls)
    out["这个房间没有 HLS：用 FLV（页面按 FLV 放）"] = (
        only_flv["hls"] == [] and only_flv["flv"] == found["flv"] and only_flv["kind"] == "flv")
    reasons = []
    nothing = play_reply()
    nothing["data"]["playurl_info"]["playurl"]["stream"] = [
        {"protocol_name": "http_stream", "format": [{"format_name": "flv", "codec": [
            {"codec_name": "hevc", "current_qn": 250, "accept_qn": [250], "base_url": "/x.flv?",
             "url_info": [{"host": "https://a.bilivideo.com", "extra": "s=1"}]}]}]},
        {"protocol_name": "http_hls", "format": [{"format_name": "fmp4", "codec": [
            {"codec_name": "avc", "current_qn": 250, "accept_qn": [250], "base_url": "/x.m3u8?",
             "url_info": [{"host": "https://a.bilivideo.com", "extra": "s=1"}]}]}]}]
    for reply in (play_reply(status=0), play_reply(status=2), play_reply(code=-412), nothing, None):
        try:
            watch_bilibili.parse(reply)
        except watch_bilibili.BiliPlayError as e:
            reasons.append(str(e))
    out["没开播、在轮播、接口报错、只有 H.265 或 fMP4：说明原因，不给地址"] = (
        len(reasons) == 5 and "没开播" in reasons[0] and "没开播" in reasons[1] and "-412" in reasons[2] and "H.264" in reasons[3])
    asked = []

    def ask(url, cookie="", form=None):
        asked.append((url, cookie))
        return play_reply(10000), {}

    best = watch_bilibili.resolve("6", None, ask, cookie=SECRET)
    watch_bilibili.resolve("6", "250", ask, cookie="")
    watch_bilibili.resolve("6", "-1", ask, cookie="")
    out["没选过清晰度要原画；选了就按选的要；HLS 和 FLV 都要；向 B站要地址时带上登录凭证，取到的地址里没有它"] = (
        best["rate"] == "10000" and asked[0][0].endswith("&room_id=6&qn=10000") and asked[0][1] == SECRET
        and "protocol=0,1" in asked[0][0] and "format=0,1" in asked[0][0] and "codec=0&" in asked[0][0]
        and asked[1][0].endswith("&qn=250") and asked[2][0].endswith("&qn=10000")
        and "SESSDATA" not in json.dumps(best) and "csrf0123456789" not in json.dumps(best)
        and not getattr(watch_bilibili, "DIRECT", False))
    watch_bilibili.remember("6", "10000", best, now=100)
    again = watch_bilibili.resolve("6", None, ask, cookie="", reuse=True, now=110)
    count = len(asked)
    later = watch_bilibili.resolve("6", None, ask, cookie="", reuse=True, now=111)
    watch_bilibili.remember("6", "10000", best, now=100)
    stale = watch_bilibili.resolve("6", None, ask, cookie="", reuse=True, now=100 + watch_bilibili.REUSE_SECONDS + 1)
    out["页面刚问过的地址，紧接着开播时直接用一次（不再问 B站），过了 20 秒就重新问"] = (
        again is best and count == 3 and later is not best and len(asked) == 5 and stale is not best)
    watch_bilibili.remember("6", None, best)
    relay = watch_bilibili.open_relay("6")
    watch_bilibili.remember("7", None, only_flv)
    flv = watch_bilibili.open_relay("7")
    out["中转：有 HLS 接 HLS（接成 TS 流），没有才接 FLV；开头用刚取到的地址，不再问一遍"] = (
        isinstance(relay, hls_relay.HlsRelay) and relay.mimetype == "video/mp2t"
        and isinstance(flv, flv_relay.Relay) and flv.mimetype == "video/x-flv" and len(asked) == 5)
    request = watch_bilibili._request("https://a.bilivideo.com/x.ts")
    out["向 B站的视频服务器取视频时带 B站自己网页那样的来源页（有的服务器不带就拒绝），不带登录凭证"] = (
        request.get_header("Referer") == "https://live.bilibili.com/" and request.get_header("Cookie") is None)


class FakeLogin:
    def __init__(self):
        self.state, self.calls = {"logged_in": False, "name": ""}, []

    def account(self, check=False):
        return dict(self.state)

    def start(self):
        self.calls.append("start")
        return {"key": KEY, "url": LOGIN_URL}

    def poll(self, key):
        self.calls.append(("poll", key))
        return {"state": "ok" if key == KEY else "waiting", "name": "示例用户" if key == KEY else ""}

    def logout(self):
        self.calls.append("logout")
        self.state = {"logged_in": False, "name": ""}
        return True


class FakeRelay:
    mimetype = "video/mp2t"

    def __init__(self):
        self.started = self.closed = False

    def start(self):
        self.started = True

    def stream(self):
        yield b"TS-DATA"

    def close(self):
        self.closed = True


class FakeBiliPlay:
    """假的"B站取地址"：经过本机中转的那一种（有 open_relay）。"""

    def __init__(self):
        self.asked, self.relays, self.fail = [], [], None

    def resolve(self, room, rate=None):
        self.asked.append((room, rate))
        return {"hls": ["https://a.bilivideo.com/live.m3u8?sign=aaa"], "flv": [], "kind": "mpegts", "ttl": None,
                "rate": "10000" if rate in (None, "10000") else str(rate),
                "qualities": [{"id": "10000", "name": "原画"}, {"id": "250", "name": "超清"}]}

    def remember(self, room, rate, found):
        self.asked.append(("remember", room, rate))

    def open_relay(self, room, rate=None, on_event=None):
        if self.fail:
            raise watch_bilibili.BiliPlayError(self.fail)
        self.relays.append((room, rate, FakeRelay()))
        return self.relays[-1][2]


def panel_checks(out):
    folder = tempfile.mkdtemp(prefix="evo-bili-")
    now = datetime.now().astimezone()
    path = Path(folder) / "esports.json"
    path.write_text(json.dumps({"version": 1, "updated": now.isoformat(), "platforms": {},
                                "players": [{"id": "bilibili-6", "name": "Sol", "team": "甲队", "platform": "bilibili", "room": "6"},
                                            {"id": "douyu-9", "name": "Kite", "team": "甲队", "platform": "douyu", "room": "9"}],
                                "events": [{"id": "live", "name": "联赛", "type": "联赛", "stage": "",
                                            "start": (now - timedelta(hours=1)).isoformat(),
                                            "end": (now + timedelta(hours=3)).isoformat(), "teams": ["甲队"]}]}), encoding="utf-8")
    esports_data.URL = str(path)
    esports_data.CACHE_PATH.unlink(missing_ok=True)
    db = Path(folder) / "evo.db"
    storage.connect(db).close()
    panel_app.app.config["DB"] = str(db)
    client = panel_app.app.test_client()
    token = panel_app.app.config["TOKEN"]
    login, player = FakeLogin(), FakeBiliPlay()
    real_logins, real_players = panel_esports.LOGINS, panel_esports.PLAYERS
    panel_esports.LOGINS, panel_esports.PLAYERS = {"bilibili": login}, {**real_players, "bilibili": player}
    try:
        page = client.get("/esports/live/watch").get_data(as_text=True)
        data = json.loads(page.split('id="watch-data">')[1].split("</script>")[0])
        out["看直播页：带着登录状态、登录的三个接口和口令；有登录按钮和扫码的弹窗；不带任何登录凭证"] = (
            data["accounts"] == {"bilibili": {"logged_in": False, "name": ""}}
            and data["login"] == {"bilibili": {"start": "/esports/login/bilibili/start", "poll": "/esports/login/bilibili/poll",
                                               "logout": "/esports/login/bilibili/logout"}}
            and data["token"] == token and 'id="watch-login"' in page and 'id="login-dialog"' in page
            and "SESSDATA" not in page and {p["id"]: bool(p["play"]) for p in data["players"]} == {"bilibili-6": True, "douyu-9": True})

        refused = [client.post(url).status_code for url in ("/esports/login/bilibili/start", "/esports/login/bilibili/poll",
                                                            "/esports/login/bilibili/logout")]
        unknown = client.post("/esports/login/douyu/start", data={"token": token}).status_code
        out["登录的接口要带面板的口令（别的网页不能替你登录、退出）；不支持登录的平台 404"] = (
            refused == [403, 403, 403] and login.calls == [] and unknown == 404)
        started = client.post("/esports/login/bilibili/start", data={"token": token}).get_json()
        out["要二维码：回 key 和画好的二维码（SVG），不把二维码的内容交给别的网站去画"] = (
            started["ok"] and started["key"] == KEY and started["svg"] == qr.svg(LOGIN_URL) and login.calls == ["start"])
        waiting = client.post("/esports/login/bilibili/poll", data={"token": token, "key": "x"}).get_json()
        done = client.post("/esports/login/bilibili/poll", data={"token": token, "key": KEY}).get_json()
        out["问扫了没有：回给页面的只有状态和昵称"] = (
            waiting == {"state": "waiting", "name": ""} and done == {"state": "ok", "name": "示例用户"})

        guest = client.get("/esports/play/bilibili-6")
        out["没登录：不给 B站的播放地址，也不去问 B站（页面用 B站官方播放器）"] = guest.status_code == 404 and player.asked == []
        login.state = {"logged_in": True, "name": "示例用户"}
        info = client.get("/esports/play/bilibili-6").get_json()
        chosen = client.get("/esports/play/bilibili-6?q=250").get_json()
        out["登录了：给清晰度和本机中转的地址（带口令、带实际那一档），告诉页面按 TS 流放；没选过要原画，选了按选的要"] = (
            info == {"ok": True, "applied": "10000", "kind": "mpegts", "stream": f"/esports/stream/bilibili-6?q=10000&t={token}",
                     "qualities": [{"id": "10000", "name": "原画"}, {"id": "250", "name": "超清"}]}
            and chosen["applied"] == "250"
            and player.asked == [("6", None), ("remember", "6", "10000"), ("6", "250"), ("remember", "6", "250")])
        refused = client.get("/esports/stream/bilibili-6?q=10000&t=wrong").status_code
        relay = client.get(f"/esports/stream/bilibili-6?q=10000&t={token}")
        out["中转：要带口令；回的是 B站那个模块接好的流（TS），不把登录凭证或 B站的地址给页面"] = (
            refused == 403 and relay.status_code == 200 and relay.mimetype == "video/mp2t" and relay.data == b"TS-DATA"
            and [(room, rate) for room, rate, _ in player.relays] == [("6", "10000")] and player.relays[0][2].started
            and "bilivideo" not in json.dumps(info) and "SESSDATA" not in json.dumps(info))
        player.fail = "B站的取流接口回了 code=-352（风控校验失败）"
        broken = client.get(f"/esports/stream/bilibili-6?q=10000&t={token}")
        player.fail = None
        out["中转时没取到地址：回 502 和原因（页面退回官方播放器并说明）"] = (
            broken.status_code == 502 and broken.get_json() == {"ok": False, "reason": "upstream", "detail": "B站的取流接口回了 code=-352（风控校验失败）"})
        told = client.post("/esports/play-failed/bilibili-6", data={"token": token, "reason": "HttpStatusCodeInvalid\n" + "x" * 500})
        out["页面里没播成：告诉后台记一笔（要带口令；名单里没有的人也不出错）"] = (
            told.get_json() == {"ok": True} and client.post("/esports/play-failed/bilibili-6", data={"reason": "x"}).status_code == 403
            and client.post("/esports/play-failed/nobody", data={"token": token, "reason": "x"}).get_json() == {"ok": True}
            and data["failed_url"] == "/esports/play-failed/")
        page = client.get("/esports/live/watch").get_data(as_text=True)
        data = json.loads(page.split('id="watch-data">')[1].split("</script>")[0])
        out["登录以后看直播页知道登录着、叫什么"] = data["accounts"] == {"bilibili": {"logged_in": True, "name": "示例用户"}}
        left = client.post("/esports/login/bilibili/logout", data={"token": token}).get_json()
        out["退出登录：回到没登录，又不给 B站的播放地址了"] = (
            left == {"ok": True} and login.calls[-1] == "logout" and client.get("/esports/play/bilibili-6").status_code == 404)
    finally:
        panel_esports.LOGINS, panel_esports.PLAYERS = real_logins, real_players


def main():
    print("B站扫码登录和 B站原画（加密保存、二维码、登录、取地址、面板的登录接口）")
    out = {}
    store_checks(out)
    qr_checks(out)
    login_checks(out)
    play_checks(out)
    panel_checks(out)
    bili_login.forget()
    return report(out)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
