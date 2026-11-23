"""修「队友名字读不出来」（2026-11-24，用户报「这把为什么显示没有队友的数据」时查出来的）。

## 问题

队友名字的检查区域（`config/summary.json` 的 `teammates.name.roi` = `[748, 300, 222, 34]`，
`config/hud.json` 的 `teammates.slots[*].name` 也是 28 高）**高度裁得太紧**：
像 `pu_ku_pu_ku_10` 这种带下划线的名字，文字底部（下划线和字母的下降部）刚好被切掉一点点，
RapidOCR 的检测网络就完全不出字（`txts: None`，不是分数低，是压根检测不到）。

实测（2026-11-24 用户第 51 局的总结页截图 `captures/live/20261124_080052/summary/02_081137.png`）：

| 区域 | OCR |
|---|---|
| 原 ROI（34 高） | 检测不到任何文字 |
| **往下加高 12 像素**（46 高） | `pu_ku_pu_` 0.998、`u_ku_10` 0.997 |
| 原 ROI，右侧队友 `qylwl` | 检测不到 |
| 往下加高 12 像素 | `qylwl` 0.992 |

对局中左下角那两行（`hud.json`，28 高）同样：原样读不出，加高后能读。

后果（用户那局）：队友名字一个都没读到 → `in_match._vote_mates` 攒不满 3 个名字 →
`post_match._merge_mates` 拿不到名字去和总结页对 → **整条队友数据（含已经认对的传奇）都不写库**，
面板上就是「没有队友的数据」。

## 做法（纯注入，`config/` 和 `code/` 一个字节都不动）

四层，都是运行时改内存里的配置字典 / 包函数：

1. **把检查区域加高**：`summary.json` 的 `teammates.name.roi`（34→46）、`hud.json` 的
   `teammates.slots[*].name`（28→48）。上游 `ocr.crop` 按基准坐标缩放裁剪，多出来的只是几像素空白。
2. **把名字门槛降到 0.8**：上游 `hud.read_name` 的默认 `min_score=0.9` 太严——加高后名字分数落在
   0.79–0.85（实测 0.9 时读出 4/10 帧，0.8 时 8/10）。总结页那边也把 `min_score` 降到 0.8。
3. **修取词 `pick_name`**：上游盲取「最左边那段」。加高后底部会带出 `↓` / `ii` / `iji` 这类杂字，
   它们比真名字更靠左、分数还可能更高（实测 `↓` 0.96 > 真名字 0.86），被当成名字又被门槛挡掉。
   改成：只在「含字母/数字、分数达标」的段里挑 left 最靠左的当名字起点；同一串里允许小重叠
   （OCR 常把 `pu_ku_pu_ku_10` 断成两段且边界重叠几像素）。
4. **总结页兜底**：包 `post_match._merge_mates`，跑完上游以后按**位置**（hud1/hud2 ↔ left/right）
   把对局中认出、但没写进库的**传奇**补上；名字也谁读到用谁。

`load_config` 每次返回新字典（`ocr.py` 里是 `json.loads`），所以直接改返回值即可，不用碰缓存。

降级：任何一步失败只写日志，照上游行为走（队友名字可能读不出，但不影响别的识别）。
"""
from __future__ import annotations

MARK = "_paimiao_mate_name"
_INSTALLED = False
_said = set()

PAD_SUMMARY = 12  # 总结页的名字区域往下多留的像素（34 → 46）：实测这样下划线就留进来了
PAD_HUD = 20      # 对局中左下角那两行（28 高的区域 → 48）：够把文字底部留进来
MIN_SCORE = 0.8   # 名字的置信度门槛：上游 0.9 太严，实测加高后名字分数落在 0.79–0.85（0.9 时读出 4/10，0.8 时 8/10）


def _log(message):
    if message in _said:
        return
    _said.add(message)
    try:
        from cloud.state import log
        log(f"[队友名字] {message}")
    except Exception:
        pass


def _widen(cfg):
    """把一个配置字典里队友名字的区域往下加高。改到就返回改了几处。"""
    changed = 0
    mates = cfg.get("teammates") if isinstance(cfg, dict) else None
    if not isinstance(mates, dict):
        return 0
    # summary.json： teammates.name.roi（34 高 → 46），并把名字门槛降到 MIN_SCORE（teammates.py 用它）
    name = mates.get("name")
    if isinstance(name, dict) and isinstance(name.get("roi"), list) and len(name["roi"]) == 4:
        name["roi"] = [name["roi"][0], name["roi"][1], name["roi"][2], name["roi"][3] + PAD_SUMMARY]
        if "min_score" not in name or name.get("min_score", 0) > MIN_SCORE:
            name["min_score"] = MIN_SCORE
        changed += 1
    # 总结页整体的 min_score（teammates.read_teammates 可能读 cfg["min_score"]）
    if isinstance(cfg.get("min_score"), (int, float)) and cfg["min_score"] > MIN_SCORE:
        cfg["min_score"] = MIN_SCORE
        changed += 1
    # hud.json：teammates.slots[i].name（28 高 → 56）
    for slot in mates.get("slots") or []:
        if isinstance(slot, dict) and isinstance(slot.get("name"), list) and len(slot["name"]) == 4:
            slot["name"] = [slot["name"][0], slot["name"][1], slot["name"][2], slot["name"][3] + PAD_HUD]
            changed += 1
    return changed


def patch_loader(module):
    """包 config 的 load_config：返回的字典里把队友名字区域加高。幂等。"""
    real = getattr(module, "load_config", None)
    if real is None:
        _log("找不到 load_config，跳过（照上游行为）")
        return False
    if getattr(real, MARK, False):
        return True

    def load_config(name, lang=None, *args, **kwargs):
        cfg = real(name, lang, *args, **kwargs) if lang is not None else real(name, *args, **kwargs)
        try:
            if _widen(cfg):
                pass
        except Exception as exc:  # noqa: BLE001
            _log(f"加高 {name} 里的队友名字区域出错（用原样）：{exc!r}")
        return cfg

    setattr(load_config, MARK, True)
    load_config.__wrapped__ = real  # type: ignore[attr-defined]
    module.load_config = load_config
    _log(f"已接管：队友名字区域加高（总结页 +{PAD_SUMMARY}、对局中 +{PAD_HUD}）（总结页和对局中都适用）")
    return True


def patch_merge(module):
    """总结页兜底：对局中没读到名字的队友，按位置用总结页的名字。幂等。"""
    cls = getattr(module, "PostMatch", None)
    real = getattr(cls, "_merge_mates", None)
    if real is None:
        _log("找不到 PostMatch._merge_mates，跳过兜底（照上游行为）")
        return False
    if getattr(real, MARK, False):
        return True

    def _merge_mates(self, match_id):
        # 先照上游走一遍（它会把对上的写好、对不上的记日志）
        real(self, match_id)
        try:
            _fill_from_summary(self, match_id)
        except Exception as exc:  # noqa: BLE001
            _log(f"总结页兜底出错（不影响已经写好的）：{exc!r}")

    setattr(_merge_mates, MARK, True)
    _merge_mates.__wrapped__ = real  # type: ignore[attr-defined]
    cls._merge_mates = _merge_mates
    _log("已接管：对局中没读到队友名字时，用总结页的名字兜底")
    return True


def _fill_from_summary(self, match_id):
    """总结页兜底：对局中没读到名字、总结页也没读到名字时，什么都不做；
    对局中读到了名字但总结页没读到，把对局中的名字补进总结页那两行（left/right）。

    真实的数据形状（2026-11-24 查用户库）：有总结页的局，`teammates` 里是 `left` / `right` 两行，
    名字来自总结页（`teammates.read_teammates`）；对局中认出的传奇由上游按名字对上去。
    所以「对局中有名字、总结页没有」时，补名字的收益和「总结页有名字、对局中没名字」是一样的：
    两边谁读到了就用谁。上游只按名字对上才补，这里补上按位置的兜底。"""
    import storage  # noqa: PLC0415

    rows = storage.teammates_of(self.conn, match_id)
    if not rows:
        return
    by_slot = {row["slot"]: row for row in rows}
    summary_rows = [by_slot[s] for s in ("left", "right") if s in by_slot]
    if not summary_rows:
        return
    hud_mates = list(self.hud.get("mates") or []) if self.hud else []
    if not hud_mates:
        return
    # 对局中的 hud1/hud2 按从左到右对到总结页的 left/right
    for i, row in enumerate(summary_rows):
        if i >= len(hud_mates):
            break
        mate = hud_mates[i] or {}
        fields = {}
        # 名字：总结页没读到、对局中读到了 → 用对局中的
        if not row["name"] and mate.get("name"):
            fields["name"] = mate["name"]
        # 传奇：这一行还没有、对局中认出来了 → 补上（这是「队友传奇丢了」的主要修复）
        if row["legend"] is None and mate.get("legend"):
            fields["legend"] = mate["legend"]
        if not fields:
            continue
        storage.update_teammate(self.conn, match_id, row["slot"], fields)
        try:
            what = "、".join(f"{k}={v}" for k, v in fields.items())
            self.log(f"           队友 {row['slot']}：按位置从对局中补上 {what}")
        except Exception:
            pass


def patch_hud_read_name(module):
    """两处：模块级 `read_name` 的默认 `min_score` 降到 MIN_SCORE；`Hud.read_name` 方法显式传 MIN_SCORE
    （方法体是 `return read_name(self.engine, snip, left_range)`，不传就用默认参数 —— 而默认参数在
    函数定义时就固定成 0.9 了，光改默认值不够，得让方法自己带上）。
    同时修 left_range 的检查：上游拿「所有段里最左那段」，可能是杂字（↓），改成拿「挑出来的名字那段」。"""
    import player_name as PN  # noqa: PLC0415
    import storage  # noqa: PLC0415

    real_module_fn = getattr(module, "read_name", None)
    cls = getattr(module, "Hud", None)
    real_method = getattr(cls, "read_name", None) if cls else None
    if real_module_fn is None or real_method is None:
        _log("找不到 hud.read_name，跳过（照上游行为）")
        return False
    if getattr(real_method, MARK, False):
        return True

    pick = PN.pick_name
    name_key = storage.name_key

    def read_name(engine, snip, left_range=None, min_score=MIN_SCORE, length=(2, 32)):
        result = engine(snip.crop(snip.box, 2))
        if not result.txts:
            return None
        found = pick(result.boxes, result.txts, result.scores, min_score)
        if found is None or not length[0] <= len(name_key(found[0])) <= length[1]:
            return None
        if left_range is not None:
            # 上游用「所有段里最左那段」的 left 做检查；但它可能是杂字（↓，left 比名字更靠左）。
            # 改成用「挑出来的名字那段」的 left：在所有段里找和名字文字一致、且最靠左的那段。
            name_text = found[0]
            cand = [(b, t) for b, t in zip(result.boxes, result.txts)
                    if str(t).strip() and str(t).strip() in name_text]
            if cand:
                left = min(float(b[:, 0].min()) for b, _t in cand) / 2 + snip.box[0]
            else:
                left = min(float(b[:, 0].min()) for b in result.boxes) / 2 + snip.box[0]
            if not left_range[0] <= left <= left_range[1]:
                return None
        return found[0]

    def method(self, snip, left_range=None):
        return read_name(self.engine, snip, left_range=left_range, min_score=MIN_SCORE) 

    setattr(read_name, MARK, True)
    read_name.__wrapped__ = real_module_fn  # type: ignore[attr-defined]
    module.read_name = read_name

    setattr(method, MARK, True)
    method.__wrapped__ = real_method  # type: ignore[attr-defined]
    cls.read_name = method
    _log(f"已接管：读名字的门槛 0.9 → {MIN_SCORE}")
    return True


def patch_pick_name(module):
    """修 `player_name.pick_name` 的取词：原来盲取「最左边那段」，加高区域后底部会多出
    `↓` / `ii` / `iji` 这类杂字（left=116，贴着区域左边，分数 0.5–0.9），被当成名字、
    又被 min_score 挡掉 → 整个读不出来。

    实测这些杂字有个共同特征：**left 比真名字更靠左**（真名字 left 123–124，杂字 left 116–117），
    而且分数更低。改成：在所有段里挑「分数达标的、left 最靠左的」那段当名字起点，
    杂字因为分数不够自然被跳过。名字后面紧挨着的段照上游的规矩接上。"""
    real = getattr(module, "pick_name", None)
    if real is None:
        _log("找不到 pick_name，跳过（照上游行为）")
        return False
    if getattr(real, MARK, False):
        return True

    def pick_name(boxes, txts, scores, min_score):
        import re  # noqa: PLC0415
        parts = list(zip(boxes, txts, scores))
        # 名字一定是「有字母或数字」的一段；箭头 / 图标（↓、ii）会被读成符号，靠这条先剔掉，
        # 否则它们分数可能更高（实测 ↓ 0.96 比真名字 0.86 还高），会被盲取成名字
        def looks_like_name(text):
            t = str(text).strip()
            return bool(re.search(r"[A-Za-z0-9\u4e00-\u9fff]", t)) and any(c.isalnum() for c in t)
        good = [p for p in parts if p[2] >= min_score and looks_like_name(p[1])]
        if not good:
            return None
        box, text, score = min(good, key=lambda p: p[0][:, 0].min())
        pieces, right, height = [text], box[:, 0].max(), box[:, 1].max() - box[:, 1].min()
        for b2, t2, s2 in sorted(parts, key=lambda p: p[0][:, 0].min()):
            if b2 is box:
                continue
            if b2[:, 0].min() < right - height:   # 明显在左边（隔了超过一个字高）：跳过；小重叠是同一串
                continue
            if b2[:, 0].min() - right > height or s2 < min_score or not looks_like_name(t2):
                break                        # 隔了超过一个字高、或不像名字的，不接
            pieces.append(t2)
            right, score = max(right, b2[:, 0].max()), min(score, s2)
        return " ".join(pieces).strip(), score

    setattr(pick_name, MARK, True)
    pick_name.__wrapped__ = real  # type: ignore[attr-defined]
    module.pick_name = pick_name
    _log("已接管：队友名字的取词跳过左侧杂字（按分数筛最靠左的那段）")
    return True


def install():
    """等 ocr / player_name / post_match 导入后挂上。幂等。"""
    global _INSTALLED
    if _INSTALLED:
        return False
    _INSTALLED = True
    try:
        from cloud import importhook
        importhook.on_import("ocr", patch_loader, "队友名字区域加高")
        importhook.on_import("player_name", patch_pick_name, "队友名字取词")
        importhook.on_import("hud", patch_hud_read_name, "队友名字门槛")
        importhook.on_import("post_match", patch_merge, "队友名字兜底")
    except Exception as exc:  # noqa: BLE001
        _INSTALLED = False
        _log(f"挂不上（照上游行为）：{exc!r}")
        return False
    return True


