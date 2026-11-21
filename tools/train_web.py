"""训练小模型用的网页（开发工具，只在本机）：核对答案、看考卷成绩，素材是 tools/train_data.py 裁好的小块。

  python tools/train_web.py          打开 http://127.0.0.1:5091（--no-browser 不自动开浏览器）

- 首页：考卷、训练素材（每个视频）各有多少块、分成哪几堆、核对了多少
- 核对答案：一屏几十块，下面是引擎读的数；对的不用管，错的改掉，Enter 保存这一页、换下一页。
  空着 = 这一块里没有数字；在框里按 Esc（或点"丢掉"）= 这一块不能用（被挡住、裁歪了）
  考卷每一块都要核对（训练时不用，是量引擎和小模型的尺子）；训练素材核对"要人看"那堆，"规则核对过"那堆抽查
- 考卷成绩：每一项引擎读对、读错、没读出几块，读一块多快、占多少内存；读错和没读出的并排列出来。
  小模型那一列等第 5 步训练以后才有
"""

import argparse
import sys
import threading
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
  --info-bg:#0F1A2B; --focus:#8EC0FF; color-scheme: dark; }
@media (prefers-color-scheme: light) { :root { --bg:#F6F6F3; --card:#FFFFFF; --inset:#ECECE8; --text:#17191E;
  --muted:#5F6670; --border:#E4E4DF; --good:#11795A; --bad:#C0352F; --warn:#7A5A00; --warn-bg:#FFF6DA;
  --info:#1F5FB0; --info-bg:#E8F1FF; --focus:#1F5FB0; color-scheme: light; } }
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
<a href="{{ url_for('score') }}" class="{{ 'on' if nav=='score' }}">考卷成绩</a>
</nav></header>
<main>{{ body|safe }}</main></body></html>"""


def page(title, nav, body_template, **ctx):
    body = render_template_string(body_template, **ctx)
    return render_template_string(BASE, title=title, nav=nav, body=body)


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

    return page("首页", "home", HOME, exam_per=exam_per, exam_total=exam_total, video_total=video_total,
                videos=videos, exam_rows=exam_rows, exam_sources=sum(1 for s in exam_rows if s["status"] == "done"),
                meta=meta, pct=pct, field_table=field_table)


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
<div class="savebar"><span class="count" id="count">这一页 {{ crops|length }} 块；这一堆还剩 {{ left }} 块没核对</span>
<button class="btn primary" type="submit">保存这一页 <kbd>Enter</kbd></button></div>
</form>
<script>
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
    `这一页 ${tiles.length} 块：改了 ${changed} 块，丢掉 ${skipped} 块；这一堆还剩 {{ left }} 块没核对`;
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
{% endif %}
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


SCORE = """
<h1>考卷成绩</h1>
<p class="muted">只算核对过的块（丢掉的不算）。<b>读错</b> = 引擎很有把握地读成了别的数（最危险：Evo 会记错）；
<b>没读出</b> = 引擎读不出来（Evo 留空，不会记错，但漏了）。小模型那一列等第 5 步训练以后才有。</p>
<div class="cards">
 <div class="card"><div class="muted">考卷核对了</div><div class="big">{{ total['n'] }} 块</div>
  <div class="muted">还有 {{ todo }} 块没核对{% if todo %}，成绩还不全{% endif %}</div></div>
 <div class="card"><div class="muted">引擎读对</div><div class="big">{{ rate(total) }}</div>
  <div class="muted">读错 <span class="bad">{{ total['wrong'] }}</span> 块，没读出 <span class="warn">{{ total['missed'] }}</span> 块</div></div>
 <div class="card"><div class="muted">引擎读一块（1 个线程）</div>
  <div class="big">{{ meta.get('engine_ms', '-') }} 毫秒</div>
  <div class="muted">加载以后多占约 {{ meta.get('engine_mem_mb', '-') }} MB 内存；笔记本慢 10–15 倍</div></div>
</div>
<h2>每一项</h2>
<table><tr><th>项</th><th class="num">核对过</th><th class="num">引擎读对</th><th class="num">读错</th><th class="num">没读出</th><th class="num">读对的比例</th><th class="num">小模型</th></tr>
{% for f in fields %}{% set c = per[f] %}<tr><td>{{ labels[f] }}</td><td class="num">{{ c['n'] }}</td><td class="num">{{ c['right'] }}</td>
<td class="num {{ 'bad' if c['wrong'] }}">{{ c['wrong'] }}</td><td class="num {{ 'warn' if c['missed'] }}">{{ c['missed'] }}</td>
<td class="num">{{ rate(c) }}</td><td class="num muted">还没有</td></tr>{% endfor %}
<tr><th>合计</th><th class="num">{{ total['n'] }}</th><th class="num">{{ total['right'] }}</th><th class="num">{{ total['wrong'] }}</th>
<th class="num">{{ total['missed'] }}</th><th class="num">{{ rate(total) }}</th><th></th></tr></table>
{% for title, items in (('引擎读错的', wrong), ('引擎没读出的', missed)) %}
<h2>{{ title }}（{{ items|length }}）</h2>
{% if items %}<div class="grid">{% for c in items[:150] %}
<div class="tile"><div class="shot"><img src="{{ url_for('image', crop_id=c['id']) }}" alt="{{ labels[c['field']] }}" loading="lazy"></div>
<div class="meta"><span>{{ labels[c['field']] }}</span><span>答案 <span class="ans">{{ c['answer'] if c['answer'] != '' else '（空）' }}</span></span></div>
<div class="meta"><span class="eng" title="{{ c['engine_text'] }}">引擎：{{ c['engine_text'] or '什么也没读到' }}</span><span>{{ '%.2f' % c['engine_score'] if c['engine_score'] is not none else '' }}</span></div>
</div>{% endfor %}</div>{% if items|length > 150 %}<p class="muted">只列前 150 块。</p>{% endif %}
{% else %}<p class="muted">没有。</p>{% endif %}{% endfor %}
"""


@app.route("/score")
def score():
    conn = db()
    rows = conn.execute("select * from crops where kind='exam' and pile!='extra' and status='done' order by field, id").fetchall()
    todo = conn.execute("select count(*) from crops where kind='exam' and pile!='extra' and status='todo'").fetchone()[0]
    per = {f: Counter() for f in data.FIELDS}
    wrong, missed = [], []
    for r in rows:
        answer, read = data.number(r["answer"]), data.number(r["engine_value"])
        c = per[r["field"]]
        c["n"] += 1
        if answer == read:
            c["right"] += 1
        elif read is not None:
            c["wrong"] += 1
            wrong.append(r)
        else:
            c["missed"] += 1
            missed.append(r)
    total = sum(per.values(), Counter())

    def rate(c):
        return f"{100 * c['right'] / c['n']:.1f}%" if c["n"] else "-"

    meta = dict(conn.execute("select key, value from meta").fetchall())
    return page("考卷成绩", "score", SCORE, per=per, total=total, todo=todo, wrong=wrong, missed=missed, rate=rate,
                fields=data.FIELDS, labels=data.LABELS, meta=meta)


@app.route("/img/<int:crop_id>")
def image(crop_id):
    row = db().execute("select img from crops where id=?", (crop_id,)).fetchone()
    if row is None:
        abort(404)
    path = (data.CROPS / row["img"]).resolve()
    if not path.is_relative_to(data.CROPS.resolve()) or not path.is_file():
        abort(404)
    return send_file(path, mimetype="image/png", max_age=3600)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="训练小模型用的网页：核对答案、看考卷成绩")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}/"
    print(f"打开 {url}（Ctrl+C 关掉）")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, (url,)).start()
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
