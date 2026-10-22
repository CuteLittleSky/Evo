"""Evo 派喵云同步 · 和派喵服务器对话（配对 + 上传都用这里）。

服务器根：战绩一族是 https://tools.skycraft.cn/apex/api/battlestats
（cloud/state.py 的 DEFAULT_SERVER），配对一族退回到 https://tools.skycraft.cn/apex/api
（同文件的 pair_server()）。路径都拼在各自的根后面：

    POST /wx/pair/start      无 body
                             → { code, expiresAt }
                             响应头 Set-Cookie: apexpacks_pair=<ticket>   ← 配对票据
    GET  /wx/pair/poll       带 apexpacks_pair cookie
                             → { status: 'pending'|'confirmed'|'expired', expiresAt }
                               确认时 Set-Cookie: apexpacks_sid=<token>   ← 会话令牌
    POST /battlestats/matches  带 Authorization: Bearer <token>
                             → { accepted, skipped, total }

⚠️ 配对三个接口在 /wx/pair 下，**不在** /battlestats/pair 下：服务端
   2026-10-22 把 pair 从 battlestats 移回 wx（ApexPacks c07b909
   "fix(api): pair 三个接口移回 /api/wx/pair，修复小程序配对 404"）。
   改路径前先确认线上真的通，不然界面上只会出现"配对码无效或已过期"。

两个关键点（踩过才知道）：
  · 配对票据和会话令牌**都只通过 Set-Cookie 下发**，响应体里没有。
    所以这里手动收 Set-Cookie、存进本地状态、之后自己拼 Cookie 头发回去。
    不用 http.cookiejar —— 我们只跟一个主机说话、只需要两枚 cookie，
    手写反而更清楚，也免得序列化整个 CookieJar。
  · 令牌既可以用 Authorization 头、也可以放在 cookie 里，上传走前者更直接。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from cloud import state as cloud_state

TIMEOUT = 8.0
MAX_BYTES = 512 * 1024
PAIR_TTL = 120              # 配对码有效期（秒），和服务器下发的 expiresAt 一致

TICKET_COOKIE = "apexpacks_pair"
SESSION_COOKIE = "apexpacks_sid"


class CloudError(RuntimeError):
    """所有网络/协议问题都用它，message 直接可以显示给用户。"""


def server_of(snapshot) -> str:
    return (snapshot.get("server") or cloud_state.default_server()).rstrip("/")


def pair_server_of(snapshot) -> str:
    """配对接口的根（.../apex/api）。见 cloud/state.py 的 pair_server()。"""
    return cloud_state.pair_server(snapshot)


def require_server(snapshot, base=None) -> str:
    """地址是我们自己内置的常量，正常不会不合规；留一道闸防止被环境变量改坏。"""
    base = (base if base is not None else server_of(snapshot)).rstrip("/")
    if not base:
        raise CloudError("没有配置服务器地址")
    if not (base.startswith("http://127.0.0.1") or base.startswith("http://localhost")
            or base.startswith("https://")):
        # 明文 http 只允许本机，其余必须 https —— 传的是玩家数据，不该裸奔
        raise CloudError("服务器地址必须是 https，或者本机的 http://127.0.0.1")
    return base


def _cookie_of(response, name, sent_at):
    """从响应的 Set-Cookie 里挑出指定名字的值。

    urllib 的 msg.get_all('Set-Cookie') 会给出全部行，同一次响应可能设多枚。
    用 sent_at 记下发时刻：cookie 的 maxAge 我们不解析，过期与否由服务器判断
    （它会回 expired），本地只负责原样带回去。
    """
    for raw in response.headers.get_all("Set-Cookie") or []:
        head = raw.split(";", 1)[0]
        key, _, value = head.partition("=")
        if key.strip() == name and value.strip():
            return value.strip()
    return ""


def request(snapshot, path, payload=None, token=None, method="POST", want_cookie=None,
            base=None):
    """发一个请求。want_cookie 给了 cookie 名就把响应里的它抓出来返回。

    base 不给就用 DEFAULT_SERVER（.../battlestats 那一族）；
    配对接口传 pair_server_of(snapshot)（.../apex/api）。

    返回 (解析后的 JSON, 抓到的 cookie 值或 "")。
    """
    base = require_server(snapshot, base)
    headers = {"Content-Type": "application/json", "User-Agent": "Evo-Cloud/0.3"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    ticket = snapshot.get("pair_ticket")
    if ticket:
        headers["Cookie"] = f"{TICKET_COOKIE}={ticket}"
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            raw = response.read(MAX_BYTES + 1)
            cookie = _cookie_of(response, want_cookie, None) if want_cookie else ""
    except urllib.error.HTTPError as exc:
        raise CloudError(_http_message(exc)) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CloudError(f"无法连接派喵服务器（{exc}）") from exc
    if len(raw) > MAX_BYTES:
        raise CloudError("服务器返回的数据过大")
    try:
        return json.loads(raw.decode("utf-8")), cookie
    except ValueError as exc:
        raise CloudError("服务器返回的不是 JSON") from exc


def _http_message(exc):
    """把常见状态码翻译成人话，剩下的带上原文。

    服务器的错误响应体可能用 statusMessage、message 或 error 三种字段名之一，
    所以这里三个都试。
    """
    try:
        body = json.loads(exc.read(4096).decode("utf-8", "replace"))
        detail = str(body.get("statusMessage") or body.get("message")
                     or body.get("error") or "")
    except Exception:
        detail = ""
    known = {
        400: "请求格式不对",
        401: "这台电脑还没配对，或者配对已失效",
        403: "服务器拒绝了这个请求",
        404: "配对码无效或已过期",
        409: "数据有冲突，请稍后重试",
        410: "配对码已过期，请重新生成",
        429: "请求太频繁，稍后再试",
    }
    text = known.get(exc.code) or f"服务器返回 {exc.code}"
    # 服务器给的说明往往和我们的翻译是同一个意思（"这台电脑还没配对"），
    # 原样拼上会变成"还没配对……（还没配对）"，所以重复时只说一遍。
    if detail and (detail in text or text in detail):
        return detail if len(detail) > len(text) else text
    return f"{text}（{detail}）" if detail else text


# ---------- 配对 ----------

def start(snapshot):
    """出一张配对码。返回 6 位码；失败抛 CloudError。"""
    result, ticket = request(snapshot, "/wx/pair/start", {}, want_cookie=TICKET_COOKIE,
                             base=pair_server_of(snapshot))
    code = str(result.get("code") or "")
    pair_ticket = ticket or snapshot.get("pair_ticket") or ""
    if len(code) != 6 or not pair_ticket:
        raise CloudError("服务器返回的配对码不完整")
    snapshot["pair_ticket"] = pair_ticket
    snapshot["pair_code"] = code
    # expiresAt 是服务器给的毫秒时间戳；本地用它算倒计时，比按 PAIR_TTL 猜更准
    expires = result.get("expiresAt")
    if isinstance(expires, (int, float)) and expires > 0:
        snapshot["pair_expires_ms"] = int(expires)
    return code


def poll(snapshot):
    """问一句手机确认了没有。确认了就拿到会话令牌并落盘。

    返回 True 表示"刚刚配对成功"。
    """
    if not snapshot.get("pair_ticket"):
        return False
    result, session = request(snapshot, "/wx/pair/poll", None, method="GET",
                              want_cookie=SESSION_COOKIE, base=pair_server_of(snapshot))
    status = str(result.get("status") or "")
    if status == "expired":
        snapshot["pair_code"] = ""
        snapshot["pair_ticket"] = ""
        snapshot["pair_expires_ms"] = 0
        raise CloudError("配对码已过期，请重新生成")
    if status != "confirmed":
        return False
    token = session or ""
    if not token:
        raise CloudError("服务器没有返回会话令牌")
    snapshot["device"] = {
        "token": token,
        "name": "这台电脑",
        "expires_text": _expires_text(result.get("expiresAt")),
    }
    snapshot["enabled"] = True
    for key in ("pair_code", "pair_ticket", "pair_expires_ms"):
        snapshot.pop(key, None)
    return True


def _expires_text(value):
    """把毫秒时间戳转成"2027-01-19 到期"这种人话；看不懂就留空。"""
    try:
        import datetime as _dt
        stamp = int(value) / 1000
        return _dt.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d") + " 到期"
    except Exception:
        return ""


# ---------- 上传 ----------

def upload(snapshot, payload):
    """传一批对局。返回服务器回的统计。"""
    token = (snapshot.get("device") or {}).get("token")
    if not token:
        raise CloudError("这台电脑还没配对")
    result, _ = request(snapshot, "/battlestats/matches", payload, token=token)
    return result


def upload_rank(snapshot, payload):
    """传一批大厅读数（段位曲线）。返回服务器回的统计。"""
    token = (snapshot.get("device") or {}).get("token")
    if not token:
        raise CloudError("这台电脑还没配对")
    result, _ = request(snapshot, "/battlestats/rank", payload, token=token)
    return result


# ---------- 取回 ----------

READ_BATCH = 200          # 服务器单次返回上限（见 server/utils/battlestats-matches.ts 的 clamp）


def download(snapshot, limit=READ_BATCH):
    """从云端取回自己的对局，时间倒序。返回 {items: [...]}。

    只读接口：服务器按 openid 过滤，我们这边只是把已经传上去的拿回来。
    走 GET，所以 request() 的 payload 传 None（否则会被当成 POST 发出去）。
    """
    token = (snapshot.get("device") or {}).get("token")
    if not token:
        raise CloudError("这台电脑还没配对")
    size = max(1, min(int(limit or READ_BATCH), READ_BATCH))
    result, _ = request(snapshot, f"/battlestats/matches?limit={size}", None,
                        token=token, method="GET")
    items = result.get("matches") if isinstance(result, dict) else None
    if not isinstance(items, list):
        items = []
    return {"items": items}


def download_rank(snapshot, limit=READ_BATCH):
    """从云端取回段位曲线（大厅读数）。"""
    token = (snapshot.get("device") or {}).get("token")
    if not token:
        raise CloudError("这台电脑还没配对")
    size = max(1, min(int(limit or READ_BATCH), READ_BATCH))
    result, _ = request(snapshot, f"/battlestats/rank?limit={size}", None,
                        token=token, method="GET")
    items = result.get("points") if isinstance(result, dict) else None
    if not isinstance(items, list):
        items = []
    return {"items": items}
