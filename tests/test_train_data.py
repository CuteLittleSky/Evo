"""训练小模型的素材、训练和网页（tools/train_data.py、train_digits.py、train_web.py）：分堆的规则、考卷避开有视频的那几天、训练用哪些答案、小模型读数的规矩、网页。不用截图、不加载识别引擎和 PyTorch。

- 分堆：同一个读数最多留 3 块（多的进 extra）；读出来、把握够、和前后对得上（伤害只增不减，剩余小队只减不增）才进
  "规则核对过"，其余"要人看"；隔了 90 秒以上算另一局，不跨局比
- 日期：从视频文件名（Evo 的录像、月-日-年）、截图文件夹名看出是哪天；有视频的那几天的截图不进考卷（可能是同一局）
- 训练用哪些答案：用户核对过的用用户的；规则核对过的用引擎的；多出来的同一个读数，同一个视频、同一项里有可信的同一个读数
  才借答案，用户改过的读数不借；丢掉的、考卷都不用。RP 的正数补"+"
- 小模型读数：缩成 32×96；CTC 解码合并重复、去掉"空"；格式、范围、把握的规矩和引擎一样，只有 RP 能带 + -
- 网页：首页、核对页、训练进度页（两张小图）、考卷成绩页（引擎和小模型并排）、小块图片打得开；保存这一页：改过的、
  空着的（这一块没有数字）、丢掉的都记对，RP 的 + 号留着、伤害的逗号去掉，填的不是数字的不存、留在"没核对"里并提示

运行：python tests/test_train_data.py
"""

import sys
import tempfile
from pathlib import Path

from fake_game import ROOT, report  # 要在项目代码之前导入（设置临时数据文件夹）

sys.path.insert(0, str(ROOT / "tools"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import train_data as data  # noqa: E402
import train_digits as digits  # noqa: E402
import train_web as web  # noqa: E402

FOLDER = Path(tempfile.mkdtemp(prefix="evo-train-"))


def make_db():
    data.CROPS = FOLDER / "crops"
    conn = data.connect(FOLDER / "train.db")
    sid = conn.execute("insert into sources (kind, path, name, status, exam_part) values ('exam', 'x', 'x', 'done', 'test')").lastrowid
    rows = [  # (项, 秒, 读数, 把握)
        ("damage", 0, "10", .99), ("damage", 5, "10", .99), ("damage", 10, "10", .99), ("damage", 15, "10", .99),
        ("damage", 20, "80", .99), ("damage", 25, "8", .99), ("damage", 30, "120", .99), ("damage", 35, None, .3),
        ("damage", 40, "130", .5), ("damage", 200, "5", .99),
        ("squads_left", 0, "20", .99), ("squads_left", 10, "21", .99), ("squads_left", 20, "18", .99),
        ("rp_delta", 0, "-43", .99), ("rp_delta", 10, "+19", .99),
    ]
    ids = {}
    for field, t, value, score in rows:
        rel = f"exam/{sid}/{field}/{t}.png"
        (data.CROPS / rel).parent.mkdir(parents=True, exist_ok=True)
        cv2.imencode(".png", np.zeros((20, 40, 3), np.uint8))[1].tofile(str(data.CROPS / rel))
        ids[(field, t)] = conn.execute(
            "insert into crops (source, kind, field, t, img, w, h, engine_value, engine_score, engine_text) "
            "values (?, 'exam', ?, ?, ?, 40, 20, ?, ?, ?)", (sid, field, t, rel, value, score, value or "")).lastrowid
    data.assign_piles(conn, sid)
    conn.commit()
    return conn, ids


def piles(conn, ids):
    return {key: conn.execute("select pile from crops where id=?", (cid,)).fetchone()[0] for key, cid in ids.items()}


def main():
    print("训练小模型的素材：分堆、日期、核对答案的网页")
    conn, ids = make_db()
    p = piles(conn, ids)
    checks = {
        "同一个读数 4 块只留 3 块（挑隔得开的：第 0、10、15 秒），第 5 秒那块进 extra":
            [p[("damage", t)] for t in (0, 5, 10, 15)] == ["auto", "extra", "auto", "auto"],
        "伤害 80 → 8 → 120：前后对不上的两块要人看，120 对得上": (p[("damage", 20)], p[("damage", 25)], p[("damage", 30)]) ==
            ("check", "check", "auto"),
        "引擎没读出、把握不够的要人看": (p[("damage", 35)], p[("damage", 40)]) == ("check", "check"),
        "隔了 90 秒以上算另一局：第 200 秒的 5 不和前面比": p[("damage", 200)] == "auto",
        "剩余小队只减不增：20 → 21 → 18 里 20、21 要人看，18 对得上":
            (p[("squads_left", 0)], p[("squads_left", 10)], p[("squads_left", 20)]) == ("check", "check", "auto"),
        "RP 带 + 号的读数也按数比（-43 → +19 对得上）": (p[("rp_delta", 0)], p[("rp_delta", 10)]) == ("auto", "auto"),
        "日期：Evo 的录像 2026-11-20_…": data.name_day("2026-11-20_201648_班加罗尔.mp4") == "20261120",
        "日期：月-日-年的录像 …_11-10-2026_…": data.name_day("Apex Legends_11-10-2026_21-46-43-433.mp4") == "20261110",
        "日期：看不出的不猜": data.name_day("BV1xx411c7mD.mp4") is None,
        "截图文件夹是哪天截的": data.folder_day(Path("E:/d/captures/live/20261110_212454/hud")) == "20261110"
                               and data.folder_day(Path("captures/aspect/16x10_20261020")) is None,
    }

    web.app.config["DB_PATH"] = FOLDER / "train.db"
    conn.execute("update crops set engine_text='11' where id=?", (ids[("damage", 35)],))  # 读到了，但把握不够
    conn.commit()
    client = web.app.test_client()
    pages = {url: client.get(url) for url in ("/", "/check/exam", "/check/video", "/score",
                                              f"/img/{ids[('damage', 0)]}")}
    checks["首页、核对页、成绩页、小块图片都打得开"] = all(r.status_code == 200 for r in pages.values())
    checks["核对页先列剩余小队（照 FIELDS 的顺序），每块的框里是引擎的读数"] = (
        pages["/check/exam"].get_data(as_text=True).find(f'id="a{ids[("squads_left", 0)]}"') <
        pages["/check/exam"].get_data(as_text=True).find(f'id="a{ids[("damage", 0)]}"')
        and f'value="+19" data-orig="+19"' in pages["/check/exam"].get_data(as_text=True))
    checks["引擎读到了字、但 Evo 不采用的，格子上写出读到什么和把握"] = "11 · 把握 0.30" in pages["/check/exam"].get_data(as_text=True)

    a, b, c, d, e = (ids[("damage", 0)], ids[("damage", 20)], ids[("damage", 25)], ids[("damage", 35)],
                     ids[("rp_delta", 10)])
    form = {"ids": [str(x) for x in (a, b, c, d, e)], "back": "/check/exam?field=all",
            f"a{a}": "1,867", f"s{a}": "0", f"a{b}": "", f"s{b}": "0", f"a{c}": "8", f"s{c}": "1",
            f"a{d}": "abc", f"s{d}": "0", f"a{e}": " +19 ", f"s{e}": "0"}
    saved = client.post("/check/exam/save", data=form)
    row = {x: conn.execute("select status, answer from crops where id=?", (x,)).fetchone() for x in (a, b, c, d, e)}
    checks["保存：伤害的逗号去掉、RP 的 + 号留着"] = tuple(row[a]) == ("done", "1867") and tuple(row[e]) == ("done", "+19")
    checks["保存：空着 = 这一块没有数字（答案是空字符串）"] = tuple(row[b]) == ("done", "")
    checks["保存：丢掉的记成 skip"] = row[c]["status"] == "skip"
    checks["保存：填的不是数字的不存，回到这一页并提示"] = (tuple(row[d]) == ("todo", None) and saved.status_code == 302
                                                  and "msg=" in saved.headers["Location"])
    score = client.get("/score").get_data(as_text=True)
    checks["成绩页：读错 2 块（10 ≠ 1867；那块其实没有数字、引擎读出了 80），+19 算读对"] = (
        "引擎读错的（2）" in score and "引擎没读出的（0）" in score)

    checks.update(digits_checks(conn))
    run = conn.execute("insert into train_runs (started, status, device, epochs, best_epoch, threshold, model_ms, "
                       "model_mem_mb, size_kb) values ('2026-11-21T15:00:00', 'done', 'GPU', 3, 2, 0.8, 0.4, 15, 470)").lastrowid
    conn.executemany("insert into train_epochs (run, epoch, loss, val_acc, val_wrong, val_missed, val_n, seconds) "
                     "values (?, ?, ?, ?, 0, 0, 10, 1)", [(run, 1, 3.0, 0.2), (run, 2, 1.0, 0.9), (run, 3, 0.8, 0.85)])
    conn.executemany("insert into model_reads (run, crop, text, score, value, ms) values (?, ?, ?, .99, ?, .4)",
                     [(run, a, "1867", "1867"), (run, b, "", None), (run, e, "+19", "+19")])
    conn.commit()
    train = client.get("/train").get_data(as_text=True)
    checks["训练进度页：列出这次训练、两张小图（每一轮的分数、loss），最后一点标上数"] = (
        "第 1 次" in train and train.count("<svg") == 2 and ">85%</text>" in train)
    score = client.get("/score").get_data(as_text=True)
    checks["成绩页：小模型那一列有数，列出小模型读对、引擎没读对的 2 块"] = (
        "小模型读对、引擎没读对的（2）" in score and "小模型读错的（0）" in score)

    f = ids[("squads_left", 20)]  # 一块没核对过的（引擎读 18）；a 是一块考卷（答案 1867）
    conn.executemany("update crops set review=?, review_run=? where id=?", [("16", run, f), ("1861", run, a)])
    conn.commit()
    page = client.get("/review").get_data(as_text=True)
    checks["复核页：列出挑出来的块，框里什么都不填、不写引擎和小模型读的，导航上写着还剩几块"] = (
        f'id="a{f}" name="a{f}" value=""' in page and f'id="a{a}" name="a{a}" value=""' in page
        and "1861" not in page and ">复核 <b" in page)
    client.post("/review/save", data={"ids": [str(f), str(a)], "back": "/review", f"a{f}": "16", f"s{f}": "0",
                                      f"a{a}": "", f"s{a}": "1"})
    rf = conn.execute("select status, answer, answer_before, reviewed_at from crops where id=?", (f,)).fetchone()
    ra = conn.execute("select status, answer_before, reviewed_at from crops where id=?", (a,)).fetchone()
    checks["复核保存：照图填的当作核对过的答案，记下原来的答案（没核对过的记引擎读数）；丢掉的记 skip"] = (
        tuple(rf)[:3] == ("done", "16", "18") and rf["reviewed_at"] and tuple(ra)[:2] == ("skip", "1867")
        and ra["reviewed_at"])
    page = client.get("/review").get_data(as_text=True)
    checks["复核完：没有要复核的了，统计写着改了 1 块、改成和小模型一样 1 块、丢掉 1 块"] = (
        "没有要复核的了" in page and "改了 1 块" in page and "和小模型读的一样 1 块" in page and "丢掉 1 块" in page)
    checks["复核过的块下次挑的时候不再挑"] = conn.execute(
        "select count(*) from crops where reviewed_at is not null and review is not null").fetchone()[0] == 2
    conn.close()
    return report(checks)


def red_text_visible():
    """灰底（128）上的红字（BGR 40,40,230），上下有白边框（像 RP 那一格）：灰度里字只比底色暗一点，最亮的颜色里亮得多。"""
    crop = np.full((40, 80, 3), 128, np.uint8)
    crop[:4], crop[-4:] = 255, 255
    crop[10:30, 20:60] = (40, 40, 230)
    x = digits.prepare(crop)  # 80×40 缩成 64×32：字在第 8–24 行、第 16–48 列
    text, back = x[:, 16, 32], x[:, 6, 2]  # 字中间、边框下面左边的底
    return abs(float(text[1] - back[1])) > 0.5 and abs(float(text[1] - back[1])) > abs(float(text[0] - back[0]))


def digits_checks(conn):
    """训练用哪些答案、小模型读数的规矩（不用 PyTorch）。"""
    sid = conn.execute("insert into sources (kind, path, name, status) values ('video', 'v', 'v.mp4', 'done')").lastrowid
    rows = [  # (项, 读数, 堆, 状态, 答案)
        ("damage", "55", "auto", "todo", None),          # 规则核对过：用引擎的读数
        ("damage", "55", "extra", "todo", None),         # 多出来的 55：借答案
        ("damage", "60", "check", "done", "68"),         # 用户把 60 改成了 68
        ("damage", "60", "extra", "todo", None),         # 多出来的 60：用户改过，不借
        ("damage", "70", "extra", "todo", None),         # 没有可信的 70：不借
        ("damage", None, "check", "skip", None),         # 丢掉的：不用
        ("rp_delta", "19", "check", "done", "19"),       # RP 正数补 +
    ]
    for field, value, pile, status, answer in rows:
        conn.execute("insert into crops (source, kind, field, t, img, engine_value, engine_score, pile, status, answer) "
                     "values (?, 'video', ?, 0, 'x.png', ?, .99, ?, ?, ?)", (sid, field, value, pile, status, answer))
    conn.commit()
    got = sorted((r["group"], r["label"]) for r in digits.training_rows(conn))
    probs = np.full((6, len(digits.CHARS) + 1), 0.01)
    for t, k in enumerate([2, 2, 0, 2, 0, 0]):  # "1" "1" 空 "1" 空 空 → "11"
        probs[t, k] = 0.95
    ranges = digits.field_ranges()
    crop = np.full((40, 60), 30, np.uint8)
    cv2.putText(crop, "57", (8, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, 255, 2)
    erased = digits.erase_digits(crop)
    return {
        "造空块：暗底白字的数字抹得掉（抹完没有亮笔画），整块都亮的不抹，RP 不造空块":
            erased is not None and erased.shape == crop.shape and int(erased.max()) < 150
            and digits.erase_digits(np.full((40, 60), 230, np.uint8)) is None and "rp_delta" not in digits.EMPTY_FIELDS,
        "训练用：用户的答案、引擎的读数、借来的答案；用户改过的、没有可信读数的、丢掉的不用；RP 正数补 +":
            got == [("auto", "55"), ("extra", "55"), ("human", "+19"), ("human", "68")],
        "小模型的输入是 2 张 32×96（灰度、最亮的颜色）、0–1 之间；老的小模型照旧 1 张":
            digits.prepare(np.zeros((45, 68, 3), np.uint8)).shape == (2, digits.H, digits.W)
            and digits.prepare(np.zeros((45, 68, 3), np.uint8), 1).shape == (1, digits.H, digits.W)
            and float(digits.prepare(np.full((20, 400), 200, np.uint8)).max()) <= 1.0,
        '红字在灰度里和灰底差不多暗，在"最亮的颜色"里是亮的': red_text_visible(),
        "CTC 解码：连着的同一个字算一个，中间隔了空的算两个": digits.decode(probs)[0] == "11",
        "小模型读数的规矩：把握不够、格式不对、超出范围都不采用，只有 RP 能带号": (
            digits.model_value("150", .99, "damage", .8, ranges) == ("150", False)
            and digits.model_value("150", .5, "damage", .8, ranges) == (None, False)
            and digits.model_value("+5", .99, "damage", .8, ranges) == (None, False)
            and digits.model_value("+19", .99, "rp_delta", .8, ranges) == ("+19", False)
            and digits.model_value("99", .99, "squads_left", .8, ranges) == (None, False)
            and digits.model_value("", .99, "rp_delta", .8, ranges) == (None, True)),
        '挑门槛：读错不比引擎多的门槛里挑读对最多的': threshold_rule(ranges),
        "考卷分两半：按文件夹一个隔一个，以后新加的放进块数少的那一半，分过的不动": exam_parts_rule(),
    }


def threshold_rule(ranges):
    """引擎在这 4 块上读错 1 块。小模型：门槛 0.3 读对 2、错 2；0.4–0.6 读对 2、错 1；0.7 读对 2、错 0；0.8 以上读对 1。
    读错不比引擎多（≤ 1）里读对最多的是 2 块，一样多挑最低的：0.4。"""
    items = [{"field": "damage", "label": lab, "engine": eng} for lab, eng in
             (("10", "10"), ("20", "26"), ("30", "30"), ("40", "40"))]
    reads = [("10", .99), ("20", .7), ("38", .65), ("41", .35)]
    return digits.pick_threshold(items, reads, ranges)[:3] == (0.4, 2, 1) and digits.engine_wrong(items) == 1


def exam_parts_rule():
    conn = data.connect(FOLDER / "parts.db")
    for n in (10, 20, 30, 40):
        sid = conn.execute("insert into sources (kind, path, name, status) values ('exam', ?, 'p', 'done')",
                           (f"p{n}",)).lastrowid
        conn.executemany("insert into crops (source, kind, field, img, pile) values (?, 'exam', 'damage', 'x', 'auto')",
                         [(sid,)] * n)
    data.assign_exam_parts(conn)
    first = [r[0] for r in conn.execute("select exam_part from sources order by id")]
    sid = conn.execute("insert into sources (kind, path, name, status) values ('exam', 'p5', 'p', 'done')").lastrowid
    conn.executemany("insert into crops (source, kind, field, img, pile) values (?, 'exam', 'damage', 'x', 'auto')", [(sid,)] * 5)
    data.assign_exam_parts(conn)
    after = [r[0] for r in conn.execute("select exam_part from sources order by id")]
    conn.close()
    # 第一次：dev 10+30=40 块、test 20+40=60 块 → 新的放进 dev
    return first == ["dev", "test", "dev", "test"] and after == ["dev", "test", "dev", "test", "dev"]


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
