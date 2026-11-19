"""联网出错时写清原因，和国内备用线路的入口（2026-11-19 用户决定加；起因见 docs/底座.md"有的国内网络连不上 evotracker.cn"）。

1. 出错的原因写清楚：一句短的中文，分得清是连接被重置、超时、查不到地址、证书不对，还是服务器回了错误代码。
   以前只写"网络不通，或者服务器暂时不可用"，查玩家日志时看不出是哪种。检查新版本、公告、匿名统计、地图轮换、
   全体玩家、赛事数据都用它：describe(e) → "连接被重置"；labeled(url, e) → "dl.evotracker.cn：连接被重置"
2. 国内备用线路的入口：有的国内网络按域名拦了 evotracker.cn，公告、赛程、地图轮换、全体玩家都取不到。
   get(name, primary, official) 先照旧连 evotracker.cn，连不上再经派圈助手的云环境取同一个文件（cloud_relay.py）。
   cloud_relay.py 和云同步一样只在开发者电脑上、不上传 GitHub：GitHub 上的 Evo 没有它，就只连 evotracker.cn，别的照旧。
   official 是 False（测试、预览把地址换成了别的）时也只连那个地址，绝不走备用线路

primary() 的规矩：从 evotracker.cn 取，返回内容（bytes）；连不上（重置、超时、查不到地址、证书不对、服务器 5xx）
抛 Unreachable，原因用 labeled() 写好；服务器回的 4xx、格式不对这些照原样抛（换条线路也一样，不走备用）。
get() 返回 (内容, 从哪取的：DIRECT / RELAY, 内容有多旧：秒，直连是 0)；两条线路都不行抛 Unreachable，说的是两边各自的原因。
"""

import socket
import ssl
import urllib.error
from urllib.parse import urlparse

DIRECT, RELAY = "direct", "relay"
COS_SUFFIX = ".myqcloud.com"


class Unreachable(OSError):
    """连不上（说的是原因，已经带了地址）。是 OSError，原来接 OSError 的地方照样接得住。"""


def describe(e):
    """把联网时的异常说成一句短的中文。认不出的就用异常自己的话（最多 80 个字）。"""
    if isinstance(e, Unreachable):
        return str(e)
    if isinstance(e, urllib.error.HTTPError):
        return f"服务器回了 HTTP {e.code}"
    if isinstance(e, urllib.error.URLError) and isinstance(e.reason, BaseException):
        return describe(e.reason)
    if isinstance(e, ssl.SSLCertVerificationError):
        return f"证书不对（{e.verify_message or e.reason or e}）"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "超时"
    if isinstance(e, ConnectionResetError):
        return "连接被重置"
    if isinstance(e, ConnectionAbortedError):
        return "连接被中止"
    if isinstance(e, ConnectionRefusedError):
        return "连接被拒绝"
    if isinstance(e, socket.gaierror):
        return "查不到服务器的地址（DNS）"
    if isinstance(e, ssl.SSLError):
        return "加密连接出错" + (f"（{e.reason}）" if getattr(e, "reason", None) else "")
    text = str(getattr(e, "reason", None) or e).strip()
    if "timed out" in text:
        return "超时"
    return text[:80] or type(e).__name__


def host_label(url):
    """地址的简称：COS 的存储桶写"COS"，别的写主机名。"""
    host = urlparse(url).hostname or url
    return "COS" if host.endswith(COS_SUFFIX) else host


def labeled(url, e):
    return f"{host_label(url)}：{describe(e)}"


def unreachable(url, e):
    """primary() 里用：这个错误算不算"连不上"。是就返回 Unreachable（带上地址和原因），不是（4xx）返回 None。"""
    if isinstance(e, urllib.error.HTTPError) and e.code < 500:
        return None
    return Unreachable(labeled(url, e))


def _relay():
    try:
        import cloud_relay  # 只在开发者电脑上（国内备用线路），GitHub 上没有
    except ImportError:
        return None
    return cloud_relay


def get(name, primary, official=True):
    """取一个公开文件（name：notice / esports / maps / summary）。见文件开头。"""
    relay = _relay() if official else None
    if relay is None:
        return primary(), DIRECT, 0
    return relay.get(name, primary)
