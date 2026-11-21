"""斗鱼登录（看直播页用：斗鱼不登录最高只给蓝光4M，登录以后能看原画。2026-11-21 用户定的，照 PureLive 的做法）。

和 B站不一样的地方：斗鱼不登录也能在 Evo 里播，登录是可选的，只为了原画。

怎么登录：Evo 窗口另开一个小窗口，里面是斗鱼官方的登录页（code/login_window.py；手机验证码、密码都是在斗鱼自己的
网页上填，Evo 不碰账号和密码）。登录成功以后，窗口把斗鱼给浏览器的那些 Cookie 交给这里（save），再把浏览器里的那一份删掉。

登录凭证怎么存（和 B站的一样）：只存在玩家自己的电脑上，数据库旁边的 douyu_login.bin，用 Windows 自带的数据保护加密
（secret_store.py：只有这台电脑上的这个 Windows 用户能解开）。不发给 Evo 的任何服务器，不写进日志，不交给页面：
只有 Evo 的后台向斗鱼要播放地址时带上（watch_douyu.py），和到期前向斗鱼的通行证续期。

照 PureLive（https://github.com/wzgrx/PureLive ，live_core 的 douyu_api.dart / douyu_site.dart）读来的几条规矩：
- 算不算登录着：Cookie 里有 acf_jwt_token、acf_auth、dy_auth 之一（空的不算）
- 设备号：登录是和 Cookie 里的 dy_did 绑在一起的，要地址时签名、表单、Cookie 三处都要用它（换一个设备号，斗鱼当你是游客）
- 要地址时带的 Cookie：dy_did、acf_did 写成这个设备号，后面接登录 Cookie 里别的项；**LTP0 不带**（它只给通行证用，
  带着去要地址会被拒绝）
- 多久过期：令牌是 JWT 的看它里面的 exp；不是的（网页登录的 dy_auth）从存下的那一刻起算 7 天
- 续期：离过期不到一天（或者刚才要地址被拒绝了）时，带着 dy_did 和 LTP0 问一次通行证的 safeAuth，把它回的 Set-Cookie
  合并进来（没提到的项留着，LTP0 也留着，不然下次续不了）；没回 Set-Cookie、合并以后没有令牌了，都算没续成，旧的照旧用
退出登录（logout）：只删掉本机存的（斗鱼那边的这次登录到期自己失效）。

接口地址在 config/watch.json 的 douyu 里。
用法：python code/douyu_login.py   → 打印现在登录着没有（不打印登录凭证）
"""

import base64
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import live_status
import secret_store
from paths import DB_PATH

CONFIG = live_status.PLATFORMS["douyu"]
STORE = DB_PATH.parent / "douyu_login.bin"
TIMEOUT = live_status.CONFIG["timeout"]
TOKENS = ("acf_jwt_token", "acf_auth", "dy_auth")  # 有其中一个（不是空的）才算登录着，前面的优先
DEVICE, LONG_KEY = "dy_did", "LTP0"
LIFETIME = 7 * 86400      # 网页登录的令牌从存下起能用这么久
RENEW_MARGIN = 86400      # 离过期不到这么久就续期
RENEW_AGAIN = 600         # 续期没成：这么久以内不再试（要地址被拒绝时的那一次不受这个限制）
NAME_RE = re.compile(r"[A-Za-z0-9_!#$%&'*+.^`|~-]{1,64}")
DID_RE = re.compile(r"[0-9a-fA-F]{16,64}")
MAX_FIELDS, MAX_VALUE = 80, 4096
_lock = threading.Lock()
_renew_tried = [0.0]


class DouyuLoginError(Exception):
    pass


def fields(cookie):
    """"名字=值; 名字=值" → {名字: 值}（照原来的顺序，后面的同名项换掉前面的；名字不对、值太长、带控制字符的不要）。"""
    out = {}
    for part in str(cookie or "").split(";"):
        name, sep, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if sep and NAME_RE.fullmatch(name) and len(value) <= MAX_VALUE and not re.search(r"[\x00-\x1f\x7f;]", value):
            out[name] = value
    return dict(list(out.items())[:MAX_FIELDS])


def _get(found, name):
    """按名字取一项（不分大小写），没有、是空的返回 None。"""
    wanted = name.lower()
    return next((value for key, value in found.items() if key.lower() == wanted and value), None)


def token(found):
    """登录令牌（没有就是没登录）。"""
    return next((value for value in (_get(found, name) for name in TOKENS) if value), None)


def expiry(found, saved):
    """这次登录到什么时候（time.time() 那种秒数）；没有令牌返回 None。"""
    value = token(found)
    if value is None:
        return None
    parts = value.split(".")
    if len(parts) == 3:  # JWT：中间那一段里写着 exp
        try:
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            exp = int(payload["exp"])
            if exp > 0:
                return float(exp)
        except (ValueError, KeyError, TypeError):
            pass
    return float(saved or 0) + LIFETIME


def nickname(found):
    """Cookie 里写着的昵称（acf_nickname，网址编码的）；没有就写"斗鱼用户"。只当纯文字用。"""
    raw = _get(found, "acf_nickname") or _get(found, "acf_username") or ""
    text = " ".join(urllib.parse.unquote(raw).split())
    return text[:40] or "斗鱼用户"


def _text(found):
    return "; ".join(f"{name}={value}" for name, value in found.items())


def _saved():
    got = secret_store.load(STORE)
    if not isinstance(got, dict) or not isinstance(got.get("cookie"), str):
        return None
    found = fields(got["cookie"])
    return {**got, "fields": found} if token(found) else None


def save(cookie, now=None):
    """登录窗口交来的 Cookie：里面有登录令牌就存下，返回 {"ok": True, "name": 昵称}；没有令牌返回 {"ok": False}。"""
    found = fields(cookie)
    if token(found) is None:
        return {"ok": False, "name": ""}
    moment = time.time() if now is None else now
    name = nickname(found)
    with _lock:
        secret_store.save(STORE, {"cookie": _text(found), "name": name, "saved": moment})
    _renew_tried[0] = 0.0
    return {"ok": True, "name": name}


def forget():
    """删掉存着的登录凭证（只删本机的）。"""
    with _lock:
        secret_store.forget(STORE)


def logout():
    """退出登录：删掉本机存的。斗鱼那边的这次登录不去动它（到期自己失效）。"""
    forget()
    return True


def account(check=False, ask=None, now=None):
    """现在登录着没有：{"logged_in": bool, "name": 昵称}。过期了：check 时试着续一次，续不上就删掉存着的、算没登录
    （没 check 时过期了也先算登录着，要地址时自然会去续）。"""
    got = _saved()
    if not got:
        return {"logged_in": False, "name": ""}
    moment = time.time() if now is None else now
    if check and expiry(got["fields"], got.get("saved")) <= moment:
        if _renew(True, ask, moment) in (NO_KEY, REFUSED):  # 续不了：回到没登录。只是连不上时照旧算登录着，下次再试
            forget()
            return {"logged_in": False, "name": ""}
        got = _saved() or got
    return {"logged_in": True, "name": str(got.get("name") or "斗鱼用户")}


def identity(now=None):
    """要播放地址时用的身份：{"did": 设备号, "cookie": 请求里带的 Cookie}；没登录返回 None。
    只给 Evo 的后台用，不交给页面、不写日志。"""
    got = _saved()
    if not got:
        return None
    found = got["fields"]
    did = _get(found, DEVICE)
    if did is None or not DID_RE.fullmatch(did):
        return None  # 登录是和设备号绑在一起的：没有它，带着令牌去要地址也只会被当成游客
    rest = [f"{name}={value}" for name, value in found.items() if name.lower() not in ("dy_did", "acf_did", "ltp0")]
    return {"did": did, "cookie": "; ".join([f"dy_did={did}", f"acf_did={did}", *rest])}


def request(url, cookie):
    """问斗鱼的通行证续期：返回回包里的 Set-Cookie 那几行。连不上抛 DouyuLoginError。测试时整个换掉。"""
    headers = {"accept": "application/json, text/plain, */*", "accept-language": "zh-CN,zh;q=0.9,en;q=0.7",
               "origin": "https://www.douyu.com", "referer": "https://www.douyu.com/",
               "user-agent": CONFIG["user_agent"], "cookie": cookie}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as response:
            response.read(200_000)
            return response.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        raise DouyuLoginError(f"斗鱼的通行证回了 HTTP {e.code}") from e
    except OSError as e:
        raise DouyuLoginError(f"连不上斗鱼的通行证（{e}）") from e


def merge(found, lines):
    """把续期回的 Set-Cookie 合并进来：没提到的项留着，给了空值的去掉；只看每行分号前面的"名字=值"。"""
    out = dict(found)
    for line in lines:
        name, sep, value = str(line).split(";", 1)[0].partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not NAME_RE.fullmatch(name) or len(value) > MAX_VALUE or re.search(r"[\x00-\x1f\x7f]", value):
            continue
        if value:
            out[name] = value
        else:
            out.pop(name, None)
    return out


RENEWED, NOT_NEEDED, NO_KEY, REFUSED, UNREACHABLE = "renewed", "not-needed", "no-key", "refused", "unreachable"


def renew(force=False, ask=None, now=None):
    """续期：离过期不到一天时（force：不管还有多久）带着 dy_did 和 LTP0 问一次通行证。返回续上了没有。
    不用续、没有 LTP0、连不上、斗鱼没给新的：都返回 False，存着的不动。不抛错。"""
    return _renew(force, ask, time.time() if now is None else now) == RENEWED


def _renew(force, ask, moment):
    """续期，返回结果是哪一种：RENEWED 续上了 / NOT_NEEDED 不用续 / NO_KEY 没有 LTP0 或设备号 /
    REFUSED 斗鱼没给新的 / UNREACHABLE 连不上。"""
    got = _saved()
    if not got:
        return NO_KEY
    found = got["fields"]
    if not force:
        if expiry(found, got.get("saved")) - moment > RENEW_MARGIN or moment - _renew_tried[0] < RENEW_AGAIN:
            return NOT_NEEDED
    key, did = _get(found, LONG_KEY), _get(found, DEVICE)
    if key is None or did is None or not DID_RE.fullmatch(did):
        return NO_KEY
    _renew_tried[0] = moment
    stamp = str(int(moment * 1000))
    url = CONFIG["login_renew_url"] + "?" + urllib.parse.urlencode(
        {"client_id": "1", "t": stamp, "_": stamp, "callback": "axiosJsonpCallback"})
    try:
        lines = (ask or request)(url, f"{DEVICE}={did};{LONG_KEY}={key}")
    except DouyuLoginError:
        return UNREACHABLE
    merged = merge(found, lines) if lines else None
    if not merged or token(merged) is None:
        return REFUSED
    with _lock:
        secret_store.save(STORE, {**{k: v for k, v in got.items() if k != "fields"}, "cookie": _text(merged),
                                  "name": nickname(merged) if _get(merged, "acf_nickname") else got.get("name"),
                                  "saved": moment})
    return RENEWED


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    state = account()
    print(f"斗鱼：已登录（{state['name']}）" if state["logged_in"] else "斗鱼：没登录")
