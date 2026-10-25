from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime

from cloud import pairing
from cloud import state as cloud_state

SCAN_LIMIT = 200
INTERVAL = 30.0
BATCH = 50
LOBBY_LIMIT = 500
LOBBY_BATCH = 100

FIELDS = ("match_id", "ended_at", "player_name", "mode", "map", "legend",
          "placement", "kills", "assists", "knockdowns", "damage",
          "participation", "rp_delta", "survival_time", "needs_review", "review_note",

          "rank_tier", "rank_division", "rank_progress",
          "revives", "respawns", "squad_kills", "started_at")

MATE_FIELDS = ("name", "legend", "kills", "assists", "damage",
               "knockdowns", "survival_time", "revives", "respawns")

LOBBY_FIELDS = ("read_at", "player_name", "rank_tier", "rank_division",
                "rank_progress", "score")

_thread = None
_wake = threading.Event()

_push_lock = threading.Lock()
_db_override = ""

def set_db_path(path):

    global _db_override
    _db_override = str(path or "")

def arm_once():

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
            with _push_lock:
                push(snapshot)
        except Exception as exc:
            _note_error(f"{type(exc).__name__}: {exc}")
        _wake.wait(INTERVAL)
        _wake.clear()

def _db_path():
    if _db_override:
        return _db_override
    from paths import DB_PATH
    return str(DB_PATH)

def _has_columns(conn, table, wanted):

    try:
        got = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return []
    return [name for name in wanted if name in got]

def read_matches(limit=SCAN_LIMIT):

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
        boss = _boss_scores_read(conn, [r["match_id"] for r in rows])
        return [dict(r, teammates=mates.get(r["match_id"], []),
                     _boss_read=boss.get(r["match_id"], False)) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()

def _boss_scores_read(conn, ids):

    if not ids:
        return {}
    marks = ", ".join("?" for _ in ids)
    try:
        rows = conn.execute(
            f"SELECT match_id, COUNT(value) AS n FROM readings "
            f"WHERE field = 'participation' AND match_id IN ({marks}) "
            f"GROUP BY match_id", ids).fetchall()
    except sqlite3.Error:
        return {}
    got = {r["match_id"]: r["n"] > 0 for r in rows}

    return {mid: got.get(mid, False) for mid in ids}

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

    material = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()

def client_id_of(match):

    stamp = str(match.get("ended_at") or "").strip()
    return "edo-" + hashlib.sha256(stamp.encode("utf-8")).hexdigest()[:28]

def to_ms(text):
    try:
        return int(datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S").astimezone().timestamp() * 1000)
    except (TypeError, ValueError):
        return 0

_FLAT_OUT = {
    "name": "name",
    "player_id": "playerId",
    "legend": "legend",
    "kills": "kills",
    "assists": "assists",
    "knockdowns": "knockdowns",
    "damage": "damage",
    "survival_time": "survivalSeconds",
    "revives": "revives",
    "respawns": "respawns",
}

def _flat_teammates(mates):

    out = {}
    for n, mate in enumerate((mates or [])[:2], start=1):
        for field, suffix in _FLAT_OUT.items():
            out[f"teammate{n}_{suffix}"] = mate.get(field)
    return out

def payload_of(match):
    payload = {
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
        "participation": _boss_payload(match),
        "rpDelta": match.get("rp_delta"),
        "survivalSeconds": match.get("survival_time"),
        "needsReview": bool(match.get("needs_review")),

        "squadKills": match.get("squad_kills"),
        "revives": match.get("revives"),
        "respawns": match.get("respawns"),
        "rankTier": match.get("rank_tier"),
        "rankDivision": match.get("rank_division"),
        "rankProgress": match.get("rank_progress"),
        "startedAt": to_ms(match.get("started_at")) if match.get("started_at") else None,
    }

    payload.update(_flat_teammates(match.get("teammates")))
    return payload

WEAPON_HOLD_FIELDS = ("weapon", "start_s", "end_s")

def read_weapon_holds(limit=SCAN_LIMIT):

    try:
        conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        cols = _has_columns(conn, "weapon_holds", WEAPON_HOLD_FIELDS)
        if "weapon" not in cols:
            return []
        rows = conn.execute(
            f"SELECT match_id, {', '.join(cols)} FROM weapon_holds "
            f"ORDER BY match_id DESC, start_s LIMIT ?", (limit,)).fetchall()
        out = {}
        for row in rows:
            out.setdefault(str(row["match_id"]), []).append(
                {"weapon": row["weapon"], "startS": row["start_s"], "endS": row["end_s"]})
        return out
    except sqlite3.Error:
        return []
    finally:
        conn.close()

def weapon_payloads(match_map, holds_by_match):

    items = []
    for match in match_map:
        holds = holds_by_match.get(str(match.get("match_id")))
        if not holds:
            continue
        items.append({"clientId": client_id_of(match), "weaponHolds": holds})
    return items

def _boss_payload(match):

    value = match.get("participation")
    if value == 0 and not match.get("_boss_read"):
        return None
    return value

def lobby_payload_of(reading):

    return {
        "readAt": to_ms(reading.get("read_at")),
        "playerName": reading.get("player_name"),
        "rankTier": reading.get("rank_tier"),
        "rankDivision": reading.get("rank_division"),
        "rankProgress": reading.get("rank_progress"),
        "score": reading.get("score"),
    }

def lobby_state_key(reading):

    return (reading.get("player_name"), reading.get("rank_tier"),
            reading.get("rank_division"), reading.get("rank_progress"),
            reading.get("score"))

def drop_repeats(lobby_rows):

    kept = []
    previous = None
    for row in _chronological(lobby_rows):
        state = lobby_state_key(row)
        if state != previous:
            payload = lobby_payload_of(row)
            kept.append((str(row.get("read_at")), fingerprint(payload), payload))
            previous = state

    return kept

def _chronological(lobby_rows):

    return sorted(lobby_rows, key=lambda row: (str(row.get("read_at")), row.get("reading_id") or 0))

def _pending(rows, known, build):

    out = []
    for row in rows:
        key = str(row["match_id"]) if "match_id" in row else str(row.get("read_at"))
        payload = build(row)
        mark = fingerprint(payload)
        if known.get(key) == mark:
            continue
        out.append((key, mark, payload))
    return out

def _carry_repeats(lobby_rows, sent_keys, lobby_known):

    previous_key = None
    previous_state = None
    for row in _chronological(lobby_rows):
        key = str(row.get("read_at"))
        state = lobby_state_key(row)
        if key in sent_keys:
            previous_key, previous_state = key, state
        elif previous_key is not None and state == previous_state:

            lobby_known.setdefault(key, lobby_known.get(previous_key, ""))
        else:

            previous_key, previous_state = None, None

def push(snapshot):

    token = (snapshot.get("device") or {}).get("token")
    if not token:

        raise pairing.CloudError("这台电脑还没配对")

    known = snapshot.get("hashes") or {}
    lobby_known = snapshot.get("lobby_hashes") or {}

    rows = read_matches()
    pending = _pending(rows, known, payload_of) if rows else []
    lobby_rows = read_lobby()

    lobby_pending = [item for item in drop_repeats(lobby_rows)
                     if lobby_known.get(item[0]) != item[1]] if lobby_rows else []
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

    lobby_sent = 0
    rank_failed = False
    for start in range(0, len(lobby_pending), LOBBY_BATCH):
        chunk = lobby_pending[start:start + LOBBY_BATCH]
        try:
            result = pairing.upload_rank(snapshot, {"readings": [item[2] for item in chunk]})
        except pairing.CloudError as exc:
            stats = snapshot.setdefault("stats", {})
            stats["rank_error"] = str(exc)[:200]
            rank_failed = True
            break
        for key, mark, _ in chunk:
            lobby_known[key] = mark

        _carry_repeats(lobby_rows, {item[0] for item in chunk}, lobby_known)
        lobby_sent += int(result.get("accepted") or len(chunk))
        del chunk

    # 武器持有段（0.4.0 起）：跟战绩同一个令牌、单独端点。失败只记一笔，不影响战绩上传。
    stats = snapshot.setdefault("stats", {})
    weapon_sent = 0
    try:
        weapon_items = weapon_payloads(rows, read_weapon_holds())
        for start in range(0, len(weapon_items), BATCH):
            chunk = weapon_items[start:start + BATCH]
            result = pairing.upload_weapons(snapshot, {"matches": chunk})
            weapon_sent += int(result.get("accepted") or len(chunk))
    except pairing.CloudError as exc:
        stats["weapon_error"] = str(exc)[:200]

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
    if weapon_sent:
        stats["weapon_uploaded"] = int(stats.get("weapon_uploaded") or 0) + weapon_sent
    stats["last_ok_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    stats["last_error"] = ""

    stats["failed"] = 0
    if not rank_failed:

        stats["rank_error"] = ""
    cloud_state.save(snapshot)
    return sent

def push_quiet(snapshot):

    try:
        with _push_lock:
            return push(snapshot)
    except Exception as exc:
        _note_error(f"{type(exc).__name__}: {exc}")
        return 0

def _note_error(message):
    try:
        snapshot = cloud_state.load()
        stats = snapshot.setdefault("stats", {})
        stats["failed"] = int(stats.get("failed") or 0) + 1
        stats["last_error"] = message[:200]
        cloud_state.save(snapshot)
    except Exception:
        pass
