"""关闭数据页"全体玩家"（上游 0.4.0 新增）：标签页不显示、网络请求不触发。

## 为什么关

上游 0.4.0 在数据页加了"全体玩家"标签（`_stats_tabs.html` + `stats_all.html` +
`code/global_stats.py`，从 stats.evotracker.cn 下载汇总）。派喵分支的匿名统计
是强关的（`cloud/upstream.py: _patch_telemetry()` 把 `telemetry.consent` 换成永远
返回 False），而"全体玩家"只在开了匿名统计时才有内容 —— 留着一个永远锁死的
标签页只会让人困惑，所以整个入口一起去掉。

## 怎么关（三道，互相兜底）

1. **模板 preprocess**：把 stats.html / stats_all.html 里
   `{% include "_stats_tabs.html" %}` 那一行整段拿掉（连外面的 `{% with %}` 一起，
   留着也无害但干脆删干净）。`stats_all.html` 的其余内容保留 —— 直接访问
   /stats?view=all 时显示的是"锁住"的样子（telemetry 关着），不崩。
2. **包 `global_stats.allowed`**：永远返回 False，锁死判断（不依赖 telemetry 注入
   先跑）。
3. **包 `global_stats.fetch`**：永远返回 {"ok": False, ...}，不发请求、不写缓存文件。

## 降级

锚点找不到（上游又改了模板/函数名）就原样返回 / 不包，只记日志，Evo 照常跑。
幂等：preprocess 靠 env 标记位，函数包装靠函数自身的 `_paimiao_patched` 标记。
"""
from __future__ import annotations

import re

from cloud import state as cloud_state

# 开关（想临时恢复上游行为时设 EVO_PAIMIAO_GLOBAL_STATS=1）
ON = True

# stats.html 里的写法（上游 0.4.0）：{% with everyone=False %}{% include ... %}{% endwith %}
_WITH_RE = re.compile(
    r"\{%-?\s*with\s+everyone=(True|False)\s*-?%}\s*"
    r"\{%-?\s*include\s+[\"']_stats_tabs\.html[\"']\s*-?%}\s*"
    r"\{%-?\s*endwith\s*-?%}")

_INSTALLED = False


def install():
    """真正的挂钩在 register()（attach 时调用，见下）。这里只做存在性检查，幂等。"""
    global _INSTALLED
    _INSTALLED = True
    return True


def register(app):
    """panel_app 导入完成（bootstrap.attach）后调用：套模板 preprocess + 包 global_stats。

    不走 cloud/importhook.py 的 on_import("panel_app")：bootstrap 的 _PanelAppFinder
    自己拦截 panel_app，_SharedFinder 的回调轮不到触发（2026-10-25 实测）。
    """
    errors = []
    try:
        _install_template_hook(app.jinja_env)
    except Exception as exc:
        errors.append(f"模板钩子：{exc!r}")
    try:
        _patch_global_stats()
    except Exception as exc:
        errors.append(f"函数包装：{exc!r}")
    if errors:
        _log(f"关闭注入只完成了一部分（不影响其余功能）：{'；'.join(errors)}")


def _after_import(module):
    """（旧入口，panel_app 已导入时）：同 register。"""
    register(module.app)


def _install_template_hook(env):
    if getattr(env, "_paimiao_global_stats_preprocess", False):
        return
    original = env.preprocess

    def preprocess(source, name, filename=None):
        try:
            source = original(source, name, filename)
        except TypeError:
            source = original(source, name)
        return _patch_template(source)

    env.preprocess = preprocess
    env._paimiao_global_stats_preprocess = True
    _log("全体玩家标签页：preprocess 已挂上")


def _patch_template(source):
    if not source or not ON:
        return source
    # "全体玩家"标签：把 {% with %}{% include "_stats_tabs.html" %}{% endwith %} 整段拿掉。
    # 只有 `_stats_tabs.html` 是两个页面里唯一出现的写法（stats.html 是 everyone=False，
    # stats_all.html 是 everyone=True），一段正则都覆盖；找不到就原样返回（降级）。
    patched, n = _WITH_RE.subn("", source, count=1)
    if n and "_stats_tabs.html" not in patched:
        _log("全体玩家标签页：已从数据页移除")
        return patched
    return source


def _patch_global_stats():
    try:
        import global_stats
    except Exception:
        _log("全体玩家关闭：code/global_stats.py 没找到（老版本？跳过）")
        return
    if not getattr(global_stats.allowed, "_paimiao_patched", False):
        def allowed():
            return False

        allowed._paimiao_patched = True
        global_stats.allowed = allowed
    if not getattr(global_stats.fetch, "_paimiao_patched", False):
        def fetch(now=None):
            return {"ok": False, "error": "本分支未开启全体玩家数据"}

        fetch._paimiao_patched = True
        global_stats.fetch = fetch
    _log("全体玩家关闭：allowed/fetch 已锁死，不会发起下载")


def _log(message):
    try:
        cloud_state.log(f"[全体玩家] {message}")
    except Exception:
        pass
