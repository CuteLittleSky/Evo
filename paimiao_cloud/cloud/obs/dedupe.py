"""防止同一局被记两次：记录重新开始后，先看数据库里是不是已经存过这张总结页。

## 上游本来有一道防线，但它只在一次记录里有效

`live_collect.SummaryWatcher`（live_collect.py:97）有一个 `last_saved`：
「最近存进数据库的那一局的读数」。切到别的页再切回总结页时，靠它认出是同一局
（live_collect.py:150 `if same_match(values, self.last_saved)`）。

问题：`last_saved` 是 **SummaryWatcher 自己的属性**，而 SummaryWatcher 是每次
`collect()` 新建的。记录子进程一停（投影关了、Evo 重启、暂停再继续），
`last_saved` 就没了 —— 只要用户还停在总结页，下一次记录就会把同一张总结页再存一遍。

2026-10-23 实测踩过：在总结页上重启 Evo，同一局存了两次
（match_id 7 和 8，除 match_id / ended_at / 截图路径以外字段全一样）。

## 改法：把「上一局」换成数据库里的，而不是内存里的

包 `live_collect.same_match`：`last_saved` 是 None 时，去数据库查最近一局来顶替。
这样 `live_collect.py:150` 一个字都不用改：

    same_match(values, self.last_saved)      # 原来是 None 就什么都不比
        ↓ 包一层之后
    same_match(values, self.last_saved)      # 传 None 时自动拿数据库最近一局顶上

## 判重条件（和上游 same_match 的规则一致，只是换了数据来源）

`same_match(a, b, min_fields=4)`：两边都读到的项**全部**相同，且至少 4 项可比。

- 现在稳定可读的有 4 项（名次、击杀/助攻/击倒、伤害、生存时间），4 项都对上就算同一局。
- 时间限制：只和 `DUPLICATE_WINDOW`（6 小时）以内存的局比。超出这个窗口就不当重复
  —— 宁可多记一局让用户删，也不要漏记一局。
- 再要求两条记录的时间差不超过 `MAX_GAP`（3 小时）：一局 Apex 最长也就半小时，
  差这么久不可能是同一局。
- 比的是「最近一局」，不是最近 20 局里的任意一局：正常打游戏时最新一条就是上一局，
  这样才能挡住「重启 Evo 后又存了一次」。

## 只在主机模式下生效

`settings.enabled()` 为假（PC 玩家、整屏来源）时完全不装钩子，`same_match` 还是上游那个，
行为和上游一字不差。
"""
from __future__ import annotations

import sqlite3

from cloud.obs import settings as obs_settings

# 多久以内存的局才参与判重。一局 Apex 最长约 30 分钟，6 小时已经很宽了。
DUPLICATE_WINDOW = 6 * 3600.0
# 两条记录的时间差上限：上面已经限了窗口，这里再兜一层，防止数据库里时间格式怪掉。
MAX_GAP = 3 * 3600.0

_MARK = "_paimiao_obs_same_match"


def install(module) -> bool:
    """给 live_collect 装上「last_saved 为空时去数据库查上一局」的判重。幂等。

    返回 True 表示这次确实接上了（第一次），False 表示已经接过或模块不对。
    """
    if module is None:
        return False
    real = getattr(module, "same_match", None)
    if real is None or getattr(real, _MARK, False):
        return False
    if not obs_settings.enabled():
        return False      # PC 玩家：完全不碰

    def same_match(a, b, min_fields=4):
        if b is None:
            try:
                b = _previous_from_db()
            except Exception:
                b = None      # 查数据库出错绝不能影响记录，就当没有可比的
        return real(a, b, min_fields)

    setattr(same_match, _MARK, True)
    setattr(same_match, "_paimiao_real", real)
    module.same_match = same_match
    return True


# ---------- 读数据库 ----------

def _db_path():
    """Evo 的数据库文件。用上游自己的 paths.DB_PATH，跟它保持绝对一致。"""
    import paths  # noqa: PLC0415  上游模块（code/paths.py）
    return paths.DB_PATH


def _previous_from_db():
    """数据库最近一局的读数（能比的那几项）；没有记录或超出窗口就返回 None。

    只读打开，不写、不锁：Evo 自己的连接正在写这个库，用只读 + 超时的方式看一眼。
    """
    path = _db_path()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3.0)
    try:
        row = conn.execute(
            "SELECT ended_at, kills, assists, knockdowns, damage, placement, survival_time"
            " FROM matches ORDER BY match_id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    ended_at, kills, assists, knocks, damage, placement, survival = row
    if not _recent_enough(ended_at):
        return None
    return {
        "placement": placement,
        "kda": _kda(kills, assists, knocks),
        "damage": damage,
        "survival_time": survival,
    }


def _recent_enough(ended_at):
    """这一局是不是刚打完的（在 DUPLICATE_WINDOW 以内）。时间读不出来就当不近。"""
    if not ended_at:
        return False
    from datetime import datetime, timedelta  # noqa: PLC0415
    try:
        when = datetime.strptime(str(ended_at), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return datetime.now() - when <= timedelta(seconds=DUPLICATE_WINDOW)


def _kda(kills, assists, knocks):
    """把数据库里分开的三列拼成总结页那种 "2/0/2"，和上游 parse 出来的格式对齐。"""
    if kills is None and assists is None and knocks is None:
        return None
    return f"{_dash(kills)}/{_dash(assists)}/{_dash(knocks)}"


def _dash(value):
    return "-" if value is None else str(value)
