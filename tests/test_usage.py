"""Evo 占用（evo_usage.py、process_info.py 的 snapshot、右上角的「Evo 占用」，2026-11-15 用户要求）。不用截图，不开浏览器。

- 读所有进程（不打开进程）：找得到自己，父进程、内存、CPU 时间对得上；子进程、孙进程都算，父进程号被别的进程用过的不算
- 算法和任务管理器一样：CPU = 用过的 CPU 时间 ÷（经过的时间 × 逻辑处理器个数）；内存是专用工作集
- 现在的（LiveUsage）：第一次没有 CPU，之后和上一次比；隔得太近沿用上一次、隔得太久重新开始；
  托盘程序记下的记录、窗口子进程（和它们下面的进程）分开算；真的开一个子进程，看得到它的内存和 CPU
- 对局中的（MatchMeter）：一段之后记下了一局才算（靶场里的不算），不到 1 分钟不算；下一局开局时才存下上一局也算上一局；
  中途打开的窗口从 0 算；停止记录时补上；只留最近 10 局；写日志；量的时候出错不影响记录（不抛出去，这次不再量）
- 右上角（2026-11-15 先做成总览页的卡片，当天用户要求改放到顶部那一行、原来派圈助手按钮的位置）：每一页都有，
  按钮上是现在的 CPU、内存；点了展开两格（现在、最近 N 局对局中），没有局时写"打完一局以后显示"；
  /usage 回现在的数字，脚本每 2 秒问一次；没有 aria-live；总览页不再有那张卡片

运行：python tests/test_usage.py
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import evo_usage
import panel_app
import process_info
import storage
from process_info import Proc

GB = 2**30


def snapshot_checks():
    out = {}
    procs = process_info.snapshot()
    me = procs.get(os.getpid())
    out["读所有进程：找得到自己，父进程对得上"] = me is not None and me.parent == os.getppid()
    out["名字是小写的 python.exe"] = me is not None and me.name == "python.exe"
    out["内存是专用工作集（比工作集小，几 MB 到几百 MB）"] = me is not None and 1 < me.memory < min(
        2000, process_info.memory_mb()[0] + 1)
    start = me.cpu
    end_at = time.perf_counter() + 0.3
    while time.perf_counter() < end_at:  # 忙 0.3 秒
        pass
    used = process_info.snapshot()[os.getpid()].cpu - start
    out["CPU 时间：忙了 0.3 秒，多了约 0.3 秒"] = 0.2 < used < 0.6
    busy, total = process_info.system_times()
    out["整台电脑的 CPU 时间：忙的比合计少"] = 0 < busy < total
    out["逻辑处理器个数"] = process_info.logical_cpus() == os.cpu_count()

    fake = {1: Proc(1, 0, "tray", 100, 0, 30), 2: Proc(2, 1, "record", 200, 0, 200),
            3: Proc(3, 2, "child", 300, 0, 5), 4: Proc(4, 1, "old", 50, 0, 9),  # 4：父进程号是别人用剩的
            5: Proc(5, 9, "other", 400, 0, 1)}
    out["子进程、孙进程都算，比父进程启动得早的不算，别的不算"] = process_info.descendants(fake, 1) == {1, 2, 3}
    out["根进程不在了：空的"] = process_info.descendants(fake, 7) == set()
    return out


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def fake_procs(tray_cpu, extra=None):
    procs = {10: Proc(10, 1, "evo.exe", 100, tray_cpu, 40.0)}
    for pid, (parent, cpu, memory) in (extra or {}).items():
        procs[pid] = Proc(pid, parent, "x.exe", 100 + pid, cpu, memory)
    return procs


def live_checks():
    out = {}
    clock, state = Clock(), {"procs": fake_procs(5.0)}
    live = evo_usage.LiveUsage(root=10, clock=clock, snap=lambda: state["procs"], cpus=16)
    first = live.read()
    out["现在：第一次没有 CPU（要和上一次比），内存有"] = first["cpu"] is None and first["memory"] == 40
    clock.now += 2
    state["procs"] = fake_procs(6.6)
    second = live.read()
    out["2 秒后：用了 1.6 秒 CPU、16 个逻辑处理器 → 5%（和任务管理器一样按整台电脑算）"] = abs(second["cpu"] - 5) < 1e-9
    clock.now += 0.2
    state["procs"] = fake_procs(9.0)
    out["隔得太近（0.2 秒）：沿用上一次的"] = live.read()["cpu"] == second["cpu"]
    clock.now += 60
    out["隔了很久（上次打开页面是很久以前）：不算平均，重新开始"] = live.read()["cpu"] is None

    evo_usage.mark(20, "record")
    evo_usage.mark(30, "window")
    clock.now += 2
    state["procs"] = fake_procs(9.0, {20: (10, 0.0, 200.0), 21: (20, 0.0, 10.0), 30: (10, 0.0, 30.0),
                                      31: (30, 0.0, 90.0), 40: (10, 0.0, 2.0), 50: (99, 0.0, 500.0)})
    parts = live.read()["parts"]
    out["分开算：记录（含它下面的）、窗口（含 WebView2）、托盘（含没记下的子进程），别的进程不算"] = (
        parts == {"tray": 42.0, "window": 120.0, "record": 210.0})
    view = evo_usage.now_view(live.read())
    out["文字：合计、分开写"] = view["memory"] == "372 MB" and view["parts"] == "托盘 42 MB · 窗口 120 MB · 记录 210 MB"
    out["没在记录、窗口没开（用浏览器打开的面板）"] = evo_usage.now_view(
        {"memory": 38.4, "cpu": 0.02, "parts": {"tray": 38.4, "window": None, "record": None}}) == {
        "cpu": "0%", "memory": "38 MB", "parts": "托盘 38 MB · 没在记录"}
    out["CPU 一位小数，读不到写 —"] = (evo_usage.cpu_text(0.94) == "0.9%" and evo_usage.cpu_text(None) == "—"
                                and evo_usage.now_view(None)["cpu"] == "—")
    evo_usage._marks.clear()

    # 真的开一个子进程：忙 1 秒、占几十 MB
    code = "import time\nb = bytearray(60 * 2**20)\nt = time.perf_counter() + 1.2\nwhile time.perf_counter() < t: pass\ntime.sleep(5)"
    child = subprocess.Popen([sys.executable, "-c", code])
    try:
        evo_usage.mark(child.pid, "record")
        real = evo_usage.LiveUsage()
        real.read()
        time.sleep(1.5)
        got = real.read()
        record_mb = got["parts"]["record"] or 0
        cpus = process_info.logical_cpus()
        out[f"真的子进程（标成记录）：内存约 60 MB 以上（{record_mb:.0f} MB），CPU 约一个核（{got['cpu'] * cpus:.0f}% 个核）"] = (
            record_mb > 55 and 40 < got["cpu"] * cpus < 250)
    finally:
        child.kill()
        child.wait()
        evo_usage._marks.clear()
    return out


class MeterRig:
    """假的进程列表、整台电脑的 CPU 时间、时钟，喂给 MatchMeter。"""

    def __init__(self, path):
        self.clock, self.tray_cpu, self.extra, self.system = Clock(), 0.0, {}, (0.0, 0.0)
        self.logs = []
        self.meter = evo_usage.MatchMeter(root=10, log=self.logs.append, path=path, clock=self.clock, snap=self.snap,
                                          sys_times=lambda: self.system, now=lambda: datetime(2026, 11, 15, 21, 0),
                                          cpus=16)

    def snap(self):
        return fake_procs(self.tray_cpu, self.extra)

    def play(self, seconds, saved, cpu_per_second=0.16, memory=None, system_busy=0.5):
        """对局中 seconds 秒：每秒调一次 tick，Evo 每秒用 cpu_per_second 秒 CPU（记录子进程）。"""
        for _ in range(int(seconds)):
            self.meter.tick(True, saved)
            self.clock.now += 1
            parent, cpu, mem = self.extra.get(20, (10, 0.0, 200.0))
            self.extra[20] = (parent, cpu + cpu_per_second, memory if memory is not None else mem)
            self.system = (self.system[0] + 16 * system_busy, self.system[1] + 16)


def meter_checks(folder):
    out = {}
    path = folder / "usage.json"
    rig = MeterRig(path)
    rig.extra = {20: (10, 0.0, 200.0)}
    rig.play(300, saved=0, memory=200.0)
    rig.play(300, saved=0, memory=240.0)
    rig.meter.tick(False, 0)  # 淘汰画面：这一段结束
    out["一段结束、还没记下一局：先不存"] = not path.exists()
    rig.meter.tick(False, 1)  # 总结页存下了
    rows = evo_usage.recent(path)
    row = rows[0] if rows else {}
    out["记下了一局：存进 usage.json"] = len(rows) == 1
    out["CPU：每秒 0.16 秒 CPU、16 个逻辑处理器 → 1%（整台电脑），16% 个核"] = (
        abs(row.get("cpu", 0) - 1.0) < 0.01 and abs(row.get("cpu_core", 0) - 16) < 0.1)
    out["内存：每 10 秒看一次，平均在 200 和 280 之间、最高 280（托盘 40 + 记录 240）"] = (
        240 < row.get("memory", 0) < 280 and row.get("memory_peak") == 280)
    out["同时整台电脑的 CPU：50%；10 分钟"] = row.get("system_cpu") == 50 and row.get("minutes") == 10
    out["记下版本号和结束的时刻"] = row.get("version") == storage.APP_VERSION and row.get("at") == "2026-11-15 21:00:00"
    out["日志写一行"] = any("这局对局中 Evo 占用：CPU 平均 1.0%" in line for line in rig.logs)

    rig.play(400, saved=1)  # 靶场：之后没记下一局
    rig.meter.tick(False, 1)
    rig.play(200, saved=1)  # 下一段开始：靶场那段丢掉
    rig.meter.tick(False, 1)
    rig.clock.now += 30
    rig.meter.tick(True, 2)  # 下一局开局时才把上一局交给 PostMatch 存下：算上一局的
    out["靶场里的那段（之后没记下一局）不算；下一局开局时才存下的上一局算上"] = len(evo_usage.recent(path)) == 2
    rig.meter.tick(False, 2)
    rig.play(30, saved=2)  # 不到 1 分钟
    rig.meter.tick(False, 3)
    out["不到 1 分钟的一段不算"] = len(evo_usage.recent(path)) == 2

    rig.extra = {20: (10, 0.0, 200.0)}
    rig.play(120, saved=3, cpu_per_second=0.0)
    rig.extra[30] = (10, 3.2, 100.0)  # 中途打开了窗口，已经用了 3.2 秒 CPU
    rig.play(80, saved=3, cpu_per_second=0.0)
    rig.meter.finish(4)  # 停止记录：没存完的局存下了
    last = evo_usage.recent(path)[-1]
    out["中途打开的窗口：从 0 算起；停止记录时补上这一局"] = (
        len(evo_usage.recent(path)) == 3 and abs(last["cpu_core"] - 3.2 / 200 * 100) < 0.1)
    for i in range(12):
        rig.play(70, saved=5 + i)
        rig.meter.tick(False, 6 + i)
    rows = evo_usage.recent(path)
    out["只留最近 10 局"] = len(rows) == 10 and json.loads(path.read_text(encoding="utf-8"))["matches"] == rows
    avg = evo_usage.average([{"cpu": 1.0, "memory": 200, "memory_peak": 230, "system_cpu": 60},
                             {"cpu": 2.0, "memory": 220, "memory_peak": 250, "system_cpu": None}])
    out["平均：每局一样重，内存最高取最高的，整台电脑的只算有的"] = avg == {
        "n": 2, "cpu": 1.5, "memory": 210, "memory_peak": 250, "system_cpu": 60}
    logs = []

    def boom():
        raise OSError("读不到进程列表")

    broken = evo_usage.MatchMeter(root=10, log=logs.append, path=folder / "broken.json", snap=boom)
    broken.tick(True, 0)
    broken.tick(True, 0)
    broken.finish(1)
    out["量的时候出错：不抛出去，日志写一次，这次记录不再量"] = (
        broken.broken and len(logs) == 1 and "不再量" in logs[0] and not (folder / "broken.json").exists())
    path.write_text("坏的", encoding="utf-8")
    out["文件坏了：当作没有"] = evo_usage.recent(path) == [] and evo_usage.average([]) is None
    return out


def panel_checks(folder):
    out = {}
    db = folder / "evo.db"
    storage.connect(db).close()
    panel_app.app.config["DB"] = db
    client = panel_app.app.test_client()

    def top(url):
        """这一页顶部那一行里的「Evo 占用」（按钮和展开的那一块）。"""
        page = client.get(url).get_data(as_text=True)
        header = page.split("</header>")[0]
        return header[header.find('id="usage-top"'):] if 'id="usage-top"' in header else "", page

    box, html = top("/")
    out["右上角有「Evo 占用」按钮：现在的 CPU、内存"] = (
        'id="usage-open"' in box and "Evo 占用" in box and 'data-usage="cpu"' in box and 'data-usage="memory"' in box)
    out["按钮在导航后面、演示数据和主题按钮前面（原来派圈助手的位置）"] = (
        html.find('class="nav"') < html.find('id="usage-top"') < html.find('id="theme-toggle"'))
    out["展开的那一块：平时收着，按钮写着控制它、没展开"] = (
        'aria-expanded="false"' in box and 'aria-controls="usage-panel"' in box and 'id="usage-panel"' in box
        and re.search(r'id="usage-panel"[^>]*hidden', box) is not None)
    out["总览页不再有那张卡片"] = 'id="usage-card"' not in html
    out["还没有局：写'打完一局以后显示'"] = "打完一局以后显示" in box and "最近 10 局对局中（平均）" in box
    rows = [{"at": f"2026-11-1{i} 20:00:00", "version": "0.5.8", "minutes": 15, "cpu": 0.8 + i * 0.1, "cpu_core": 13,
             "memory": 200 + i * 10, "memory_peak": 230 + i, "system_cpu": 50 + i} for i in range(3)]
    (folder / "usage.json").write_text(json.dumps({"matches": rows}), encoding="utf-8")
    conn = storage.connect(db)
    storage.insert_match(conn, {"ended_at": "2026-11-15 20:00:00", "mode": "ranked", "placement": 3})
    conn.close()
    box, html = top("/")
    out["有局：最近 3 局对局中的平均 CPU、内存，最高、同时整台电脑"] = (
        "最近 3 局对局中（平均）" in box and "0.9%" in box and "210 MB" in box and "最高 232 MB" in box
        and "同时整台电脑 CPU 51%" in box)
    out["现在那一格：页面所在的进程（托盘）有内存"] = "托盘" in box and "没在记录" in box
    others = [top(url)[0] for url in ("/matches", "/stats", "/seasons", "/settings")]
    out["每一页都有（战绩、数据、赛季、设置）"] = all('id="usage-open"' in b and "最近 3 局对局中" in b for b in others)
    out["数字每 2 秒变：不用 aria-live"] = "aria-live" not in box
    first = client.get("/usage").get_json()
    time.sleep(0.6)
    now = client.get("/usage").get_json()
    out["/usage：回现在的 CPU、内存、分开的文字"] = (
        set(now) == {"cpu", "memory", "parts"} and now["memory"].endswith(" MB") and now["cpu"].endswith("%")
        and first["parts"].startswith("托盘"))
    out["脚本每 2 秒问一次 /usage，窗口最小化时不问；点了展开、点外面和 Esc 收起"] = (
        "setInterval(update, 2000)" in html and 'document.visibilityState !== "visible"' in html
        and "setOpen(panel.hidden)" in html and '"Escape"' in html and "box.contains(e.target)" in html)
    return out


def main():
    print("Evo 占用（读进程、现在的、对局中最近 10 局、右上角）")
    folder = storage.DB_PATH.parent / "usage-test"
    folder.mkdir(parents=True, exist_ok=True)
    checks = {**snapshot_checks(), **live_checks(), **meter_checks(folder), **panel_checks(folder)}
    panel_app.app.config["DB"] = storage.DB_PATH
    return report(checks)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
