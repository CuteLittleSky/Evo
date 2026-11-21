"""训练读数字的小模型（开发工具）：用 tools/train_data.py 裁好、用户核对过的小块训练，导出成 ONNX，再用考卷考一遍。

小模型：把一小条数字（缩成高 H、宽 W 的灰度图）整条读出来（卷积 + 双向 GRU + CTC），只认 0–9 和 + -，
也会读出"空的"（这一块没有数字）。约 0.5 MB，导出成 ONNX，在 Evo 里用安装包本来就有的 onnxruntime 跑（1 个线程）。
见 claude/todo.md"哪些识别换成自己训练的小模型"。

训练要的 PyTorch（显卡版）装在单独的 .venv-train 里，只在开发电脑上，不进 Evo 自己的环境、不进安装包。
运行都用 Evo 自己的 Python：训练时才把 .venv-train 里的库加进来（use_train_env；用 .venv-train 自己的 python.exe
在这台电脑上写不了 captures/，2026-11-21 试过）：
    python tools/train_digits.py train       训练，完了自动考一遍考卷（另开一个进程，只用 onnxruntime，和 Evo 里一样）
    python tools/train_digits.py exam [--run 编号]   单独考一遍（考完顺便挑出要复核的块，见 mark_disputes）
    python tools/train_digits.py disputes [--run 编号]   只挑要复核的块
装训练环境（只要一次）：Python 3.13 建 .venv-train，pip install torch==2.14.1 --index-url https://download.pytorch.org/whl/cu130，
再装和 .venv 一样版本的 numpy、opencv-python-headless，和 onnx、onnxscript（导出用）
训练进度、考卷成绩在网页上看：python tools/train_web.py

训练用哪些小块、答案从哪来（training_rows）：考卷一块都不用；用户核对过的用用户的答案（最要紧，抽得最多）；
规则核对过的用引擎的读数；同一个读数多出来的，同一个视频、同一项里有核对过的同一个读数（用户没改过它）才借它的答案。
丢掉的不用。留一个视频不训练（VAL_SOURCE_HINT），每一轮用它看分数、挑最好的一轮和"把握"的门槛。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

import train_data as data

CHARS = "0123456789+-"  # 类别 0 是 CTC 的"空"，1..12 是这些字
H, W = 32, 96           # 小模型看的图：高 32、宽 96（宽 / 4 = 24 个时间步，最多读 6 位足够）
MODELS = data.TRAIN / "models"
EVO_PYTHON = data.ROOT / ".venv" / "Scripts" / "python.exe"
VAL_SOURCE_HINT = "2026-11-21_"  # 留着不训练、拿来挑最好一轮的视频（Evo 11-21 那局录像）；找不到就用最后一个视频
SHARES = {"human": 0.3, "auto": 0.15, "extra": 0.55}  # 每一轮抽多少成：用户核对过的、规则核对过的、借答案的


# ---------- 两个环境都用到的（只用 numpy、cv2） ----------

CHANNELS = 2  # 小模型看几张图：1 = 只有灰度（第 1–3 次训练）；2 = 灰度 + 每个点最亮的那个颜色
# （红字、紫字转成灰度和灰底差不多暗，第 3 次训练读不出"-48 RP"这种；在"最亮的颜色"里它们是亮的）


def prepare(img, channels=CHANNELS):
    """小块（BGR 或灰度，原分辨率）→ 小模型的输入：(channels, H, W)，0–1 的 float32。
    按高缩放，宽不够在右边补边上的颜色，太宽就压窄；每一张再按这一块自己的亮暗拉开对比度（背景、压缩、HDR 都不一样）。"""
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    planes = [cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)]
    if channels == 2:
        planes.append(img.max(axis=2))
    h, w = img.shape[:2]
    new_w = max(1, min(W, round(w * H / h)))
    out = []
    for plane in planes:
        small = cv2.resize(plane, (new_w, H), interpolation=cv2.INTER_AREA if h > H else cv2.INTER_LINEAR)
        if new_w < W:
            small = cv2.copyMakeBorder(small, 0, 0, 0, W - new_w, cv2.BORDER_REPLICATE)
        x = small.astype(np.float32) / 255.0
        lo, hi = np.percentile(x, 2), np.percentile(x, 98)
        out.append(np.clip((x - lo) / max(hi - lo, 0.1), 0.0, 1.0))
    return np.stack(out)


def run_channels(info_path):
    """这一次训练的小模型看几张图（老的没写 = 1）。"""
    try:
        return json.loads(Path(info_path).read_text(encoding="utf-8")).get("channels", 1)
    except (OSError, ValueError):
        return 1


def decode(probs):
    """CTC 贪心解码：probs 是 (时间步, 类别) 的概率。返回 (读出的字, 把握)。
    把握：读出的每个字最高的那一刻的概率，取最小的；读出空的时候，取每一刻"空"的概率里最小的。"""
    best = probs.argmax(1)
    text, conf, prev = [], [], 0
    for t, k in enumerate(best):
        if k != 0 and k != prev:
            text.append(CHARS[k - 1])
            conf.append(float(probs[t, k]))
        elif k != 0 and k == prev:
            conf[-1] = max(conf[-1], float(probs[t, k]))
        prev = k
    if text:
        return "".join(text), min(conf)
    return "", float(probs[:, 0].min())


def field_ranges():
    """每一项读数的范围（config/hud.json，和 Evo 一样）。"""
    top = json.loads((data.ROOT / "config" / "hud.json").read_text(encoding="utf-8"))["top_right"]
    ranges = {name: spec["range"] for name, spec in top["fields"].items()}
    ranges["damage"] = top["damage"]["range"]
    for name in data.KP_ROW:
        ranges[name] = top["kp_row"]["range"]
    return ranges


def model_value(text, score, field, threshold, ranges):
    """小模型读出的字 → Evo 会采用的读数（和引擎一样的规矩：格式、范围对，把握到门槛）。
    返回 (读数或 None, 是不是"有把握地读出空的")。只有 RP 可以带 + -。"""
    if score < threshold:
        return None, False
    if text == "":
        return None, True
    sign = "[+-]?" if field == "rp_delta" else ""
    if not re.fullmatch(sign + r"\d{1,5}", text):
        return None, False
    low, high = ranges[field]
    return (text if low <= int(text) <= high else None), False


def label_text(answer, field):
    """答案 → 训练用的字：去掉逗号空格；RP 的正数补上"+"（游戏里写"+19 RP"），0 不带号。"""
    text = str(answer).replace(",", "").replace(" ", "")
    if field == "rp_delta" and text and text[0] not in "+-" and text != "0" and text.isdigit():
        text = "+" + text
    return text


def load_crop(rel):
    """小块原图（彩色 BGR）。"""
    return cv2.imdecode(np.fromfile(str(data.CROPS / rel), dtype=np.uint8), cv2.IMREAD_COLOR)


# ---------- 训练用哪些小块 ----------

def training_rows(conn):
    """[{id, img, label, group, source, field}]：group 是 human / auto / extra（见文件开头）。"""
    rows = conn.execute("select id, source, field, img, engine_value, pile, status, answer from crops "
                        "where kind='video' and status!='skip'").fetchall()
    trusted, wrong = defaultdict(set), defaultdict(set)  # (来源, 项) → 可以借答案的读数 / 用户改过的读数
    for r in rows:
        key = (r["source"], r["field"])
        if r["status"] == "done":
            same = data.number(r["answer"]) == data.number(r["engine_value"])
            if r["engine_value"] is not None:
                (trusted if same else wrong)[key].add(r["engine_value"])
        elif r["pile"] == "auto":
            trusted[key].add(r["engine_value"])
    out = []
    for r in rows:
        key = (r["source"], r["field"])
        if r["status"] == "done":
            label, group = r["answer"], "human"
        elif r["pile"] == "auto":
            label, group = r["engine_value"], "auto"
        elif r["pile"] == "extra" and r["engine_value"] in trusted[key] and r["engine_value"] not in wrong[key]:
            label, group = r["engine_value"], "extra"
        else:
            continue  # "要人看"还没核对的、借不到答案的
        out.append({"id": r["id"], "img": r["img"], "label": label_text(label, r["field"]), "group": group,
                    "source": r["source"], "field": r["field"]})
    return out


def val_source(conn):
    rows = conn.execute("select id, name from sources where kind='video' and status='done' order by id").fetchall()
    for r in rows:
        if r["name"].startswith(VAL_SOURCE_HINT):
            return r["id"], r["name"]
    return (rows[-1]["id"], rows[-1]["name"]) if rows else (None, None)


# ---------- 训练时把图弄"坏"一点，见过各种画质 ----------

def augment(color, rng):
    """彩色小块（BGR）弄"坏"一点：裁偏、横向拉宽压窄、低清、模糊、放大、亮度对比度、噪点、换颜色、反色、JPG 压缩。"""
    h, w = color.shape[:2]
    img = color.astype(np.float32)
    if rng.random() < 0.7:  # 裁的位置偏一点：每边多留一点或少裁一点（少裁最多 4%，免得切掉数字）
        def side(n):
            return int(round(n * rng.uniform(-0.04, 0.12)))
        top, bottom, left, right = side(h), side(h), side(w), side(w)
        y0, y1 = max(0, -top), h - max(0, -bottom)
        x0, x1 = max(0, -left), w - max(0, -right)
        img = img[y0:y1, x0:x1]
        img = cv2.copyMakeBorder(img, max(0, top), max(0, bottom), max(0, left), max(0, right), cv2.BORDER_REPLICATE)
    h, w = img.shape[:2]
    if rng.random() < 0.7:  # 画面比例：4:3 拉伸的字横着宽约 1.2 倍，16:9 窄约 0.9 倍（训练素材全是 16:10）
        f = float(np.exp(rng.uniform(np.log(0.8), np.log(1.4))))
        img = cv2.resize(img, (max(4, round(w * f)), h), interpolation=cv2.INTER_LINEAR)
        w = img.shape[1]
    if rng.random() < 0.5:  # 低分辨率：先缩小再放大
        f = rng.uniform(0.3, 1.0)
        small = cv2.resize(img, (max(4, round(w * f)), max(4, round(h * f))), interpolation=cv2.INTER_AREA)
        img = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
    if rng.random() < 0.25:
        img = cv2.GaussianBlur(img, (3, 3), rng.uniform(0.3, 1.0))
    if rng.random() < 0.15:  # 4K、高分辨率：放大
        f = rng.uniform(1.2, 2.0)
        img = cv2.resize(img, (round(w * f), round(h * f)), interpolation=cv2.INTER_CUBIC)
    img = img * rng.uniform(0.7, 1.3) + rng.uniform(-30, 30)  # 亮度、对比度
    if rng.random() < 0.3:
        img = img + rng.normal(0, rng.uniform(2, 8), img.shape)
    img = np.clip(img, 0, 255).astype(np.uint8)
    if rng.random() < 0.3:  # 换颜色：红字、蓝底……训练素材里的颜色不全（RP 的红字、紫字很少）
        img = np.ascontiguousarray(img[:, :, rng.permutation(3)])
    if rng.random() < 0.08:  # 反色：白底黑字（考卷里有，训练素材里几乎没有）
        img = 255 - img
    if rng.random() < 0.5:  # 压缩（视频、JPG 截图）
        q = int(rng.integers(25, 95))
        img = cv2.imdecode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])[1], cv2.IMREAD_COLOR)
    return img


EMPTY_SHARE = 0.06  # 每一批里有这么多块换成"抹掉数字"的，答案是空（训练素材里"空着"的太少，2026-11-21 是 0 块）
EMPTY_FIELDS = {"damage", "kills", "assists", "participation", "squads_left"}  # RP 那一格边框比字还亮，抹不干净，不用


def erase_digits(img):
    """把小块（彩色或灰度）里的数字抹掉、用旁边的背景补上，当作"这一块没有数字"。亮的地方太多（亮背景）或太少时抹不准，返回 None。"""
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    thr = max(150.0, float(np.percentile(gray, 85)))
    mask = (gray >= thr).astype(np.uint8) * 255
    share = float(np.mean(mask > 0))
    if not 0.03 <= share <= 0.35:
        return None
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=2)
    return cv2.inpaint(img, mask, 5, cv2.INPAINT_TELEA)


# ---------- 小模型（只在训练环境里用 PyTorch） ----------

def build_model(channels=CHANNELS):
    import torch.nn as nn

    def block(i, o, pool):
        layers = [nn.Conv2d(i, o, 3, padding=1, bias=False), nn.BatchNorm2d(o), nn.ReLU(inplace=True)]
        if pool:
            layers.append(nn.MaxPool2d(pool))
        return layers

    class DigitNet(nn.Module):
        """32×96 → 卷积（16→32→48→48→64 个通道，缩到 2×24）→ 24 个时间步，每步 128 个数 → 双向 GRU（各 48）→ 13 类。"""

        def __init__(self):
            super().__init__()
            self.cnn = nn.Sequential(*block(channels, 16, 2), *block(16, 32, 2), *block(32, 48, None),
                                     *block(48, 48, (2, 1)), *block(48, 64, (2, 1)))
            self.rnn = nn.GRU(64 * 2, 48, bidirectional=True, batch_first=True)
            self.fc = nn.Linear(96, len(CHARS) + 1)

        def forward(self, x):  # x: (N, 1, H, W)
            f = self.cnn(x)
            n, c, h, w = f.shape
            f = f.permute(0, 3, 1, 2).reshape(n, w, c * h)
            f, _ = self.rnn(f)
            return self.fc(f)  # (N, 时间步, 类别) 的分数（还没 softmax）

    return DigitNet()


def evaluate(model, items, images, ranges, device, threshold=None):
    """在 items 上读一遍（不弄坏），返回每块的 (读出的字, 把握)。"""
    import torch

    model.eval()
    out = []
    with torch.no_grad():
        for i in range(0, len(items), 512):
            chunk = items[i:i + 512]
            x = torch.from_numpy(np.stack([prepare(images[it["id"]]) for it in chunk])).to(device)
            probs = model(x).softmax(2).cpu().numpy()
            out += [decode(p) for p in probs]
    model.train()
    return out


def tally(items, reads, threshold, ranges):
    """读对、读错、没读出（和考卷成绩一样的算法：按数比，答案是空的和"没读出"算对）。"""
    right = wrong = missed = 0
    for it, (text, score) in zip(items, reads):
        value, _ = model_value(text, score, it["field"], threshold, ranges)
        answer = data.number(it["label"]) if it["label"] != "" else None
        read = data.number(value)
        if answer == read:
            right += 1
        elif read is not None:
            wrong += 1
        else:
            missed += 1
    return right, wrong, missed


def pick_threshold(items, reads, ranges):
    """在验证用的视频上挑"把握"的门槛：读错的每一块算 20 块没读出那么糟（读错会记进战绩，没读出只是留空）。"""
    best = None
    for t in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.97, 0.99]:
        right, wrong, missed = tally(items, reads, t, ranges)
        cost = wrong * 20 + missed
        if best is None or cost < best[0]:
            best = (cost, t, right, wrong, missed)
    return best[1:]


def use_train_env():
    """把 .venv-train 里的库（PyTorch）加进来；Evo 自己的环境里本来就有的（numpy、cv2，版本一样）照用。"""
    try:
        import torch  # noqa: F401
        return
    except ImportError:
        pass
    site = data.ROOT / ".venv-train" / "Lib" / "site-packages"
    if not site.is_dir():
        sys.exit("没有训练环境 .venv-train（装法见 tools/train_digits.py 开头）")
    sys.path.append(str(site))  # 放在最后：同名的库先用 Evo 自己的


def cmd_train(args):
    use_train_env()
    import torch

    conn = data.connect()
    ranges = field_ranges()
    rows = training_rows(conn)
    vid, vname = val_source(conn)
    train_items = [r for r in rows if r["source"] != vid]
    val_items = [r for r in rows if r["source"] == vid and r["group"] != "extra"]
    if not train_items or not val_items:
        sys.exit("训练用的小块不够：先运行 python tools/train_data.py videos，再在网页上核对")
    print(f"训练用 {len(train_items)} 块（" + "，".join(
        f"{g} {sum(r['group'] == g for r in train_items)}" for g in SHARES) + f"）；验证用 {vname} 的 {len(val_items)} 块",
        flush=True)
    started = time.time()
    images = {}
    with ThreadPoolExecutor(8) as pool:
        for r, img in zip(rows, pool.map(lambda r: load_crop(r["img"]), rows)):
            images[r["id"]] = img
    print(f"读好图片：{time.time() - started:.0f} 秒", flush=True)

    device = "cuda" if torch.cuda.is_available() and not args.cpu else "cpu"
    torch.manual_seed(args.seed)
    rng = np.random.default_rng(args.seed)
    model = build_model().to(device)
    params = {"epochs": args.epochs, "per_epoch": args.per_epoch, "batch": args.batch, "lr": args.lr,
              "H": H, "W": W, "chars": CHARS, "shares": SHARES, "val": vname, "seed": args.seed,
              "params": sum(p.numel() for p in model.parameters())}
    run = conn.execute("insert into train_runs (started, status, device, params, epochs) values (?, 'running', ?, ?, ?)",
                       (datetime.now().isoformat(timespec="seconds"),
                        torch.cuda.get_device_name(0) if device == "cuda" else "CPU",
                        json.dumps(params, ensure_ascii=False), args.epochs)).lastrowid
    conn.commit()
    print(f"第 {run} 次训练：{params['params']:,} 个参数，用 {device}", flush=True)

    counts = {g: sum(r["group"] == g for r in train_items) for g in SHARES}
    weights = np.array([SHARES[r["group"]] / counts[r["group"]] for r in train_items])
    weights /= weights.sum()
    steps = args.per_epoch // args.batch
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=args.lr, total_steps=args.epochs * steps, pct_start=0.15)
    ctc = torch.nn.CTCLoss(blank=0, zero_infinity=True)
    best = None
    MODELS.mkdir(parents=True, exist_ok=True)
    best_path = MODELS / f"run{run}.pt"

    def make_batch(idx, seed):
        local = np.random.default_rng(seed)
        imgs, labels = [], []
        for i in idx:
            it = train_items[i]
            img, label = images[it["id"]], it["label"]
            if it["field"] in EMPTY_FIELDS and local.random() < EMPTY_SHARE:
                erased = erase_digits(img)
                if erased is not None:
                    img, label = erased, ""
            imgs.append(prepare(augment(img, local)))
            labels.append(label)
        targets = [CHARS.index(ch) + 1 for label in labels for ch in label]
        return np.stack(imgs), targets, [len(label) for label in labels]

    pool = ThreadPoolExecutor(8)
    try:
        for epoch in range(1, args.epochs + 1):
            t0 = time.time()
            batches = [rng.choice(len(train_items), size=args.batch, p=weights) for _ in range(steps)]
            futures = [pool.submit(make_batch, b, int(rng.integers(1 << 31))) for b in batches[:4]]
            total = 0.0
            for s in range(steps):
                x, targets, lengths = futures[s].result()
                if s + 4 < steps:
                    futures.append(pool.submit(make_batch, batches[s + 4], int(rng.integers(1 << 31))))
                x = torch.from_numpy(x).to(device)
                logits = model(x)
                log_probs = logits.log_softmax(2).permute(1, 0, 2)  # (时间步, N, 类别)
                n, t = logits.shape[0], logits.shape[1]
                loss = ctc(log_probs, torch.tensor(targets, dtype=torch.long),
                           torch.full((n,), t, dtype=torch.long), torch.tensor(lengths, dtype=torch.long))
                opt.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
                opt.step()
                sched.step()
                total += loss.item()
            reads = evaluate(model, val_items, images, ranges, device)
            right, wrong, missed = tally(val_items, reads, 0.8, ranges)
            conn.execute("insert or replace into train_epochs (run, epoch, loss, val_acc, val_wrong, val_missed, val_n, seconds) "
                         "values (?, ?, ?, ?, ?, ?, ?, ?)",
                         (run, epoch, total / steps, right / len(val_items), wrong, missed, len(val_items), time.time() - t0))
            conn.commit()
            # 挑最好的一轮：每一轮先挑好"把握"门槛再比（读错一块算 20 块没读出那么糟）。
            # 不能固定用 0.8 比：那样后面学得好、只是门槛该更高的几轮会被前面还没学会的一轮比下去（第 1 次训练就是这样）
            _, _, w_best, m_best = pick_threshold(val_items, reads, ranges)
            cost = w_best * 20 + m_best
            if best is None or cost <= best[0]:  # 一样好时选后面的（学得更久）
                best = (cost, epoch)
                torch.save(model.state_dict(), best_path)
            print(f"第 {epoch}/{args.epochs} 轮：loss {total / steps:.3f}，验证 读对 {right}/{len(val_items)}，"
                  f"读错 {wrong}，没读出 {missed}（{time.time() - t0:.0f} 秒）", flush=True)
    except BaseException as exc:
        conn.execute("update train_runs set status='error', finished=?, note=? where id=?",
                     (datetime.now().isoformat(timespec="seconds"), repr(exc)[:500], run))
        conn.commit()
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    model.load_state_dict(torch.load(best_path, map_location=device))
    reads = evaluate(model, val_items, images, ranges, device)
    threshold, right, wrong, missed = pick_threshold(val_items, reads, ranges)
    onnx_path = MODELS / f"run{run}.onnx"
    export_onnx(model.cpu(), onnx_path)
    (MODELS / f"run{run}.json").write_text(json.dumps({"chars": CHARS, "H": H, "W": W, "channels": CHANNELS, "threshold": threshold},
                                                      ensure_ascii=False), encoding="utf-8")
    conn.execute("update train_runs set status='done', finished=?, best_epoch=?, threshold=?, val_right=?, val_wrong=?, "
                 "val_missed=?, val_n=?, model_path=?, size_kb=? where id=?",
                 (datetime.now().isoformat(timespec="seconds"), best[1], threshold, right, wrong, missed, len(val_items),
                  str(onnx_path.relative_to(data.TRAIN)), onnx_path.stat().st_size / 1024, run))
    conn.commit()
    print(f"最好的是第 {best[1]} 轮；把握门槛 {threshold}：验证 读对 {right}，读错 {wrong}，没读出 {missed}；"
          f"导出 {onnx_path}（{onnx_path.stat().st_size / 1024:.0f} KB）。一共 {time.time() - started:.0f} 秒", flush=True)
    if not args.no_exam:
        print("用 Evo 自己的环境考一遍考卷……", flush=True)
        subprocess.run([str(EVO_PYTHON), str(Path(__file__)), "exam", "--run", str(run)], check=False)


def export_onnx(model, path):
    """导出成 ONNX，输出直接是概率（softmax 以后），一次可以读好几块。"""
    import torch

    class WithSoftmax(torch.nn.Module):
        def __init__(self, inner):
            super().__init__()
            self.inner = inner

        def forward(self, x):
            return self.inner(x).softmax(2)

    wrapped = WithSoftmax(model).eval()
    dummy = torch.zeros(1, CHANNELS, H, W)
    try:
        torch.onnx.export(wrapped, (dummy,), str(path), input_names=["x"], output_names=["probs"],
                          dynamic_axes={"x": {0: "n"}, "probs": {0: "n"}}, opset_version=17, dynamo=False)
    except Exception as exc:  # 老的导出方式没了：换新的
        print(f"（老的导出方式不行：{exc!r}，换新的）", flush=True)
        torch.onnx.export(wrapped, (dummy,), str(path), input_names=["x"], output_names=["probs"],
                          dynamic_shapes={"x": {0: torch.export.Dim("n")}}, dynamo=True)


# ---------- 用考卷考（Evo 自己的环境，onnxruntime） ----------

def cmd_exam(args):
    import onnxruntime as ort
    from process_info import memory_mb

    conn = data.connect()
    run = args.run or conn.execute("select max(id) from train_runs where status='done'").fetchone()[0]
    info = conn.execute("select * from train_runs where id=?", (run,)).fetchone()
    if info is None or not info["model_path"]:
        sys.exit("还没有训练好的小模型：先训练（见文件开头）")
    path = data.TRAIN / info["model_path"]
    ranges = field_ranges()
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = opts.inter_op_num_threads = 1  # 和 Evo 的引擎一样：1 个线程，不和游戏抢
    before, _ = memory_mb()
    session = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
    rows = conn.execute("select id, field, img from crops where kind='exam' and pile!='extra' order by id").fetchall()
    images = [load_crop(r["img"]) for r in rows]
    channels = run_channels(path.with_suffix(".json"))
    session.run(None, {"x": prepare(images[0], channels)[None]})  # 第一次慢（准备），不算
    after, _ = memory_mb()
    reads, times = [], []
    for r, img in zip(rows, images):
        t0 = time.perf_counter()
        probs = session.run(None, {"x": prepare(img, channels)[None]})[0][0]
        text, score = decode(probs)
        value, _ = model_value(text, score, r["field"], info["threshold"], ranges)
        ms = (time.perf_counter() - t0) * 1000
        times.append(ms)
        reads.append((run, r["id"], text, score, value, ms))
    conn.execute("delete from model_reads where run=?", (run,))
    conn.executemany("insert into model_reads (run, crop, text, score, value, ms) values (?, ?, ?, ?, ?, ?)", reads)
    conn.execute("update train_runs set model_ms=?, model_mem_mb=? where id=?",
                 (float(np.mean(times)), max(0.0, after - before), run))
    conn.commit()
    done = conn.execute("select c.answer, m.value, c.engine_value from crops c join model_reads m on m.crop=c.id "
                        "where m.run=? and c.status='done'", (run,)).fetchall()

    def score(column):
        right = wrong = 0
        for d in done:
            a, v = data.number(d["answer"]), data.number(d[column])
            right += a == v
            wrong += v is not None and a != v
        return right, wrong, len(done) - right - wrong

    m, e = score("value"), score("engine_value")
    print(f"考卷 {len(done)} 块：小模型 读对 {m[0]}，读错 {m[1]}，没读出 {m[2]}；"
          f"引擎 读对 {e[0]}，读错 {e[1]}，没读出 {e[2]}")
    print(f"小模型读一块平均 {np.mean(times):.2f} 毫秒（含缩放；1 个线程），加载以后多占约 {max(0.0, after - before):.0f} MB，"
          f"模型文件 {path.stat().st_size / 1024:.0f} KB")
    report_disputes(mark_disputes(conn, session, run, channels))


# ---------- 挑出要复核的块 ----------

DISPUTE_SCORE = 0.5  # 小模型至少有这么大把握、读的又和答案不一样，才挑出来复核


def mark_disputes(conn, session, run, channels=CHANNELS):
    """挑出要复核的块（网页"复核"页）：小模型读的和答案不一样、而且它有点把握（DISPUTE_SCORE）。
    比的是考卷里核对过的，和训练素材里训练要用的（用户的答案、引擎的读数、借来的答案）。核对时框里预先填着引擎的数，
    数字一糊人容易跟着认（2026-11-21 看到 538/539、585/565、清楚的"1"填成空），复核时不预先填。
    复核过的不再挑；以前挑出来、还没复核的先清掉，按这一次的小模型重挑。返回 {考卷 / 训练素材的哪一堆: 块数}。"""
    conn.execute("update crops set review=null, review_run=null where reviewed_at is null")
    done = {r["id"] for r in conn.execute("select id from crops where reviewed_at is not null")}
    items = training_rows(conn)
    items += [{"id": r["id"], "img": r["img"], "label": label_text(r["answer"], r["field"]), "group": "exam",
               "field": r["field"]}
              for r in conn.execute("select id, field, img, answer from crops where kind='exam' and status='done' "
                                    "and pile!='extra'")]
    items = [it for it in items if it["id"] not in done]
    flagged, counts = [], defaultdict(int)
    for i in range(0, len(items), 256):
        chunk = items[i:i + 256]
        probs = session.run(None, {"x": np.stack([prepare(load_crop(it["img"]), channels) for it in chunk])})[0]
        for it, p in zip(chunk, probs):
            text, score = decode(p)
            if score >= DISPUTE_SCORE and data.number(text or None) != data.number(it["label"] or None):
                flagged.append((text, run, it["id"]))
                counts[it["group"]] += 1
    conn.executemany("update crops set review=?, review_run=? where id=?", flagged)
    conn.commit()
    return dict(counts)


def report_disputes(counts):
    names = {"exam": "考卷", "human": "核对过的训练素材", "auto": "规则核对过的", "extra": "借答案的"}
    total = sum(counts.values())
    print(f"挑出 {total} 块要复核（" + "，".join(f"{names[g]} {n}" for g, n in counts.items()) + '）：网页上的"复核"页'
          if total else "没有要复核的块")


def cmd_disputes(args):
    import onnxruntime as ort

    conn = data.connect()
    run = args.run or conn.execute("select max(id) from train_runs where status='done'").fetchone()[0]
    info = conn.execute("select * from train_runs where id=?", (run,)).fetchone()
    if info is None or not info["model_path"]:
        sys.exit("还没有训练好的小模型：先训练（见文件开头）")
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 4
    path = data.TRAIN / info["model_path"]
    session = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
    report_disputes(mark_disputes(conn, session, run, run_channels(path.with_suffix(".json"))))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="训练读数字的小模型，用考卷考")
    parser.add_argument("command", choices=("train", "exam", "disputes"))
    parser.add_argument("--run", type=int, help="exam、disputes：用第几次训练的小模型（默认最新的）")
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--per-epoch", type=int, default=40000, help="每一轮抽多少块")
    parser.add_argument("--batch", type=int, default=256)
    parser.add_argument("--lr", type=float, default=3e-3)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--cpu", action="store_true", help="不用显卡")
    parser.add_argument("--no-exam", action="store_true", help="训练完不自动考")
    args = parser.parse_args()
    {"train": cmd_train, "exam": cmd_exam, "disputes": cmd_disputes}[args.command](args)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
