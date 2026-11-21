// 战队页（2026-11-21 赛管毛毛要的"分赛区的页面，方便查找"、"老选手老队伍可以批量删掉了"）：每支队标赛区、看最近一场赛事、勾选整队删掉；
// 还有删之前的确认页（整队删、批量修改页勾选的几位选手）。选手库、批量修改页上的赛区标签也用这里的 regionTabs。外框、颜色在 pages.js。

import { can } from "./auth.js";
import { beijingText, parseIso } from "./esports.js";
import { escape, layout } from "./pages.js";
import { NONE, REGIONS } from "./teams.js";

// 页面上一支队的编号（地址后面的 #t-…）：队名什么字都有，用它的哈希（FNV-1a）
export function teamAnchor(team) {
  let h = 0x811c9dc5;
  for (const ch of String(team)) {
    h ^= ch.codePointAt(0);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return `t-${h.toString(16).padStart(8, "0")}`;
}

export const regionName = (key) => REGIONS[key] ?? "没标赛区";

// 赛区的几个标签：全部、四个大区、没标赛区（没有的不列）。counts：{赛区: 几支队}；href(key) 是这个标签的地址
export function regionTabs(current, counts, href, total, { label = "支队", extra = [] } = {}) {
  const keys = [...Object.keys(REGIONS), NONE].filter((k) => counts[k]);
  const tab = (key, name, n) => `<a href="${href(key)}"${current === key ? ' aria-current="page"' : ""}>${escape(name)}（${n}）</a>`;
  return `<nav class="tabs" aria-label="按赛区看">${extra.join("")}${tab("all", "全部", total)}${keys.map((k) => tab(k, regionName(k), counts[k])).join("")}</nav>
    <span class="sr-only">数字是${label}的个数</span>`;
}

function notes({ error = "", ok = "" }) {
  return (error ? `<div class="note note-error" role="alert">${escape(error).replace(/\n/g, "<br>")}</div>` : "")
    + (ok ? `<p class="note note-ok" role="status">${escape(ok)}</p>` : "");
}

// rows：teams.js 的 teamOverview；region：现在看哪个赛区（all / 大区 / none）
export function teamsPage(me, rows, { now, region = "all", error = "", ok = "", banner = "" }) {
  const editable = can(me, "edit");
  const counts = {};
  for (const r of rows) counts[r.region] = (counts[r.region] ?? 0) + 1;
  const shown = rows.filter((r) => region === "all" || r.region === region)
    .sort((a, b) => (parseIso(b.last?.end ?? "") ?? 0) - (parseIso(a.last?.end ?? "") ?? 0) || (a.team < b.team ? -1 : 1));
  const tabs = regionTabs(region, counts, (k) => (k === "all" ? "/teams" : `/teams?region=${k}`), rows.length);
  const option = (key, name, current) => `<option value="${key}"${key === current ? " selected" : ""}>${escape(name)}</option>`;
  const row = (r, i) => {
    const value = r.guessed ? "" : r.region === NONE ? "" : r.region;  // 打开时选的：手动标过的那一个，否则"没标"
    const guessText = r.guessed ? `按 Liquipedia 猜：${regionName(r.region)}` : "没标（按 Liquipedia 猜）";
    const last = r.last ? `最近：${escape(r.last.label)}，${escape(beijingText(parseIso(r.last.end)).slice(0, 10))} 结束` : "没参加过赛程里的赛事";
    return `<li class="row team-row" id="${escape(`tr-${i}`)}">
      ${editable ? `<label class="del-pick" title="勾上，点下面的&quot;删掉勾选的队&quot;"><input type="checkbox" name="del" value="${escape(r.team)}" form="del-form"${r.busy ? " disabled" : ""}
        aria-label="勾选 ${escape(r.team)}（删掉用）"><span class="sr-only">删</span></label>` : "<span></span>"}
      <div class="row-main">
        <div class="row-name"><a href="/players${r.region !== NONE ? `?region=${r.region}` : `?region=${NONE}`}#${teamAnchor(r.team)}">${escape(r.team)}</a></div>
        <div class="row-meta"><span>${r.count} 人</span><span>${last}</span>${r.busy ? '<span class="badge">有进行中、即将开始的赛事，不能删</span>' : ""}</div>
      </div>
      <div class="team-region">
        <input type="hidden" name="tn_${i}" value="${escape(r.team)}" form="region-form"><input type="hidden" name="tw_${i}" value="${value}" form="region-form">
        <select class="input" name="tr_${i}" form="region-form" aria-label="${escape(r.team)} 的赛区"${editable ? "" : " disabled"}>
          ${option("", r.guessed ? guessText : "没标赛区", value)}${Object.entries(REGIONS).map(([k, name]) => option(k, name, value)).join("")}</select>
      </div></li>`;
  };
  return layout({ title: "战队", me, current: "players", banner, body: `
<p class="small"><a href="/players">‹ 选手库</a></p>
<div class="stack"><h1>战队和赛区</h1>
  <p class="muted">赛区照 ALGS 的四个大区，国内队算亚太南。没标的队按 Liquipedia 猜（队里选手现在的战队，没有战队的看国籍），下拉里写着"按 Liquipedia 猜：…"；
    猜得不对就选一个，点"保存赛区"。最近有比赛的队排在前面，老队伍在后面：勾上点"删掉勾选的队"，连同队里的选手一起删（删之前会再问一次）。</p></div>
${notes({ error, ok })}
${tabs}
${shown.length ? `<form id="region-form" method="post" action="/teams/regions"><input type="hidden" name="region" value="${escape(region)}"></form>
<form id="del-form" method="post" action="/teams/delete"><input type="hidden" name="region" value="${escape(region)}"></form>
<section class="card"><ul class="list">${shown.map(row).join("")}</ul></section>
${editable ? `<div class="bulk-bar"><button class="btn btn-primary" type="submit" form="region-form">保存赛区</button>
  <button class="btn btn-danger" type="submit" form="del-form">删掉勾选的队…</button>
  <span class="small muted">保存赛区只存改过的</span></div>` : ""}` : '<p class="muted">这个赛区还没有队。</p>'}` });
}

// 删之前再问一次：items 是 [{text, meta}]，hidden 是要带着的 [[名字, 值]]
export function confirmPage(me, { title, lead, items, action, hidden, back, button }) {
  return layout({ title, me, current: "players", body: `
<p class="small"><a href="${escape(back)}">‹ 回去</a></p>
<div class="stack"><h1>${escape(title)}</h1><p>${escape(lead)}</p></div>
<section class="card"><ul class="list">${items.map((x) => `<li class="row"><div class="row-main"><div class="row-name">${escape(x.text)}</div>
  ${x.meta ? `<div class="row-meta"><span>${escape(x.meta)}</span></div>` : ""}</div></li>`).join("")}</ul></section>
<form class="stack" method="post" action="${escape(action)}">
  ${hidden.map(([k, v]) => `<input type="hidden" name="${escape(k)}" value="${escape(v)}">`).join("")}<input type="hidden" name="confirm" value="1">
  <p class="small muted">删了找不回来，要的话得重新添加。</p>
  <div class="links"><button class="btn btn-danger" type="submit">${escape(button)}</button><a class="btn" href="${escape(back)}">不删了</a></div>
</form>` });
}
