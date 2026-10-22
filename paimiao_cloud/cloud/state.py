"""Evo 派喵云同步 · 本地状态（不碰 Evo 的 settings.json）。

状态机只有三个状态，页面显示什么完全由它决定：
    未同意协议 → 已同意但未配对 → 已配对
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

STATE_NAME = "state.json"
STATE_FOLDER = "cloud-sync"   # 放在 Evo 数据文件夹下，不叫 cloud（避免和我们的包名混淆）
LOG_NAME = "cloud.log"

# 派喵服务器。用户不需要填地址 —— 这是内置的、唯一的目标服务器。
DEFAULT_SERVER = "https://tools.skycraft.cn/apex/api/battlestats"


def default_server() -> str:
    """每次现读环境变量：自检要指向本机假接口，而端口是运行时才定的。

    生产环境没有这个变量，行为就是上面那个常量。
    """
    return (os.environ.get("EVO_CLOUD_SERVER") or DEFAULT_SERVER).rstrip("/")

# 协议版本：改了协议正文就必须改这里，老用户会被要求重新同意
# 2026-10-22 → 2026-10-22b：正文加了「从云端取回」和「只读的唯一例外」两段，
# 这是新的数据处理行为，拿旧同意去覆盖它不成立，所以升一个版本让用户重新看一遍。
CONSENT_VERSION = "2026-10-22b"


def data_dir() -> Path:
    """跟着 Evo 的数据库走（源码运行时可能是安装版的数据文件夹）。

    用 DB_PATH 而不是 DATA_DIR：源码运行 evo_app.py 时 source_run.py 会把
    EVO_DATA_DIR 指到安装版的数据文件夹，DB_PATH 自动跟着走，
    我们的状态就不会落在项目根目录里污染仓库。
    """
    try:
        from paths import DB_PATH        # noqa: PLC0415  Evo 自己的模块
        return Path(DB_PATH).parent / STATE_FOLDER
    except Exception:
        base = os.environ.get("EVO_DATA_DIR")
        root = Path(base) if base else Path(__file__).resolve().parent.parent.parent / "data"
        return root / STATE_FOLDER


def log_path() -> Path:
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder / LOG_NAME


def state_path() -> Path:
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder / STATE_NAME


DEFAULTS = {
    "consent": {},       # {agreed: bool, version: str, at: "2026-10-21 22:00"}
    "server": "",        # 服务器地址（内置，不需要用户填；留着是为了日后可切镜像）
    "device": {},        # {token, name, expires_text}
    "enabled": True,     # 只有配对后才起作用
    "pair_code": "",
    "pair_hint": "",
    "pair_ticket": "",
    "pair_code_at": 0.0,  # 配对码生成时刻（time.time()），用来算还剩多久
    "pair_expires_ms": 0,  # 服务器给的过期毫秒戳，优先用它算倒计时
    "hashes": {},        # {match_id: sha256} 判断"这一局传过没有、变没变"
    "stats": {"uploaded": 0, "failed": 0, "last_ok_at": "", "last_error": ""},
    # 派喵分支说明弹窗：看过的是哪个版本。见 cloud/notice.py
    "notice_seen": "",
}


def load() -> dict:
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in raw.items() if k in DEFAULTS})
    merged["stats"] = {**DEFAULTS["stats"], **(raw.get("stats") or {})}
    merged["device"] = raw.get("device") or {}
    merged["hashes"] = raw.get("hashes") or {}
    merged["consent"] = raw.get("consent") or {}
    merged["notice_seen"] = str(raw.get("notice_seen") or "")
    # 服务器地址是内置常量，用户不需要填；只有测试时用环境变量覆盖。
    # 老状态文件里存过 server 的（上一版界面让用户填），一律以常量为准，
    # 免得升级后还指着那个已经不用的地址。
    merged["server"] = default_server()
    return merged


def save(state: dict) -> None:
    """原子写：先写 .tmp 再 os.replace，断电也不会留半截文件。"""
    path = state_path()
    handle, temp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def agreed(snapshot: dict) -> bool:
    """同意过**当前版本**的协议才算同意。"""
    consent = snapshot.get("consent") or {}
    return bool(consent.get("agreed")) and consent.get("version") == CONSENT_VERSION


def paired(snapshot: dict) -> bool:
    return bool((snapshot.get("device") or {}).get("token"))


def log(message: str) -> None:
    try:
        import datetime
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path(), "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass
