"""战绩页：外卡局的显示。纯内存注入，上游 `panel/` 一个字节不动。

要改的就三处（都在 `panel/templates/matches.html` 里）：

    ① 模式筛选加「外卡」：`('ranked', 'pubs')` → `('ranked', 'pubs', 'other', 'wildcard')`
    ② 队友那一行的「急救 / 重生」→ 外卡局写「自我急救 / 最后幸存」
    ③ 详情页那行小字里的「急救 / 重生」→ 同上

还有一处不在模板里：上游 `panel_app.py:52` 的

    MODES = {"ranked": "排位", "pubs": "匹配", "other": "其他"}

里面没有 wildcard，`MODES.get(row["mode"], "—")` 会把外卡局显示成「—」。
按注入规范 3.1 **原地改这张表**（update），不整个换掉对象 —— 别人手里可能还攥着它。

另外上游 `view()` 返回的字典里没有 `wildcard` 这个键，模板判断要用；
也不改上游 `view`，只包一层加一个键，签名不变。
"""
from __future__ import annotations

from functools import wraps

from cloud.wildcard import rules

# 模板锚点。
#
# 外卡局要在「急救 / 重生」前面多出「自我 / 最后幸」这几个字。做法是**整段换掉**
# 那一小段 HTML（带标签、带变量），而不是往原来的字前面插一个空判断 —— 2026-10-24
# 实测过「插前缀」的写法，`急救 {% if ... %}自我{% endif %}{{ m.revives }}` 渲染出来是
# 「急救 自我1 · 最后幸2」（`{{ }}` 前面必须有的空格把词劈成了两半），所以放弃。
#
# 整段换的代价是：`{% if %}` 的换行和空格会被 Jinja 的 trim 吃掉，所以这里
#   · 两个分支的字**都写全**（普通分支也要把「急救」「重生」写出来）
#   · `<span>` 和我方文字之间不留空格，免得 `{% if %}` 的行尾把空格吃掉
#   · 换完之后判重靠 `wildcard_` 前缀 + 锚点已经不在，重复套不会叠两层
_LOOP_OLD = "{% for key in ('ranked', 'pubs') %}"
_LOOP_MID = "{% for key in ('ranked', 'pubs', 'other') %}"
_LOOP_NEW = "{% for key in ('ranked', 'pubs', 'other', 'wildcard') %}"

# 队友行：`<span class="mate-extra">急救 {{ m.revives }} · 重生 {{ m.respawns }}</span>`
_MATE_OLD = ('<span class="mate-extra">急救 {{ m.revives }} · 重生 {{ m.respawns }}</span>')
_MATE_NEW = ('<span class="mate-extra">'
             "{% if r.wildcard %}" + rules.REVIVE_LABEL + "{% else %}急救{% endif %}"
             " {{ m.revives }} · "
             "{% if r.wildcard %}" + rules.RESPAWN_LABEL + "{% else %}重生{% endif %}"
             " {{ m.respawns }}</span>")

# 详情页那行小字：`· 急救 {{ r.revives }} · 重生 {{ r.respawns }}`
_NOTE_REVIVE_OLD = "急救 {{ r.revives }} · "
_NOTE_REVIVE_NEW = ("{% if r.wildcard %}" + rules.REVIVE_LABEL + "{% else %}急救{% endif %}"
                    " {{ r.revives }} · ")

_NOTE_SPAWN_OLD = "重生 {{ r.respawns }}"
_NOTE_SPAWN_NEW = ("{% if r.wildcard %}" + rules.RESPAWN_LABEL + "{% else %}重生{% endif %}"
                   " {{ r.respawns }}")


def register(app, module):
    """panel_app 加载完时调用（bootstrap.attach 里）。任何一步失败都只写日志。"""
    installed = []
    try:
        install_modes(module)
        installed.append("modes")
    except Exception as exc:
        _log(f"[外卡模式] 模式中文名没补上（不影响记录）：{exc!r}")
    try:
        install_view(module)
        installed.append("view")
    except Exception as exc:
        _log(f"[外卡模式] 战绩页数据没接上（不影响记录）：{exc!r}")
    try:
        install_preprocess(app.jinja_env)
        installed.append("templates")
    except Exception as exc:
        _log(f"[外卡模式] 模板没接上（页面按上游原样显示）：{exc!r}")
    return installed


# ---------------------------------------------------------------------------
# ① 模式中文名
# ---------------------------------------------------------------------------

def install_modes(module):
    """把 "wildcard" → "外卡" 原地补进上游的 MODES。

    lobby_mode/inject.py 的 `_patch_mode_labels()` 也做这件事（那里顺手补了
    post_match.MODE_LABELS，日志里也用得上），两处都是 `table[key] = ...`，幂等。
    """
    table = getattr(module, "MODES", None)
    if isinstance(table, dict):
        table[rules.MODE_KEY] = rules.MODE_LABEL
    return table


# ---------------------------------------------------------------------------
# ② 战绩页数据：加一个「这一局是不是外卡」
# ---------------------------------------------------------------------------

def install_view(module):
    view = getattr(module, "view", None)
    if view is None:
        raise RuntimeError("上游没有 view()")
    if getattr(view, "_paimiao_wildcard", False):
        return

    @wraps(view)
    def patched_view(row):
        data = view(row)
        try:
            data["wildcard"] = row["mode"] == rules.MODE_KEY
        except Exception:
            data["wildcard"] = False
        return data

    patched_view._paimiao_wildcard = True     # type: ignore[attr-defined]
    module.view = patched_view


# ---------------------------------------------------------------------------
# ③ 模板
# ---------------------------------------------------------------------------

def install_preprocess(env):
    """套一层 jinja preprocess，和其他几层（upstream / notice / obs）叠加。幂等。"""
    if env is None:
        raise RuntimeError("jinja_env 是空的")
    if getattr(env, "_paimiao_wildcard_preprocess", False):
        return
    original = env.preprocess

    def preprocess(source, name, filename=None):
        try:
            source = original(source, name, filename)
        except TypeError:                 # 老版本 jinja 的 preprocess 只有两个参数
            source = original(source, name)
        return patch_source(source)

    env.preprocess = preprocess
    env._paimiao_wildcard_preprocess = True


def patch_source(source: str) -> str:
    """对一个模板的源码做外卡相关的修改。找不到锚点就原样返回，绝不抛异常。

    **必须幂等**：锚点不存在、或者已经改过，就原样返回（上游改版后重复调用也不会套两层）。
    """
    if not source:
        return source

    if _LOOP_OLD in source:
        source = source.replace(_LOOP_OLD, _LOOP_NEW, 1)
    elif _LOOP_MID in source:
        # 上游将来自己加了「其他」：只补「外卡」
        source = source.replace(_LOOP_MID, _LOOP_NEW, 1)

    # 详情页那行小字先换（锚点是 `急救 {{ r.revives }} · `），再换队友行。
    # 「重生」那一段和队友行整段不重叠：队友行整段换掉时里面已经带了新写法，
    # 所以队友行要**先换**，再换详情页的「重生」。
    source = _rewrite(source, _MATE_OLD, _MATE_NEW)                     # 队友行整段换
    source = _rewrite(source, _NOTE_REVIVE_OLD, _NOTE_REVIVE_NEW)       # 详情页：急救
    source = _rewrite(source, _NOTE_SPAWN_OLD, _NOTE_SPAWN_NEW)         # 详情页：重生
    return source


def _rewrite(source, old, new):
    """换一处文本；已经换过就不动，找不到锚点也不动（宁可不显示，也不乱插）。"""
    if new in source or old not in source:
        return source
    return source.replace(old, new, 1)


def _log(message):
    try:
        from cloud import state as cloud_state      # noqa: PLC0415

        cloud_state.log(message)
    except Exception:
        pass
