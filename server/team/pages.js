// 协作网页的页面（服务器直接拼好 HTML，不用框架）。颜色、字号照 Evo 窗口（panel/static/style.css）：
// 深色是默认，系统是浅色时换浅色。手机（包括微信里打开）和电脑都能用：按钮至少 44 像素高，输入框 16 像素字（iPhone 不会自动放大）。

import { ROLES, INVITE_DAYS, SESSION_DAYS, can } from "./auth.js";

export function escape(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// Evo 的图标（assets/icon/evo.svg）
const ICON = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024"><rect width="1024" height="1024" rx="224" fill="#0F141A"/><path d="M256 240L384 240L384 456L664 456L600 568L384 568L384 672L568 672L504 784L256 784Z" fill="#E8ECF1"/><path d="M384 240L768 240L704 352L384 352Z" fill="#3B9EFF"/></svg>`;
const FAVICON = `data:image/svg+xml,${encodeURIComponent(ICON)}`;

const CSS = `
:root {
  --bg: #0D0F13; --card: #151820; --inset: #0D0F13; --text: #ECEEF2; --muted: #9AA3B2;
  --border: #262C38; --row-border: #222733; --input-border: #3A4150; --nav-on-bg: #1B1F29;
  --accent: #FF5A5A; --primary-bg: #FF5A5A; --primary-fg: #2A0A0A; --bad: #FF8A8A; --danger-border: #6B2A2E;
  --ok-bg: #0F1C17; --ok-border: #1F4A3A; --ok-text: #7CE8B8;
  --info-bg: #0F1A2B; --info-border: #24456E; --info-text: #8EC0FF;
  --warn-bg: #1E1A10; --warn-border: #5A4A1E; --warn-text: #F4D68A;
  --focus: #8EC0FF;
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --band: rgba(255,255,255,.07);  /* 图表的三种颜色（dataviz 参考色板的深色那一套，验过） */
  color-scheme: dark;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #F6F6F3; --card: #FFFFFF; --inset: #F6F6F3; --text: #17191E; --muted: #5F6670;
    --border: #E4E4DF; --row-border: #EDEDEA; --input-border: #C9C9C3; --nav-on-bg: #E7E7E2;
    --accent: #D93636; --primary-bg: #D93636; --primary-fg: #FFFFFF; --bad: #B42828; --danger-border: #E3B4B4;
    --ok-bg: #EEF7F2; --ok-border: #BFE0CF; --ok-text: #1E7A52;
    --info-bg: #EEF4FC; --info-border: #BCD3F0; --info-text: #1D4F99;
    --warn-bg: #FBF5E3; --warn-border: #E8D9A8; --warn-text: #6B4E06;
    --focus: #1D4F99;
    --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --band: rgba(0,0,0,.05);
    color-scheme: light;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body { background: var(--bg); color: var(--text); font: 16px/1.6 system-ui, -apple-system, "PingFang SC", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
  -webkit-text-size-adjust: 100%; }
a { color: var(--info-text); }
h1 { font-size: 24px; line-height: 1.3; margin: 0; }
h2 { font-size: 18px; line-height: 1.4; margin: 0 0 12px; }
p { margin: 0; }
.muted { color: var(--muted); }
.small { font-size: 14px; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }

.top { border-bottom: 1px solid var(--border); background: var(--card); }
.top-inner { max-width: 960px; margin: 0 auto; padding: 10px 16px; display: flex; flex-wrap: wrap; align-items: center; gap: 8px 20px; }
.brand { display: flex; align-items: center; gap: 10px; color: var(--text); text-decoration: none; min-height: 44px; }
.brand svg { width: 28px; height: 28px; flex: none; }
.brand b { font-weight: 700; font-size: 20px; letter-spacing: 3px; }
.brand span { color: var(--muted); font-size: 14px; }
.nav { display: flex; flex-wrap: wrap; gap: 4px; }
.nav a { display: flex; align-items: center; min-height: 44px; padding: 0 14px; border-radius: 6px; color: var(--muted); text-decoration: none; }
.nav a:hover { color: var(--text); }
.nav a[aria-current="page"] { background: var(--nav-on-bg); color: var(--text); font-weight: 600; }
.me { margin-left: auto; display: flex; align-items: center; gap: 10px; font-size: 14px; min-width: 0; }
.me-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 10em; }
.me form { margin: 0; }

.wrap { max-width: 960px; margin: 0 auto; padding: 24px 16px 48px; display: flex; flex-direction: column; gap: 20px; }
.narrow { max-width: 560px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }
.stack { display: flex; flex-direction: column; gap: 12px; }
.title-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }

.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 44px; padding: 0 18px;
  border-radius: 6px; border: 1px solid var(--border); background: var(--nav-on-bg); color: var(--text);
  font: inherit; font-size: 15px; white-space: nowrap; cursor: pointer; text-decoration: none; transition: background-color .15s, border-color .15s; }
.btn:hover { border-color: var(--muted); }
.btn-primary { background: var(--primary-bg); border-color: var(--primary-bg); color: var(--primary-fg); font-weight: 600; }
.btn-primary:hover { border-color: var(--primary-bg); filter: brightness(1.06); }
.btn-danger { background: transparent; border-color: var(--danger-border); color: var(--bad); }
.btn-danger:hover { border-color: var(--bad); }
.btn-quiet { background: transparent; }
.btn-wide { width: 100%; }
@media (prefers-reduced-motion: reduce) { .btn { transition: none; } }

.badge { display: inline-flex; align-items: center; padding: 1px 10px; border-radius: 999px; font-size: 13px;
  border: 1px solid var(--border); color: var(--muted); white-space: nowrap; }
.badge-admin { border-color: var(--danger-border); color: var(--accent); }
.badge-editor { border-color: var(--info-border); color: var(--info-text); }
.badge-soon { border-style: dashed; }

.note { border-radius: 8px; padding: 12px 16px; border: 1px solid; }
.note-ok { background: var(--ok-bg); border-color: var(--ok-border); color: var(--ok-text); }
.note-error { background: var(--warn-bg); border-color: var(--warn-border); color: var(--warn-text); }
.note-info { background: var(--info-bg); border-color: var(--info-border); color: var(--info-text); }

label.field { display: flex; flex-direction: column; gap: 6px; font-weight: 600; }
.help { font-weight: 400; font-size: 14px; color: var(--muted); }
.input { width: 100%; min-height: 44px; padding: 8px 12px; border-radius: 6px; border: 1px solid var(--input-border);
  background: var(--inset); color: var(--text); font: inherit; font-size: 16px; }
.mono { font-family: ui-monospace, Consolas, monospace; font-size: 15px; }
fieldset { border: 0; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
legend { font-weight: 600; margin-bottom: 6px; padding: 0; }
.option { display: flex; gap: 12px; align-items: flex-start; padding: 12px 14px; border: 1px solid var(--border);
  border-radius: 8px; cursor: pointer; }
.option:has(input:checked) { border-color: var(--info-border); background: var(--info-bg); }
.option input { width: 20px; height: 20px; margin: 2px 0 0; flex: none; accent-color: var(--accent); }
.option b { display: block; font-weight: 600; }
.option span { display: block; font-size: 14px; color: var(--muted); }
.check { display: flex; gap: 10px; align-items: center; min-height: 44px; cursor: pointer; }
.check input { width: 20px; height: 20px; margin: 0; accent-color: var(--accent); }
.copy-row { display: flex; gap: 8px; }
.copy-row .input { flex: 1; min-width: 0; }
.hints { margin: 0; padding-left: 20px; color: var(--muted); font-size: 14px; display: flex; flex-direction: column; gap: 4px; }

.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
.tile { display: flex; flex-direction: column; gap: 6px; padding: 16px; border: 1px solid var(--border); border-radius: 10px;
  background: var(--card); color: var(--text); text-decoration: none; }
a.tile:hover { border-color: var(--muted); }
.tile-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; font-weight: 600; }
.tile p { font-size: 14px; color: var(--muted); }

.list { list-style: none; margin: 0; padding: 0; }
.row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px; padding: 14px 0; border-top: 1px solid var(--row-border); }
.row:first-child { border-top: 0; padding-top: 0; }
.row-main { flex: 1 1 260px; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.row-name { font-weight: 600; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
/* 很长又没有空格的名字（Liquipedia 的页面名、链接）在手机上折行，不撑出屏幕 */
.row-name, .row-meta, .help, .note { overflow-wrap: anywhere; }
.search-row { display: flex; gap: 8px; }
.logo-box { width: 48px; height: 48px; flex: none; padding: 6px; border-radius: 8px; background: #14171F; display: inline-flex;
  align-items: center; justify-content: center; }
.logo-box img { max-width: 100%; max-height: 100%; object-fit: contain; }
.logo-box[hidden] { display: none; }
.logo-small { width: 36px; height: 36px; padding: 4px; border-radius: 6px; }
.logo-big { width: 72px; height: 72px; padding: 8px; }
.logo-grid { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 10px; }
.logo-item { display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 12px 8px; border: 1px solid var(--border);
  border-radius: 8px; text-align: center; overflow-wrap: anywhere; }
.logo-item form { margin: 0; }
.main-row { display: grid; grid-template-columns: 7em 1fr; gap: 6px; }  /* 主舞台一行：名字（手机上占一整行）、平台、房间号 */
.main-row + .main-row { margin-top: 4px; padding-top: 10px; border-top: 1px dashed var(--border); }
.main-row input[name="main_name"] { grid-column: 1 / -1; }
@media (min-width: 560px) { .main-row { grid-template-columns: 1fr 7em 10em; } .main-row input[name="main_name"] { grid-column: auto; } }
.row[id], section[id] { scroll-margin-top: 72px; }  /* 保存以后跳回那一行（地址后面的 #），别被顶上的导航挡住 */
.row:target { background: var(--target, rgba(59, 130, 246, .10)); border-radius: 6px; }
.pick-row { display: flex; align-items: center; gap: 10px; min-height: 44px; cursor: pointer; }
.pick-row input { width: 18px; height: 18px; margin: 0; flex: none; accent-color: var(--accent); }
.logo-link { display: inline-flex; flex-direction: column; align-items: center; gap: 2px; color: var(--muted); text-decoration: none; }
.logo-empty { color: #9AA3B2; font-size: 18px; }
.search-row .input { flex: 1; min-width: 0; }
.place { font-weight: 700; margin-right: 2px; }
details.more > summary { list-style: none; margin-top: 12px; }
details.more > summary::-webkit-details-marker { display: none; }
details.more[open] > summary { display: none; }
.row-meta { font-size: 14px; color: var(--muted); display: flex; flex-wrap: wrap; gap: 4px 10px; align-items: center; }
.row-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-start; }
.row-actions form { margin: 0; }
details.act { position: relative; }
details.act > summary { list-style: none; }
details.act > summary::-webkit-details-marker { display: none; }
details.act[open] > summary { border-color: var(--muted); }
/* 手机上展开的面板占满一整行（放在按钮下面）；电脑上是挂在按钮右下方的小框 */
details.act[open] { flex: 1 1 100%; }
.panel { margin-top: 8px; padding: 14px; border: 1px solid var(--border); border-radius: 8px; background: var(--inset);
  display: flex; flex-direction: column; gap: 10px; }
@media (min-width: 720px) {
  .top-inner { padding: 10px 24px; }
  .wrap { padding: 32px 24px 64px; }
  .card { padding: 24px; }
  details.act[open] { flex: none; }
  details.act[open] > .panel { position: absolute; right: 0; z-index: 5; width: 360px; box-shadow: 0 8px 24px rgba(0,0,0,.25); }
}

h3 { font-size: 15px; margin: 4px 0 0; color: var(--muted); font-weight: 600; }
.badge-warn { border-color: var(--warn-border); color: var(--warn-text); background: var(--warn-bg); }
.badge-live { border-color: var(--ok-border); color: var(--ok-text); background: var(--ok-bg); }
.badge-upcoming { border-color: var(--info-border); color: var(--info-text); }
.badge-ended { color: var(--muted); }
.warn { color: var(--warn-text); }
.grow { flex: 1; }
.plain { margin: 6px 0 0; padding-left: 20px; }
.note b { font-weight: 600; }
textarea.input { min-height: 120px; resize: vertical; line-height: 1.5; }
select.input { appearance: auto; }
.two { display: grid; gap: 12px; grid-template-columns: 1fr; }
@media (min-width: 560px) { .two { grid-template-columns: 1fr 1fr; } }
.fold { padding: 0; }
.fold > summary { list-style: none; cursor: pointer; display: flex; align-items: center; gap: 12px;
  min-height: 56px; padding: 0 20px; font-weight: 600; }
.fold > summary::-webkit-details-marker { display: none; }
.fold > summary::after { content: "+"; margin-left: auto; font-size: 20px; color: var(--muted); }
.fold[open] > summary::after { content: "−"; }
.fold > form, .fold > div { padding: 0 20px 20px; }
.inline-options { display: flex; flex-wrap: wrap; gap: 8px; }
.option-inline { padding: 8px 14px; align-items: center; min-height: 44px; }
.picks { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 6px; margin-top: 6px; }
.pick { display: flex; align-items: center; gap: 8px; min-height: 44px; padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; cursor: pointer; }
.pick:has(input:checked) { border-color: var(--info-border); background: var(--info-bg); }
.pick input { width: 18px; height: 18px; margin: 0; flex: none; accent-color: var(--accent); }
.pick span:first-of-type { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.team-head { margin-bottom: 4px; }
.team-head h2 { margin: 0; }
.list.compact .row { padding: 10px 0; }
.switches { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.btn-small { min-height: 36px; padding: 0 12px; font-size: 14px; }
.btn[disabled] { opacity: .6; cursor: not-allowed; }
.tile-count { font-size: 14px; color: var(--text); }
/* 收起来的提示：点"还有 N 条"、点标题展开 */
details.rest > summary, details.note > summary { list-style: none; cursor: pointer; min-height: 44px; display: flex; align-items: center; gap: 8px; font-weight: 600; }
details.rest > summary { margin-top: 4px; }
details.note > summary::-webkit-details-marker, details.rest > summary::-webkit-details-marker { display: none; }
details.note > summary::before, details.rest > summary::before { content: "▸"; flex: none; }
details.note[open] > summary::before, details.rest[open] > summary::before { content: "▾"; }
details.note > summary .help { color: inherit; opacity: .85; }
.team-pend li + li { margin-top: 4px; }
/* 批量填直播间：手机上一人一块（名字一行，平台、房间号并排），电脑上一人一行 */
.tabs { display: flex; flex-wrap: wrap; gap: 4px; border-bottom: 1px solid var(--border); }
.tabs a { display: flex; align-items: center; min-height: 44px; padding: 0 14px; color: var(--muted); text-decoration: none; border-bottom: 2px solid transparent; margin-bottom: -1px; }
.tabs a[aria-current="page"] { color: var(--text); font-weight: 600; border-bottom-color: var(--accent); }
.bulk-team { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
.bulk-team h3 { margin: 0 0 8px; color: var(--text); font-size: 16px; }
.bulk-row { display: grid; grid-template-columns: 9.5em 1fr; gap: 6px 8px; align-items: center; padding: 10px 0; border-top: 1px solid var(--row-border); }
.bulk-row:first-child { border-top: 0; padding-top: 0; }
.bulk-who { grid-column: 1 / -1; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; overflow-wrap: anywhere; }
@media (min-width: 720px) {
  .bulk-row { grid-template-columns: minmax(10em, 1fr) 9em 12em; }
  .bulk-who { grid-column: auto; }
}
.bulk-bar { position: sticky; bottom: 0; z-index: 4; display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; padding: 12px 16px;
  background: var(--card); border: 1px solid var(--border); border-radius: 10px; box-shadow: 0 -6px 18px rgba(0,0,0,.18); }
/* 一键配队徽：勾选、队名、猜的那张、换一张 */
.match-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; padding: 10px 0; border-top: 1px solid var(--row-border); }
.match-row:first-child { border-top: 0; padding-top: 0; }
.match-pick { display: flex; align-items: center; gap: 10px; min-height: 44px; flex: 1 1 180px; min-width: 0; cursor: pointer; font-weight: 600; overflow-wrap: anywhere; }
.match-pick input { width: 20px; height: 20px; margin: 0; flex: none; accent-color: var(--accent); }
.match-logo { display: flex; align-items: center; gap: 10px; flex: 1 1 200px; min-width: 0; overflow-wrap: anywhere; }
.match-row > a { min-height: 44px; display: inline-flex; align-items: center; }
.banner { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px; }
.links { display: flex; flex-wrap: wrap; gap: 4px 20px; }
/* 「数据」页（pages_stats.js）：图、图例、横条、数字表、悬停的小框 */
.tiles.kpis { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }  /* 手机上两个一行 */
.tile p.hero { font-size: 28px; line-height: 1.2; font-weight: 700; color: var(--text); font-variant-numeric: tabular-nums; }
.viz .title-row h2 { margin: 0; }
.chart { display: block; width: 100%; height: auto; }
.chart .grid { stroke: var(--row-border); stroke-width: 1; }
.chart .axis { fill: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.chart .line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.chart .line.s1 { stroke: var(--s1); } .chart .line.s2 { stroke: var(--s2); } .chart .line.s3 { stroke: var(--s3); }
.chart .line.partial { stroke-dasharray: 4 4; }
.chart .bar.s1 { fill: var(--s1); } .chart .bar.s2 { fill: var(--s2); } .chart .bar.s3 { fill: var(--s3); }
.chart .bar.partial { opacity: .45; }
.chart .hit { fill: transparent; }
.chart .hit:hover { fill: var(--band); }
.chart-narrow { display: none; }
@media (max-width: 599px) { .chart-wide { display: none; } .chart-narrow { display: block; } }
.legend { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 14px; color: var(--muted); }
.legend li { display: flex; align-items: center; gap: 6px; }
.swatch { width: 12px; height: 12px; border-radius: 3px; flex: none; }
.swatch.s1 { background: var(--s1); } .swatch.s2 { background: var(--s2); } .swatch.s3 { background: var(--s3); }
.hbars { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.hbars li { display: grid; grid-template-columns: 6em 1fr auto; gap: 10px; align-items: center; font-size: 14px; font-variant-numeric: tabular-nums; }
.hb-track { height: 12px; }
.hb-bar { display: block; height: 100%; min-width: 2px; border-radius: 0 4px 4px 0; }
.hb-bar.s1 { background: var(--s1); }
.hb-val { color: var(--muted); }
.table-wrap { overflow-x: auto; }
table.nums { border-collapse: collapse; width: 100%; font-size: 14px; font-variant-numeric: tabular-nums; }
table.nums th, table.nums td { padding: 6px 10px; text-align: right; border-bottom: 1px solid var(--row-border); white-space: nowrap; }
table.nums th:first-child, table.nums td:first-child { text-align: left; }
table.nums th { color: var(--muted); font-weight: 600; }
table.nums tr.sum td { font-weight: 600; border-bottom: 0; }
#viz-tip { position: fixed; z-index: 50; pointer-events: none; max-width: 260px; padding: 8px 10px; border-radius: 8px; border: 1px solid var(--border);
  background: var(--card); color: var(--text); font-size: 13px; line-height: 1.5; white-space: pre-line; box-shadow: 0 6px 18px rgba(0,0,0,.25); }
.banner-text { flex: 1 1 240px; min-width: 0; }
@media (min-width: 720px) {
  .fold > summary { padding: 0 24px; }
  .fold > form, .fold > div { padding: 0 24px 24px; }
  details.act[open] > .panel-wide { width: min(640px, 90vw); }
}
`;

// 复制按钮（微信里复制不了时提示长按）。放在单独的 /app.js：网页只许运行本站的脚本
export const APP_JS = `
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const input = document.getElementById(button.dataset.copy);
  if (!input) return;
  input.focus();
  input.select();
  input.setSelectionRange(0, input.value.length);
  let copied = false;
  try { await navigator.clipboard.writeText(input.value); copied = true; }
  catch { try { copied = document.execCommand("copy"); } catch { copied = false; } }
  button.textContent = copied ? "已复制" : "请长按复制";
  setTimeout(() => { button.textContent = "复制"; }, 2500);
});
// 主舞台名字那一格从下拉（datalist main-rooms）里选了一位（名字一字不差）：平台、房间号换成他的（11-21 直播间记忆；
// 复制来的一场把某一行换成别人时也跟着换）
document.addEventListener("input", (event) => {
  const input = event.target;
  if (!input.matches || !input.matches("input[name=main_name]") || !input.list) return;
  const option = [...input.list.options].find((o) => o.value === input.value.trim());
  const row = input.closest(".main-row");
  if (!option || !row) return;
  const platform = row.querySelector("select[name=main_platform]");
  const room = row.querySelector("input[name=main_room]");
  if (platform && room) {
    platform.value = option.dataset.p;
    room.value = option.dataset.r;
  }
});
// 上传队徽：先在浏览器里缩到最长边 256（不放大）、60 KB 以内，存成 WebP（导不出 WebP 的浏览器存 PNG），再换掉要上传的文件
document.addEventListener("change", async (event) => {
  const input = event.target;
  if (!input.matches || !input.matches("input[type=file][data-logo]")) return;
  const file = input.files && input.files[0];
  const note = document.getElementById(input.dataset.logo);
  const box = document.getElementById(input.dataset.logo + "-box");
  const preview = document.getElementById(input.dataset.logo + "-img");
  if (!file || !note) return;
  try {
    const url = URL.createObjectURL(file);
    const image = await new Promise((ok, fail) => { const i = new Image(); i.onload = () => ok(i); i.onerror = fail; i.src = url; });
    let blob = null;
    let size = "";
    for (const side of [256, 192, 128]) {
      const scale = Math.min(1, side / Math.max(image.naturalWidth, image.naturalHeight));
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
      canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
      canvas.getContext("2d").drawImage(image, 0, 0, canvas.width, canvas.height);
      blob = await new Promise((ok) => canvas.toBlob(ok, "image/webp", 0.9));
      if (!blob || blob.type !== "image/webp") blob = await new Promise((ok) => canvas.toBlob(ok, "image/png"));
      size = canvas.width + "×" + canvas.height;
      if (blob && blob.size <= 60 * 1024) break;
    }
    URL.revokeObjectURL(url);
    if (!blob || blob.size > 60 * 1024) { note.textContent = "这张图压不到 60 KB 以内：换一张简单一点的"; return; }
    const ext = blob.type === "image/webp" ? "webp" : "png";
    const list = new DataTransfer();
    list.items.add(new File([blob], "logo." + ext, { type: blob.type }));
    input.files = list.files;
    if (preview && box) { preview.src = URL.createObjectURL(blob); box.hidden = false; }
    note.textContent = "缩好了：" + size + "，" + Math.ceil(blob.size / 1024) + " KB。看一眼在深色底上清不清楚，再点上传";
  } catch {
    note.textContent = "这张图读不出来：换一张 PNG、JPG 或 WebP";
  }
});
// 「数据」页的图：悬停（手机上点一下）看那一天的数（每一天一整列，data-tip 里是要显示的字）
let vizTip = null;
function showVizTip(event) {
  const target = event.target.closest && event.target.closest("[data-tip]");
  if (!target) { if (vizTip) vizTip.hidden = true; return; }
  if (!vizTip) { vizTip = document.createElement("div"); vizTip.id = "viz-tip"; document.body.appendChild(vizTip); }
  vizTip.textContent = target.dataset.tip;
  vizTip.hidden = false;
  const box = vizTip.getBoundingClientRect();
  let left = event.clientX + 14;
  if (left + box.width > innerWidth - 8) left = event.clientX - 14 - box.width;
  let top = event.clientY + 14;
  if (top + box.height > innerHeight - 8) top = event.clientY - 14 - box.height;
  vizTip.style.left = Math.max(8, left) + "px";
  vizTip.style.top = Math.max(8, top) + "px";
}
document.addEventListener("pointermove", showVizTip);
document.addEventListener("pointerdown", showVizTip);
document.addEventListener("scroll", () => { if (vizTip) vizTip.hidden = true; }, { passive: true });
document.addEventListener("focusin", (event) => {
  if (event.target.matches && event.target.matches("input[readonly]")) event.target.select();
});
`;

// ---------- 小工具 ----------

// 一串提示（发布前要改的、提醒）：前几条直接列出来，多的收起来，免得一长串把页面正文挤到下面去（2026-11-19）
export function noteList(kind, title, items, show = 5) {
  if (!items.length) return "";
  const li = (t) => `<li>${escape(t)}</li>`;
  const rest = items.slice(show);
  return `<div class="note note-${kind}"><b>${escape(title)}</b><ul class="plain">${items.slice(0, show).map(li).join("")}</ul>
    ${rest.length ? `<details class="rest"><summary>还有 ${rest.length} 条</summary><ul class="plain">${rest.map(li).join("")}</ul></details>` : ""}</div>`;
}

const BEIJING = 8 * 3600 * 1000;

function beijing(ms) {
  return new Date(ms + BEIJING).toISOString().slice(0, 16).replace("T", " ");
}

// "刚刚"、"3 小时前"、"5 天前"，再早写日期（北京时间）
export function ago(ms, now) {
  if (!ms) return "还没来过";
  const minutes = Math.floor((now - ms) / 60000);
  if (minutes < 2) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 48 * 60) return `${Math.floor(minutes / 60)} 小时前`;
  if (minutes < 30 * 24 * 60) return `${Math.floor(minutes / 1440)} 天前`;
  return beijing(ms).slice(0, 10);
}

function inDays(ms, now) {
  const hours = Math.ceil((ms - now) / 3600000);
  return hours <= 24 ? `${Math.max(hours, 1)} 小时后过期` : `${Math.ceil(hours / 24)} 天后过期`;
}

function roleBadge(role) {
  return `<span class="badge badge-${escape(role)}">${escape(ROLES[role] ?? role)}</span>`;
}

export function abilities(member) {
  if (member.role === "admin") return "邀请成员、改选手库和赛程、发布到 Evo，什么都能做";
  if (member.role === "editor") {
    return member.publish ? "改选手库、赛程、收录哪些比赛，也能发布到 Evo" : "改选手库、赛程、收录哪些比赛（发布到 Evo 要找管理员）";
  }
  return "只能看，不能改";
}

// 页面顶上的"有改动还没发布"（publish.js 的 unpublished；首页、选手库、赛程、发布页上显示）。onPublish：就在发布页上，不放"去发布"
export function publishBanner(me, info, now, onPublish = false) {
  if (!info || !me) return "";
  const last = info.last ? `玩家现在看到的是 ${info.last.by} ${ago(info.last.at, now)}发的。` : "正式文件还没从这个网页发过。";
  const who = info.who.length ? `之后 ${info.who.join("、")} 改过。` : "";
  const action = onPublish ? "" : can(me, "publish") ? `<a class="btn btn-primary" href="/publish">去发布</a>`
    : `<span class="small">请管理员（或者能发布的人）发布。</span>`;
  return `<div class="note note-error banner" role="status"><div class="banner-text"><b>有改动还没发布到 Evo</b>${info.errors ? `<span>（现在还发不了：有 ${info.errors} 处要改）</span>` : ""}
    <p class="small">${escape(last)}${escape(who)}</p></div>${action}</div>`;
}

// ---------- 外框 ----------

export function layout({ title, me = null, current = "", body, narrow = false, banner = "" }) {
  const link = (href, key, text) => `<a href="${href}"${current === key ? ' aria-current="page"' : ""}>${text}</a>`;
  const nav = me ? `<nav class="nav" aria-label="主导航">
      ${link("/", "home", "首页")}${link("/stats", "stats", "数据")}${link("/players", "players", "选手库")}${link("/events", "events", "赛程")}${link("/publish", "publish", "发布")}${link("/pros", "pros", "资料")}
      ${me.role === "admin" ? link("/members", "members", "成员") : ""}
    </nav>
    <div class="me"><span class="me-name" title="${escape(me.name)}">${escape(me.name)}</span>${roleBadge(me.role)}
      <form method="post" action="/logout"><button class="btn btn-quiet" type="submit">退出</button></form></div>` : "";
  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>${escape(title)} · Evo 后台</title>
<link rel="icon" href="${FAVICON}">
<style>${CSS}</style>
</head>
<body>
<header class="top"><div class="top-inner">
  <a class="brand" href="/" aria-label="Evo 后台 首页">${ICON}<b>EVO</b><span>后台</span></a>
  ${nav}
</div></header>
<main class="wrap${narrow ? " narrow" : ""}">
${banner}${body}
</main>
<script src="/app.js" defer></script>
</body>
</html>`;
}

// ---------- 页面 ----------

export function loginPage() {
  return layout({ title: "登录", narrow: true, body: `
<section class="card stack">
  <h1>Evo 后台</h1>
  <p>这是 Evo 的后台：看 Evo 的使用数据，几个人一起管职业选手、直播间和赛程。</p>
  <p class="muted">要进来，请找管理员要一个邀请链接。进来过的人在原来那台设备上直接打开这个网址就行；换了手机或电脑，请管理员再发一个新链接。</p>
</section>` });
}

export function invitePage(invite, action) {
  const who = invite.memberId
    ? `<p>这是给「${escape(invite.name)}」的新登录链接（换了手机或电脑时用），进去以后还是原来的身份：${roleBadge(invite.role)}</p>`
    : `<p>${invite.createdBy === "命令行" ? "你被邀请加入" : `${escape(invite.createdBy)} 邀请你加入`}，身份是 ${roleBadge(invite.role)}，进来以后显示的名字是「${escape(invite.name)}」。</p>
  <p class="muted small">你能做的：${escape(abilities(invite))}。</p>`;
  return layout({ title: "加入", narrow: true, body: `
<section class="card stack">
  <h1>加入 Evo 后台</h1>
  ${who}
  <form method="post" action="${escape(action)}"><button class="btn btn-primary btn-wide" type="submit">进入</button></form>
  <p class="muted small">点了以后这个链接就作废了；这个浏览器会记住你 ${SESSION_DAYS} 天，以后用同一个浏览器打开 team.evotracker.cn 就行。建议用 Chrome 这类常用的浏览器，不要在微信里直接点开。</p>
</section>` });
}

export function messagePage(title, text, me = null) {
  return layout({ title, me, narrow: true, body: `
<section class="card stack">
  <h1>${escape(title)}</h1>
  <p>${escape(text)}</p>
  <p><a class="btn" href="/">回首页</a></p>
</section>` });
}

const SOON = [];

// 改动记录（首页最近几条、改动记录页）
export function logList(items, now) {
  if (!items.length) return `<p class="muted">还没有改动。</p>`;
  return `<ul class="list compact">${items.map((x) => `<li class="row"><div class="row-main"><div>${escape(x.text)}</div>
    <div class="row-meta"><span>${escape(x.by)}</span><span>${escape(ago(x.at, now))}</span><span>${escape(beijing(x.at))}</span></div></div></li>`).join("")}</ul>`;
}

// stats：{players, teams, events, published}，recent：最近几条改动
export function homePage(me, { stats, recent, now, banner = "" }) {
  const tile = (href, name, text, count) => `<a class="tile" href="${href}">
      <div class="tile-head">${escape(name)}<span aria-hidden="true">›</span></div><p>${escape(text)}</p>${count ? `<p class="tile-count">${escape(count)}</p>` : ""}</a>`;
  const last = stats.published?.official;
  const tiles = [
    tile("/stats", "数据", "日活、对局、版本、识别情况、Evo 的占用（只算开了匿名统计的玩家）", stats.dau !== null && stats.dau !== undefined ? `昨天日活 ${stats.dau}` : ""),
    tile("/players", "选手库", "职业选手、战队和直播间", `${stats.players} 位选手、${stats.teams} 支队`),
    tile("/events", "赛程", "联赛、训练赛、杯赛", `${stats.events} 场赛事`),
    tile("/publish", "发布", "生成 Evo 赛事页读的文件", last ? `正式文件：${last.by} ${ago(last.at, now)}发的` : "正式文件还没从这里发过"),
    tile("/pros", "资料", "职业选手和战队：资料、经历、成绩（从 Liquipedia 抓，可以手动修正）", `抓到 ${stats.lpPlayers ?? 0} 位选手`),
    tile("/liquipedia#confirm", "待确认", "选手库的人和 Liquipedia 对上、修正过的资料 Liquipedia 又改了", stats.pending ? `${stats.pending} 条要确认` : "没有要确认的"),
    me.role === "admin" ? tile("/members", "成员", "邀请人、改身份、移出", "") : "",
    ...SOON.map(([name, text]) => `<div class="tile" aria-disabled="true">
      <div class="tile-head">${escape(name)}<span class="badge badge-soon">还没上线</span></div><p>${escape(text)}</p></div>`),
  ].join("");
  return layout({ title: "首页", me, current: "home", banner, body: `
<section class="stack">
  <div class="title-row"><h1>你好，${escape(me.name)}</h1>${roleBadge(me.role)}</div>
  <p class="muted">你能做的：${escape(abilities(me))}。</p>
</section>
<section class="tiles" aria-label="各个部分">${tiles}</section>
<section class="card"><div class="title-row"><h2>最近的改动</h2><span class="grow"></span><a class="small" href="/log">全部</a></div>
  ${logList(recent, now)}</section>` });
}

function roleFields(name, role, publish) {
  const option = (value, title, text) => `<label class="option"><input type="radio" name="${name}" value="${value}"${role === value ? " checked" : ""} required>
      <div><b>${title}</b><span>${text}</span></div></label>`;
  return `<fieldset><legend>身份</legend>
    ${option("editor", "编辑", "能改选手库、赛程、收录哪些比赛")}
    ${option("viewer", "只读", "只能看")}
    ${option("admin", "管理员", "什么都能做，包括邀请人、发布到 Evo")}
  </fieldset>
  <label class="check"><input type="checkbox" name="publish" value="1"${publish ? " checked" : ""}>编辑也能发布到 Evo（发布会改到所有玩家的赛事页）</label>`;
}

function createdCard(created, origin) {
  if (!created) return "";
  const link = `${origin}/join/${created.token}`;
  const title = created.invite.memberId
    ? `把这个新链接发给「${escape(created.invite.name)}」`
    : `把这个链接发给「${escape(created.invite.name)}」`;
  return `<section class="card stack note-info" aria-live="polite">
  <h2>${title}</h2>
  <div class="copy-row"><input class="input mono" id="invite-link" value="${escape(link)}" readonly aria-label="邀请链接">
    <button class="btn btn-primary" type="button" data-copy="invite-link">复制</button></div>
  <ul class="hints">
    <li>用微信发给对方就行。${INVITE_DAYS} 天内有效，打开以后点"进入"就作废，转发给别人也用不了。</li>
    <li>建议对方把链接复制到 Chrome 这类常用的浏览器里打开：在哪个浏览器里点"进入"，以后就用哪个浏览器打开这个网页。</li>
    <li>链接只显示这一次。没发出去就关掉了的话，在下面"还没用的邀请"里作废它，再生成一个。</li>
  </ul>
</section>`;
}

const OK = { role: "身份改好了", removed: "已经移出", cancelled: "邀请已作废" };

export function membersPage(me, data, { origin, now, created = null, error = "", ok = "" }) {
  const notes = (error ? `<p class="note note-error" role="alert">${escape(error)}</p>` : "")
    + (OK[ok] ? `<p class="note note-ok" role="status">${escape(OK[ok])}</p>` : "");
  const members = [...data.members].sort((a, b) => (a.role === b.role ? a.created - b.created : a.role === "admin" ? -1 : b.role === "admin" ? 1 : 0));
  const rows = members.map((m) => {
    const devices = (m.sessions ?? []).filter((s) => s.expires > now);
    const deviceText = devices.length ? `${devices.length} 台设备（${devices.map((s) => escape(s.device)).join("、")}）` : "没有登录的设备";
    return `<li class="row">
    <div class="row-main">
      <div class="row-name">${escape(m.name)}${m.id === me.id ? '<span class="badge">你</span>' : ""}</div>
      <div class="row-meta">${roleBadge(m.role)}${m.role === "editor" && m.publish ? '<span class="badge">能发布</span>' : ""}
        <span>最后来过：${escape(ago(m.lastSeen, now))}</span><span>${deviceText}</span></div>
    </div>
    <div class="row-actions">
      <details class="act"><summary class="btn">改身份</summary>
        <form class="panel" method="post" action="/members/${m.id}/role">${roleFields("role", m.role, m.publish)}
          <button class="btn btn-primary" type="submit">保存</button></form></details>
      <form method="post" action="/members/${m.id}/link"><button class="btn" type="submit">发新链接</button></form>
      <details class="act"><summary class="btn btn-danger">移出</summary>
        <div class="panel"><p class="small">移出以后「${escape(m.name)}」马上就进不来了，还没用的链接也作废。</p>
          <form method="post" action="/members/${m.id}/remove"><button class="btn btn-danger btn-wide" type="submit">确定移出「${escape(m.name)}」</button></form></div></details>
    </div>
  </li>`;
  }).join("");
  const invites = data.invites.filter((i) => i.expires > now);
  const inviteRows = invites.map((i) => `<li class="row">
    <div class="row-main">
      <div class="row-name">${escape(i.name)}${i.memberId ? '<span class="badge">换设备的新链接</span>' : ""}</div>
      <div class="row-meta">${roleBadge(i.role)}<span>${escape(i.createdBy)} 发的</span><span>${escape(inDays(i.expires, now))}</span></div>
    </div>
    <div class="row-actions"><form method="post" action="/invites/${i.id}/cancel"><button class="btn btn-danger" type="submit">作废</button></form></div>
  </li>`).join("");
  return layout({ title: "成员", me, current: "members", body: `
<h1>成员</h1>
${notes}
${createdCard(created, origin)}
<section class="card">
  <h2>邀请新成员</h2>
  <form class="stack" method="post" action="/members/invite">
    <label class="field">名字<span class="help">页面上显示的名字，比如微信昵称，最多 20 个字</span>
      <input class="input" name="name" maxlength="20" required autocomplete="off"></label>
    ${roleFields("role", "editor", false)}
    <div><button class="btn btn-primary" type="submit">生成邀请链接</button></div>
  </form>
</section>
<section class="card">
  <h2>成员（${members.length}）</h2>
  <ul class="list">${rows}</ul>
</section>
${invites.length ? `<section class="card"><h2>还没用的邀请（${invites.length}）</h2><ul class="list">${inviteRows}</ul></section>` : ""}` });
}
