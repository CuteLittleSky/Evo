// 手动修正 Liquipedia 抓来的选手资料（2026-11-18 第 4 步；用户 11-17 同意的规矩："人改过的优先于所有自动来源，
// 那个来源后来变了就进待确认"）。比如选手改了 ID，Liquipedia 还没更新：在选手页上把 ID 改掉，写上原因。
//
// 存在 manual/overrides.json：{"items": {Liquipedia 页面名: {字段: {"value", "reason", "by", "at",
//   "source": 改的时候 Liquipedia 写的是什么, "conflict"?: {"value": Liquipedia 后来写的, "at"}}}}}
// 定时任务每抓到一批选手资料就对一次账（reconcile）：
//   - Liquipedia 也改成和我们一样了 → 这条修正撤掉（改动记录里写"Liquipedia 已经同步"）
//   - Liquipedia 改成了别的（和改的时候不一样，也和我们改的不一样）→ 标 conflict，进待确认，人选"用 Liquipedia 的"或"留着我们改的"
//   - 没变 → 照旧用我们改的

import { Refuse, readDoc, updateDoc } from "./store.js";
import { squash, length } from "./esports.js";
import { writeLog } from "./data.js";

export const OVERRIDES = "manual/overrides.json";
// 能改的几项（页面上的叫法）
export const FIELDS = { id: "ID", name: "真名", team: "现在的队", nats: "国籍", birth: "生日", input: "输入设备", status: "状态" };
const MAX_VALUE = 80;
const MAX_REASON = 100;

const emptyOverrides = () => ({ items: {} });
const SYSTEM = { name: "Liquipedia 同步" };

export async function overrides(bucket) {
  return (await readDoc(bucket, OVERRIDES, emptyOverrides)).data.items;
}

// Liquipedia 那边这一项是什么（写成一段字，好比较）
export function lpValue(p, field) {
  if (!p || p.missing) return "";
  if (field === "nats") return (p.nats ?? []).join("、");
  return squash(p[field] ?? "");
}

function natsList(text) {
  return String(text ?? "").split(/[、,，/]/).map(squash).filter(Boolean);
}

function cleanValue(field, value) {
  if (!Object.hasOwn(FIELDS, field)) throw new Refuse("这一项不能改");
  let text = squash(value);
  if (field === "nats") text = natsList(text).join("、");
  if (field === "birth" && text && !/^\d{4}-\d{2}-\d{2}$/.test(text)) throw new Refuse("生日写成 1999-05-20 这样");
  if (!text && field !== "team") throw new Refuse(`「${FIELDS[field]}」不能空着（现在没有队的话，"现在的队"可以空着）`);
  if (length(text) > MAX_VALUE) throw new Refuse(`最多 ${MAX_VALUE} 个字`);
  return text;
}

// 页面上显示的选手资料：有修正的项换成修正的值；fixed 里是每一项的修正（带 Liquipedia 原来写的）
export function merged(p, ov) {
  if (!p) return null;
  const out = { ...p, fixed: {} };
  for (const [field, o] of Object.entries(ov ?? {})) {
    out.fixed[field] = { ...o, original: lpValue(p, field) };
    out[field] = field === "nats" ? natsList(o.value) : o.value;
  }
  return out;
}

export async function setOverride(bucket, by, page, field, value, reason, idx, now) {
  const p = idx.players[page];
  if (!p || p.missing) throw new Refuse("没有这位选手的资料（可能还没抓到）");
  const text = cleanValue(field, value);
  const why = squash(reason);
  if (length(why) > MAX_REASON) throw new Refuse(`原因最多 ${MAX_REASON} 个字`);
  const source = lpValue(p, field);
  if (text === source) throw new Refuse("和 Liquipedia 写的一样，不用改");
  await updateDoc(bucket, OVERRIDES, emptyOverrides, (data) => {
    data.items[page] = { ...data.items[page], [field]: { value: text, reason: why, by: by.name, at: now, source } };
  });
  await writeLog(bucket, by, now, `手动修正 ${p.id} 的${FIELDS[field]}：${source || "（空）"} → ${text || "（空）"}${why ? `（${why}）` : ""}`);
}

export async function removeOverride(bucket, by, page, field, now) {
  let gone = null;
  await updateDoc(bucket, OVERRIDES, emptyOverrides, (data) => {
    gone = data.items[page]?.[field] ?? null;
    if (!gone) throw new Refuse("这一项没有修正（可能刚被别人撤掉了）");
    delete data.items[page][field];
    if (!Object.keys(data.items[page]).length) delete data.items[page];
  });
  await writeLog(bucket, by, now, `撤掉对 ${page} 的${FIELDS[field]}的修正（改回用 Liquipedia 的）`);
}

// 待确认里点"用 Liquipedia 的"（撤掉修正）或"留着我们改的"（以后 Liquipedia 再变才再问）
export async function resolveConflict(bucket, by, page, field, keepOurs, now) {
  let text = "";
  await updateDoc(bucket, OVERRIDES, emptyOverrides, (data) => {
    const o = data.items[page]?.[field];
    if (!o?.conflict) throw new Refuse("这一条已经处理过了");
    if (keepOurs) {
      text = `留着对 ${page} 的${FIELDS[field]}的修正「${o.value}」（Liquipedia 写的是「${o.conflict.value}」）`;
      o.source = o.conflict.value;
      delete o.conflict;
    } else {
      text = `${page} 的${FIELDS[field]}改用 Liquipedia 的「${o.conflict.value}」（撤掉修正「${o.value}」）`;
      delete data.items[page][field];
      if (!Object.keys(data.items[page]).length) delete data.items[page];
    }
  });
  await writeLog(bucket, by, now, text);
}

// 定时任务抓到一批选手资料以后对账（见文件开头）
export async function reconcile(bucket, idx, pages, now) {
  const logs = [];
  const current = await overrides(bucket);
  if (!pages.some((page) => current[page])) return;
  await updateDoc(bucket, OVERRIDES, emptyOverrides, (data) => {
    for (const page of pages) {
      const fields = data.items[page];
      const p = idx.players[page];
      if (!fields || !p || p.missing) continue;
      for (const [field, o] of Object.entries(fields)) {
        const now_ = lpValue(p, field);
        if (now_ === o.value) {
          logs.push(`Liquipedia 已经把 ${page} 的${FIELDS[field]}改成「${o.value}」了，手动修正撤掉`);
          delete fields[field];
        } else if (now_ !== o.source && o.conflict?.value !== now_) {
          o.conflict = { value: now_, at: now };
        }
      }
      if (!Object.keys(fields).length) delete data.items[page];
    }
  });
  if (logs.length) await writeLog(bucket, SYSTEM, now, logs);
}

// 待确认用：Liquipedia 后来改了、和我们改的不一样的
export function conflicts(items) {
  const out = [];
  for (const [page, fields] of Object.entries(items)) {
    for (const [field, o] of Object.entries(fields)) if (o.conflict) out.push({ page, field, ...o });
  }
  return out;
}
