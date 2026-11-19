"""弹幕（code/danmaku.py 和面板的 /esports/danmaku/<选手>）：不联网。

- 斗鱼：从房间信息里拿真实房间号（外面包几层都认）；弹幕服务器的地址照配置给，必须是 wss://
- 没做的平台、房间号写得不对：不给
- 面板的接口：只给能在窗口里用 Evo 自己的播放器播、弹幕做了的选手；平台那边没问到说明原因
- 看直播页给脚本的数据里，只有这样的选手带着问弹幕的地址；页面上有弹幕的开关和连斗鱼的脚本

运行：python tests/test_danmaku.py
"""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import danmaku
import esports_data
import live_status
import panel_app
import panel_esports
import storage

NOW = datetime.now().astimezone().replace(microsecond=0)


def body(room):
    return json.dumps(room).encode("utf-8")


def main():
    print("弹幕（斗鱼：真实房间号、面板的接口、页面）")
    out = {}
    out["斗鱼：从房间信息里拿真实房间号，外面包几层都认；没有、不是数字、不是 JSON 就是没有"] = (
        danmaku.douyu_room(body({"room": {"room_id": 9263298}})) == "9263298"
        and danmaku.douyu_room(body({"data": {"room": {"room_id": "52"}}})) == "52"
        and danmaku.douyu_room(body({"data": {"room_id": 7}})) == "7" and danmaku.douyu_room(body({"room_id": 8})) == "8"
        and danmaku.douyu_room(body({"room": {"room_id": "abc"}})) is None and danmaku.douyu_room(body({"room": {}})) is None
        and danmaku.douyu_room(b"<html>") is None and danmaku.douyu_room(body([1, 2])) is None)
    asked = []

    def get(url, headers):
        asked.append((url, headers))
        return body({"room": {"room_id": 1234567, "show_status": 1}})

    found = danmaku.info("douyu", "88", get)
    out["斗鱼：问一次房间信息（和查开播状态同一个地址），回弹幕服务器的地址（照配置）和真实房间号"] = (
        found == {"platform": "douyu", "url": live_status.PLATFORMS["douyu"]["danmaku_url"], "room": "1234567"}
        and found["url"].startswith("wss://") and len(asked) == 1 and asked[0][0] == "https://www.douyu.com/betard/88"
        and "Cookie" not in asked[0][1])
    count = len(asked)
    try:
        danmaku.info("douyu", "88", lambda url, headers: body({"error": 1}))
        refused = False
    except live_status.StatusError:
        refused = True
    out["没做的平台、房间号写得不对：不给，也不去问；斗鱼回的数据里没有房间号：说明原因"] = (
        danmaku.info("huya", "88", get) is None and danmaku.info("bilibili", "88", get) is None and danmaku.info("douyin", "88", get) is None
        and danmaku.info("douyu", "8x8", get) is None and len(asked) == count and refused
        and danmaku.supported("douyu") and not danmaku.supported("huya") and not danmaku.supported("nope"))
    real_url = live_status.PLATFORMS["douyu"]["danmaku_url"]
    live_status.PLATFORMS["douyu"]["danmaku_url"] = "http://example.invalid"
    try:
        out["配置里的地址不是 wss://：不给"] = danmaku.douyu("88", get) is None
        live_status.PLATFORMS["douyu"]["danmaku_url"] = ""
        closed = not danmaku.supported("douyu")
    finally:
        live_status.PLATFORMS["douyu"]["danmaku_url"] = real_url
    out["配置里把地址去掉：这个平台就没有弹幕"] = closed

    # ---- 面板 ----
    folder = tempfile.mkdtemp(prefix="evo-danmaku-")
    db = Path(folder) / "evo.db"
    storage.connect(db).close()
    panel_app.app.config["DB"] = str(db)
    client = panel_app.app.test_client()
    players = [{"id": "douyu-88", "name": "示例选手1", "team": "示例战队", "platform": "douyu", "room": "88"},
               {"id": "huya-89", "name": "示例选手2", "team": "示例战队", "platform": "huya", "room": "89"},
               {"id": "bilibili-90", "name": "示例选手3", "team": "示例战队", "platform": "bilibili", "room": "90"}]
    events = [{"id": "live", "name": "示例赛", "type": "联赛", "stage": "", "start": (NOW - timedelta(hours=1)).isoformat(),
               "end": (NOW + timedelta(hours=3)).isoformat(), "teams": ["示例战队"]}]

    def serve(platforms=None):
        path = Path(folder) / "esports.json"
        path.write_text(json.dumps({"version": 1, "updated": NOW.isoformat(), "players": players, "events": events,
                                    "platforms": platforms or {}}), encoding="utf-8")
        esports_data.URL = str(path)
        esports_data.CACHE_PATH.unlink(missing_ok=True)

    serve()
    page = client.get("/esports/live/watch").get_data(as_text=True)
    start = page.find('<script type="application/json" id="watch-data">') + len('<script type="application/json" id="watch-data">')
    data = json.loads(page[start:page.find("</script>", start)])
    urls = {p["id"]: p["danmaku"] for p in data["players"]}
    out["看直播页给脚本的数据：只有弹幕做了、能用 Evo 自己的播放器播的选手带着问弹幕的地址"] = (
        urls == {"douyu-88": "/esports/danmaku/douyu-88", "huya-89": None, "bilibili-90": None})
    out["页面上有弹幕的开关和连斗鱼的脚本：弹幕只当纯文字显示，那一层不挡点击、不让读屏软件念"] = (
        "watch-danmaku-btn" in page and "function douyuPacket(text)" in page and "type@=joingroup/rid@=" in page
        and 'el("span", "watch-danmaku-item", String(text).slice(0, 60))' in page and 'layer.setAttribute("aria-hidden", "true")' in page)
    real_info = danmaku.info
    danmaku.info = lambda platform, room, get=None: {"platform": platform, "url": "wss://example.invalid:1", "room": "1234567"}
    try:
        good = client.get("/esports/danmaku/douyu-88")
        missing = [client.get(f"/esports/danmaku/{pid}").status_code for pid in ("huya-89", "bilibili-90", "nobody-1")]

        def broken(platform, room, get=None):
            raise live_status.StatusError("连不上斗鱼")

        danmaku.info = broken
        failed = client.get("/esports/danmaku/douyu-88").get_json()
        serve({"douyu": False})
        client.get("/esports")
        switched = client.get("/esports/danmaku/douyu-88").status_code
    finally:
        danmaku.info = real_info
    out["面板的接口：给地址和真实房间号；没做的平台、名单外的人 404；平台那边没问到说明原因；平台开关关了也 404"] = (
        good.get_json() == {"ok": True, "platform": "douyu", "url": "wss://example.invalid:1", "room": "1234567"}
        and missing == [404, 404, 404] and failed == {"ok": False, "reason": "upstream", "detail": "连不上斗鱼"} and switched == 404)
    panel_esports._status.update(at=0.0, found={})
    return report(out)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
