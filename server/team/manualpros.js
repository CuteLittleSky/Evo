// 手动建的选手资料（2026-11-19 用户定的："liquipedia 没有页面也允许后台管理人员创建选手"；管理员和编辑都能建、能改）。
// 起因：LCQ 名单里 37 位选手在 Liquipedia 上没有页面，待确认里一直挂着"Liquipedia 上没有这个页面"，谁也修不了。
//
// 存在 manual/pros.json：{"items": {页面名: {"id", "name", "team", "nats": [...], "birth", "input", "status",
//   "created", "createdBy", "updated", "updatedBy", "rev"}}}
// - 页面名：从待确认里建的，用选手库里填的那个 Liquipedia 页面名（选手库那一行就连上了，待确认里那一条也就没了）；
//   在资料页上新建的，用 ID 把空格换成下划线（和 Liquipedia 的页面名一个写法）
// - 资料页、搜索里和 Liquipedia 抓来的放在一起，标"手动建的"。地址同样是 /pros/<页面名>
// - 以后 Liquipedia 有了这个页面（抓到了、不是 missing）：用 Liquipedia 的，手动建的这份留着不显示（宁可旧、不可编：
//   不悄悄合并），选手页上提一句

import { Refuse, readDoc, updateDoc } from "./store.js";
import { length, squash } from "./esports.js";
import { PLAYERS, writeLog } from "./data.js";

export const PROS = "manual/pros.json";
export const STATUSES = { active: "现役", inactive: "不活跃", retired: "已退役" };
const MAX_TEXT = 80;
const emptyPros = () => ({ items: {} });

export async function manualPros(bucket) {
  return (await readDoc(bucket, PROS, emptyPros)).data.items;
}

// Liquipedia 上有这个页面（抓到了）：用 Liquipedia 的
export const onLiquipedia = (idx, page) => Boolean(idx.players[page] && !idx.players[page].missing);

// 资料页上用的样子（和 Liquipedia 抓来的、合并了修正的选手一个形状）
export function asPro(page, x) {
  return { page, id: x.id, name: x.name, team: x.team, nats: x.nats ?? [], birth: x.birth, input: x.input, status: x.status, fixed: {},
           manual: { by: x.createdBy, at: x.created, updatedBy: x.updatedBy, updated: x.updated } };
}

// 手动建的、Liquipedia 上又没有的（显示用）
export function visible(items, idx) {
  return Object.entries(items).filter(([page]) => !onLiquipedia(idx, page)).map(([page, x]) => asPro(page, x));
}

export function pageFromId(id) {
  return squash(id).replace(/ /g, "_");
}

function text(value, label, required = false) {
  const t = squash(value);
  if (required && !t) throw new Refuse(`「${label}」不能空着`);
  if (length(t) > MAX_TEXT) throw new Refuse(`「${label}」最多 ${MAX_TEXT} 个字`);
  return t;
}

export function cleanFields(form) {
  const id = text(form.id, "ID", true);
  if (length(id) > 40) throw new Refuse("「ID」最多 40 个字");
  const birth = squash(form.birth);
  if (birth && !/^\d{4}-\d{2}-\d{2}$/.test(birth)) throw new Refuse("生日写成 1999-05-20 这样（不知道就空着）");
  const status = squash(form.status);
  if (status && !Object.hasOwn(STATUSES, status)) throw new Refuse("状态只能是 现役 / 不活跃 / 已退役");
  const nats = String(form.nats ?? "").split(/[、,，/]/).map(squash).filter(Boolean);
  if (length(nats.join("、")) > MAX_TEXT) throw new Refuse(`「国籍」最多 ${MAX_TEXT} 个字`);
  return { id, name: text(form.name, "真名"), team: text(form.team, "现在的队"), nats, birth, input: text(form.input, "输入设备"), status: status || "active" };
}

// 新建：page 是待确认里那个页面名（可以没有：用 ID 起一个）；link 是选手库里要连上的人（名字，可以空着）
export async function createPro(bucket, by, form, idx, now) {
  const fields = cleanFields(form);
  const page = squash(form.page) || pageFromId(fields.id);
  if (!page || length(page) > 100 || /[#<>[\]|{}]/.test(page)) throw new Refuse(`页面名「${page}」不能用：ID 里别带 # < > [ ] | { } 这些符号`);
  if (onLiquipedia(idx, page)) throw new Refuse(`Liquipedia 上已经有 ${page} 的资料了，不用手动建（在资料页搜一下）`);
  await updateDoc(bucket, PROS, emptyPros, (data) => {
    if (data.items[page]) throw new Refuse(`已经手动建过 ${page} 的资料了（可能别人刚建的）`);
    data.items[page] = { ...fields, created: now, createdBy: by.name, updated: now, updatedBy: by.name, rev: 1 };
  });
  // 连上选手库里的人：填上他的 Liquipedia 页面名（已经填了别的就不动）
  const linkName = squash(form.link);
  let linked = "";
  if (linkName) {
    await updateDoc(bucket, PLAYERS, () => ({ version: 1, items: [] }), (list) => {
      const p = list.items.find((x) => x.name === linkName);
      if (!p || (p.liquipedia && p.liquipedia !== page)) return;
      if (p.liquipedia !== page) {
        p.liquipedia = page;
        p.updated = now;
        p.updatedBy = by.name;
        p.rev = (p.rev ?? 0) + 1;
      }
      linked = p.name;
    });
  }
  await writeLog(bucket, by, now, `手动建了选手资料 ${fields.id}（Liquipedia 上没有页面）${linked ? `，连上选手库的「${linked}」` : ""}`);
  return page;
}

const LABELS = { id: "ID", name: "真名", team: "现在的队", nats: "国籍", birth: "生日", input: "输入设备", status: "状态" };

export async function savePro(bucket, by, page, form, now) {
  const fields = cleanFields(form);
  const lines = await updateDoc(bucket, PROS, emptyPros, (data) => {
    const item = data.items[page];
    if (!item) throw new Refuse("没有这份手动建的资料（可能刚被删了）");
    if (form.rev !== undefined && form.rev !== "" && Number(form.rev) !== item.rev) {
      throw new Refuse(`${item.updatedBy} 刚改过这份资料（你打开以后），没有保存：请看一眼现在的样子再改`);
    }
    const show = (field, value) => (field === "nats" ? value.join("、") : field === "status" ? STATUSES[value] ?? value : value) || "（空）";
    const out = Object.keys(LABELS).filter((f) => JSON.stringify(item[f] ?? "") !== JSON.stringify(fields[f] ?? ""))
      .map((f) => `${LABELS[f]} ${show(f, item[f] ?? (f === "nats" ? [] : ""))} → ${show(f, fields[f])}`);
    if (out.length) Object.assign(item, fields, { updated: now, updatedBy: by.name, rev: item.rev + 1 });
    return out;
  });
  if (lines.length) await writeLog(bucket, by, now, `修改手动建的选手资料 ${fields.id}：${lines.join("，")}`);
}

export async function deletePro(bucket, by, page, now) {
  const gone = await updateDoc(bucket, PROS, emptyPros, (data) => {
    const item = data.items[page];
    if (!item) throw new Refuse("没有这份手动建的资料（可能刚被删了）");
    delete data.items[page];
    return item;
  });
  await writeLog(bucket, by, now, `删除手动建的选手资料 ${gone.id}（选手库里填的页面名没动）`);
}
