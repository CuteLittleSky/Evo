"""Evo 自己占多少 CPU、内存：总览页的「Evo 占用」显示现在的和最近 10 局对局中的平均，匿名统计带上每局的（2026-11-15 用户要求）。

- 算法和任务管理器一样（用户决定）：CPU 按整台电脑算 = 用过的 CPU 时间 ÷（经过的时间 × 逻辑处理器个数），
  16 线程的电脑上日志里的"15% 个核"在这里是约 1%；内存是"专用工作集"（任务管理器默认显示的那个，见 process_info.py）
- Evo 的全部进程都算：托盘程序（页面也在它里面）、记录子进程、Evo 窗口和它下面的 WebView2。托盘程序开子进程时用 mark
  记下哪个是记录、哪个是窗口，它们下面的进程跟着算
- 现在的（LiveUsage）：总览页开着时每隔几秒问一次 /usage，和上一次比
- 对局中的（MatchMeter）：记录子进程从认出自己的界面到这一局结束（in_match.active）量一段：CPU 用开始和结束时两份进程
  列表相减；内存每 SAMPLE_SECONDS 秒看一次，取平均和最高；同时记下整台电脑的 CPU。这一段之后真的记下了一局
  （总结页存下了、或者交给 PostMatch 存下了）才算一局，在靶场里的不算。只留最近 KEEP 局，存在数据库旁边的
  usage.json（不进数据库，不改表结构；2026-11-15 用户决定直接在 main 上做）

用法：python code/evo_usage.py   → 打印现在 Evo 的占用（量 1 秒）和最近几局的记录
"""

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import process_info
import storage

KEEP = 10  # 最近几局（用户定的）
SAMPLE_SECONDS = 10  # 对局中每隔几秒看一次内存（看一次约 5 毫秒）
MIN_SECONDS = 60  # 不到 1 分钟的一段不算一局（认错了、刚进去就退出）
FILE_NAME = "usage.json"

ROLES = ("tray", "window", "record")  # 显示的顺序
ROLE_LABELS = {"tray": "托盘", "window": "窗口", "record": "记录"}
_marks = {}  # 进程号 → 角色：托盘程序开记录子进程、窗口子进程时记下（mark）


def mark(pid, role):
    """托盘程序新开了一个子进程：记下它是记录（record）还是窗口（window）。"""
    _marks[pid] = role


def roles_of(procs, pids, root):
    """pids 里每个进程算哪一部分：往上找到 mark 过的进程就是它的角色，一直找到 root 都没有就算托盘。"""
    out = {root: "tray"}
    for pid in pids:
        chain, cur = [], pid
        while cur not in out:
            if _marks.get(cur) in ROLES:
                out[cur] = _marks[cur]
            elif cur not in pids:  # 不会走到这里（pids 往上总能走到 root），以防万一
                out[cur] = "tray"
            else:
                chain.append(cur)
                cur = procs[cur].parent
        for p in chain:
            out[p] = out[cur]
    return {p: out[p] for p in pids}


def _cpu_used(before, procs, pids):
    """这段时间 pids 一共用了多少 CPU 时间（秒）。before：{进程号: (启动时刻, CPU 时间)}；中途新开的进程从 0 算起。"""
    used = 0.0
    for pid in pids:
        p = procs[pid]
        created, cpu = before.get(pid, (None, 0.0))
        used += p.cpu - cpu if created == p.created else p.cpu
    return max(used, 0.0)


def _base(procs, pids):
    return {pid: (procs[pid].created, procs[pid].cpu) for pid in pids}


# ---------- 现在（总览页）----------

class LiveUsage:
    """总览页上"现在"那一格：每次 read() 和上一次比，算出这几秒的 CPU；内存是这一刻的。"""

    MIN_GAP, MAX_GAP = 0.5, 10  # 和上一次隔得太近：沿用上一次算出的 CPU；隔得太久（上次打开页面是很久以前）：不算平均，重新开始

    def __init__(self, root=None, clock=time.monotonic, snap=process_info.snapshot, cpus=None):
        self.root = os.getpid() if root is None else root
        self.clock, self.snap = clock, snap
        self.cpus = cpus or process_info.logical_cpus()
        self._last = None  # (时刻, {进程号: (启动时刻, CPU 时间)})
        self._cpu = None  # 上一次算出的 CPU
        self._lock = threading.Lock()  # 页面是多线程的

    def read(self):
        """{"memory": 合计 MB, "cpu": 整台电脑的 %（第一次、隔了很久时是 None）, "parts": {角色: MB（没开着是 None）}}。
        读不到进程列表时返回 None。"""
        with self._lock:
            now, procs = self.clock(), self.snap()
            pids = process_info.descendants(procs, self.root)
            if not pids:
                return None
            roles = roles_of(procs, pids, self.root)
            parts = {role: None for role in ROLES}
            for pid in pids:
                parts[roles[pid]] = (parts[roles[pid]] or 0.0) + procs[pid].memory
            gap = None if self._last is None else now - self._last[0]
            if gap is None or gap > self.MAX_GAP:
                self._cpu = None
            elif gap >= self.MIN_GAP:
                self._cpu = _cpu_used(self._last[1], procs, pids) / gap / self.cpus * 100
            if gap is None or gap >= self.MIN_GAP:
                self._last = (now, _base(procs, pids))
            return {"memory": sum(v for v in parts.values() if v), "cpu": self._cpu, "parts": parts}


_live = None


def live():
    """页面用的那一个 LiveUsage（量的是页面所在的进程，也就是托盘程序，和它下面的子进程）。"""
    global _live
    if _live is None:
        _live = LiveUsage()
    return _live


# ---------- 对局中（记录子进程）----------

def usage_path(db_path=storage.DB_PATH):
    return Path(db_path).parent / FILE_NAME


def recent(path=None):
    """最近几局对局中的占用（从早到晚），见 MatchMeter._record。文件不在、坏了返回 []。"""
    try:
        data = json.loads(Path(path or usage_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("matches") if isinstance(data, dict) else None
    keys = ("at", "minutes", "cpu", "cpu_core", "memory", "memory_peak")
    return [r for r in rows if isinstance(r, dict) and all(k in r for k in keys)][-KEEP:] if isinstance(rows, list) else []


def _save(path, rows):
    """先写到临时文件再换过去：页面正好在读时不会读到写了一半的文件。换不过去（别的进程正开着）就重试几次。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"matches": rows[-KEEP:]}, ensure_ascii=False, indent=1), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1)


def average(rows):
    """最近几局的平均（每局一样重）：{"n", "cpu", "memory", "memory_peak"（这几局里最高的）, "system_cpu"}；没有局时 None。"""
    if not rows:
        return None
    systems = [r["system_cpu"] for r in rows if isinstance(r.get("system_cpu"), (int, float))]
    return {"n": len(rows), "cpu": sum(r["cpu"] for r in rows) / len(rows),
            "memory": sum(r["memory"] for r in rows) / len(rows),
            "memory_peak": max(r["memory_peak"] for r in rows),
            "system_cpu": sum(systems) / len(systems) if systems else None}


class MatchMeter:
    """记录子进程里量"对局中 Evo 占多少"：每截一张调用一次 tick，停止记录时调用 finish。见本文件开头。"""

    def __init__(self, root=None, log=None, path=None, clock=time.monotonic, snap=process_info.snapshot,
                 sys_times=process_info.system_times, now=datetime.now, cpus=None):
        self.root = os.getpid() if root is None else root  # 托盘程序（记录子进程的父进程）；单独运行时是自己
        self.log = log or (lambda text: None)
        self.path = Path(path or usage_path())
        self.clock, self.snap, self.sys_times, self.now = clock, snap, sys_times, now
        self.cpus = cpus or process_info.logical_cpus()
        self.cur = None  # 正在量的这一段
        self.pending = None  # 量完了、还没确认记下了一局的那一段：{"saved": 开始时的局数, "record": …}
        self.broken = False  # 量的时候出过错：这次记录不再量（量占用出错不能影响记录）

    def tick(self, active, saved):
        """active：现在是不是对局中（in_match.active）；saved：这次记录到现在一共记下了几局。"""
        self._safely(self._tick, active, saved)

    def finish(self, saved):
        """停止记录时（没存完的局都存下以后）：正在量的这一段到此为止，记下了局就算上。"""
        self._safely(self._finish, saved)

    def _safely(self, step, *args):
        if self.broken:
            return
        try:
            step(*args)
        except Exception as e:  # noqa: BLE001
            self.broken, self.cur, self.pending = True, None, None
            self.log(f"           量对局中的占用出错，这次记录不再量：{e!r}")

    def _tick(self, active, saved):
        self._commit_if(saved)
        if active and self.cur is None:
            self.pending = None  # 上一段之后一直没记下一局（靶场里、没认出结算）：不算
            self._start(saved)
        elif active:
            if self.clock() >= self.cur["next"]:
                self._sample()
        elif self.cur is not None:
            self._end()
            self._commit_if(saved)

    def _finish(self, saved):
        if self.cur is not None:
            self._end()
        self._commit_if(saved)
        self.pending = None

    def _start(self, saved):
        procs = self.snap()
        pids = process_info.descendants(procs, self.root)
        if not pids:
            return
        start = self.clock()
        self.cur = {"saved": saved, "start": start, "next": start + SAMPLE_SECONDS, "base": _base(procs, pids),
                    "memory": [sum(procs[p].memory for p in pids)], "system": self.sys_times()}

    def _sample(self):
        procs = self.snap()
        pids = process_info.descendants(procs, self.root)
        if pids:
            self.cur["memory"].append(sum(procs[p].memory for p in pids))
        self.cur["next"] += SAMPLE_SECONDS

    def _end(self):
        cur, self.cur = self.cur, None
        seconds = self.clock() - cur["start"]
        if seconds < MIN_SECONDS:
            return
        procs = self.snap()
        pids = process_info.descendants(procs, self.root)
        if not pids:
            return
        memory = cur["memory"] + [sum(procs[p].memory for p in pids)]
        used = _cpu_used(cur["base"], procs, pids)
        system, before = self.sys_times(), cur["system"]
        system_cpu = None
        if system and before and system[1] > before[1]:
            system_cpu = round((system[0] - before[0]) / (system[1] - before[1]) * 100)
        record = {"at": f"{self.now():%Y-%m-%d %H:%M:%S}", "version": storage.APP_VERSION,
                  "minutes": round(seconds / 60, 1),
                  "cpu": round(used / seconds / self.cpus * 100, 2),  # 整台电脑的 %，和任务管理器一样
                  "cpu_core": round(used / seconds * 100, 1),  # 占一个核的 %（和日志里的"占用"一样）
                  "memory": round(sum(memory) / len(memory)), "memory_peak": round(max(memory)),
                  "system_cpu": system_cpu}
        self.pending = {"saved": cur["saved"], "record": record}

    def _commit_if(self, saved):
        if self.pending is None or saved <= self.pending["saved"]:
            return
        record, self.pending = self.pending["record"], None
        try:
            _save(self.path, recent(self.path) + [record])
        except OSError as e:
            self.log(f"           对局中的占用没存上：{e}")
            return
        system = f"；同时整台电脑 CPU {record['system_cpu']}%" if record["system_cpu"] is not None else ""
        self.log(f"           这局对局中 Evo 占用：CPU 平均 {record['cpu']:.1f}%（整台电脑，约 {record['cpu_core']:.0f}% 个核），"
                 f"内存平均 {record['memory']} MB、最高 {record['memory_peak']} MB{system}（{record['minutes']:g} 分钟）")


# ---------- 总览页显示 ----------

def cpu_text(value):
    """和任务管理器一样一位小数，如 "0.9%"；不到 0.05% 写 "0%"；没有写 "—"。"""
    if value is None:
        return "—"
    return "0%" if value < 0.05 else f"{value:.1f}%"


def mb_text(value):
    return "—" if value is None else f"{value:.0f} MB"


def now_view(reading):
    """"现在"那一格的文字（/usage 也回这个）：{"cpu", "memory", "parts": "托盘 37 MB · 窗口 115 MB · 没在记录"}。"""
    if reading is None:
        return {"cpu": "—", "memory": "—", "parts": "—"}
    parts = reading["parts"]
    shown = [f"{ROLE_LABELS[r]} {mb_text(parts[r])}" for r in ROLES if parts[r] is not None]
    if parts["record"] is None:
        shown.append("没在记录")
    return {"cpu": cpu_text(reading["cpu"]), "memory": mb_text(reading["memory"]), "parts": " · ".join(shown)}


def card(db_path=storage.DB_PATH, meter=None):
    """总览页「Evo 占用」卡片要的东西：现在的（第一次打开时 CPU 是 "—"，页面一秒后再问）和最近几局的平均（没有局时 None）。"""
    avg = average(recent(usage_path(db_path)))
    return {"now": now_view((meter or live()).read()), "recent": None if avg is None else {
        "n": avg["n"], "cpu": cpu_text(avg["cpu"]), "memory": mb_text(avg["memory"]),
        "peak": mb_text(avg["memory_peak"]),
        "system": None if avg["system_cpu"] is None else f"{avg['system_cpu']:.0f}%"}}


if __name__ == "__main__":
    meter = LiveUsage()
    meter.read()
    time.sleep(1)
    now_usage = meter.read()
    print(f"现在（只有这个进程）：内存 {now_usage['memory']:.0f} MB，CPU {now_usage['cpu']:.1f}%")
    for row in recent():
        print(row)
