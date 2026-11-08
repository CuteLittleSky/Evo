"""语义化版本比较（只用于通知的 min/maxVersion 过滤）。

接受 `1` / `1.2` / `1.2.3` / `1.2.3.4`，按数字段比较；解析不了的返回 None。
"""
from __future__ import annotations

import re

_RE = re.compile(r"^\d+(\.\d+){0,3}$")


def parse(value) -> tuple[int, ...] | None:
    s = str(value or "").strip()
    if not _RE.match(s):
        return None
    return tuple(int(x) for x in s.split("."))


def compare(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    n = max(len(a), len(b))
    for i in range(n):
        x = a[i] if i < len(a) else 0
        y = b[i] if i < len(b) else 0
        if x > y:
            return 1
        if x < y:
            return -1
    return 0


def in_range(current, min_version, max_version) -> bool:
    """当前版本是否在 [min_version, max_version] 内。

    有任一限制但当前版本解析不了 → 返回 False（宁可不显示，也不给错版本弹）。
    """
    if not min_version and not max_version:
        return True
    cur = parse(current)
    if cur is None:
        return False
    if min_version:
        mn = parse(min_version)
        if mn is not None and compare(cur, mn) < 0:
            return False
    if max_version:
        mx = parse(max_version)
        if mx is not None and compare(cur, mx) > 0:
            return False
    return True
