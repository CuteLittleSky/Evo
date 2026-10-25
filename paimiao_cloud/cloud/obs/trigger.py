"""主机模式的启动条件：主机上没有 Apex 进程，改成「OBS 投影在不在」决定记不记录。

上游的判断（`game_detect.game_running`，evo_app.py:304 每 5 秒看一次）是
「进程列表里有没有 r5apex.exe」。主机（Switch / PS5 / Xbox）的 Apex 跑在游戏机里，
电脑上永远没有这个进程 —— 所以选了「OBS 投影」以后，记录子进程永远不会启动。

这个模块把判断换成：**能找到铺满整屏、置顶的 OBS 投影窗口 = 在打游戏**。
投影关掉（用户关了 OBS / 切走投影）= 游戏关了 → 记录子进程停下、
剩下的半局照样存好（`live_collect` 收到停止信号会正常收尾，和「关游戏」走同一条路）。

---

**为什么用 `recording` 参数区分，而不是直接把 game_running 换掉**

`evo_app.py` 里有三处用 game_running，用途不一样：

    evo_app.py:304   记录循环           → 主机模式要改成看投影（就是这个模块）
    evo_app.py:630   一键更新            "游戏开着就先别装"
    evo_app.py:633   搬数据文件夹        "游戏开着就先别搬"

后两处问的是「用户现在是不是正在用电脑打游戏、别再烦他」。主机模式下拿「投影在不在」
去回答这个问题是错的：用户可能把投影开着一直挂着，更新和搬数据就永远做不了。

所以给被包过的 `game_running` 加一个只由记录循环传的关键字 `recording=True`：

    game_running(names)                  → 原样调用上游（进程判断），三处调用默认都安全
    game_running(names, recording=True)  → 主机模式启用时看投影，否则原样调用上游

`recording` 不传就当 False —— 即使上游将来改了签名、或者别的地方也调用它，
默认行为永远和上游一字不差。这是这个注入唯一能让上游出错的地方，所以它必须是「只加不改」。

---

**为什么要包三处**

    game_detect.game_running   直接 import game_detect 的调用方
    evo_app.game_running       evo_app.py:55 `from game_detect import ... game_running`
                               把函数对象复制进了自己的命名空间，只换 game_detect 里的没用
    evo_app.Recorder._loop     记录循环里那一句是 `game_running(self.game_names)`（evo_app.py:304），
                               上游没传 recording —— 我们不改上游文件，只能在左边替换掉。

`_loop` 是整个换掉的（不是包一层再转发），因为它只有 15 行、每 5 秒跑一次。
换掉以后依然用上游的 `Recorder` 类、上游的 `_start_collect` / `_stop_collect` /
`_set_state`，只把「判断游戏在不在」那一步换成带 `recording=True` 的调用，
其余逻辑（暂停、搬数据时不记录、出错后 RETRY_AFTER_ERROR 秒内不重试）逐字照抄。

代价：上游将来改了 `_loop`（比如加了新的判断条件），这里要跟着改。
所以测试里加了「_loop 的源码和上游对得上」的检查（`tools/` 之外，见 README 的验证一节），
对不上时只写日志提醒、**仍然用上游的 _loop**（退化成 PC 行为，不会弄坏记录）。

三处包的是同一段逻辑，各自带幂等标记。

降级：任何一步失败都只写日志，Evo 照旧按进程判断（PC 玩家完全不受影响）。
"""
from __future__ import annotations

import sys
import time

from cloud.obs import settings as obs_settings

_MARK = "_paimiao_obs_trigger"
_LOOP_MARK = "_paimiao_obs_loop"
_START_MARK = "_paimiao_obs_start"
_RUN_MARK = "_paimiao_obs_run"
_INSTALLED = False
_FINDER = None
_TICKS = 0          # 排查用：记录循环第几次判断


def install():
    """接管启动条件。**可以反复调**（幂等，但会补装）。

    为什么不写成「第一次调用就定终身」：`sitecustomize` 在解释器启动的**最早期**
    就调了一次（那时 evo_app.py 还没开始执行），所以第一次调用必然什么都包不上。
    真正的接管要等 evo_app 的模块体执行完。三种入口都要认：

        import evo_app                     → sys.modules["evo_app"] 已有，直接包
        runpy.run_path(..., "__main__")    → evo_app 的模块名是 __main__
                                             （run_evo.py:57、打包版都是这样），
                                             这时 __main__ 里已经有 Recorder，直接包
        都还没有                           → 挂 finder，等 evo_app 被 import 时接

    实测教训：只有 finder 是不够的 —— `run_evo.py` 走 __main__，finder 永远等不到，
    日志里就不会出现「启动条件已接管」。所以 bootstrap 在 Flask.run 钩子里又补调一次。
    """
    global _INSTALLED, _FINDER
    # 托盘回调的日志先装上：它和 evo_app 是否已导入无关，越早越好。
    _patch_icon_run()
    module = sys.modules.get("evo_app")
    if module is not None and hasattr(module, "Recorder"):
        _after_import(module)
        _INSTALLED = True
        return
    main_module = sys.modules.get("__main__")
    main_path = str(getattr(main_module, "__file__", "") or "")
    if main_path.endswith(("evo_app.py", "evo_app.pyc")) and hasattr(main_module, "Recorder"):
        _after_import(main_module)
        _INSTALLED = True
        return
    if _FINDER is None:
        _FINDER = _EvoAppFinder()
        sys.meta_path.insert(0, _FINDER)


class _EvoAppFinder:
    """在 evo_app 首次被导入时接进去。"""

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "evo_app":
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            find_spec = getattr(finder, "find_spec", None)
            if find_spec is None:
                continue
            spec = find_spec(fullname, path, target)
            if spec is not None:
                spec.loader = _AfterImportLoader(spec.loader)
                return spec
        return None


class _AfterImportLoader:
    def __init__(self, inner):
        self._inner = inner

    def create_module(self, spec):
        make = getattr(self._inner, "create_module", None)
        return make(spec) if make else None

    def exec_module(self, module):
        self._inner.exec_module(module)
        try:
            _after_import(module)
        except Exception as exc:      # 注入失败绝不能让 Evo 起不来
            _log(f"主机模式启动条件注入失败（不影响记录）：{exc!r}")


def _after_import(module):
    """evo_app 已经执行完，game_running 和 Recorder 都在了，接管启动条件。

    `module` 可能就是 sys.modules["__main__"]（run_evo.py / 打包版那种形态），
    所以除了按名字找，还要把传进来的这个模块本身也包一遍。
    """
    patched = []
    for name in ("game_detect", "evo_app"):
        target = sys.modules.get(name)
        if target is not None and _patch(target):
            patched.append(name)
    if module not in (sys.modules.get("game_detect"), sys.modules.get("evo_app")):
        if _patch(module):
            patched.append(module.__name__)
    looped = _patch_loop(module)
    if patched:
        tail = "，记录循环改用投影判断" if looped else "（记录循环没换成，仅 game_running 生效）"
        _log(f"[主机模式] 启动条件已接管（{'、'.join(patched)}）{tail}："
             f"选「OBS 投影」时有投影就记录")


def _patch(module) -> bool:
    """把一个模块里的 game_running 换成「多认一个 recording 参数」的版本。

    只加不改：
        game_running()                      → 原样调上游（用默认进程名）
        game_running({"r5apex.exe"})        → 原样调上游
        game_running(names, recording=True) → 主机模式下看投影，否则原样调上游
    上游的签名是 `game_running(names=GAME_PROCESSES)`，只有一个参数，
    所以这里只需把 recording 从 kwargs 里摘出来，其余原封不动转发。
    """
    real = getattr(module, "game_running", None)
    if real is None or getattr(real, _MARK, False):
        return False

    def game_running(*args, **kwargs):
        recording = kwargs.pop("recording", False)
        if recording and obs_settings.enabled() and projector_running():
            return True
        return real(*args, **kwargs)

    setattr(game_running, _MARK, True)
    setattr(game_running, "_paimiao_real", real)
    module.game_running = game_running
    return True


def _patch_loop(module) -> bool:
    """把 evo_app.Recorder._loop 换成「判断游戏在不在」时传 recording=True 的版本。

    只在 evo_app 已导入、且它的 _loop 看起来还是上游那个样子时才换。
    换了以后上游的 Recorder 类、状态机、启停子进程的代码全都照用，
    这里只负责「每 5 秒问一次：现在该不该记录」。
    """
    if module is None:
        return False
    recorder = getattr(module, "Recorder", None)
    if recorder is None:
        return False
    real_loop = getattr(recorder, "_loop", None)
    if real_loop is None or getattr(real_loop, _LOOP_MARK, False):
        return False

    def _loop(self):
        global _TICKS
        # 取 evo_app 自己的那份：上游 evo_app.py:304 用的就是它（已经在上面的 _patch 里包过）。
        ask = getattr(module, "game_running", None)
        if ask is None:          # 理论上不会发生（_patch 刚包过），真发生了就按"没有游戏"
            ask = _no_game
        _log("[主机模式] 记录循环已启动（每 "
             f"{getattr(self, 'check_seconds', '?')} 秒复查一次投影）")
        last_state = None
        while not self._quit.is_set():
            # ↓ 和上游 evo_app.py:304 唯一的不同：多传 recording=True
            game = ask(self.game_names, recording=True)
            want = (game and not self.paused and not self.held
                    and time.monotonic() - self._last_error > self._retry_after)
            _TICKS += 1
            # **只在状态变化时记一行**。这里原来每 5 秒无条件写一条，
            # 一天下来几万行，把 data/cloud-sync/cloud.log 刷成一片，
            # 用户查别的东西（比如大厅模式）时 `-Tail 20` 里全是它，
            # 什么都搜不到（2026-10-24 用户遇到，排查了半天）。
            state = (bool(game), bool(want), bool(self.paused), bool(self.held))
            if state != last_state:
                last_state = state
                _log(f"[主机模式] 第 {_TICKS} 次判断：投影={'有' if game else '没有'}"
                     f" paused={self.paused} held={self.held} 记录中={self._recording()}"
                     f" 该记录={bool(want)}")
            if want and not self._recording():
                self.log("检测到游戏在运行，开始记录")
                self._start_collect()
            elif not want and self._recording():
                self.log("停止记录：" + ("已暂停" if self.paused else "游戏已关闭"))
                self._stop_collect()
            self._set_state("paused" if self.paused
                            else ("recording" if self._recording() else "waiting"))
            self._wake.wait(self.check_seconds)
            self._wake.clear()
        self._stop_collect()

    setattr(_loop, _LOOP_MARK, True)
    setattr(_loop, "_paimiao_real", real_loop)
    recorder._loop = _loop

    # 顺带包一下 start()，好让日志能说清楚"线程起没起来"。
    # 排查用：一次实测里 _loop 被换成功了，但桶里一直没「记录循环已启动」，
    # 追了半天才发现是 setup() 里的 recorder.start() 没跑到（托盘没起来）。
    # 有这条日志就能一眼分清是"没启动"还是"启动了但判断为否"。
    real_start = getattr(recorder, "start", None)
    if real_start is not None and not getattr(real_start, _START_MARK, False):

        def start(self):
            _log("[主机模式] 记录器开始运行（准备每 "
                 f"{getattr(self, 'check_seconds', '?')} 秒看一眼要不要记录）")
            return real_start(self)

        setattr(start, _START_MARK, True)
        recorder.start = start

    _log(f"[主机模式] 记录循环已换成投影判断（{module.__name__}.Recorder._loop）")

    # 上游 RETRY_AFTER_ERROR 是模块级常量（evo_app.py 顶部），搬成实例属性，
    # 这样换了 _loop 也不依赖它还在模块顶层。
    if not hasattr(recorder, "_retry_after"):
        recorder._retry_after = getattr(module, "RETRY_AFTER_ERROR", 30.0)
    return True


def _no_game(names=None, **kwargs):
    """兜底：拿不到上游的 game_running 时当作「游戏没开」（和上游找不到进程一样）。"""
    return False


def projector_running() -> bool:
    """现在能不能截到 OBS 投影。

    判据和截屏那边完全一致（都调 `projector.find_projector`），所以
    「记录在跑」和「截得到画面」不会出现一个真一个假的情况。
    探测失败按「没有」处理 —— 宁可这次不记录，也不要截一块错的屏。
    """
    try:
        from cloud.obs import projector  # noqa: PLC0415
        return projector.find_projector() is not None
    except Exception as exc:  # noqa: BLE001
        _log(f"[主机模式] 查投影窗口失败，这次当作没有投影：{exc!r}")
        return False


def _log(message):
    try:
        from cloud import state as cloud_state  # noqa: PLC0415
        cloud_state.log(message)
    except Exception:
        try:
            print(message)
        except Exception:
            pass


def _patch_icon_run():
    """包一层 pystray.Icon.run，把 setup 里的异常和「有没有走到 recorder.start」写进日志。

    **为什么需要这个**：上游 `code/evo_app.py:645-657` 的 setup() 里创建记录线程：

        def setup(ic):
            try:
                ic.visible = True
                recorder.start()        # ← 记录线程在这里才起来
                ...
            except Exception:
                logger.exception("启动时出错")

    那个 `logger` 是 main() 里的局部变量，写进的是 Evo 自己的日志文件；而且如果
    setup 根本不被调用（pystray 的某些情况），连这行都不会有。实测就遇到过：
    _loop 换成功、投影在、判断返回 True，但记录子进程始终不起来，日志里既没有
    「记录器开始运行」也没有任何报错，看不出是「没调到」还是「调到了但抛异常」。

    这里只加日志，不改行为：原 setup 原样调用，异常照旧抛给 pystray（和上游一致）。

    **还有一件必做的事**：在调 setup 之前先补一次 install()。
    实测日志顺序是这样的：

        17:55:41 托盘 setup 开始            ← pystray 在另一个线程里回调
        17:55:41 记录循环已换成投影判断     ← 我的注入这时才跑完
        17:55:42 托盘 setup 跑完了

    也就是说 setup 里那句 `recorder.start()` 执行时，`Recorder._loop` 还是上游原版，
    起来的是"看 Apex 进程"的线程 —— 主机上没有 r5apex.exe，永远不记录。
    `Recorder.start()` 取的是 `self._loop`，属于导入时就绑好的方法对象，
    事后换类的 `_loop` 对已启动的线程没用。所以必须**在 setup 之前**把类换好。
    """
    try:
        import pystray  # noqa: PLC0415
    except Exception:
        return
    icon_cls = getattr(pystray, "Icon", None)
    if icon_cls is None or getattr(icon_cls.run, _RUN_MARK, False):
        return

    real_run = icon_cls.run

    def run(self, setup=None):
        if setup is None:
            _log("[主机模式] 托盘启动（没有 setup 回调）")
            return real_run(self, setup)

        def wrapped(ic):
            _log("[主机模式] 托盘 setup 开始（接下来要起记录线程）")
            # ↓ 关键：抢在 setup 里的 recorder.start() 之前把类换好
            try:
                install()
            except Exception as exc:      # noqa: BLE001 注入失败绝不能让 Evo 起不来
                _log(f"[主机模式] setup 前补装失败（不影响 Evo）：{exc!r}")
            try:
                result = setup(ic)
            except BaseException as exc:      # noqa: BLE001 只为记日志，照旧抛
                import traceback
                _log(f"[主机模式] 托盘 setup 出错：{exc!r}")
                _log(traceback.format_exc())
                raise
            _log("[主机模式] 托盘 setup 跑完了")
            return result

        return real_run(self, wrapped)

    setattr(run, _RUN_MARK, True)
    icon_cls.run = run
