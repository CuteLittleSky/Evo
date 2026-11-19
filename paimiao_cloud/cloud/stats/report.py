"""派喵匿名统计上报（未绑定派喵云的用户）。

设计要点（见 docs/Evo默认匿名统计方案.md）：
- 采集复用 cloud.sync 的只读读取函数（read_matches / read_weapon_holds / payload_of）。
- 只去掉能定位到人的字段（游戏名、玩家ID、队友名字/ID），保留匿名编号 + Evo 版本号。
- 匿名编号 = SHA-256(本地随机 secret)[:16]：固定不变、16 位十六进制、不含账号/设备标识。
- 一局入库后延迟约 60 秒、合并这一分钟内的局再上报（去抖），不做每小时定时。
- 门控：已绑定派喵云（paired）不发；用户关闭（paimiao_stats_opt_out）不发。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request

from cloud import state as cloud_state
from cloud import net_hosts
from cloud import sync as cloud_sync
from cloud.state import log

import app_settings
import storage

DEBOUNCE_SECONDS = 60.0
_SENT_SCHEMA = 3  # 发送记录格式版本；改上报字段/指纹口径时 +1（升级后只重建基线、不补发历史）
MAX_BATCH = 20
# 要剥掉的 PII（游戏名、玩家ID、队友名字/ID）。clientId 是 ended_at 的哈希，也不发。
_PII = {"clientId", "playerName",
        "teammate1_name", "teammate1_playerId", "teammate2_name", "teammate2_playerId"}

# 这些是整数列；本地库里可能是 REAL（武器/存活秒数等），发送前四舍五入成 int
_NUM_KEYS = {
    "placement", "kills", "assists", "knockdowns", "damage", "participation",
    "rpDelta", "survivalSeconds", "squadKills", "revives", "respawns", "rankProgress",
    "teammate1_kills", "teammate1_assists", "teammate1_knockdowns", "teammate1_damage",
    "teammate1_survivalSeconds", "teammate1_revives", "teammate1_respawns",
    "teammate2_kills", "teammate2_assists", "teammate2_knockdowns", "teammate2_damage",
    "teammate2_survivalSeconds", "teammate2_revives", "teammate2_respawns",
}


def _as_int(value):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return value


def _merge_weapon_seconds(holds):
    """同一局同一把武器的各段按持有秒数累加，合成一条（对齐 cloud 的 battlestats-weapon-holds）。
    返回 [{weapon, startS:0, endS:累计秒}]，四舍五入、上限 2400、0 秒丢弃。"""
    totals = {}
    for h in holds or []:
        weapon = str(h.get("weapon") or "").strip()
        if not weapon:
            continue
        try:
            start = float(h.get("startS") or 0)
            end = float(h.get("endS") or 0)
        except (TypeError, ValueError):
            continue
        if end < start:
            continue
        totals[weapon] = totals.get(weapon, 0.0) + max(0.0, end - start)
    out = []
    for weapon, total in totals.items():
        seconds = min(2400, int(round(total)))
        if seconds > 0:
            out.append({"weapon": weapon, "startS": 0, "endS": seconds})
    return out

_timer = None
_pending_ids: set = set()
_lock = threading.Lock()


def _secret() -> str:
    s = None
    try:
        s = app_settings.get("paimiao_stats_secret")
    except Exception:
        s = None
    if not s:
        s = secrets.token_hex(16)  # 32 hex，只在本机，用来派生每日匿名编号，永不发送
        try:
            app_settings.set_value("paimiao_stats_secret", s)
        except Exception:
            pass
    return s


def anonym_for(ended_ms: int = 0) -> str:
    """固定的匿名编号：只由本机 secret 派生（16 位 hex），不随日期变化。"""
    return hashlib.sha256(_secret().encode("utf-8")).hexdigest()[:16]


def _sent_path() -> str:
    return os.path.join(cloud_state.data_dir(), "stats_sent.json")


def _load_sent():
    try:
        with open(_sent_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_sent(data: dict) -> None:
    try:
        path = _sent_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"schema": _SENT_SCHEMA, "sent": data}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:
        log(f"匿名统计：写发送记录失败（{exc!r}）")


def enabled() -> bool:
    try:
        if app_settings.get("paimiao_stats_opt_out"):
            return False
    except Exception:
        pass
    try:
        if cloud_state.paired(cloud_state.load()):
            return False  # 已绑定派喵云 → 走云同步，不再发匿名统计
    except Exception:
        pass
    return True


def _app_version() -> str:
    return str(getattr(storage, "APP_VERSION", "") or "")


def _stats_url() -> str:
    """上报地址：跟随派喵 API 主/备线路（net_hosts），默认 主线路 tools.skycraft.cn。"""
    override = os.environ.get("EVO_STATS_URL")
    if override:
        return override
    return net_hosts.api_root().rstrip("/") + "/stats/evo"


def _post(body: dict) -> int:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(_stats_url(), data=data, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": net_hosts.UA,
    })
    try:
        # 主线路失败会自动探活并回退到 tools1/tools3 备用线路（与云同步同一套）
        with net_hosts.urlopen_with_failover(req, 15) as resp:
            return int(getattr(resp, "status", resp.getcode()))
    except Exception as exc:
        log(f"匿名统计：上报失败（{exc!r}）")
        return 0


def _prepare_row(row, holds):
    payload = cloud_sync.payload_of(row)
    ended = int(payload.get("endedAt") or 0)
    if not ended:
        return None
    anon = anonym_for(ended)
    clean = {k: v for k, v in payload.items() if k not in _PII}
    for _k in list(clean):
        if _k in _NUM_KEYS and clean[_k] is not None:
            clean[_k] = _as_int(clean[_k])
    clean["weaponHolds"] = _merge_weapon_seconds(holds.get(str(row.get("match_id")), []) or [])
    fp = cloud_sync.fingerprint(clean)
    return anon, ended, clean, f"{anon}:{ended}", fp


def purge_if_bound() -> bool:
    """绑定派喵云后：把本机的固定匿名编号报给服务端，由服务端删掉之前的匿名数据。
    幂等：同一个匿名编号只报一次（记在 app_settings）。失败静默，下次启动再试。"""
    try:
        snap = cloud_state.load()
        if not cloud_state.paired(snap):
            return False
    except Exception:
        return False
    try:
        anon = anonym_for()
    except Exception:
        return False
    try:
        if app_settings.get("paimiao_stats_purged_id") == anon:
            return False
    except Exception:
        pass
    token = str((snap.get("device") or {}).get("token") or "")
    if not token:
        return False
    url = net_hosts.api_root().rstrip("/") + "/stats/evo-purge"
    req = urllib.request.Request(
        url, data=json.dumps({"anonymId": anon}, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token, "User-Agent": net_hosts.UA},
    )
    try:
        with net_hosts.urlopen_with_failover(req, 15) as resp:
            status = int(getattr(resp, "status", resp.getcode()))
    except urllib.error.HTTPError as exc:
        status = int(getattr(exc, "code", 0) or 0)
        if status != 409:  # 409 = 这个 token 已经删过一次，视为已完成
            log(f"匿名统计：删除匿名数据失败（{exc!r}）")
            return False
    except Exception as exc:
        log(f"匿名统计：删除匿名数据失败（{exc!r}）")
        return False
    if 200 <= status < 300 or status == 409:
        try:
            app_settings.set_value("paimiao_stats_purged_id", anon)
        except Exception:
            pass
        log("匿名统计：已请求/完成清理匿名数据（每个设备 token 只允许一次）")
        return True
    return False


def send_pending() -> None:
    """只上报「刚入库、还没发成功」的那几局（_pending_ids）；历史/其它局一律不碰。"""
    if not enabled():
        return
    all_rows = cloud_sync.read_matches()
    holds = cloud_sync.read_weapon_holds()
    if not isinstance(holds, dict):
        holds = {}
    data = _load_sent()
    if not isinstance(data, dict) or data.get("schema") != _SENT_SCHEMA:
        # 首次运行，或上报字段/指纹口径升级：只重建基线，绝不补发历史对局
        sent = {}
        for row in all_rows:
            prepared = _prepare_row(row, holds)
            if prepared:
                sent[prepared[3]] = prepared[4]
        _save_sent(sent)
        with _lock:
            _pending_ids.clear()
        log("匿名统计：已建立基线（不补发历史对局），此后只报新对局")
        return
    sent = data.get("sent") or {}
    with _lock:
        want = set(_pending_ids)
    groups: dict[str, list] = {}
    pending_fp: dict[str, str] = {}
    key_id: dict[str, int] = {}
    done: set = set()
    keep: set = set()
    for row in all_rows:
        prepared = _prepare_row(row, holds)
        if not prepared:
            continue
        anon, ended, clean, key, fp = prepared
        keep.add(key)
        try:
            mid = int(row.get("match_id"))
        except (TypeError, ValueError):
            continue
        if mid not in want:
            continue  # 只发“这一局/待发集合”里的，历史一律跳过
        if sent.get(key) == fp:
            done.add(mid)
            continue
        groups.setdefault(anon, []).append(clean)
        pending_fp[key] = fp
        key_id[key] = mid
    version = _app_version()
    for anon, matches in groups.items():
        for i in range(0, len(matches), MAX_BATCH):
            chunk = matches[i:i + MAX_BATCH]
            status = _post({"anonymId": anon, "version": version, "matches": chunk})
            if 200 <= status < 300:
                for m in chunk:
                    key = f"{anon}:{m.get('endedAt')}"
                    sent[key] = pending_fp.get(key, "")
                    if key in key_id:
                        done.add(key_id[key])
    sent = {k: v for k, v in sent.items() if k in keep}
    _save_sent(sent)
    with _lock:
        _pending_ids.difference_update(done)
        remaining = bool(_pending_ids)
    if remaining:
        schedule()  # 还有没发成功的，60 秒后再试一次


def _fire() -> None:
    global _timer
    with _lock:
        _timer = None
    try:
        send_pending()
    except Exception as exc:
        log(f"匿名统计：上报异常（{exc!r}）")


def schedule(match_id=None) -> None:
    """一局入库后调用：把**这一局**加入待发集合，重置 60 秒去抖计时器；到点只上报这些局。"""
    global _timer
    with _lock:
        if match_id is not None:
            try:
                _pending_ids.add(int(match_id))
            except (TypeError, ValueError):
                pass
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(DEBOUNCE_SECONDS, _fire)
        _timer.daemon = True
        _timer.start()
