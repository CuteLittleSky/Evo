"""开发者公告（2026-11-01 用户决定加）：下载 notice.json 这个公开的小文件，有公告就在 Evo 窗口顶部显示，
重要的再用托盘通知提醒一次。和检查新版本共用"自动检查新版本"这个开关（用户决定不另外问）：关着时不联网，也不显示。

联网和 update_check.py 一样：先读国内线路（COS）上的，读不到再读 https://dl.evotracker.cn/notice.json；
请求里只带"Evo/<版本号>"，不发送任何战绩、截图或电脑信息。托盘程序每 30 分钟读一次（用户定的），
打开窗口时上次读过了 5 分钟也读一次（evo_app.start_notice_checker）。连不上、格式不对都当作"这次没读到"，
留着上次读到的。文件放在 R2 上、不经过统计服务器：统计服务器（Worker、D1）免费版有每天的上限，超了就报错。
没读到时写明每个地址各自的原因（net_route.labeled，2026-11-19 起；原来只写第一个地址的，COS 从 10-31 起一直回 451，
dl.evotracker.cn 的原因被盖住了）。

国内备用线路（2026-11-19 用户决定，net_route.get）：有的国内网络按域名拦了 evotracker.cn，两个地址都连不上时经派圈助手的
云环境取同一个 notice.json（cloud_relay.py，只在开发者电脑上；GitHub 上的 Evo 没有）。只有正式地址才走（测试、
EVO_NOTICE_URL 换了地址时不走）。走备用线路读到的，RELAY_EVERY（3 小时）以内不再读：派圈助手的云环境每月有调用上限。

notice.json 由 tools/notice.py 上传到 R2 和 COS：
    {"notices": [{"id": "20261102-1530", "level": "important", "text": "……", "link": "https://evotracker.cn/……",
                  "until": "2026-11-05", "versions": "<=0.5.0"}]}
- id：每条不一样（字母、数字、- 和 _）。点"知道了"以后记下来，这条不再显示
- level："important"（重要：也用托盘通知提醒一次，Apex 开着时等关了游戏再弹）或 "info"（只在窗口里显示）
- text：只当纯文字显示，最多 TEXT_MAX 个字
- link（可以没有）：只接受自己网站上的 https 地址（update_check.TRUSTED_HOSTS），不对就不显示链接
- until（可以没有）：过了这一天（电脑上的日期）就不显示
- versions（可以没有）：只给这些版本的 Evo 看，如 "<=0.5.0"、">=0.5.1,<0.6.0"、"0.5.1"
格式不对的那一条去掉，不影响别的。存储桶的密钥就算泄露，别人也只能让 Evo 显示几行字，不能让它打开别的网站。

用到的设置（app_settings，存在数据文件夹的 settings.json）：
- notice：上次读的结果 {"list": 公告（同上的格式）, "checked": 读的时间（time.time()）, "ok": 读到没有}。
  读没读到都记时间（托盘据此决定打开窗口时要不要再读）；没读到时 list 留着上次的
- notice_dismissed：点过"知道了"的公告 id
- notice_notified：用托盘通知提醒过的公告 id

测试时可以用环境变量 EVO_NOTICE_URL 换成本机的测试地址（这时也不读 COS 上的）。
用法：python code/notice_check.py   → 读一次并打印结果
"""

import json
import os
import re
import threading
import time
import urllib.request
from datetime import date
from urllib.parse import urlparse

import app_settings
import net_route
import update_check
from storage import APP_VERSION

DEFAULT_URL = "https://dl.evotracker.cn/notice.json"
NOTICE_URL = os.environ.get("EVO_NOTICE_URL", DEFAULT_URL)
# 先读 COS 上的（国内快），读不到再读 dl.evotracker.cn 的；设了 EVO_NOTICE_URL 时只读它
URLS = ([f"{update_check.COS_BASE}/notice.json"] if update_check.COS_BASE and not os.environ.get("EVO_NOTICE_URL")
        else []) + [NOTICE_URL]
TIMEOUT = update_check.TIMEOUT
RELAY_EVERY = 3 * 3600  # 秒：走国内备用线路读到的，这么久以内不再读（2026-11-19 用户定的，平常是 30 分钟一次）
MAX_BYTES = 64 * 1024
MAX_NOTICES = 5  # 一次最多显示几条
TEXT_MAX = 200
KEEP_IDS = 50  # "知道了"、提醒过的 id 只留最近这么多条
LEVELS = ("info", "important")
STATE_KEY, DISMISSED_KEY, NOTIFIED_KEY = "notice", "notice_dismissed", "notice_notified"

wake = threading.Event()  # 打开窗口时（evo_app.py）叫醒托盘里读公告的线程


class NoticeError(Exception):
    pass


def enabled():
    """"自动检查新版本"开着才读、才显示公告（2026-11-01 用户决定：不另外问）。"""
    return app_settings.get("update_auto") is True


def versions_ok(rule, version=APP_VERSION):
    """versions 这一项：没写 → 谁都给看；"<=0.5.0"、">=0.5.1,<0.6.0" 这类（逗号隔开的都要满足）。写错了返回 None。
    测试版的版本号（"0.7.0b"，2026-11-21 起）在规则里也认，比较规则同 update_check.version_tuple（测试版比同号的正式版旧）。"""
    if rule is None:
        return True
    current = update_check.version_tuple(version)
    ok = True
    for part in str(rule).split(","):
        m = re.fullmatch(r"\s*(<=|>=|<|>|==|=)?\s*(\d+\.\d+\.\d+(?:b\d*)?)\s*", part)
        if not m or current is None:
            return None
        target = update_check.version_tuple(m.group(2))
        op = m.group(1) or "="
        ok = ok and {"<": current < target, "<=": current <= target, ">": current > target,
                     ">=": current >= target, "=": current == target, "==": current == target}[op]
    return ok


def safe_link(url):
    """链接只接受自己网站上的 https 地址；不对返回 None（不显示链接）。"""
    try:
        parsed = urlparse(str(url))
    except ValueError:
        return None
    return str(url) if parsed.scheme == "https" and parsed.hostname in update_check.TRUSTED_HOSTS else None


def clean(raw):
    """检查一条公告，格式不对返回 None。返回的格式和 notice.json 里的一样（没写的项是 None）。"""
    if not isinstance(raw, dict):
        return None
    nid, level, text = raw.get("id"), raw.get("level", "info"), raw.get("text")
    if not isinstance(nid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", nid):
        return None
    if level not in LEVELS or not isinstance(text, str) or not text.strip():
        return None
    until = raw.get("until")
    if until is not None:
        try:
            date.fromisoformat(str(until))
        except ValueError:
            return None
    versions = raw.get("versions")
    if versions is not None and versions_ok(versions) is None:
        return None
    return {"id": nid, "level": level, "text": " ".join(text.split())[:TEXT_MAX],
            "link": safe_link(raw["link"]) if raw.get("link") else None,
            "until": str(until) if until is not None else None,
            "versions": str(versions) if versions is not None else None}


def clean_list(raw):
    """notice.json 里的 notices（或存着的 list）：一条条检查，不对的去掉，最多 MAX_NOTICES 条，id 不重复。"""
    out, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        notice = clean(item)
        if notice is not None and notice["id"] not in seen:
            out.append(notice)
            seen.add(notice["id"])
    return out[:MAX_NOTICES]


def fetch(timeout=TIMEOUT):
    """下载并检查 notice.json，返回公告列表（可能是空的）。都不行抛 NoticeError（说的是每个地址各自的原因）。"""
    return _fetch_routed(timeout)[0]


def _fetch_routed(timeout):
    """同 fetch，返回 (公告列表, 从哪读到的：net_route.DIRECT / RELAY)。先读 COS 上的，读不到（或格式不对）再读下一个；
    有地址是连不上（不是格式不对、不是 4xx）时，再走国内备用线路。"""
    def primary():
        errors, unreachable = [], False
        for url in URLS:
            try:
                body = _download(url, timeout)
                _parse(body)
                return body
            except NoticeError as e:
                errors.append(f"{net_route.host_label(url)}：{e}")
            except OSError as e:
                errors.append(net_route.labeled(url, e))
                unreachable = unreachable or net_route.unreachable(url, e) is not None
        if unreachable:
            raise net_route.Unreachable("；".join(errors))
        raise NoticeError("；".join(errors))

    try:
        body, via, _age = net_route.get("notice", primary, official=URLS[-1] == DEFAULT_URL)
    except net_route.Unreachable as e:
        raise NoticeError(f"连不上（{e}）") from e
    return _parse(body), via


def _download(url, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": f"Evo/{APP_VERSION}", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_BYTES)


def _parse(body):
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("notices"), list):
        raise NoticeError("公告文件的格式不对")
    return clean_list(data["notices"])


def check(timeout=TIMEOUT):
    """读一次公告并存下。开关关着、走备用线路读到的还不到 RELAY_EVERY 时不联网，返回 None；否则返回
    {"ok": True, "notices": [...], "via": 从哪读到的} 或 {"ok": False, "error": 原因}。
    读到了就换成新的（没有公告时是空的：撤下的公告不再显示）。"""
    if not enabled():
        return None
    old = app_settings.get(STATE_KEY)
    old = old if isinstance(old, dict) else {}
    checked = old.get("checked")
    if (old.get("ok") is True and old.get("via") == net_route.RELAY and isinstance(checked, (int, float))
            and 0 <= time.time() - checked < RELAY_EVERY):
        return None
    try:
        notices, via = _fetch_routed(timeout)
    except NoticeError as e:
        app_settings.set_value(STATE_KEY, {"list": clean_list(old.get("list")), "checked": time.time(), "ok": False})
        return {"ok": False, "error": str(e)}
    app_settings.set_value(STATE_KEY, {"list": notices, "checked": time.time(), "ok": True, "via": via})
    return {"ok": True, "notices": notices, "via": via}


def due(seconds):
    """上次读公告是不是超过 seconds 秒以前了（没读过也算）。"""
    state = app_settings.get(STATE_KEY)
    checked = state.get("checked") if isinstance(state, dict) else None
    return not isinstance(checked, (int, float)) or time.time() - checked >= seconds


def _ids(key):
    ids = app_settings.get(key)
    return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []


def visible(today=None):
    """窗口顶部要显示的公告：开关开着、没点过"知道了"、没过期、给这个版本看的。"""
    if not enabled():
        return []
    state = app_settings.get(STATE_KEY)
    dismissed, today = set(_ids(DISMISSED_KEY)), today or date.today()
    out = []
    for notice in clean_list(state.get("list") if isinstance(state, dict) else None):
        if notice["id"] in dismissed or versions_ok(notice["versions"]) is not True:
            continue
        if notice["until"] is not None and date.fromisoformat(notice["until"]) < today:
            continue
        out.append(notice)
    return out


def to_notify(today=None):
    """要用托盘通知提醒的：显示着的重要公告里还没提醒过的。"""
    notified = set(_ids(NOTIFIED_KEY))
    return [n for n in visible(today) if n["level"] == "important" and n["id"] not in notified]


def _remember(key, nid):
    ids = [i for i in _ids(key) if i != nid] + [nid]
    app_settings.set_value(key, ids[-KEEP_IDS:])


def mark_notified(nid):
    _remember(NOTIFIED_KEY, nid)


def dismiss(nid):
    """点了"知道了"：这条不再显示，也不再用托盘通知提醒。"""
    if isinstance(nid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", nid):
        _remember(DISMISSED_KEY, nid)
        _remember(NOTIFIED_KEY, nid)


if __name__ == "__main__":
    print(f"读公告的地址：{'，读不到时 '.join(URLS)}")
    try:
        found = fetch()
    except NoticeError as error:
        print(f"没读到：{error}")
    else:
        print(f"{len(found)} 条公告" + ("" if found else "（现在没有公告）"))
        for item in found:
            print(f"  · [{item['level']}] {item['id']}：{item['text']}"
                  + (f"（{item['link']}）" if item["link"] else "")
                  + (f"，到 {item['until']}" if item["until"] else "")
                  + (f"，只给 {item['versions']}" if item["versions"] else ""))
