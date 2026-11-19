"""老板分（参与分）补算：上游只从「对局中右上角」读，读不到时按云同步同一公式补算。

公式（和 ApexPacks `server/utils/battlestats-matches.ts` 的 derived 规则一致）：
    老板分 = clamp(小队总击杀 − 自己击杀 − 助攻, 0, 999)

只在这些情况补算（避免把“真读到的 0”改掉）：
  · 这一局**没有任何 participation 读数**（对局中没读到）；
  · 且总结页读到了 squad_kills。
做法：包装上游模块级 `post_match.settle`（以及 calibration 里 `from post_match import settle`
拿到的那个引用），结算完再补算并落库。上游文件一个字节不动。
"""
from __future__ import annotations

import post_match
import storage

from cloud.state import log


def _has_participation_reading(conn, match_id) -> bool:
    for r in storage.readings_of(conn, match_id):
        if r["field"] == "participation" and r["value"] is not None:
            return True
    return False


def _derive(conn, match_id) -> None:
    row = storage.get_match(conn, match_id)
    if row is None:
        return
    if _has_participation_reading(conn, match_id):
        return  # 对局中读到了（哪怕是 0）→ 用读到的，不改
    squad_kills = row["squad_kills"]
    if squad_kills is None:
        return  # 连小队总击杀都没有 → 算不出
    kills = row["kills"] or 0
    assists = row["assists"] or 0
    value = max(0, min(999, int(squad_kills) - int(kills) - int(assists)))
    storage.update_match(conn, match_id, {"participation": value})
    log(f"老板分没读到，按 小队总击杀{squad_kills} − 击杀{kills} − 助攻{assists} = {value} 补算（编号 {match_id}）")


def _wrap(original):
    def settle(conn, match_id, log=print):
        result = original(conn, match_id, log)
        try:
            _derive(conn, match_id)
        except Exception as exc:  # 静默降级：绝不因为它影响结算主流程
            try:
                log(f"老板分补算失败（不影响其它）：{exc!r}")
            except Exception:
                pass
        return result

    settle._paimiao_boss = True
    return settle


def install() -> None:
    if getattr(post_match, "_paimiao_boss_installed", False):
        return
    original = post_match.settle
    if getattr(original, "_paimiao_boss", False):
        return
    wrapped = _wrap(original)
    post_match.settle = wrapped
    post_match._paimiao_boss_installed = True
    # calibration.py 是 `from post_match import settle`，拿的是原函数引用，单独替一份
    try:
        import calibration
        if getattr(calibration, "settle", None) is original:
            calibration.settle = wrapped
    except Exception:
        pass
    log("老板分补算：已挂载（没读到且有 squad_kills 时按 小队总击杀−击杀−助攻）")
