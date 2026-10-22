"""Evo 派喵云同步 · 上传线程。

三条硬约束决定了写法：

1. **只读**：用 `file:...?mode=ro` 打开 Evo 的库，物理上不可能写坏它。
2. **不挂 hook**：post_match.settle() 有 12 个调用点，且一局会跑多次；
   挂在那里既要改上游文件，又要把网络 IO 塞进记录子进程。
   改成"定时扫最近 N 局 + 内容哈希"，上游怎么改都不影响我们。
3. **网络 IO 不碰记录子进程**：线程只活在主程序里。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime

from cloud import pairing
from cloud import state as cloud_state

SCAN_LIMIT = 200          # 每次扫最近多少局
INTERVAL = 30.0           # 多久扫一次（秒）
BATCH = 50                # 一次最多上传多少局
LOBBY_LIMIT = 500         # 每次扫最近多少条大厅读数
LOBBY_BATCH = 100         # 一次最多上传多少条大厅读数
# 一局的字段。顺序不重要，但**必须是 matches 表里真实存在的列**。
# 没在表里的列会被 _select_columns 自动剔掉（老库没有新列时不会报错）。
FIELDS = ("match_id", "ended_at", "player_name", "mode", "map", "legend",
          "placement", "kills", "assists", "knockdowns", "damage",
          "participation", "rp_delta", "survival_time", "needs_review", "review_note",
          # 2026-10-22 新增：段位三列 + 总结页上本来就在显示、之前没传的几项
          "rank_tier", "rank_division", "rank_progress",
          "revives", "respawns", "squad_kills", "started_at")
# 队友的字段。name/legend/kills 是老字段，后面几个是新加的。
MATE_FIELDS = ("name", "legend", "kills", "assists", "damage",
               "knockdowns", "survival_time", "revives", "respawns")
# 大厅读数（段位曲线）的字段
LOBBY_FIELDS = ("read_at", "player_name", "rank_tier", "rank_division",
                "rank_progress", "score")

_thread = None
_wake = threading.Event()
_db_override = ""         # 面板用 --db 指定了别的库时，跟着它走


def set_db_path(path):
    """bootstrap 在挂载时把 app.config["DB"] 记下来。

    不直接用 paths.DB_PATH：面板支持 --db 指向别的库（演示数据就是这么跑的），
    那种情况下 paths.DB_PATH 并不是面板正在显示的那个库。
    """
    global _db_override
    _db_override = str(path or "")


def arm_once():
    """第一个面板请求到来时才起线程 —— 不拖慢 Evo 启动。

    只有"同意过 + 配对过 + 开关打开"才真的起；否则什么都不做。
    """
    global _thread
    if _thread is not None:
        return
    snapshot = cloud_state.load()
    if not (cloud_state.agreed(snapshot) and cloud_state.paired(snapshot)
            and snapshot.get("enabled")):
        return
    _thread = threading.Thread(target=_loop, name="cloud-sync", daemon=True)
    _thread.start()


def _loop():
    while True:
        try:
            snapshot = cloud_state.load()
            if not (cloud_state.agreed(snapshot) and cloud_state.paired(snapshot)
                    and snapshot.get("enabled")):
                return
            push(snapshot)
        except Exception as exc:
            _note_error(f"{type(exc).__name__}: {exc}")
        _wake.wait(INTERVAL)
        _wake.clear()


def _db_path():
    if _db_override:
        return _db_override
    from paths import DB_PATH      # noqa: PLC0415  Evo 自己的模块
    return str(DB_PATH)


def _has_columns(conn, table, wanted):
    """表里实际有哪些列。

    为什么要问：老库没有后来才加的列（比如 rank_progress 是迁移 6 才有的），
    直接 SELECT 会 "no such column" 让整次同步失败。宁可少传几个字段，也不能一条都传不上去。
    """
    try:
        got = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return []
    return [name for name in wanted if name in got]


def read_matches(limit=SCAN_LIMIT):
    """只读方式扫最近若干局。连不上就当没数据，绝不影响 Evo。"""
    try:
        conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        cols = _has_columns(conn, "matches", FIELDS)
        if "match_id" not in cols:
            return []
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM matches ORDER BY match_id DESC LIMIT ?",
            (limit,)).fetchall()
        mates = _mates(conn, [r["match_id"] for r in rows])
        return [dict(r, teammates=mates.get(r["match_id"], [])) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _mates(conn, ids):
    if not ids:
        return {}
    marks = ", ".join("?" for _ in ids)
    cols = _has_columns(conn, "teammates", MATE_FIELDS)
    if "name" not in cols:
        return {}
    try:
        rows = conn.execute(
            f"SELECT match_id, {', '.join(cols)} FROM teammates "
            f"WHERE match_id IN ({marks}) ORDER BY rowid", ids).fetchall()
    except sqlite3.Error:
        return {}
    out = {}
    for row in rows:
        mate = {name: row[name] for name in cols}
        out.setdefault(row["match_id"], []).append(mate)
    return out


def read_lobby(limit=LOBBY_LIMIT):
    """只读方式扫最近若干条大厅读数 —— 段位曲线的数据源。

    为什么段位不在每一局上：Evo 只给"没进总结页、只进经验值页"的排位局写 rank_tier
    （code/post_match.py:561），有总结页的排位局那三列是空的。大厅读数才是连续的时间序列。
    量很小：只在"连续两次读到一样的段位和分数"时才落库（code/lobby.py:117）。
    """
    try:
        conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        cols = _has_columns(conn, "lobby_readings", LOBBY_FIELDS)
        if "read_at" not in cols:
            return []
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM lobby_readings "
            f"ORDER BY read_at DESC, reading_id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def fingerprint(payload):
    """内容哈希：ended_at 一局只写一次，所以重传天然幂等；
    但回大厅校准会改 rp_delta —— 哈希变了就自动重传，二次定稿不会被漏掉。"""
    material = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def client_id_of(match):
    """这一局在服务器上的唯一键：只用对局结束时间。

    为什么不用 Evo 自己的 match_id：它是 SQLite 的 AUTOINCREMENT 主键，
    **每台电脑各算各的**。同一个玩家在两台机器上（或把数据文件夹拷到新机器）
    同一局会拿到两个不同的编号，服务器就当成两局了。换绑小程序账号时更糟：
    新账号的第一局也会叫 evo-1、evo-2……直接覆盖掉旧账号的记录。

    为什么用 ended_at：它是这一局在时间轴上的绝对位置，
    · 复制数据文件夹过去 —— 时间一模一样，服务器自动合并成一条
    · 换绑小程序账号 —— 同一局时间不变，合并
    · 两台各打各的 —— 时间本来就不一样，各自保留

    为什么不能带上账号 ID / 击杀 / 名次这些字段：
    · 账号 ID 是 OCR 读出来的，读错就换一个键（这是明确否决的方案）
    · 击杀、名次是**用户可改**的，改了哈希就变，服务器上会同时留下改前改后两条
      （这是实际发生过的 bug）

    已知残余风险：两台机器**同时**录同一场时，各自识别到总结页的秒数可能差几秒，
    会各存一条。概率低且不影响单机使用，暂时接受。
    """
    stamp = str(match.get("ended_at") or "").strip()
    return "edo-" + hashlib.sha256(stamp.encode("utf-8")).hexdigest()[:28]


def to_ms(text):
    try:
        return int(datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S").astimezone().timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


MATE_PAYLOAD = ("name", "legend", "kills", "assists", "damage",
                "knockdowns", "survivalSeconds", "revives", "respawns")


def _mate_payload(mate):
    """一个队友。survival_time 在库里是秒数，出去叫 survivalSeconds。"""
    out = {}
    for key in MATE_PAYLOAD:
        out[key] = mate.get("survival_time") if key == "survivalSeconds" else mate.get(key)
    return out


def payload_of(match):
    return {
        "clientId": client_id_of(match),
        "endedAt": to_ms(match.get("ended_at")),
        "playerName": match.get("player_name"),
        "mode": match.get("mode"),
        "map": match.get("map"),
        "legend": match.get("legend"),
        "placement": match.get("placement"),
        "kills": match.get("kills"),
        "assists": match.get("assists"),
        "damage": match.get("damage"),
        "knockdowns": match.get("knockdowns"),
        "participation": match.get("participation"),
        "rpDelta": match.get("rp_delta"),
        "survivalSeconds": match.get("survival_time"),
        "needsReview": bool(match.get("needs_review")),
        # 2026-10-22 新增。旧记录里这些多半是空的（那时还不读/不存），传上去就是 null。
        "squadKills": match.get("squad_kills"),
        "revives": match.get("revives"),
        "respawns": match.get("respawns"),
        "rankTier": match.get("rank_tier"),
        "rankDivision": match.get("rank_division"),
        "rankProgress": match.get("rank_progress"),
        "startedAt": to_ms(match.get("started_at")) if match.get("started_at") else None,
        "teammates": [_mate_payload(m) for m in (match.get("teammates") or [])[:2]],
    }


def lobby_payload_of(reading):
    """一条大厅读数。read_at 是库里的本地时间文本，出去用毫秒戳。"""
    return {
        "readAt": to_ms(reading.get("read_at")),
        "playerName": reading.get("player_name"),
        "rankTier": reading.get("rank_tier"),
        "rankDivision": reading.get("rank_division"),
        "rankProgress": reading.get("rank_progress"),
        "score": reading.get("score"),
    }


def _pending(rows, known, build):
    """挑出"没传过、或者传过但内容变了"的行。

    known 的键是行标识，值是内容哈希。哈希只用来决定"要不要再传一次"，
    **绝不参与 client_id**（那是幂等主键，用内容算会让改一次数据多出一条）。
    """
    out = []
    for row in rows:
        key = str(row["match_id"]) if "match_id" in row else str(row.get("read_at"))
        payload = build(row)
        mark = fingerprint(payload)
        if known.get(key) == mark:
            continue
        out.append((key, mark, payload))
    return out


def push(snapshot):
    """把"没传过、或者传过但内容变了"的局传上去。返回这次传了几局（对局数，不含大厅读数）。"""
    token = (snapshot.get("device") or {}).get("token")
    if not token:
        # 不能安静地 return 0：页面上会显示成"还没有上传过"，看不出是没配对。
        # 抛出去让 _loop / push_quiet 记账，用户能看到原因。
        raise pairing.CloudError("这台电脑还没配对")

    known = snapshot.get("hashes") or {}
    lobby_known = snapshot.get("lobby_hashes") or {}

    rows = read_matches()
    pending = _pending(rows, known, payload_of) if rows else []
    lobby_rows = read_lobby()
    lobby_pending = _pending(lobby_rows, lobby_known, lobby_payload_of) if lobby_rows else []
    if not pending and not lobby_pending:
        return 0

    sent = 0
    for start in range(0, len(pending), BATCH):
        chunk = pending[start:start + BATCH]
        result = pairing.upload(snapshot, {"matches": [item[2] for item in chunk]})
        for key, mark, _ in chunk:
            known[key] = mark
        sent += int(result.get("accepted") or len(chunk))
        del chunk

    # 大厅读数是另一张表，单独走 /rank。老服务端没有这个接口时会抛 CloudError，
    # 不能让整个同步失败 —— 对局已经传成功了，段位下次再补。
    lobby_sent = 0
    for start in range(0, len(lobby_pending), LOBBY_BATCH):
        chunk = lobby_pending[start:start + LOBBY_BATCH]
        try:
            result = pairing.upload_rank(snapshot, {"readings": [item[2] for item in chunk]})
        except pairing.CloudError as exc:
            stats = snapshot.setdefault("stats", {})
            stats["rank_error"] = str(exc)[:200]
            break
        for key, mark, _ in chunk:
            lobby_known[key] = mark
        lobby_sent += int(result.get("accepted") or len(chunk))
        del chunk

    # 只留最近若干个哈希，防止 state.json 无限长大
    if len(known) > SCAN_LIMIT * 2:
        keep = {str(m["match_id"]) for m in rows}
        known = {k: v for k, v in known.items() if k in keep}
    if len(lobby_known) > LOBBY_LIMIT * 2:
        keep = {str(m.get("read_at")) for m in lobby_rows}
        lobby_known = {k: v for k, v in lobby_known.items() if k in keep}

    snapshot["hashes"] = known
    snapshot["lobby_hashes"] = lobby_known
    stats = snapshot.setdefault("stats", {})
    stats["uploaded"] = int(stats.get("uploaded") or 0) + sent
    stats["rank_uploaded"] = int(stats.get("rank_uploaded") or 0) + lobby_sent
    stats["last_ok_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    stats["last_error"] = ""
    cloud_state.save(snapshot)
    return sent


def push_quiet(snapshot):
    """给"立即同步"按钮用：出错只记下来，不往外抛。"""
    try:
        push(snapshot)
    except Exception as exc:
        _note_error(f"{type(exc).__name__}: {exc}")


def _note_error(message):
    try:
        snapshot = cloud_state.load()
        stats = snapshot.setdefault("stats", {})
        stats["failed"] = int(stats.get("failed") or 0) + 1
        stats["last_error"] = message[:200]
        cloud_state.save(snapshot)
    except Exception:
        pass
