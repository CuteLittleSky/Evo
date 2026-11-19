"""B站：取一个直播间的视频地址，并经本机中转播放（玩家登录了 B站以后，看直播页用 Evo 自己的播放器播 B站时用）。

问 B站网页版自己用的取流接口 getRoomPlayInfo（只要 H.264；HLS 的 TS 和 FLV 两种都要）：
回这个房间有哪些清晰度（qn：10000 原画、400 蓝光、250 超清、150 高清）、现在给的是哪一档，和视频地址。
- **不登录最高只给超清（1280×720）**，不管要哪一档（2026-11-17 实测）；带上登录凭证（bili_login.py）才给原画。
  所以没登录的玩家不走这里，用 B站官方的嵌入播放器（用户定的）
- **视频经过本机中转（2026-11-19 改的，原来是页面直接连 FLV）**：0.6.0 发出去以后国内的玩家反馈"登录了也没有原画"：
  登录着，画面却是 B站官方播放器，说明 Evo 自己的播放器没播成、退回去了。在瑞士复现不了（B站给海外的是另一批服务器）；
  参考项目 DTV 记着国内 FLV 直连 404、HLS 直连 200，有的服务器还要求请求带来源页（2026-11-19 实测 d1--ov-gotcha05：
  不带来源页 403）。所以改成：**优先 HLS（TS）**，Evo 的后台去取、带上 B站自己网页那样的来源页，接成一条流转给页面
  （hls_relay.py）；这个房间没有 HLS 才用 FLV（flv_relay.py，同样经过中转）。几个服务器按顺序试，名字带 mcdn 的排最后
- **先接 FLV，接不上再接 HLS（2026-11-19 晚上改的，上面"优先 HLS"作废）**：用户录介绍视频时发现大部分 B站房间"很久才打开、
  自带的播放器播不了"。实测（瑞士，登录着）：官方赛事台的 HLS 播放列表 1 秒就回；普通主播的房间 B站照样给了 HLS 的地址，
  播放列表却一直不回（等 45 秒也不回），同一个房间的 FLV 2–5 秒出第一块数据、约 10 Mbps。开源的 PureLive
  （https://github.com/wzgrx/PureLive ，AGPL-3.0，用户觉得它效果更好）也是这个顺序：http_stream 的 FLV 排在 HLS 前面，
  名字带 mcdn 的排最后，一条线不行换下一条。FLV 不通时是马上回错（403 / 404），HLS 不通时是一直不回，所以先试 FLV 等得少。
  做法（open_relay()）：两种地址都有时先开 FLV 的中转（每个服务器最多等 FLV_WAIT 秒），没接上再开 HLS 的；
  页面里的 mpegts.js 自己认得出来的是 FLV 还是 TS，所以哪一种接上了不用提前告诉它
- 登录凭证只在这里向 B站要地址时用，不交给页面；视频地址里是 B站签好的参数，不带登录凭证
- 房间号写短号也行（B站自己会换成真实房间号）
接口地址在 config/watch.json 的 bilibili 里。

用法：python code/watch_bilibili.py 房间号 [清晰度编号]   → 打印清晰度和地址（地址里的参数不打印）
"""

import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import bili_login
import flv_relay
import hls_relay
import live_status

CONFIG = bili_login.CONFIG
TIMEOUT = live_status.CONFIG["timeout"]
BEST = 10000        # 没选过清晰度时要这一档（原画）
REUSE_SECONDS = 20  # 刚取过的地址这么久以内直接用（页面先问有哪些清晰度，紧接着就要开播）
MAX_SEGMENT = 64 * 1024 * 1024  # HLS 的一段最多读这么多（原画 3 秒一段约 3 MB）
FLV_WAIT = 5        # 连 FLV 的一个服务器最多等这么久（后面还有别的服务器和 HLS 可以试）
_recent = {}
_lock = threading.Lock()


class BiliPlayError(Exception):
    """没取到地址。"""


def _number(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _urls(codec):
    """一种写法的几个服务器地址：只要 https 的；名字带 mcdn 的（B站的 P2P 节点，常连不上）排最后。"""
    urls = [str(info.get("host") or "") + str(codec.get("base_url") or "") + str(info.get("extra") or "")
            for info in codec.get("url_info") or [] if isinstance(info, dict)]
    urls = [url for url in urls if url.startswith("https://")]
    return sorted(urls, key=lambda url: "mcdn" in urllib.parse.urlsplit(url).netloc)


def parse(body):
    """getRoomPlayInfo 的回包 → {"hls": [播放列表地址], "flv": [FLV 地址], "kind": 先接哪一种（"flv" / "mpegts"；只是告诉页面一声，
    播放器自己认得出格式），"rate", "qualities", "ttl"}。只看 H.264 的。没开播、两种地址都没有抛 BiliPlayError。"""
    if not isinstance(body, dict) or body.get("code") != 0:
        raise BiliPlayError(f"B站的取流接口回了 code={body.get('code') if isinstance(body, dict) else body}"
                            f"（{body.get('message') if isinstance(body, dict) else ''}）")
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    if _number(data.get("live_status")) != 1:
        raise BiliPlayError("这个直播间现在没开播")
    play = (data.get("playurl_info") or {}).get("playurl") or {}
    names = {_number(q.get("qn")): str(q.get("desc") or "") for q in play.get("g_qn_desc") or [] if isinstance(q, dict)}
    found = {}  # "hls" / "flv" → 那一种写法的 codec
    for stream in play.get("stream") or []:
        for kind in stream.get("format") or []:
            key = {("http_hls", "ts"): "hls", ("http_stream", "flv"): "flv"}.get((stream.get("protocol_name"), kind.get("format_name")))
            for codec in kind.get("codec") or []:
                if key and key not in found and codec.get("codec_name") == "avc" and _urls(codec):
                    found[key] = codec
    if not found:
        raise BiliPlayError("B站没给 H.264 的视频地址")
    chosen = found.get("flv") or found["hls"]  # 清晰度看先接的那一种
    accept = [qn for qn in map(_number, chosen.get("accept_qn") or []) if qn is not None]
    return {"hls": _urls(found["hls"]) if "hls" in found else [], "flv": _urls(found["flv"]) if "flv" in found else [],
            "kind": "flv" if "flv" in found else "mpegts", "rate": str(_number(chosen.get("current_qn"))), "ttl": None,
            "qualities": [{"id": str(qn), "name": names.get(qn) or str(qn)} for qn in dict.fromkeys(accept)]}


def resolve(room, rate=None, ask=None, cookie=None, reuse=False, now=None):
    """取地址。rate：清晰度编号（None = 要原画）。返回 parse() 的结果（"rate" 是实际给的那一档）。
    reuse：REUSE_SECONDS 秒以内 remember() 过同一个（房间, 清晰度）就直接用（只用一次）。"""
    wanted = _number(rate)
    wanted = wanted if wanted and wanted > 0 else BEST
    moment = time.time() if now is None else now
    if reuse:
        with _lock:
            saved = _recent.pop((str(room), wanted), None)
        if saved and 0 <= moment - saved[0] < REUSE_SECONDS:
            return saved[1]
    url = CONFIG["play_api"] + f"&room_id={urllib.parse.quote(str(room))}&qn={wanted}"
    try:
        body, _ = (ask or bili_login.request)(url, bili_login.cookie() if cookie is None else cookie)
    except bili_login.BiliError as e:
        raise BiliPlayError(str(e)) from e
    return parse(body)


def remember(room, rate, found, now=None):
    """页面刚问过清晰度：把取到的地址留着，紧接着开播时直接用。"""
    with _lock:
        _recent.clear()  # 一次只播一路，只留最近的一个
        _recent[(str(room), _number(rate) or BEST)] = (time.time() if now is None else now, found)


def _request(url):
    """向 B站的视频服务器发的请求：带上 B站自己网页那样的来源页（有的服务器不带就回 403）。不带登录凭证。"""
    return urllib.request.Request(url, headers={
        "User-Agent": CONFIG["user_agent"], "Accept": "*/*", "Referer": CONFIG["referer"],
        "Origin": CONFIG["referer"].rstrip("/")})


def fetch(url):
    """取 HLS 的播放列表或一段视频，返回字节；连不上、不是 200 抛 OSError（hls_relay 用）。"""
    with urllib.request.urlopen(_request(url), timeout=TIMEOUT) as response:
        return response.read(MAX_SEGMENT)


def open_stream(url, wait=TIMEOUT):
    """连上 FLV 的视频服务器，返回可以 read() 的东西。连不上抛 BiliPlayError。"""
    try:
        return urllib.request.urlopen(_request(url), timeout=wait)
    except urllib.error.HTTPError as e:
        raise BiliPlayError(f"B站的视频服务器回了 HTTP {e.code}") from e
    except OSError as e:
        raise BiliPlayError(f"连不上 B站的视频服务器（{e}）") from e


class Either:
    """几种接法按顺序试（B站：先 FLV，再 HLS），第一个接上的就是这一路中转。和 flv_relay.Relay 一样用：start()、stream()、close()、mimetype。"""

    def __init__(self, makers, on_event=None):
        self.makers = makers  # [(名字, 造一路中转的函数), ...]
        self.on_event = on_event or (lambda _text: None)
        self.relay, self.used, self.closed = None, None, False
        self.mimetype = "video/x-flv"

    def start(self):
        problem = None
        for name, make in self.makers:
            if self.closed:
                break
            relay = make()
            try:
                relay.start()
            except flv_relay.RelayError as e:
                relay.close()
                problem = problem or e
                self.on_event(f"{name} 没接上（{e}）")
                continue
            self.relay, self.used, self.mimetype = relay, name, relay.mimetype
            if self.closed:  # 等的时候页面已经走了
                relay.close()
            if problem is not None:
                self.on_event(f"改用 {name} 接上了")
            return self
        raise problem or flv_relay.RelayError("upstream", "B站没给视频地址")

    def stream(self):
        return self.relay.stream()

    def close(self):
        self.closed = True
        if self.relay is not None:
            self.relay.close()


def open_relay(room, rate=None, on_event=None):
    """这个房间的一路中转（面板的 /esports/stream 用）：有 FLV 先接 FLV，没接上、没有 FLV 再接 HLS（接成 TS 流）。
    返回的东西有 start()、stream()、close() 和 mimetype（start() 以后才知道是哪一种）。取不到地址抛 BiliPlayError。"""
    first = resolve(room, rate, reuse=True)
    pending = {"flv": [first], "hls": [first]}  # 每种接法第一次用刚取到的，之后（地址过期、断了）重新要同一档

    def again(kind):
        return pending[kind].pop() if pending[kind] else resolve(room, first["rate"])

    def flv():
        def open_segment():
            problem = None
            for url in again("flv")["flv"]:  # 几个服务器按顺序试
                try:
                    return open_stream(url, FLV_WAIT), None
                except BiliPlayError as e:
                    problem = problem or e
            raise problem or BiliPlayError("B站没给 FLV 的地址")

        relay = flv_relay.Relay(open_segment, on_event=on_event)
        relay.mimetype = "video/x-flv"
        return relay

    def hls():
        return hls_relay.HlsRelay(lambda: again("hls")["hls"], fetch, on_event=on_event)

    makers = [(name, make) for name, make in (("FLV", flv), ("HLS", hls)) if first[name.lower()]]
    return Either(makers, on_event)


def mask(url):
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path[:24]}…"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2 or not live_status.ROOM_RE.fullmatch(sys.argv[1]):
        raise SystemExit(__doc__)
    try:
        result = resolve(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    except BiliPlayError as error:
        print(f"没取到：{error}")
        raise SystemExit(1) from None
    print("登录着：", bili_login.account()["logged_in"])
    print("清晰度：", "、".join(f"{q['name']}（{q['id']}）" for q in result["qualities"]))
    print("实际给的：", result["rate"], " 用", "HLS" if result["hls"] else "FLV")
    for name in ("hls", "flv"):
        print(f"  {name.upper()}：", "、".join(mask(url) for url in result[name]) or "没有")
