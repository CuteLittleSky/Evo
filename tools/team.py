"""协作网页 team.evotracker.cn 的成员（命令行）：生成第一个管理员的邀请链接，管理员自己进不来时也用它。
平常在网页「成员」页操作就行。改的是 R2 存储桶 evo-team 里的 members.json（格式见 server/team/auth.js 开头），
要 wrangler（已经 wrangler login）。

    python tools/team.py invite 名字                  新成员的邀请链接（默认管理员；--role editor / viewer，编辑加 --publish 能发布）
    python tools/team.py link 名字                    已有成员换了手机、电脑：再要一个链接（进去以后还是原来的身份）
    python tools/team.py list                         看有哪些成员、还没用的邀请
    加 --local：改 wrangler dev 用的本机数据（cd server/team && wrangler dev 时试用，链接是 http://localhost:8787/…）

链接只打印这一次（存的只是口令的 SHA-256），7 天内有效，打开以后点"进入"就作废。
这里改文件不做"同时有人在改"的检查（网页那边做了）：网页上有人正在改成员的那一刻别运行。
"""

import argparse
import hashlib
import json
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import code_path  # noqa: F401  先把 code/ 加进搜索路径（见 code_path.py）

SERVER = code_path.ROOT / "server" / "team"
BUCKET = "evo-team"
KEY = "members.json"
SITE = "https://team.evotracker.cn"
LOCAL_SITE = "http://localhost:8787"
ROLES = {"admin": "管理员", "editor": "编辑", "viewer": "只读"}
INVITE_DAYS = 7
NAME_MAX = 20
DAY_MS = 24 * 3600 * 1000
BEIJING = timezone(timedelta(hours=8))


def wrangler(*args):
    exe = shutil.which("wrangler")
    if not exe:
        sys.exit("找不到 wrangler：先装 Node.js 和 Wrangler（npm install -g wrangler），再运行 wrangler login")
    # 在 server/team 里运行：--local 时和 wrangler dev 用同一份本机数据（server/team/.wrangler/）
    return subprocess.run([exe, *args], cwd=SERVER, capture_output=True)


def where(local):
    return "--local" if local else "--remote"


def read(local):
    result = wrangler("r2", "object", "get", f"{BUCKET}/{KEY}", "--pipe", where(local))
    if result.returncode != 0:
        message = (result.stderr + result.stdout).decode("utf-8", "replace")
        if re.search(r"does not exist|not found|NoSuchKey", message, re.I):
            return {"version": 1, "members": [], "invites": []}  # 刚部署，还没有人
        sys.exit(f"读不到成员文件 {BUCKET}/{KEY}：\n{message}")
    data = json.loads(result.stdout.decode("utf-8"))
    data.setdefault("members", [])
    data.setdefault("invites", [])
    return data


def write(data, local):
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / KEY
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        result = wrangler("r2", "object", "put", f"{BUCKET}/{KEY}", "--file", str(path),
                          "--content-type", "application/json; charset=utf-8", where(local))
    if result.returncode != 0:
        sys.exit("写不进去：\n" + (result.stderr + result.stdout).decode("utf-8", "replace"))


def clean_name(value):
    name = " ".join(re.sub(r"[\x00-\x1f\x7f]", "", value).split())
    if not name:
        sys.exit("名字不能空着")
    if len(name) > NAME_MAX:
        sys.exit(f"名字最多 {NAME_MAX} 个字")
    return name


def add_invite(data, now, fields):
    """加一个邀请，返回链接里的口令（原文只在这里出现一次）。格式同 server/team/auth.js 的 createInvite。"""
    token = secrets.token_urlsafe(24)  # 32 个字母数字，同 auth.js 的 newToken
    data["invites"] = [i for i in data["invites"] if i["expires"] > now]
    data["invites"].append({"id": secrets.token_hex(4), "h": hashlib.sha256(token.encode()).hexdigest(), **fields,
                            "created": now, "createdBy": "命令行", "expires": now + INVITE_DAYS * DAY_MS})
    return token


def show_link(token, local, name):
    site = LOCAL_SITE if local else SITE
    print(f"\n给「{name}」的链接（{INVITE_DAYS} 天内有效，打开以后点“进入”就作废）：\n\n    {site}/join/{token}\n")


def beijing(ms):
    return datetime.fromtimestamp(ms / 1000, BEIJING).strftime("%Y-%m-%d %H:%M") if ms else "还没来过"


def main():
    parser = argparse.ArgumentParser(description="协作网页 team.evotracker.cn 的成员（命令行）")
    parser.add_argument("--local", action="store_true", help="改 wrangler dev 用的本机数据")
    sub = parser.add_subparsers(dest="command", required=True)
    invite = sub.add_parser("invite", help="新成员的邀请链接")
    invite.add_argument("name")
    invite.add_argument("--role", choices=ROLES, default="admin")
    invite.add_argument("--publish", action="store_true", help="编辑也能发布到 Evo")
    link = sub.add_parser("link", help="已有成员换设备：再要一个链接")
    link.add_argument("name")
    sub.add_parser("list", help="看成员和还没用的邀请")
    args = parser.parse_args()

    now = int(time.time() * 1000)
    data = read(args.local)

    if args.command == "list":
        print(f"成员（{len(data['members'])}）：")
        for m in data["members"]:
            devices = [s["device"] for s in m.get("sessions", []) if s["expires"] > now]
            publish = "，能发布" if m["role"] == "editor" and m.get("publish") else ""
            print(f"  {m['name']}  {ROLES.get(m['role'], m['role'])}{publish}  最后来过 {beijing(m.get('lastSeen'))}"
                  f"  {len(devices)} 台设备{('（' + '、'.join(devices) + '）') if devices else ''}")
        invites = [i for i in data["invites"] if i["expires"] > now]
        print(f"还没用的邀请（{len(invites)}）：")
        for i in invites:
            kind = "换设备的新链接" if i.get("memberId") else ROLES.get(i["role"], i["role"])
            print(f"  {i['name']}  {kind}  {i['createdBy']} 发的，{beijing(i['expires'])} 过期")
        return

    if args.command == "invite":
        name = clean_name(args.name)
        if any(m["name"] == name for m in data["members"]):
            sys.exit(f"已经有一个叫「{name}」的成员了：换设备用 python tools/team.py link {name}")
        publish = args.role == "admin" or (args.role == "editor" and args.publish)
        token = add_invite(data, now, {"name": name, "role": args.role, "publish": publish})
    else:
        matches = [m for m in data["members"] if m["name"] == args.name.strip()]
        if not matches:
            sys.exit(f"没有叫「{args.name}」的成员（python tools/team.py list 看有哪些）")
        if len(matches) > 1:
            sys.exit(f"有 {len(matches)} 个叫「{args.name}」的成员：请在网页「成员」页对着要的那一个点“发新链接”")
        member = matches[0]
        data["invites"] = [i for i in data["invites"] if i.get("memberId") != member["id"]]
        token = add_invite(data, now, {"name": member["name"], "role": member["role"], "publish": member.get("publish", False),
                                       "memberId": member["id"]})
        name = member["name"]
    write(data, args.local)
    show_link(token, args.local, name)


if __name__ == "__main__":
    main()
