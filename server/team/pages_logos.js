// 一支队的队徽页（2026-11-18）：现在用的、按队名猜的、搜队标库、上传。外框在 pages.js，规矩在 logos.js。
// 只读的人能看；编辑能换、去掉。图都放在深色的小方块里显示（Evo 里也是这样：那一批是给深色背景做的）。

import { can } from "./auth.js";
import { PUBLIC_LOGO_URL } from "./logos.js";
import { ago, escape, layout } from "./pages.js";

export const logoHref = (team, extra = "") => `/logo?team=${encodeURIComponent(team)}${extra}`;

function notes({ error = "", ok = "" }) {
  return (error ? `<div class="note note-error" role="alert">${escape(error)}</div>` : "")
    + (ok ? `<p class="note note-ok" role="status">${escape(ok)}</p>` : "");
}

export function logoBox(src, alt, size = "") {
  return `<span class="logo-box${size ? ` logo-${size}` : ""}"><img src="${escape(src)}" alt="${escape(alt)}" loading="lazy"></span>`;
}

function grid(me, team, items) {
  const editable = can(me, "edit");
  return `<ul class="logo-grid">${items.map((x) => `<li class="logo-item">
    ${logoBox(`/logo-library/${x.file}`, x.name)}
    <span class="small">${escape(x.name)}</span>
    ${editable ? `<form method="post" action="/logo/pick"><input type="hidden" name="team" value="${escape(team)}">
      <input type="hidden" name="file" value="${escape(x.file)}"><button class="btn btn-small" type="submit">用这个</button></form>` : ""}
  </li>`).join("")}</ul>`;
}

export function logoPage(me, { team, current, suggestions, results, q, libraryCount, now, error = "", ok = "" }) {
  const editable = can(me, "edit");
  const now_ = current ? `<div class="title-row">${logoBox(PUBLIC_LOGO_URL + current.logo, team, "big")}
      <div class="stack"><p>${escape(current.source)}</p><p class="small muted">${escape(current.by)} ${escape(ago(current.at, now))}换的</p></div>
      ${editable ? `<span class="grow"></span><details class="act"><summary class="btn btn-danger">去掉队徽</summary><div class="panel">
        <p class="small">去掉以后，Evo 里这支队显示队名的第一个字。</p>
        <form method="post" action="/logo/remove"><input type="hidden" name="team" value="${escape(team)}">
          <button class="btn btn-danger btn-wide" type="submit">确定去掉</button></form></div></details>` : ""}</div>`
    : `<p class="muted">还没有队徽：Evo 里这支队显示队名的第一个字。</p>`;
  return layout({ title: `${team} 的队徽`, me, current: "players", body: `
<p class="small"><a href="/players">‹ 选手库</a></p>
<h1>「${escape(team)}」的队徽</h1>
${notes({ error, ok })}
<section class="card stack"><h2>现在用的</h2>${now_}</section>
${libraryCount ? `<section class="card stack"><h2>按队名猜的</h2>
  ${suggestions.length ? grid(me, team, suggestions) : '<p class="muted">队标库里没猜到，在下面搜一下，或者自己上传。</p>'}</section>
<section class="card stack"><h2>搜队标库</h2>
  <p class="small muted">派圈助手做好的 ${libraryCount} 支队的队标（来自 Liquipedia 和 ALS；队标是各战队的商标）。英文队名搜，比如 Liquid、Wolves。</p>
  <form class="search" method="get" action="/logo" role="search"><input type="hidden" name="team" value="${escape(team)}">
    <span class="search-row"><input class="input" type="search" name="q" value="${escape(q)}" aria-label="搜队标库" autocomplete="off" autocapitalize="off" spellcheck="false">
      <button class="btn btn-primary" type="submit">搜</button></span></form>
  ${q ? (results.length ? grid(me, team, results) : `<p class="muted">没找到「${escape(q)}」。</p>`) : ""}</section>`
    : `<p class="note note-info">队标库还没传上来，现在只能自己上传。</p>`}
${editable ? `<section class="card stack"><h2>自己上传</h2>
  <p class="small muted">用适合深色背景的那一版（Evo 里队徽放在深色的小方块里）。PNG、JPG、WebP 都行，会自动缩到最长边 256 像素、60 KB 以内；不收 SVG。</p>
  <form class="stack" method="post" action="/logo/upload" enctype="multipart/form-data"><input type="hidden" name="team" value="${escape(team)}">
    <input class="input" type="file" name="file" accept="image/png,image/jpeg,image/webp" required data-logo="logo-note">
    <div class="title-row"><span class="logo-box logo-big" id="logo-note-box" hidden><img id="logo-note-img" alt="缩好的队徽"></span>
      <p class="small muted" id="logo-note" aria-live="polite"></p></div>
    <div><button class="btn btn-primary" type="submit">上传</button></div></form></section>` : ""}` });
}
