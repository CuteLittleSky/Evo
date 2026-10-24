"""把大厅里读到的模式补给「这一局」，最后写进 matches.mode。

## 为什么需要这一步

上游只在**开局画面**顶部读模式（`config/match_start.json`），而且必须读到
「排位赛」三个字才算排位。没截到那几帧，这一局的 mode 就永远是空的。
`cloud/lobby_mode` 能在准备界面提前读到模式，但它原来只是「读给你看」，没有写进库。

这个文件把两件事接起来，**不碰数据库、不碰上游写库的地方**：

    大厅读到 mode  →  记成「待用」
    这一局真的开始  →  交给上游原有的 post.on_match_start()，它会写进 self.info
    上游建局时      →  _base_fields() 自己从 self.info 取 mode（一个字都不用改）

## 两道交付时机 + 每局复位（2026-10-24 用户澄清后重写）

**第一道：开局画面出现时。** 上游自己那一句
`if screen == "match_start" and not post.info_complete()` 会先跑；它没读到模式，
我们就把大厅读到的补上。

**第二道：刚进对局时。** 有些局根本没有开局横幅（跳得快、或者这一局就没有），
对局中右上角也没有排位分，这时只能靠大厅那一次读数。
所以**第一次认出「对局中」画面时也补一次**。

第二道接 `InMatchWatcher.update` 而不是 `ScreenWatcher.update`：
`code/live_collect.py` 的主循环顺序是
    426  in_match.update(...)   ← 对局中的数据
    428  other.update(...)      ← ScreenWatcher，也就是第一道时机
对局中那一行**先跑**，所以第二道挂在更早的 426 行上，补完才轮到 428。

**每局复位：用户 2026-10-24 原话（m00515）**

    「我是通过总结界面右上角继续匹配的……你从大厅读出来那个模式之后，
      不仅可以在这一局使用，而且**只要没有返回大厅，它就一直有效**。」

也就是说第 2、3、4 局**每一局**都要把同一个模式交出去，不是"交一次就完了"。
而「继续匹配」这条路**根本不经过大厅**：总结页点继续匹配 → 直接进下一局，
`ScreenWatcher` 一帧 `lobby` 都给不了，也给不出新的 `match_start`（它一直停在 summary 上）。
所以复位的唯一可靠时刻，是「对这一局来说，现在才开打」——

`InMatchWatcher._reset(...)`：上游每次开新一局都会调它
（`live_collect.py:75/102`、`in_match.py:102/190`），一局正好一次、不重不漏。
我们把 `_FEED_DONE` 在它里复位，第二道时机于是每局都能重新交一次。

原来只在 `screen == "match_start"` 时复位，而「继续匹配」没有开局横幅 →
`_FEED_DONE` 一直是 True → 第 2 局以后一帧都不补。这就是用户报的
「继续匹配之后模式读不出来」。

## 为什么第一道必须在 original **之后**（2026-10-24 实测 bug）

`code/live_collect.py:249-254` 的 `ScreenWatcher.update` 一进来就做两件事：

```python
if screen != self.current:              # 这一帧换了画面
    self.leave()                        # ← 顺手把上一局的总结页/经验值页结算掉
    if screen in ("match_start", "lobby"):
        self.post.finish()              # ← 写上一局的行，然后 self.info 清空
```

`finish()` 里 `self.info, self.unknown_maps = {}, set()`（post_match.py:395）。

原来 `_feed()` 写在 `original(...)` **之前**，于是从大厅进开局画面那一帧：

    我们 _feed → info["mode"] = "wildcard"
    original → 进 match_start → finish() → info 被清空 → 模式没了
    original → 再跑 `on_match_start(read_match_start(...))`，横幅没截到 → 还是空

`matches.mode` 永远是 NULL。这就是「大厅明明读到了、打完一局模式还是没显示」的真正原因
（match_id 15，2026-10-24 14:52:27，mode 为空，而 14:45:48 日志里明确读到过「外卡」）。
必须先让 `original` 把上一局交掉、把 `info` 清干净，再往里放这一局的模式。

## 为什么不能直接写 self.info

`PostMatch.finish()` 只在 `had_match` 为真（这一局真的打过）时才清空 `self.info`（post_match.py:391-395）。
如果在大厅读到模式就直接写进 info，那么「在大厅切了模式、但一局都没打就关掉游戏」这种情况，
这个模式会残留下来，被记到下一局头上。

所以模式先存在这里（`_CURRENT`，代表「这一轮」），等这一局真的开始了才算。

## 第三道来源：每一张截图都顺手看一眼大厅（2026-10-24 补）

`ScreenWatcher.update` 只在上游**改判**画面时才会被调用（`if screen != self.current`）。
而用户打完一局常常一直停在结算界面：小队淘汰 → 排名第 3 → 名次横幅散去，
这几十秒里 `screen` 一直是同一个值，一次都不触发。

实测 2026-10-24 16:25:28 打完第 17 局之后，50 秒里一帧大厅都没被认出来
（数据库 `lobby_readings` 到 17:00 都没有新的一条），然后下一帧直接是开局横幅 ——
`ScreenWatcher.update` 这时才把上一局交掉、`finish()` 清空 `self.info`。
这一段里我们的卡片读取一次都没跑过，所以第 18 局的模式是空的。

所以再挂一道：截屏 (`dxcam.create(...).grab`) 的返回值上顺手过一次
`screen.handle()`。它开头就是一张卡片色条的结构检查（实测 0.8~1.8 毫秒，同一帧只算一次），不像就立刻返回，
**对局中一次识别都不会多跑**（对局画面没有那条色条）。
判据、节流、日志、待用值和 `ScreenDetector.detect` 那条路是同一份代码，不会有两套标准。

## 优先级

开局画面读到的模式**永远优先**：几个时机都先看 `post.info["mode"]`，
上游有了就不插手。`_CURRENT` 不随「交出去」而清空 —— 只要没回大厅，下一局继续用它。
"""
from __future__ import annotations

import sys
import time
from typing import Any

# 这一轮的模式：{"mode": "ranked", "raw": "排位赛", "at": monotonic}
#
# 「这一轮」= 从上一次回到大厅开始，到下一次回到大厅为止。用户 2026-10-24 定：
# 大厅读到的模式**只要没有返回大厅就一直有效**（打完一局点「继续匹配」不回大厅，
# 第 2、3、4 局都算同一个模式）。所以这里**不设过期时间**，只在两个时刻清：
#   * 回到大厅且这一帧没读出模式  → saw_lobby_blank()
#   * 回到大厅又读到了新模式      → remember() 覆盖
_CURRENT: dict[str, Any] = {}

# 这一轮是不是已经交给上游一次了（对局中每 0.3 秒一帧，不能每帧都交）
_FEED_DONE = {"done": False}

# 截屏钩子：当前这台记录程序的识别引擎（由 collect 开头那行建引擎时抓到）
_ENGINE = {"engine": None}

# 建过的所有相机（引擎晚于相机建好时要回过来补挂，见 _hook_camera）
_CAMERAS: list[Any] = []

_INSTALLED = False


def _log(message):
    """写一行到 Evo 的日志（data/cloud-sync/cloud.log），带「大厅模式」前缀方便搜。"""
    try:
        from cloud import state as cloud_state      # noqa: PLC0415

        cloud_state.log("[大厅模式] %s" % message)
    except Exception:
        pass        # 日志写不下绝不影响记录


def remember(mode, raw=None):
    """大厅读到模式时调用（由 screen.py 调）：存成「当前这一轮的模式」。

    ## 用户 2026-10-24 定的规矩（本轮按它重写）

    用户原话：「你从大厅读出来那个模式之后，不仅可以在这一局使用，而且**只要没有返回大厅，
    它就一直有效**。」

    所以这里的读数**不是**「一共只能用一次」的待用值，而是「这一轮的模式」：

        读到大厅的模式  →  _CURRENT 记住（随时刷新，取消换模式就以最后一次为准）
        打完一局继续匹配 →  _CURRENT 仍然有效 → 第 2、3、4 局全都用它
        返回大厅        →  forget() 清掉；这一轮没读到就作废（见 saw_lobby_blank）
        下一轮在大厅读到 →  remember() 覆盖成新的

    原来写成「一局用一次」（create/take），第 1 局用掉之后 `_COMING` 永远是 False，
    后面几局只能靠 `_FEED_DONE` 这个标记撑着，标记一乱就整轮丢模式 —— 2026-10-24
    第 18 局就是这么丢的（见 b10/b11 里的排查记录）。
    """
    if not mode:
        return
    _CURRENT.clear()
    _CURRENT.update(mode=mode, raw=raw, at=time.monotonic())
    # 新一轮读完：这一轮还没交出去过，所以「已经交过了」复位
    _FEED_DONE["done"] = False


def saw_lobby_blank():
    """回到大厅、确认是大厅，但这一轮**没读到模式**（过渡帧、标题还没渲染完）。

    2026-10-24 用户选了保守做法（方案 A）：这种时候把模式作废，不要拿上一局的读数顶上。

    为什么必须作废 —— 一个真实会发生的错：

        打完一局排位 → 回大厅 → 切到「外卡」再点准备 →
        这一次 remember() 没成功（画面还没稳，found=0）→
        手里还是上一轮的「排位赛」→ **外卡那局被记成排位**。

    注意它只在「回到/停留在大厅、但这一帧一个字没读出来」时调用（screen.py 的 _blank）。
    打完一局停在结算界面、或者已经进了下一局（Apex 的「继续匹配」不回大厅）时不会调用，
    那条路上模式一直有效 —— 正是用户要的。
    """
    _CURRENT.clear()
    _FEED_DONE["done"] = False


def current() -> str | None:
    """这一轮的模式（大厅读到的，还没回大厅作废）。只读，用于日志。"""
    return _CURRENT.get("mode")


def _mark_fed():
    """这一轮已经交给上游一次了：后面的帧不再重复交（一局只交一次）。"""
    _FEED_DONE["done"] = True


def install(live_collect_module):
    """把上面这套接到上游的 ScreenWatcher / InMatchWatcher 上。幂等。

    **幂等不等于"早退就不做事"**（2026-10-24 19:32 实测踩过）：
    原来 `if _INSTALLED: return` 和 `update 已包过: return` 都是直接返回，
    把 `_install_capture_hook()` 一起跳过了 —— 只要 install 被调两次（或者
    ScreenWatcher.update 已经被包过），截屏钩子就永远装不上，日志里也就
    永远没有「已接上截屏钩子」那一行。现在两条跳过路径都先补装钩子再返回。
    """
    global _INSTALLED
    if _INSTALLED:
        _log("install 已经装过了（补装截屏钩子后返回）")
        _safe_capture_hook()
        return
    _log("install 开始")
    watcher = getattr(live_collect_module, "ScreenWatcher", None)
    if watcher is None:
        raise RuntimeError("上游没有 ScreenWatcher，跳过（不影响记录）")
    original = watcher.update
    if getattr(original, "_paimiao_mode_feed", False):
        _log("install：update 已经包过（补装截屏钩子后返回）")
        _safe_capture_hook()
        _INSTALLED = True
        return

    # 只做交付，**不做清理**（清理的时机见 forget()：由大厅新读数覆盖）。
    #
    # 必须放在 original **之后**（2026-10-24 实测 bug，见下面「为什么必须在 original 之后」）。
    def update(self, screen, frame, now):
        try:
            # ① 一局的开头：上游 ScreenWatcher.update 在「改判成开局画面/大厅」的那一帧
            #    已经调过 post.finish()（上一局写库、self.info 清空）。我们排在 original
            #    之后，所以这里交进去的模式一定落在**新的一局**上。
            #
            #    2026-10-24 补：原来只在 screen == "match_start" 时补，而 Apex 的
            #    「继续匹配」（总结页右上角排队）**不一定有开局横幅**，于是第 2 局以后
            #    _FEED_DONE 一直是 True，一帧都不补 —— 这就是「继续匹配之后模式读不出来」。
            #
            #    这里不按「一局开头」复位：ScreenWatcher 是状态机，`frames` 表示这个状态
            #    已经待了几帧，拿它判断"是不是刚切过来"只会写出一堆猜的代码。真正决定
            #    成败的入口在 _install_in_match（对局开始那一刻），见那里的说明。
            if screen == "match_start":
                _FEED_DONE["done"] = False
        except Exception:
            pass
        result = original(self, screen, frame, now)
        if screen == "match_start":
            _feed(self, now)
        return result

    update._paimiao_mode_feed = True      # type: ignore[attr-defined]
    watcher.update = update
    _install_in_match(live_collect_module)
    _safe_capture_hook()
    _INSTALLED = True
    # 这行说明「全部装好了」。真正的「已接上截屏钩子」要等记录子进程里建出识别引擎
    # 才会打印（那一句在 HookedEngine.__init__ 里）—— 没进游戏时本来就看不到它，
    # 2026-10-24 19:32 我误把它当成安装失败的证据，白等了两轮。
    _log("install 完成：模式喂给对局 + 截屏钩子都已装好"
         "（进游戏建出识别引擎时会再打印一行「已接上截屏钩子」）")


def _install_in_match(live_collect_module):
    """第二道时机（见文档）：对局画面第一帧就交一次模式，不等开局横幅。

    挂在**实例**上，不挂在类上（见 _wrap_instance 的说明）。原来挂在类上，
    于是「每开一局复位」这个关键动作没地方挂，只能靠 `active` 自己比状态 ——
    那正是「继续匹配之后模式读不出来」的根源（m00515）。
    """
    in_match_cls = getattr(live_collect_module, "InMatchWatcher", None)
    if in_match_cls is None:
        return
    if getattr(in_match_cls.__init__, "_paimiao_mode_bind", False):
        return
    original_init = in_match_cls.__init__

    def __init__(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        _wrap_instance(self)

    __init__._paimiao_mode_bind = True      # type: ignore[attr-defined]
    in_match_cls.__init__ = __init__


def _wrap_instance(self):
    """给**这个 InMatchWatcher 实例**挂上两件事（包过就跳过，幂等）：

        update  对局画面第一帧补一次模式（第二道时机）
        _reset  每开一局复位「这一轮已经交过了」（「继续匹配」那条路的关键）

    为什么按实例挂、不挂类：套错一层的运气成本太高 —— 2026-10-24 前面两版都栽在
    这里（`screen == "in_match"` 永远为假；`_PENDING`/`_FED` 变成一次性）。挂在
    实例上，`self.post`、`self.active`、`self._reset` 都是眼前这个对象的真东西。
    """
    if getattr(self, "_paimiao_mode_instance", False):
        return
    self._paimiao_mode_instance = True

    # —— update：对局画面第一帧 ——
    update = self.update

    def wrapped_update(screen, frame, now):
        try:
            # 对局中的帧 screen 是 None（不是 "in_match"，那个字符串根本不存在）；
            # 再加上 active：准备界面、匹配中也是 None，那时候交出去会被记到别的局上。
            if screen is None and getattr(self, "active", False) and not _FEED_DONE["done"]:
                if _CURRENT.get("mode"):
                    _feed(self, now)
                else:
                    _mark_fed()     # 这一轮手里没有模式：这一局不再问了
        except Exception:
            pass        # 补不上就退回到"这一局模式留空"，绝不影响记录
        return update(screen, frame, now)

    self.update = wrapped_update

    # —— _reset：每开一局复位 ——
    #
    # 上游每次开新一局都会调 _reset（live_collect.py:75/102、in_match.py:102/190），
    # 一局正好一次、不重不漏。**这是「继续匹配」那条路唯一的入口**：
    # 总结页点继续匹配 → 不回大厅 → 直接进下一局，ScreenWatcher 一帧 lobby 都给不了。
    reset = getattr(self, "_reset", None)

    def wrapped_reset(saw_start=None, *args, **kwargs):
        try:
            _FEED_DONE["done"] = False
        except Exception:
            pass
        return reset(saw_start, *args, **kwargs)

    if reset is not None:
        self._reset = wrapped_reset


def _feed(watcher, now):
    """把大厅读到的模式交给上游的 PostMatch。

    参数是 **watcher**（ScreenWatcher / InMatchWatcher），因为对局中那道时机拿到的
    也是 InMatchWatcher —— 两边都从 watcher.post 取 PostMatch。原来这里参数名叫 post，
    却一直被传成 watcher，`getattr(watcher, "info", None)` 永远是 None，于是整条链路
    静默失效（try/except 吞掉了），表现就是「模式一个都没记上」。
    """
    post = getattr(watcher, "post", None)
    if post is None:
        return
    info = getattr(post, "info", None) or {}
    # 上游已经读到模式了（开局画面顶部那行），以它为准
    if info.get("mode") is not None:
        if _CURRENT.get("mode"):
            _log("补给跳过：开局画面已经读到 %s（大厅读到的是 %s）"
                 % (info["mode"], _CURRENT.get("mode")))
        _mark_fed()
        return
    # 这一局已经补过（或者已经确认没得补），别再往里塞
    if _FEED_DONE["done"]:
        return
    mode = _CURRENT.get("mode")
    if mode is None:
        return
    try:
        # 复用上游自己的入口：它会写进 self.info，建局时 _base_fields() 自然带上。
        # map 传 None —— 这里只补模式，地图仍然只由开局画面决定，绝不猜。
        post.on_match_start({"mode": mode, "map": None, "raw_mode": "", "raw_map": ""}, now)
        # 记一笔：用户 2026-10-24 要能看出"这一局的模式是不是大厅给的"
        _log("补给这一局：%s（来自大厅读数）" % mode)
    except Exception as exc:
        _log("补给失败（这一局模式留空）：%r" % (exc,))


# ---------------------------------------------------------------------------
# 截屏钩子：每一张截图都顺手看一眼是不是准备界面（见开头「第三道来源」）
# ---------------------------------------------------------------------------

def _safe_capture_hook():
    """装截屏钩子，**任何异常都不许往外抛**（注入层不能影响上游记录）。

    和 `_install_capture_hook()` 的区别只有一个：这里兜住异常并写日志。
    `install()` 的跳过路径用它 —— 那几条路是「模式已经接好、只是可能没装钩子」，
    钩子出问题绝不能把 install 搞崩。
    """
    try:
        _install_capture_hook()
    except Exception as exc:                                  # noqa: BLE001
        _log(f"截屏钩子没接上（不影响记录）：{exc!r}")


def _install_capture_hook():
    """包住 dxcam 那台相机的 grab()。

    为什么挂在这里：`ScreenWatcher.update` 只在画面**改判**时才被调（上游
    `live_collect.py:251` 那行 `if screen != self.current`），而用户打完一局往往一直停在
    结算界面，那几十秒里一次都不触发（2026-10-24 第 18 局模式就是这么丢的）。
    截屏是主循环里**每张**都会走的那一步，挂在这里才不漏。

    代价：每张截图多一次卡片色条的结构检查（实测 0.8~1.8 毫秒，见 rules.CARD_PROBE；
    上游那条路已经算过的帧直接取缓存，不重算）。
    不像准备界面就立刻返回，一次识别都不会多跑 —— 对局画面没有那条色条。

    找不到 dxcam / 抓不到识别引擎时只写一行日志（降级成原来的行为，不影响记录）。
    """
    module = sys.modules.get("dxcam")
    if module is None:
        try:
            import dxcam as module      # noqa: PLC0415
        except Exception as exc:
            _log("截屏钩子没接上（不影响记录）：%r" % (exc,))
            return
    create = getattr(module, "create", None)
    if create is None or getattr(create, "_paimiao_capture_hook", False):
        return
    real_create = create
    _engine_from_create_args()

    def create(*args, **kwargs):
        camera = real_create(*args, **kwargs)
        _hook_camera(camera)
        return camera

    create._paimiao_capture_hook = True      # type: ignore[attr-defined]
    module.create = create


def _hook_camera(camera):
    """给一台相机挂上「每张截图都看一眼」。已经挂过就直接返回。

    **不能在这里判断引擎建没建好**（2026-10-24 实测踩过）：
    `code/live_collect.py:373` 先 `dxcam.create(...)`，`:375` 才 `LazyEngine(...)`，
    所以在 create() 那一刻引擎必然是 None。原来写成 `if engine_for():` → 永远不成立 →
    钩子一张都没挂上，18:20 那次启动之后 lobby_mode.json 一直停在 16:11，
    第 19、20 局 mode 全空（用户报的「继续匹配读不出来模式」）。

    现在改成：这里先挂上（`_capture` 自己会检查引擎好了没有，没好就直接返回），
    引擎建好之后再 `_hook_camera` 补一遍（处理反过来的顺序）。
    """
    if camera is None or getattr(camera, "_paimiao_capture_hook", False):
        return camera
    try:
        original = camera.grab

        def grab(*a, **k):
            frame = original(*a, **k)
            _capture(frame)
            return frame

        grab._paimiao_capture_hook = True      # type: ignore[attr-defined]
        camera.grab = grab
        camera._paimiao_capture_hook = True    # type: ignore[attr-defined]
        _CAMERAS.append(camera)
    except Exception:
        pass        # 包不上就退回到原来的行为
    return camera


def _capture(frame):
    """一张截图到手：先花一两毫秒看是不是准备界面，不像就立刻返回。

    这一路只是在 `detect()` 之外多问一遍 —— 「刚回到大厅」那行日志归 `detect()` 管，
    所以这里 force_log 永远是 False，日志不会写两遍。
    """
    engine = _ENGINE.get("engine")
    if engine is None or frame is None:
        return
    try:
        from cloud.lobby_mode import screen as lobby_screen      # noqa: PLC0415

        lobby_screen.handle(engine, frame)
    except Exception:
        pass        # 看错了也不影响记录，只是这一张不算大厅


def _engine_from_create_args():
    """拿到 collect() 建识别引擎时给它记下的 launcher。

    `code/live_collect.py:375` 这一行：
        engine = LazyEngine(SMALL_CROP_PARAMS, MODEL_IDLE_SECONDS, clock=..., log=log)
    LazyEngine 是「按需加载」的惰性包装，这里包住它的类本身（不碰实例），
    建出一个就把那台引擎记下来 —— 识别引擎是单线程共用的，截屏钩子直接用不会多占资源。
    """
    import live_collect      # noqa: PLC0415

    marker = getattr(live_collect, "LazyEngine", None)
    if marker is None or getattr(marker, "_paimiao_engine_hook", False):
        return _ENGINE.get

    class HookedEngine(marker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            _ENGINE["engine"] = self
            # 相机一般是先于引擎建好的（live_collect.py:373 在 :375 之前），
            # 那台相机在 _hook_camera 里已经包好了；这里只是把「反过来的顺序」也补上。
            for camera in list(_CAMERAS):
                _hook_camera(camera)
            _log("已接上截屏钩子：每一张截图都会看一眼是不是准备界面"
                 "（不像就只花一两毫秒）")

    HookedEngine._paimiao_engine_hook = True      # type: ignore[attr-defined]
    HookedEngine.__name__ = marker.__name__
    HookedEngine.__qualname__ = marker.__qualname__
    live_collect.LazyEngine = HookedEngine
    return _ENGINE.get

