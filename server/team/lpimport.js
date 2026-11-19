// 从 Liquipedia 一键加进选手库（2026-11-18 用户要的：抓完以后不用一个一个手动加）。
// 战队页"把现役选手加进选手库"、收录的比赛"把这场的参赛队加进选手库"都走这里：
// - 选手名用 Liquipedia 上的 ID（手动修正过的用修正的），战队名用 Liquipedia 上的队名（想用中文名，在选手库"改队名"），
//   Liquipedia 页面名填好（资料页和选手库就连上了）
// - 直播间：Liquipedia 资料 links 里有斗鱼、虎牙、B站直播间、抖音直播间地址的，自动填上（别人已经在用的房间不填）；
//   没有的平台空着，标"直播间待填"——不挡发布，发布时按"不直播"写进去、提醒一句（esports.js 的 build）
// - 选手库里已经有的（同一个 Liquipedia 页面名，或者同一个名字）跳过，不加重复

import { Refuse, randomHex, readDoc, updateDoc } from "./store.js";
import { PLAYERS, writeLog } from "./data.js";
import { length, normalRoom, roomOk, squash } from "./esports.js";
import { eventKey, index } from "./lpsync.js";
import { merged, overrides } from "./overrides.js";

const emptyList = () => ({ version: 1, items: [] });

// 收录的一场比赛：名单里每支队的每个人 → [{page, name, team}]
export async function eventEntries(bucket, page) {
  const event = (await readDoc(bucket, eventKey(page), () => ({}))).data;
  if (!event.teams?.length) throw new Refuse("这场比赛的名单还没抓到（等抓取进度走到这一场）");
  return event.teams.flatMap((t) => t.players.map((p) => ({ page: p.page, name: p.id, team: t.team })));
}

// 一支战队的现役选手（我们抓到资料的人里，现在在这支队的）→ [{page, name, team}]
export function teamEntries(idx, ov, page) {
  const team = idx.teams[page];
  if (!team || team.missing) throw new Refuse("没有这支战队的资料");
  return Object.values(idx.players).filter((p) => !p.missing).map((p) => merged(p, ov[p.page]))
    .filter((p) => (p.teamPage === page && !p.fixed?.team) || (p.fixed?.team && p.team === team.name))
    .map((p) => ({ page: p.page, name: p.id, team: team.name }));
}

export async function addFromLiquipedia(bucket, by, entries, source, now) {
  if (!entries.length) throw new Refuse("没有可以加的人");
  const [idx, ov] = await Promise.all([index(bucket), overrides(bucket)]);
  const result = await updateDoc(bucket, PLAYERS, emptyList, (list) => {
    const out = { added: 0, pending: 0, skipped: 0 };
    const taken = new Set(list.items.filter((p) => p.platform && p.platform !== "none").map((p) => `${p.platform}-${p.room}`));
    for (const e of entries) {
      const raw = idx.players[e.page];
      const p = raw && !raw.missing ? merged(raw, ov[e.page]) : null;
      const name = squash(p?.id ?? e.name);
      const team = squash(e.team);
      if (!name || !team || length(name) > 40 || length(team) > 40
          || list.items.some((x) => x.liquipedia === e.page || x.name === name)) {
        out.skipped++;
        continue;
      }
      const stream = (raw?.streams ?? []).find((s) => roomOk(s.platform, s.room) && !taken.has(`${s.platform}-${normalRoom(s.platform, s.room)}`));
      const record = { id: randomHex(4), name, team, platform: stream?.platform ?? "", room: stream ? normalRoom(stream.platform, stream.room) : "",
                       liquipedia: e.page, created: now, createdBy: by.name, updated: now, updatedBy: by.name, rev: 1 };
      if (stream) taken.add(`${record.platform}-${record.room}`);
      else out.pending++;
      list.items.push(record);
      out.added++;
    }
    if (list.items.length > 1500) throw new Refuse("选手最多 1500 个");
    return out;
  });
  await writeLog(bucket, by, now, `从 Liquipedia 加进选手库（${source}）：新加 ${result.added} 位（${result.pending} 位直播间待填），`
    + `跳过 ${result.skipped} 位（选手库里已经有了）`);
  return result;
}
