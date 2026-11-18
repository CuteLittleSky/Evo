"""总结页停留时的几种情况：名次晚出现要等它；提前离开、等太久、停止记录时，用读得最全的一次存下并标记待确认；
在总结页和别的画面之间来回切换，同一局不重复存。
同一次停留里没变的项不再识别、沿用上次的（2026-11-01，summary_page.read_field 的 memo）：同一张读第二遍一次都不识别；
名次滑进来时只重读名次、小队总击杀；伤害换了一个数时只重读伤害；配置里去掉 unchanged 就每遍都全读。

用的是 2026-10-16 晚的真实截图，外加一张拼出来的"名次还没滑进来"的画面（见 late_placement）。
截图不在仓库里，见 fake_game.py。

运行：python tests/test_summary_page.py
"""

from fake_game import MissingShots, load, report, run_collect, shot  # 要在项目代码之前导入（设置临时数据文件夹）

import itertools
import re
from datetime import datetime, timedelta
from unittest import mock

import live_collect
import ocr
import screenshots
from paths import data_absolute
from summary_page import read_summary

NIGHT = "live/20261016_211452/"
RUNS = itertools.count(1)


def late_placement(full, blank):
    """拼一张"名次还没滑进来"的总结页：把右上角（名次、小队总击杀）换成另一张截图里还是空白的同一块。
    坐标是 1920×1080 下的 [1400, 140, 480, 100]，换算到 2560×1440。"""
    frame = full.copy()
    frame[187:320, 1867:2507] = blank[187:320, 1867:2507]
    return frame


def other_damage(full, other):
    """拼一张"伤害换了一个数"的总结页：中间卡片上伤害那一格换成另一张截图的（1920×1080 下 [748, 472, 180, 46]，
    换算到 2560×1440 再放宽一点）。"""
    frame = full.copy()
    frame[620:700, 990:1245] = other[620:700, 990:1245]
    return frame


def blank_rows(full, *rows):
    """拼一张"中间卡片上这几行没读到"的总结页：这几行（2560×1440 下的上下范围）涂成旁边的底色。"""
    frame = full.copy()
    for top, bottom in rows:
        frame[top:bottom, 990:1245] = frame[top - 10, 1000]
    return frame


def run_with_jumps(frames, images):
    """和 run_collect 一样，只是帧名写成"观战+130"时，这一帧让时间往后跳 130 秒（存一局的时间用 datetime.now()，
    fake_game 的假时钟管不到）。每次从不同的小时开始，截图文件夹不重名。"""
    ahead = [next(RUNS) * 3600]

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=ahead[0])

    def picture(label):
        name, _, seconds = label.partition("+")
        ahead[0] += int(seconds or 0)
        return images[name]

    with mock.patch.object(live_collect, "datetime", Later):
        return run_collect(frames, picture, keep_all=False)


def revisit_checks(images):
    """又回到总结页的几种情况（2026-11-09，PR #6）：没读全就切走、很快回到同一张，补进原来那一局；
    隔了很久、回过大厅，同一局也不重复存。"""
    rows, _, _ = run_with_jumps(["观战", "少项", "少项", "观战", "F5", "F5", "大厅", "大厅"], images)
    saved = data_absolute(rows[0]["evidence_path"]) if rows and rows[0]["evidence_path"] else None
    out = {
        "第一次只读到击杀、伤害、生存时间，又回到同一张读全：只存 1 局、补全名次、不待确认":
            len(rows) == 1 and (rows[0]["placement"], rows[0]["damage"], rows[0]["needs_review"]) == (8, 688, 0),
        "补全以后总结页截图还在（名字照旧，一直留着），不多留一张":
            saved is not None and saved.exists() and screenshots.is_summary(saved)
            and [p.name for p in saved.parent.iterdir()] == [saved.name],
    }
    rows, _, _ = run_with_jumps(["观战", "B5", "B5", "观战", "缺伤害", "观战", "大厅", "大厅"], images)
    out["第一次读到伤害，第二次停得短没读到：保留第一次读到的 688"] = len(rows) == 1 and rows[0]["damage"] == 688
    rows, _, _ = run_with_jumps(["观战", "F5", "F5", "F5", "观战", "观战+130", "F5", "F5", "观战"], images)
    out["过了 2 分钟以上再切回同一局的总结页：不重复存"] = len(rows) == 1
    rows, _, _ = run_with_jumps(["观战", "F5", "F5", "F5", "大厅", "大厅", "F5", "F5", "大厅"], images)
    out["回到大厅后重新打开上一局的总结页：不重复存"] = len(rows) == 1
    return out


def memo_checks(images):
    """直接读几遍总结页（同一个 memo），数每一遍识别了几次。"""
    real = ocr.create_engine(ocr.SMALL_CROP_PARAMS)
    calls = [0]

    def engine(img, **kwargs):
        calls[0] += 1
        return real(img, **kwargs)

    cfg = ocr.load_config("summary.json")

    def reads(names, config=cfg):
        """按顺序读 names 里的几张（同一个 memo），返回每一遍 (识别了几次, 读到的值)。"""
        memo, out = {}, []
        for name in names:
            before = calls[0]
            results, _ = read_summary(engine, images[name], config, memo=memo)
            out.append((calls[0] - before, {k: r["value"] for k, r in results.items()}))
        return out

    f5 = reads(["F5", "F5"])
    late = reads(["B5", "F5"])
    damage = reads(["F5", "F5改伤害"])
    plain = reads(["F5", "F5"], {k: v for k, v in cfg.items() if k != "unchanged"})
    return {
        f"同一张读第二遍：一次都不识别，读到的一样（{f5[0][0]} → {f5[1][0]} 次）":
            f5[0][0] == 7 and f5[1][0] == 0 and f5[0][1] == f5[1][1],
        f"名次滑进来：只重读名次、小队总击杀，读到第 8 名（{late[1][0]} 次）":
            late[0][1]["placement"] is None and late[1][0] == 2 and late[1][1] == f5[0][1],
        f"伤害换了一个数：只重读伤害（{damage[1][0]} 次，{damage[0][1]['damage']} → {damage[1][1]['damage']}）":
            damage[1][0] == 1 and (damage[0][1]["damage"], damage[1][1]["damage"]) == (688, 599)
            and {k: v for k, v in damage[1][1].items() if k != "damage"} == {k: v for k, v in f5[0][1].items() if k != "damage"},
        f"配置里去掉 unchanged：每遍都全读（{[n for n, _ in plain]}）": [n for n, _ in plain] == [7, 7],
    }


def main():
    print("总结页停留")
    try:
        f5 = load(shot(NIGHT + "05_222749.png"))  # 第 8 名，3 杀，688 伤害
        images = {
            "观战": load(shot("samples/vlcsnap-2026-10-16-18h27m31s652.png")),  # 不是总结页的画面
            "F5": f5,
            "B5": late_placement(f5, load(shot(NIGHT + "01_213331.png"))),  # 01 那张保存时名次还没滑进来
            "M9": load(shot(NIGHT + "09_233033.png")),  # 名次没滑进来就离开了：0 伤害
            "M8": load(shot(NIGHT + "08_231728.png")),  # 95 伤害
            "M7": load(shot(NIGHT + "07_231013.png")),  # 599 伤害
            "大厅": load(shot(NIGHT + "lobby_211506_enter.png")),
        }
        images["F5改伤害"] = other_damage(f5, images["M7"])  # 第 8 名、3 杀，伤害换成 599
        images["少项"] = blank_rows(images["B5"], (810, 880), (905, 975))  # 名次没滑进来，急救、重生也没读到
        images["缺伤害"] = blank_rows(images["B5"], (620, 700))  # 名次没滑进来，伤害也没读到
    except MissingShots as e:
        print(f"  ⏭ 跳过：找不到截图 {e}（截图只在开发者电脑上，不在仓库里）")
        return None

    frames = ["观战", "B5", "B5", "B5", "F5", "F5", "F5", "观战", "F5", "F5", "观战", "M9", "M9", "观战", "M8"]
    rows, sleeps, log = run_collect(frames, images.get)
    stats = next((line for line in log if line.startswith("本次记录")), "")
    used = re.search(r"总结页读了 (\d+) 项（没变沿用 (\d+) 项）", stats)
    first, second, third = (rows + [None] * 3)[:3]
    checks = {
        "名次晚出现：等到第 8 名才存，不标待确认": first is not None and (
            first["placement"], first["squad_kills"], first["kills"], first["damage"], first["needs_review"]) == (8, 3, 3, 688, 0),
        "等名次时保持快速检查（每 0.3 秒一次）": all(s <= 0.3 for frame, s in sleeps if frame == "B5"),
        "切回同一局的总结页不重复存：一共 3 局": len(rows) == 3,
        "名次没滑进来就离开、其他数字已连续两次一致：存下，只标\"名次没读到\"（经验值页能补）": second is not None and (
            second["placement"], second["kills"], second["damage"], second["needs_review"], second["review_note"])
        == (None, 0, 0, 1, "名次：没读到（OCR 原文：无）"),
        "停止记录时总结页只读了一次：也存下，标待确认并注明停留太短": third is not None and (
            third["placement"] is None and third["damage"] == 95 and third["needs_review"] == 1
            and "停留太短" in (third["review_note"] or "")),
        f"日志写了总结页读了几项、沿用了几项（{used and used[0]}）": used is not None and int(used[2]) > 0,
    }
    checks.update(memo_checks(images))

    frames = ["观战"] + ["M7"] * 8
    rows, sleeps, _ = run_collect(frames, images.get, summary_timeout=1)
    m7_sleeps = [s for frame, s in sleeps if frame == "M7"]
    checks.update({
        "名次一直不出现：等满 1 秒先存下，标待确认，只存一次": len(rows) == 1 and rows[0]["damage"] == 599
        and rows[0]["needs_review"] == 1 and (rows[0]["review_note"] or "").startswith("名次：没读到"),
        "存下以后不再快速检查（回到每秒一次）": m7_sleeps[-1] == 1.0 and min(m7_sleeps) <= 0.3,
    })

    # 2026-11-06 实战：名次还没滑进来就离开总结页（存成待确认），几秒后回到同一张、名次也滑进来了 ——
    # 应该把那一局补全，不能再存一局（以前会存成两局：待确认 + 正常）。
    frames = ["观战", "B5", "B5", "观战", "F5", "F5"]
    rows, _, _ = run_collect(frames, images.get)
    checks["名次没滑进来就离开、随后回到同一张读全：只存 1 局并补全名次"] = (
        len(rows) == 1 and rows[0]["placement"] == 8 and rows[0]["needs_review"] == 0)
    checks.update(revisit_checks(images))
    return report(checks)


if __name__ == "__main__":
    raise SystemExit(0 if main() is not False else 1)
