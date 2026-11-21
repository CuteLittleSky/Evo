"""在 Evo 窗口旁边另开一个小窗口，显示直播平台官方的登录页（现在只有斗鱼；2026-11-21 用户定的，照 PureLive 的做法）。

在 Evo 窗口的进程里运行（app_window.py 的 WindowApi.platform_login，看直播页点"登录斗鱼看原画"时调用）：
1. 开一个小窗口，打开平台官方的登录页（地址写在 config/watch.json 的 login_page，只认 https 的；页面给的地址不听）。
   账号、密码、验证码都是玩家在平台自己的网页上填的，Evo 不碰
2. 每秒看一次这个平台几个地址（login_domains）下的 Cookie：出现了表示"登录好了"的那几个（login_signals）之一，
   就把这些 Cookie 交给 Evo 的后台存起来（POST 面板的 /esports/login/<平台>/save，只发给本机的面板、带着面板的口令），
   **再把浏览器里的这些 Cookie 删掉**（登录凭证只留后台加密存的那一份，见 douyu_login.py），然后关掉小窗口
3. 玩家自己关了小窗口：什么都不存

登录凭证不经过页面里的脚本（页面只知道"登录好了没有"），不写进日志。
这个文件只在窗口进程里用，不加载面板和识别用的库。
"""

import json
import threading
import time
import urllib.parse
import urllib.request

import webview

from paths import CONFIG_DIR

PLATFORMS = json.loads((CONFIG_DIR / "watch.json").read_text(encoding="utf-8"))["platforms"]
SIZE = (460, 780)       # 小窗口的大小（斗鱼的登录页是手机版的）
SETTLE = 1.5            # 秒：看到登录好了以后再等一下，让平台把剩下的 Cookie 写完
POLL = 1.0              # 秒：多久看一次
BLANK = "<!doctype html><meta charset='utf-8'><body style='margin:0;background:#0D0F13'></body>"
_open = {}              # 平台 → 开着的登录窗口（一个平台一次只开一个）
_lock = threading.Lock()


def config(platform):
    """这个平台登录要的几项；没有官方登录页（或者写得不对）返回 None。"""
    found = PLATFORMS.get(platform)
    if not isinstance(found, dict):
        return None
    page, domains, signals = found.get("login_page"), found.get("login_domains"), found.get("login_signals")
    if (not isinstance(page, str) or not page.startswith("https://") or not isinstance(domains, list) or not domains
            or not all(isinstance(d, str) and d.startswith("https://") for d in domains)
            or not isinstance(signals, list) or not signals):
        return None
    return {"name": str(found.get("name") or platform), "page": page, "domains": domains,
            "signals": {str(s) for s in signals}, "user_agent": str(found.get("user_agent") or "")}


def save_url(panel_url, platform):
    """后台收登录凭证的地址：只能是本机的面板（http://127.0.0.1:端口）；不是返回 None。"""
    parts = urllib.parse.urlsplit(str(panel_url or ""))
    if parts.scheme != "http" or parts.hostname != "127.0.0.1" or not parts.port:
        return None
    return f"http://127.0.0.1:{parts.port}/esports/login/{urllib.parse.quote(platform, safe='')}/save"


def signed_in(cookies, signals):
    """这些 Cookie（[(名字, 值, ...)]）里有没有表示"登录好了"的。"""
    return any(name in signals and value for name, value, *_ in cookies)


def cookie_text(cookies):
    """[(名字, 值, ...)] → "名字=值; 名字=值"（同名的留后面的；空的不要）。"""
    merged = {}
    for name, value, *_ in cookies:
        if name and value:
            merged[name] = value
    return "; ".join(f"{name}={value}" for name, value in merged.items())


def post(url, token, cookie):
    """把登录凭证交给本机的面板。返回它存下了没有。"""
    data = urllib.parse.urlencode({"token": token, "cookie": cookie}).encode("utf-8")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as response:
            return bool(json.loads(response.read(100_000).decode("utf-8")).get("ok"))
    except (OSError, ValueError):
        return False


# ---------- 下面要用到真的窗口（WebView2），测试里不跑 ----------

def _cookies(window, url):
    """这个地址下的 Cookie：[(名字, 值, 浏览器里的那个 Cookie 对象)]。在窗口自己的线程里问、在那个线程里读。"""
    from Microsoft.Web.WebView2.Core import CoreWebView2Cookie
    from System import Action
    from System.Collections.Generic import List
    from System.Threading.Tasks import Task

    form, done, out = window.native, threading.Event(), []

    def parse(found):
        try:
            for item in found:
                out.append((str(item.Name), str(item.Value), item))
        finally:
            done.set()

    def callback(task):
        found = list(task.Result)
        form.Invoke(Action(lambda: parse(found)))

    def ask():
        form.browser.webview.CoreWebView2.CookieManager.GetCookiesAsync(url).ContinueWith(
            Action[Task[List[CoreWebView2Cookie]]](callback))

    form.Invoke(Action(ask))
    done.wait(5)
    return out


def _all_cookies(window, domains):
    found = []
    for url in domains:
        found += _cookies(window, url)
    return found


def _delete(window, cookies):
    """把这些 Cookie 从浏览器里删掉。"""
    from System import Action

    form = window.native

    def go():
        manager = form.browser.webview.CoreWebView2.CookieManager
        for _, _, item in cookies:
            manager.DeleteCookie(item)

    form.Invoke(Action(go))


def _browser_ready(window):
    """窗口里的浏览器建好了没有。浏览器的对象只能在窗口自己的线程里碰：在别的线程里读它的属性，会和窗口的线程
    互相等住（2026-11-21 试的时候整个进程卡死过），所以连"好了没有"也交给那个线程去看。"""
    from System import Action

    form, out = window.native, []
    form.Invoke(Action(lambda: out.append(form.browser.webview.CoreWebView2 is not None)))
    return bool(out and out[0])


def _set_user_agent(window, user_agent):
    from System import Action

    form = window.native
    form.Invoke(Action(lambda: setattr(form.browser.webview.CoreWebView2.Settings, "UserAgent", user_agent)))


def _watch(window, platform, cfg, url, token, closed):
    """登录窗口开着的时候在后台线程里跑：等登录好了，交给后台，删掉浏览器里的那份，关窗口。"""
    try:
        window.events.shown.wait(10)
        for _ in range(100):  # 等窗口里的浏览器准备好（最多 10 秒）
            if closed.is_set():
                return
            try:
                if _browser_ready(window):
                    break
            except Exception:  # noqa: BLE001  窗口还没建好
                pass
            time.sleep(0.1)
        if cfg["user_agent"]:
            _set_user_agent(window, cfg["user_agent"])  # Evo 窗口的浏览器标识里有 EvoApp，平台的登录页不认识：换成普通浏览器的
        window.load_url(cfg["page"])
        while not closed.wait(POLL):
            if not signed_in(_all_cookies(window, cfg["domains"]), cfg["signals"]):
                continue
            closed.wait(SETTLE)
            found = _all_cookies(window, cfg["domains"])
            post(url, token, cookie_text(found))
            _delete(window, found)
            break
    except Exception:  # noqa: BLE001  窗口正在关、浏览器出了错：当作没登录，不影响 Evo 窗口
        pass
    finally:
        with _lock:
            _open.pop(platform, None)
        if not closed.is_set():
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass


def open_login(main, platform, token):
    """开这个平台的登录窗口。main：Evo 的主窗口（从它的地址知道面板在哪个端口）。
    返回开了没有（平台没有官方登录页、主窗口不是本机的面板：不开）。已经开着就把它调到前面。"""
    cfg = config(str(platform))
    url = save_url(main.get_current_url(), str(platform)) if cfg else None
    if not cfg or not url or not token:
        return False
    with _lock:
        already = _open.get(platform)
        if already is not None:
            try:
                already.show()
            except Exception:  # noqa: BLE001
                pass
            return True
        window = webview.create_window(f"登录{cfg['name']} · Evo", html=BLANK, width=SIZE[0], height=SIZE[1],
                                       min_size=(360, 560), background_color="#0D0F13")
        _open[platform] = window
    closed = threading.Event()
    window.events.closed += lambda: closed.set()
    threading.Thread(target=_watch, args=(window, platform, cfg, url, str(token), closed), name="platform-login",
                     daemon=True).start()
    return True
