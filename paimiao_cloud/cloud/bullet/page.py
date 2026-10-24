"""战绩页：子弹时间这一局的显示。纯内存注入，上游 `panel/` 一个字节不动。

要改的就两处（都在 `panel/templates/matches.html` 里）：

    ① 模式筛选加「混合模式·子弹时间」：
         `('ranked', 'pubs', 'other', 'wildcard')`
       → `('ranked', 'pubs', 'other', 'wildcard', 'mixed_bullet_time')`
    ② 详情页那行小字末尾补上「降级近战攻击 {{ r.melee }}」（只在子弹时间那几局）

**模式中文名不用补**：`mixed_bullet_time` 这一项 lobby_mode 注入时已经写进上游
MODES 了（「混合模式·子弹时间」），和大厅读到子弹时间时显示的名字一致。

另外上游 `view()` 返回的字典里没有 `bullet` 这个键，模板判断要用；
也不改上游 `view`，只包一层加一个键，签名不变。
（键名叫 bullet 是「这一局是不是子弹时间」的意思，和库里的模式键不是一回事。）

## 为什么详情页要动

用户 2026-10-24 的要求是「子弹时间按降级近战攻击这一行来定」。这一局的
「降级近战攻击」数字（实测是 0）上游本来就存在 `matches.melee` 里，但页面上
没写它 —— 认出来了却不显示，用户核对不了。所以子弹时间的局在详情那行小字里
补一段「降级近战攻击 0」，和外卡补「自我急救 / 最后幸存」是一个道理。

普通局一个字都不加：上游模板里根本没有这一段可写的锚点，找不到就原样返回。
"""
from __future__ import annotations

from functools import wraps

from cloud.bullet import rules

# 模板锚点。
#
# ① 筛选：上游是 `{% for key in ('ranked', 'pubs') %}`；外卡注入会先把它改成
#    `('ranked', 'pubs', 'other', 'wildcard')`，这里按最常见的几种写法各兜一次。
_LOOPS = (
    "{% for key in ('ranked', 'pubs') %}",
    "{% for key in ('ranked', 'pubs', 'other') %}",
    "{% for key in ('ranked', 'pubs', 'other', 'wildcard') %}",
)
_LOOP_NEW = "{% for key in ('ranked', 'pubs', 'other', 'wildcard', 'mixed_bullet_time') %}"

# ② 详情页那行小字：`· 急救 {{ r.revives }} · 重生 {{ r.respawns }}`。
#    外卡注入会把「急救」「重生」两段各包一层条件，所以这一段的写法有好几种可能；
#    我们只认**尾部**：小字的最后是「重生 …{{ r.respawns }}」或者它被换掉的写法。
#
#    做法：不碰「重生」那几个字，只认**取值本身** `{{ r.respawns }}`，在它后面接一段
#    只在子弹时间才渲染的文字。这样不管是上游原样、还是外卡已经改过的那几种写法，
#    锚点都在（外卡的锚点是「重生 {{ r.respawns }}」，改成「重生 {% if … %}」之后，
#    后面那串 `{{ r.respawns }}` 仍然原封不动），也不会和外卡抢同一段文字。
#
#    队友行（第 93 行）同一个取值串**前面**是 `m.` 不是 `r.`，那里匹配不到；
#    最后一行的 `r.rp_source` 接在末尾，我们插在它前面，位置正好。
_NOTE_OLD = "{{ r.respawns }}"
_NOTE_NEW = ("{{ r.respawns }}{% if r.bullet %} · " + rules.MELEE_LABEL + " {{ r.melee }}{% endif %}")


def register(app, module):
    """panel_app 加载完时调用（bootstrap.attach 里）。任何一步失败都只写日志。"""
    installed = []
    try:
        install_modes(module)
        installed.append("modes")
    except Exception as exc:
        _log(f"[子弹时间] 模式中文名没补上（不影响记录）：{exc!r}")
    try:
        install_view(module)
        installed.append("view")
    except Exception as exc:
        _log(f"[子弹时间] 战绩页数据没接上（不影响记录）：{exc!r}")
    try:
        install_preprocess(app.jinja_env)
        installed.append("templates")
    except Exception as exc:
        _log(f"[子弹时间] 模板没接上（页面按上游原样显示）：{exc!r}")
    return installed


# ---------------------------------------------------------------------------
# ① 模式中文名（兜底）
# ---------------------------------------------------------------------------

def install_modes(module):
    """确保 MODES 里有 mixed_bullet_time。幂等。

    正常情况下 lobby_mode 已经写进去了（「混合模式·子弹时间」），这里只在
    它的注入挂掉、或者上游 MODES 被换了一张新表时兜一下，**不覆盖已有的值**。
    """
    table = getattr(module, "MODES", None)
    if isinstance(table, dict) and not table.get(rules.MODE_KEY):
        table[rules.MODE_KEY] = rules.MODE_LABEL
    return table


# ---------------------------------------------------------------------------
# ② 战绩页数据：加一个「这一局是不是子弹时间」
# ---------------------------------------------------------------------------

def _is_bullet(row):
    try:
        return row["mode"] == rules.MODE_KEY
    except Exception:
        return False


def install_view(module):
    view = getattr(module, "view", None)
    if view is None:
        raise RuntimeError("上游没有 view()")
    if getattr(view, "_paimiao_bullet", False):
        return

    @wraps(view)
    def patched_view(row):
        data = view(row)
        try:
            data["bullet"] = _is_bullet(row)
        except Exception:
            data["bullet"] = False
        return data

    patched_view._paimiao_bullet = True     # type: ignore[attr-defined]
    module.view = patched_view


# ---------------------------------------------------------------------------
# ③ 模板
# ---------------------------------------------------------------------------

def install_preprocess(env):
    """套一层 jinja preprocess，和其他几层（upstream / notice / obs / wildcard）叠加。幂等。"""
    if env is None:
        raise RuntimeError("jinja_env 是空的")
    if getattr(env, "_paimiao_bullet_preprocess", False):
        return
    original = env.preprocess

    def preprocess(source, name, filename=None):
        try:
            source = original(source, name, filename)
        except TypeError:                 # 老版本 jinja 的 preprocess 只有两个参数
            source = original(source, name)
        return patch_source(source)

    env.preprocess = preprocess
    env._paimiao_bullet_preprocess = True


def patch_source(source: str) -> str:
    """对一个模板的源码做子弹时间相关的修改。找不到锚点就原样返回，绝不抛异常。

    **必须幂等**：已经改过就原样返回（上游改版、重复调用都不会套两层）。
    """
    if not source:
        return source

    if _LOOP_NEW not in source:
        for old in _LOOPS:
            if old in source:
                source = source.replace(old, _LOOP_NEW, 1)
                break

    source = _rewrite(source, _NOTE_OLD, _NOTE_NEW)
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
