// 后台「数据」页（2026-11-19 用户要的：日活趋势、对局趋势的一个中心；有后台权限的人都能看）。
// 数是 statsroll.js 每 3 小时从匿名统计的数据库增量算好存下的，这一页不读数据库。
// 图在服务器上画成 SVG（网页只许运行本站的脚本，不引外面的图表库）；电脑、手机各画一张（字不会被缩得太小），CSS 按屏幕宽度挑一张。
// 悬停看每一天的数（/app.js 的 data-tip）；每张图下面有"看数字"的表。颜色是 dataviz 的参考色板（深浅色各一套，验过色盲区分度）。
// 口径：只算开了匿名统计的玩家（默认不开）；日期是玩家电脑上的日期；对局结束 1 小时以后才传上来，今天的数不全。

import { ago, escape, layout } from "./pages.js";
import { isoDay, series, usageByVersion } from "./statsroll.js";

const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;
export const RANGES = { 30: "近 30 天", 90: "近 90 天", all: "全部" };

const fmt = (n) => (n === null || n === undefined || Number.isNaN(n) ? "—" : Math.round(n).toLocaleString("en-US"));
const pctText = (n) => (n === null || n === undefined || Number.isNaN(n) ? "—" : `${n.toFixed(1)}%`);
const short = (day) => day.slice(5);  // 11-18

// 刻度取整：0、一半、最大（最大取 1、2、2.5、5 乘 10 的几次方）
function niceMax(max) {
  if (!(max > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(max));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= max) return m * p;
  return 10 * p;
}

// 一张图（两种尺寸各画一张）。kind：line（几条线）或 stack（叠起来的柱子）。
// lines / stacks：[{name, cls: "s1" | "s2" | "s3", values: [每天的数或 null]}]；days：日期；today：今天（画成虚线、浅色：还没算完）
function chartSvg({ kind, items, days, today, unit = "", w, h, labels }) {
  const m = { l: 44, r: 10, t: 10, b: 24 };
  const pw = w - m.l - m.r;
  const ph = h - m.t - m.b;
  const n = days.length;
  const totals = kind === "stack" ? days.map((_, i) => items.reduce((s, it) => s + (it.values[i] ?? 0), 0)) : [];
  const max = niceMax(Math.max(0, ...(kind === "stack" ? totals : items.flatMap((it) => it.values.filter((v) => v !== null)))));
  const y = (v) => m.t + ph - (v / max) * ph;
  const band = pw / n;
  const x = (i) => (kind === "stack" ? m.l + band * i + band / 2 : m.l + (n > 1 ? (pw * i) / (n - 1) : pw / 2));
  const tick = (v) => `${fmt(v)}${unit}`;
  const grid = [0, max / 2, max].map((v) => `<line class="grid" x1="${m.l}" x2="${w - m.r}" y1="${y(v)}" y2="${y(v)}"/>
    <text class="axis" x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${tick(v)}</text>`).join("");
  const every = Math.max(1, Math.ceil(n / labels));
  const xLabels = days.map((d, i) => (i % every === 0 || i === n - 1) && !(i !== n - 1 && n - 1 - i < every / 2)
    ? `<text class="axis" x="${x(i)}" y="${h - 6}" text-anchor="${i === 0 ? "start" : i === n - 1 ? "end" : "middle"}">${short(d)}</text>` : "").join("");
  let marks = "";
  if (kind === "line") {
    for (const it of items) {
      // 断开没有数据的日子；今天那一段画成虚线（还没算完）
      let path = "";
      let dashed = "";
      it.values.forEach((v, i) => {
        if (v === null) return;
        const prev = i > 0 ? it.values[i - 1] : null;
        const point = `${x(i).toFixed(1)},${y(v).toFixed(1)}`;
        if (days[i] === today && prev !== null) dashed = `M${x(i - 1).toFixed(1)},${y(prev).toFixed(1)}L${point}`;
        else path += prev === null || i === 0 ? `M${point}` : `L${point}`;
      });
      marks += `<path class="line ${it.cls}" d="${path}"/>${dashed ? `<path class="line ${it.cls} partial" d="${dashed}"/>` : ""}`;
    }
  } else {
    const bw = Math.max(1, band - 2);  // 柱子之间 2px 的空
    days.forEach((d, i) => {
      let base = 0;
      for (const it of items) {
        const v = it.values[i] ?? 0;
        if (v <= 0) continue;
        const top = y(base + v);
        const height = y(base) - top - (base > 0 ? 1 : 0);  // 叠起来的段之间留一点空
        if (height > 0) marks += `<rect class="bar ${it.cls}${d === today ? " partial" : ""}" x="${(x(i) - bw / 2).toFixed(1)}" y="${top.toFixed(1)}" width="${bw.toFixed(1)}" height="${height.toFixed(1)}"/>`;
        base += v;
      }
    });
  }
  // 悬停：每一天一整列（比线、柱子大得多，好对准）
  const hit = days.map((d, i) => {
    const lines = items.map((it) => `${it.name} ${it.values[i] === null ? "没有数据" : tick(it.values[i])}`);
    const tip = `${d}${d === today ? "（今天，还没算完）" : ""}\n${lines.join("\n")}${kind === "stack" ? `\n合计 ${tick(totals[i])}` : ""}`;
    const left = kind === "stack" ? m.l + band * i : i === 0 ? m.l : (x(i - 1) + x(i)) / 2;
    const right = kind === "stack" ? m.l + band * (i + 1) : i === n - 1 ? w - m.r : (x(i) + x(i + 1)) / 2;
    return `<rect class="hit" x="${left.toFixed(1)}" y="${m.t}" width="${Math.max(1, right - left).toFixed(1)}" height="${ph}" data-tip="${escape(tip)}"/>`;
  }).join("");
  return `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="${escape(items.map((it) => it.name).join("、"))}的走势">${grid}${marks}${xLabels}${hit}</svg>`;
}

function chartCard({ title, note = "", kind, items, days, today, unit = "" }) {
  const legend = items.length > 1 ? `<ul class="legend">${items.map((it) => `<li><span class="swatch ${it.cls}"></span>${escape(it.name)}</li>`).join("")}</ul>` : "";
  const rows = days.map((d, i) => `<tr><td>${d}${d === today ? "（今天）" : ""}</td>${items.map((it) => `<td>${it.values[i] === null ? "—" : `${fmt(it.values[i])}${unit}`}</td>`).join("")}</tr>`).reverse().join("");
  return `<section class="card stack viz">
    <div class="title-row"><h2>${escape(title)}</h2>${legend}</div>
    ${note ? `<p class="small muted">${note}</p>` : ""}
    <div class="chart-wide">${chartSvg({ kind, items, days, today, unit, w: 720, h: 240, labels: 7 })}</div>
    <div class="chart-narrow">${chartSvg({ kind, items, days, today, unit, w: 360, h: 220, labels: 3 })}</div>
    <details class="rest"><summary>看数字</summary><div class="table-wrap"><table class="nums"><thead><tr><th>日期</th>${items.map((it) => `<th>${escape(it.name)}</th>`).join("")}</tr></thead>
      <tbody>${rows}</tbody></table></div></details>
  </section>`;
}

function tile(name, value, note) {
  return `<div class="tile"><div class="tile-head">${escape(name)}</div><p class="hero">${value}</p><p>${note}</p></div>`;
}

// 版本号比大小（0.6.10 > 0.6.9）
function versionKey(v) {
  return String(v).split(/[.\-b]/).map((x) => Number.parseInt(x, 10) || 0);
}
function byVersionDesc(a, b) {
  const x = versionKey(a);
  const y = versionKey(b);
  for (let i = 0; i < Math.max(x.length, y.length); i++) if ((x[i] ?? 0) !== (y[i] ?? 0)) return (y[i] ?? 0) - (x[i] ?? 0);
  return 0;
}

export function statsPage(me, { doc, range, now, banner = "" }) {
  const today = isoDay(now + 8 * HOUR);  // 北京时间的今天（玩家大多在国内）
  const yesterday = isoDay(now + 8 * HOUR - DAY);
  const first = doc.first;
  if (!first || !Object.keys(doc.days).length) {
    return layout({ title: "数据", me, current: "stats", banner, body: `
<div class="stack"><h1>数据</h1><p class="muted">Evo 的使用数据：日活、对局、版本、识别情况、Evo 的占用。</p></div>
<p class="note note-info">还没算过：定时任务每 3 分钟看一次，第一次要从最早的一天补算，过十几分钟再来看。</p>` });
  }
  const span = range === "all" ? null : Number(range);
  const from = span ? isoDay(Date.parse(`${today}T00:00:00Z`) - (span - 1) * DAY) : first;
  const rows = series(doc, from < first ? first : from, today);
  const days = rows.map((r) => r.day);
  const val = (key) => rows.map((r) => (r.missing ? null : r[key] ?? 0));
  const ratio = (part) => rows.map((r) => (r.missing || !r.games ? null : (r[part] / r.games) * 100));
  const day = (d) => doc.days[d];
  const week = Array.from({ length: 7 }, (_, i) => isoDay(Date.parse(`${yesterday}T00:00:00Z`) - i * DAY)).map(day).filter(Boolean);
  const avg = week.length ? week.reduce((s, x) => s + x.dau, 0) / week.length : null;
  const total = Object.values(doc.days).reduce((s, x) => s + (x.fresh ?? 0), 0);
  const y = day(yesterday);
  const before = day(isoDay(Date.parse(`${yesterday}T00:00:00Z`) - DAY));
  const change = y && before?.dau ? `比前一天 ${y.dau >= before.dau ? "+" : ""}${(((y.dau - before.dau) / before.dau) * 100).toFixed(0)}%` : "";
  const tiles = `<section class="tiles kpis" aria-label="几个数">
    ${tile("昨天日活", fmt(y?.dau), `${short(yesterday)}${change ? ` · ${change}` : ""}`)}
    ${tile("近 7 天平均日活", fmt(avg), `${week.length} 天的平均`)}
    ${tile("昨天的对局", fmt(y?.games), `${fmt(y?.players)} 人打了`)}
    ${tile("开过匿名统计的玩家", fmt(total), `从 ${first} 起，第一次出现就算一个`)}</section>`;
  // 版本：最近一个整天（昨天；昨天没数就往前找）
  const latest = [yesterday, ...Object.keys(doc.days).sort().reverse()].find((d) => d <= yesterday && day(d)?.dau);
  const vers = latest ? Object.entries(day(latest).versions).sort((a, b) => byVersionDesc(a[0], b[0])) : [];
  const vmax = Math.max(1, ...vers.map(([, c]) => c));
  const vsum = vers.reduce((s, [, c]) => s + c, 0);
  const versionCard = vers.length ? `<section class="card stack"><h2>在用的版本</h2>
    <p class="small muted">${latest}（最近一个整天）开着 Evo 的人，各用哪个版本。</p>
    <ul class="hbars">${vers.map(([v, c]) => `<li><span class="hb-name">${escape(v)}</span>
      <span class="hb-track"><span class="hb-bar s1" style="width:${((c / vmax) * 100).toFixed(1)}%"></span></span>
      <span class="hb-val">${fmt(c)} 人 · ${((c / vsum) * 100).toFixed(0)}%</span></li>`).join("")}</ul></section>` : "";
  const usage = Object.entries(usageByVersion(rows.filter((r) => !r.missing))).sort((a, b) => byVersionDesc(a[0], b[0]));
  const usageCard = usage.length ? `<section class="card stack"><h2>Evo 自己的占用</h2>
    <p class="small muted">对局中 Evo 平均占一个 CPU 核的多少、内存（${escape(RANGES[range] ?? "")}，0.5.9 起的 Evo 才发）。目标：识别时内存不超过 300 MB。</p>
    <div class="table-wrap"><table class="nums"><thead><tr><th>版本</th><th>局数</th><th>平均 CPU（一个核）</th><th>平均内存</th><th>最高内存</th></tr></thead>
    <tbody>${usage.map(([v, u]) => `<tr><td>${escape(v)}</td><td>${fmt(u.n)}</td><td>${pctText(u.cpu / u.n)}</td><td>${fmt(u.mem / u.n)} MB</td>
      <td>${fmt(u.peak)} MB</td></tr>`).join("")}</tbody></table></div></section>` : "";
  const tabs = `<nav class="tabs" aria-label="看多长时间">${Object.entries(RANGES).map(([k, v]) => `<a href="/stats${k === "30" ? "" : `?range=${k}`}"${String(range) === k ? ' aria-current="page"' : ""}>${v}</a>`).join("")}</nav>`;
  return layout({ title: "数据", me, current: "stats", banner, body: `
<div class="stack"><h1>数据</h1>
  <p class="muted">Evo 的使用数据，只算<b>开了匿名统计的玩家</b>（默认不开，所以比真实的玩家少）。日期是玩家电脑上的日期；对局结束 1 小时以后才传上来，最右边的今天还没算完（虚线、浅色）。
    这些数是 ${escape(ago(doc.computedAt, now))}算的，每 3 小时算一次。只有汇总的数，看不到任何一个人的数据。</p></div>
${tiles}
${tabs}
${chartCard({ title: "日活", note: "每天开着 Evo 的人，和其中打了对局的人", kind: "line", days, today,
  items: [{ name: "开着 Evo", cls: "s1", values: val("dau") }, { name: "打了对局", cls: "s2", values: val("players") }] })}
${chartCard({ title: "新玩家", note: "这一天第一次出现的人（第一次开匿名统计，或者重装以后换了匿名编号）", kind: "stack", days, today,
  items: [{ name: "新玩家", cls: "s1", values: val("fresh") }] })}
${chartCard({ title: "每天的对局", note: "排位、匹配叠起来；认不出模式的算其他", kind: "stack", days, today,
  items: [{ name: "排位", cls: "s1", values: val("ranked") }, { name: "匹配", cls: "s2", values: val("pubs") },
          { name: "其他", cls: "s3", values: rows.map((r) => (r.missing ? null : Math.max(0, (r.games ?? 0) - (r.ranked ?? 0) - (r.pubs ?? 0)))) }] })}
${chartCard({ title: "识别情况", note: "每天的对局里标了待确认的、玩家在战绩页改过的占多少（越低说明 Evo 读得越准）", kind: "line", days, today, unit: "%",
  items: [{ name: "待确认", cls: "s1", values: ratio("review") }, { name: "玩家改过", cls: "s2", values: ratio("edited") }] })}
${versionCard}
${usageCard}` });
}
