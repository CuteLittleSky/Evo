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

// ---------- 赛程：复制这一场 ----------
r = await viewer.get("/events");
check("只读的没有复制", !r.text.includes("?copy="));
r = await editor.get("/events");
check("每一场都有复制", (r.text.match(/href="\/events\?copy=[0-9a-f]{8}#add"/g) ?? []).length === eventList().length);
r = await editor.get(`/events?copy=${groupA().id}`);
check("点复制 → 添加表单打开、照这一场填好（名称、阶段、时间、队伍、主舞台）", r.text.includes('id="add" open')
  && r.text.includes("照「LCQ胜者组第1轮 · A组」填好了") && r.text.includes('value="2026-11-20T17:00"')
  && r.text.split('action="/events/add"')[1].split("</form>")[0].includes('value="11235066"')
  && /action="\/events\/add"[\s\S]*?value="星火" checked[\s\S]*?<\/form>/.test(r.text));
const count = eventList().length;
r = await editor.post("/events/add", eventForm("LCQ胜者组第1轮", "B组", "2026-11-20T17:00", "2026-11-20T20:00", ["星火"],
  [["douyin", "11235066", "解说 贝克"], ["bilibili", "768756", "快乐滋崩在这里"]]));
check("改了阶段再添加 → 多一场", r.location === "/events?ok=added" && eventList().length === count + 1);
r = await editor.get("/events?copy=ffffffff");
check("要复制的那场没了 → 提示", r.text.includes("要复制的那一场找不到了") && !r.text.includes('id="add" open'));

// ---------- 选手换队、两支队并成一支 ----------
const P = () => env.DATA.json(PLAYERS).items;
await add(editor, "Tastingblood", "HAB", "none", "");
await add(editor, "Ahao", "HAB", "none", "");
r = await editor.get("/players");
check("选手每一行有换队、队名旁边有并到另一支队（用页面上那一份队名下拉）", r.text.includes('action="/players/') && r.text.includes("/move")
  && r.text.includes('action="/teams/merge"') && r.text.includes('name="to" list="team-names"'));
r = await viewer.get("/players");
check("只读的没有换队、并队", !r.text.includes("/move") && !r.text.includes("/teams/merge"));
let tb = P().find((p) => p.name === "Tastingblood");
r = await viewer.post(`/players/${tb.id}/move`, { team: "HABNW", rev: String(tb.rev) });
check("只读的不能换队", r.status === 403);
r = await editor.post(`/players/${tb.id}/move`, { team: " HABNW ", rev: String(tb.rev) });
tb = P().find((p) => p.name === "Tastingblood");
check("换队 → 只改他的队，编号不变、记下来", r.location === "/players?ok=moved" && tb.team === "HABNW" && tb.updatedBy === "毛毛"
  && logTexts()[0] === "选手 Tastingblood 换队：HAB → HABNW");
r = await editor.post(`/players/${tb.id}/move`, { team: "HABNW", rev: String(tb.rev) });
check("换到他已经在的队 → 提示", r.status === 400 && r.text.includes("已经在「HABNW」了"));
r = await editor.post(`/players/${tb.id}/move`, { team: "新队", rev: "1" });
check("别人刚改过（rev 对不上）→ 不换", r.status === 400 && r.text.includes("刚改过") && P().find((p) => p.name === "Tastingblood").team === "HABNW");
const ah = P().find((p) => p.name === "Ahao");
await editor.post("/events/add", eventForm("周赛", "", "2026-12-01T19:00", "2026-12-01T22:00", ["HAB", "HABNW", "星火"]));
r = await editor.post(`/players/${ah.id}/move`, { team: "全新队", rev: String(ah.rev) });
check("换到新队名 → 新建一支队；原来的队没人了 → 提醒去赛程里换掉", r.location === "/players?ok=moved_emptied"
  && P().find((p) => p.name === "Ahao").team === "全新队" && logTexts()[0] === "选手 Ahao 换队：HAB → 全新队（「HAB」没人了）");
r = await editor.get(r.location);
check("页面上写着原来那支队没人了", r.text.includes("原来那支队没人了"));
// 并队：全新队（Ahao）并进 HABNW；赛程里两支都有的只留一个；队徽
await env.DATA.put("manual/teams.json", JSON.stringify({ items: { 全新队: { logo: "aaaaaaaaaaaaaaaa.png", source: "上传的", by: "毛毛", at: now } } }));
await editor.post("/events/add", eventForm("周赛二", "", "2026-12-02T19:00", "2026-12-02T22:00", ["全新队", "HABNW", "星火"]));
r = await viewer.post("/teams/merge", { from: "全新队", to: "HABNW" });
check("只读的不能并队", r.status === 403);
r = await editor.post("/teams/merge", { from: "全新队", to: "不存在的队" });
check("并进一支没有的队 → 提示用改队名", r.status === 400 && r.text.includes("没有叫「不存在的队」的队") && r.text.includes("改队名"));
r = await editor.post("/teams/merge", { from: "全新队", to: "全新队" });
check("并进自己 → 提示", r.status === 400 && r.text.includes("选另一支队"));
r = await editor.post("/teams/merge", { from: "全新队", to: "HABNW" });
const week2 = eventList().find((e) => e.name === "周赛二");
check("并队：选手挪过去、赛程里换掉且不重复、队徽跟过去、记下来", r.location === "/players?ok=merged"
  && P().find((p) => p.name === "Ahao").team === "HABNW" && JSON.stringify(week2.teams) === JSON.stringify(["HABNW", "星火"])
  && env.DATA.json("manual/teams.json").items.HABNW?.logo === "aaaaaaaaaaaaaaaa.png" && !env.DATA.json("manual/teams.json").items.全新队
  && logTexts()[0] === "战队「全新队」并进「HABNW」（1 位选手，赛程里 1 场跟着改了，队徽用「全新队」的）");
r = await editor.post("/teams/rename", { from: "星火", to: "HABNW" });
check("改队名改成已有的队 → 提示可以并队", r.status === 400 && r.text.includes("并到另一支队"));
