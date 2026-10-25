"""检查 Evo 有没有新版本。Evo 会联网的只有这里、一键更新（updater.py，用户点了"立即更新"才下载）和匿名统计
（telemetry.py，用户同意了才发），都可以关掉。

只做一件事：下载 https://dl.evotracker.cn/latest.json 这个公开的小文件，看里面写的最新版本号。
请求里只带"Evo/<版本号>"，不发送任何战绩、截图或电脑信息；和打开任何网页一样，服务器能看到访问者的 IP 地址。
几秒钟连不上、文件格式不对，都当作"这次没查到"，不影响 Evo 的其他功能。

latest.json 由发布脚本生成，和安装包一起传到发布站：
    {"version": "0.4.1", "date": "2026-10-30", "page": "https://evotracker.cn/#download", "notes": ["……"],
     "download": {"url": "https://dl.evotracker.cn/Evo-0.4.1-win64.zip", "sha256": "……", "size": 111692635}}
download 是一键更新用的安装包（0.3.3 起才有）：地址只接受 dl.evotracker.cn，没有或格式不对就只能去官网下载。
不用上游的备用线路（mirrors）和增量补丁包（patches）：派喵版只有 dl.evotracker.cn 一条线路，更新只下完整的安装包。

用到的设置（app_settings，存在数据文件夹的 settings.json）：
- update_auto：自动检查（True / False；还没问过用户时没有这一项）
- update_latest：上次检查发现的新版本（没有新版本时为 None）
- update_dismissed：用户点了"以后再说"的版本号（这个版本不再显示提示条）
- update_notified：已经用托盘通知提醒过的版本号（同一个版本只通知一次）
- update_last_check：上次检查的时间和结果（设置页显示）

测试时可以用环境变量 EVO_UPDATE_URL 换成本机的测试地址。
用法：python code/update_check.py   → 检查一次并打印结果
"""

import json
import os
import re
import time
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

import app_settings
from storage import APP_VERSION

UPDATE_URL = os.environ.get("EVO_UPDATE_URL", "https://dl.evotracker.cn/latest.json")
DEFAULT_PAGE = "https://evotracker.cn/#download"
TRUSTED_HOSTS = {"evotracker.cn", "www.evotracker.cn", "dl.evotracker.cn"}  # 只打开自己网站上的链接
DOWNLOAD_HOSTS = {"dl.evotracker.cn"}  # 一键更新从这里下载安装包（派喵版只有这一条线路）
# 用 EVO_UPDATE_URL 换成本机的测试地址时，也接受从同一个地址下载安装包（http 也行），方便不上线就试一键更新
TEST_HOST = urlparse(UPDATE_URL).hostname if os.environ.get("EVO_UPDATE_URL") else None
MAX_PACKAGE = 1024 ** 3  # 安装包现在约 105 MB，超过 1 GB 一定是写错了
TIMEOUT = 15  # 秒：国内移动宽带到 dl.evotracker.cn 绕得远，晚上还可能丢包，5 秒常常不够（0.3.6 及以前是 5 秒）
RETRY_PAUSE = 5  # 秒：自动检查没查到时，等一下再试一次
MAX_BYTES = 64 * 1024  # latest.json 只有几百字节，多的不读


class UpdateError(Exception):
    pass


def version_tuple(text):
    """"0.10.2" → (0, 10, 2)，用来比较新旧；格式不对返回 None。"""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(text or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def safe_page(url):
    """下载页链接只接受自己网站上的 https 地址，否则用官网下载页（防止被改成别的网站）。"""
    try:
        parsed = urlparse(str(url))
    except ValueError:
        return DEFAULT_PAGE
    return str(url) if parsed.scheme == "https" and parsed.hostname in TRUSTED_HOSTS else DEFAULT_PAGE


def _parse(url):
    try:
        return urlparse(str(url))
    except ValueError:
        return None


def _test_url(parsed):
    """设了 EVO_UPDATE_URL 时，和它同一个地址的 http / https 都接受（测试用）。"""
    return TEST_HOST is not None and parsed.scheme in ("http", "https") and parsed.hostname == TEST_HOST


def _package(info):
    """一个要下载的文件 {"url", "sha256", "size"}：地址只接受 dl.evotracker.cn 的 https（测试地址除外），
    SHA-256 和大小的格式要对，不对返回 None。latest.json 里写的 mirrors（备用线路）、patches（补丁包）一律忽略。"""
    if not isinstance(info, dict):
        return None
    url, digest, size = info.get("url"), info.get("sha256"), info.get("size")
    parsed = _parse(url)
    if parsed is None:
        return None
    host_ok = (parsed.scheme == "https" and parsed.hostname in DOWNLOAD_HOSTS) or _test_url(parsed)
    if not host_ok or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_PACKAGE:
        return None
    return {"url": str(url), "sha256": digest, "size": size}


def safe_download(info):
    """latest.json 里的安装包信息（规矩见 _package），没有或不对返回 None（这个版本只能去官网下载）。"""
    return _package(info)


def fetch_latest(timeout=TIMEOUT):
    """下载并检查 latest.json，返回 {"version", "date", "page", "notes", "download"}（download 没有时为 None）。
    连不上或格式不对抛出 UpdateError。"""
    return _fetch_latest(UPDATE_URL, timeout)


def _fetch_latest(url, timeout):
    request = urllib.request.Request(
        url, headers={"User-Agent": f"Evo/{APP_VERSION}", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_BYTES)
    except OSError as e:
        raise UpdateError("连不上更新服务器（网络不通，或者服务器暂时不可用）") from e
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        data = None
    if not isinstance(data, dict) or version_tuple(data.get("version")) is None:
        raise UpdateError("更新服务器返回的版本信息格式不对")
    notes = data.get("notes") if isinstance(data.get("notes"), list) else []
    return {
        "version": str(data["version"]).strip(),
        "date": str(data.get("date", ""))[:20],
        "page": safe_page(data.get("page", DEFAULT_PAGE)),
        "notes": [str(n)[:200] for n in notes if isinstance(n, str)][:10],
        "download": safe_download(data.get("download")),
    }


def check(timeout=TIMEOUT, attempts=1):
    """检查一次，把结果记进设置（面板据此显示提示条）。attempts：没查到时一共试几次（自动检查试 2 次，
    中间等 RETRY_PAUSE 秒；设置页的按钮只试 1 次，页面不用等太久）。返回：
    {"ok": True, "latest": {...}, "newer": 是否比现在装的新, "checked_at": 时间}，
    或 {"ok": False, "error": 原因, "checked_at": 时间}。"""
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    latest, error = None, None
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(RETRY_PAUSE)
        try:
            latest = fetch_latest(timeout)
            break
        except UpdateError as e:
            error = e
    if latest is None:
        result = {"ok": False, "error": str(error), "checked_at": checked_at}
    else:
        newer = version_tuple(latest["version"]) > version_tuple(APP_VERSION)
        result = {"ok": True, "latest": latest, "newer": newer, "checked_at": checked_at}
        app_settings.set_value("update_latest", latest if newer else None)
    app_settings.set_value("update_last_check", result)
    return result


def known_newer():
    """上次检查发现的新版本（确实比现在装的新）；没有返回 None。设置页用它显示下载按钮。"""
    latest = app_settings.get("update_latest")
    if not isinstance(latest, dict) or version_tuple(latest.get("version")) is None:
        return None
    if version_tuple(latest["version"]) <= version_tuple(APP_VERSION):
        return None  # 已经装上这个版本或更新的了
    return latest


def available():
    """要提醒用户的新版本：known_newer()，但用户点了"以后再说"的那个版本不再提醒。没有返回 None。"""
    latest = known_newer()
    if latest and app_settings.get("update_dismissed") == latest["version"]:
        return None
    return latest


if __name__ == "__main__":
    print(f"现在的版本：{APP_VERSION}；检查地址：{UPDATE_URL}")
    r = check()
    if not r["ok"]:
        print(f"没查到：{r['error']}")
    elif r["newer"]:
        print(f"发现新版本 {r['latest']['version']}（{r['latest']['date']}）：{r['latest']['page']}")
        for note in r["latest"]["notes"]:
            print(f"  · {note}")
    else:
        print(f"已经是最新版本（服务器上的最新版本是 {r['latest']['version']}）")
