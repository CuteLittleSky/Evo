"""繁体中文游戏画面（config/lang/zh-TW.json）：用用户 2026-10-24 截的繁体截图（captures/frames/繁体，2560x1440、游戏 16:10 拉伸，
匹配三排两局 + 排位一局，每秒一张，共 872 张）整段喂给记录程序，看它认不认得出繁体、三局记得对不对；再单独读几张。

- 认语言：开游戏后第一个画面（匹配的大厅"普通對戰"）就认定是繁体
- 匹配两局：开局横幅"三排"记成匹配、奧林匹斯 → 奥林匹斯；第 17 名（0/0/0、伤害 33）、第 13 名（1/0/2、伤害 807）
- 排位局："積分賽"、殘月 → 残月；第 16 名、0/0/1、伤害 381、RP −48（经验值页，和对局中一致）、白金 II 258
  （"下個排名：白金1"的"1"常被漏掉，只框住那一行才稳；漏掉时不猜，见 xp_page.current_rank）
- 大厅：排位时读出白金 II 306（"白金Ⅱ""306/1000RP"）；匹配时左下角没有段位，写一行就不读
- 单张：经验值页、淘汰画面"排名#17"、观战标签"觀戰"；桌面上录像软件黑色的播放器不会被当成淘汰画面（screens.match）

队友的名字是别的玩家的，这里不写。截图不在仓库里，找不到时跳过（见 fake_game.py）。

运行：python tests/test_traditional.py
"""

from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fake_game import BLACK, MissingShots, SHOTS, load, report, run_collect  # 要在项目代码之前导入

import fake_game
import layout
import panel_app
import ranks
import storage
import xp_page
from eliminated import read_eliminated
from hud import Hud
from lobby import LobbyReader, LobbyWatcher
from ocr import SMALL_CROP_PARAMS, LazyEngine, load_config
from post_match import PostMatch, rank_text
from screens import GameScreens, ScreenDetector
from xp_page import current_rank, _read_progress_by_layout, read_xp

FOLDER = SHOTS / "frames" / "繁体"
COUNT = 872
FIELDS = ("mode", "map", "legend", "placement", "squad_kills", "kills", "assists", "knockdowns", "damage", "survival_time",
          "revives", "respawns", "rp_delta", "rp_source", "rank_tier", "rank_division", "rank_progress", "needs_review")
WANT = [
    dict(zip(FIELDS, ("pubs", "奥林匹斯", "罗芭", 17, 0, 0, 0, 0, 33, 80, 0, 0, None, None, None, None, None, 0))),
    dict(zip(FIELDS, ("pubs", "奥林匹斯", "罗芭", 13, 3, 1, 0, 2, 807, 178, 0, 0, None, None, None, None, None, 0))),
    dict(zip(FIELDS, ("ranked", "残月", "罗芭", 16, 0, 0, 0, 1, 381, 181, 0, 0, -48, "经验值页", "白金", "II", 258, 0))),
]


def rule_checks():
    """不用截图也验证语言覆盖、旧位置回退和缺失分数的诊断。"""
    cfg = load_config("xp.json", "zh-TW")
    spec = cfg["fields"]["progress"]
    def read(next_text, values):
        with mock.patch("xp_page.read_text", side_effect=values):
            return _read_progress_by_layout(None, None, spec, cfg, next_text)
    def selected(next_text, values):
        with mock.patch("xp_page.read_text", side_effect=values) as reader:
            value = _read_progress_by_layout(None, None, spec, cfg, next_text)
        return value, [call.args[2]["roi"] for call in reader.call_args_list]
    known = {"rp_shown": "74", "combat_total": "70 RP", "bonus_total": "0 RP", "placement_total": "4 RP",
             "kills": "1 70 RP", "assists": "0 0 RP", "placement": "#9 24 RP", "entry_fee": "-20 RP",
             "next_rank": "白銀Ⅱ"}
    with mock.patch("xp_page.read_text") as reader:
        incomplete, problems = read_xp(None, None, cfg, known)
    conn = storage.connect(":memory:")
    post = PostMatch(conn, log=lambda message: None)
    when = datetime(2026, 11, 13, 21, 22)
    post.on_xp(incomplete, problems, Path("xp.png"), when)
    post.finish()
    post.on_xp(incomplete, problems, Path("xp.png"), when + timedelta(minutes=1))
    post.finish()
    rows = [tuple(row) for row in conn.execute("SELECT rank_tier, rank_division, rank_progress, rp_delta, placement FROM matches")]
    conn.close()
    names = ranks.tier_names("zh-TW")
    return {
        "新旧布局分别只读对应的段内分数区域":
            spec["roi"] == [838, 322, 125, 26] and spec["anchor"] == "top"
            and spec["also"][0]["roi"] == [860, 290, 240, 30]
            and selected("64 RP 白銀Ⅱ", [("436 RP", 0.99)]) == ("436 RP", [[838, 322, 125, 26]])
            and selected("下個排名:白金1", [("258 RP", 0.99)]) == ("258 RP", [[860, 290, 240, 30]]),
        "布局无法确定时不猜；分数无效或置信度低时不跨布局回退":
            selected("白銀Ⅱ", []) == ("", []) and read("64 RP 白銀Ⅱ", [("123", 0.99)]) == ""
            and read("下個排名:白金1", [("258 RP", 0.1)]) == "",
        "新界面的真实零分可保存；旧界面不读取新位置可能出现的起点零分":
            read("750 RP 白銀Ⅱ", [("0 RP", 0.99)]) == "0 RP"
            and selected("下個排名:白金1", [("258 RP", 0.99)]) == ("258 RP", [[860, 290, 240, 30]]),
        "布局不明时分数留空，但已核对的 RP 和名次保留":
            (incomplete["rank_tier"], incomplete["rank_progress"], incomplete["rp_delta"], incomplete["placement"], problems)
            == ("白银", None, 74, 9, []) and not reader.called,
        "分数留空时 RP、名次仍入库，重新打开经验值页不重复记局": rows == [("白银", "III", None, 74, 9)],
        "繁体新旧下一段位和罗马数字": current_rank("64 RP ▶ 白銀Ⅱ", names) == ("白银", "III")
            and current_rank("下個排名:白金1", names) == ("白金", "II"),
        "缺少段内分数的日志明确指出原因并保留原文":
            rank_text({"rank_tier": "白银", "rank_division": "III", "rank_progress": None,
                       "raw": {"next_rank": "64 RP 白銀Ⅱ", "progress": ""}})
            == "白银 III（段内进度没读到；下一段位'64 RP 白銀Ⅱ'）",
        "完整分数和零分正常记录": rank_text({"rank_tier": "白银", "rank_division": "III", "rank_progress": 0}) == "白银 III 0",
    }


def status_checks():
    """真实调用入口：截图缺失时跳过，规则或截图失败时仍为失败。"""
    out = []
    for rules, season in (({"规则": True}, MissingShots("新截图")),
                          ({"规则": True}, {"新截图": True}),
                          ({"规则": True}, {"新截图": False}),
                          ({"规则": False}, MissingShots("新截图"))):
        with mock.patch.dict(globals(), {"rule_checks": lambda: dict(rules), "status_checks": lambda: {}, "retry_checks": lambda: {},
                                        "season31_checks": mock.Mock(side_effect=season) if isinstance(season, Exception)
                                        else lambda: dict(season),
                                        "frames": mock.Mock(side_effect=MissingShots("旧截图"))}), redirect_stdout(StringIO()):
            out.append(main())
    return {"截图缺失报告跳过，检查失败不被跳过掩盖": out == [None, None, False, False]}


def retry_checks():
    """假时钟、模拟 OCR 和临时数据库：先保存 RP，再在同一次停留补全段位。"""
    cfg = load_config("xp.json", "zh-TW")
    known = {"rp_shown": "74", "combat_total": "70 RP", "bonus_total": "0 RP", "placement_total": "4 RP",
             "kills": "1 70 RP", "assists": "0 0 RP", "placement": "#9 24 RP", "entry_fee": "-20 RP",
             "next_rank": "白銀Ⅱ"}
    incomplete, problems = read_xp(None, None, cfg, known)
    full = {**incomplete, "rank_progress": 436, "raw": {**incomplete["raw"], "next_rank": "64 RP 白銀Ⅱ", "progress": "436 RP"}}
    when = datetime(2026, 11, 13, 21, 22)

    def make_post(with_summary=True):
        conn = storage.connect(":memory:")
        post = PostMatch(conn, log=lambda message: None)
        if with_summary:
            post.match_id = storage.insert_match(conn, {"ended_at": "2026-11-13 21:22:00"})
            post.summary = ({"placement": 9, "kda": (1, 0, 0)}, when)
        return conn, post

    def rows(conn):
        return [tuple(row) for row in conn.execute("SELECT rank_tier, rank_division, rank_progress, rp_delta, placement FROM matches")]

    conn, post = make_post()
    with TemporaryDirectory() as folder:
        watcher = fake_game.live_collect.ScreenWatcher(None, load_config("screens.json", "zh-TW"), cfg, {}, {}, post,
                                                       Path(folder), lambda message: None, keep_all=False)
        ticks = iter([0, .31, 1.0, 2.0])
        with mock.patch.object(fake_game.live_collect, "time", mock.Mock(monotonic=lambda: next(ticks))), \
             mock.patch.object(fake_game.live_collect, "numbers_snapshot", return_value=None), \
             mock.patch.object(fake_game.live_collect, "numbers_moved", return_value=False), \
             mock.patch.object(fake_game.live_collect, "read_rp", return_value=(74, {})), \
             mock.patch.object(fake_game.live_collect, "read_xp", side_effect=[(incomplete, problems), (full, [])]) as reads, \
             mock.patch.object(fake_game.live_collect, "save_png"):
            watcher._watch_xp(BLACK, when)
            watcher._watch_xp(BLACK, when)
            before = rows(conn)
            pending = not watcher.xp_full
            watcher._watch_xp(BLACK, when + timedelta(seconds=1))
            after = rows(conn)
            watcher._watch_xp(BLACK, when + timedelta(seconds=2))
        bounded_after_success = watcher.xp_full and reads.call_count == 2
    sources = [(r["field"], r["value"]) for r in storage.readings_of(conn, post.match_id) if r["field"] == "rank_progress"]
    post.finish()
    post.on_xp(full, [], Path("xp.png"), when + timedelta(minutes=1))
    post.finish()
    no_duplicate = len(rows(conn)) == 1
    conn.close()

    conn, post = make_post(False)
    post.on_xp(incomplete, [], Path("xp.png"), when)
    post.on_xp(full, [], Path("xp.png"), when + timedelta(seconds=1))
    post.finish()
    no_summary = rows(conn)
    conn.close()

    unconfirmed_other = []
    for later in (full, incomplete):
        conn, post = make_post(False)
        post.on_xp({**incomplete, "kills": None}, [], Path("xp.png"), when)
        post.on_xp({**later, "rp_delta": 75}, [], Path("xp2.png"), when + timedelta(seconds=1))
        post.finish()
        unconfirmed_other.append(rows(conn))
        conn.close()

    rejected = []
    for candidate, bad_problems, edit in (({**full, "rp_delta": 75}, [], None),
                                          ({**full, "placement": 8}, [], None),
                                          ({**full, "rank_tier": "黄金"}, [], None),
                                          (full, ["RP 对不上"], None),
                                          (full, [], {"rank_progress": 123, "edited": "rank_progress"}),
                                          (full, [], {"rank_progress": 456})):
        conn, post = make_post()
        post.on_xp(incomplete, [], Path("xp.png"), when)
        if edit:
            storage.update_match(conn, post.match_id, edit)
        post.on_xp(candidate, bad_problems, Path("xp2.png"), when + timedelta(seconds=1))
        rejected.append(rows(conn)[0][2])
        conn.close()

    conn, post = make_post(False)
    post.on_xp(incomplete, [], Path("xp.png"), when)
    post.finish()
    post.on_xp(full, [], Path("xp2.png"), when + timedelta(minutes=1))
    post.finish()
    reopened = rows(conn)
    conn.close()

    conn, post = make_post(False)
    post.on_xp(incomplete, [], Path("xp.png"), when)
    post.finish()
    post.on_match_start({"mode": "ranked", "map": "残月", "raw_map": "残月"}, when + timedelta(minutes=3))
    post.on_xp(full, [], Path("xp2.png"), when + timedelta(minutes=4))
    post.finish()
    new_round = rows(conn)
    conn.close()

    conn, post = make_post()
    original_id = post.match_id
    storage.update_match(conn, original_id, {"rank_tier": "黄金", "rank_division": "I", "rank_progress": 100})
    post.summary = ({"placement": 3, "kda": (1, 0, 0)}, when)
    post.on_xp(incomplete, [], Path("xp.png"), when)
    xp_id = post.xp_match_id
    post.on_xp(full, [], Path("xp2.png"), when + timedelta(seconds=1))
    conflict_rows = rows(conn)
    conn.close()

    conn, post = make_post()
    post.on_xp(incomplete, [], Path("xp.png"), when)
    with TemporaryDirectory() as folder:
        watcher = fake_game.live_collect.ScreenWatcher(None, load_config("screens.json", "zh-TW"), cfg, {}, {}, post,
                                                       Path(folder), lambda message: None, keep_all=False)
        watcher.current, watcher.last, watcher.frames = "xp", (BLACK, when + timedelta(seconds=1)), 1
        with mock.patch.object(fake_game.live_collect, "read_xp", return_value=(full, [])), \
             mock.patch.object(fake_game.live_collect, "save_png"):
            watcher.leave()
        last_frame = rows(conn)
    conn.close()

    conn, post = make_post()
    with TemporaryDirectory() as folder:
        watcher = fake_game.live_collect.ScreenWatcher(None, load_config("screens.json", "zh-TW"), cfg, {}, {}, post,
                                                       Path(folder), lambda message: None, keep_all=False)
        ticks = iter(range(20))
        with mock.patch.object(fake_game.live_collect, "time", mock.Mock(monotonic=lambda: next(ticks))), \
             mock.patch.object(fake_game.live_collect, "numbers_snapshot", return_value=None), \
             mock.patch.object(fake_game.live_collect, "numbers_moved", return_value=False), \
             mock.patch.object(fake_game.live_collect, "read_rp", return_value=(74, {})), \
             mock.patch.object(fake_game.live_collect, "read_xp", return_value=(incomplete, [])) as reads, \
             mock.patch.object(fake_game.live_collect, "save_png"):
            for second in range(10):
                watcher._watch_xp(BLACK, when + timedelta(seconds=second))
        limited = reads.call_count == cfg["still"]["max_reads"] and rows(conn) == [("白银", "III", None, 74, 9)]
    conn.close()
    return {
        "先保存已核对 RP、名次，缺少分数时继续重试": before == [("白银", "III", None, 74, 9)] and pending,
        "重试只更新同一局的缺失段位，成功后停止且不重复记局":
            after == [("白银", "III", 436, 74, 9)] and bounded_after_success and no_duplicate and len(sources) == 1,
        "没有总结页时保留更完整的读数，收尾只记一局": no_summary == [("白银", "III", 436, 74, 9)],
        "没有总结页时即使后一次字段更多，RP 不一致也不覆盖原读数":
            unconfirmed_other == [[("白银", "III", None, 74, 9)]] * 2,
        "RP、名次、段位不一致或核对失败不合并；不覆盖用户修改或其他已有数据":
            rejected == [None, None, None, None, 123, 456],
        "和总结页冲突时只补经验值页单独记下的那局":
            xp_id != original_id and conflict_rows == [("黄金", "I", 100, None, None), ("白银", "III", 436, 74, 9)],
        "离开经验值页时最后一帧读全也补进原局": last_frame == [("白银", "III", 436, 74, 9)],
        "在大厅重新打开先前缺段位的经验值页不重复记局": len(reopened) == 1,
        "已经出现下一局开局画面时不误当作重开上一局": len(new_round) == 2,
        "一直读不到段位时按配置限制重试次数，保留 RP 和名次": limited,
    }


def season31_checks():
    """2026-11-13 新繁体截图，截图不在仓库中。"""
    paths = [SHOTS / "frames/20261113_212220/0041_212301_2560x1440.png",
             SHOTS / "frames/20261113_213811/0001_213811_2560x1440.png",
             SHOTS / "frames/20261113_213811/0027_213838_2560x1440.png"]
    if not all(p.is_file() for p in paths):
        raise MissingShots("2026-11-13 繁体大厅和经验值页")
    before, xp_frame, after = (load(p) for p in paths)
    engine = LazyEngine(SMALL_CROP_PARAMS)
    detector = ScreenDetector(engine, lang="zh-TW")
    reader = LobbyReader(engine, load_config("lobby.json", "zh-TW"))
    xp, problems = read_xp(engine, xp_frame, load_config("xp.json", "zh-TW"))
    conn = storage.connect(":memory:")
    log = []
    watcher = LobbyWatcher(reader, PostMatch(conn, log=log.append), conn, log.append)
    t0 = datetime(2026, 11, 13, 21, 22, 20)
    watcher.update(detector.detect(before), before, t0)
    first_count = watcher.saved
    watcher.update(detector.detect(before), before, t0 + timedelta(seconds=1))
    watcher.update("xp", xp_frame, t0 + timedelta(minutes=16))
    watcher.update(detector.detect(after), after, t0 + timedelta(minutes=17))
    watcher.update(detector.detect(after), after, t0 + timedelta(minutes=17, seconds=1))
    lobbies = storage.lobby_readings(conn)
    card = panel_app.rank_card([], lobbies)
    values = [(r["rank_tier"], r["rank_division"], r["rank_progress"]) for r in lobbies]
    conn.close()
    conn = storage.connect(":memory:")
    post = PostMatch(conn, log=lambda message: None)
    cfg = load_config("xp.json", "zh-TW")
    original_read = xp_page.read_text
    next_reads = []
    def transient(engine_arg, img, spec, config):
        if spec["roi"] == cfg["fields"]["next_rank"]["roi"]:
            next_reads.append(1)
            if len(next_reads) == 1:
                return "白銀Ⅱ", 0.99  # 真截图 + 模拟首次 OCR 漏掉布局特征
        return original_read(engine_arg, img, spec, config)
    with TemporaryDirectory() as folder:
        screen = fake_game.live_collect.ScreenWatcher(engine, load_config("screens.json", "zh-TW"), cfg, {}, {}, post,
                                                      Path(folder), lambda message: None, keep_all=False)
        ticks = iter([0, .31, 1.0])
        with mock.patch.object(fake_game.live_collect, "time", mock.Mock(monotonic=lambda: next(ticks))), \
             mock.patch.object(fake_game.live_collect, "save_png"), mock.patch.object(xp_page, "read_text", side_effect=transient):
            screen._watch_xp(xp_frame, t0)
            screen._watch_xp(xp_frame, t0)
            first = post.xp[0]["rank_progress"]
            screen._watch_xp(xp_frame, t0 + timedelta(seconds=1))
        post.finish()
        replay = [tuple(row) for row in conn.execute("SELECT rank_tier, rank_division, rank_progress, rp_delta, placement FROM matches")]
    conn.close()
    return {
        "繁体新大厅和经验值页识别正确，黑屏不是大厅": detector.detect(before) == "lobby"
            and detector.detect(after) == "lobby" and detector.detect(xp_frame) == "xp" and detector.detect(BLACK) is None,
        "经验值页得到白银 III 436、RP +74、第 9 名且明细核对通过":
            (xp["rank_tier"], xp["rank_division"], xp["rank_progress"], xp["rp_delta"], xp["placement"], problems)
            == ("白银", "III", 436, 74, 9, []),
        "大厅须连续两次相同才保存；离开再回来写入更新后的分数": first_count == 0
            and values == [("白银", "III", 377), ("白银", "III", 436)],
        "段位卡片使用最新大厅读数": card is not None and card["name"] == "白银 III"
            and card["progress"] == 436 and "大厅" in card["as_of"],
        "真截图模拟首次 OCR 漏字，重试实际 OCR 后同一局补全段位":
            first is None and len(next_reads) == 2 and replay == [("白银", "III", 436, 74, 9)],
    }


def frames():
    found = sorted(FOLDER.glob("*.jpg"))
    if len(found) != COUNT:
        raise MissingShots(f"{FOLDER}（要 {COUNT} 张，找到 {len(found)} 张）")
    return found


def frame(files, n):
    path = files[n - 1]
    assert path.name.startswith(f"{n:04d}_"), path.name
    return load(path)


def single_checks(files):
    """单独读几张（按 16:10 换算）。"""
    engine = LazyEngine(SMALL_CROP_PARAMS)
    layout.set_game_aspect(16 / 10)
    try:
        xp, problems = read_xp(engine, frame(files, 862), load_config("xp.json", "zh-TW"))
        elim = [read_eliminated(engine, frame(files, n), load_config("eliminated.json", "zh-TW"))["placement"]
                for n in (238, 534, 850)]
        lobby = LobbyReader(engine, load_config("lobby.json", "zh-TW")).read(frame(files, 600))
        hud = Hud(engine, lang="zh-TW")
        spectate = [hud.spectating(frame(files, n)) for n in (205, 400, 700)]
        screens = GameScreens(engine, ["zh-CN", "en", "zh-TW"])
        seen = [screens.detect(frame(files, n)) for n in (45, 600, 73, 629, 538, 862, 541, 238, 205, 871)]
        flat = ScreenDetector(engine, lang="zh-TW").similarity(frame(files, 871), "eliminated")
    finally:
        layout.set_game_aspect(None)
    return {"xp": ({k: xp[k] for k in ("rp_delta", "placement", "kills", "assists", "rank_tier", "rank_division",
                                       "rank_progress")}, problems),
            "elim": elim, "lobby": (lobby["tier"], lobby["division"], lobby["progress"]), "spectate": spectate,
            "seen": seen, "lang": (screens.lang, screens.locked), "flat": flat}


def collect_checks(files):
    rows, _, log = run_collect(files, load, game_aspect="16:10", keep_all=False)
    conn = storage.connect(fake_game.last_db)
    mates = [len(storage.teammates_of(conn, r["match_id"])) for r in rows]
    lobbies = [(r["rank_tier"], r["rank_division"], r["rank_progress"]) for r in storage.lobby_readings(conn)]
    conn.close()
    return {"rows": [{k: r[k] for k in FIELDS} for r in rows], "log": log, "mates": mates, "lobbies": lobbies}


def main():
    print("繁体中文游戏画面（三局整段重放 + 单张）")
    checks = {**rule_checks(), **status_checks(), **retry_checks()}
    skipped = False
    try:
        checks.update(season31_checks())
    except MissingShots as e:
        print(f"  ⏭ 跳过新赛季截图部分：找不到 {e}")
        skipped = True
    try:
        files = frames()
    except MissingShots as e:
        print(f"  ⏭ 跳过旧赛季截图部分：找不到截图 {e}（截图只在开发者电脑上，不在仓库里）")
        return None if report(checks) else False
    s, c = single_checks(files), collect_checks(files)
    rows = c["rows"]
    said = [line.split("] ", 1)[-1].strip() for line in c["log"]]
    wrong = [{k: (row[k], want[k]) for k in FIELDS if row[k] != want[k]} for row, want in zip(rows, WANT)]
    checks.update({
        "经验值页（排位）：RP −48、第 16 名、计分击杀 0 助攻 0、白金 II 258（'下個排名：白金1'读到了段数），明细核对通过":
            s["xp"] == ({"rp_delta": -48, "placement": 16, "kills": 0, "assists": 0, "rank_tier": "白金",
                         "rank_division": "II", "rank_progress": 258}, []),
        f"淘汰画面：'排名 #17''#13''#16'（读到 {s['elim']}）": s["elim"] == [17, 13, 16],
        f"大厅：白金Ⅱ、306/1000RP → 白金 II 306（读到 {s['lobby']}）": s["lobby"] == ("白金", "II", 306),
        "观战标签'觀戰'：观战时认得出，对局中不会认错": s["spectate"] == [True, False, False],
        f"认画面：匹配大厅'普通對戰'和排位大厅'積分賽'都是大厅，'三排''積分賽'横幅都是开局画面，总结页，"
        f"排位的经验值页；匹配的经验值页不算（底部有'返回大廳'，算小队已淘汰）；淘汰画面；观战队友、桌面都不是"
        f"（认到 {s['seen']}）":
            s["seen"] == ["lobby", "lobby", "match_start", "match_start", "summary", "xp", "squad_out", "eliminated",
                          None, None],
        "三种语言一起认时，第一张（匹配大厅）就认定繁体": s["lang"] == ("zh-TW", True),
        f"桌面上黑色的播放器不像淘汰画面（纯色的位置不算，相似度 {s['flat']:.2f}）": s["flat"] < 0.35,
        "记录程序：开游戏后在大厅就认定繁体": "游戏语言：繁體中文（从大厅（匹配）认出）" in said,
        f"记下 3 局（{len(rows)}）": len(rows) == 3,
        f"三局全对（对不上的：{[w for w in wrong if w] or '无'}）": len(rows) == 3 and not any(wrong),
        "开局画面读到两次匹配、奥林匹斯和一次排位、残月":
            said.count("开局画面：读到 匹配、奥林匹斯") == 2 and said.count("开局画面：读到 排位、残月") == 1,
        f"每局 2 名队友（{c['mates']}）": c["mates"] == [2, 2, 2],
        f"大厅：排位时读出白金 II 306（{c['lobbies']}）": c["lobbies"] == [("白金", "II", 306)],
        "匹配大厅：未读到段位，写一行就不读": said.count("大厅：未读到段位，可能为非排位模式或识别失败，这次不读") == 1,
        "排位局的 RP 和对局中最后读到的一致": "RP 核对：经验值页、对局中都是 -48（编号 3）" in said,
    })
    ok = report(checks)
    return None if ok and skipped else ok


if __name__ == "__main__":
    raise SystemExit(1 if main() is False else 0)
