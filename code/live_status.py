"""查直播间现在在不在播（斗鱼、虎牙、B站、抖音的公开网页接口，不登录）：赛事页、看直播页和 tools/esports.py --check 用。

只查交给它的房间（esports.json 名单里的），只在页面开着或开发者运行 --check 时查；同时最多 CONFIG["parallel"] 个请求
（用户定的：要克制），B站一次查多个房间。请求和浏览器打开直播间时发的一样（带浏览器的 User-Agent，不带 Evo 的版本号、
不发送任何战绩或电脑信息）；抖音给的游客 Cookie（ttwid）只留在内存里，不存盘，不登录任何平台账号。
接口地址、请求头都在 config/watch.json（平台改了接口先改那里）；怎么查是照 DTV（https://github.com/chen-zeong/DTV ）
的流程写的，每个平台回什么见 watch.json 里的说明。

每个房间的结果：{"state": "live" 在播 / "offline" 没开播 / "missing" 房间不存在 / "error" 没查到,
                "loop": 是不是在轮播（算没开播）, "name": 平台上的主播名, "title": 直播标题, "avatar": 头像地址,
                "since": 开播时刻（time.time() 那种秒数，不知道是 None）, "error": 没查到的原因}
房间号只接受数字：esports.json 被人改了也只能让 Evo 去问这几个平台的直播间，不能让它连别的地址。

用法：python code/live_status.py 斗鱼 9999 B站 6   → 查这几个房间并打印
"""

import http.cookiejar
import json
import re
import secrets
import string
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import abogus
from paths import CONFIG_DIR

CONFIG = json.loads((CONFIG_DIR / "watch.json").read_text(encoding="utf-8"))
PLATFORMS = CONFIG["platforms"]
NAMES = {key: platform["name"] for key, platform in PLATFORMS.items()}  # {"douyu": "斗鱼", ...}
LIVE, OFFLINE, MISSING, ERROR = "live", "offline", "missing", "error"
BEIJING = timezone(timedelta(hours=8))
ROOM_RE = re.compile(r"\d{1,24}")
DOUYIN_ROOM_ID = re.compile(rb'\\"roomId\\":\\"(\d{10,})\\"|"roomId":"(\d{10,})"|\\"room_id\\":\\"(\d{10,})\\"')
MAX_BYTES = 1_000_000
CHUNK = 64 * 1024

_cookies = http.cookiejar.CookieJar()  # 只给抖音用：游客 Cookie，只在内存里
_cookie_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookies))


class StatusError(Exception):
    """这次没查到（连不上、被拦、回的格式不认识）。status：HTTP 状态码（没连上是 None）。"""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def room_url(platform, room):
    """这个直播间的网页地址（给"去 X 看"用）；平台、房间号不对返回 None。"""
    if platform not in PLATFORMS or not ROOM_RE.fullmatch(str(room)):
        return None
    return PLATFORMS[platform]["room_url"].format(room=room)


def result(state, name="", title="", avatar="", since=None, loop=False, error=""):
    since = since if isinstance(since, (int, float)) and since > 0 else None
    return {"state": state, "loop": bool(loop), "name": str(name or ""), "title": str(title or ""),
            "avatar": str(avatar or ""), "since": since, "error": error}


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _dict(value):
    return value if isinstance(value, dict) else {}


def _json(body, who):
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise StatusError(f"{who}回的不是房间数据")
    return data


# ---------- 联网 ----------

def fetch(url, headers, limit=MAX_BYTES, until=None, cookies=False):
    """下载一个地址，最多读 limit 字节；给了 until（正则）时读到它出现就停（抖音的直播间网页有 1 MB，
    要的房间号在前面）。连不上、状态码不是 200 抛 StatusError。测试时整个换掉。"""
    request = urllib.request.Request(url, headers=headers)
    opener = _cookie_opener.open if cookies else urllib.request.urlopen
    try:
        with opener(request, timeout=CONFIG["timeout"]) as response:
            body = b""
            while len(body) < limit:
                chunk = response.read(min(CHUNK, limit - len(body)))
                if not chunk:
                    break
                tail = len(body) - 200  # 要找的字可能正好跨在两块中间
                body += chunk
                if until is not None and until.search(body, max(0, tail)):
                    break
            return body
    except urllib.error.HTTPError as e:
        raise StatusError(f"HTTP {e.code}", e.code) from e
    except OSError as e:
        raise StatusError(f"连不上（{e}）") from e


def _headers(platform, room=None):
    cfg = PLATFORMS[platform]
    headers = {"User-Agent": cfg["user_agent"], "Accept": "application/json, text/plain, */*",
               "Accept-Language": "zh-CN,zh;q=0.9",
               "Referer": cfg.get("referer") or cfg["room_url"].format(room=room)}
    if cfg.get("origin"):
        headers["Origin"] = cfg["origin"]
    return headers


# ---------- 每个平台：读回包（不联网，测试直接测这些） ----------

def parse_douyu(body):
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        if PLATFORMS["douyu"]["missing_text"] in body.decode("utf-8", "replace"):
            return result(MISSING)
        raise StatusError("斗鱼回的不是房间数据") from None
    # 不同房间外面包的层数不一样（DTV 实测）：{data: {room}} / {data} / {room} / 直接就是房间
    outer = _dict(data)
    room = _dict(outer.get("data")).get("room") or outer.get("data") or outer.get("room") or outer
    room = _dict(room)
    if not room.get("room_id"):
        raise StatusError("斗鱼回的数据里没有房间")
    loop = _int(room.get("videoLoop")) == 1 or _int(room.get("isLoop")) == 1
    live = _int(room.get("show_status")) == 1 and not loop
    return result(LIVE if live else OFFLINE, loop=loop and _int(room.get("show_status")) == 1,
                  name=room.get("nickname") or room.get("owner_name"), title=room.get("room_name"),
                  avatar=room.get("avatar_mid") or _dict(room.get("avatar")).get("middle"),
                  since=_int(room.get("show_time")) if live else None)


def parse_huya(body):
    data = _json(body, "虎牙")
    status = _int(data.get("status"))
    if status == 422 or "不存在" in str(data.get("message") or ""):
        return result(MISSING)
    info = _dict(data.get("data"))
    if status != 200 or not info:
        raise StatusError(f"虎牙回了 status={data.get('status')}")
    profile, live_data = _dict(info.get("profileInfo")), _dict(info.get("liveData"))
    state = str(info.get("liveStatus") or "").upper()
    return result(LIVE if state == "ON" else OFFLINE, loop=state == "REPLAY",
                  name=profile.get("nick") or live_data.get("nick"),
                  title=live_data.get("introduction") or live_data.get("roomName"),
                  avatar=profile.get("avatar180") or live_data.get("avatar180"),
                  since=_int(live_data.get("startTime")) if state == "ON" else None)


def parse_bilibili(body, rooms):
    """一次查多个的回包 → {房间号: 结果}。表里写短号、真实房间号都认。"""
    data = _json(body, "B站")
    if _int(data.get("code")) != 0:
        raise StatusError(f"B站回了 code={data.get('code')}（{data.get('message')}）")
    found = {}
    for key, item in _dict(_dict(data.get("data")).get("by_room_ids")).items():
        if isinstance(item, dict):
            found[str(key)] = item
            if _int(item.get("short_id")):
                found[str(_int(item["short_id"]))] = item
    out = {}
    for room in rooms:
        item = found.get(str(room))
        if item is None:
            out[room] = result(MISSING)
            continue
        status = _int(item.get("live_status"))
        since = None
        if status == 1:
            try:
                since = datetime.strptime(str(item.get("live_time")), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=BEIJING).timestamp()
            except ValueError:
                since = None
        out[room] = result(LIVE if status == 1 else OFFLINE, loop=status == 2, name=item.get("uname"),
                           title=item.get("title"), since=since)
    return out


def parse_douyin_page(body):
    """抖音直播间网页 → 内部房间号（每次开播都换，查状态要用它）；页面里没有返回 None，被拦抛 StatusError。"""
    found = DOUYIN_ROOM_ID.search(body)
    if found:
        return next(group for group in found.groups() if group).decode("ascii")
    text = body.decode("utf-8", "replace")
    if any(mark in text for mark in PLATFORMS["douyin"]["blocked_text"]):
        raise StatusError("被抖音拦住了（要验证码），过一会儿再试")
    return None


def parse_douyin_room(body):
    data = _json(body, "抖音")
    room = _dict(_dict(data.get("data")).get("room"))
    if not room:
        raise StatusError(f"抖音没给房间数据（status_code={data.get('status_code')}）")
    owner = _dict(room.get("owner"))
    avatars = _dict(owner.get("avatar_thumb")).get("url_list")
    live = _int(room.get("status")) == 2
    return result(LIVE if live else OFFLINE, name=owner.get("nickname"), title=room.get("title"),
                  avatar=avatars[0] if isinstance(avatars, list) and avatars else "",
                  since=_int(room.get("create_time")) if live else None)


# ---------- 每个平台：查（rooms → {房间号: 结果}） ----------

def check_douyu(rooms, get):
    out = {}
    for room in rooms:
        try:
            body = get(PLATFORMS["douyu"]["info_url"].format(room=room), _headers("douyu", room))
        except StatusError as e:
            if e.status != 404:
                raise
            out[room] = result(MISSING)
        else:
            out[room] = parse_douyu(body)
    return out


def check_huya(rooms, get):
    return {room: parse_huya(get(PLATFORMS["huya"]["info_url"].format(room=room), _headers("huya", room)))
            for room in rooms}


def check_bilibili(rooms, get):
    url = PLATFORMS["bilibili"]["batch_url"] + "".join(f"&room_ids={room}" for room in rooms)
    return parse_bilibili(get(url, _headers("bilibili")), rooms)


def check_douyin(rooms, get):
    cfg, out = PLATFORMS["douyin"], {}
    for room in rooms:
        page_headers = {**_headers("douyin"), "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
        page = get(cfg["page_url"].format(room=room), page_headers, limit=cfg["page_max_bytes"],
                   until=DOUYIN_ROOM_ID, cookies=True)
        inner = parse_douyin_page(page)
        if inner is None:
            out[room] = result(MISSING)
            continue
        token = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(107))
        params = [*cfg["info_params"], ("room_id", inner), ("msToken", token)]
        url = abogus.signed_url(cfg["info_url"], params, cfg["user_agent"])
        out[room] = parse_douyin_room(get(url, _headers("douyin"), cookies=True))
    return out


CHECKERS = {"douyu": check_douyu, "huya": check_huya, "bilibili": check_bilibili, "douyin": check_douyin}


def query(players, get=None):
    """查一批选手的直播间。players：[{"id", "platform", "room"}, ...]（esports.json 里的选手就是这样）。
    返回 {选手 id: 结果}。同一个房间只查一次；某个平台出错只影响它自己的房间（state 是 "error"）。
    get：测试时换掉联网的那个函数。"""
    get = get or fetch
    wanted = {}  # (平台, 房间号) → 结果
    for player in players:
        key = (player.get("platform"), str(player.get("room")))
        if key[0] not in CHECKERS or not ROOM_RE.fullmatch(key[1]):
            wanted[key] = result(ERROR, error="平台或房间号写得不对")
        else:
            wanted.setdefault(key, None)
    todo = sorted(key for key, value in wanted.items() if value is None)
    size = max(1, _int(PLATFORMS["bilibili"].get("batch_size")) or 30)
    bili = [room for platform, room in todo if platform == "bilibili"]
    jobs = [("bilibili", bili[i:i + size]) for i in range(0, len(bili), size)]
    jobs += [(platform, [room]) for platform, room in todo if platform != "bilibili"]

    def run(job):
        platform, rooms = job
        try:
            return CHECKERS[platform](rooms, get)
        except StatusError as e:
            return {room: result(ERROR, error=str(e)) for room in rooms}
        except Exception as e:  # noqa: BLE001  平台回了没见过的东西：只当这次没查到，不让页面出错
            return {room: result(ERROR, error=f"{type(e).__name__}: {e}") for room in rooms}

    if jobs:
        with ThreadPoolExecutor(max_workers=max(1, _int(CONFIG.get("parallel")) or 4)) as pool:
            for (platform, rooms), found in zip(jobs, pool.map(run, jobs)):
                for room in rooms:
                    wanted[(platform, room)] = found.get(room) or result(ERROR, error="没有查到")
    return {player.get("id"): wanted[(player.get("platform"), str(player.get("room")))] for player in players}


def describe(found):
    """一个结果写成一句话（--check 和下面的命令行用）。"""
    if found["state"] == LIVE:
        minutes = f"，开播 {int((time.time() - found['since']) // 60)} 分钟" if found["since"] else ""
        return f"在播{minutes}：{found['name']}「{found['title']}」"
    if found["state"] == OFFLINE:
        return ("在轮播（不算在播）" if found["loop"] else "没开播") + (f"：{found['name']}" if found["name"] else "")
    if found["state"] == MISSING:
        return "房间不存在"
    return f"没查到（{found['error']}）"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    keys = {name: key for key, name in NAMES.items()}
    args = sys.argv[1:]
    if not args or len(args) % 2:
        raise SystemExit(__doc__)
    asked = [{"id": str(i), "platform": keys.get(args[i], args[i]), "room": args[i + 1]} for i in range(0, len(args), 2)]
    started = time.time()
    for player, found in zip(asked, query(asked).values()):
        print(f"{NAMES.get(player['platform'], player['platform'])} {player['room']}：{describe(found)}")
    print(f"用了 {time.time() - started:.1f} 秒")
