"""子弹时间：总结页卡片上有「降级近战攻击」→ 这一局是子弹时间（上游 0 文件改动）。

## 用户 2026-10-24 的要求（原话）

> 子弹时间是混合模式里面的，就按降级近战攻击这一行来定吧

也就是：判据只有一条 —— 自己那张卡片上出现「降级近战攻击」。坐标怎么来的
（为什么要自己算黑边、为什么有三块候选）见 rules.py 开头那段。

## 和外卡（cloud/wildcard）的关系：并列，不重叠

| | 外卡 | 子弹时间 |
|---|---|---|
| 判据 | 卡片上是「最后幸存 / 自我急救」 | 卡片上是「降级近战攻击」 |
| 写库 | mode="wildcard" | mode="bullet" |
| 详情页那两行 | 换成「自我急救 / 最后幸存」 | 不动（子弹时间没有这两行） |

两个模块包的是同样几个函数，**后包的那层调先包的那层**（`@wraps` 保序），
所以谁装在前面都不会互相踩。两个判据不会同时成立：外卡那版卡片上是
「生存时间 / 最后幸存 / 自我急救」，没有「降级近战攻击」这一行。

## 和「排位 / 匹配」的关系：大厅的答案更权威

用户 2026-10-24 的原话是「子弹时间是混合模式里面的」—— 混合模式本身是排位还是匹配，
大厅卡片上已经写着。所以：

    大厅读到 排位/匹配 → **保留**（子弹时间只是这一局的玩法，排位分、筛选都照旧）
    大厅没读到        → 填 bullet「子弹时间」

这一点和 on_summary_saved 的保险配合：只有 `info["mode"]` 是空的（或者还没有）
才把我们认到的 bullet 写进去，绝不覆盖大厅读到的排位/匹配。

## 一共三处注入（全部在内存里）

| 注入点 | 做什么 |
|---|---|
| `SummaryWatcher.update` | 停在总结页的帧上按行位找「降级近战攻击」；读到就记住，并**立刻**告诉 `summary_to_match` |
| `summary_to_match` | 认出来了、并且上游没读到模式时写 mode="bullet" |
| `PostMatch.on_summary_saved` | 认出来了、并且 info 里没有模式时补上 bullet，免得被空值写回库 |

`SummaryWatcher._record` 存完这一局把标记归零（和外卡同一个写法）。

## 为什么扫描只在「这一局还没认出」时做

一局里会停在总结页好几十秒（上游要读两次相同才存局），每帧扫三块要 300 毫秒。
所以第一次读到就置位、之后不再扫；这一局存完（`_record`）才复位。
一局最多扫到认出来为止，对帧率、内存没有可感知的影响。
"""
from __future__ import annotations

from functools import wraps

from cloud.bullet import rules

# 跑过一遍 install 了没（幂等）
_INSTALLED = False

# 存局期间「这一局是不是子弹时间」。由 update 认出来时置位、_record 存完复位；一局一次，不会并发。
_RECORDING: dict = {"bullet": False}

# 本次记录认出来的标记（只用于日志和排查）
_STATE: dict = {"seen": False, "label": ""}


# ---------------------------------------------------------------------------
# 识别：这张总结页上有没有「降级近战攻击」
# ---------------------------------------------------------------------------

def to_screenshot(frame, roi, rect=None):
    """基准坐标 [x, y, 宽, 高] → 截图上的像素 (左, 上, 右, 下)。

    按 rules.RECT 把 1920×1080 的画面放进截图（四周留黑边）再放大；截图比画面还小、
    或者尺寸看不出来时，退化成等比拉伸（也就是上游 crop 的做法）。
    """
    rect = rect or rules.RECT
    height, width = frame.shape[:2]
    bw, bh = rules.BASE_RESOLUTION
    left, top, canvas_w, canvas_h = rect
    scale_x, scale_y = width / float(canvas_w), height / float(canvas_h)
    x, y, w, h = roi
    return (round((left + x) * scale_x), round((top + y) * scale_y),
            round((left + x + w) * scale_x), round((top + y + h) * scale_y))


def read_label(engine, frame):
    """在一帧总结页上找「降级近战攻击」，返回读到的 (整段文字, 置信度)；没读到返回 None。

    按 rules.CANDIDATES 顺序读（子弹时间的位置在最上面，普通版式那一行排第二），
    第一个命中就返回。裁剪失败、引擎出错都按「没读到」处理（宁可空着，不猜）。
    """
    import cv2                              # noqa: PLC0415  和上游一样用 dsh 自带的 opencv
    import ocr                              # noqa: PLC0415  上游模块（只用它的 crop）

    cfg = rules.ocr_cfg()
    bw, bh = rules.BASE_RESOLUTION
    for roi in rules.CANDIDATES:
        try:
            left, top, right, bottom = to_screenshot(frame, roi)
            piece = frame[max(top, 0):max(bottom, 0), max(left, 0):max(right, 0)]
            if piece is None or piece.size == 0:
                continue
            # 放大到「基准尺寸 × upscale」，和上游 crop 得到的块一样大
            piece = cv2.resize(piece, (roi[2] * rules.UPSCALE, roi[3] * rules.UPSCALE),
                               interpolation=cv2.INTER_CUBIC)
            if cfg.get("grayscale"):
                piece = cv2.cvtColor(cv2.cvtColor(piece, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
            result = engine(piece)
        except Exception as exc:
            _log("[子弹时间] 识别失败（这一局按普通模式记录）：%r" % (exc,))
            continue
        for text, score in zip(getattr(result, "txts", None) or (),
                               getattr(result, "scores", None) or ()):
            if rules.LABEL in (text or "") and float(score or 0.0) >= rules.MIN_SCORE:
                return (text or "", float(score))
    return None


def detect(engine, frame):
    """这张总结页是不是子弹时间。"""
    return read_label(engine, frame) is not None


# ---------------------------------------------------------------------------
# 记录子进程：识别 + 写库
# ---------------------------------------------------------------------------

def install(live_collect_module):
    """接三处注入。幂等，失败只由调用方写日志。

    `live_collect.py` 里是 `from summary_page import summary_to_match`，
    所以每个模块各有一份引用；`_record` 用的是 `live_collect` 那个 —— 两处都包。
    """
    global _INSTALLED
    if _INSTALLED:
        return True
    watcher = getattr(live_collect_module, "SummaryWatcher", None)
    if watcher is None:
        raise RuntimeError("上游没有 SummaryWatcher")
    import summary_page                     # noqa: PLC0415  上游模块

    _INSTALLED = True
    _patch_summary_to_match(live_collect_module, summary_page)
    _patch_update(watcher)
    try:
        _patch_summary_saved()
    except Exception as exc:     # 保险挂了也照常认子弹时间：summary_to_match 那一步已经写了
        _log("[子弹时间] on_summary_saved 保险没装上：%r" % (exc,))
    return True


def _patch_summary_to_match(*modules):
    """让 `summary_to_match` 在「认出来了、上游又没读到模式」时带上 mode="bullet"。

    只补空位：大厅已经读到排位/匹配时不动它（见模块说明）。
    """
    done = set()
    for module in modules:
        if module is None or id(module) in done:
            continue
        original = getattr(module, "summary_to_match", None)
        if original is None or getattr(original, "_paimiao_bullet", False):
            continue
        done.add(id(module))

        @wraps(original)
        def summary_to_match(results, problems, ended_at, evidence_path=None, _original=original):
            match = _original(results, problems, ended_at, evidence_path)
            try:
                if _RECORDING["bullet"] and not match.get("mode"):
                    match["mode"] = rules.MODE_KEY
            except Exception:
                pass        # 补不上就留空，上游照常写库
            return match

        summary_to_match._paimiao_bullet = True      # type: ignore[attr-defined]
        module.summary_to_match = summary_to_match


def _patch_update(watcher):
    """停在总结页的帧上找「降级近战攻击」（认出来之后就不再扫）。"""
    original = watcher.update
    if getattr(original, "_paimiao_bullet_scan", False):
        return

    @wraps(original)
    def update(self, on_summary, frame, now, *args, **kwargs):
        pending = bool(on_summary) and not getattr(self, "done", False)
        if pending and not getattr(self, "_paimiao_bullet", False):
            try:
                found = read_label(self.engine, frame)
            except Exception as exc:      # 认不出就按普通模式走，绝不影响记录
                found = None
                _log(f"[子弹时间] 识别失败（这一局按普通模式记录）：{exc!r}")
            if found:
                # 先置位：这一步早于上游读完、更早于 _record
                self._paimiao_bullet = True
                _RECORDING["bullet"] = True
                _STATE["seen"] = True
                _STATE["label"] = found[0]
                _say(self, f"总结页卡片上是「{found[0]}」{found[1]:.2f} → 这局是子弹时间")
        elif not pending and not self.waiting:
            # 离开了这一局的总结页、这一局也已经存过：标记归零，免得带到下一局
            _RECORDING["bullet"] = False
            _STATE["seen"] = False
        # 上游这个方法的签名以后可能多出参数，*args 原样转交，不去猜它们的名字。
        return original(self, on_summary, frame, now, *args, **kwargs)

    update._paimiao_bullet_scan = True      # type: ignore[attr-defined]
    watcher.update = update


def _patch_summary_saved():
    """保险：这局是子弹时间、而 info 里还没有模式时补上 bullet。

    理由和外卡那份一样（见 cloud/wildcard/detect.py 的说明）：`on_summary_saved`
    会把 `self.info` 整份再写一遍库，info 里是**开局时大厅读到的**模式。
    和外卡不同的是这里**只填空位**：大厅读到排位/匹配时，那是更权威的答案
    （子弹时间是混合模式里的玩法），不能拿 bullet 盖掉它。
    """
    from post_match import PostMatch         # noqa: PLC0415  上游模块

    original = PostMatch.on_summary_saved
    if getattr(original, "_paimiao_bullet_mode_guard", False):
        return

    @wraps(original)
    def on_summary_saved(self, match_id, values, when, player_name=None):
        try:
            info = getattr(self, "info", None)
            if _RECORDING["bullet"] and isinstance(info, dict) and not info.get("mode"):
                info["mode"] = rules.MODE_KEY
                _log("[子弹时间] 第 %s 局：大厅没读到模式，按子弹时间存" % (match_id,))
        except Exception:
            pass            # 改不了就算了，summary_to_match 那一步已经把 bullet 写进去了
        return original(self, match_id, values, when, player_name)

    on_summary_saved._paimiao_bullet_mode_guard = True     # type: ignore[attr-defined]
    PostMatch.on_summary_saved = on_summary_saved


def _patch_record(watcher):
    original = watcher._record
    if getattr(original, "_paimiao_bullet_mode", False):
        return

    @wraps(original)
    def _record(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        finally:
            _RECORDING["bullet"] = False
            _STATE["seen"] = False
            self._paimiao_bullet = False

    _record._paimiao_bullet_mode = True     # type: ignore[attr-defined]
    watcher._record = _record


def stats():
    """排查用：现在这一帧认没认出子弹时间。"""
    return dict(_STATE, recording=_RECORDING["bullet"])


def _say(watcher, message):
    """记录程序的日志（用户打进日志文件里看）。没有 log 就只写 cloud.log。"""
    log = getattr(watcher, "log", None)
    try:
        if callable(log):
            log(message)
            return
    except Exception:
        pass
    _log("[子弹时间] " + message)


def _log(message):
    try:
        from cloud import state as cloud_state      # noqa: PLC0415

        cloud_state.log(message)
    except Exception:
        pass
