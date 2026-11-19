"""核对统计服务器"全体玩家"的汇总：从 matches 表按 server/stats/worker.js 开头写的规则独立算一遍，和 Worker 出的汇总逐项比。
不用 SQL 聚合、不用 worker.js 的代码，四舍五入照 JS 的 Math.round。改了汇总的算法以后用（2026-11-19 增量算法上线前就是这样核对的，
见 docs/底座.md"匿名统计"）。只在开发电脑上用，不碰线上。

用法：python tools/summary_check.py 数据库.sqlite 汇总.json
  数据库.sqlite：有 matches 表的 SQLite 文件（线上数据库导出后导进本机的副本，见 docs/底座.md"部署、改服务器"）
  汇总.json：同一份数据算出来的 summary 表 v1 那一行的 body。"近 7 天"按它的 made_at 那天算，本赛季从它的 season_from 算起
"""
import datetime
import json
import math
import sqlite3
import sys
from collections import defaultdict

c = sqlite3.connect(sys.argv[1])
c.row_factory = sqlite3.Row
rows = c.execute('select * from matches').fetchall()
target = json.load(open(sys.argv[2], encoding='utf-8'))
today = datetime.date.fromisoformat(target['made_at'][:10])
recent_from = (today - datetime.timedelta(days=6)).isoformat()
SEASON_FROM, TOP, MINP, MAXP, SHARE = target['season_from'], 30, 3, 201, 0.05
TIER_ORDER = ["菜鸟", "青铜", "白银", "黄金", "白金", "钻石", "大师", "Apex 猎杀者"]


def group(t):
    return 'low' if t in TIER_ORDER[:5] else 'diamond' if t == '钻石' else 'master' if t in ('大师', 'Apex 猎杀者') else None


def jsround(x, d):  # JS 的 Math.round(x * f) / f
    f = 10 ** d
    return math.floor(x * f + 0.5) / f


def ratio(a, b, d):
    return jsround(a / b, d) if b else None


def points(vals, d):
    s = sorted(jsround(v, d) for v in vals)
    if len(s) <= MAXP:
        return s
    return [s[math.floor(i * (len(s) - 1) / (MAXP - 1) + 0.5)] for i in range(MAXP)]


def in_range(r, day):
    return r == 'all' or (day is not None and day >= (recent_from if r == '7d' else SEASON_FROM))


def ok(x):
    return x['review'] == 0


out = {}
for r in ('all', '7d', 'season'):
    for key in ('ranked.all', 'ranked.low', 'ranked.diamond', 'ranked.master', 'pubs.all'):
        m, g = key.split('.')
        ms = [x for x in rows if in_range(r, x['day']) and ((x['mode'] == 'ranked') == (m == 'ranked'))
              and (g == 'all' or group(x['tier']) == g)]
        L = defaultdict(lambda: [0] * 9)
        for x in ms:
            for name, k, d in ((x['legend'], x['kills'], x['damage']), (x['mate1'], x['mate1_kills'], x['mate1_damage']),
                               (x['mate2'], x['mate2_kills'], x['mate2_damage'])):
                if name is None:
                    continue
                v = L[name]
                v[0] += 1
                if ok(x) and x['placement'] is not None:
                    v[1] += 1; v[2] += x['placement']; v[3] += x['placement'] == 1; v[4] += x['placement'] <= 5
                if ok(x) and k is not None:
                    v[5] += 1; v[6] += k
                if ok(x) and d is not None:
                    v[7] += 1; v[8] += d
        picks = sum(v[0] for v in L.values())
        legends = [{'name': n, 'n': v[0], 'pick': ratio(v[0], picks, 4), 'win': ratio(v[3], v[1], 4), 'top5': ratio(v[4], v[1], 4),
                    'place': ratio(v[2], v[1], 2), 'kills': ratio(v[6], v[5], 2), 'damage': ratio(v[8], v[7], 0)}
                   for n, v in sorted(L.items(), key=lambda kv: (-kv[1][0], kv[0]))]
        # 组合：并列时按原来 SQL 出来的先后（段位组 NULL<diamond<low<master、近 7 天、本赛季 NULL<0<1 里最早出现的，再按名字）
        C = {}
        for x in ms:
            t = (x['legend'], x['mate1'], x['mate2'])
            if None in t or len(set(t)) < 3:
                continue
            names = tuple(sorted(t))
            dated = x['day'] is not None
            order = ({None: 0, 'diamond': 1, 'low': 2, 'master': 3}[group(x['tier'])],
                     (1 + (x['day'] >= recent_from)) if dated else 0, (1 + (x['day'] >= SEASON_FROM)) if dated else 0)
            v = C.setdefault(names, [0, 0, 0, order])
            v[0] += 1
            if ok(x) and x['placement'] is not None:
                v[1] += 1; v[2] += x['placement']
            v[3] = min(v[3], order)
        squads = sum(v[0] for v in C.values())
        cl = sorted(C.items(), key=lambda kv: (-kv[1][0], ratio(kv[1][2], kv[1][1], 2) if kv[1][1] else 99, kv[1][3], kv[0]))
        combos = [{'legends': list(n), 'n': v[0], 'rate': ratio(v[0], squads, 4), 'place': ratio(v[2], v[1], 2)} for n, v in cl[:TOP]]
        P = defaultdict(lambda: [0] * 14)
        pool = defaultdict(lambda: defaultdict(int))
        for x in ms:
            p = P[x['id']]
            p[0] += 1
            if ok(x) and x['placement'] is not None:
                p[1] += 1; p[2] += x['placement']; p[3] += x['placement'] == 1; p[4] += x['placement'] <= 5
            for i, col in ((5, 'kills'), (7, 'assists'), (9, 'damage')):
                if ok(x) and x[col] is not None:
                    p[i] += 1; p[i + 1] += x[col]
            if ok(x) and None not in (x['kills'], x['assists'], x['mate1_kills'], x['mate2_kills']):
                p[11] += 1; p[12] += x['kills'] + x['assists']; p[13] += x['kills'] + x['mate1_kills'] + x['mate2_kills']
            if x['legend'] is not None:
                pool[x['id']][x['legend']] += 1
        ids = list(P.values())
        sizes = []
        for counts in pool.values():
            tot = sum(counts.values())
            if tot >= MINP:
                sizes.append(sum(1 for n in counts.values() if n / tot >= SHARE))
        W = defaultdict(lambda: [0, 0])
        wg = 0
        for x in ms:
            if x['weapons'] is None:
                continue
            wg += 1
            for k, s in json.loads(x['weapons']).items():
                W[k][0] += 1; W[k][1] += s
        held = sum(v[1] for v in W.values())
        en = lambda n: n >= MINP
        out[f'{r}.{key}'] = {
            'players': len(ids), 'matches': sum(p[0] for p in ids), 'legends': legends, 'combos': combos,
            'people': {'place': points([p[2] / p[1] for p in ids if en(p[1])], 2), 'win': points([p[3] / p[1] for p in ids if en(p[1])], 4),
                       'kills': points([p[6] / p[5] for p in ids if en(p[5])], 2), 'damage': points([p[10] / p[9] for p in ids if en(p[9])], 0),
                       'assists': points([p[8] / p[7] for p in ids if en(p[7])], 2), 'top5': points([p[4] / p[1] for p in ids if en(p[1])], 4),
                       'kp': points([min(1, p[12] / p[13]) for p in ids if en(p[11]) and p[13] > 0], 4), 'pool': points(sizes, 0)},
            'weapon_games': wg,
            'weapons': [{'key': k, 'n': v[0], 'share': ratio(v[1], held, 4), 'rate': ratio(v[0], wg, 4)}
                        for k, v in sorted(W.items(), key=lambda kv: (-kv[1][1], kv[0]))],
        }

latest = {}
for x in rows:
    if x['mode'] == 'ranked' and x['tier'] is not None and x['day'] is not None:
        d, ts = latest.get(x['id'], ('', set()))
        if x['day'] > d:
            latest[x['id']] = (x['day'], {x['tier']})
        elif x['day'] == d:
            ts.add(x['tier'])
tiers = {}
for r, frm in (('all', ''), ('7d', recent_from), ('season', SEASON_FROM)):
    counts = defaultdict(int)
    for d, ts in latest.values():
        if d < frm:
            continue
        rank = lambda t: TIER_ORDER.index(t) if t in TIER_ORDER else -1
        counts[sorted(ts, key=lambda t: (-rank(t), t))[0]] += 1
    tiers[r] = {'players': sum(counts.values()), 'counts': dict(counts)}

mine = {'recent_from': recent_from, 'season_from': SEASON_FROM, 'slices': out, 'tiers': tiers}
theirs = {k: target[k] for k in mine}
diffs = []
leaves = [0]


def walk(x, y, path):
    if len(diffs) > 15:
        return
    if isinstance(x, dict) and isinstance(y, dict):
        for k in set(x) | set(y):
            if k not in x or k not in y:
                diffs.append(f'{path}.{k} 只在一边')
            else:
                walk(x[k], y[k], f'{path}.{k}')
    elif isinstance(x, list) and isinstance(y, list):
        if len(x) != len(y):
            diffs.append(f'{path} 长度 {len(x)} ≠ {len(y)}')
        for i, (u, v) in enumerate(zip(x, y)):
            walk(u, v, f'{path}[{i}]')
    else:
        leaves[0] += 1
        if x != y or isinstance(x, bool) != isinstance(y, bool):
            diffs.append(f'{path}: Python {x!r} ≠ Worker {y!r}')


walk(mine, theirs, '$')
print('\n'.join(diffs) if diffs else f'独立算的和 {sys.argv[2]} 逐项一样（{len(rows)} 局，{leaves[0]} 个值）')
sys.exit(1 if diffs else 0)
