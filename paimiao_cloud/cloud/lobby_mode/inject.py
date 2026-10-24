"""注入点：在准备界面认出这一局是什么模式（上游文件 0 改动）。

## 挂在哪

和 `cloud/obs/inject.py` 是同一套写法（meta_path 钩子等 live_collect 导入），
只是接进去以后动的东西不同：

    live_collect 导入完
        ↓ 换掉 live_collect 命名空间里的 ScreenDetector（子类，见 screen.py）
        ↓ 把「大厅卡片粗筛」加进 ScreenDetector.detect（在识别老画面之前先看一眼）

## 为什么要在 detect() 里加粗筛

2026-10-24 实测：用户给的 6 张大厅截图里，只有 1 张能被上游的「大厅」认出来
（相似度 0.87），另外 5 张都是 0.12~0.15 —— 门槛 0.35，全部漏掉。
原因是那张大厅参照图（`config/templates/lobby.png`）取的是「排位点数」那一小行，
模板比对时那行字不在同一个位置、或者字号不同就不像。

所以：**老路照旧走**（上游怎么判就怎么判），只在它前面加一步便宜的粗筛 ——
卡片色条在，就当这一帧算大厅，顺手把模式读出来。
上游那张参照图要不要重做，是上游的事，这里不改 config/。

降级：任何一步失败都只写日志，Evo 照常跑（只是少了一项"提前认出模式"）。
"""
from __future__ import annotations

import sys

_INSTALLED = False


def install():
    """登记「等 live_collect 导入后接管 ScreenDetector」。幂等。

    **不再自己挂 sys.meta_path finder**（2026-10-24 实测的大 bug）：
    Python 只问第一个返回 spec 的 finder，进程里同时有 4 个注入 finder 时，
    排第一的 legend_pick 拿走后，这里的 `_after_import` 永远不会跑 ——
    日志里「大厅模式」一行都没有，用户看到的就是「大厅模式还是没启动」。
    现在统一交给 `cloud/importhook.py`：一个 finder，所有注入方登记回调。
    """
    global _INSTALLED
    if _INSTALLED:
        return True
    _INSTALLED = True
    try:
        from cloud import importhook  # noqa: PLC0415

        importhook.on_import("live_collect", _after_import, label="大厅模式")
    except Exception:
        _INSTALLED = False
        raise
    return True


def _after_import(module):
    """live_collect 已导入：把 ScreenDetector 换成我们的子类，并补上中文名。

    **每一步各自兜异常**（2026-10-24 实测的教训）：原来四步串在一个 try 里，
    任何一步抛了（包括它自己 except 没抓住的 BaseException），后面全部装不上，
    而且日志里只留下前面那一行 —— 19:32、19:39 两次重启都是这样，
    「已补上中文名」之后什么都没有，查了半天以为是安装逻辑坏了。
    现在一步失败只影响自己，后面的照装，并且失败那步会写明「哪个步骤」。
    """
    steps = (
        ("补中文名", lambda: _patch_mode_labels()),
        ("模式喂给对局", lambda: _install_mode_feed(module)),
        ("地图回推", lambda: _install_map_guess()),
        ("接管大厅判定", lambda: _install_screen_detector(module)),
    )
    for name, step in steps:
        try:
            step()
        except BaseException as exc:          # noqa: BLE001  含 SystemExit/KeyboardInterrupt
            try:
                import traceback  # noqa: PLC0415

                where = traceback.extract_tb(exc.__traceback__)
                spot = (f"{where[-1].filename.rsplit(chr(92), 1)[-1]}:{where[-1].lineno}"
                        if where else "?")
                _log(f"[大厅模式] 步骤「{name}」没装上："
                     f"{type(exc).__name__}: {exc} @ {spot}")
            except Exception:                 # 连日志都写不出就别再折腾了
                pass


def _install_screen_detector(module):
    """把 ScreenDetector 换成带「顺手读大厅模式」的子类。"""
    old = getattr(module, "ScreenDetector", None)
    if old is None:
        _log("[大厅模式] 上游没有 ScreenDetector，跳过接管")
        return
    if getattr(old, "_paimiao_lobby_mode", False):
        _log("[大厅模式] 大厅判定已经接管过了（跳过）")
        return
    from cloud.lobby_mode import screen as lobby_screen  # noqa: PLC0415

    new = lobby_screen.build(old)
    module.ScreenDetector = new
    # screens 命名空间里那个也换掉：否则两处名字指向不同的类，谁 new 了哪个全看运气
    screens_module = sys.modules.get("screens")
    base = getattr(screens_module, "ScreenDetector", None) if screens_module else None
    if base is old:
        screens_module.ScreenDetector = new
    _log("[大厅模式] 已接管：大厅里读左下角模式卡片（进开局画面仍然照旧再读一次）")


def _install_mode_feed(module):
    """大厅读到的模式 → 喂给这一局（写进 matches.mode）。失败只记日志。"""
    try:
        from cloud.lobby_mode import match_mode  # noqa: PLC0415

        match_mode.install(module)
        _log("[大厅模式] 已接上：大厅读到的模式会记进这一局的 mode")
    except Exception as exc:
        # 把出错的文件:行号也写进去 —— 2026-10-24 19:32 只写一句 repr，
        # 看不出是 install 里哪一步挂的，查了两轮。
        import traceback  # noqa: PLC0415

        where = traceback.extract_tb(exc.__traceback__)
        spot = f"{where[-1].filename.rsplit(chr(92), 1)[-1]}:{where[-1].lineno}" if where else "?"
        _log(f"[大厅模式] 模式写库没接上（不影响记录）：{exc!r} @ {spot}")


def _install_map_guess():
    """没读到地图时按时间回推（见 map_guess.py）。失败只记日志。"""
    try:
        from cloud.lobby_mode import map_guess  # noqa: PLC0415

        map_guess.install(None)
        _log("[地图回推] 已接上：总结页没读到地图时，按「结束 − 最长存活 − 1 分钟」查地图轮换"
             "（只在启用派喵云同步后才真的联网）")
    except Exception as exc:
        _log("[地图回推] 没接上（不影响记录）：%r" % (exc,))


def _patch_mode_labels():
    """把 wildcard / mixed_control / bot 的中文名补进上游那两张字典。

    上游只认识 ranked / pubs / other：
      code/post_match.py:61   MODE_LABELS = {"ranked": "排位", "pubs": "匹配", "other": "其他"}
      code/panel_app.py:52    MODES = {"ranked": "排位", "pubs": "匹配", "other": "其他"}
    不补的话，`MODE_LABELS.get(v, v)` 会把英文原样显示在页面和日志里。

    按注入规范 3.1：**必须原地改**（update），不能整个换掉对象 —— 别人可能已经
    持有了那个 dict 的引用。幂等：反复调用只是重复覆盖同样的键。
    """
    from cloud.lobby_mode import rules  # noqa: PLC0415

    patched = []
    for name, attr in (("post_match", "MODE_LABELS"), ("panel_app", "MODES")):
        target = sys.modules.get(name)
        table = getattr(target, attr, None) if target is not None else None
        if isinstance(table, dict):
            try:
                table.update(rules.MODE_LABELS)
                patched.append("%s.%s" % (name, attr))
            except Exception:
                pass
    if patched:
        _log("[大厅模式] 已补上中文名：%s" % "、".join(patched))


def _log(message):
    """写一行日志。**两条路都试**（2026-10-24 改）：

    原来 `cloud_state.log` 成功就 return，不再 print；而记录子进程是独立进程，
    `print` 只进它自己的控制台、不落 cloud.log。反过来说 —— 一旦子进程里
    `cloud_state.log` 悄悄失败（比如 data_dir 那一刻还算不出来），这条日志就
    彻底消失，两边都没有，看起来就像「这行代码根本没执行」。
    19:32、19:39 两次重启后 `install 开始` 一行都没有，正是这个坑。

    现在：能写文件就写文件，**同时**往控制台打一份。重复一点无所谓，
    日志丢了的代价是查不出问题。
    """
    wrote = False
    try:
        from cloud import state as cloud_state  # noqa: PLC0415

        cloud_state.log(message)
        wrote = True
    except Exception:
        pass
    if not wrote:
        try:
            print(message, flush=True)
        except Exception:
            pass
