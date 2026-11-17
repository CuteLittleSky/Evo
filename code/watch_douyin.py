"""抖音：取一个直播间的视频地址（看直播页用 Evo 自己的播放器播抖音时用）。

问的是查开播状态的同一个接口（live_status.douyin_info，带 a_bogus 签名；流程照 DTV internal/platforms/douyin）。
回包的 room.stream_url.live_core_sdk_data.pull_data 里：
- stream_data（一段 JSON 文字）：每一档的 FLV 地址和参数（sdk_params 里 VCodec 编码、resolution、fps、vbitrate 码率）。
  档：origin 主播推上来的原画（5–20 Mbps）、uhd 1080p60（8 Mbps，原画码率高的房间才有）、hd 720p60（6 Mbps）、
  sd 720p30（2 Mbps）、ld 540p、md 360p；ao 只有声音（不要）
- options.qualities：抖音网页自己的菜单上每一档叫什么（蓝光、超清、高清、标清）
只要 H.264 的档（2026-11-17、18 试了 12 个在播的 Apex 房间，每一档都是 H.264）；一档都没有就报错，页面显示退路。
- 地址有效期 7 天、不看来源页、回的跨域头是 *：**不用本机中转**，页面里的 mpegts.js 直接连抖音的视频服务器。
  抖音给的地址是 http 的，这里换成 https（实测一样能连）
- 不登录：和查开播状态一样，游客 Cookie 只留在内存里（live_status.py）
- 没选过清晰度（或者选的那一档这个房间没有）：要码率不超过 config/watch.json 里 default_max_bitrate 的最好一档。
  原画有的房间有 15–20 Mbps，和 8 Mbps 的 1080p60 看着差不多、流量多一倍；想看原画的自己在菜单里选

用法：python code/watch_douyin.py 房间号 [档]   → 打印清晰度和地址（地址里的参数不打印）
"""

import json
import re
import sys
import urllib.parse

import live_status

CONFIG = live_status.PLATFORMS["douyin"]
DIRECT = True   # 页面直接连平台的视频服务器，不经过本机中转（panel_esports.py 据此决定给页面什么地址）
ORDER = ("origin", "uhd", "hd", "sd", "ld", "md")  # 从好到差；没见过的档名排在最后
NAMES = {"origin": "原画", "uhd": "蓝光", "hd": "超清", "sd": "高清", "ld": "标清", "md": "流畅"}  # 抖音的菜单上没有的档叫什么
H264 = {"h264", "264", "avc"}
TIER_RE = re.compile(r"[a-z0-9_]{1,12}")
URL_RE = re.compile(r"https://[A-Za-z0-9.-]+(:\d+)?/[^\s\"'<>\\]+")
SIZE_RE = re.compile(r"(\d{2,5})x(\d{2,5})")
LIVE = 2  # room.status：2 在播、4 没开播


class DouyinError(Exception):
    """没取到地址。"""


class NotLive(DouyinError):
    """这一场已经结束了（或者还没开播）。"""


def _dict(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _loads(text):
    try:
        return _dict(json.loads(text or "{}"))
    except (TypeError, ValueError):
        return {}


def secure(url):
    """抖音给的视频地址：http 的换成 https；不像地址的不要。"""
    url = str(url or "").strip()
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url if URL_RE.fullmatch(url) else ""


def tiers(room):
    """这个房间有画面的各档，从好到差：[{"id": "uhd", "name": "蓝光 1080P60", "url", "bitrate", "codec": "h264"}, ...]。"""
    pull = _dict(_dict(_dict(room.get("stream_url")).get("live_core_sdk_data")).get("pull_data"))
    menu = {str(q.get("sdk_key")): str(q.get("name") or "").strip()
            for q in _dict(pull.get("options")).get("qualities") or [] if isinstance(q, dict)}
    out = []
    for key, entry in _dict(_loads(pull.get("stream_data")).get("data")).items():
        main = _dict(_dict(entry).get("main"))
        params = _loads(main.get("sdk_params"))
        url, bitrate = secure(main.get("flv")), _number(params.get("vbitrate"))
        size = SIZE_RE.fullmatch(str(params.get("resolution") or ""))
        if not url or not TIER_RE.fullmatch(str(key)) or (bitrate <= 0 and not size):  # 没有地址、只有声音的（ao）
            continue
        name = menu.get(key) or NAMES.get(key) or key
        if size:  # 竖着播的（720x1280）也按短的那一边叫
            name += f" {min(int(size[1]), int(size[2]))}P" + ("60" if _number(params.get("fps")) >= 50 else "")
        elif bitrate >= 1_000_000:  # 原画不写分辨率：写码率
            name += f" {round(bitrate / 1_000_000)}M"
        out.append({"id": key, "name": name, "url": url, "bitrate": bitrate, "codec": str(params.get("VCodec") or "").lower()})
    return sorted(out, key=lambda tier: ORDER.index(tier["id"]) if tier["id"] in ORDER else len(ORDER))


def pick(found, rate=None):
    """要哪一档。rate：玩家选的（这个房间有就用它）；没选、这个房间没有：码率不超过 default_max_bitrate 的最好一档，
    都超过就要码率最低的。found 是 tiers() 给的（从好到差）。"""
    for tier in found:
        if tier["id"] == rate:
            return tier
    limit = _number(CONFIG.get("default_max_bitrate"))
    fit = [tier for tier in found if limit <= 0 or tier["bitrate"] <= limit]
    return fit[0] if fit else min(found, key=lambda tier: tier["bitrate"])


def parse(body, rate=None):
    """房间接口的回包 → {"url", "rate": 给的是哪一档, "qualities": [{"id", "name"}], "ttl": None}。
    没开播抛 NotLive；没有视频地址、没有 H.264 的档抛 DouyinError。"""
    try:
        data = json.loads(body.decode("utf-8"))
    except (AttributeError, ValueError):
        data = None
    room = _dict(_dict(_dict(data).get("data")).get("room"))
    if not room:
        raise DouyinError(f"抖音没给房间数据（status_code={_dict(data).get('status_code')}）")
    if _number(room.get("status")) != LIVE:
        raise NotLive("这个直播间现在没开播")
    found = tiers(room)
    if not found:
        raise DouyinError("抖音没给视频地址")
    usable = [tier for tier in found if tier["codec"] in H264]
    if not usable:
        raise DouyinError("这个直播间没有 H.264 的画面（只有 H.265），Evo 里放不了")
    chosen = pick(usable, rate)
    return {"url": chosen["url"], "rate": chosen["id"], "ttl": None,
            "qualities": [{"id": tier["id"], "name": tier["name"]} for tier in usable]}


def resolve(room, rate=None, get=None):
    """取地址。rate：档名（None = 没选过）。查开播状态时记着这一场的内部房间号就直接问接口（一个请求）；
    没记着、记着的那一场结束了：开直播间网页读现在这一场的再问。"""
    room = str(room)
    try:
        known = live_status.douyin_known(room)
        if known:
            try:
                return parse(live_status.douyin_info(known, get), rate)
            except NotLive:
                pass  # 记着的那一场结束了：看看是不是又开了新的一场
        inner = live_status.douyin_inner(room, get)
        if inner is None:
            raise DouyinError("抖音上没有找到这个直播间")
        if inner == known:
            raise NotLive("这个直播间现在没开播")
        return parse(live_status.douyin_info(inner, get), rate)
    except live_status.StatusError as e:
        raise DouyinError(f"问抖音没问到：{e}") from e


def mask(url):
    """地址里的口令不打印：只留主机和路径的开头。"""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path[:24]}…"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2 or not live_status.ROOM_RE.fullmatch(sys.argv[1]):
        raise SystemExit(__doc__)
    try:
        result = resolve(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    except DouyinError as error:
        print(f"没取到：{error}")
        raise SystemExit(1) from None
    print("清晰度：", "、".join(f"{q['name']}（{q['id']}）" for q in result["qualities"]))
    print("给的是：", result["rate"], " 地址：", mask(result["url"]))
