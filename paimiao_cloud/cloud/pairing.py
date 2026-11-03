from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from cloud import state as cloud_state

TIMEOUT = 8.0
MAX_BYTES = 512 * 1024
PAIR_TTL = 120

TICKET_COOKIE = "apexpacks_pair"
SESSION_COOKIE = "apexpacks_sid"

AUDIENCE = "evo"

class CloudError(RuntimeError):
    pass

def server_of(snapshot) -> str:
    return (snapshot.get("server") or cloud_state.default_server()).rstrip("/")

def pair_server_of(snapshot) -> str:

    return cloud_state.pair_server(snapshot)

def require_server(snapshot, base=None) -> str:

    base = (base if base is not None else server_of(snapshot)).rstrip("/")
    if not base:
        raise CloudError("没有配置服务器地址")
    if not (base.startswith("http://127.0.0.1") or base.startswith("http://localhost")
            or base.startswith("https://")):

        raise CloudError("服务器地址必须是 https，或者本机的 http://127.0.0.1")
    return base

def _cookie_of(response, name, sent_at):

    for raw in response.headers.get_all("Set-Cookie") or []:
        head = raw.split(";", 1)[0]
        key, _, value = head.partition("=")
        if key.strip() == name and value.strip():
            return value.strip()
    return ""

def _is_cert_error(exc):
    """判断是不是证书类错误（urllib 会把 ssl.SSLError 包在 URLError.reason 里）。"""
    seen = [exc]
    reason = getattr(exc, "reason", None)
    if reason is not None and reason is not exc:
        seen.append(reason)
    text = " ".join(repr(item) for item in seen).upper()
    return "SSL" in text or "CERTIFICATE" in text


def _cert_diag(url):
    """证书校验失败时，再用不校验证书的方式连一次，把对端证书信息和本机时间记下来。
    这样能区分三种情况：服务器证书真的过期、本机时间不对、被代理/中间人替换了证书。"""
    import datetime as _dt
    import socket
    import ssl
    import tempfile
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or 443
    now = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    out = [f"本机时间={now}"]
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                pem = ssl.DER_cert_to_PEM_cert(tls.getpeercert(binary_form=True))
        handle, path = tempfile.mkstemp(suffix=".pem")
        try:
            with os.fdopen(handle, "w", encoding="ascii") as stream:
                stream.write(pem)
            decoded = ssl._ssl._test_decode_cert(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        out.append(f"对端证书 subject={decoded.get('subject')}")
        out.append(f"issuer={decoded.get('issuer')}")
        out.append(f"notBefore={decoded.get('notBefore')} notAfter={decoded.get('notAfter')}")
    except Exception as exc:
        out.append(f"抓取对端证书失败 {type(exc).__name__}: {exc}")
    return " ｜ ".join(out)


def _describe_failure(method, url, exc):
    """把一次网络请求失败写进 cloud.log（配对失败原来只弹提示、不落日志，这里补上）。
    证书类错误额外附带对端证书有效期，方便事后判断是谁的问题。"""
    kind = type(exc).__name__
    detail = f"{kind}: {exc!r}"
    reason = getattr(exc, "reason", None)
    if reason is not None and reason is not exc:
        detail += f" ｜ reason={type(reason).__name__}: {reason!r}"
    line = f"云同步请求失败 [{method} {url}] {detail}"
    if _is_cert_error(exc):
        line += " ｜ " + _cert_diag(url)
    try:
        cloud_state.log(line)
    except Exception:
        pass


def _connect_message(exc):
    """给玩家看的连接失败提示。证书类错误直接教他去校准时间 / 关代理。"""
    if _is_cert_error(exc):
        return (f"无法连接派喵服务器（证书校验失败：{exc}）。"
                "请先把 Windows 时间设为自动同步、并关闭代理/VPN 后重试")
    return f"无法连接派喵服务器（{exc}）"


def request(snapshot, path, payload=None, token=None, method="POST", want_cookie=None,
            base=None):

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
    url = base + path
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            raw = response.read(MAX_BYTES + 1)
            cookie = _cookie_of(response, want_cookie, None) if want_cookie else ""
    except urllib.error.HTTPError as exc:
        _describe_failure(method, url, exc)
        raise CloudError(_http_message(exc)) from exc
    except (urllib.error.URLError, OSError) as exc:
        _describe_failure(method, url, exc)
        raise CloudError(_connect_message(exc)) from exc
    if len(raw) > MAX_BYTES:
        cloud_state.log(f"云同步请求失败 [{method} {url}] 服务器返回 {len(raw)} 字节，超过上限")
        raise CloudError("服务器返回的数据过大")
    try:
        return json.loads(raw.decode("utf-8")), cookie
    except ValueError as exc:
        cloud_state.log(f"云同步请求失败 [{method} {url}] 返回内容不是 JSON：{raw[:200]!r}")
        raise CloudError("服务器返回的不是 JSON") from exc

def _http_message(exc):

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
    if detail and (detail in text or text in detail):
        return detail if len(detail) > len(text) else text
    return f"{text}（{detail}）" if detail else text

def start(snapshot):

    result, ticket = request(snapshot, "/wx/pair/start", {"audience": AUDIENCE},
                             want_cookie=TICKET_COOKIE, base=pair_server_of(snapshot))
    code = str(result.get("code") or "")
    pair_ticket = ticket or snapshot.get("pair_ticket") or ""
    if len(code) != 6 or not pair_ticket:
        raise CloudError("服务器返回的配对码不完整")
    got = str(result.get("audience") or AUDIENCE)
    if got != AUDIENCE:
        raise CloudError(f"服务器把这张码当成了 {got} 端，请更新 Evo 后重试")
    snapshot["pair_ticket"] = pair_ticket
    snapshot["pair_code"] = code
    snapshot["pair_qr_ticket"] = str(result.get("qrTicket") or "")
    snapshot["pair_qr_url"] = str(result.get("qrUrl") or "")
    snapshot["pair_mini_code"] = str(result.get("miniCode") or "")
    snapshot["pair_mini_code_mime"] = str(result.get("miniCodeMime") or "")
    snapshot["pair_qr_revoked"] = False

    expires = result.get("expiresAt")
    if isinstance(expires, (int, float)) and expires > 0:
        snapshot["pair_expires_ms"] = int(expires)
    cloud_state.log(f"已生成配对码（{code}，服务器={pair_server_of(snapshot)}）")
    return code

def revoke_qr(snapshot):

    if not snapshot.get("pair_ticket") or snapshot.get("pair_qr_revoked"):
        return False
    try:
        request(snapshot, "/wx/pair/qr", None, method="DELETE",
                base=pair_server_of(snapshot))
    except CloudError:
        return False
    snapshot["pair_qr_revoked"] = True
    snapshot["pair_qr_ticket"] = ""
    snapshot["pair_qr_url"] = ""
    snapshot["pair_mini_code"] = ""
    return True

def poll(snapshot):

    if not snapshot.get("pair_ticket"):
        return False
    result, session = request(snapshot, "/wx/pair/poll", None, method="GET",
                              want_cookie=SESSION_COOKIE, base=pair_server_of(snapshot))
    status = str(result.get("status") or "")
    if status == "expired":
        snapshot["pair_code"] = ""
        snapshot["pair_ticket"] = ""
        snapshot["pair_qr_ticket"] = ""
        snapshot["pair_qr_url"] = ""
        snapshot["pair_mini_code"] = ""
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
    for key in ("pair_code", "pair_ticket", "pair_expires_ms",
                "pair_qr_ticket", "pair_qr_url", "pair_mini_code",
                "pair_mini_code_mime", "pair_qr_revoked"):
        snapshot.pop(key, None)
    return True

def _expires_text(value):

    try:
        import datetime as _dt
        stamp = int(value) / 1000
        return _dt.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d") + " 到期"
    except Exception:
        return ""

def upload(snapshot, payload):

    token = (snapshot.get("device") or {}).get("token")
    if not token:
        raise CloudError("这台电脑还没配对")
    result, _ = request(snapshot, "/matches", payload, token=token)
    return result

def upload_rank(snapshot, payload):

    token = (snapshot.get("device") or {}).get("token")
    if not token:
        raise CloudError("这台电脑还没配对")
    result, _ = request(snapshot, "/rank", payload, token=token)
    return result

def upload_weapons(snapshot, payload):

    token = (snapshot.get("device") or {}).get("token")
    if not token:
        raise CloudError("这台电脑还没配对")
    result, _ = request(snapshot, "/weapon-holds", payload, token=token)
    return result

def upload_seasons(snapshot, payload):
    """上传赛季聚合（派喵服务器 `POST /apex/api/battlestats/seasons`）。"""

    token = (snapshot.get("device") or {}).get("token")
    if not token:
        raise CloudError("这台电脑还没配对")
    result, _ = request(snapshot, "/seasons", payload, token=token)
    return result

READ_BATCH = 200

def download(snapshot, limit=READ_BATCH):

    token = (snapshot.get("device") or {}).get("token")
    if not token:
        raise CloudError("这台电脑还没配对")
    size = max(1, min(int(limit or READ_BATCH), READ_BATCH))
    result, _ = request(snapshot, f"/matches?limit={size}", None,
                        token=token, method="GET")
    items = result.get("matches") if isinstance(result, dict) else None
    if not isinstance(items, list):
        items = []
    return {"items": items}

def download_rank(snapshot, limit=READ_BATCH):

    token = (snapshot.get("device") or {}).get("token")
    if not token:
        raise CloudError("这台电脑还没配对")
    size = max(1, min(int(limit or READ_BATCH), READ_BATCH))
    result, _ = request(snapshot, f"/rank?limit={size}", None,
                        token=token, method="GET")
    items = result.get("points") if isinstance(result, dict) else None
    if not isinstance(items, list):
        items = []
    return {"items": items}
