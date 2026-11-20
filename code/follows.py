"""电竞页的"关注"：玩家在 Evo 里关注的选手、主播（只是 Evo 自己的名单，和直播平台上的关注没有关系），和要不要开播通知。

存在数据文件夹的 settings.json 里（不改数据库），只在这台电脑上，不上传：
- esports_follows：关注的人的 id（esports.json 里选手、主播的 id），照关注的先后，最多 MAX 位
- esports_follow_notify：关注的主播开播时要不要用 Windows 通知提醒。None = 还没问过（第一次点"关注"时页面问一次），
  True / False = 玩家答过了（电竞页"我关注的"那一块和设置页随时能改）。只有 True 时托盘里的 Evo 才在后台查开播
  （follow_alerts.py、evo_app.start_follow_checker）

2026-11-20 用户要的，规则见 docs/面板.md"关注"。
"""

import app_settings
import esports_data

KEY = "esports_follows"
NOTIFY_KEY = "esports_follow_notify"
MAX = 100  # 最多关注几位（名单一共一百多人；防止设置文件被写得很大）


def ids():
    """关注的人的 id，照关注的先后。设置文件里写得不对的那几项不算。"""
    saved = app_settings.get(KEY)
    if not isinstance(saved, list):
        return []
    out = []
    for item in saved:
        if isinstance(item, str) and esports_data.ID_RE.fullmatch(item) and item not in out:
            out.append(item)
    return out[:MAX]


def set_following(player_id, on):
    """关注 / 取消关注一位，返回现在关注的 id。id 的样子不对、已经满了（MAX 位）时不加。"""
    now = ids()
    if on and player_id not in now and len(now) < MAX and esports_data.ID_RE.fullmatch(str(player_id)):
        now.append(player_id)
    elif not on and player_id in now:
        now.remove(player_id)
    else:
        return now
    app_settings.set_value(KEY, now)
    return now


def notify():
    """开播通知：True 开着 / False 关着 / None 还没问过玩家。"""
    saved = app_settings.get(NOTIFY_KEY)
    return saved if isinstance(saved, bool) else None


def set_notify(on):
    app_settings.set_value(NOTIFY_KEY, bool(on))
    return bool(on)


def players(data, streaming_only=False):
    """关注的人里现在还在名单上的（赛事数据里的选手、另外加的主播），照关注的先后；streaming_only：只要开直播的。"""
    if not data:
        return []
    known = {item["id"]: item for item in [*(data.get("streamers") or []), *(data.get("players") or [])]}
    found = [known[pid] for pid in ids() if pid in known]
    return esports_data.streaming(found) if streaming_only else found


def events(data, now=None):
    """正在进行、即将开始的赛事里，有关注的人参赛（或在观战）的那几场：[(赛事, [关注的人, ...]), ...]，
    正在进行的在前，再照开始时间。没有关注任何人、没有赛事数据返回 []。"""
    wanted = set(ids())
    if not data or not wanted:
        return []
    found = esports_data.groups(data, now)
    out = []
    for event in found[esports_data.LIVE] + found[esports_data.UPCOMING]:
        mine = [player for player in esports_data.event_players(data, event) if player["id"] in wanted]
        if mine:
            out.append((event, mine))
    return out
