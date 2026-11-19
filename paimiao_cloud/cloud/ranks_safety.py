"""段位名兜底：数据库里可能有非简体段位名（如英文 Silver / Unranked，旧数据或英文界面遗留）。

上游 `ranks.has_divisions(tier)` 直接 `_BY_NAME[tier]`，遇到不认识的段位就 KeyError。
面板打开时 `panel_app.db()` 会跑大厅校准补全（calibration.fill_ranks），记录程序开始时也会跑，
于是这种脏数据会把面板打成 500、或让记录子进程起不来。

这里只给 `has_divisions` 包一层：认识的段位行为完全不变，不认识的返回 False。
不改上游文件；bootstrap.install() 在进程启动时调用（托盘、面板、记录子进程都会走到）。
"""

from __future__ import annotations

_installed = False


def install() -> bool:
    global _installed
    if _installed:
        return True
    try:
        import ranks  # noqa: PLC0415 上游模块
    except Exception:
        return False
    if getattr(ranks.has_divisions, "_paimiao_safe", False):
        _installed = True
        return True
    original = ranks.has_divisions

    def has_divisions(tier):
        try:
            return original(tier)
        except (KeyError, TypeError):
            return False

    has_divisions._paimiao_safe = True  # type: ignore[attr-defined]
    ranks.has_divisions = has_divisions
    _installed = True
    return True
