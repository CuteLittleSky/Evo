"""弹幕（看直播页用，只看不发）：告诉页面一个直播间的弹幕要连哪里、带什么。连接本身由页面里的脚本直接连平台的弹幕服务器
（浏览器自带的 WebSocket），不经过这里、也不经过开发者的服务器；这里只做页面做不了的那一步。

2026-11-19 用户要的（看了开源的 PureLive https://github.com/wzgrx/PureLive 以后提的，协议是照它读来的，代码是自己写的）。
- 斗鱼：弹幕服务器的地址写在 config/watch.json（douyu 的 danmaku_url），不用登录，只要真实房间号。选手库里写的可能是短号、
  靓号，所以先问一次房间信息（和查开播状态是同一个地址）拿真实房间号
- 别的平台还没做：返回 None，页面就不显示弹幕的开关

用法：python code/danmaku.py 斗鱼房间号   → 打印页面会拿到的东西
"""

import json
import sys

import live_status


def douyu_room(body):
    """斗鱼房间信息的回包 → 真实房间号（字符串）；没有返回 None。外面包的层数不一样，和 live_status.parse_douyu 一样找。"""
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        return None
    outer = data if isinstance(data, dict) else {}
    inner = outer.get("data") if isinstance(outer.get("data"), dict) else {}
    room = inner.get("room") or outer.get("data") or outer.get("room") or outer
    rid = str(room.get("room_id") or "") if isinstance(room, dict) else ""
    return rid if rid.isdigit() and len(rid) <= 24 else None


def douyu(room, get=None):
    cfg = live_status.PLATFORMS["douyu"]
    url = cfg.get("danmaku_url")
    if not isinstance(url, str) or not url.startswith("wss://"):
        return None
    body = (get or live_status.fetch)(cfg["info_url"].format(room=room), live_status._headers("douyu", room))
    rid = douyu_room(body)
    if rid is None:
        raise live_status.StatusError("斗鱼回的数据里没有房间号")
    return {"platform": "douyu", "url": url, "room": rid}


SOURCES = {"douyu": douyu}


def supported(platform):
    """这个平台的弹幕做了没有、配置里开着没有。"""
    return platform in SOURCES and bool(live_status.PLATFORMS.get(platform, {}).get("danmaku_url"))


def info(platform, room, get=None):
    """页面连弹幕要的东西：{"platform", "url", "room", ...}；这个平台没做返回 None。平台那边出错抛 live_status.StatusError。"""
    if not supported(platform) or not live_status.room_ok(platform, room):
        return None
    return SOURCES[platform](room, get)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    print(info("douyu", sys.argv[1]))
