"""协作网页的"队标库"：把派圈助手那一批压好的战队队标传进不公开的存储桶 evo-team，网页上给战队挑队徽用（2026-11-18）。
用户定的：队徽先用派圈那一批，没有的队再在网页上手动上传（见 claude/todo.md「赛事数据管理网页」）。

    python tools/team_logos.py            检查一遍、打印要传多少，不上传
    python tools/team_logos.py --upload   上传到线上的 evo-team（logo-library/），要 wrangler（已经 wrangler login）
    加 --local：传到 wrangler dev 用的本机数据（cd server/team && wrangler dev 时试用）

图：E:\\Evo\\assets\\队标\\v1（851 个 PNG，最长边 192 像素，派圈的 tools/build_pro_logos.py 做的，不进 git）；
对照表：派圈仓库的 cloudfunctions/apex/pro_logos.json（Liquipedia 战队页面名 → 文件名）。
存成 logo-library/<图片内容 sha256 的前 16 位>.png，外加 logo-library/index.json：
    {"version": 1, "items": [{"page": Liquipedia 页面名, "name": 队名, "file": "3fa9c1d2e4b5a6f7.png", "w", "h", "bytes"}]}
文件名用内容哈希：和 Evo 读的队徽文件名同一个写法（[a-z0-9]{8,40}.png），网页挑中以后原样复制到公开存储桶的 esports/logos/。
队徽是各战队的商标，不在 Liquipedia 的 CC BY-SA 授权里（派圈那边 11-08 用户定过用这一批，这里沿用）。
"""

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import code_path  # noqa: F401

ROOT = code_path.ROOT
LOGOS = ROOT / "assets" / "队标" / "v1"
MAPPING = Path(r"E:\paiquan-miniprogram\cloudfunctions\apex\pro_logos.json")
SERVER = ROOT / "server" / "team"
BUCKET = "evo-team"
PREFIX = "logo-library/"
MAX_BYTES = 60 * 1024   # 同 server/team/logos.js
MAX_SIDE = 256


def png_size(data):
    """PNG 的宽高；不是 PNG 返回 None。"""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def build(logos, mapping):
    """→ (要传的 [(key, 本机文件)], index.json 的内容, 问题)"""
    problems, pairs, items = [], [], []
    files = json.loads(mapping.read_text(encoding="utf-8"))["files"]
    for page, name in sorted(files.items()):
        path = logos / f"{name}.png"
        if not path.exists():
            problems.append(f"对照表里有 {page}，但没有 {path.name}")
            continue
        data = path.read_bytes()
        size = png_size(data)
        if not size:
            problems.append(f"{path.name} 不是 PNG")
            continue
        if len(data) > MAX_BYTES or max(size) > MAX_SIDE:
            problems.append(f"{path.name} 太大（{len(data)} 字节，{size[0]}×{size[1]}）")
            continue
        file = hashlib.sha256(data).hexdigest()[:16] + ".png"
        pairs.append((PREFIX + file, path))
        items.append({"page": page, "name": page.replace("_", " ").strip(), "file": file, "w": size[0], "h": size[1], "bytes": len(data)})
    items.sort(key=lambda x: x["name"].lower())
    return pairs, {"version": 1, "items": items}, problems


def wrangler(*args):
    exe = shutil.which("wrangler")
    if not exe:
        sys.exit("找不到 wrangler：先装 Node.js 和 Wrangler（npm install -g wrangler），再运行 wrangler login")
    result = subprocess.run([exe, *args], cwd=SERVER, capture_output=True)
    if result.returncode != 0:
        sys.exit("wrangler 出错：\n" + (result.stderr + result.stdout).decode("utf-8", "replace")[-2000:])
    return result


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="把派圈的队标传进协作网页的队标库")
    parser.add_argument("--upload", action="store_true", help="上传（不加只检查）")
    parser.add_argument("--local", action="store_true", help="传到 wrangler dev 用的本机数据")
    parser.add_argument("--logos", default=str(LOGOS), help="队标文件夹")
    parser.add_argument("--mapping", default=str(MAPPING), help="派圈的 pro_logos.json")
    args = parser.parse_args()

    pairs, index, problems = build(Path(args.logos), Path(args.mapping))
    unique = {key: path for key, path in pairs}  # 两支队用同一张图的只传一次
    total = sum(path.stat().st_size for path in unique.values())
    print(f"队标库：{len(index['items'])} 支队、{len(unique)} 张图，共 {total / 1024 / 1024:.1f} MB")
    for problem in problems:
        print(f"  ⚠ {problem}")
    if not args.upload:
        print("没有上传（加 --upload 才传）")
        return 0
    where = "--local" if args.local else "--remote"
    with tempfile.TemporaryDirectory() as folder:
        pairs_file = Path(folder) / "pairs.json"
        pairs_file.write_text(json.dumps([{"key": key, "file": str(path)} for key, path in unique.items()]), encoding="utf-8")
        print(f"上传 {len(unique)} 张图……")
        wrangler("r2", "bulk", "put", BUCKET, "--filename", str(pairs_file), "--content-type", "image/png", where)
        index_file = Path(folder) / "index.json"
        index_file.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        wrangler("r2", "object", "put", f"{BUCKET}/{PREFIX}index.json", "--file", str(index_file),
                 "--content-type", "application/json; charset=utf-8", where)
    print(f"传好了：{BUCKET}/{PREFIX}（{len(unique)} 张图 + index.json）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
