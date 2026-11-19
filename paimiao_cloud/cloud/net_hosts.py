"""派喵 API 线路（主 + 备用）与自动回退。

背景：
  Evo 的派喵相关联网（地图轮换、云同步上传、通知、选取率、配对等）都打
  `tools.skycraft.cn`。海外 CDN 对部分大陆用户不稳，故增加备用线路：
      · 主：tools.skycraft.cn
      · 备：tools1.skycraft.cn、tools3.skycraft.cn

策略（与小程序端一致）：
  · 默认**永远先用主线路**；只有主线路确认不可达才换线；
  · 换线时先切到第一个备用保证马上可用，同时对备用测延迟，取可达里延迟最低的；
  · 选择只写本机数据目录 `cloud-sync/net.json`，不参与云同步、不影响账号数据。

约束（AGENT_RULES）：
  · 本模块属 paimiao_cloud，不改上游任何文件；
  · 任何探测/读写失败都静默降级（保底主线路），绝不能让 Evo 起不来；
  · 中文注释、4 空格、snake_case。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path

PRIMARY = "https://tools.skycraft.cn"
BACKUPS = ["https://tools1.skycraft.cn", "https://tools3.skycraft.cn"]
ALL = [PRIMARY] + BACKUPS

API_SUFFIX = "/apex/api"
PROBE_PATH = "/apex/api/maprotation"
PROBE_TIMEOUT = 6.0
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Evo-Tracker/0.4"

_lock = threading.Lock()
_active: str | None = None


# ---------------------------------------------------------------------------
# 本机数据目录（与 cloud.state 一致；lazy import 避免循环依赖）
# ---------------------------------------------------------------------------

def _data_dir() -> Path:
    try:
        from cloud import state as cloud_state  # noqa: PLC0415

        return cloud_state.data_dir()
    except Exception:
        base = os.environ.get("EVO_DATA_DIR")
        root = Path(base) if base else Path(__file__).resolve().parent.parent.parent / "data"
        return root / "cloud-sync"


def _cfg_path() -> Path:
    return _data_dir() / "net.json"


# ---------------------------------------------------------------------------
# 当前线路
# ---------------------------------------------------------------------------

def active_origin() -> str:
    """当前生效的 origin（含 scheme，不带 /apex/api）。默认主线路。"""
    global _active
    if _active:
        return _active
    env = os.environ.get("EVO_API_HOST")
    if env:
        _active = env.rstrip("/")
        return _active
    try:
        raw = json.loads(_cfg_path().read_text(encoding="utf-8"))
        host = str(raw.get("origin") or "").rstrip("/")
        if host in ALL:
            _active = host
            return _active
    except Exception:
        pass
    _active = PRIMARY
    return _active


def set_active_origin(origin: str) -> None:
    """切换并持久化到本机（仅本地文件，不云同步）。"""
    global _active
    origin = (origin or "").rstrip("/")
    if origin not in ALL:
        return
    _active = origin
    try:
        path = _cfg_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"origin": origin}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def api_root() -> str:
    """当前 API 根：`<origin>/apex/api`。"""
    return active_origin().rstrip("/") + API_SUFFIX


def asset_origin() -> str:
    """当前静态资源（图片等）origin。"""
    return active_origin()


def resolve(url: str) -> str:
    """把 URL 里写死的 tools*/ 主机换成当前生效 origin（让常量跟随线路）。"""
    if not url:
        return url
    for host in ALL:
        if url.startswith(host):
            return active_origin() + url[len(host):]
    return url


def _origin_of(url: str) -> str | None:
    for host in ALL:
        if url.startswith(host):
            return host
    return None


def urlopen_with_failover(request, timeout: float):
    """发一次请求；网络失败时确认并切到备用线路，用同路径重试一次。

    返回值就是 urllib 的 response（可直接 `with ... as resp:`）。
    任何失败都仍会 raise（保持调用方原有异常处理），只是先尝试过回退。
    """
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except Exception:
        try:
            full_url = getattr(request, "full_url", "") or ""
            on_network_error(_origin_of(full_url))
            new_url = resolve(full_url)
            if new_url and new_url != full_url:
                headers = dict(request.header_items()) if hasattr(request, "header_items") else {}
                method = request.get_method() if hasattr(request, "get_method") else "GET"
                retry = urllib.request.Request(
                    new_url, data=getattr(request, "data", None), headers=headers, method=method)
                return urllib.request.urlopen(retry, timeout=timeout)
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# 探测与自动回退
# ---------------------------------------------------------------------------

def probe(origin: str, timeout: float = PROBE_TIMEOUT) -> dict:
    """测某个 origin 的延迟。返回 {"ok": bool, "ms": int}；失败也算耗时。"""
    url = origin.rstrip("/") + PROBE_PATH + "?ping=%d" % int(time.time() * 1000)
    start = time.monotonic()
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            ok = 200 <= int(getattr(response, "status", 200)) < 300
        return {"ok": ok, "ms": int((time.monotonic() - start) * 1000)}
    except Exception:
        return {"ok": False, "ms": int((time.monotonic() - start) * 1000)}


def _pick_lowest_backup(exclude: str) -> str:
    """对除 exclude 外的备用测延迟，返回可达里延迟最低的；都不可达返回 ''。"""
    candidates = [h for h in ALL if h not in (exclude, PRIMARY)] or [h for h in ALL if h != exclude]
    results: list[tuple[int, str]] = []
    for host in candidates:
        r = probe(host)
        if r.get("ok"):
            results.append((int(r.get("ms") or 0), host))
    results.sort(key=lambda item: item[0])
    return results[0][1] if results else ""


def on_network_error(failed_origin: str | None = None) -> str:
    """某个请求网络失败后调用：确认当前线路确实不可达才切，返回（可能新的）生效 origin。

    规则：先确认 failed_origin 真不可达（偶发抖动不切）→ 先切第一个备用 →
    再对备用测延迟，取可达里最低的。
    设置了 EVO_API_HOST 时视为用户强制指定，不自动切换。
    """
    if os.environ.get("EVO_API_HOST"):
        return active_origin()
    with _lock:
        origin = (failed_origin or active_origin()).rstrip("/")
        # 先确认 origin 本身是否真的不可达（能探通说明只是偶发抖动）
        if probe(origin).get("ok"):
            return active_origin()
        backups = [h for h in ALL if h != origin]
        if not backups:
            return active_origin()
        # 先切第一个备用，保证马上可用
        set_active_origin(backups[0])
        # 再择优：可达的备用里延迟最低的
        best = _pick_lowest_backup(exclude=origin)
        if best:
            set_active_origin(best)
        return active_origin()


def reset_for_tests() -> None:
    """仅供测试：清掉内存里的 active 缓存。"""
    global _active
    _active = None
