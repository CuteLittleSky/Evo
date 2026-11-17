"""斗鱼：取一个直播间的视频地址（看直播页在 Evo 窗口里播斗鱼时用）。只在玩家自己的电脑上取，不登录斗鱼。

照 DTV（https://github.com/chen-zeong/DTV ，internal/platforms/douyu/stream_url.go，它又对齐 pure_live）的流程
用 Python 自带的功能重写，不跑 JS：
1. 自己随机生成一个 32 位的设备号（每次打开 Evo 换一个，只在内存里）
2. 问 getEncryption 要"签名材料"：一把钥匙 key、一个随机串 rand_str、要算几遍 enc_time（1–16）、enc_data。能用 5 分钟
3. 算签名：rand_str 和 key 拼起来做 MD5，重复 enc_time 遍，最后和 key、房间号、当前时间再做一次 MD5
4. 带着签名问 getH5PlayV1（写明不要 H.265）：回这个房间有哪些清晰度、哪些线路，和一个 FLV 地址
清晰度的编号（rate）是斗鱼的"请求码"，不是码率：一般 0 原画、4 蓝光4M、3 超清、2 高清，按斗鱼给的顺序列，不排序。
不登录要原画时，斗鱼实际给的是蓝光4M（1920×1080、30 帧、4 Mbps）；回包里的 rate 是实际给的那一档
（2026-11-17 实测，见 docs/底座.md"看直播"）。
接口地址、请求头在 config/watch.json 的 douyu 里。

用法：python code/watch_douyu.py 房间号 [清晰度编号]   → 打印清晰度、线路和地址（地址里的口令不打印）
"""

import hashlib
import html
import json
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import live_status

CONFIG = live_status.PLATFORMS["douyu"]
TIMEOUT = live_status.CONFIG["timeout"]
ENCRYPTION_SECONDS = 300   # 签名材料最多用这么久
ENCRYPTION_GUARD = 30      # 离它过期不到这么久就重新要
REUSE_SECONDS = 20         # 刚取过的地址这么久以内直接用（页面先问有哪些清晰度，紧接着就要开播）
DEVICE_ID = secrets.token_hex(16)  # 这次运行的设备号
_lock = threading.Lock()
_encryption = {}           # 上次要到的签名材料
_recent = {}               # (房间号, 清晰度) → (取到的时间, 结果)


class DouyuError(Exception):
    """没取到地址。"""


def _md5(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _headers(room=""):
    return {"accept": "application/json, text/plain, */*", "accept-language": "zh-CN,zh;q=0.9,en;q=0.7",
            "origin": "https://www.douyu.com", "referer": CONFIG["room_url"].format(room=room),
            "user-agent": CONFIG["user_agent"], "cookie": f"dy_did={DEVICE_ID}; acf_did={DEVICE_ID}"}


def fetch_json(url, room="", form=None):
    """问斗鱼的一个接口（给了 form 就是提交表单），返回解析好的 JSON。测试时整个换掉。"""
    data = urllib.parse.urlencode(form).encode("ascii") if form is not None else None
    extra = {"content-type": "application/x-www-form-urlencoded"} if form is not None else {}
    request = urllib.request.Request(url, data=data, headers={**_headers(room), **extra})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read(2_000_000).decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise DouyuError(f"斗鱼回了 HTTP {e.code}") from e
    except (OSError, ValueError) as e:
        raise DouyuError(f"连不上斗鱼（{e}）") from e


def encryption(get=None, force=False, now=None):
    """签名材料：{"key", "rand_str", "enc_time", "enc_data", "is_special", "expire_at"}。没过期就用上次的。"""
    get, now = get or fetch_json, time.time() if now is None else now
    with _lock:
        fresh = (_encryption and now - _encryption["_at"] < ENCRYPTION_SECONDS
                 and _encryption["expire_at"] > now + ENCRYPTION_GUARD)
        if force or not fresh:
            got = get(CONFIG["encryption_url"].format(did=DEVICE_ID))
            data = got.get("data") if isinstance(got, dict) else None
            if not isinstance(data, dict) or got.get("error") != 0:
                raise DouyuError(f"斗鱼的签名接口回了 error={got.get('error') if isinstance(got, dict) else got}")
            try:
                times, expire = int(data["enc_time"]), int(data["expire_at"])
            except (KeyError, TypeError, ValueError):
                raise DouyuError("斗鱼的签名接口回的东西不认识") from None
            if not (data.get("key") and data.get("rand_str") and data.get("enc_data")) or not 1 <= times <= 16:
                raise DouyuError("斗鱼的签名接口回的东西不认识")
            _encryption.clear()
            _encryption.update(key=str(data["key"]), rand_str=str(data["rand_str"]), enc_time=times,
                               enc_data=str(data["enc_data"]), is_special=data.get("is_special"), expire_at=expire, _at=now)
        return dict(_encryption)


def sign(enc, room, timestamp):
    """算 getH5PlayV1 要的 auth。"""
    secret = enc["rand_str"]
    for _ in range(enc["enc_time"]):
        secret = _md5(secret + enc["key"])
    suffix = "" if enc.get("is_special") == 1 else f"{room}{timestamp}"
    return _md5(secret + enc["key"] + suffix)


def play_data(room, rate=-1, cdn="", get=None, now=None):
    """问 getH5PlayV1，返回回包里的 data。rate=-1 是不指定清晰度。签名不认时重新要一次材料再试。"""
    get, now = get or fetch_json, time.time() if now is None else now
    problem = "没有回数据"
    for attempt in range(2):
        enc = encryption(get, force=attempt > 0, now=now)
        timestamp = int(now)
        form = {"enc_data": enc["enc_data"], "tt": str(timestamp), "did": DEVICE_ID, "auth": sign(enc, room, timestamp),
                "cdn": cdn, "rate": str(rate), "hevc": "0", "fa": "0", "ive": "0", "ver": "Douyu_new", "iar": "0"}
        got = get(CONFIG["play_url"].format(room=room), room, form)
        code = (got.get("error") or got.get("code") or 0) if isinstance(got, dict) else -1
        if code == 0 and isinstance(got.get("data"), dict):
            return got["data"]
        problem = f"error={code}（{got.get('msg') if isinstance(got, dict) else ''}）"
    raise DouyuError(f"斗鱼的取流接口回了 {problem}")


def _number(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def qualities(data):
    """这个房间有哪些清晰度：[{"id": "0", "name": "原画1080P60"}, ...]，按斗鱼给的顺序。"""
    out, seen = [], set()
    for item in data.get("multirates") or []:
        rate = _number(item.get("rate")) if isinstance(item, dict) else None
        if rate is not None and rate not in seen:
            seen.add(rate)
            out.append({"id": str(rate), "name": str(item.get("name") or "").strip() or "默认"})
    if not out:
        rate = _number(data.get("rate"))
        out.append({"id": str(rate if rate is not None else -1), "name": "默认"})
    return out


def lines(data):
    """线路（cdn 的名字）：斗鱼现在给的那条在前，scdn 开头的排最后。"""
    found = [str(c.get("cdn") or "").strip() for c in data.get("cdnsWithName") or [] if isinstance(c, dict)]
    current = str(data.get("rtmp_cdn") or "").strip()
    ordered = [c for c in dict.fromkeys([current, *found]) if c]
    return sorted(ordered, key=lambda c: c.lower().startswith("scdn"))


def stream_url(data):
    """回包里的视频地址（rtmp_url + rtmp_live）；没有抛 DouyuError。"""
    live = html.unescape(str(data.get("rtmp_live") or "")).strip()
    base = html.unescape(str(data.get("rtmp_url") or "")).strip()
    url = live if re.match(r"https?://", live) else f"{base.rstrip('/')}/{live.lstrip('/')}" if base and live else ""
    if not re.match(r"https?://[^/]+/.+", url):
        raise DouyuError("斗鱼的回包里没有视频地址（多半是没开播）")
    # DTV / pure_live 的做法：有的网络拿到的是限 300 秒的地址（expire=300），补一个 expire=0 让它不限时
    return url + "&expire=0" if "expire=300" in url and "fcdn=ws" in url else url


def lifetime(url):
    """这个地址多久以后会被服务器断开（秒）；不限时返回 None。地址里 expire=300 是限 300 秒，expire=0 不限。"""
    found = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("expire")
    seconds = _number(found[-1]) if found else None
    return seconds if seconds and 0 < seconds < 86400 else None


def resolve(room, rate=None, cdn="", get=None, now=None, reuse=False):
    """取地址。rate：清晰度编号（None = 要最好的：先要 0，斗鱼不给就降到它肯给的）。
    返回 {"url", "rate": 实际给的那一档, "qualities", "lines", "cdn", "ttl": 多久后被断开或 None}。
    reuse：REUSE_SECONDS 秒以内取过同一个（房间, 清晰度）就直接用（只用一次）。"""
    wanted = _number(rate)
    wanted = 0 if wanted is None else wanted
    key, moment = (str(room), wanted), time.time() if now is None else now
    if reuse and not cdn:
        with _lock:
            saved = _recent.pop(key, None)
        if saved and 0 <= moment - saved[0] < REUSE_SECONDS:
            return saved[1]
    data = play_data(room, wanted, cdn, get, now)
    url = stream_url(data)
    applied = _number(data.get("rate"))
    return {"url": url, "rate": str(applied if applied is not None else wanted), "qualities": qualities(data),
            "lines": lines(data), "cdn": cdn or str(data.get("rtmp_cdn") or ""), "ttl": lifetime(url)}


def remember(room, rate, found, now=None):
    """页面刚问过清晰度：把取到的地址留着，紧接着开播时直接用。"""
    with _lock:
        _recent.clear()  # 一次只播一路，只留最近的一个
        _recent[(str(room), _number(rate) or 0)] = (time.time() if now is None else now, found)


def open_stream(url, room):
    """连上视频服务器，返回可以 read() 的东西。连不上抛 DouyuError。"""
    request = urllib.request.Request(url, headers={
        "User-Agent": CONFIG["user_agent"], "Accept": "video/x-flv,application/octet-stream,*/*",
        "Referer": CONFIG["room_url"].format(room=room)})
    try:
        return urllib.request.urlopen(request, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        raise DouyuError(f"斗鱼的视频服务器回了 HTTP {e.code}") from e
    except OSError as e:
        raise DouyuError(f"连不上斗鱼的视频服务器（{e}）") from e


def open_segment(room, rate=None, reuse=False):
    """取地址并连上：返回 (可以 read() 的东西, 取地址的结果)。默认的线路连不上就换别的线路试。"""
    first = resolve(room, rate, reuse=reuse)
    problem = None
    for cdn in dict.fromkeys(["", *first["lines"]]):
        try:
            found = first if not cdn else resolve(room, first["rate"], cdn)
            return open_stream(found["url"], room), found
        except DouyuError as e:
            problem = problem or e
    raise problem


def mask(url):
    """地址里的口令不打印：只留主机、路径的开头和有哪些参数。"""
    parts = urllib.parse.urlsplit(url)
    keys = [k for k, _ in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
    return f"{parts.scheme}://{parts.netloc}{parts.path[:24]}…（参数：{'、'.join(keys)}）"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2 or not live_status.ROOM_RE.fullmatch(sys.argv[1]):
        raise SystemExit(__doc__)
    try:
        result = resolve(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    except DouyuError as error:
        print(f"没取到：{error}")
        raise SystemExit(1) from None
    print("清晰度：", "、".join(f"{q['name']}（{q['id']}）" for q in result["qualities"]))
    print("实际给的：", result["rate"], " 线路：", "、".join(result["lines"]) or "（没写）")
    print("地址：", mask(result["url"]), " 多久后断开：", f"{result['ttl']} 秒" if result["ttl"] else "不限时")
