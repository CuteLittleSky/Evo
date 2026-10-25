"""这一局没有读到地图时，按「结束时间 − 队内最长存活 − 1 分钟」回推，再对照地图轮换查出来。

## 用户 2026-10-24 的原话

> 如果结束后没有获取到当前对局的地图信息时，且具有总结页面（有存活时间的），
> 按照结算时间 - 队友最长存活时间得出开局时间，根据游戏模式从
> https://tools.skycraft.cn/apex/api/maprotation?evo=true 判断
> ……只有在启用派喵云同步后才启用此功能

## 两个来源，开局横幅优先（用户 2026-10-24 定）

1. **有开局横幅** → 直接用横幅那一刻（`ScreenWatcher` 第一次认出 `match_start` 的时刻）。
   这是**准确值**，不推断、不查边界，直接拿它去查轮换。
2. **没有开局横幅** → 按下面的公式回推。
3. **两个都没有** → 留空。

`started_at` 上游建了列但一直没写（`code/storage.py:26` 注释「等状态机做好才有」），
这里用注入补上：包 `PostMatch.on_match_start`，第一次调用时把 `when` 记进 `started_at`。

## 公式（用户 2026-10-24 补充确认）

    开局时刻 = 结束时间 − 队内最长存活时间 − 1 分钟

- 队内最长存活一般来自**最后一个死的队友**，所以他死亡的时刻约等于小队团灭的时刻
- 再减 1 分钟是留给「结算画面出现到真正记下结束时间」这段延迟的余量
- 存活时间取自总结页（`code/summary_page.py` 读自己，`code/teammates.py` 读队友）

为了不把这一局推错，实际上会拿**几个候选时刻**都去查一遍，全部算出同一张图才写：

    结束 − 最长存活 − 60           ← 用户给的公式，最贴近真实开局
    结束 − 最长存活 − 60 − 自己存活
    结束 − 自己存活 − 60           ← 自己先死时，自己死亡时刻更准

三个时刻一般落在同一个 90 分钟窗口里，结果相同；**只要有一个落到别的图上就不填**
（说明这一局跨了轮换边界，或者存活时间没读全）。

## 什么情况不推（用户 2026-10-24 选定）

1. **上游已经读到地图** —— 永远以上游为准，这里一个字都不改
2. **读不到任何开局时刻** —— 既没有开局横幅，也没有总结页的存活时间
3. **不认识这一局的模式** —— 不知道该查哪一组地图
4. **推出来的地图对不上已知地图名单** —— 接口给的 code 在 config 里找不到，不猜
5. **候选时刻跨了轮换边界** —— 见上
6. **没开派喵云同步** —— 不发请求（`map_lookup.enabled()`）
7. **排位分 RP 有变化** —— 说明这是排位赛（`cloud` 侧 2026-10-24 的规则）：
   RP 有变化时按 ranked 组查，不按大厅读到的模式查

## 接在哪

**只接 `PostMatch.on_summary_saved` 一个地方算地图**。为什么不接 `storage.insert_match`：

    总结页那条路（code/live_collect.py:157-176）是
        summary_to_match() → insert_match() → save_teammates() → on_summary_saved()
    也就是说到了 on_summary_saved 这里，**队友的存活时间已经进库、mode 也在 self.info 里**。
    而在 insert_match 里做时队友还没存、mode 也拿不到，只能用自己的存活时间按普通匹配猜
    —— 2026-10-24 实测过：13:29 结束的一局会被猜成「奥林匹斯」，而加上队友后
    两个候选落在不同地图上，正确的答案是「不填」。宁可留空，不写错图。

上游另外三处写库（`_insert_elim_only` / `_insert_hud_only` / `_insert_xp_only`）
都**没有存活时间**（那条路根本读不到总结页），公式用不起来，所以也不接。
"""
from __future__ import annotations

import time
from typing import Any

# 用户定的余量：结算画面出现到记下结束时间之间的延迟（秒）
LAG_SECONDS = 60

# 现在的这一局：{match_id, ended_at(Unix 秒), started(Unix 秒或 None), own, mates: [秒, ...]}
_STATE: dict[str, Any] = {}

# 最近一次从开局横幅拿到的时刻（Unix 秒）。横幅出现在建局之前，所以先单独存着。
_STARTED: dict[str, Any] = {}

_INSTALLED = False


def reset():
    _STATE.clear()
    _STARTED.clear()


def observe(match_id, ended_at, own, mates):
    """总结页存下这一局时调用（`on_summary_saved` 的包装里）。

    own：自己的存活时间（秒）
    mates：队友的存活时间（秒，读不到的为 None）
    """
    _STATE.clear()
    _STATE.update(
        match_id=match_id,
        ended_at=int(ended_at),
        started=_STARTED.get("at"),
        own=_to_int(own),
        mates=[v for v in (_to_int(m) for m in (mates or [])) if v],
    )


def candidates():
    """候选开局时刻（Unix 秒），按「最贴近真实开局」排序。

    **有开局横幅时只用横幅那一刻**（准确值，不需要也不该再加候选）；
    没有横幅时才走「结束 − 队内最长存活 − 1 分钟」那套。都没有返回空列表。
    """
    started = _STATE.get("started")
    if started:
        return [(int(started), "开局横幅读到的时刻")]
    ended = _STATE.get("ended_at")
    if not ended:
        return []
    lengths = list(_STATE.get("mates") or [])
    own = _STATE.get("own")
    longest = max(lengths) if lengths else own
    if not longest:
        return []
    spans = []
    base = ended - longest - LAG_SECONDS
    spans.append((base, "结束 − 队内最长存活 %d 秒 − 60 秒" % longest))
    if own and own != longest:
        spans.append((base - own, "再减去自己的存活 %d 秒" % own))
        spans.append((ended - own - LAG_SECONDS, "结束 − 自己存活 %d 秒 − 60 秒" % own))
    return spans


def guess(mode=None, rp_changed=False):
    """按当前记下的这一局推地图。成功返回 {"map", "code", "when", "why"}，否则抛异常。

    mode：大厅/开局画面读到的模式
    rp_changed：这一局 RP 有没有变化（有变化 = 铁定排位赛）
    """
    from cloud.lobby_mode import map_lookup  # noqa: PLC0415

    if not map_lookup.enabled():
        raise map_lookup.Unavailable("没有启用派喵云同步，不联网查地图")
    spans = candidates()
    if not spans:
        raise map_lookup.Unavailable("没有存活时间，推不出开局时刻")
    # 用户 2026-10-24：未上榜就是普通匹配（battle_royale）。
    # RP 有变化时直接按排位赛查 —— 比大厅卡片的读数更硬。
    use = "ranked" if rp_changed else (mode or "pubs")
    if not map_lookup.group_of(use):
        raise map_lookup.Unavailable("不认识这一局的模式（%r），不猜" % (mode,))
    return map_lookup.resolve(use, spans)


def install(live_collect_module):
    """把上面这套接到上游。幂等；任何一步拿不到就静默降级。"""
    global _INSTALLED
    if _INSTALLED:
        return
    _install_started_hook()
    _install_summary_hook()
    _INSTALLED = True


def _install_started_hook():
    """包 PostMatch.on_match_start：第一次看到开局横幅时，记下这一刻。

    上游 `code/storage.py:26` 建了 started_at 列却一直没写（注释「等状态机做好才有」）。
    这里补上：用户 2026-10-24 定「有开局横幅就按横幅来」，所以横幅时刻是最准的开局时间。

    只记第一次：横幅可能连着好几帧都在，第一帧就是真正进入跳伞的时刻。
    on_match_start 在 `PostMatch.info` 已有 mode 时也会被调（上游不判重），
    所以这里自己判 `_STARTED` 是否已有值，不重复记。
    """
    from post_match import PostMatch  # noqa: PLC0415

    original = PostMatch.on_match_start
    if getattr(original, "_paimiao_map_guess", False):
        return

    def on_match_start(self, reading, when):
        try:
            at = when.timestamp() if hasattr(when, "timestamp") else None
            if at and not _STARTED.get("at"):
                _STARTED["at"] = int(at)
                _STARTED["match_id"] = getattr(self, "match_id", None)
                # 这一局要是已经建好了（少数情况），直接把 started_at 补进库
                match_id = getattr(self, "match_id", None)
                if match_id is not None:
                    import storage  # noqa: PLC0415

                    row = storage.get_match(self.conn, match_id)
                    if row is not None and not row["started_at"]:
                        storage.update_match(
                            self.conn, match_id,
                            {"started_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                                         time.localtime(at))})
                        _log("第 %s 局开局时刻：%s（开局横幅）"
                             % (match_id, time.strftime("%H:%M:%S", time.localtime(at))))
        except Exception:
            pass        # 记不下就算了：地图那一侧会退回用存活时间推
        return original(self, reading, when)

    on_match_start._paimiao_map_guess = True    # type: ignore[attr-defined]
    PostMatch.on_match_start = on_match_start


def _install_summary_hook():
    """包 PostMatch.on_summary_saved：这是**主要**的推地图时机。

    为什么选这里（2026-10-24 定的）：
    总结页那条路在 code/live_collect.py:157-176 里是
        summary_to_match() → storage.insert_match() → save_teammates() → post.on_summary_saved()
    也就是说到了 on_summary_saved 这里，**队友的存活时间已经进库了**，而 mode 在 self.info 里。
    反过来在 insert_match 里做，队友还没存、也拿不到 mode（PostMatch.info 那时刚写进字段），
    只能用自己的存活时间按普通匹配猜 —— 排位局会被猜成大逃杀图、外卡局会瞎猜。
    所以真正算在 on_summary_saved（先算、再调原函数，原函数会用 self.info 覆盖 mode，
    但不覆盖 map，所以 map 写进去就是我们的）。
    """
    from post_match import PostMatch  # noqa: PLC0415

    original = PostMatch.on_summary_saved
    if getattr(original, "_paimiao_map_guess", False):
        return

    def on_summary_saved(self, match_id, values, when, player_name=None):
        try:
            _observe_from(self, match_id, values, when)
            _fill_saved(self, match_id)
        except Exception:
            pass        # 推不出来就算了：这一局地图留空，绝不影响记录
        return original(self, match_id, values, when, player_name)

    on_summary_saved._paimiao_map_guess = True      # type: ignore[attr-defined]
    PostMatch.on_summary_saved = on_summary_saved

    finish = PostMatch.finish
    if not getattr(finish, "_paimiao_map_guess", False):

        def finish_wrapper(self):
            try:
                return finish(self)
            finally:
                reset()     # 一局收尾：不管推没推，都不许带到下一局

        finish_wrapper._paimiao_map_guess = True    # type: ignore[attr-defined]
        PostMatch.finish = finish_wrapper


def _fill_saved(post, match_id=None):
    """已经进库的这一局没有地图时，补一个。

    match_id 用参数传进来：包装里的 self.match_id 那时还没设（原函数里才设）。
    """
    import storage  # noqa: PLC0415

    if match_id is None:
        match_id = getattr(post, "match_id", None)
    if match_id is None:
        return
    row = storage.get_match(post.conn, match_id)
    if row is None or row["map"]:
        return                   # 上游自己读到了，不动
    mode = (getattr(post, "info", None) or {}).get("mode")
    rp_changed = row["rp_delta"] not in (None, 0)
    try:
        found = guess(mode=mode, rp_changed=rp_changed)
    except Exception as exc:
        _log("第 %s 局没读到地图，按时间推：%s" % (match_id, exc))
        return
    storage.update_match(post.conn, match_id, {"map": found["map"]})
    _log("第 %s 局地图推出来：%s（%s → %s）"
         % (match_id, found["map"], found["why"],
            time.strftime("%H:%M:%S", time.localtime(found["when"]))))


def _observe_from(post, match_id, values, when):
    """从总结页的读数和数据库里这一局的队友，凑出候选时刻要用的存活时间。"""
    import storage  # noqa: PLC0415

    # 总结页给的是 "06:03" 这种字符串，要转成秒；库里存的是秒。
    own = _to_seconds((values or {}).get("survival_time"))
    if own is None:
        try:
            own = storage.get_match(post.conn, match_id)["survival_time"]
        except Exception:
            pass
    mates = []
    try:
        for row in storage.teammates_of(post.conn, match_id):
            mates.append(row["survival_time"])
    except Exception:
        pass
    ended = when.timestamp() if hasattr(when, "timestamp") else None
    if ended is None:
        return
    observe(match_id, ended, own, mates)


def _to_seconds(value):
    """总结页的 "06:03" → 363；已经是数字就原样；认不出返回 None。

    上游 `code/summary_page.py` 的 to_seconds 就是从这格式转的，
    这里不 import 它（那个函数在 main 里才用），自己转一遍更省事。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) != 2:
        return _to_int(text)
    try:
        minutes, seconds = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return minutes * 60 + seconds



def _to_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _log(message):
    try:
        from cloud import state as cloud_state  # noqa: PLC0415

        cloud_state.log("[地图回推] %s" % message)
        return
    except Exception:
        pass
    try:
        print("[地图回推] %s" % message)
    except Exception:
        pass
