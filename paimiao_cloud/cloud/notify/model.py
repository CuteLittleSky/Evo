"""通知数据模型与清洗（防 RCE 的核心）。

服务端下发的一切都只当**纯文本数据**处理：
  · 文本去掉控制字符并截断；
  · 动作只认白名单类型（page / update / web）；
  · page/update 的目标必须是单个 `/` 开头的站内路径（挡掉 //host、javascript:、反斜杠、协议）；
  · web 只接受 http/https，且不能带账号密码。
任何字段都不会被 eval/exec —— 渲染时还会再做 html 转义。
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

LEVELS = ("info", "success", "warning", "error")
ACTION_TYPES = ("page", "update", "web")

_VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")


def _text(value, max_len: int) -> str:
    s = str(value or "")
    s = "".join(" " if (ord(c) < 32 or ord(c) == 127) else c for c in s)
    return s.strip()[:max_len]


def _version(value):
    s = str(value or "").strip()
    return s if _VERSION_RE.match(s) else ""


def safe_internal_path(value):
    # 先剔除控制字符再做白名单校验：浏览器解析 URL 时会删掉换行/制表符，若直接放行
    # "/\n/evil.example.com" 就会变成协议相对地址 //evil.example.com 跳到站外（挡 //host 失效）。
    s = "".join(c for c in str(value or "") if ord(c) >= 32 and ord(c) != 127).strip()[:300]
    if not s or not s.startswith("/") or s.startswith("//") or "\\" in s or ":" in s:
        return None
    return s


def safe_http_url(value):
    s = str(value or "").strip()[:500]
    if not s:
        return None
    try:
        parts = urlsplit(s)
    except Exception:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    if parts.username or parts.password:
        return None
    if not parts.hostname:
        return None
    return s


def _normalize_action(raw):
    if not isinstance(raw, dict):
        return None
    label = _text(raw.get("label"), 20)
    if not label:
        return None
    kind = str(raw.get("type") or "").strip()
    if kind not in ACTION_TYPES:
        return None
    value = ""
    if kind == "page":
        value = safe_internal_path(raw.get("value")) or ""
        if not value:
            return None
    elif kind == "update":
        value = safe_internal_path(raw.get("value")) or "/settings"
    elif kind == "web":
        value = safe_http_url(raw.get("value")) or ""
        if not value:
            return None
    return {"label": label, "type": kind, "value": value}


def normalize_item(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    title = _text(raw.get("title"), 80)
    body = _text(raw.get("body"), 500)
    if not title and not body:
        return None
    level = str(raw.get("level") or "info").strip()
    if level not in LEVELS:
        level = "info"
    try:
        priority = int(raw.get("priority") or 0)
    except (TypeError, ValueError):
        priority = 0
    priority = max(0, min(999, priority))

    actions = []
    raw_actions = raw.get("actions")
    if isinstance(raw_actions, list):
        for a in raw_actions[:4]:
            n = _normalize_action(a)
            if n:
                actions.append(n)

    notice_id = _text(raw.get("id"), 60)
    if not notice_id:
        return None
    return {
        "id": notice_id,
        "title": title,
        "body": body,
        "level": level,
        "priority": priority,
        "windowsNotify": bool(raw.get("windowsNotify", True)),
        "silent": bool(raw.get("silent", False)),
        "minVersion": _version(raw.get("minVersion")),
        "maxVersion": _version(raw.get("maxVersion")),
        "actions": actions,
    }
