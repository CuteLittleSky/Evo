// 2026-11-19 那一批优化的测试（发布这一块）。由 test.mjs 运行。
// 走一遍：已经结束的赛事有问题不挡发布（只提醒、不放那支队）、提示太多时收起来、直播间待填按队列出来、
// 页面顶上"有改动还没发布"、改动记录写清楚（A 组 / B 组、换了哪支队、哪一路主舞台）。

import { hash, newToken, update } from "./auth.js";
import { EVENTS, LOG, PLAYERS } from "./data.js";
import { FakeBucket, check, makeBrowser } from "./testkit.mjs";

const HOUR = 3600 * 1000;
let now = Date.UTC(2026, 10, 20, 4, 0);  // 北京时间 11-20 12:00
const env = { DATA: new FakeBucket(), PUBLIC: new FakeBucket() };
const browser = () => makeBrowser(env, () => now);
const logTexts = () => env.DATA.json(LOG, '{"items":[]}').items.map((x) => x.text);
const eventList = () => env.DATA.json(EVENTS, '{"items":[]}').items;

async function join(name, role, publish = false) {
  const token = newToken();
  const h = await hash(token);
  await update(env.DATA, (data) => {
    data.invites.push({ id: Math.random().toString(16).slice(2, 10).padEnd(8, "0"), h, name, role, publish, created: now,
                        createdBy: "命令行", expires: now + 7 * 24 * HOUR });
  });
  const b = browser();
  await b.post(`/join/${token}`);
  return b;
}

const admin = await join("黄指导", "admin");
const editor = await join("毛毛", "editor");
const viewer = await join("看看", "viewer");

const add = (b, name, team, platform, room) => b.post("/players/add", { name, team, platform, room });
await add(editor, "BlackRoad", "HABNW", "douyin", "1228146728");
await add(editor, "Ahao", "HAB", "none", "");
await add(editor, "Kite", "星火", "douyu", "520013");
await add(editor, "Ace", "星火", "bilibili", "100");
const eventForm = (name, stage, start, end, teams, main = []) => [["name", name], ["type", "训练赛"], ["stage", stage], ["start", start], ["end", end],
  ...teams.map((t) => ["teams", t]), ...main.flatMap((m) => [["main_platform", m[0]], ["main_room", m[1]], ["main_name", m[2]]])];
// 昨天的训练赛里有 HAB；今天晚上的还没开始
await editor.post("/events/add", eventForm("LCQ 训练赛", "第一组", "2026-11-19T16:00", "2026-11-19T19:00", ["HAB", "HABNW", "星火"]));
await editor.post("/events/add", eventForm("LCQ胜者组第1轮", "A组", "2026-11-20T17:00", "2026-11-20T20:00", ["HABNW", "星火"]));

// ---------- 已经结束的赛事有问题不挡发布 ----------
// 毛毛把 HAB 的人挪到 HABNW（删了再加），昨天的赛程里还写着 HAB
const ahao = env.DATA.json(PLAYERS).items.find((p) => p.name === "Ahao");
await editor.post(`/players/${ahao.id}/delete`);
let r = await admin.get("/publish");
check("结束了的赛事里有不在选手库的队 → 照样能发布", !r.text.includes("还不能发布") && r.text.includes("检查过了，可以发布"));
check("提醒写着哪一场（带阶段）、哪支队、发布时不放", r.text.includes("「LCQ 训练赛 · 第一组」（已经结束）：队伍「HAB」不在选手库里")
  && r.text.includes("发布时不放这支队"));
check("结束了的赛事不再提醒队数不是 20", !r.text.includes("「LCQ 训练赛 · 第一组」有 3 支队"));
r = await admin.post("/publish/official");
let sent = env.PUBLIC.json("esports.json");
check("发出去：结束了的那场只放选手库里有的队", r.location === "/publish?ok=official"
  && JSON.stringify(sent.events.find((e) => e.stage === "第一组").teams) === JSON.stringify(["HABNW", "星火"]));
// 还没结束的赛事有问题照旧挡着
await env.DATA.put(EVENTS, JSON.stringify({ version: 1, items: eventList().map((e) => (e.stage === "A组" ? { ...e, teams: [...e.teams, "HAB"] } : e)) }));
r = await admin.get("/publish");
check("还没结束的赛事里有不在选手库的队 → 还是不能发布", r.text.includes("还不能发布") && r.text.includes("「LCQ胜者组第1轮 · A组」：队伍「HAB」不在选手库里"));
await env.DATA.put(EVENTS, JSON.stringify({ version: 1, items: eventList().map((e) => (e.stage === "A组" ? { ...e, teams: e.teams.filter((t) => t !== "HAB") } : e)) }));
// 一支队都不剩的已结束赛事：整场不放
await env.DATA.put(EVENTS, JSON.stringify({ version: 1, items: [...eventList(), { id: "deadbeef", name: "老比赛", type: "训练赛", stage: "", start: "2026-11-18T10:00:00+08:00",
  end: "2026-11-18T12:00:00+08:00", teams: ["没了的队"], main: [], rev: 1 }] }));
r = await admin.post("/publish/test");
sent = env.PUBLIC.json("esports_test.json");
check("一支队都不剩的已结束赛事整场不放", r.location === "/publish?ok=test" && !sent.events.some((e) => e.name === "老比赛") && sent.events.length === 2);
await env.DATA.put(EVENTS, JSON.stringify({ version: 1, items: eventList().filter((e) => e.name !== "老比赛") }));

// ---------- 提示太多时收起来 ----------
// 存储桶里直接放 8 个撞房间的人（网页上存不进去，只有老数据会这样）
const crowd = Array.from({ length: 8 }, (_, i) => ({ id: `0000000${i}`, name: `重${i}`, team: "撞车队", platform: "douyu", room: "999", liquipedia: "", rev: 1 }));
const saved = env.DATA.json(PLAYERS);
await env.DATA.put(PLAYERS, JSON.stringify({ ...saved, items: [...saved.items, ...crowd] }));
r = await editor.get("/players");
check("选手库：要改的先列 5 条，多的收起来", r.text.includes("发布前要改好（7 处）") && r.text.includes("还有 2 条"));
r = await admin.get("/publish");
check("发布页：要改的也收起来", r.text.includes("还不能发布，有 7 处要改") && r.text.includes("还有 2 条"));
await env.DATA.put(PLAYERS, JSON.stringify(saved));

// ---------- 直播间待填按队列出来 ----------
await env.DATA.put(PLAYERS, JSON.stringify({ ...saved, items: [...saved.items,
  { id: "a0000001", name: "Chisu", team: "ARAM", platform: "", room: "", liquipedia: "Chisu", rev: 1 },
  { id: "a0000002", name: "SongYou", team: "ARAM", platform: "", room: "", liquipedia: "", rev: 1 },
  { id: "a0000003", name: "Razi", team: "ASC3NT", platform: "", room: "", liquipedia: "", rev: 1 }] }));
r = await editor.get("/players");
check("选手库：待填的人按队列出来、默认收起", r.text.includes("3 位选手直播间待填") && r.text.includes("2 支队")
  && r.text.includes("<b>ARAM</b>：Chisu、SongYou") && /<details class="note note-info"><summary>/.test(r.text));
check("待填的不算发布前要改的", !r.text.includes("发布前要改好"));
await env.DATA.put(PLAYERS, JSON.stringify(saved));

// ---------- 页面顶上"有改动还没发布" ----------
r = await editor.get("/players");
check("改的都发出去了 → 不提醒", !r.text.includes("有改动还没发布"));
const kite = env.DATA.json(PLAYERS).items.find((p) => p.name === "Kite");
now += HOUR;
await editor.post(`/players/${kite.id}/save`, { name: "Kite", team: "星火", platform: "douyu", room: "520014", rev: String(kite.rev) });
r = await editor.get("/players");
check("改了直播间 → 选手库顶上提醒、写着谁改过、上次谁发的", r.text.includes("有改动还没发布到 Evo") && r.text.includes("之后 毛毛 改过")
  && r.text.includes("玩家现在看到的是 黄指导 1 小时前发的"));
check("不能发布的编辑：请管理员发布，没有去发布按钮", r.text.includes("请管理员（或者能发布的人）发布") && !r.text.includes('href="/publish">去发布'));
r = await admin.get("/events");
check("管理员在赛程页也看到，有去发布", r.text.includes("有改动还没发布到 Evo") && r.text.includes('href="/publish">去发布'));
r = await viewer.get("/");
check("只读的在首页也看到", r.text.includes("有改动还没发布到 Evo"));
r = await admin.get("/publish");
check("发布页上也提醒，但不再放去发布", r.text.includes("有改动还没发布到 Evo") && !r.text.includes('href="/publish">去发布'));
await editor.post(`/players/${kite.id}/save`, { name: "Kite", team: "星火", platform: "douyu", room: "520013", rev: String(kite.rev + 1) });
r = await editor.get("/players");
check("改回去了（和线上一样）→ 不提醒", !r.text.includes("有改动还没发布"));
await editor.post(`/players/${kite.id}/save`, { name: "Kite", team: "星火", platform: "douyu", room: "520014", rev: String(kite.rev + 2) });
await env.DATA.put(EVENTS, JSON.stringify({ version: 1, items: eventList().map((e) => (e.stage === "A组" ? { ...e, teams: [...e.teams, "HAB"] } : e)) }));
r = await editor.get("/players");
check("发不了的时候写着还有几处要改", r.text.includes("现在还发不了：有 1 处要改"));
await env.DATA.put(EVENTS, JSON.stringify({ version: 1, items: eventList().map((e) => (e.stage === "A组" ? { ...e, teams: e.teams.filter((t) => t !== "HAB") } : e)) }));
await admin.post("/publish/official");
r = await editor.get("/players");
check("发布以后不再提醒", !r.text.includes("有改动还没发布"));
now += 8 * 24 * HOUR;
r = await editor.get("/players");
check("过了 8 天，赛事都该下架了（只是日子过去了）→ 不算有改动", !r.text.includes("有改动还没发布"));

// ---------- 改动记录写清楚 ----------
now += HOUR;
const groupA = () => eventList().find((e) => e.stage === "A组");
const saveA = (teams, main) => editor.post(`/events/${groupA().id}/save`, [...eventForm("LCQ胜者组第1轮", "A组", "2026-11-20T17:00", "2026-11-20T20:00", teams, main),
  ["rev", String(groupA().rev)]]);
await saveA(["HABNW", "星火"], [["bilibili", "768756", "快乐滋崩在这里"], ["douyin", "11235066", "解说贝克"]]);
check("改动记录：带上阶段，主舞台写加了哪几路", logTexts()[0] === "修改赛事「LCQ胜者组第1轮 · A组」：主舞台：加了 快乐滋崩在这里（B站 768756）、解说贝克（抖音 11235066）");
await saveA(["星火"], [["bilibili", "768756", "快乐滋崩在这里"], ["douyin", "11235066", "解说 贝克"]]);
check("改动记录：队伍写去掉了谁、现在几支；主舞台同一个直播间改了名字", logTexts()[0]
  === "修改赛事「LCQ胜者组第1轮 · A组」：队伍：去掉了 HABNW（现在 1 支）；主舞台：「解说贝克」改名「解说 贝克」");
await saveA(["星火"], [["douyin", "11235066", "解说 贝克"], ["bilibili", "768756", "快乐滋崩在这里"]]);
check("改动记录：只换了顺序", logTexts()[0] === "修改赛事「LCQ胜者组第1轮 · A组」：主舞台：顺序换了");
const before = logTexts().length;
await saveA(["星火"], [["douyin", "11235066", "解说 贝克"], ["bilibili", "768756", "快乐滋崩在这里"]]);
check("什么都没改 → 不记", logTexts().length === before);
