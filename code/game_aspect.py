"""游戏画面的宽高比（16:9、16:10、4:3）：Evo 按它换算界面在截图上的位置（见 layout.py）。

截图的尺寸只说明显示器的比例：DX12 全屏用拉伸分辨率（如启动项 -w 2560 -h 1600）时，游戏按 16:10 摆界面，
再压扁成显示器的 2560×1440，截图看起来还是 16:9。所以要另外知道游戏自己用的分辨率（2026-10-20 用户决定）：
- 设置页"游戏画面比例"（settings.json 里的 game_aspect）：自动检测（默认）/ 16:9 / 16:10 / 4:3
- 自动检测：读 Apex 自己的视频设置文件（存档文件夹里的 Respawn\\Apex\\local\\videoconfig.txt，只读不改，原则 1 的例外）。
  全屏时用里面的分辨率（setting.defaultres × setting.defaultresheight）。2026-10-20 实测：DX12 全屏时文件里是
  fullscreen 1、nowindowborder 1，分辨率就是启动项写的那个
- 文件里没有能用的全屏分辨率（没有这个文件、没有分辨率、不是全屏）：看游戏画面认（AspectGuess，2026-11-11 用户决定：
  1diot 的文件里没有分辨率，4:3 拉伸被当成 16:9，什么都认不出）。认出来之前按截图的比例
记录程序每隔几秒看一眼（AspectWatcher：设置改了、文件改了才重新读），变了就写一行日志。

查看自动检测的结果：python code/game_aspect.py

app_settings 用到时才 import：诊断工具（evo_diag.py）只读视频设置文件，不能连带 import paths.py
（它一 import 就会在程序旁边建数据文件夹）。
"""

import ctypes
import os
import sys
import time
import uuid
from collections import Counter
from ctypes import wintypes
from fractions import Fraction
from pathlib import Path

import layout

SETTING_KEY = "game_aspect"
AUTO = "auto"
ASPECTS = {"16:9": 16 / 9, "16:10": 16 / 10, "4:3": 4 / 3}  # 设置页能选的
CHECK_SECONDS = 5  # 记录程序每隔几秒看一眼设置和视频设置文件
GUESS_SECONDS = 1.0  # 看游戏画面认比例时每隔几秒认一次（每次 40–90 毫秒；认出来就不再认）
GUESS_VOTES = 3  # 同一种比例认出这么多次才算数
GUESS_HINT = "记录时看游戏画面认"  # 设置页、测试工具里说明读不到文件里的分辨率时怎么办


def _saved_games():
    """Windows 的"保存的游戏"文件夹（一般是 C:\\Users\\<用户名>\\Saved Games，用户可能挪过位置，所以问系统）。"""
    try:
        guid = uuid.UUID("4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4")  # FOLDERID_SavedGames
        buf = (ctypes.c_ubyte * 16).from_buffer_copy(guid.bytes_le)
        path = ctypes.c_wchar_p()
        shell32 = ctypes.WinDLL("shell32")
        shell32.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE,
                                                 ctypes.POINTER(ctypes.c_wchar_p)]
        if shell32.SHGetKnownFolderPath(ctypes.byref(buf), 0, None, ctypes.byref(path)) == 0:
            found = Path(path.value)
            ctypes.WinDLL("ole32").CoTaskMemFree(path)
            return found
    except (OSError, AttributeError, ValueError):
        pass
    return Path.home() / "Saved Games"


# 测试时用 EVO_VIDEOCONFIG 指到临时文件夹，免得读到开发者电脑上真实的游戏设置
VIDEO_CONFIG = (Path(os.environ["EVO_VIDEOCONFIG"]) if os.environ.get("EVO_VIDEOCONFIG")
                else _saved_games() / "Respawn" / "Apex" / "local" / "videoconfig.txt")


def name(aspect):
    """宽高比 → "16:10" 这样的写法（不是常见的几种就写成 "1.85:1"）。"""
    for label, value in ASPECTS.items():
        if abs(aspect - value) < layout.SAME_ASPECT:
            return label
    r = Fraction(aspect).limit_denominator(20)
    return f"{r.numerator}:{r.denominator}" if r.denominator <= 20 else f"{aspect:.2f}:1"


def read_video_config(path=None):
    """读 Apex 的视频设置文件，返回 {"setting.defaultres": "2560", ...}；读不到返回 None。
    文件是 UTF-16（带 BOM）或 UTF-8 的文本，每行 "键" "值"。"""
    try:
        raw = Path(path or VIDEO_CONFIG).read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "replace")
    out = {}
    for line in text.splitlines():
        parts = [p for p in line.strip().split('"') if p.strip()]
        if len(parts) == 2:
            out[parts[0].strip()] = parts[1].strip()
    return out


def from_video_config(path=None):
    """自动检测：(宽高比或 None, 说明)。None = 文件里没有能用的全屏分辨率（记录程序看游戏画面认，见 AspectGuess）。"""
    cfg = read_video_config(path)
    if cfg is None:
        return None, "没找到 Apex 的视频设置文件"
    try:
        w, h = int(cfg["setting.defaultres"]), int(cfg["setting.defaultresheight"])
        fullscreen = cfg.get("setting.fullscreen") == "1"
    except (KeyError, ValueError):
        return None, "Apex 的视频设置文件里没有分辨率"
    if not fullscreen:
        return None, f"Apex 视频设置：不是全屏（文件里的分辨率 {w}×{h} 不一定在用）"
    if w <= 0 or h <= 0:
        return None, "Apex 视频设置里的分辨率不对"
    return w / h, f"Apex 视频设置：全屏 {w}×{h}"


def choice():
    """设置页选的：AUTO 或 ASPECTS 里的一个。"""
    import app_settings

    value = app_settings.get(SETTING_KEY, AUTO)
    return value if value in ASPECTS else AUTO


def current(path=None):
    """现在该用的宽高比和说明：(宽高比或 None, 说明)。"""
    picked = choice()
    if picked != AUTO:
        return ASPECTS[picked], f"设置页手动选的 {picked}"
    aspect, why = from_video_config(path)
    return aspect, f"自动检测（{why}）"


class AspectGuess:
    """看游戏画面认比例（视频设置文件里没有能用的全屏分辨率时，见本文件开头）。vote(截图) 把截图按几种比例
    （截图本身的、16:9、16:10、4:3）各比一次外观，不用识别引擎：
    - 先比左下角自己的传奇（legends.py）：最像的那种比例下认得出，而且比第二像的那种比例高出 aspect_margin
      （config/legends.json），投这种比例一票。对局中几乎每张都投得出
    - 认不出传奇时比画面（大厅、总结页、开局横幅……，录入模式里还有数据页；screens.py）：每种画面最像的那种比例
      达到门槛、比第二像的高出 aspect_margin（config/screens.json、season_stats.json），投它一票；几种画面投的不一样，这一张不算
    比例不对时界面挪了位置、压扁了，参照图就比不上。2026-11-11 用约 6000 张实战截图（16:9、16:10、4:3）按时间顺序试过：
    一票都没投错，每一段都认对（见 docs/识别经验.md"不同画面比例"）。"""

    LEGEND = "左下角的传奇"

    def __init__(self, legends, sources):
        """legends：legends.LegendDetector；sources：函数，返回现在要比的 screens.ScreenDetector 列表
        （规则里有 aspect_margin 的才比，画面规则里写了 "aspect_vote": false 的不比）。"""
        self.legends, self.sources = legends, sources

    @staticmethod
    def candidates(frame):
        """要比的几种比例：截图本身的，加上 ASPECTS 里和它不一样的。"""
        h, w = frame.shape[:2]
        found = [w / h]
        for aspect in ASPECTS.values():
            if all(abs(aspect - x) >= layout.SAME_ASPECT for x in found):
                found.append(aspect)
        return found

    @staticmethod
    def _best(scores, margin):
        """最像的那种比例；比第二像的高出不到 margin 时返回 None。"""
        order = sorted(scores, key=scores.get, reverse=True)
        if len(order) < 2 or scores[order[0]] - scores[order[1]] < margin:
            return None
        return order[0]

    def vote(self, frame):
        """这一张投哪种比例：(宽高比, 从哪认出的，如"左下角的传奇")；投不出返回 (None, "")。"""
        before = layout.game_aspect()
        try:
            return self._vote(frame, self.candidates(frame))
        finally:
            layout.set_game_aspect(before)

    def _vote(self, frame, aspects):
        legend = {}
        for aspect in aspects:
            layout.set_game_aspect(aspect)
            ranked = self.legends.scores(frame)
            legend[aspect] = (ranked[0][1] if ranked else -1.0, self.legends.decide(ranked) is not None)
        best = self._best({a: score for a, (score, _) in legend.items()}, self.legends.cfg["aspect_margin"])
        if best is not None and legend[best][1]:
            return best, self.LEGEND
        checks = [(d, name, spec) for d in self.sources() if "aspect_margin" in d.cfg
                  for name, spec in d.cfg["screens"].items()
                  if spec.get("aspect_vote", True) and d.templates.get(name) is not None]
        sims = [{} for _ in checks]
        for aspect in aspects:
            layout.set_game_aspect(aspect)
            for one, (d, name, _) in zip(sims, checks):
                one[aspect] = d.similarity(frame, name)
        votes = {}
        for one, (d, name, spec) in zip(sims, checks):
            best = self._best(one, d.cfg["aspect_margin"])
            if best is not None and one[best] >= spec.get("min_similarity", d.cfg["min_similarity"]):
                votes.setdefault(best, spec.get("label", name))
        if len(votes) == 1:
            return next(iter(votes.items()))
        return None, ""


class AspectWatcher:
    """记录程序里每截一张调用一次 check(截图)：每隔 CHECK_SECONDS 秒看一眼设置和视频设置文件，
    宽高比变了就设给 layout，并写一行日志。fixed：测试时直接指定（如 "16:10"），不看设置和文件。
    guess：AspectGuess。自动检测、文件里没有能用的全屏分辨率时，每隔 GUESS_SECONDS 秒用它认一次，
    同一种比例认出 GUESS_VOTES 次就用它（之前按截图的比例）；不给就一直按截图的比例。
    认出来以后，同样大小的截图上不再认（视频设置文件又改了、还是没有分辨率时照用）。"""

    def __init__(self, log, clock=time.monotonic, path=None, fixed=None, guess=None):
        self.log, self.clock, self.path, self.fixed, self.guess = log, clock, path, fixed, guess
        self.next_check = None
        self.state = None  # (设置, 文件修改时间, 截图尺寸)：都没变就不用重新读
        self.announced = None
        self.guessing = None  # 正在看游戏画面认：为什么要认（自动检测的说明）；没在认是 None
        self.guessed = None  # 认出来的：(截图尺寸, 宽高比, 从哪认出的)
        self.votes = Counter()
        self.next_guess = None

    def check(self, frame):
        now = self.clock()
        if self.next_check is None or now >= self.next_check:
            self.next_check = now + CHECK_SECONDS
            self._look(frame)
        if self.guessing is not None and (self.next_guess is None or now >= self.next_guess):
            self.next_guess = now + GUESS_SECONDS
            self._guess(frame)

    def _say(self, text):
        if text != self.announced:
            self.log(text)
            self.announced = text

    def _look(self, frame):
        try:
            mtime = Path(self.path or VIDEO_CONFIG).stat().st_mtime
        except OSError:
            mtime = None
        h, w = frame.shape[:2]
        state = (self.fixed or choice(), mtime, (w, h))
        if state == self.state:
            return
        self.state = state
        self.guessing = None
        if self.fixed:
            aspect, why = ASPECTS[self.fixed], f"测试时指定的 {self.fixed}"
        else:
            aspect, why = current(self.path)
        if aspect is None and self.guess is not None:
            if self.guessed is not None and self.guessed[0] == (w, h):
                aspect, why = self.guessed[1], f"{why}，看游戏画面认出：{self.guessed[2]}"
            else:
                self.guessing, self.guessed, self.next_guess = why, None, None
                self.votes.clear()
                layout.set_game_aspect(None)
                self._say(f"游戏画面比例：先按截图的 {name(w / h)}，看游戏画面认（{why}；截图 {w}×{h}）")
                return
        layout.set_game_aspect(aspect)
        self._say(f"游戏画面比例：{name(aspect or w / h)}（{why}；截图 {w}×{h}）")

    def _guess(self, frame):
        aspect, where = self.guess.vote(frame)
        if aspect is None:
            return
        self.votes[aspect] += 1
        if self.votes[aspect] < GUESS_VOTES:
            return
        h, w = frame.shape[:2]
        self.guessed = ((w, h), aspect, where)
        layout.set_game_aspect(aspect)
        self._say(f"游戏画面比例：{name(aspect)}（{self.guessing}，看游戏画面认出：{where}；截图 {w}×{h}）")
        self.guessing = None

    def reset(self):
        """停止记录时：换算回"和截图一样"，不影响同一个进程里以后的事（如测试）。"""
        layout.set_game_aspect(None)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"视频设置文件：{VIDEO_CONFIG}")
    aspect, why = from_video_config()
    print(f"自动检测：{name(aspect) if aspect else GUESS_HINT}（{why}）")
    print(f"设置页选的：{'自动检测' if choice() == AUTO else choice()}")


if __name__ == "__main__":
    main()
