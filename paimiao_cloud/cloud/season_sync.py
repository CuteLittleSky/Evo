from __future__ import annotations

"""赛季聚合上传：把本地 season_stats 里「待核对 + 已保存」的行传上去。

设计（与战绩上传同一条线）：
- 只读本地库（sqlite 只读连接），不改任何东西；
- 按行算指纹（payload 的 sha256），只有变了才传；重传由服务端主键
  (openid, kind, season) 覆盖更新，玩家手改后再传会同步到最新；
- 断网 / 服务器错只记 stats["season_error"]，不影响战绩上传，下次再试；
- 读不到的项传 None（服务端存 NULL），绝不写成 0。
"""

import hashlib
import json
import sqlite3
from datetime import datetime

from cloud import pairing

BATCH = 50
MAX_ROWS = 500

FIELDS = ("player_name", "season", "kind",
          "games", "wins", "top5", "damage", "damage_max", "damage_avg",
          "kills", "deaths", "kd", "kills_max", "knockdowns", "assists",
          "win_streak", "revives", "respawns", "approx", "read_at", "status")


def _has_table(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'season_stats'").fetchone()
    return row is not None


def read_seasons(db_path):
    """本地 season_stats 的行（只读）；表不存在 / 打不开时返回 []。"""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        if not _has_table(conn):
            return []
        cols = {r[1] for r in conn.execute("PRAGMA table_info(season_stats)")}
        wanted = [f for f in FIELDS if f in cols]
        if "kind" not in wanted:
            return []
        rows = conn.execute(
            f"SELECT {', '.join(wanted)} FROM season_stats ORDER BY stat_id DESC LIMIT ?",
            (MAX_ROWS,)).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _ms(text):
    """把 read_at（'2026-11-03 16:16:32'）转成毫秒时间戳；转不了返回 0。"""
    try:
        return int(datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


def _approx(text):
    return [item.strip() for item in str(text or "").split(",") if item.strip()]


def _number(value, decimals=False):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if decimals:
        return round(number, 2)
    return int(round(number))


def payload_of(row):
    """本地一行 → 服务端载荷（camelCase）；读不到的项是 None。"""
    kind = str(row.get("kind") or "")
    season = 0 if kind == "career" else _number(row.get("season")) or 0
    return {
        "kind": kind,
        "season": season,
        "playerName": row.get("player_name") or "",
        "readAt": _ms(row.get("read_at")),
        "needsReview": (row.get("status") or "") != "saved",
        "games": _number(row.get("games")),
        "wins": _number(row.get("wins")),
        "top5": _number(row.get("top5")),
        "damage": _number(row.get("damage")),
        "damageMax": _number(row.get("damage_max")),
        "damageAvg": _number(row.get("damage_avg"), decimals=True),
        "kills": _number(row.get("kills")),
        "deaths": _number(row.get("deaths")),
        "kd": _number(row.get("kd"), decimals=True),
        "killsMax": _number(row.get("kills_max")),
        "knockdowns": _number(row.get("knockdowns")),
        "assists": _number(row.get("assists")),
        "winStreak": _number(row.get("win_streak")),
        "revives": _number(row.get("revives")),
        "respawns": _number(row.get("respawns")),
        "approx": _approx(row.get("approx")),
    }


def fingerprint(payload):
    material = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def pending(snapshot, rows):
    """还没传过（或变过）的行：[(key, mark, payload), ...]。"""
    known = snapshot.get("season_hashes") or {}
    out = []
    for row in rows:
        payload = payload_of(row)
        if not payload["kind"]:
            continue
        key = f"{payload['kind']}:{payload['season']}"
        mark = fingerprint(payload)
        if known.get(key) == mark:
            continue
        out.append((key, mark, payload))
    return out


def send(snapshot, items, keep_keys):
    """把 pending() 的结果分批传上去；成功才推进指纹。返回传了几条。

    keep_keys = 本地当前还存在的赛季键；不在这里的指纹要清掉（比如本地清库后）。
    """
    known = snapshot.get("season_hashes") or {}
    sent = 0
    for start in range(0, len(items), BATCH):
        chunk = items[start:start + BATCH]
        result = pairing.upload_seasons(snapshot, {"seasons": [item[2] for item in chunk]})
        for key, mark, _ in chunk:
            known[key] = mark
        sent += int(result.get("accepted") or len(chunk))
    snapshot["season_hashes"] = {k: v for k, v in known.items() if k in keep_keys}
    return sent