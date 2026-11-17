// 资料页、选手页、战队页、手动修正的测试（第 4 步）。由 test.mjs 运行。
// 走一遍：搜索（ID、真名、战队，不管大小写）、选手页（年龄、成绩、经历、链接）、战队页（现役、待过的）、地址写错、
// 权限、修正（存下 Liquipedia 原来写的、页面上换成修正的、各种写错）、撤掉、待确认按修正的 ID 比名字、
// 和 Liquipedia 对账（一样了自动撤掉、改成别的进待确认、两种处理）、定时任务里真的会对账。

import { hash, newToken, update } from "./auth.js";
import { LOG, PLAYERS } from "./data.js";
import { INDEX, SQUADS, STATE, resultsKey, tick } from "./lpsync.js";
import { OVERRIDES, reconcile } from "./overrides.js";
import { FakeBucket, check, makeBrowser } from "./testkit.mjs";

const MINUTE = 60 * 1000;
let now = Date.UTC(2026, 10, 18, 4, 0);  // 北京 11-18 12:00
const env = { DATA: new FakeBucket(), PUBLIC: new FakeBucket() };
const browser = () => makeBrowser(env, () => now);
const doc = (key, fallback = "{}") => env.DATA.json(key, fallback);
const put = (key, value) => env.DATA.put(key, JSON.stringify(value));
const logs = () => doc(LOG, '{"items":[]}').items.map((x) => x.text);

async function join(name, role) {
  const token = newToken();
  const h = await hash(token);
  await update(env.DATA, (data) => {
    data.invites.push({ id: Math.random().toString(16).slice(2, 10).padEnd(8, "0"), h, name, role, publish: false, created: now,
                        createdBy: "命令行", expires: now + 7 * 24 * 60 * MINUTE });
  });
  const b = browser();
  await b.post(`/join/${token}`);
  return b;
}

const PANIC = "Panic_(American_player)";
const idx = {
  events: {},
  players: {
    [PANIC]: { page: PANIC, id: "Panic", name: "Jane Doe", localName: "", nats: ["United States"], birth: "2000-05-20", status: "active",
               teamPage: "Team_Liquid", team: "Team Liquid", earnings: 12345, input: "Mouse & Keyboard", legends: ["Wraith"], fetchedAt: now - 60 * MINUTE, results: 2 },
    "Kite_(player)": { page: "Kite_(player)", id: "Kite", name: "", nats: ["China"], birth: "", status: "active", teamPage: "Wolves_Esports",
                       team: "Wolves Esports", fetchedAt: now - 60 * MINUTE },
    Ghost: { page: "Ghost", missing: true, fetchedAt: now },
  },
  teams: {
    Team_Liquid: { page: "Team_Liquid", name: "Team Liquid", region: "North America", status: "active", created: "2015-01-01", earnings: 99999, fetchedAt: now },
    Wolves_Esports: { page: "Wolves_Esports", name: "Wolves Esports", region: "China", status: "active", fetchedAt: now },
  },
};
await put(INDEX, idx);
await put(SQUADS, { [PANIC]: [
  { teamPage: "Team_Liquid", team: "Team Liquid", join: "2024-01-01", leave: "", status: "active", type: "player", role: "" },
  { teamPage: "Wolves_Esports", team: "Wolves Esports", join: "2022-01-01", leave: "2023-12-01", status: "former", type: "player", role: "" }] });
await put(resultsKey(PANIC), { page: PANIC, rows: [
  { date: "2025-06-01", event: "Small_Cup", tournament: "Small Cup", tier: "3", tierType: "", place: "9-12", prize: 0, team: "Wolves Esports" },
  { date: "2026-11-01", event: "ALGS/2026", tournament: "ALGS Split 2 Playoffs", tier: "1", tierType: "", place: "1", prize: 500000, team: "Team Liquid" }] });

const admin = await join("黄指导", "admin");
const editor = await join("Momona", "editor");
const viewer = await join("看看", "viewer");
await editor.post("/players/add", { name: "Panic", team: "猎鹰", platform: "douyu", room: "1", liquipedia: `https://liquipedia.net/apexlegends/${PANIC}` });
await editor.post("/players/add", { name: "Kitey", team: "狼队", platform: "douyu", room: "2", liquipedia: "" });

// ---------- 资料页、搜索 ----------
let r = await viewer.get("/pros");
check("资料页：列出抓到的选手（没找到的不列）、战队", r.status === 200 && r.text.includes(">Panic</a>") && r.text.includes(">Kite</a>")
  && !r.text.includes("Ghost") && r.text.includes(">Team Liquid</a>"));
check("资料页：标出选手库里连着的人、写明数据来源", r.text.includes("选手库：Panic") && r.text.includes("CC BY-SA 3.0"));
check("导航里是「资料」", r.text.includes('href="/pros" aria-current="page"'));
r = await viewer.get("/pros?q=LIQUID");
check("搜战队名（不管大小写）→ 选手和战队", r.text.includes(">Panic</a>") && !r.text.includes(">Kite</a>") && r.text.includes("战队（1）"));
r = await viewer.get(`/pros?q=${encodeURIComponent("jane doe")}`);
check("搜真名", r.text.includes(">Panic</a>") && r.text.includes("选手（1）"));
r = await viewer.get("/pros?q=nobody");
check("没搜到 → 提示", r.text.includes("没找到「nobody」"));

// ---------- 选手页 ----------
const panicPage = `/pros/${encodeURIComponent(PANIC)}`;
r = await viewer.get(panicPage);
check("选手页：ID、真名、现在的队（能点）、国籍、年龄", r.status === 200 && r.text.includes("<h1>Panic</h1>") && r.text.includes("Jane Doe")
  && r.text.includes('href="/teams/Team_Liquid"') && r.text.includes("United States") && r.text.includes("26 岁"));
check("选手页：成绩新的在前、名次、等级、全队奖金", r.text.indexOf("ALGS Split 2 Playoffs") < r.text.indexOf("Small Cup")
  && r.text.includes("第 1 名") && r.text.includes("第 9–12 名") && r.text.includes("S 级") && r.text.includes("全队奖金 $500,000"));
check("选手页：战队经历、Liquipedia 链接、选手库里是谁", r.text.includes("2024-01-01 – 至今") && r.text.includes("2022-01-01 – 2023-12-01")
  && r.text.includes(`https://liquipedia.net/apexlegends/${encodeURIComponent(PANIC)}`) && r.text.includes("选手库里是 「Panic」（猎鹰）"));
check("只读的看不到改", !r.text.includes("/fix"));
r = await viewer.get("/pros/Ghost");
check("Liquipedia 上没有的 → 404", r.status === 404);
r = await viewer.get("/pros/%E0%A4");
check("地址写坏了 → 没有这个页面", r.status === 404);

// ---------- 战队页 ----------
r = await viewer.get("/teams/Team_Liquid");
check("战队页：资料、现役选手", r.status === 200 && r.text.includes("North America") && r.text.includes("现役选手（1）") && r.text.includes(">Panic</a>"));
r = await viewer.get("/teams/Wolves_Esports");
check("战队页：待过的选手（从经历里来）", r.text.includes("现役选手（1）") && r.text.includes("待过的选手（1）") && r.text.includes("2022-01-01 – 2023-12-01"));
r = await viewer.get("/teams/Nope");
check("没有的战队 → 404", r.status === 404);

// ---------- 修正 ----------
r = await viewer.post(`${panicPage}/fix`, { field: "id", value: "PanicNew" });
check("只读的不能修正", r.status === 403);
r = await editor.post(`${panicPage}/fix`, { field: "id", value: " PanicNew ", reason: "11-18 改 ID 了，Liquipedia 还没更新" });
check("修正 ID → 回选手页", r.location === `${panicPage}?ok=fixed`);
let ov = doc(OVERRIDES).items[PANIC].id;
check("存下修正的值、原因、谁改的、Liquipedia 原来写的", ov.value === "PanicNew" && ov.source === "Panic" && ov.by === "Momona" && ov.reason.startsWith("11-18"));
r = await viewer.get(panicPage);
check("页面上用修正的、标出来、写着 Liquipedia 原来写的", r.text.includes("<h1>PanicNew</h1>") && r.text.includes("手动修正")
  && r.text.includes("Liquipedia 写的是「Panic」"));
r = await viewer.get("/pros?q=panicnew");
check("搜修正以后的 ID 也搜得到", r.text.includes(">PanicNew</a>") && r.text.includes("有修正"));
check("改动记录写明", logs()[0] === "手动修正 Panic 的ID：Panic → PanicNew（11-18 改 ID 了，Liquipedia 还没更新）");
r = await editor.post(`${panicPage}/fix`, { field: "nats", value: "United States" });
check("和 Liquipedia 写的一样 → 不用改", r.status === 400 && r.text.includes("和 Liquipedia 写的一样"));
r = await editor.post(`${panicPage}/fix`, { field: "earnings", value: "1" });
check("不能改的项 → 提示", r.status === 400 && r.text.includes("这一项不能改"));
r = await editor.post(`${panicPage}/fix`, { field: "id", value: "  " });
check("ID 空着 → 提示", r.status === 400 && r.text.includes("不能空着"));
r = await editor.post(`${panicPage}/fix`, { field: "birth", value: "2000/5/20" });
check("生日写法不对 → 提示", r.status === 400 && r.text.includes("1999-05-20"));
r = await editor.post(`${panicPage}/fix`, { field: "nats", value: "United States，Canada" });
check("国籍可以写好几个（逗号、顿号都行）", doc(OVERRIDES).items[PANIC].nats.value === "United States、Canada");
r = await editor.post(`${panicPage}/unfix`, { field: "nats" });
check("撤掉修正", r.location === `${panicPage}?ok=unfixed` && !doc(OVERRIDES).items[PANIC].nats && logs()[0].startsWith("撤掉对"));
r = await editor.post(`${panicPage}/unfix`, { field: "nats" });
check("撤掉没有的修正 → 提示", r.status === 400);

// 待确认按修正以后的 ID 比名字：Kite 改成 Kitey，选手库里的 Kitey 就对上了
await editor.post("/pros/Kite_(player)/fix", { field: "id", value: "Kitey", reason: "改名" });
r = await viewer.get("/liquipedia");
check("待确认按修正的 ID 比名字", r.text.includes("选手库的「Kitey」（狼队）是不是 Liquipedia 上的 Kitey"));

// ---------- 和 Liquipedia 对账 ----------
const fresh = structuredClone(idx);
fresh.players[PANIC].id = "PanicNew";      // Liquipedia 也改成一样的了
fresh.players["Kite_(player)"].id = "KiteX";  // Liquipedia 改成了别的
now += 10 * MINUTE;
await reconcile(env.DATA, fresh, [PANIC, "Kite_(player)"], now);
check("Liquipedia 改成一样的 → 修正自动撤掉、记下来", !doc(OVERRIDES).items[PANIC]
  && logs().some((t) => t === `Liquipedia 已经把 ${PANIC} 的ID改成「PanicNew」了，手动修正撤掉`));
const kite = () => doc(OVERRIDES).items["Kite_(player)"]?.id;
check("Liquipedia 改成别的 → 标出来，修正照旧用", kite().value === "Kitey" && kite().conflict.value === "KiteX");
await put(INDEX, fresh);
r = await admin.get("/liquipedia");
check("待确认里列出来、能选用哪个", r.text.includes("你们改成了「Kitey」，Liquipedia 后来改成了「KiteX」") && r.text.includes("用 Liquipedia 的")
  && r.text.includes("留着我们改的"));
r = await admin.get("/");
check("首页的待确认算上它", /[1-9] 条要确认/.test(r.text));
r = await viewer.post("/pros/Kite_(player)/resolve", { field: "id", keep: "ours" });
check("只读的不能处理", r.status === 403);
r = await editor.post("/pros/Kite_(player)/resolve", { field: "id", keep: "ours" });
check("留着我们改的 → 不再问，记下 Liquipedia 现在写的", r.location === "/liquipedia?ok=resolved#confirm" && !kite().conflict && kite().source === "KiteX");
await reconcile(env.DATA, fresh, ["Kite_(player)"], now);
check("Liquipedia 没再变 → 不再问", !kite().conflict);
fresh.players["Kite_(player)"].id = "KiteY";
await reconcile(env.DATA, fresh, ["Kite_(player)"], now);
r = await editor.post("/pros/Kite_(player)/resolve", { field: "id", keep: "theirs" });
check("又变了 → 用 Liquipedia 的 → 修正撤掉", !kite() && logs()[0].includes("改用 Liquipedia 的「KiteY」"));
r = await editor.post("/pros/Kite_(player)/resolve", { field: "id", keep: "theirs" });
check("处理过的再点 → 回待确认", r.status === 303);

// ---------- 定时任务里真的会对账 ----------
await editor.post(`${panicPage}/fix`, { field: "team", value: "100 Thieves", reason: "转会了" });
await put(STATE, { queue: [{ kind: "players", pages: [PANIC] }], round: { started: now, done: 0, total: 1 }, recent: [], attempts: {},
                   errors: [], lastRequestAt: 0, lastRoundDone: 0, refresh: false });
env.LIQUIPEDIA_API_KEY = "k";
const api = async () => new Response(JSON.stringify({ result: [{ pagename: PANIC, id: "PanicNew", teampagename: "100_Thieves", nationality: "United States" }] }));
check("定时任务抓到选手资料", (await tick(env, now + 10 * MINUTE, api)) === "done");
check("抓到以后对账：现在的队一样了 → 修正撤掉", !doc(OVERRIDES).items[PANIC]);
r = await viewer.get(panicPage);
check("页面上不再标修正", !r.text.includes("Liquipedia 写的是") && r.text.includes("100 Thieves"));
