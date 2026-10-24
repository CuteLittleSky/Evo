"""外卡模式：总结页卡片上是「最后幸存」「自我急救」→ 这一局是外卡（上游 0 文件改动）。

## 用户 2026-10-24 的要求（原话）

> 如果最后总结界面识别到了"自我急救"和"最后幸存"，认为它是外卡模式，
> 并且在详情中 OCR 这两个选项（仅外卡模式再出现）。

也就是：**认模式的依据是总结页，不是大厅卡片**。这两行字只在总结页出现，而且只在
外卡模式出现（普通模式同一位置写的是「给予急救次数」「给予重生次数」）：

    普通： 生存时间 / 给予急救次数 / 给予重生次数     ← 数字照旧读，模式照旧
    外卡： 生存时间 / 最后幸存     / 自我急救         ← 数字位置一模一样，只是标签换了

所以数字一个都不用改（上游 `summary_page.read_summary` 照常读），我们只多做两件事：

    ① 存总结页那一帧时看一眼标签：读到就是外卡 → 这一局写 mode="wildcard"；
    ② 战绩页把这一局的两行写成「自我急救 / 最后幸存」，和游戏里的叫法一致。

## 为什么不改 config/summary.json 加一个 ocr 项

可以加（`cloud/lobby_mode` 就是这么读大厅卡片模式的），但那要把坐标写进**上游的**配置文件。
坐标放在我们自己的 `rules.py` 里更好：上游升级、重算坐标时不会有第二份规则漂移；
而且这两行标签和"读数据"是两件事，混进 `fields` 会让 `read_summary` 把它们当读数去校验。

## 一共四处注入（全部在内存里）

| 注入点 | 做什么 |
|---|---|
| `SummaryWatcher.update` | 停在总结页的帧上读两行标签；认出来就记住，并**立刻**告诉 `summary_to_match` |
| `SummaryWatcher._record` | 存完这一局，把标记归零 |
| `summary_to_match` | 认出来了才写 mode="wildcard"，没认出来不碰上游读到的模式 |
| `PostMatch.on_summary_saved` | 认出来了就把 `post.info["mode"]` 也改成 wildcard，免得**大厅的旧模式**把它盖回去（见下） |

上游四处都是**先调原函数、再加工**，签名和语义不变；任何一步失败都只写日志。

### 为什么还要包 on_summary_saved（2026-10-24 补的「保险」）

写库的顺序是 `summary_to_match() → storage.insert_match() → … → post.on_summary_saved()`，
而 `code/post_match.py:351` 是 `fields = dict(self.info)`、`:357` 再 `update_match` 一次 ——
`self.info` 里是**这一局开局时大厅读到的**模式。于是有这么一个组合会翻车：

    大厅按「重新单人匹配」读成 ranked → info["mode"] = "ranked"
    这一局其实是外卡 → insert_match 写 wildcard
    on_summary_saved 又把 info 里的 "ranked" 写一遍 → 库里最终是 ranked

反过来（大厅读到外卡、总结页没截到）不补：那种情况 info 本身就是对的。

所以在调原函数**之前**，只在这局被总结页判成外卡时把 `self.info["mode"]` 改成 wildcard，
原函数照自己的逻辑把 info 写进库，就不会用旧值覆盖。**不改 self.info 里别的键**
（map 由 lobby_mode 的 map_guess 负责，legend / player_name 由上游自己 `_legend()` /
`on_player_name` 盖，乱动会抢它们的活）。

### update 里那行「太早」的赋值是什么意思

「认出来」的那一刻 `update` 还没有返回，`_record` 当然还没被调用 —— 上游是
读完两次相同之后才存局。所以直接在 update 里把 `_RECORDING["wildcard"]` 设成 True：
直到 `_record` 存完这一局，都是外卡局；`_record` 存完再复位。
这样既不依赖调用顺序，也不会漏。

### 为什么只挂这两次「有机会命中」的调用

`update` 每 0.3 秒调一次，但只有停在总结页、这一局还没存时才读；`_record` 一局一次。
标签读一次就记住（置位后不再读），一整局最多两次识别，对帧率、内存没有可感知的影响。
"""
from __future__ import annotations

from functools import wraps

from cloud.wildcard import rules

# 跑过一遍 install 了没（幂等）
_INSTALLED = False

# 存局期间「这一局是不是外卡」。上游 `_record` 调 `summary_to_match` 时看不到 watcher，
# 所以由 update 认出来时置位、_record 存完复位；一局只有一次，不会并发。
_RECORDING: dict = {"wildcard": False}

# 本次记录认出来的外卡标记（只用于日志和排查）
_STATE: dict = {"seen": False}


# ---------------------------------------------------------------------------
# 识别：这张总结页是不是外卡
# ---------------------------------------------------------------------------

def read_labels(engine, frame, cfg):
    """在总结页上读两条标签，返回 {标签名: (读到的整段文字, 置信度)}。

    cfg 是上游的 summary.json（只借用它的 base_resolution / upscale / grayscale /
    anchor，也就是 `crop` 换算坐标用的那几个字段），我们自己不另算一遍坐标 ——
    16:10、4:3、2560×1440 这些情况上游 `layout.py` 已经处理好了。

    读不到、认出别的字、引擎出错都按「没读到」处理（宁可空着，不猜）。
    """
    from ocr import crop                     # noqa: PLC0415  上游模块

    try:
        piece = crop(frame, rules.LABEL_ROI, cfg)
        result = engine(piece)
    except Exception:
        return {}
    texts = getattr(result, "txts", None) or ()
    scores = getattr(result, "scores", None) or ()
    seen = {}
    for item in rules.WILDCARD_LABELS:
        best, best_text = 0.0, ""
        for text, score in zip(texts, scores):
            if item["text"] in (text or "") and float(score or 0.0) >= best:
                best, best_text = float(score), text or ""
        if best >= rules.MIN_SCORE:
            seen[item["name"]] = (best_text, best)
    return seen


def detect(engine, frame, cfg):
    """这张总结页是不是外卡：读到的标签数达到 MIN_HITS 就是。"""
    return len(read_labels(engine, frame, cfg)) >= rules.MIN_HITS


# ---------------------------------------------------------------------------
# 记录子进程：识别 + 写库
# ---------------------------------------------------------------------------

def install(live_collect_module):
    """接三处注入。幂等，失败只由调用方写日志。

    注意 `live_collect.py` 里是 `from summary_page import summary_to_match`，
    所以每个模块各有一份引用；`_record` 用的是 `live_collect` 那个 —— 两处都包，
    将来上游改成 `import summary_page` 也照样生效。
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
    _patch_record(watcher)
    try:
        _patch_summary_saved()
    except Exception as exc:     # 保险挂了也照常记外卡：insert_match 那一步已经写进去了
        _log("[外卡模式] on_summary_saved 保险没装上：%r" % (exc,))
    return True


def _patch_summary_to_match(*modules):
    """让 `summary_to_match` 带上 mode="wildcard"。

    为什么不重写 `_record`（那一百多行）：整段复制就成了上游代码的影子副本，
    规范 1.2 明确禁止。`summary_to_match` 只拼字典，包一层既最小又不会漏。
    """
    done = set()
    for module in modules:
        if module is None or id(module) in done:
            continue
        original = getattr(module, "summary_to_match", None)
        if original is None or getattr(original, "_paimiao_wildcard", False):
            continue
        done.add(id(module))

        @wraps(original)
        def summary_to_match(results, problems, ended_at, evidence_path=None, _original=original):
            match = _original(results, problems, ended_at, evidence_path)
            try:
                if _RECORDING["wildcard"]:
                    match["mode"] = rules.MODE_KEY
            except Exception:
                pass        # 补不上就留空，上游照常写库
            return match

        summary_to_match._paimiao_wildcard = True      # type: ignore[attr-defined]
        module.summary_to_match = summary_to_match


def _patch_update(watcher):
    """停在总结页的帧上读一次标签（认出来之后就只置位、不再读）。"""
    original = watcher.update
    if getattr(original, "_paimiao_wildcard_scan", False):
        return

    @wraps(original)
    def update(self, on_summary, frame, now, *args, **kwargs):
        pending = bool(on_summary) and not getattr(self, "done", False)
        if pending and not getattr(self, "_paimiao_wildcard", False):
            try:
                seen = read_labels(self.engine, frame, self.cfg)
            except Exception as exc:      # 认不出就按普通模式走，绝不影响记录
                seen = {}
                _log(f"[外卡模式] 识别失败（这一局按普通模式记录）：{exc!r}")
            if len(seen) >= rules.MIN_HITS:
                # 先置位：这一步早于上游读完、更早于 _record（见模块说明）
                self._paimiao_wildcard = True
                _RECORDING["wildcard"] = True
                _STATE["seen"] = True
                detail = "、".join(f"「{text}」{score:.2f}" for text, score in seen.values() if text)
                _say(self, f"总结页卡片上是{detail or '外卡标签'} → 这局按外卡模式记录")
        elif not pending and not self.waiting:
            # 离开了这一局的总结页、这一局也已经存过：标记归零，免得带到下一局
            _RECORDING["wildcard"] = False
            _STATE["seen"] = False
        # 上游这个方法的签名以后可能多出参数，*args 原样转交，不去猜它们的名字。
        return original(self, on_summary, frame, now, *args, **kwargs)

    update._paimiao_wildcard_scan = True      # type: ignore[attr-defined]
    watcher.update = update


def _patch_summary_saved():
    """保险：总结页判成外卡时，把 `post.info["mode"]` 也改成 wildcard。

    起因：`PostMatch.on_summary_saved`（code/post_match.py:351 `fields = dict(self.info)`、
    :357 `update_match`）会把**开局时大厅读到的**模式再写一遍库。大厅读成 ranked、
    这一局其实是外卡时，insert_match 刚写好的 wildcard 就被旧的 ranked 盖回去了。

    修法只碰 `self.info["mode"]` 一个键，其余的键原样不动（map 归 lobby_mode 的
    map_guess，legend / player_name 归上游自己）。只在这局被判成外卡时才改，
    没判成外卡就完全跟原来的行为一样。
    """
    from post_match import PostMatch         # noqa: PLC0415  上游模块

    original = PostMatch.on_summary_saved
    if getattr(original, "_paimiao_wildcard_mode_guard", False):
        return

    @wraps(original)
    def on_summary_saved(self, match_id, values, when, player_name=None):
        try:
            info = getattr(self, "info", None)
            if _RECORDING["wildcard"] and isinstance(info, dict) and info.get("mode") != rules.MODE_KEY:
                # 有旧值才提示「盖回去了」——这才是原来会出错的那种局
                if info.get("mode") is not None:
                    _log("[外卡模式] 第 %s 局：大厅记的是 %s，总结页判成外卡，改按外卡存"
                         % (match_id, info.get("mode")))
                info["mode"] = rules.MODE_KEY
        except Exception:
            pass            # 改不了就算了，insert_match 那一步已经把 wildcard 写进去了
        return original(self, match_id, values, when, player_name)

    on_summary_saved._paimiao_wildcard_mode_guard = True     # type: ignore[attr-defined]
    PostMatch.on_summary_saved = on_summary_saved


def _patch_record(watcher):
    original = watcher._record
    if getattr(original, "_paimiao_wildcard_mode", False):
        return

    @wraps(original)
    def _record(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        finally:
            _RECORDING["wildcard"] = False
            _STATE["seen"] = False
            self._paimiao_wildcard = False

    _record._paimiao_wildcard_mode = True     # type: ignore[attr-defined]
    watcher._record = _record


def stats():
    """排查用：现在这一帧认没认出外卡。"""
    return dict(_STATE, recording=_RECORDING["wildcard"])


def _say(watcher, message):
    """记录程序的日志（用户打进日志文件里看）。没有 log 就只写 cloud.log。"""
    log = getattr(watcher, "log", None)
    try:
        if callable(log):
            log(message)
            return
    except Exception:
        pass
    _log("[外卡模式] " + message)


def _log(message):
    try:
        from cloud import state as cloud_state      # noqa: PLC0415

        cloud_state.log(message)
    except Exception:
        pass
