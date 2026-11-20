"""录像接进记录程序（live_collect）：看设置开没开，一局开始开录，记事件，一局结束在后台拼成 MP4（见 docs/录像.md）。

记录程序每截一张调用一次 update(画面, in_match, 时刻)，停止记录时调用 close()。
设置（settings.json）的 recording_enabled 默认关：只在可能是新的一局开始时看一眼，关着时什么都不做，记录程序和以前一样。
- 开始：看到开局画面，或第一次看到自己的界面（靶场没有开局画面，也录）
- 事件（.json 的 events，时刻是 MP4 里的位置）：倒地 knocked、被扶起 revived、成盒 dead、被复活 respawned、冠军 champion
  看的是 InMatchWatcher 记下的状态（in_match.py）；击倒对手 knock 看画面中间那一行的"击倒"（hud.knock，只比外观，
  录像开着时才看）：rearm_seconds 没看到才算下一次，还在显示时左右挪了 moved 以上（换了名字）也算
- 右上角每变一次记一个时刻（.json 的 hud_changes）：以后从录像里找击杀用（击杀数只会变多，按这些时刻二分查找）
- 停：这一局结束（结算画面、回到大厅、停止记录）以后再录 tail_seconds 秒，拼 MP4 在后台线程里做，不耽误读结算画面
- 设置里录不了（找不到 ffmpeg、没有硬件编码器、抓不到画面）：日志里说一次，每局开始时再试
"""

import threading
import time
from pathlib import Path

import app_settings
import game_detect
import recorder
from in_match import END_SCREENS, STATE_ALIVE, STATE_DEAD, STATE_KNOCKED
from paths import DATA_DIR

SETTING = "recording_enabled"
DEFAULT_LIMIT_GB = 30
POLL_SECONDS = 1.0  # 多久看一次 ffmpeg 还在不在录
BANNER = object()  # 在开局画面试过开录、还不知道是哪一局（见 _new_match）
TRANSITIONS = {  # (之前的状态, 现在的状态) → 事件
    (STATE_ALIVE, STATE_KNOCKED): "knocked",
    (STATE_KNOCKED, STATE_ALIVE): "revived",
    (STATE_ALIVE, STATE_DEAD): "dead",
    (STATE_KNOCKED, STATE_DEAD): "dead",
    (STATE_DEAD, STATE_ALIVE): "respawned",
    (STATE_DEAD, STATE_KNOCKED): "respawned",
}


def options_from(settings):
    """设置 → recorder.Options（没设过的用默认）。"""
    return recorder.Options(source=settings.get("recording_source", "window"),
                            size=settings.get("recording_size", "native"),
                            fps=int(settings.get("recording_fps", 60)),
                            codec=settings.get("recording_codec", "h264"),
                            quality=settings.get("recording_quality", "high"),
                            audio=bool(settings.get("recording_audio", True)))


def apex_pid():
    for pid, name in game_detect.processes():
        if name in game_detect.GAME_PROCESSES:
            return pid
    return None


class MatchRecorder:
    """settings()：读设置（字典）；factory：建 recorder.Recording（测试换成假的）；clock：time.monotonic（测试换）。"""

    def __init__(self, log, settings=app_settings.load, data_dir=DATA_DIR, factory=recorder.Recording,
                 clock=time.monotonic, find_pid=apex_pid):
        self.log, self.settings, self.data_dir = log, settings, Path(data_dir)
        self.factory, self.clock, self.find_pid = factory, clock, find_pid
        self.rec = None
        self.rules = None
        self.covered = None  # 已经录过（或正在录）的那一局：InMatchWatcher.started，或开局画面的 "start"
        self.state = None
        self.top = None  # 右上角最近一块（看它变没变）
        self.stop_at = None  # 这一局结束了：到这个时刻停
        self.next_poll = 0.0
        self.said = set()  # 日志里已经说过的"录不了"的原因（每种说一次）
        self.workers = []  # 后台拼 MP4 的线程
        self.legend_name = None
        self.recovered = False  # 上次没拼完的已经拼过了（每次记录只做一次）

    # ---------- 记录程序调用的 ----------

    def update(self, screen, in_match, frame=None):
        """frame：这一张截图（认"击倒"用；不给就不认）。"""
        tick = self.clock()
        if self.rec is None:
            if self._new_match(screen, in_match):
                self._start(in_match)
            return
        self._follow(screen, in_match)
        if frame is not None and screen is None:
            self._watch_knock(in_match.hud.knock(frame), in_match.hud.cfg.get("knock") or {}, tick)
        if self.stop_at is None and (in_match.done or screen in END_SCREENS or screen == "lobby"):
            self.stop_at = tick + self.rules.get("tail_seconds", 3)
        if self.stop_at is not None and tick >= self.stop_at:
            self._finish()
            return
        if tick >= self.next_poll:
            self.next_poll = tick + POLL_SECONDS
            self.rec.poll()

    def close(self, wait=300):
        """停止记录时：还在录的这一局拼完；等后台拼 MP4 的线程做完（最多 wait 秒）。"""
        if self.rec is not None:
            self._finish()
        for worker in self.workers:
            worker.join(wait)

    # ---------- 里面的 ----------

    def _new_match(self, screen, in_match):
        """可能是新的一局开始：开局画面（这一局的横幅还没试过），或看到了自己的界面、而这一局还没录过。
        一局最多试两次（横幅一次、看到自己的界面一次）：录不了的电脑上每试一次要跑一下 ffmpeg，不能每截一张都试。"""
        if self.covered is BANNER and (screen == "lobby" or in_match.done):
            self.covered = None  # 横幅试过、这一局没看到自己的界面就结束了：下一局的横幅再试
        if screen == "match_start":
            return self.covered is not BANNER and (in_match.started is None or in_match.started != self.covered)
        return in_match.pending and in_match.active and in_match.started != self.covered

    def _start(self, in_match):
        settings = self.settings()
        # 这一局试过了：开局画面时还不知道是哪一局（BANNER），看到自己的界面以后在 _follow 里换成 InMatchWatcher.started
        self.covered = BANNER if in_match.started is None else in_match.started
        if not settings.get(SETTING, False):
            return
        self.rules = self.rules or recorder.load_rules()
        ffmpeg = recorder.find_ffmpeg(settings.get("recording_ffmpeg"), self.data_dir)
        out_dir = Path(settings.get("recording_dir") or self.data_dir / "recordings")
        rec = self.factory(ffmpeg, options_from(settings), out_dir, log=self.log, rules=self.rules)
        try:
            rec.start(self.find_pid())
        except recorder.RecordingUnavailable as e:
            if str(e) not in self.said:
                self.said.add(str(e))
                self.log(f"录像：录不了（{e}）")
            return
        self.rec, self.state, self.top, self.stop_at = rec, in_match.state, None, None
        self.knock_seen, self.knock_x = None, None
        self.limit = float(settings.get("recording_limit_gb", DEFAULT_LIMIT_GB)) * 2 ** 30
        self.next_poll = self.clock() + POLL_SECONDS
        if not self.recovered and ffmpeg:  # 上次 Evo 崩了没拼完的：这次第一次开录时在后台拼好
            self.recovered = True
            self._background(lambda: recorder.recover_leftovers(ffmpeg, out_dir, log=self.log), "录像拼回上次的")

    def _background(self, job, name):
        worker = threading.Thread(target=job, name=name)
        worker.start()
        self.workers = [w for w in self.workers if w.is_alive()] + [worker]

    def _follow(self, screen, in_match):
        """对局中：从 InMatchWatcher 记下的状态里看出事件。"""
        if self.covered in (None, BANNER) and in_match.started is not None:
            self.covered = in_match.started
        if in_match.state != self.state:
            event = TRANSITIONS.get((self.state, in_match.state))
            if event:
                self.rec.mark(event)
            self.state = in_match.state
        if screen == "champion" and not any(e["kind"] == "champion" for e in self.rec.events):
            self.rec.mark("champion")
        top = in_match.tops[-1] if in_match.tops else None
        if top is not None and top is not self.top:
            self.top = top
            self.rec.hud_change()
        if in_match.legend is not None:
            self.legend_name = in_match.hud.legends.name(in_match.legend)

    def _watch_knock(self, x, cfg, tick):
        """中间那一行的"击倒"：刚出现、或者隔了 rearm_seconds 又出现、或者还在显示但左右挪了（换了名字）→ 一次击倒。"""
        if x is None:
            return
        fresh = (self.knock_seen is None or tick - self.knock_seen > cfg.get("rearm_seconds", 0.6)
                 or abs(x - self.knock_x) > cfg.get("moved", 20))
        if fresh:
            self.rec.mark("knock")
        self.knock_seen, self.knock_x = tick, x

    def _finish(self):
        rec, self.rec = self.rec, None
        info = {"legend": self.legend_name} if self.legend_name else {}
        self.legend_name, self.stop_at = None, None
        limit, out_dir = self.limit, rec.out_dir

        def work():
            path = rec.finish(info)
            if path is not None:
                recorder.enforce_limit(out_dir, limit, log=self.log)

        self._background(work, "录像拼 MP4")
