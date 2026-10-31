"""设置页的那 1.4 秒：把上游 `panel_app.folder_usage()` 换成一次 scandir。

## 为什么

上游 `code/panel_app.py:1085` 的 `folder_usage()` 是这么写的：

    files = [p for p in path.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)

设置页（`panel_app.settings()`，上游 `code/panel_app.py:1105`）**每渲染一次就调它一次**，
扫的是 `captures/live`（截屏全留着，会一直长）。实测（2026-10-27 本机源码运行）：

    785 个文件 / 2.7 GB → folder_usage 1361 ms，整个设置页 1435 ms
    同一目录 /stats 39 ms、/pickrate 33 ms、/  69 ms —— 只有设置页慢

慢在两处：`rglob("*")` 走 pathlib 的递归拼接，785 个文件每个还被 `is_file()` 和
`stat()` 各碰一次（1618 次 `nt.stat`，共 1280 ms）。这不是注入造成的：cProfile 里
注入层渲染期全部开销（preprocess + 各 `app_context_processor` + 地图轮换）加起来 13 ms。

换成 `os.scandir` 递归 + 目录项自带的 `st_size`：**22 ms**，同一目录同一结果
（785 / 2874499037 字节，和原版逐字节相同），约 60 倍。

## 怎么算的（必须和上游语义一致）

设置页上显示的是「截屏：785 张 · 2.7 GB」，用户会拿它来决定清不清截图，
所以数字不能糊。原版的行为：

    - 跟符号链接（pathlib.rglob 默认 follow，is_file/stat 也跟随）
    - 数的是**普通文件**（目录除外）
    - `stat()` 失败在原来那句里是直接抛异常（rglob 遇到坏链接会跳过，stat 时已过时才会炸）

这里的做法是同样的规则：`e.is_dir(follow_symlinks=True)` 继续往下走，
`e.is_file(follow_symlinks=True)` 计入并取 `st_size`。
唯一放宽的一处：单个文件 stat 出错时**跳过它**而不是让整页 500
（上游那句是真会抛的；跳过一个读不到的文件，对"大概多大"没有影响）。

## 缓存

一个目录的 `st_size` 只在文件增删 / 截断时才变，而截屏目录只会新增。
所以按「目录」缓存结果，用目录自身的 `st_mtime_ns` 判断有没有变：
设置页连开两次、或者切走再回来，只有根目录被重新 scandir 一次（几百微秒），
26 个子目录一律命中缓存。**不设过期时间** —— 靠 mtime 失效比靠时间可靠，
不会出现"刚清过截图，数字还是旧的"。

线程安全：Flask 是多线程的，设置页可能被并发打开。缓存只在锁里读写，
扫描本身**不持锁**（扫描期间别的线程还能用旧结果，不会排队）。
锁是 `RLock`：`_scan` 自己会递归调用，单靠一把普通锁会把自己锁死。

## 幂等

`install()` 在 `cloud/bootstrap.py: attach()` 里跟着其它注入一起跑，用上游函数上的
标记位判重：包过一次就不再套第二层（重复执行不会出现「包装的包装」）。

## 降级

上游将来把这个函数改了名、或者把路由改成不调它，这里什么都不做（原样返回 False）。
任何异常都只写日志，设置页最多慢回 1.4 秒，**不会打不开**。
删掉整个 `paimiao_cloud/` 之后 Evo 照常跑，只是又变回 1.4 秒。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from cloud import state as cloud_state

# 上游函数上的标记位：包过了就不再包
_MARK = "_paimiao_folder_usage"

# 目录 → (目录 mtime_ns, 文件数, 总字节)。空 dict 表示还没扫过任何目录
_CACHE: dict = {}

# RLock 不是 Lock：_scan 递归时会重入
_LOCK = threading.RLock()

# 只在慢的时候说一句，别每开一次设置页写一行日志（cloud.log 已经很长了）
_SLOW_SECONDS = 0.25


def _log(message):
    try:
        cloud_state.log("[设置页] %s" % message)
    except Exception:
        pass


def _cacheable(path: str) -> bool:
    """这个目录能不能数：得能 stat 到它本身。

    目录不存在（或没权限）时返回 False，调用方照上游 `if path.is_dir()` 的分支
    直接给 `(0, 0)` —— 和上游逐字节一致。
    """
    try:
        os.stat(path)
    except OSError:
        return False
    return True


def _key(stat_result) -> tuple:
    """缓存键：只认目录内容变化的时间戳。

    用 `st_mtime_ns` 不用 `st_mtime`：NTFS 的时间戳精度是 100 ns，
    浮点的 `st_mtime` 只有微秒级且会丢精度，一秒内连删加改可能判成"没变"。
    """
    return (stat_result.st_mtime_ns, stat_result.st_ctime_ns)


def _scan(path: str) -> tuple[int, int]:
    """递归数文件和字节数（规则见模块开头）。读不了的目录跳过，不抛。"""
    try:
        with _LOCK:
            cached = _CACHE.get(path)
        st = os.stat(path)
        if cached is not None and cached[0] == _key(st):
            return cached[1], cached[2]
    except OSError:
        cached = None
        st = None

    count = 0
    size = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=True):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=True):
                            count += 1
                            size += entry.stat(follow_symlinks=True).st_size
                    except OSError:
                        # 刚被删掉 / 读不到：跳过这一个，别让设置页打开失败
                        continue
        except OSError:
            # 目录没了或没权限：这一层当空的
            continue

    if st is not None:
        with _LOCK:
            _CACHE[path] = (_key(st), count, size)
    return count, size


def install(module=None) -> bool:
    """把 `panel_app.folder_usage` 换掉。幂等，失败返回 False（上游照常）。"""
    if module is None:
        try:
            import panel_app as module
        except Exception:
            return False

    real = getattr(module, "folder_usage", None)
    if real is None or getattr(real, _MARK, False):
        return False

    def folder_usage(path):
        # 上游签名收的是 pathlib.Path（上游自己写的是 `path.rglob("*")`、`path.is_dir()`），
        # 降级回去时原样把它交给上游，别换类型
        started = time.perf_counter()
        try:
            # 目录不存在时上游返回 (0, 0)（`if path.is_dir()` 那个分支），别去扫
            if not _cacheable(os.fspath(path)):
                return 0, 0
            return _scan(os.fspath(path))
        except Exception as exc:  # noqa: BLE001 —— 设置页不能被这个拖挂
            _log("数文件夹出错，改用上游原版：%r" % exc)
            return real(Path(path))
        finally:
            spent = time.perf_counter() - started
            if spent >= _SLOW_SECONDS:
                _log("数文件夹用了 %.0f ms（%s）" % (spent * 1000, path))

    setattr(folder_usage, _MARK, True)
    setattr(folder_usage, "_paimiao_real", real)
    folder_usage.__doc__ = real.__doc__
    module.folder_usage = folder_usage
    return True
