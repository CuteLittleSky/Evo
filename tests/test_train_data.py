"""训练小模型的素材和网页（tools/train_data.py、tools/train_web.py）：分堆的规则、考卷避开有视频的那几天、核对答案的网页。不用截图、不加载识别引擎。

- 分堆：同一个读数最多留 3 块（多的进 extra）；读出来、把握够、和前后对得上（伤害只增不减，剩余小队只减不增）才进
  "规则核对过"，其余"要人看"；隔了 90 秒以上算另一局，不跨局比
- 日期：从视频文件名（Evo 的录像、月-日-年）、截图文件夹名看出是哪天；有视频的那几天的截图不进考卷（可能是同一局）
- 网页：首页、核对页、考卷成绩页、小块图片打得开；保存这一页：改过的、空着的（这一块没有数字）、丢掉的都记对，
  RP 的 + 号留着、伤害的逗号去掉，填的不是数字的不存、留在"没核对"里并提示

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
import train_web as web  # noqa: E402

FOLDER = Path(tempfile.mkdtemp(prefix="evo-train-"))


def make_db():
    data.CROPS = FOLDER / "crops"
    conn = data.connect(FOLDER / "train.db")
    sid = conn.execute("insert into sources (kind, path, name, status) values ('exam', 'x', 'x', 'done')").lastrowid
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
    client = web.app.test_client()
    pages = {url: client.get(url) for url in ("/", "/check/exam", "/check/video", "/score",
                                              f"/img/{ids[('damage', 0)]}")}
    checks["首页、核对页、成绩页、小块图片都打得开"] = all(r.status_code == 200 for r in pages.values())
    checks["核对页先列剩余小队（照 FIELDS 的顺序），每块的框里是引擎的读数"] = (
        pages["/check/exam"].get_data(as_text=True).find(f'id="a{ids[("squads_left", 0)]}"') <
        pages["/check/exam"].get_data(as_text=True).find(f'id="a{ids[("damage", 0)]}"')
        and f'value="+19" data-orig="+19"' in pages["/check/exam"].get_data(as_text=True))

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
    conn.close()
    return report(checks)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
