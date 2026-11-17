// Liquipedia 这一页（2026-11-18 第 3 步）：收录的比赛、抓取进度和额度、待确认、抓到了多少。外框在 pages.js。
// 只读的人能看；编辑能加删比赛、点"现在更新"、处理待确认。

import { can } from "./auth.js";
import { beijingText } from "./esports.js";
import { HOURLY_CAP } from "./lpsync.js";
import { SITE } from "./liquipedia.js";
import { ago, escape, layout } from "./pages.js";

function notes({ error = "", ok = "" }) {
  return (error ? `<div class="note note-error" role="alert">${escape(error)}</div>` : "")
    + (ok ? `<p class="note note-ok" role="status">${escape(ok)}</p>` : "");
}

const lpLink = (page, text) => `<a href="${escape(SITE + page.split("/").map(encodeURIComponent).join("/"))}" target="_blank" rel="noopener noreferrer">${escape(text ?? page)}</a>`;

function statusCard(me, p, keySet, now) {
  const editable = can(me, "edit");
  const round = !keySet
    ? `<p class="note note-error">还没配 Liquipedia 的 Key，现在什么都不会抓。管理员在电脑上配一次就行（见 docs/底座.md"协作网页"）。</p>`
    : p.running
      ? `<p>正在抓：第 ${p.done} / ${p.total} 步，大约还要 ${p.left * 3 < 60 ? `${Math.max(p.left * 3, 3)} 分钟` : `${Math.ceil(p.left * 3 / 6) / 10} 小时`}（每 3 分钟抓一步）。</p>`
      : p.lastRoundDone
        ? `<p>上一轮 ${escape(ago(p.lastRoundDone, now))}抓完（${escape(beijingText(p.lastRoundDone))}）。${p.refresh ? "已经排上，几分钟内开始新的一轮。" : `下一轮大约在 ${escape(beijingText(p.next))}。`}</p>`
        : `<p>还没抓过。${p.refresh ? "已经排上，几分钟内开始。" : ""}</p>`;
  const errors = p.errors.length ? `<details class="small"><summary>最近的出错（${p.errors.length}）</summary><ul class="plain">
    ${p.errors.slice(0, 8).map((e) => `<li>${escape(beijingText(e.at))} ${escape(e.text)}</li>`).join("")}</ul></details>` : "";
  const refresh = editable && keySet && !p.running && !p.refresh ? `<form method="post" action="/liquipedia/refresh">
    <button class="btn" type="submit">现在更新</button></form>` : "";
  return `<section class="card stack"><h2>抓取进度</h2>${round}
    <p class="small muted">这一小时用了 ${p.recent} 次请求（这里最多 ${HOURLY_CAP} 次；Liquipedia 给这个 Key 一小时 60 次，派圈那边另外最多用 12 次）。
      一轮抓完歇 24 小时；"现在更新"只是让下一轮马上开始，不会多用额度。</p>
    ${errors}${refresh}</section>`;
}

function trackedCard(me, tracked, idx, now) {
  const editable = can(me, "edit");
  const rows = tracked.map((t) => {
    const info = idx.events[t.page] ?? {};
    const state = info.missing ? '<span class="badge badge-warn">Liquipedia 上没有这个页面</span>'
      : info.fetchedAt ? `<span>${escape(info.start || "")}${info.end && info.end !== info.start ? ` – ${escape(info.end)}` : ""}</span>`
        + (info.rosterAt ? `<span>名单 ${info.rosterTeams} 支队、${info.rosterPlayers} 人</span>` : "") + `<span>${escape(ago(info.rosterAt ?? info.fetchedAt, now))}抓的</span>`
      : '<span class="badge">还没抓</span>';
    return `<li class="row"><div class="row-main">
      <div class="row-name">${info.name ? escape(info.name) : escape(t.page)}</div>
      <div class="row-meta">${lpLink(t.page, t.page)}${state}<span>${escape(t.addedBy)} 加的</span></div></div>
      ${editable ? `<div class="row-actions"><details class="act"><summary class="btn btn-danger">删除</summary><div class="panel">
        <p class="small">不再收录这场比赛？抓过的资料照旧留着。</p>
        <form method="post" action="/liquipedia/untrack"><input type="hidden" name="page" value="${escape(t.page)}">
          <button class="btn btn-danger btn-wide" type="submit">确定不再收录</button></form></div></details></div>` : ""}
    </li>`;
  }).join("");
  const add = editable ? `<form class="stack" method="post" action="/liquipedia/track">
    <label class="field">加一场比赛<span class="help">贴 Liquipedia 比赛页的地址，比如 https://liquipedia.net/apexlegends/Apex_Legends_Global_Series/2026/…</span>
      <input class="input" name="page" required autocomplete="off" inputmode="url"></label>
    <div><button class="btn btn-primary" type="submit">收录</button></div></form>` : "";
  return `<section class="card stack"><h2>收录的比赛（${tracked.length}）</h2>
    <p class="small muted">这些比赛的参赛名单里的人，加上选手库里填了 Liquipedia 页面名的人，都会去抓资料、战队经历和成绩。</p>
    ${tracked.length ? `<ul class="list">${rows}</ul>` : `<p class="muted">还没有。</p>`}${add}</section>`;
}

function pendingCard(me, pend) {
  const editable = can(me, "edit");
  const count = pend.suggestions.length + pend.missing.length;
  const suggestions = pend.suggestions.map(({ player, candidates }) => candidates.map((c) => `<li class="row"><div class="row-main">
      <div class="row-name">选手库的「${escape(player.name)}」（${escape(player.team)}）是不是 Liquipedia 上的 ${escape(c.id)}？</div>
      <div class="row-meta">${lpLink(c.page)}${c.name ? `<span>${escape(c.name)}</span>` : ""}${c.team ? `<span>现在在 ${escape(c.team)}</span>` : ""}
        ${c.nats?.length ? `<span>${escape(c.nats.join("、"))}</span>` : ""}</div></div>
      ${editable ? `<div class="row-actions">
        <form method="post" action="/liquipedia/match"><input type="hidden" name="player" value="${player.id}"><input type="hidden" name="page" value="${escape(c.page)}">
          <input type="hidden" name="answer" value="yes"><button class="btn btn-primary" type="submit">是他</button></form>
        <form method="post" action="/liquipedia/match"><input type="hidden" name="player" value="${player.id}"><input type="hidden" name="page" value="${escape(c.page)}">
          <input type="hidden" name="answer" value="no"><button class="btn" type="submit">不是</button></form></div>` : ""}
    </li>`).join("")).join("");
  const missing = pend.missing.map((m) => `<li class="row"><div class="row-main">
      <div class="row-name">选手库的「${escape(m.name)}」填的 Liquipedia 页面名「${escape(m.liquipedia)}」，Liquipedia 上没有这个页面</div>
      <div class="row-meta"><span>大小写、下划线、括号里的说明（比如 Panic_(American_player)）都要一样</span><a href="/players">去选手库改</a></div></div></li>`).join("");
  return `<section class="card stack" id="confirm"><h2>待确认（${count}）</h2>
    <p class="small muted">选手库里没填 Liquipedia 页面名、名字又和抓到的某位选手一样的，列在这里；点"是他"就把页面名填进选手库。拿不准的点开链接看一眼。</p>
    ${count ? `<ul class="list">${suggestions}${missing}</ul>` : `<p class="muted">没有要确认的。</p>`}</section>`;
}

export function liquipediaPage(me, { tracked, idx, progress, pend, keySet, now, error = "", ok = "" }) {
  const players = Object.values(idx.players).filter((p) => !p.missing).length;
  const teams = Object.values(idx.teams).filter((t) => !t.missing).length;
  return layout({ title: "Liquipedia", me, current: "liquipedia", body: `
<div class="stack"><h1>Liquipedia 资料</h1>
  <p class="muted">定时从 Liquipedia 抓职业选手的资料、战队经历和比赛成绩，和选手库分开存，谁也不改谁的。
    数据来自 <a href="https://liquipedia.net/apexlegends/" target="_blank" rel="noopener noreferrer">Liquipedia</a>（CC BY-SA 3.0）。</p></div>
${notes({ error, ok })}
${pendingCard(me, pend)}
${statusCard(me, progress, keySet, now)}
${trackedCard(me, tracked, idx, now)}
<section class="card stack"><h2>抓到的</h2>
  <p>${players} 位选手、${teams} 支战队、${Object.values(idx.events).filter((e) => !e.missing).length} 场比赛。每个人的资料、经历和成绩下一步做成可以搜、可以点开看的页面。</p></section>` });
}
