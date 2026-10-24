"""从一张真实截图切出三条提示的参照图（只在开发时跑，不进记录流程）。

    python paimiao_cloud/cloud/legend_pick/make_templates.py <截图> [--write]

不写 `--write` 时只打印三条的相似度（拿这张图自己的对应区域比，应该都在 0.9 以上），
用来核对坐标改对了没有。写的话生成 templates/legendpick_<编号>.png。

参照图全部是**轮廓图**（笔画边缘），不是原图：不比明暗，只比形状。
同一张图里切出来的，所以 1.00 是自比；拿别的截图比会低一些（留了阈值，见 rules.py）。
"""
from __future__ import annotations

import pathlib
import sys

import cv2
import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "code"))

from layout import cut            # noqa: E402  上游的裁切（基准坐标 → 截图上的像素）
from ocr import load_image        # noqa: E402  上游的读图（兼容中文路径）

from . import rules               # noqa: E402

HERE = pathlib.Path(__file__).parent
TEMPLATES = HERE / "templates"


def prep(img, roi, anchor=None):
    """按基准坐标裁一块，缩到基准大小，再取轮廓（= 运行时认画面时的同一条路）。"""
    crop = cut(img, roi, rules.BASE, anchor or rules.ANCHOR)
    if crop is None or crop.size == 0 or crop.shape[0] < 4 or crop.shape[1] < 4:
        return None
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    # 先缩回 1920 基准的大小再做轮廓：直接在 2560 原图上取边缘，笔画又细又碎，
    # 和缩小的参照图对不上（实测 0.23 vs 0.92，见 rules.py 的说明）
    gray = cv2.resize(gray, (roi[2], roi[3]), interpolation=cv2.INTER_AREA)
    return rules.outline(gray)


def score(img, key):
    """这张图里第 key 条提示的相似度（在 ±SEARCH_PAD 里滑，取最高）。"""
    roi = rules.PROMPTS[key]
    pad = rules.SEARCH_PAD
    search = [roi[0] - pad, roi[1] - pad, roi[2] + 2 * pad, roi[3] + 2 * pad]
    region = prep(img, search)
    template = _load(key)
    if region is None or template is None:
        return 0.0
    if region.shape[0] < template.shape[0] or region.shape[1] < template.shape[1]:
        return 0.0
    result = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
    return float(result.max())


def _load(key):
    name = rules.TEMPLATE_PREFIX + key + ".png"
    path = TEMPLATES / name
    if not path.is_file():
        return None
    data = np.fromfile(str(path), dtype=np.uint8)      # 兼容中文路径
    return cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    path = pathlib.Path(argv[0])
    img = load_image(path)
    if img is None:
        print(f"读不了：{path}")
        return 1
    write = "--write" in argv
    if not write:
        for key in rules.PROMPTS:
            print(f"{key:8s} {score(img, key):.3f}")
        return 0
    TEMPLATES.mkdir(parents=True, exist_ok=True)
    for key in rules.PROMPTS:
        template = prep(img, rules.PROMPTS[key])
        if template is None:
            print(f"{key}: 裁不到")
            continue
        name = rules.TEMPLATE_PREFIX + key + ".png"
        ok, buf = cv2.imencode(".png", template)
        if not ok:
            print(f"{key}: 存不了")
            continue
        buf.tofile(str(TEMPLATES / name))       # 兼容中文路径
        print(f"写了 {name} {template.shape[1]}x{template.shape[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
