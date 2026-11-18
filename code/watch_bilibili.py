"""B站：取一个直播间的视频地址（玩家登录了 B站以后，看直播页用 Evo 自己的播放器播 B站时用）。

问 B站网页版自己用的取流接口 getRoomPlayInfo（照 DTV internal/platforms/bilibili/stream_url.go；只要 FLV、只要 H.264）：
回这个房间有哪些清晰度（qn：10000 原画、400 蓝光、250 超清、150 高清）、现在给的是哪一档，和 FLV 地址。
- **不登录最高只给超清（1280×720）**，不管要哪一档（2026-11-17 实测）；带上登录凭证（bili_login.py）才给原画。
  所以没登录的玩家不走这里，用 B站官方的嵌入播放器（用户定的）
- 地址允许页面直接连（不看来源页，回的跨域头是 *）：**不用本机中转**，页面里的 mpegts.js 直接连 B站的视频服务器。
  地址里带的是 B站签好的参数，不带登录凭证；登录凭证只在这里向 B站要地址时用一次，不交给页面
- 房间号写短号也行（B站自己会换成真实房间号）
接口地址在 config/watch.json 的 bilibili 里。

用法：python code/watch_bilibili.py 房间号 [清晰度编号]   → 打印清晰度和地址（地址里的参数不打印）
"""

import sys
import urllib.parse

import bili_login
import live_status

CONFIG = bili_login.CONFIG
DIRECT = True   # 页面直接连平台的视频服务器，不经过本机中转（panel_esports.py 据此决定给页面什么地址）
BEST = 10000    # 没选过清晰度时要这一档（原画）


class BiliPlayError(Exception):
    """没取到地址。"""


def _number(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse(body):
    """getRoomPlayInfo 的回包 → {"url", "urls", "rate", "qualities", "ttl"}。没开播、没有 H.264 的 FLV 抛 BiliPlayError。"""
    if not isinstance(body, dict) or body.get("code") != 0:
        raise BiliPlayError(f"B站的取流接口回了 code={body.get('code') if isinstance(body, dict) else body}"
                            f"（{body.get('message') if isinstance(body, dict) else ''}）")
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    if _number(data.get("live_status")) != 1:
        raise BiliPlayError("这个直播间现在没开播")
    play = (data.get("playurl_info") or {}).get("playurl") or {}
    names = {_number(q.get("qn")): str(q.get("desc") or "") for q in play.get("g_qn_desc") or [] if isinstance(q, dict)}
    for stream in play.get("stream") or []:
        for kind in stream.get("format") or []:
            if kind.get("format_name") != "flv":
                continue
            for codec in kind.get("codec") or []:
                if codec.get("codec_name") != "avc" or not codec.get("url_info"):
                    continue
                urls = [str(info.get("host") or "") + str(codec.get("base_url") or "") + str(info.get("extra") or "")
                        for info in codec["url_info"] if isinstance(info, dict)]
                urls = [url for url in urls if url.startswith("https://")]
                if not urls:
                    continue
                accept = [qn for qn in map(_number, codec.get("accept_qn") or []) if qn is not None]
                current = _number(codec.get("current_qn"))
                return {"url": urls[0], "urls": urls, "rate": str(current), "ttl": None,
                        "qualities": [{"id": str(qn), "name": names.get(qn) or str(qn)} for qn in dict.fromkeys(accept)]}
    raise BiliPlayError("B站没给 H.264 的视频地址")


def resolve(room, rate=None, ask=None, cookie=None):
    """取地址。rate：清晰度编号（None = 要原画）。返回 {"url", "rate": 实际给的那一档, "qualities", …}。"""
    wanted = _number(rate)
    url = CONFIG["play_api"] + f"&room_id={urllib.parse.quote(str(room))}&qn={wanted if wanted and wanted > 0 else BEST}"
    try:
        body, _ = (ask or bili_login.request)(url, bili_login.cookie() if cookie is None else cookie)
    except bili_login.BiliError as e:
        raise BiliPlayError(str(e)) from e
    return parse(body)


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
    print("实际给的：", result["rate"], " 地址：", mask(result["url"]), f"（共 {len(result['urls'])} 个主机）")
