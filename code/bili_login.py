"""B站扫码登录（看直播页用：B站不登录最高只给 720p，登录以后能看原画。2026-11-17 用户定的，照 DTV 的做法）。

用的是 B站网页版自己的扫码接口，Evo 不碰账号和密码（照 DTV internal/platforms/bilibili/qrlogin.go 的流程重写）：
1. 向 B站要一个二维码（start）：回二维码的内容（一个网址）和一个 key，约 3 分钟有效。Evo 把它画成二维码（qr.py）
2. 玩家用 B站 App 扫、在手机上点确认；这期间 Evo 每 2 秒拿 key 问一次状态（poll）：
   没扫（86101）、扫了还没确认（86090）、过期了（86038）、成功（0）
3. 成功时 B站在回包里给登录凭证（SESSDATA、bili_jct、DedeUserID 这几个 Cookie）。Evo 再问一次"我是谁"核对、拿昵称

登录凭证怎么存（用户定的）：只存在玩家自己的电脑上，数据库旁边的 bilibili_login.bin，用 Windows 自带的数据保护加密
（secret_store.py：只有这台电脑上的这个 Windows 用户能解开）。不发给 Evo 的任何服务器，不写进日志，不交给页面：
只有 Evo 的后台向 B站要播放地址时带上（watch_bilibili.py）。
退出登录（logout）：先告诉 B站让这次登录失效，再删掉这个文件；B站那边没告诉成，本机也照样删掉。
登录过期了（B站说没登录）：删掉文件，回到没登录的样子。每 CHECK_SECONDS 秒最多问一次。

接口地址在 config/watch.json 的 bilibili 里。
用法：python code/bili_login.py   → 打印现在登录着没有（不打印登录凭证）
"""

import http.cookiejar
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

CONFIG = live_status.PLATFORMS["bilibili"]
STORE = DB_PATH.parent / "bilibili_login.bin"
TIMEOUT = live_status.CONFIG["timeout"]
KEY_RE = re.compile(r"[0-9a-f]{32}")
COOKIE_NAMES = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")  # 登录凭证是这几个 Cookie
NEEDED = ("SESSDATA", "bili_jct")
CHECK_SECONDS = 6 * 3600   # 隔这么久向 B站核对一次登录还有没有效
WAITING, SCANNED, EXPIRED, OK, ERROR = "waiting", "scanned", "expired", "ok", "error"
STATES = {86101: WAITING, 86090: SCANNED, 86038: EXPIRED, 0: OK}
_lock = threading.Lock()


class BiliError(Exception):
    pass


def request(url, cookie="", form=None):
    """问 B站的一个接口：返回 (解析好的 JSON, 回包里给的 Cookie {名字: 值})。测试时整个换掉。"""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {"User-Agent": CONFIG["user_agent"], "Referer": "https://www.bilibili.com/", "Origin": "https://www.bilibili.com"}
    if cookie:
        headers["Cookie"] = cookie
    data = urllib.parse.urlencode(form).encode("ascii") if form is not None else None
    try:
        with opener.open(urllib.request.Request(url, data=data, headers=headers), timeout=TIMEOUT) as response:
            body = json.loads(response.read(1_000_000).decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise BiliError(f"B站回了 HTTP {e.code}") from e
    except (OSError, ValueError) as e:
        raise BiliError(f"连不上 B站（{e}）") from e
    if not isinstance(body, dict):
        raise BiliError("B站回的东西不认识")
    return body, {c.name: c.value for c in jar}


def _cookie_text(fields):
    return "; ".join(f"{name}={fields[name]}" for name in COOKIE_NAMES if fields.get(name))


def start(ask=None):
    """要一个登录二维码：{"key": 问状态用的, "url": 二维码的内容}。"""
    body, _ = (ask or request)(CONFIG["qr_generate_url"])
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    key, url = str(data.get("qrcode_key") or ""), str(data.get("url") or "")
    if body.get("code") != 0 or not KEY_RE.fullmatch(key) or not url.startswith("https://"):
        raise BiliError(f"B站没给二维码（code={body.get('code')}）")
    return {"key": key, "url": url}


def poll(key, ask=None, now=None):
    """扫了没有：{"state": "waiting" 没扫 / "scanned" 扫了还没确认 / "expired" 二维码过期 / "ok" 登录好了 / "error",
    "name": 登录好了时的昵称}。登录好了就存下来。"""
    ask = ask or request
    if not KEY_RE.fullmatch(str(key)):
        return {"state": ERROR, "name": ""}
    body, cookies = ask(CONFIG["qr_poll_url"] + "&qrcode_key=" + key)
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    state = STATES.get(data.get("code")) if body.get("code") == 0 else None
    if state != OK:
        return {"state": state or ERROR, "name": ""}
    # 登录凭证在回包的 Set-Cookie 里；data.url 的参数里也有一份（照 DTV：两处都看）
    fields = {k: v[-1] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(str(data.get("url") or "")).query).items()}
    fields.update(cookies)
    if not all(fields.get(name) for name in NEEDED):
        return {"state": ERROR, "name": ""}
    cookie = _cookie_text(fields)
    name = who(cookie, ask)
    if name is None:
        return {"state": ERROR, "name": ""}
    moment = time.time() if now is None else now
    with _lock:
        secret_store.save(STORE, {"cookie": cookie, "name": name, "saved": moment, "checked": moment})
    return {"state": OK, "name": name}


def who(cookie, ask=None):
    """带着登录凭证问 B站"我是谁"：返回昵称；B站说没登录返回 None；连不上抛 BiliError。"""
    body, _ = (ask or request)(CONFIG["nav_url"], cookie)
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    if body.get("code") != 0 or not data.get("isLogin"):
        return None
    return str(data.get("uname") or "")[:40] or "B站用户"


def _saved():
    found = secret_store.load(STORE)
    return found if isinstance(found, dict) and isinstance(found.get("cookie"), str) and found["cookie"] else None


def cookie():
    """存着的登录凭证（向 B站要播放地址时带上）；没登录是空的。只给 Evo 的后台用，不交给页面、不写日志。"""
    found = _saved()
    return found["cookie"] if found else ""


def account(check=False, ask=None, now=None):
    """现在登录着没有：{"logged_in": bool, "name": 昵称}。check：隔了 CHECK_SECONDS 秒没核对过就问一次 B站，
    B站说没登录（过期了、在别处退出了）就删掉存着的；连不上时照旧算登录着。"""
    found = _saved()
    if not found:
        return {"logged_in": False, "name": ""}
    moment = time.time() if now is None else now
    if check and moment - float(found.get("checked") or 0) >= CHECK_SECONDS:
        try:
            name = who(found["cookie"], ask)
        except BiliError:
            name = found.get("name") or ""
        else:
            if name is None:
                forget()
                return {"logged_in": False, "name": ""}
            with _lock:
                secret_store.save(STORE, {**found, "name": name, "checked": moment})
        return {"logged_in": True, "name": name}
    return {"logged_in": True, "name": str(found.get("name") or "")}


def forget():
    """删掉存着的登录凭证（只删本机的）。"""
    with _lock:
        secret_store.forget(STORE)


def logout(ask=None):
    """退出登录：先告诉 B站让这次登录失效（要带登录凭证里的 bili_jct），再删掉本机存的。
    返回 B站那边有没有成功；不管成没成，本机都回到没登录。"""
    found = _saved()
    done = False
    if found:
        csrf = dict(part.split("=", 1) for part in found["cookie"].split("; ") if "=" in part).get("bili_jct", "")
        try:
            body, _ = (ask or request)(CONFIG["logout_url"], found["cookie"], {"biliCSRF": csrf})
            done = body.get("code") in (0, -101)  # -101：B站说本来就没登录着，也算退出了
        except BiliError:
            done = False
    forget()
    return done


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    state = account()
    print(f"B站：已登录（{state['name']}）" if state["logged_in"] else "B站：没登录")
