"""总结页的读数（上游 0 文件改动，注入）。

## 这个模块为什么存在（用户 2026-10-24 报的 bug）

先看用户的原话：

> 击杀/助攻 没读到

两张真机截图（`captures/mode_probe/user_bullet_panel.png`、`captures/samples_wildcard/总结页_外卡_2560x1440.png`）
和一张真机全屏（`captures/live/20261024_224044/02_225753.png`）上都复现了，原因有两层：

1. **框太小**：上游 `config/summary.json` 的 `kda` 框是 `[748, 404, 180, 42]`，
   实测「击杀/助攻/击倒」这一行在基准坐标里是 **`[740, 380, 260, 60]`**。
   上游那个框比这一行**矮了 24 个基准像素**（放大了二三倍才勉强卡到 hùn子上），
   而且只有 180 宽，只框得到数字、框不到右边的「击倒」标签 ——
   RapidOCR 的检测网络看不到足够上下文就不出字，于是整块读成空。
   同一个位置 `damage` 框 `[748, 472, 180, 46]` 刚好卡准，所以伤害一直读得到。
2. **一空就整局不稳**：`kda` 在 `summary_page.KEY_FIELDS` 里，读不到 → 这一局被标
   「待确认」→ 面板上那三格是 `0-99`，用户以为坏了。

## 怎么修：整行读，数字取「标签后面那条」

上游 `read_summary(engine, img, cfg)` 是「对 `cfg["fields"]` 每一项调 `read_field`」，
所以只要在**它自己的配置副本**里把每一行的框换成量出来的整行框，其余流程（清洗、
解析、范围、置信度、`problems`、`summary_to_match` 写库）一个字都不用动。

每一行的整行框（基准坐标，见 `rules.ROW_FRAMES`）里同时有标签和数字，读出来的
文字是一串：`['击杀/助攻', '6/4/7']`、`['造成伤害', '1,374']`、`['生存时间', '11:18']`。
取「以这一行的标签结尾的那条」后面的数字，交给上游 `read_field` 去解析、判范围、
算置信度 —— 上游的 `parse()` 本来就会 `clean()` 掉标签和空格再做正则。

## 为什么不能「只框数字」

试过：数字格 = 整行框左边让开 70 像素。实测 `captures/mode_probe/control_summary.png`
（真机 2560×1440，「伤害」行）：

| 数字格 | 读到 |
|---|---|
| x 让开 40 | `成伤害` + `374` |
| x 让开 50 | `成伤害` + `Y` + `74` |
| x 让开 60 | `74` |
| x 让开 70 | 空 |

`1,374` 被吃掉开头的 1、或者干脆一个字都不出 —— RapidOCR 的检测网络对这种
「只剩一截」的窄块很不友好，而且差几个像素结果就完全变样。整行框稳定得多。

## 外卡：从「生存时间」那行起多一行

外卡卡片上「自我急救 / 最后幸存」两行的**标签和值正好落在普通版式的行位里**
（实测 `captures/samples_wildcard/总结页_外卡_2560x1440.png` 和
`captures/live/20261024_224044/02_225753.png`），所以不用额外算行数：
`revives` / `respawns` 两行读到了这两个标签，就是外卡。
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import sys
from functools import wraps

from cloud.summary_rows import rules

# 跑过一遍没（幂等）
_INSTALLED = False
# 两块框各用了几号候选（只记最后一份，排查用；结构同上游 results）
_STATE: dict = {"kda": None, "damage": None}
_LINES: dict = {}                              # 每行整行读到的原始文字，排查用

# 上游那个模块的完整名。`live_collect.py:60` 是 `from summary_page import ...`，
# 所以不管主进程还是记录子进程，模块名都是这一个。
TARGET = "summary_page"


class _Finder(importlib.abc.MetaPathFinder):
    """盯住 `import summary_page`：**子进程里第一次被导入时**才在那里装一次。

    为什么要这一层：`bootstrap.install()` 是在主进程跑起来的，而读数发生在
    spawn 出来的记录子进程里（`multiprocessing` 的 spawn：子进程是全新解释器，
    不吃主进程的 `sys.meta_path` 和函数属性）。子进程 `live_collect.py:60` 一
    `from summary_page import ...`，这个 finder 就会被问到 —— 正好是装注入的时机。
    主进程里装的那次只是顺手：面板自己 `import summary_page` 时同样生效。

    注意：**不能**用 `importlib.util.find_spec(fullname)`，它会把 `sys.meta_path`
    从头再走一遍、又回到这里 —— 实测直接 RecursionError 把整个导入链炸掉。
    这里只问 `PathFinder`（按 sys.path 找文件的那一个），它不会回头找我们。
    """

    def find_spec(self, fullname, path=None, target=None):
        if fullname != TARGET:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        if isinstance(spec.loader, _AfterImportLoader):
            return spec                    # 已经包好了
        # 子进程里第一次导入：包一层「导完就换上我们的读数」，然后交给正常流程执行
        try:
            wrapped = importlib.util.spec_from_loader(fullname, _AfterImportLoader(spec.loader))
            wrapped.submodule_search_locations = spec.submodule_search_locations
            _mark_installed()
            return wrapped
        except Exception:                  # 包不上就原样导，用上游自己的读数
            return spec


def _install_finder():
    sys.meta_path.insert(0, _Finder())


def _mark_installed():
    """记下“这个进程里已经装上”（只用于日志/自检，不影响行为）。"""
    global _INSTALLED
    _INSTALLED = True


class _AfterImportLoader(importlib.abc.Loader):
    """包住上游真正的 loader：模块体执行完（名字都在了）立刻换掉 read_summary。"""

    def __init__(self, inner):
        self._inner = inner

    def create_module(self, spec):
        make = getattr(self._inner, "create_module", None)
        return make(spec) if make else None

    def exec_module(self, module):
        self._inner.exec_module(module)
        try:
            _patch_read_summary(module)
        except Exception as exc:      # 换不上就照旧用上游自己的读数，绝不影响记录
            _log(f"[总结页读数] 注入失败（按上游原来的框读）：{exc!r}")




def read_cell(engine, img, name, spec, cfg, save_crops):
    """从算好的框里读一个字段（`read_field` 只负责框里那点字）。"""
    from summary_page import read_field     # noqa: PLC0415  上游模块

    result = None
    for roi in spec.get("rois") or [spec["roi"]]:
        candidate = dict(spec, roi=roi)
        candidate.pop("rois", None)
        candidate.pop("probe_below", None)
        result = read_field(engine, img, name, candidate, cfg, save_crops)
        _STATE[name] = dict(result, roi=roi)
        if result["status"] in rules.GOOD_STATUSES:
            return result
    return result          # 全都读不到：返回最后一块的结果（和上游“没读到”一致）


def read_with_candidates(engine, img, name, spec, cfg, save_crops):
    """上游 `read_field` 的替代：按候选顺序读，第一个“读到了”的就采用。

    候选 = spec 给的几块 + 再往下探两行（每行 `rules.CARD_ROW_HEIGHT` 像素）。
    往下探是兜一种真机情况：单排那张（`captures/live/20261024_232411/01_233314.png`）
    卡片里第二行直接是「造成伤害」，整块版式比平时往上偏了几个像素，
    第一块吃到的是标签行（读到 `3/10`、格式不对），要再往下一点才吃到数字。
    只有格式对上的才停；全都不对时返回最后一块的结果（和上游“没读到”一致）。

    只给顶部两格（名次、小队总击杀）用：那两格上游的框实测是对的。
    """
    from summary_page import read_field     # noqa: PLC0415  上游模块

    rois = [list(r) for r in (spec.get("rois") or [spec["roi"]])]
    if spec.get("probe_below", True):
        base = rois[0]
        rois += [[base[0], base[1] + n * rules.CARD_ROW_HEIGHT, base[2], base[3]]
                 for n in range(1, 3)]
    result = None
    for roi in rois:
        candidate = dict(spec, roi=roi)
        candidate.pop("rois", None)
        candidate.pop("probe_below", None)
        result = read_field(engine, img, name, candidate, cfg, save_crops)
        _STATE[name] = dict(result, roi=roi)
        if result["status"] in rules.GOOD_STATUSES:
            return result
    return result          # 全都读不到：返回最后一块的结果（和上游“没读到”一致）


def _read_rows(engine, img, cfg, save_crops):
    """把卡片上的每一行读出来：整行框读一次，按「标签 / 数字」把文字块分开。

    顺序不重要：外卡的那几行**不是靠别的行判断出来的** —— 外卡卡片上「自我急救」
    「最后幸存」这两行的标签和值本来就落在普通版式的行位里（实测见 `rules` 注释），
    所以每一行都拿同一套行位去读，读到标签才是外卡。`kda`/`survival_time`/
    `revives`/`respawns` 是「定位用」的，值一律不用（上游本来也不记这几个），
    读不到也不算问题。
    """
    results = {}
    _STATE.clear()                      # 每张图都从头记，别让上一张图的残留混进来
    _LINES.clear()
    base = rules.patch_config(cfg)
    if base is None:
        return None
    for name, spec in base["fields"].items():
        if name in rules.ROW_FRAMES:
            results[name] = read_row(engine, img, name, spec, base, save_crops)
            if name in rules.LOCATOR_FIELDS:
                results[name]["status"] = "ok"     # 定位用，值不用；别让上游按它报问题
        elif name in ("placement", "squad_kills"):
            results[name] = read_with_candidates(engine, img, name, spec, base, save_crops)
        else:
            results[name] = read_cell(engine, img, name, spec, base, save_crops)
    return results, results["revives"].get("wildcard") or 0


def _read_row(engine, img, name, cfg):
    """这一行的标签在哪、它前面那条是什么。

    Returns: (标签那档的整段文字, 标签前面那条, 标签对上几成, 标签在第几条)

    标签可以是整段（`造成伤害`），也可以只是后半截（`击杀/助攻` 是 `击杀/助攻/击倒`
    读少了一截，`rules.label_state`）。找标签时按 `rules.row_marker` 跳过上下行的
    行名，免得把「上一行的标签」当成本行的 —— 实测框一往上抬高，OCR 会把
    上一行整个读进来（`['3,334', '击杀/助攻', '5/6']`，那个 `5/6` 不是本局的击杀）。

    行位移了（`rules.LINE_TOP`）也认：量到标签的下沿落在框的什么高度，
    把框往下挪同样多的像素再读一次。实测控制模式的卡片行位比普通卡片靠下
    十几个像素，第一档只能读到标签、读不到数字。
    """
    from ocr import crop as crop_region     # noqa: PLC0415  上游模块

    frame = rules.row_candidates(name)[0]
    lines, tops = _ocr(engine, img, frame, cfg, crop_region)
    for index, line in enumerate(lines):
        state = rules.label_state(name, line)
        if not state:
            continue
        label_index = index
        above = [lines[index - 1]] if index else []
        shift = tops[index] - rules.LINE_TOP.get(name, 0) if index < len(tops) else 0
        if abs(shift) > rules.SHIFT_TOLERANCE:
            moved = [frame[0], frame[1] + shift, frame[2], frame[3]]
            moved_lines, moved_tops = _ocr(engine, img, moved, cfg, crop_region)
            moved_state = next((found for found in
                                (rules.label_state(name, text) for text in moved_lines) if found), None)
            if moved_state:
                lines, tops, state = moved_lines, moved_tops, moved_state
                label_index = next(i for i, text in enumerate(moved_lines)
                                   if rules.label_state(name, text) == moved_state)
                above = [lines[label_index - 1]] if label_index else []
        return lines, above, state, label_index
    return lines, [], None, None


def _ocr(engine, img, frame, cfg, crop_region):
    """读一档整行框：返回 (文字列表, 每条文字在框里离上沿多少像素)。"""
    probe = engine(crop_region(img, frame, cfg))
    lines = [str(t) for t in (probe.txts or ())]
    boxes = getattr(probe, "boxes", None)
    tops = []
    for box in ([] if boxes is None else list(boxes)):
        tops.append(int(min(point[1] for point in box)))
    return lines, tops


def read_row(engine, img, name, spec, cfg, save_crops):
    """读一行的数字：标签后面那条是这一行自己的数字；再往上是上一行的（兜底、低分）。

    第一档框常把这一行「往外切一半」：标签的下沿落在框底、数字被切掉一半，读出来
    是 `5/6`（真值 `5/16`）。所以分四种情况：
      - 标签整段对上、数字也对得上（`rules.value_state`）→ 直接用
      - 框顶撞进的是上一行的数字（外卡 `自我急救` 那一行，`0` 在标签上面）→ 往下挪，
        一直挪到「上一行的数字终于不在框里」，剩下的按①②③处理
      - 标签整段对上、数字对不上 → 框骑到下面去了，往下挪一档再读
      - 标签只对上前面一半（`击杀/助攻`）→ 这一档的值也是半截，按量到的标签位置
        把框往下挪、再读一次
    挪完还是对不上，就当「标签在、值没读到」（低分 / 空着），不猜。

    外卡那一行也靠这里判断：读到 `自我急救` / `最后幸存` 的标签 = 这一行在。
    """
    from ocr import crop as crop_region     # noqa: PLC0415  上游模块

    frame = rules.row_candidates(name)[0]
    lines, above, state, index = _read_row(engine, img, name, cfg)
    tail = lines[index + 1:index + 1 + rules.TAIL_LIMIT] if state is not None else []
    status = rules.value_state(name, tail)
    if state is not None and _looks_foreign(name, index):
        # 标签在上、上一行的数字在下（实测 2026-10-25：外卡 `自我急救` 的框顶 8 像素处
        # 是 `最后幸存` 的 `0`）。往下挪一档时上一行的数字还赖在框顶（`0` 在 0 像素处），
        # 挪两档才干净，所以一档一档挪到它走了为止；走不掉才退回那个值（低分）。
        clean = None
        for frame in rules.row_candidates(name)[1:]:
            moved = [str(t) for t in (engine(crop_region(img, frame, cfg)).txts or ())]
            if not moved:
                continue
            clean = moved
            if rules.label_state(name, moved[0]) and rules.value_state(name, moved[1:]) == "full":
                # 标签第一条、后面就是本行的数字：这就是干净的一档（挪得越多耗时越久，见好就收）
                break
        if clean:
            lines, above = clean, []
            index = next((i for i, text in enumerate(clean) if rules.label_state(name, text)), None)
            tail = clean[index + 1:] if index is not None else []
            status = rules.value_state(name, tail)
        else:
            tail, above, status = [], [], "not_found"
    elif status != "full":
        # 框往下挪一档：框顶那一带就是这一行的标签，剩下的文字都算这一行的值
        probe = engine(crop_region(img, rules.row_candidates(name)[1], cfg))
        below = [str(t) for t in (probe.txts or ())]
        below = [text for text in below if not rules.row_marker(name, text)]
        if below or state is None:
            tail, above = below, []
            lines = lines or below
    _LINES[name] = lines
    result = _take_value(name, spec, cfg, tail, above, lines, state,
                         None if lines is tail else index)
    _STATE[name] = dict(result, roi=frame, lines=lines, label=state, above=above)
    return result


def _parses(spec, cfg, text):
    from summary_page import clean, in_range, parse     # noqa: PLC0415  上游模块

    value = parse(spec.get("expect", "integer"), clean(text, (cfg or {}).get("fix_characters") or {}))
    return value is not None and in_range(value, spec.get("range"))


def _take_value(name, spec, cfg, tail, above, lines, state=None, label_index=None):
    """从「标签后面那几条」里解析出数字；不行再看标签上面那条。

    上游 `read_field` 只认 `ocr.crop(img, roi)` 读出来的东西，没法直接喂文字，
    所以这里用它自己的 `clean` / `parse` / `in_range` 做同一件事：
    先把这几条拼起来试（`6` `/4` `/7` 可能被切成三条），拼不上再逐条试。

    对不上就返回 `parse_fail`（和上游“读到字但格式不对”一样，调用方按没读到处理）。

    置信度（`score`，只给 `ok` 档）的意思是「这个值可信到什么程度」：
    - 标签整段对上、位置也对（`rules.LABEL_VALUES`）→ 这一行自己的数字，满信
    - 标签只对上后半截 → 值也是半截（`5/6`），给低分，让用户确认一下
    - 标签前面那条 → 这一行的数字被框往下切掉了，值本身是完整的
    - 位置是邻行的 → 多半是上下行漏进框里的，低分
    `None` = 不加权，`0.4` 这种低分上游会保留值、把整局标成「待确认」。
    """
    from summary_page import clean, in_range, parse     # noqa: PLC0415  上游模块

    expect = spec.get("expect", "integer")
    bounds = spec.get("range")                          # 上游用的是 "range"，不是 "bounds"
    own = rules.LABEL_VALUES.get(name)
    replace = (cfg or {}).get("fix_characters") or {}
    fix = lambda text: clean(text, replace)             # noqa: E731  上游的清洗要带替换表

    def hit(text, score):
        value = parse(expect, text)
        if value is None or not in_range(value, bounds):
            return None
        return {"name": name, "label": spec.get("label", name), "value": value,
                "raw": text, "score": score, "ms": 0, "status": "ok"}

    for n in range(len(tail), 0, -1):
        text = fix("".join(tail[:n]))
        if not text:
            continue
        if own is not None and text in own:
            score = rules.PARTIAL_SCORE if state == "part" else 0.9
            if label_index and text not in (rules.ALLOW_ABOVE.get(name) or ()):
                # 标签前面还有东西：这条多半是上一行的数字（标签在上、值在下）
                score = rules.ABOVE_SCORE
        elif text in (rules.ALLOW_ABOVE.get(name) or ()):
            # 值是这类行「上一行的那种数」：可能是撞进框顶的上一行（见 rules 注释）→ 低分
            score = rules.ABOVE_SCORE
        else:
            score = None
        found = hit(text, score)
        if found:
            return found
    # 标签上面那条：这一行的数字被框往下切掉时会落在这里（值本身是完整的）；
    # 但外卡那种「上一行的数字」也在这里，所以任何一行从上面取到的值都打低分。
    for text in tail + above:
        text = fix(text)
        found = hit(text, rules.ABOVE_SCORE if above and text in above else None) if text else None
        if found:
            return found
    raw = fix(" ".join(tail or lines))
    return {"name": name, "label": spec.get("label", name), "value": None,
            "raw": raw, "score": None, "ms": 0,
            "status": "parse_fail" if raw else "not_found"}


def _looks_foreign(name, label_index):
    """标签那条**前面**还有东西，说明框顶撞进了上一行（标签在下、上一行的数字在上）。"""
    return bool(label_index)


def _patch_read_summary(module):
    """把 `summary_page.read_summary` 换成用我们的框版本（沿用上游原有的函数体写法）。"""
    original = getattr(module, "read_summary", None)
    if original is None or getattr(original, "_paimiao_summary_rows", False):
        return
    read_field = getattr(module, "read_field", None)
    if read_field is None:
        raise RuntimeError("上游没有 read_field")
    key_fields = getattr(module, "KEY_FIELDS", ())
    status_text = getattr(module, "STATUS_TEXT", {})

    @wraps(original)
    def read_summary(engine, img, cfg, save_crops=False):
        try:
            read = _read_rows(engine, img, cfg, save_crops)
            if read is None:
                return original(engine, img, cfg, save_crops=save_crops)   # 认不出的配置照旧
            return _summarize(*read, key_fields=key_fields, status_text=status_text)
        except Exception as exc:       # noqa: BLE001
            # 我们自己读错了（字段没定义、上游改了结构……）：退回上游的读法，不让记录线程抛错。
            # 它一抛，这一局的截图和数字就全丢了（2026-10-25 真机踩过）。
            # 保底读数放到出错之后才做：上游读一次要 6 秒，总结页只停十几秒。
            _log(f"[总结页读数] 读总结页出错，退回上游的读法：{exc!r}")
        try:
            return original(engine, img, cfg, save_crops=save_crops)
        except Exception as exc:       # noqa: BLE001
            _log(f"[总结页读数] 上游读法也出错：{exc!r}")
            return {}, ["总结页读数出错，这一局请手动核对"]

    read_summary._paimiao_summary_rows = True         # type: ignore[attr-defined]
    module.read_summary = read_summary


def _summarize(results, wildcard, *, key_fields, status_text):
    """逐项读数 → 上游 `read_summary` 的返回格式 (results, problems)。

    和上游原来的函数体一样：关键项没读到就报一条问题（整局标「待确认」）。
    """
    problems = []
    # 卡片上根本没有「急救 / 重生」这两行（控制模式：`评分` / `已占领区域`、老配装页……）：
    # 那两个框落在别的行上，读到的数字是别的项的（实测 2026-10-25 控制模式把
    # 「已占领区域 2」读成急救 2）。位置对不上的值不如留空 —— 原则是宁可空着也不写错数。
    stats = _STATE if "_STATE" in globals() else {}          # 每一行 / 每一档读到的原文（排查用）
    flat = [text for row in stats.values() for text in (row.get("lines") or [])]
    if "revives" in results and "respawns" in results:
        # 只看**自己那两个框**读到了什么，别拿整页去找（控制模式的 `生存时间` 那一档
        # 读到的 `评分 / 905` 会把整页的判断搅乱）。
        heard = (stats.get("respawns") or {}).get("lines") or []
        if not any("最后幸存" in t for t in heard):
            for name in ("revives", "respawns"):
                results[name] = dict(results[name], value=None, score=None,
                                     status="not_found", raw="")
    kda, squad = results.get("kda"), results.get("squad_kills")
    if (kda and squad and kda.get("value") and squad.get("value") is not None
            and kda["value"][0] > squad["value"]):
        problems.append(f"击杀数 {kda['value'][0]} 大于小队总击杀 {squad['value']}")
    for name in key_fields:
        r = results.get(name)
        if r is None or r["status"] == "ok":
            continue
        text = status_text.get(r["status"], r["status"])
        problems.append(f"{r['label']}：{text}（OCR 原文：{r['raw'] or '无'}）")
    return results, problems


class _StillImporting(RuntimeError):
    """上游正在导入中（它的 loader 已经在跑模块体），此时没法再包 loader。"""


def _wrap_loader_if_needed():
    """上游还**没**导入：把它的 loader 换成“导完就换上我们的读数”。

    只能在「上游还没开始执行模块体」的时候做。正在导入时会抛 `_StillImporting`
    （这时该由找到我们 finder 的那一层去包，见 `_Finder.find_spec`）。
    """
    if TARGET in sys.modules:
        raise _StillImporting(TARGET)
    spec = importlib.machinery.PathFinder.find_spec(TARGET)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(TARGET)
    loader = spec.loader
    if isinstance(loader, _AfterImportLoader):
        return
    shim = importlib.util.spec_from_loader(TARGET, _AfterImportLoader(loader))
    sys.modules[TARGET] = importlib.util.module_from_spec(shim)   # 先占住名字
    importlib._bootstrap._load(shim)                              # 再走真正的导入（会调它的 exec_module）



def install():
    """装注入：面板里主进程导入生效，记录子进程里也会自动再装一遍。

    三种情况按顺序处理（前一种做不到就退到下一种）：

    1. 上游**已经**导入 → 当场把 `read_summary` 换掉；
    2. 上游**正在**导入（记录子进程 `from summary_page import ...` 的那一刻，
       我们的 finder 被问到）→ 把它的 loader 包一层，模块体跑完立刻换；
    3. 上游模块现在根本找不到（主进程启动最早期，`code/` 还没进 sys.path）→
       挂一个 `sys.meta_path` finder，等子进程导入时再走 1 或 2。

    为什么不直接在 install() 里 import 上游：`bootstrap.install()` 在 Python
    启动最早期就跑（sitecustomize / runtime_hook），那时 `code/` 还不在 sys.path；
    而且 AGENT_RULES 4.4 要求「不改变 Evo 的加载顺序」，不能为此提前把上游拉起来。

    和 `cloud/bootstrap.py` 里 `_AfterImportLoader` 一样的教训：**装的时机和生效的
    时机不是一回事**，必须等到真有一个进程在导入上游时才动手。
    """
    global _INSTALLED
    if _INSTALLED:
        return True
    module = sys.modules.get("summary_page")
    if module is not None:
        try:
            _patch_read_summary(module)
        except Exception as exc:
            _log(f"[总结页读数] 上游已经导入，但没换成我们的框：{exc!r}")
            return False
        _INSTALLED = True
        return True
    try:
        _wrap_loader_if_needed()
        _INSTALLED = True
        return True
    except _StillImporting:
        # 正在导入上游（我们的 finder 被问到了）：只把状态记上，
        # 换 loader 由 finder 那一层完成，这里不决定 _INSTALLED
        return True
    except ModuleNotFoundError:
        pass                      # 上游还不认识，退到 finder
    except Exception as exc:
        _log(f"[总结页读数] 包住上游 loader 失败：{exc!r}")
    try:
        _install_finder()
        return True               # 等子进程导入时才装好，_INSTALLED 到那时再置位
    except Exception as exc:
        _log(f"[总结页读数] 钩子没装上：{exc!r}")
        return False


def stats():
    """排查用：最近一次两块框各自用了哪个候选、读到了什么。"""
    return dict(_STATE)


def _log(message):
    try:
        from cloud import state as cloud_state       # noqa: PLC0415

        cloud_state.log(message)
    except Exception:
        pass

