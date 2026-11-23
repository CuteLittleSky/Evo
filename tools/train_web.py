"""训练小模型用的网页（开发工具，只在本机）：核对答案、看训练进度和考卷成绩，素材是 tools/train_data.py 裁好的小块。

  python tools/train_web.py          打开 http://127.0.0.1:5091（--no-browser 不自动开浏览器）

- 首页：考卷、训练素材（每个视频）各有多少块、分成哪几堆、核对了多少
- 核对答案：一屏几十块，下面是引擎读的数；对的不用管，错的改掉，Enter 保存这一页、换下一页。
  空着 = 这一块里没有数字；在框里按 Esc（或点"丢掉"）= 这一块不能用（被挡住、裁歪了）
  考卷每一块都要核对（训练时不用，是量引擎和小模型的尺子）；训练素材核对"要人看"那堆，"规则核对过"那堆抽查
- 复核：小模型读的和答案不一样的块（tools/train_digits.py 的 mark_disputes 挑），框里不预先填数、不告诉你引擎和小模型
  各读了什么，照图重填（核对时框里填着引擎的数，人容易跟着认）；复核过的块当作用户核对过的，训练、考卷都用新答案
- 训练进度：每一次训练（tools/train_digits.py）的验证分数、考卷分数、速度；选中的那一次每一轮的分数和 loss（两张小图）。
  正在训练时每 5 秒刷新
- 考卷成绩：每一项引擎和小模型各读对、读错、没读出几块，读一块多快、占多少内存；读错、没读出、
  小模型比引擎强的那些块并排列出来
"""

import argparse
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime

from flask import Flask, abort, redirect, render_template_string, request, send_file, url_for

import train_data as data

PORT = 5091
PAGE = 48
app = Flask(__name__)
KINDS = {"exam": "考卷", "video": "训练素材"}
PILES = {"check": "要人看", "auto": "规则核对过", "all": "全部"}
STATUSES = {"todo": "没核对", "done": "核对过", "skip": "丢掉的"}


def db():
    return data.connect(app.config.get("DB_PATH"))


BASE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} · Evo 训练素材</title>
<style>
:root { --bg:#0D0F13; --card:#151820; --inset:#0D0F13; --text:#ECEEF2; --muted:#9AA3B2; --border:#262C38;
  --accent:#FF5A5A; --good:#7CE8B8; --bad:#FF8A8A; --warn:#F4D68A; --warn-bg:#1E1A10; --info:#8EC0FF;
  --info-bg:#0F1A2B; --focus:#8EC0FF; --series:#3987e5; --grid:#262C38; color-scheme: dark; }
@media (prefers-color-scheme: light) { :root { --bg:#F6F6F3; --card:#FFFFFF; --inset:#ECECE8; --text:#17191E;
  --muted:#5F6670; --border:#E4E4DF; --good:#11795A; --bad:#C0352F; --warn:#7A5A00; --warn-bg:#FFF6DA;
  --info:#1F5FB0; --info-bg:#E8F1FF; --focus:#1F5FB0; --series:#2a78d6; --grid:#E4E4DF; color-scheme: light; } }
.chart { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:12px 14px; }
.chart h3 { font-size:14px; margin:0 0 6px; font-weight:600; }
.chart svg { width:100%; height:auto; display:block; }
.chart .line { fill:none; stroke:var(--series); stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }
.chart .grid { stroke:var(--grid); stroke-width:1; }
.chart text { fill:var(--muted); font-size:11px; font-variant-numeric:tabular-nums; }
.chart text.last { fill:var(--text); font-weight:600; font-size:12px; }
.chart .dot { fill:var(--series); stroke:var(--card); stroke-width:2; opacity:0; }
.chart .pt:hover .dot, .chart .pt:focus .dot, .chart .pt.end .dot { opacity:1; }
.chart .hit { fill:transparent; }
.charts { display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:16px; margin-top:12px; }
tr.on td { background:var(--inset); }
* { box-sizing: border-box; }
html { scroll-padding-bottom: 96px; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 "Microsoft YaHei UI","PingFang SC",system-ui,sans-serif; }
a { color: var(--info); }
header { display:flex; gap:24px; align-items:center; padding:12px 24px; border-bottom:1px solid var(--border); background:var(--card);
  position:sticky; top:0; z-index:2; flex-wrap:wrap; }
header b { font-size:16px; letter-spacing:.5px; }
header nav { display:flex; gap:4px; flex-wrap:wrap; }
header nav a { color:var(--muted); text-decoration:none; padding:6px 12px; border-radius:6px; }
header nav a:hover { color:var(--text); background:var(--inset); }
header nav a.on { color:var(--text); background:var(--inset); }
main { padding:20px 24px 120px; max-width:1500px; margin:0 auto; }
h1 { font-size:20px; margin:0 0 4px; } h2 { font-size:16px; margin:28px 0 10px; }
.muted { color:var(--muted); } .good { color:var(--good); } .bad { color:var(--bad); } .warn { color:var(--warn); }
.cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:16px; margin-top:16px; }
.card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px 18px; }
.card .big { font-size:26px; font-weight:600; font-variant-numeric:tabular-nums; }
.bar { height:8px; border-radius:4px; background:var(--inset); overflow:hidden; margin:10px 0 6px; }
.bar i { display:block; height:100%; background:var(--good); }
.btn { display:inline-block; padding:9px 16px; border-radius:8px; border:1px solid var(--border); background:var(--inset);
  color:var(--text); text-decoration:none; cursor:pointer; font:inherit; min-height:40px; }
.btn:hover { border-color:var(--muted); }
.btn.primary { background:var(--accent); border-color:var(--accent); color:#2A0A0A; font-weight:600; }
:focus-visible { outline:2px solid var(--focus); outline-offset:2px; }
table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--border); vertical-align:top; }
th { color:var(--muted); font-weight:500; font-size:13px; }
td.num, th.num { text-align:right; }
code { background:var(--inset); padding:1px 6px; border-radius:4px; font-size:13px; }
.filters { display:flex; gap:20px; flex-wrap:wrap; align-items:center; margin:14px 0 6px; }
.filters span.lab { color:var(--muted); font-size:13px; margin-right:6px; }
.seg { display:inline-flex; border:1px solid var(--border); border-radius:8px; overflow:hidden; }
.seg a { padding:6px 12px; color:var(--muted); text-decoration:none; border-right:1px solid var(--border); font-size:14px; }
.seg a:last-child { border-right:0; } .seg a:hover { color:var(--text); }
.seg a.on { background:var(--inset); color:var(--text); font-weight:600; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(168px,1fr)); gap:10px; margin-top:14px; }
.tile { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:8px; display:flex; flex-direction:column; gap:6px; }
.tile .shot { height:64px; display:flex; align-items:center; justify-content:center; background:#000; border-radius:5px; overflow:hidden; cursor:text; }
.tile .shot img { height:56px; max-width:100%; object-fit:contain; image-rendering:pixelated; }
.tile .meta { display:flex; justify-content:space-between; gap:6px; font-size:12px; color:var(--muted); min-height:18px; }
.tile .meta .eng { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:130px; }
.tile .row { display:flex; gap:6px; }
.tile input[type=text] { flex:1; min-width:0; font:600 18px/1 ui-monospace,Consolas,monospace; padding:8px 10px; border-radius:6px;
  border:1px solid var(--border); background:var(--inset); color:var(--text); }
.tile input[type=text]:focus { border-color:var(--focus); outline:2px solid var(--focus); outline-offset:0; }
.tile .skip { font-size:13px; padding:0 10px; border-radius:6px; border:1px solid var(--border); background:transparent; color:var(--muted); cursor:pointer; }
.tile .skip:hover { color:var(--text); border-color:var(--muted); }
.tile.miss { border-color:#5A4A1E; background:var(--warn-bg); }
.tile.changed { border-color:var(--info); background:var(--info-bg); }
.tile.skipped { opacity:.45; } .tile.skipped input[type=text] { text-decoration:line-through; }
.tile.skipped .skip { color:var(--bad); border-color:var(--bad); }
.tile .ans { font:600 18px/1.2 ui-monospace,Consolas,monospace; }
.savebar { position:fixed; left:0; right:0; bottom:0; background:var(--card); border-top:1px solid var(--border); padding:12px 24px;
  display:flex; gap:16px; align-items:center; justify-content:flex-end; z-index:2; }
.savebar .count { margin-right:auto; color:var(--muted); }
.note { background:var(--info-bg); border:1px solid var(--border); border-radius:8px; padding:10px 14px; margin:12px 0; font-size:14px; }
.note.warn { background:var(--warn-bg); }
kbd { border:1px solid var(--border); border-bottom-width:2px; border-radius:4px; padding:0 5px; font-size:12px; background:var(--inset); }
.btn.primary kbd { background:rgba(0,0,0,.12); border-color:rgba(0,0,0,.35); color:inherit; }
</style></head><body>
<header><b>Evo 训练素材</b><nav>
<a href="{{ url_for('home') }}" class="{{ 'on' if nav=='home' }}">首页</a>
<a href="{{ url_for('check', kind='exam') }}" class="{{ 'on' if nav=='exam' }}">核对考卷</a>
<a href="{{ url_for('check', kind='video') }}" class="{{ 'on' if nav=='video' }}">核对训练素材</a>
<a href="{{ url_for('review') }}" class="{{ 'on' if nav=='review' }}">复核{% if review_left %} <b class="warn">{{ review_left }}</b>{% endif %}</a>
<a href="{{ url_for('train') }}" class="{{ 'on' if nav=='train' }}">训练进度</a>
<a href="{{ url_for('score') }}" class="{{ 'on' if nav=='score' }}">考卷成绩</a>
</nav></header>
<main>{{ body|safe }}</main></body></html>"""


def page(title, nav, body_template, **ctx):
    body = render_template_string(body_template, **ctx)
    left = db().execute("select count(*) from crops where review is not null and reviewed_at is null").fetchone()[0]
    return render_template_string(BASE, title=title, nav=nav, body=body, review_left=left)


def progress(conn, kind):
    """{项: Counter(pile, status)} 和合计。"""
    per = defaultdict(Counter)
    for r in conn.execute("select field, pile, status from crops where kind=?", (kind,)):
        c = per[r["field"]]
        c[r["pile"]] += 1
        if r["pile"] != "extra":
            c[r["status"]] += 1
            c["need"] += 1
    total = sum(per.values(), Counter())
    return per, total


HOME = """
<h1>训练小模型：读数字</h1>
<p class="muted">第一个小模型读对局中右上角的六项：剩余小队、击杀、助攻、老板分、伤害、本局 RP。流程和规矩见 claude/todo.md"哪些识别换成自己训练的小模型"。</p>
<div class="cards">
 <div class="card"><div class="muted">考卷（我们自己的真实截图，训练时不用）</div>
  <div class="big">{{ exam_total['done'] + exam_total['skip'] }} / {{ exam_total['need'] }}</div>
  <div class="bar"><i style="width:{{ pct(exam_total) }}%"></i></div>
  <div class="muted">核对过 / 要核对（每一块都要）；{{ exam_sources }} 个截图文件夹</div>
  <p><a class="btn primary" href="{{ url_for('check', kind='exam') }}">去核对考卷</a></p></div>
 <div class="card"><div class="muted">训练素材（视频）</div>
  <div class="big">{{ video_total['done'] + video_total['skip'] }} / {{ video_total['check'] }}</div>
  <div class="bar"><i style="width:{{ pct(video_total, 'check') }}%"></i></div>
  <div class="muted">"要人看"核对了多少；规则核对过 {{ video_total['auto'] }} 块，多出来的 {{ video_total['extra'] }} 块不用看</div>
  <p><a class="btn primary" href="{{ url_for('check', kind='video') }}">去核对训练素材</a></p></div>
 <div class="card"><div class="muted">现在的引擎（RapidOCR + PP-OCRv6，1 个线程）</div>
  {% if meta.get('engine_ms') %}<div class="big">{{ meta['engine_ms'] }} 毫秒 / 块</div>
  <div class="muted">中位数 {{ meta['engine_ms_median'] }} 毫秒；加载以后多占约 {{ meta['engine_mem_mb'] }} MB 内存<br>量于 {{ meta['engine_measured'] }}</div>
  {% else %}<div class="muted">还没量：<code>python tools/train_data.py exam --measure</code></div>{% endif %}
  <p><a class="btn" href="{{ url_for('score') }}">看考卷成绩</a></p></div>
 <div class="card"><div class="muted">小模型{% if last %}（第 {{ last['id'] }} 次训练，考卷考试的一半）{% endif %}</div>
  {% if last_score %}<div class="big">{{ '%.2f' % last['model_ms'] }} 毫秒 / 块</div>
  <div class="muted">考卷读对 {{ rate(last_score.total) }}（读错 {{ last_score.total['wrong'] }}，没读出 {{ last_score.total['missed'] }}）；
  多占约 {{ '%.0f' % last['model_mem_mb'] }} MB 内存，模型文件 {{ '%.0f' % last['size_kb'] }} KB</div>
  {% else %}<div class="muted">还没有训练好、考过的：<code>python tools/train_digits.py train</code></div>{% endif %}
  <p><a class="btn" href="{{ url_for('train') }}">看训练进度</a></p></div>
</div>

<h2>考卷每一项</h2>
{{ field_table(exam_per)|safe }}

<h2>训练素材：视频</h2>
<p class="muted">把视频放进 <code>captures/videos/</code>，再运行 <code>python tools/train_data.py videos</code>（切过的跳过）。Evo 自己的录像也会一起切。</p>
{% if videos %}
<table><tr><th>视频</th><th>比例</th><th>分辨率</th><th class="num">时长</th><th>情况</th><th class="num">规则核对过</th><th class="num">要人看</th><th class="num">核对过</th></tr>
{% for v in videos %}<tr><td>{{ v['name'] }}</td><td>{{ v['aspect'] or '-' }}</td><td>{{ v['width'] or '' }}{% if v['width'] %}×{{ v['height'] }}{% endif %}</td>
<td class="num">{% if v['seconds'] %}{{ '%d:%02d' % (v['seconds'] // 60, v['seconds'] % 60) }}{% endif %}</td>
<td class="{{ 'warn' if v['status'] != 'done' }}">{{ {'done':'切好了','skipped':'跳过','error':'出错','running':'正在切'}.get(v['status'], v['status']) }}{% if v['note'] %}<br><span class="muted">{{ v['note'] }}</span>{% endif %}</td>
<td class="num">{{ v['auto'] }}</td><td class="num">{{ v['check'] }}</td><td class="num">{{ v['checked'] }}</td></tr>{% endfor %}
</table>
{% else %}<p class="note">还没有切过视频。</p>{% endif %}

<h2>考卷的截图文件夹</h2>
<details><summary class="muted">{{ exam_rows|length }} 个（点开看每个文件夹认出的比例、裁了几块；跳过的一般是没有对局画面）</summary>
<table><tr><th>文件夹</th><th>比例</th><th>情况</th><th class="num">块数</th></tr>
{% for s in exam_rows %}<tr><td>{{ s['name'] }}</td><td>{{ s['aspect'] or '-' }}</td><td class="{{ 'warn' if s['status'] != 'done' }}">{{ s['note'] or s['status'] }}</td><td class="num">{{ s['n'] }}</td></tr>{% endfor %}
</table></details>
"""

FIELD_TABLE = """<table><tr><th>项</th><th class="num">要核对</th><th class="num">核对过</th><th class="num">丢掉</th><th class="num">引擎没读出</th></tr>
{% for f in fields %}{% set c = per.get(f, {}) %}<tr><td>{{ labels[f] }}</td><td class="num">{{ c.get('need', 0) }}</td><td class="num">{{ c.get('done', 0) }}</td>
<td class="num">{{ c.get('skip', 0) }}</td><td class="num">{{ missing.get(f, 0) }}</td></tr>{% endfor %}</table>"""


@app.route("/")
def home():
    conn = db()
    exam_per, exam_total = progress(conn, "exam")
    _, video_total = progress(conn, "video")
    missing = Counter(r["field"] for r in conn.execute(
        "select field from crops where kind='exam' and pile!='extra' and engine_value is null"))
    videos = conn.execute("""select s.*, sum(c.pile='auto') auto, sum(c.pile='check') "check",
        sum(c.pile!='extra' and c.status!='todo') checked from sources s left join crops c on c.source=s.id
        where s.kind='video' group by s.id order by s.id""").fetchall()
    exam_rows = conn.execute("""select s.*, count(c.id) n from sources s left join crops c on c.source=s.id
        where s.kind='exam' group by s.id order by s.name""").fetchall()
    meta = dict(conn.execute("select key, value from meta").fetchall())

    def pct(total, base="need"):
        return round(100 * (total["done"] + total["skip"]) / total[base]) if total[base] else 0

    def field_table(per):
        return render_template_string(FIELD_TABLE, per=per, fields=data.FIELDS, labels=data.LABELS, missing=missing)

    runs = tested_runs(conn)
    last = runs[-1] if runs else None
    return page("首页", "home", HOME, exam_per=exam_per, exam_total=exam_total, video_total=video_total,
                videos=videos, exam_rows=exam_rows, exam_sources=sum(1 for s in exam_rows if s["status"] == "done"),
                meta=meta, pct=pct, field_table=field_table, last=last,
                last_score=model_scores(conn, last["id"]) if last else None, rate=rate)


# 核对页、复核页共用：改过的标蓝、Esc 或点"丢掉"切换丢掉、点图片跳到框里、保存只点一次。
# 底下那一行的后半句写在 #count 的 data-tail 里
TILE_JS = """<script>
const form = document.getElementById('f');
function mark(tile) {
  const input = tile.querySelector('input[type=text]');
  tile.classList.toggle('changed', input.value.trim() !== input.dataset.orig);
}
function toggleSkip(tile) {
  const hidden = tile.querySelector('input[name^=s]'), button = tile.querySelector('.skip');
  const on = hidden.value !== '1';
  hidden.value = on ? '1' : '0';
  tile.classList.toggle('skipped', on);
  button.setAttribute('aria-pressed', on);
  update();
}
function update() {
  const tiles = form.querySelectorAll('.tile');
  let changed = 0, skipped = 0;
  tiles.forEach(t => { if (t.classList.contains('changed')) changed++; if (t.classList.contains('skipped')) skipped++; });
  document.getElementById('count').textContent =
    `这一页 ${tiles.length} 块：改了 ${changed} 块，丢掉 ${skipped} 块；${document.getElementById('count').dataset.tail}`;
}
form.querySelectorAll('.tile').forEach(tile => {
  const input = tile.querySelector('input[type=text]');
  input.addEventListener('input', () => { mark(tile); update(); });
  input.addEventListener('keydown', e => { if (e.key === 'Escape') { e.preventDefault(); toggleSkip(tile); } });
  tile.querySelector('.skip').addEventListener('click', () => toggleSkip(tile));
  tile.querySelector('.shot').addEventListener('click', () => input.focus());
});
let sent = false;
form.addEventListener('submit', e => { if (sent) e.preventDefault(); sent = true; });
</script>
"""

CHECK = """
<h1>核对{{ kinds[kind] }}</h1>
{% if kind == 'exam' %}<p class="muted">考卷是量引擎和小模型的尺子，每一块都要核对。</p>
{% else %}<p class="muted">"要人看"那堆全看；"规则核对过"那堆抽查（顺序是打乱的），发现错的多就告诉 Claude 调规则。</p>{% endif %}
<p class="muted">框里是现在的引擎读的数：对的不用管，错的改掉（RP 带上 + 或 −，伤害不用逗号）。<b>空着</b> = 这一块里没有数字；
在框里按 <kbd>Esc</kbd> 或点"丢掉" = 这一块不能用（被挡住、裁歪了、不是数字那一格）。<kbd>Tab</kbd> 到下一块，<kbd>Enter</kbd> 保存这一页。
黄底的是引擎没读出来的：右下角是它读到的字和把握，格式不对（如"15:"）或把握不到 0.8，Evo 都不采用；蓝底的是你改过的。</p>
<div class="filters">
 <div><span class="lab">项</span><span class="seg">{% for key, label in field_opts %}<a href="{{ link(field=key) }}" class="{{ 'on' if field==key }}">{{ label }}</a>{% endfor %}</span></div>
 <div><span class="lab">堆</span><span class="seg">{% for key, label in piles.items() %}<a href="{{ link(pile=key) }}" class="{{ 'on' if pile==key }}">{{ label }}</a>{% endfor %}</span></div>
 <div><span class="lab">状态</span><span class="seg">{% for key, label in statuses.items() %}<a href="{{ link(status=key) }}" class="{{ 'on' if status==key }}">{{ label }}</a>{% endfor %}</span></div>
</div>
{% if message %}<div class="note warn" role="alert">{{ message }}</div>{% endif %}
{% if not crops %}<div class="note">这里没有要核对的了{% if status == 'todo' %}，这一堆核对完了{% endif %}。</div>
{% else %}
<form method="post" action="{{ url_for('save', kind=kind) }}" id="f">
<input type="hidden" name="back" value="{{ request.full_path }}">
<div class="grid">
{% for c in crops %}{% set pre = prefill(c) %}
<div class="tile{% if c['engine_value'] is none and c['status'] == 'todo' %} miss{% endif %}{% if c['status'] == 'skip' %} skipped{% endif %}">
 <div class="shot" data-for="a{{ c['id'] }}"><img src="{{ url_for('image', crop_id=c['id']) }}" alt="{{ labels[c['field']] }}" width="{{ c['w'] }}" height="{{ c['h'] }}"></div>
 <div class="meta"><span>{{ labels[c['field']] }}</span><span class="eng" title="引擎读到的全部文字（把握 {{ '%.2f' % c['engine_score'] if c['engine_score'] is not none else '-' }}）">
  {%- if not c['engine_text'] %}什么也没读到{% elif c['engine_value'] is none %}{{ c['engine_text'] }} · 把握 {{ '%.2f' % c['engine_score'] if c['engine_score'] is not none else '-' }}{% else %}{{ c['engine_text'] }}{% endif -%}
 </span></div>
 <div class="row">
  <input type="text" id="a{{ c['id'] }}" name="a{{ c['id'] }}" value="{{ pre }}" data-orig="{{ pre }}" inputmode="numeric" autocomplete="off"
   spellcheck="false" aria-label="{{ labels[c['field']] }}的答案" placeholder="{{ '没读出' if c['engine_value'] is none else '' }}"{% if loop.first %} autofocus{% endif %}>
  <button type="button" class="skip" tabindex="-1" aria-pressed="{{ 'true' if c['status']=='skip' else 'false' }}">丢掉</button>
  <input type="hidden" name="s{{ c['id'] }}" value="{{ '1' if c['status']=='skip' else '0' }}">
  <input type="hidden" name="ids" value="{{ c['id'] }}">
 </div>
</div>{% endfor %}
</div>
<div class="savebar"><span class="count" id="count" data-tail="这一堆还剩 {{ left }} 块没核对">这一页 {{ crops|length }} 块；这一堆还剩 {{ left }} 块没核对</span>
<button class="btn primary" type="submit">保存这一页 <kbd>Enter</kbd></button></div>
</form>
""" + TILE_JS + """{% endif %}
"""


def queue(conn, kind, field, pile, status, limit=PAGE):
    where = ["kind=?", "pile!='extra'", "status=?"]
    args = [kind, status]
    if pile != "all":
        where.append("pile=?")
        args.append(pile)
    if field != "all":
        where.append("field=?")
        args.append(field)
    cond = " and ".join(where)
    if pile == "auto" and kind == "video":  # 抽查：打乱（但每次一样），不是总看同一个视频的开头
        order = "(id * 2654435761) % 1000003"
    else:  # 一项一项来（同一项的小块长得像，看得快），每一项里先看"要人看"的
        field_order = "case field " + " ".join(f"when '{f}' then {i}" for i, f in enumerate(data.FIELDS)) + " end"
        order = f"{field_order}, pile='auto', source, t, id"
    crops = conn.execute(f"select * from crops where {cond} order by {order} limit ?", (*args, limit)).fetchall()
    left = conn.execute(f"select count(*) from crops where {cond}", (kind, "todo", *args[2:])).fetchone()[0]  # 同样的堆、项里没核对的
    return crops, left


@app.route("/check/<kind>")
def check(kind):
    if kind not in KINDS:
        abort(404)
    field = request.args.get("field", "all")
    pile = request.args.get("pile", "all" if kind == "exam" else "check")
    status = request.args.get("status", "todo")
    if field not in ("all", *data.FIELDS) or pile not in PILES or status not in STATUSES:
        abort(400)
    conn = db()
    crops, left = queue(conn, kind, field, pile, status)

    def link(**change):
        params = {"field": field, "pile": pile, "status": status, **change}
        return url_for("check", kind=kind, **params)

    def prefill(c):
        if c["status"] != "todo":
            return c["answer"] or ""
        return c["engine_value"] or ""

    return page(f"核对{KINDS[kind]}", kind, CHECK, kind=kind, kinds=KINDS, crops=crops, left=left, field=field, pile=pile,
                status=status, piles=PILES, statuses=STATUSES, labels=data.LABELS, link=link, prefill=prefill,
                field_opts=[("all", "全部"), *((f, data.LABELS[f]) for f in data.FIELDS)],
                message=request.args.get("msg"), request=request)


def clean_answer(text):
    """用户填的答案：去掉空格、逗号，"−"换成"-"；不是数字（可以带 + -）返回 None（不存）。空着 = 没有数字（''）。"""
    text = (text or "").strip().replace(" ", "").replace(",", "").replace("，", "").replace("−", "-").replace("—", "-")
    if text == "":
        return ""
    body = text[1:] if text[0] in "+-" else text
    return text if body.isdigit() else None


@app.route("/check/<kind>/save", methods=["POST"])
def save(kind):
    if kind not in KINDS:
        abort(404)
    conn = db()
    now = datetime.now().isoformat(timespec="seconds")
    bad = 0
    for cid in request.form.getlist("ids"):
        skip = request.form.get(f"s{cid}") == "1"
        answer = clean_answer(request.form.get(f"a{cid}"))
        if skip:
            conn.execute("update crops set status='skip', answer=null, checked_at=? where id=? and kind=?", (now, cid, kind))
        elif answer is None:
            bad += 1  # 填的不是数字：这一块不存，留在"没核对"里
        else:
            conn.execute("update crops set status='done', answer=?, checked_at=? where id=? and kind=?", (answer, now, cid, kind))
    conn.commit()
    back = request.form.get("back") or url_for("check", kind=kind)
    if not back.startswith("/check/"):
        back = url_for("check", kind=kind)
    if bad:
        back += ("&" if "?" in back else "?") + f"msg=有 {bad} 块填的不是数字，没有保存，还留在这一页"
    return redirect(back)


REVIEW = """
<h1>复核</h1>
<p class="muted">这些块小模型读的和原来的答案不一样。原来核对时框里预先填着引擎的数，数字一糊容易跟着认，所以这次<b>框里什么都不填</b>，
也不告诉你引擎、小模型各读了什么：<b>照图填你看到的数</b>（RP 带上 + 或 −）。图里没有数字就空着；看不清是几、被挡住、裁歪了，
在框里按 <kbd>Esc</kbd> 或点"丢掉"。<kbd>Enter</kbd> 保存这一页。</p>
{% if stats['n'] %}<div class="note">复核过 {{ stats['n'] }} 块：和原来的答案一样 {{ stats['same'] }} 块，改了 {{ stats['changed'] }} 块
（改成和小模型读的一样 {{ stats['to_model'] }} 块），丢掉 {{ stats['skip'] }} 块。</div>{% endif %}
<div class="filters"><div><span class="lab">哪些</span><span class="seg">{% for key, label in (('all', '全部'), ('exam', '考卷'), ('video', '训练素材')) %}
<a href="{{ url_for('review', kind=key) }}" class="{{ 'on' if kind == key }}">{{ label }}</a>{% endfor %}</span></div></div>
{% if message %}<div class="note warn" role="alert">{{ message }}</div>{% endif %}
{% if not crops %}<div class="note">没有要复核的了。挑要复核的块：训练完会自动挑，也可以 <code>python tools/train_digits.py disputes</code>。</div>
{% else %}
<form method="post" action="{{ url_for('review_save') }}" id="f">
<input type="hidden" name="back" value="{{ request.full_path }}">
<div class="grid">
{% for c in crops %}
<div class="tile">
 <div class="shot"><img src="{{ url_for('image', crop_id=c['id']) }}" alt="{{ labels[c['field']] }}" width="{{ c['w'] }}" height="{{ c['h'] }}"></div>
 <div class="meta"><span>{{ labels[c['field']] }}</span><span>{{ kinds[c['kind']] }}</span></div>
 <div class="row">
  <input type="text" id="a{{ c['id'] }}" name="a{{ c['id'] }}" value="" data-orig="" inputmode="numeric" autocomplete="off"
   spellcheck="false" aria-label="{{ labels[c['field']] }}：照图填" placeholder="照图填"{% if loop.first %} autofocus{% endif %}>
  <button type="button" class="skip" tabindex="-1" aria-pressed="false">丢掉</button>
  <input type="hidden" name="s{{ c['id'] }}" value="0">
  <input type="hidden" name="ids" value="{{ c['id'] }}">
 </div>
</div>{% endfor %}
</div>
<div class="savebar"><span class="count" id="count" data-tail="还剩 {{ left }} 块要复核">这一页 {{ crops|length }} 块；还剩 {{ left }} 块要复核</span>
<button class="btn primary" type="submit">保存这一页 <kbd>Enter</kbd></button></div>
</form>
""" + TILE_JS + """{% endif %}
"""


@app.route("/review")
def review():
    kind = request.args.get("kind", "all")
    if kind not in ("all", "exam", "video"):
        abort(400)
    conn = db()
    cond = "review is not null and reviewed_at is null" + ("" if kind == "all" else " and kind=?")
    args = () if kind == "all" else (kind,)
    field_order = "case field " + " ".join(f"when '{f}' then {i}" for i, f in enumerate(data.FIELDS)) + " end"
    crops = conn.execute(f"select * from crops where {cond} order by kind='video', {field_order}, id limit ?",
                         (*args, PAGE)).fetchall()
    left = conn.execute(f"select count(*) from crops where {cond}", args).fetchone()[0]
    stats = Counter()
    for r in conn.execute("select status, answer, answer_before, review from crops where reviewed_at is not null"):
        stats["n"] += 1
        if r["status"] == "skip":
            stats["skip"] += 1
        elif data.number(r["answer"] or None) == data.number(r["answer_before"] or None):
            stats["same"] += 1
        else:
            stats["changed"] += 1
            stats["to_model"] += data.number(r["answer"] or None) == data.number(r["review"] or None)
    return page("复核", "review", REVIEW, crops=crops, left=left, kind=kind, kinds=KINDS, labels=data.LABELS,
                stats=stats, message=request.args.get("msg"), request=request)


@app.route("/review/save", methods=["POST"])
def review_save():
    conn = db()
    now = datetime.now().isoformat(timespec="seconds")
    bad = 0
    for cid in request.form.getlist("ids"):
        row = conn.execute("select * from crops where id=? and review is not null and reviewed_at is null", (cid,)).fetchone()
        if row is None:
            continue
        before = row["answer"] if row["status"] == "done" else (row["engine_value"] or "")  # 没核对过的：引擎的读数 / 借来的答案
        if request.form.get(f"s{cid}") == "1":
            conn.execute("update crops set status='skip', answer=null, answer_before=?, reviewed_at=?, checked_at=? where id=?",
                         (before, now, now, cid))
            continue
        answer = clean_answer(request.form.get(f"a{cid}"))
        if answer is None:
            bad += 1
            continue
        conn.execute("update crops set status='done', answer=?, answer_before=?, reviewed_at=?, checked_at=? where id=?",
                     (answer, before, now, now, cid))
    conn.commit()
    back = request.form.get("back") or url_for("review")
    if not back.startswith("/review"):
        back = url_for("review")
    if bad:
        back += ("&" if "?" in back else "?") + f"msg=有 {bad} 块填的不是数字，没有保存，还留在这一页"
    return redirect(back)


TILES = """{% macro tiles(items, who) %}
{% if items %}<div class="grid">{% for c in items[:150] %}
<div class="tile"><div class="shot"><img src="{{ url_for('image', crop_id=c['id']) }}" alt="{{ labels[c['field']] }}" loading="lazy"></div>
<div class="meta"><span>{{ labels[c['field']] }}</span><span>答案 <span class="ans">{{ c['answer'] if c['answer'] != '' else '（空）' }}</span></span></div>
{% if who != 'model' %}<div class="meta"><span class="eng" title="{{ c['engine_text'] }}">引擎：{{ c['engine_text'] or '什么也没读到' }}</span><span>{{ '%.2f' % c['engine_score'] if c['engine_score'] is not none else '' }}</span></div>{% endif %}
{% if who != 'engine' and c['mtext'] is defined %}<div class="meta"><span class="eng">小模型：{{ c['mtext'] if c['mtext'] else '（空）' }}</span><span>{{ '%.2f' % c['mscore'] if c['mscore'] is not none else '' }}</span></div>{% endif %}
</div>{% endfor %}</div>{% if items|length > 150 %}<p class="muted">只列前 150 块。</p>{% endif %}
{% else %}<p class="muted">没有。</p>{% endif %}
{% endmacro %}"""

SCORE = TILES + """
<h1>考卷成绩</h1>
<p class="muted">只算核对过的块（丢掉的不算）。<b>读错</b> = 很有把握地读成了别的数（最危险：Evo 会记错）；
<b>没读出</b> = 读不出来，或者把握不够（Evo 留空，不会记错，但漏了）。图里没有数字、也没读出数的算读对。</p>
<p class="muted">考卷按截图文件夹分成两半：小模型的"把握"门槛在<b>挑门槛的一半</b>上挑（读错不比引擎多），
<b>考试的一半</b>小模型没见过、门槛也不是在它上面挑的，所以成绩默认只算这一半。</p>
<div class="filters">{% if runs %}<div><span class="lab">小模型</span><span class="seg">{% for r in runs %}
<a href="{{ url_for('score', run=r['id'], part=part) }}" class="{{ 'on' if r['id'] == run }}">第 {{ r['id'] }} 次训练</a>{% endfor %}</span></div>{% endif %}
<div><span class="lab">算哪些</span><span class="seg">{% for key, label in parts.items() %}
<a href="{{ url_for('score', run=run, part=key) }}" class="{{ 'on' if key == part }}">{{ label }}</a>{% endfor %}</span></div></div>
<div class="cards">
 <div class="card"><div class="muted">{{ parts[part] }}核对了</div><div class="big">{{ eng.total['n'] }} 块</div>
  <div class="muted">还有 {{ todo }} 块没核对{% if todo %}，成绩还不全{% endif %}</div></div>
 <div class="card"><div class="muted">引擎读对</div><div class="big">{{ rate(eng.total) }}</div>
  <div class="muted">读错 <span class="bad">{{ eng.total['wrong'] }}</span> 块，没读出 <span class="warn">{{ eng.total['missed'] }}</span> 块</div></div>
 <div class="card"><div class="muted">小模型读对{% if run %}（第 {{ run }} 次训练）{% endif %}</div>
  {% if mod %}<div class="big">{{ rate(mod.total) }}</div>
  <div class="muted">读错 <span class="bad">{{ mod.total['wrong'] }}</span> 块，没读出 <span class="warn">{{ mod.total['missed'] }}</span> 块</div>
  {% else %}<div class="muted">还没有训练好、考过的小模型（训练进度页）</div>{% endif %}</div>
 <div class="card"><div class="muted">读一块（1 个线程）</div>
  <div class="big">{{ meta.get('engine_ms', '-') }} → {{ '%.2f' % info['model_ms'] if info and info['model_ms'] else '-' }} 毫秒</div>
  <div class="muted">引擎 → 小模型；加载以后多占内存 {{ meta.get('engine_mem_mb', '-') }} → {{ '%.0f' % info['model_mem_mb'] if info and info['model_mem_mb'] is not none else '-' }} MB；
  模型文件 {{ '%.0f' % info['size_kb'] if info and info['size_kb'] else '-' }} KB</div></div>
</div>
<h2>每一项</h2>
<table><tr><th rowspan="2">项</th><th class="num" rowspan="2">核对过</th><th class="num" colspan="4">现在的引擎</th><th class="num" colspan="4">小模型</th></tr>
<tr><th class="num">读对</th><th class="num">读错</th><th class="num">没读出</th><th class="num">读对的比例</th>
<th class="num">读对</th><th class="num">读错</th><th class="num">没读出</th><th class="num">读对的比例</th></tr>
{% for f in fields + ['合计'] %}{% set c = eng.per[f] if f != '合计' else eng.total %}{% set m = (mod.per[f] if f != '合计' else mod.total) if mod else none %}
<tr><td>{{ labels.get(f, f) }}</td><td class="num">{{ c['n'] }}</td><td class="num">{{ c['right'] }}</td>
<td class="num {{ 'bad' if c['wrong'] }}">{{ c['wrong'] }}</td><td class="num {{ 'warn' if c['missed'] }}">{{ c['missed'] }}</td><td class="num">{{ rate(c) }}</td>
{% if m %}<td class="num">{{ m['right'] }}</td><td class="num {{ 'bad' if m['wrong'] }}">{{ m['wrong'] }}</td>
<td class="num {{ 'warn' if m['missed'] }}">{{ m['missed'] }}</td><td class="num"><b>{{ rate(m) }}</b></td>
{% else %}<td class="num muted" colspan="4">还没有</td>{% endif %}</tr>{% endfor %}</table>
{% if mod %}
<h2>小模型读对、引擎没读对的（{{ wins|length }}）</h2>{{ tiles(wins, 'both') }}
<h2>小模型读错的（{{ mod.wrong|length }}）</h2>{{ tiles(mod.wrong, 'both') }}
<h2>小模型没读出的（{{ mod.missed|length }}）</h2>{{ tiles(mod.missed, 'both') }}
{% endif %}
<h2>引擎读错的（{{ eng.wrong|length }}）</h2>{{ tiles(eng.wrong, 'engine') }}
<h2>引擎没读出的（{{ eng.missed|length }}）</h2>{{ tiles(eng.missed, 'engine') }}
"""


class Scores:
    """考卷上每一项读对、读错、没读出几块。rows 里每块要有 answer 和读数 v。"""

    def __init__(self, rows):
        self.per = {f: Counter() for f in data.FIELDS}
        self.wrong, self.missed, self.right_ids = [], [], set()
        for r in rows:
            answer, read = data.number(r["answer"]), data.number(r["v"])
            c = self.per[r["field"]]
            c["n"] += 1
            if answer == read:
                c["right"] += 1
                self.right_ids.add(r["id"])
            elif read is not None:
                c["wrong"] += 1
                self.wrong.append(r)
            else:
                c["missed"] += 1
                self.missed.append(r)
        self.total = sum(self.per.values(), Counter())


PARTS = {"test": "考试的一半", "dev": "挑门槛的一半", "all": "整份考卷"}


def part_cond(part):
    """考卷的哪一半（train_data.assign_exam_parts）：成绩默认只算考试的一半（门槛是在另一半上挑的）。"""
    return "" if part == "all" else " and c.source in (select id from sources where exam_part=?)"


def engine_scores(conn, part="test"):
    args = () if part == "all" else (part,)
    return Scores(conn.execute("select c.*, c.engine_value as v from crops c where c.kind='exam' and c.pile!='extra' "
                               "and c.status='done'" + part_cond(part) + " order by c.field, c.id", args).fetchall())


def model_rows(conn, run, part="test"):
    args = (run,) if part == "all" else (run, part)
    return conn.execute("select c.*, m.value as v, m.text as mtext, m.score as mscore from crops c join model_reads m "
                        "on m.crop=c.id and m.run=? where c.kind='exam' and c.pile!='extra' and c.status='done'"
                        + part_cond(part) + " order by c.field, c.id", args).fetchall()


def model_scores(conn, run, part="test"):
    rows = model_rows(conn, run, part)
    return Scores(rows) if rows else None


def rate(c):
    return f"{100 * c['right'] / c['n']:.1f}%" if c["n"] else "-"


def tested_runs(conn):
    return conn.execute("select * from train_runs where id in (select distinct run from model_reads) order by id").fetchall()


@app.route("/score")
def score():
    conn = db()
    part = request.args.get("part", "test")
    if part not in PARTS:
        abort(400)
    todo = conn.execute("select count(*) from crops where kind='exam' and pile!='extra' and status='todo'").fetchone()[0]
    runs = tested_runs(conn)
    run = request.args.get("run", type=int) or (runs[-1]["id"] if runs else None)
    info = conn.execute("select * from train_runs where id=?", (run,)).fetchone() if run else None
    eng = engine_scores(conn, part)
    mod = model_scores(conn, run, part) if run else None
    wins = []
    if mod:
        rows = {r["id"]: r for r in model_rows(conn, run, part)}
        wins = [rows[r["id"]] for r in eng.wrong + eng.missed if r["id"] in mod.right_ids]
    meta = dict(conn.execute("select key, value from meta").fetchall())
    return page("考卷成绩", "score", SCORE, eng=eng, mod=mod, wins=wins, todo=todo, rate=rate, runs=runs, run=run,
                info=info, fields=list(data.FIELDS), labels=data.LABELS, meta=meta, part=part, parts=PARTS)


def line_chart(points, title, fmt, low=None, high=None):
    """一条线的小图：points = [(第几轮, 值)]。只有一条线，不要图例，标题就是名字；最后一个点直接标上数；
    悬停在点上看每一轮的数（SVG 的 title）。"""
    if not points:
        return ""
    width, height, left, right, top, bottom = 560, 160, 46, 48, 10, 24
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    x0, x1 = 1, max(max(xs), 2)
    y0 = min(ys) if low is None else low
    y1 = max(ys) if high is None else high
    if y1 - y0 < 1e-9:
        y1 = y0 + 1

    def sx(x):
        return left + (x - x0) / (x1 - x0) * (width - left - right)

    def sy(y):
        return top + (1 - (y - y0) / (y1 - y0)) * (height - top - bottom)

    parts = [f'<div class="chart"><h3>{title}</h3><svg viewBox="0 0 {width} {height}" role="img" aria-label="{title}">']
    for v in (y0, (y0 + y1) / 2, y1):
        parts.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{sy(v):.1f}" y2="{sy(v):.1f}"/>'
                     f'<text x="{left - 6}" y="{sy(v) + 4:.1f}" text-anchor="end">{fmt(v)}</text>')
    for x in sorted({x0, round((x0 + x1) / 2), x1}):
        parts.append(f'<text x="{sx(x):.1f}" y="{height - 6}" text-anchor="middle">第 {x} 轮</text>')
    parts.append('<polyline class="line" points="' + " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in points) + '"/>')
    for i, (x, y) in enumerate(points):
        end = " end" if i == len(points) - 1 else ""
        parts.append(f'<g class="pt{end}" tabindex="0"><title>第 {x} 轮：{fmt(y)}</title>'
                     f'<circle class="hit" cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="9"/>'
                     f'<circle class="dot" cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="4"/></g>')
    x, y = points[-1]
    parts.append(f'<text class="last" x="{sx(x) + 8:.1f}" y="{sy(y) + 4:.1f}">{fmt(y)}</text></svg></div>')
    return "".join(parts)


TRAIN = """
<h1>训练进度</h1>
<p class="muted">训练：<code>python tools/train_digits.py train</code>（用显卡，几分钟；完了自动考一遍考卷）。
验证 = 留着不训练的那个视频（用来挑最好的一轮）；"把握"的门槛考试时在考卷挑门槛的一半上挑；成绩算考卷考试的一半（小模型没见过，门槛也不是在它上面挑的）。
{% if running %}<b>正在训练，这一页每 5 秒刷新。</b>{% endif %}</p>
{% if not runs %}<div class="note">还没有训练过。</div>{% else %}
<table><tr><th>第几次</th><th>开始</th><th>情况</th><th>用什么算</th><th class="num">最好的一轮</th><th class="num">门槛</th>
<th class="num">挑门槛的一半 读对 / 读错 / 没读出</th><th class="num">考试的一半 读对 / 读错 / 没读出</th><th class="num">一块几毫秒</th><th class="num">内存</th><th class="num">大小</th></tr>
{% for r in runs %}{% set s = exams.get(r['id']) %}<tr class="{{ 'on' if r['id'] == sel }}">
<td><a href="{{ url_for('train', run=r['id']) }}">第 {{ r['id'] }} 次</a></td><td>{{ (r['started'] or '')[5:16]|replace('T', ' ') }}</td>
<td class="{{ 'bad' if r['status'] == 'error' else ('warn' if r['status'] == 'running' else '') }}">{{ {'done':'训练好了','running':'正在训练','error':'出错'}.get(r['status'], r['status']) }}{% if r['note'] %}<br><span class="muted">{{ r['note'] }}</span>{% endif %}</td>
<td>{{ r['device'] }}</td><td class="num">{{ r['best_epoch'] or '-' }} / {{ r['epochs'] }}</td><td class="num">{{ r['threshold'] or '-' }}</td>
<td class="num">{% if r['val_n'] %}{{ r['val_right'] }} / <span class="{{ 'bad' if r['val_wrong'] }}">{{ r['val_wrong'] }}</span> / {{ r['val_missed'] }}（{{ r['val_n'] }}）{% else %}-{% endif %}</td>
<td class="num">{% if s %}{{ s.total['right'] }} / <span class="{{ 'bad' if s.total['wrong'] }}">{{ s.total['wrong'] }}</span> / {{ s.total['missed'] }}（{{ rate(s.total) }}）{% else %}-{% endif %}</td>
<td class="num">{{ '%.2f' % r['model_ms'] if r['model_ms'] else '-' }}</td><td class="num">{{ '%.0f MB' % r['model_mem_mb'] if r['model_mem_mb'] is not none else '-' }}</td>
<td class="num">{{ '%.0f KB' % r['size_kb'] if r['size_kb'] else '-' }}</td></tr>{% endfor %}</table>
<p class="muted">现在的引擎在考卷考试的一半上：{{ eng.total['right'] }} / <span class="bad">{{ eng.total['wrong'] }}</span> / {{ eng.total['missed'] }}（{{ rate(eng.total) }}），一块 {{ meta.get('engine_ms', '-') }} 毫秒，多占 {{ meta.get('engine_mem_mb', '-') }} MB。</p>
<h2>第 {{ sel }} 次训练：每一轮</h2>
{% if epochs %}<div class="charts">{{ acc_chart|safe }}{{ loss_chart|safe }}</div>
<details><summary class="muted">看表格</summary>
<table><tr><th class="num">轮</th><th class="num">loss</th><th class="num">验证读对</th><th class="num">读错</th><th class="num">没读出</th><th class="num">秒</th></tr>
{% for e in epochs %}<tr><td class="num">{{ e['epoch'] }}</td><td class="num">{{ '%.3f' % e['loss'] }}</td><td class="num">{{ '%.1f%%' % (100 * e['val_acc']) }}</td>
<td class="num">{{ e['val_wrong'] }}</td><td class="num">{{ e['val_missed'] }}</td><td class="num">{{ '%.0f' % e['seconds'] }}</td></tr>{% endfor %}</table></details>
<p class="muted">每一轮的验证分数按把握 0.8 算；挑最好的一轮时每一轮先在验证视频上挑好门槛（读错不比引擎多）再比读对几块；考试时门槛在考卷挑门槛的一半上重新挑。</p>
{% else %}<p class="muted">还没有跑完一轮。</p>{% endif %}
{% endif %}
"""


@app.route("/train")
def train():
    conn = db()
    runs = conn.execute("select * from train_runs order by id desc").fetchall()
    sel = request.args.get("run", type=int) or (runs[0]["id"] if runs else None)
    epochs = conn.execute("select * from train_epochs where run=? order by epoch", (sel,)).fetchall()
    exams = {r["id"]: model_scores(conn, r["id"]) for r in runs}
    exams = {k: v for k, v in exams.items() if v}
    meta = dict(conn.execute("select key, value from meta").fetchall())
    running = any(r["status"] == "running" for r in runs)
    acc = line_chart([(e["epoch"], 100 * e["val_acc"]) for e in epochs], "验证视频上读对的比例（%）",
                     lambda v: f"{v:.0f}%", 0, 100)
    loss = line_chart([(e["epoch"], e["loss"]) for e in epochs], "loss（越小越好）", lambda v: f"{v:.2f}", 0)
    body = page("训练进度", "train", TRAIN, runs=runs, sel=sel, epochs=epochs, exams=exams, eng=engine_scores(conn),
                rate=rate, meta=meta, running=running, acc_chart=acc, loss_chart=loss)
    if running:
        body = body.replace("<head>", '<head><meta http-equiv="refresh" content="5">', 1)
    return body


@app.route("/img/<int:crop_id>")
def image(crop_id):
    row = db().execute("select img from crops where id=?", (crop_id,)).fetchone()
    if row is None:
        abort(404)
    path = (data.CROPS / row["img"]).resolve()
    if not path.is_relative_to(data.CROPS.resolve()) or not path.is_file():
        abort(404)
    return send_file(path, mimetype="image/png", max_age=3600)


@app.route("/__quit", methods=["POST"])
def quit_server():
    """再运行一次本程序时，新的请旧的关掉（见 stop_old）。只认本机来的。"""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(403)
    threading.Timer(0.3, os._exit, (0,)).start()
    return "bye"


def port_busy(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def stop_old(port):
    """这个端口上已经开着一个（以前运行的没关）：请它自己关掉。Windows 上两个程序能同时开在一个端口上，
    请求会一直给旧的那个（2026-11-21：用户重新运行了几次，看到的一直是早上的旧网页）。关掉了返回 True。"""
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/__quit", method="POST"), timeout=2)
    except OSError:
        pass  # 旧版本没有这个地址
    for _ in range(25):
        if not port_busy(port):
            return True
        time.sleep(0.2)
    return False


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="训练小模型用的网页：核对答案、复核、看训练进度和考卷成绩")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}/"
    if port_busy(args.port):
        if stop_old(args.port):
            print("关掉了以前开着的网页，换成新的")
        else:
            print(f"{url} 上已经开着一个旧的网页，关不掉它：先关掉以前运行网页的那个终端窗口，再运行一次")
            return
    print(f"打开 {url}（Ctrl+C 关掉）")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, (url,)).start()
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
