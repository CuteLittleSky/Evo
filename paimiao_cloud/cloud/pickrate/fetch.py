"""从派喵的「传奇选取率」接口取一份数据（只读，不写任何东西回服务端）。

## 接口

    GET {pair_server}/evo/pick-rate?tier=all
    Authorization: Bearer <device.token>

对应 ApexPacks 仓库 `server/api/evo/pick-rate.get.ts`，响应形状：

    {"success": true,
     "tier": "all", "tierLabel": "全部段位", "updatedAt": 1761...（毫秒）,
     "total": 28,
     "roles": [...],                      # 我们不用，见下
     "legends": [{"rank":1,"name":"Wraith","nameZh":"恶灵","pickRate":12.3,
                  "trend":0.4,"trendDir":"up","avgRank":"15","portrait":"...",
                  "roleKey":"skirmisher","roleLabel":"游击","roleColor":"#e0912f",
                  "winRate":11.8,"kpm":5.2}, ...]}

**只依赖 `success` / `updatedAt` / `legends[]` 这三样**。`roles[]` / `total` 一律不读 ——
服务端那两组是给小程序和网页画的，字段名以后可能动，我们不该跟着动。
`winRate` / `kpm` 可能整列为 `null`（数据源没取到），模板按「全 null 就隐藏该列」处理。

## 缓存（用户 2026-10-25 定：30 分钟）

服务端自己就有 5 分钟缓存 + 每分钟最多刷一次，我们这边 30 分钟够用：选取率是按天变的，
面板开一次不该去打一次接口。

两级缓存：
- 内存：同一个 Evo 进程里反复翻页不重复请求
- 磁盘：`<数据目录>/cloud-sync/pickrate-<tier>.json`。**断电重启后第一屏也有东西看**，
  并且网络不通时还能显示上一次的数据（页面上标「上次的」+ 时间）

## 失败退避

请求失败后一段时间内不再重试，免得每次翻页都卡 5 秒等超时：
鉴权失败（401/403）退避 1 小时 —— 令牌没换之前重试也没用；
其余（断网、超时、5xx）退避 10 分钟。退避期间仍然返回磁盘上的旧数据。

## 依赖

只用标准库 `urllib`（和 `cloud/lobby_mode/map_lookup.py`、`cloud/pairing.py` 一样），
**不新增依赖**、不占常驻内存：数据只有几十 KB，且只在打开面板那一刻取。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# 接口路径（挂在配对根下面，与 /battlestats、/maprotation 同级）
API_PATH = "/evo/pick-rate"
TIMEOUT = 5.0
# 同一份数据用多久（秒）。用户 2026-10-25 定 30 分钟。
TTL = 30 * 60.0
# 少于此数就当「响应不完整」，不拿去渲染（服务端 MIN_LEGEND_ROWS 也是 20）
MIN_ROWS = 20

# 8 个段位档（服务端 server/utils/legends-pickrate.ts 的 TIERS，键要与那边逐字一致，
# 大小写混写是服务端原样，别「顺手规范成小写」）。
# 留一份在本地的原因：**首屏还没拿到任何响应时也要把 8 个 tab 画出来**，
# 否则点了切换页面才出现按钮，会跳。
TIERS = (
    ("all", "全部段位"),
    ("Unranked", "非排位/菜鸟"),
    ("Bronze", "青铜"),
    ("Silver", "白银"),
    ("Gold", "黄金"),
    ("Platinum", "铂金"),
    ("Diamond", "钻石"),
    ("Masterpred", "大师/猎杀"),
)
TIER_KEYS = tuple(key for key, _ in TIERS)
DEFAULT_TIER = "all"  # 用户 2026-10-25 定：默认「全部段位」，可以切换

# 鉴权失败的退避时长 / 其他失败的退避时长（秒）
BACKOFF_AUTH = 3600.0
BACKOFF_OTHER = 600.0


class Unavailable(Exception):
    """取不到。页面上会显示原因，不猜数据。"""


class AuthExpired(Unavailable):
    """令牌失效。单独一类，好让页面说「去重新配对」而不是「网络不好」。"""


# tier -> {"at": float(monotonic), "data": dict}
_memory: dict[str, dict] = {}
# tier -> {"until": float(monotonic), "reason": str}
_backoff: dict[str, dict] = {}


# ---------------------------------------------------------------- 前置条件


def unavailable_reason():
    """现在还不能取数据的原因；能取返回 None。

    和 `cloud/sync.py`、`map_lookup.enabled()` 用的是同一套判断，
    但这里**不要求云同步开关打开**：读榜不等于上传，用户关掉上传也该能看榜。
    """
    try:
        from cloud import state as cloud_state  # noqa: PLC0415

        snapshot = cloud_state.load()
        if not cloud_state.agreed(snapshot):
            return "先去「派喵云同步」同意数据传输指引"
        if not cloud_state.paired(snapshot):
            return "先去「派喵云同步」完成设备配对"
        return None
    except Exception as exc:  # 注入层自己出问题时也当「没准备好」
        return "云同步状态读不出来：%s" % exc


def _device_token():
    from cloud import state as cloud_state  # noqa: PLC0415

    token = (cloud_state.load().get("device") or {}).get("token")
    return str(token or "").strip()


def _api_url(tier):
    from cloud import state as cloud_state  # noqa: PLC0415

    root = cloud_state.pair_server({})  # .../apex/api
    return "%s%s?tier=%s" % (root.rstrip("/"), API_PATH, tier)


def _headers():
    headers = {
        "User-Agent": "EvoTracker/pickrate",
        "Accept": "application/json",
    }
    token = _device_token()
    if token:
        headers["Authorization"] = "Bearer %s" % token
    return headers


# ---------------------------------------------------------------- 磁盘缓存


def _cache_dir():
    try:
        from cloud import state as cloud_state  # noqa: PLC0415

        return cloud_state.data_dir()
    except Exception:
        return None


def _cache_file(tier):
    folder = _cache_dir()
    if folder is None:
        return None
    return Path(folder) / ("pickrate-%s.json" % tier)


def _read_disk(tier):
    path = _cache_file(tier)
    if path is None:
        return None
    try:
        with path.open("r", encoding="utf-8") as fp:
            blob = json.load(fp)
    except Exception:
        return None
    data = blob.get("data") if isinstance(blob, dict) else None
    if not _usable(data):
        return None
    return {"at_wall": float(blob.get("at") or 0.0), "data": data}


def _write_disk(tier, data, at_wall):
    path = _cache_file(tier)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".pickrate-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump({"at": at_wall, "data": data}, fp, ensure_ascii=False)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:
        # 磁盘写不进去不影响这次显示，只是下次要重新请求
        pass


# ---------------------------------------------------------------- 校验


def _usable(data):
    """响应能不能拿去渲染。宁可不显示，也不显示半张表。"""
    if not isinstance(data, dict):
        return False
    if data.get("success") is not True:
        return False
    legends = data.get("legends")
    if not isinstance(legends, list) or len(legends) < MIN_ROWS:
        return False
    for item in legends:
        if not isinstance(item, dict):
            return False
        if not isinstance(item.get("pickRate"), (int, float)):
            return False
    return True


# ---------------------------------------------------------------- 取数


def _request(tier):
    """真正发一次请求。抛 Unavailable / AuthExpired。"""
    req = urllib.request.Request(_api_url(tier), headers=_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AuthExpired("令牌已失效或被拒（HTTP %s），请重新配对" % exc.code) from exc
        raise Unavailable("接口返回 HTTP %s" % exc.code) from exc
    except Exception as exc:
        raise Unavailable("连不上派喵服务器：%s" % exc) from exc
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise Unavailable("响应不是合法 JSON：%s" % exc) from exc

    if isinstance(data, dict) and data.get("success") is False:
        raise Unavailable(str(data.get("message") or "服务端说取不到"))
    if not _usable(data):
        raise Unavailable("响应不完整（传奇行数不足或字段缺失）")
    return data


def _cooling(tier, now):
    entry = _backoff.get(tier)
    if not entry:
        return None
    if now >= entry["until"]:
        _backoff.pop(tier, None)
        return None
    return entry["reason"]


def _cool(tier, now, reason, auth):
    _backoff[tier] = {
        "until": now + (BACKOFF_AUTH if auth else BACKOFF_OTHER),
        "reason": reason,
    }


def snapshot(tier, force=False):
    """取某个段位档的数据。

    返回 {"legends": [...], "updated_at": int(毫秒), "stale": bool,
          "error": str|None, "fetched_at": float(毫秒)}；
    一份都取不到（内存、磁盘都没有、请求也失败）时抛 `Unavailable`。
    """
    if tier not in TIER_KEYS:
        tier = DEFAULT_TIER
    now = time.monotonic()

    hit = _memory.get(tier)
    if hit is not None and not force and now - hit["at"] < TTL:
        return _view(hit["data"], stale=False, error=None, at=hit["at_wall"])

    # 退避期内不重试，直接给能拿到的旧数据
    cooling = None if force else _cooling(tier, now)
    if cooling:
        stale = hit or _read_disk(tier)
        if stale:
            return _view(stale["data"], stale=True, error=cooling, at=stale["at_wall"])
        raise Unavailable(cooling)

    try:
        data = _request(tier)
    except Unavailable as exc:
        _cool(tier, now, str(exc), auth=isinstance(exc, AuthExpired))
        stale = hit or _read_disk(tier)
        if stale:
            return _view(stale["data"], stale=True, error=str(exc), at=stale["at_wall"])
        raise

    at_wall = time.time()
    _memory[tier] = {"at": now, "at_wall": at_wall, "data": data}
    _backoff.pop(tier, None)
    _write_disk(tier, data, at_wall)
    return _view(data, stale=False, error=None, at=at_wall)


def _view(data, stale, error, at):
    return {
        "legends": data.get("legends") or [],
        "updated_at": data.get("updatedAt") or 0,
        "stale": stale,
        "error": error,
        "fetched_at": (at or 0.0) * 1000.0,
    }


def clear_cache(tier=None):
    """清掉内存缓存（磁盘那份留着，它本来就有时间戳）。自检用。"""
    if tier is None:
        _memory.clear()
        _backoff.clear()
    else:
        _memory.pop(tier, None)
        _backoff.pop(tier, None)
