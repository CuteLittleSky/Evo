// 赛管毛毛用过以后的选手库优化（2026-11-21）：分赛区、战队页、整队删、批量修改（名字、战队也能改）、勾选几位一起删、改完回到原来的地方。
// 由 test.mjs 运行。赛区照 ALGS 四个大区、国内队算亚太南（用户定的）；没手动标的按 Liquipedia 猜（现在的战队的赛区，没有战队看国籍）。

import { hash, newToken, update } from "./auth.js";
import { EVENTS, LOG, PLAYERS } from "./data.js";
import { teamAnchor } from "./pages_teams.js";
import { INDEX } from "./lpsync.js";
import { REGION_FILE, guessRegions } from "./teams.js";
import { FakeBucket, check, makeBrowser } from "./testkit.mjs";

const HOUR = 3600 * 1000;
const now = Date.UTC(2026, 10, 21, 4, 0);  // 北京时间 11-21 12:00
const env = { DATA: new FakeBucket(), PUBLIC: new FakeBucket() };
const P = () => env.DATA.json(PLAYERS, '{"items":[]}').items;
const logs = () => env.DATA.json(LOG, '{"items":[]}').items.map((x) => x.text);
const who = (name) => P().find((p) => p.name === name);
const regionsSaved = () => env.DATA.json(REGION_FILE, '{"items":{}}').items;

async function join(name, role) {
  const token = newToken();
  const h = await hash(token);
  await update(env.DATA, (data) => {
    data.invites.push({ id: Math.random().toString(16).slice(2, 10).padEnd(8, "0"), h, name, role, publish: role === "admin", created: now,
                        createdBy: "命令行", expires: now + 7 * 24 * HOUR });
  });
  const b = makeBrowser(env, () => now);
  await b.post(`/join/${token}`);
  return b;
}
const editor = await join("毛毛", "editor");
const viewer = await join("看看", "viewer");

// 选手库：五支队。Liquipedia 上：日本队的两位有战队（Japan）；国内队一位有战队（China）、一位只有国籍；北美队看国籍；
// 台湾队看国籍（猜亚太北）；老队谁都认不出
const add = (name, team, liquipedia = "", room = "") => editor.post("/players/add", { name, team, platform: room ? "douyu" : "none", room, liquipedia });
await add("Nao", "日本队", "Nao", "11");
await add("Ken", "日本队", "Ken", "12");
await add("张三", "国内队", "Zhang", "21");
await add("李四", "国内队", "Li", "22");
await add("Bob", "北美队", "Bob");
await add("阿明", "台湾队", "Ming");
await add("老一", "老队");
await add("老二", "老队");
await env.DATA.put(INDEX, JSON.stringify({ events: {}, teams: { JPTeam: { page: "JPTeam", region: "Japan" }, CNTeam: { page: "CNTeam", region: "China" } },
  players: { Nao: { teamPage: "JPTeam", nats: ["Japan"] }, Ken: { teamPage: "JPTeam", nats: ["Japan"] }, Zhang: { teamPage: "CNTeam", nats: ["China"] },
             Li: { teamPage: "", nats: ["China"] }, Bob: { teamPage: "", nats: ["United States"] }, Ming: { nats: ["Taiwan"] } } }));
await env.DATA.put("manual/teams.json", JSON.stringify({ items: { 老队: { logo: "aaaaaaaaaaaaaaaa.png" } } }));
const event = (name, start, end, teams) => editor.post("/events/add", [["name", name], ["type", "联赛"], ["stage", ""], ["start", start], ["end", end],
  ...teams.map((t) => ["teams", t])]);
await event("下周的比赛", "2026-11-25T19:00", "2026-11-25T22:00", ["日本队", "国内队"]);
await event("上个月的比赛", "2026-10-10T19:00", "2026-10-10T22:00", ["老队", "北美队"]);

// ---------- 按 Liquipedia 猜赛区 ----------
const idx = env.DATA.json(INDEX);
check("猜赛区：战队的赛区（日本 → 亚太北、中国 → 亚太南）、没有战队看国籍（美国 → 美洲、台湾 → 亚太北）、认不出的不猜",
  JSON.stringify(guessRegions(P(), idx)) === JSON.stringify({ 日本队: "apac_n", 国内队: "apac_s", 北美队: "americas", 台湾队: "apac_n" }));

// ---------- 战队页 ----------
let r = await viewer.get("/teams");
check("只读的能看战队页，不能改（下拉灰掉、没有保存和删的按钮）", r.status === 200 && r.text.includes("<h1>战队和赛区</h1>")
  && r.text.includes(" disabled") && !r.text.includes('form="region-form">保存赛区') && !r.text.includes('name="del"'));
r = await editor.get("/teams");
check("赛区的标签：全部、亚太北、亚太南、美洲、没标赛区（有队的才列）", r.text.includes('href="/teams" aria-current="page">全部（5）')
  && r.text.includes('href="/teams?region=apac_n">亚太北（2）') && r.text.includes('href="/teams?region=apac_s">亚太南（1）')
  && r.text.includes('href="/teams?region=americas">美洲（1）') && r.text.includes('href="/teams?region=none">没标赛区（1）') && !r.text.includes("EMEA（"));
check("每一行：几个人、最近一场赛事；猜的写着按 Liquipedia 猜", r.text.includes("按 Liquipedia 猜：亚太北") && r.text.includes("2 人")
  && r.text.includes("最近：「上个月的比赛」") && r.text.includes("没参加过赛程里的赛事"));
check("有比赛快开始的队不能勾选删", /value="日本队" form="del-form" disabled/.test(r.text) && !/value="老队" form="del-form" disabled/.test(r.text)
  && r.text.includes("有进行中、即将开始的赛事，不能删"));
check("最近有比赛的队排前面，老的在后面", r.text.indexOf(">日本队<") < r.text.indexOf(">老队<") && r.text.indexOf(">老队<") < r.text.indexOf(">台湾队<"));
r = await editor.get("/teams?region=apac_n");
check("只看亚太北", r.text.includes(">日本队<") && r.text.includes(">台湾队<") && !r.text.includes(">国内队<"));

// ---------- 标赛区 ----------
const teamRows = (text) => [...text.matchAll(/name="tn_(\d+)" value="([^"]*)"/g)].map((m) => [m[2], m[1]]);
r = await editor.get("/teams");
const idxOf = Object.fromEntries(teamRows(r.text));
const regionForm = (picks) => {
  const out = [["region", "all"]];
  for (const [team, i] of Object.entries(idxOf)) out.push([`tn_${i}`, team], [`tw_${i}`, ""], [`tr_${i}`, picks[team] ?? ""]);
  return out;
};
r = await viewer.post("/teams/regions", regionForm({ 台湾队: "apac_s" }));
check("只读的不能标赛区", r.status === 403);
r = await editor.post("/teams/regions", regionForm({ 台湾队: "apac_s", 老队: "emea" }));
check("标赛区：只存改过的两支，记下来（照页面上的顺序）", r.location === "/teams?ok=regions&n=2" && regionsSaved().台湾队 === "apac_s"
  && regionsSaved().老队 === "emea" && Object.keys(regionsSaved()).length === 2 && logs()[0] === "标赛区（2 支队）：老队 → EMEA、台湾队 → 亚太南");
r = await editor.get(r.location);
check("回到战队页：说改了几支；手动标的不再写猜的", r.text.includes("保存好了：改了 2 支队的赛区") && r.text.includes('href="/teams?region=emea">EMEA（1）')
  && r.text.includes('href="/teams?region=apac_s">亚太南（2）'));
r = await editor.post("/teams/regions", regionForm({}));
check("什么都没改 → 说没有改动", r.location === "/teams?ok=regions&n=0");
r = await editor.post("/teams/regions", [["tn_0", "台湾队"], ["tw_0", "apac_s"], ["tr_0", ""]]);
check("选回没标 → 去掉手动标的（回到按 Liquipedia 猜）", !("台湾队" in regionsSaved()) && logs()[0] === "标赛区（1 支队）：台湾队 → 没标（按 Liquipedia 猜）");
r = await editor.post("/teams/regions", [["tn_0", "台湾队"], ["tw_0", ""], ["tr_0", "mars"]]);
check("不认识的赛区 → 不存", r.status === 400 && r.text.includes("「台湾队」的赛区不认识"));

// ---------- 选手库：分赛区、跳到某一队、改完回到原来的地方 ----------
r = await editor.get("/players");
check("选手库：赛区的标签、每支队标着赛区（猜的写猜的）、去战队页的链接", r.text.includes('href="/players?region=apac_n">亚太北（2）')
  && r.text.includes("亚太北（猜的）") && r.text.includes(">EMEA</a>") && r.text.includes('href="/teams">战队和赛区 ›'));
check("每支队、每个人有编号；顶上有跳到某一队的目录", r.text.includes(`<section class="card" id="${teamAnchor("国内队")}">`)
  && r.text.includes(`<li class="row" id="p-${who("张三").id}">`) && r.text.includes(`<a href="#${teamAnchor("老队")}">老队</a>`));
r = await editor.get("/players?region=apac_n");
check("只看亚太北：日本队、台湾队", r.text.includes("<h2>日本队</h2>") && r.text.includes("<h2>台湾队</h2>") && !r.text.includes("<h2>国内队</h2>")
  && r.text.includes('<input type="hidden" name="view" value="apac_n">') && r.text.includes('href="/players/rooms?view=apac_n">在一张表里批量修改'));
const nao = who("Nao");
r = await editor.post(`/players/${nao.id}/save`, { rev: String(nao.rev), name: "Nao", team: "日本队", platform: "douyu", room: "19", liquipedia: "Nao", view: "apac_n" });
check("改一个人：回到同一个赛区标签、那个人那里", r.location === `/players?ok=saved&region=apac_n#p-${nao.id}`);
r = await editor.post(`/players/${who("Ken").id}/delete`, { view: "apac_n" });
check("删一个人：回到他那一队", r.location === `/players?ok=deleted&region=apac_n#${teamAnchor("日本队")}`);
r = await editor.post(`/players/${nao.id}/save`, { rev: String(who("Nao").rev), name: "Nao", team: "日本队", platform: "douyu", room: "18", liquipedia: "Nao", view: "../x" });
check("地址里的赛区写乱了 → 当作全部", r.location === `/players?ok=saved#p-${nao.id}`);

// ---------- 战队改名、并队：手动标的赛区跟着搬 ----------
await editor.post("/teams/rename", { from: "老队", to: "老老队" });
check("战队改名：赛区跟着搬", regionsSaved().老老队 === "emea" && !("老队" in regionsSaved()));

// ---------- 批量修改：名字、战队也能改 ----------
r = await editor.get("/players/rooms?view=apac_s");
check("批量修改：按赛区看，名字、战队、平台、房间号四格，勾选删的框", r.status === 200 && r.text.includes("<h1>批量修改选手</h1>")
  && r.text.includes(`name="n_${who("张三").id}"`) && r.text.includes(`name="t_${who("张三").id}"`) && r.text.includes(`name="d" value="${who("张三").id}"`)
  && !r.text.includes(`name="n_${nao.id}"`) && r.text.includes('formaction="/players/bulk-delete"') && r.text.includes('<datalist id="team-names">'));
const zs = who("张三");
const ls = who("李四");
const bulk = (rows, more = []) => [...rows.flatMap(([p, name, team, room]) => [[`n_${p.id}`, name], [`t_${p.id}`, team], [`p_${p.id}`, "douyu"],
  [`r_${p.id}`, room], [`v_${p.id}`, String(p.rev)]]), ["view", "apac_s"], ...more];
r = await editor.post("/players/rooms", bulk([[zs, "李四", "国内队", "21"], [ls, "李四", "国内队", "22"]]));
check("改完和别人重名 → 整批不存", r.status === 400 && r.text.includes("名字「李四」有两位了") && who("张三"));
r = await editor.post("/players/rooms", bulk([[zs, "张三丰", "新国内队", "21"], [ls, "李四", "国内队", "23"]]));
check("改名字、换队、改房间号，一次存下；回到同一个标签", r.location === "/players/rooms?ok=rooms&n=2&view=apac_s"
  && who("张三丰").team === "新国内队" && who("李四").room === "23");
check("改动记录写清楚每个人改了什么", logs()[0] === "批量修改选手（2 位）：张三：名字 张三 → 张三丰，战队 国内队 → 新国内队；李四：房间号 22 → 23");

// ---------- 勾选几位一起删 ----------
r = await editor.post("/players/bulk-delete", [["view", "all"]]);
check("一个都没勾 → 提示", r.status === 400 && r.text.includes("没有勾选要删的选手"));
r = await editor.post("/players/bulk-delete", [["d", who("老一").id], ["d", who("老二").id], ["view", "all"], [`n_${who("老一").id}`, "老一"]]);
check("勾了两位 → 先问一次（列出是谁），还没删", r.status === 200 && r.text.includes("删掉 2 位选手？") && r.text.includes("老一") && r.text.includes("老二")
  && r.text.includes('name="confirm" value="1"') && who("老一"));
r = await viewer.post("/players/bulk-delete", [["d", who("老一").id], ["confirm", "1"]]);
check("只读的不能删", r.status === 403 && who("老一"));
r = await editor.post("/players/bulk-delete", [["d", who("老一").id], ["d", who("老二").id], ["view", "all"], ["confirm", "1"]]);
check("确定以后删掉，记下来", r.location === "/players/rooms?ok=pdeleted&n=2&view=all" && !who("老一") && !who("老二")
  && logs()[0] === "删掉选手（2 位）：老一（老老队）、老二（老老队）");

// ---------- 整队删 ----------
await add("老三", "老老队");
r = await editor.post("/teams/delete", [["region", "all"]]);
check("一支都没勾 → 提示", r.status === 400 && r.text.includes("没有勾选要删的队"));
r = await editor.post("/teams/delete", [["del", "日本队"], ["confirm", "1"]]);
check("有比赛快开始的队 → 不删，说是哪场", r.status === 400 && r.text.includes("「日本队」在进行中、即将开始的赛事里") && who("Nao"));
r = await editor.post("/teams/delete", [["del", "老老队"], ["del", "北美队"], ["region", "all"]]);
check("勾了两支 → 先问一次（列出队里的人）", r.status === 200 && r.text.includes("删掉 2 支队？") && r.text.includes("1 位选手：老三") && who("老三"));
r = await editor.post("/teams/delete", [["del", "老老队"], ["del", "北美队"], ["region", "all"], ["confirm", "1"]]);
const logosLeft = env.DATA.json("manual/teams.json").items;
check("确定以后整队删掉：选手、队徽、赛区都去掉，记下来", r.location === "/teams?ok=tdeleted&teams=2&players=2" && !who("老三") && !who("Bob")
  && !("老老队" in logosLeft) && !("老老队" in regionsSaved()) && logs()[0] === "删掉战队（2 支，连同 2 位选手）：老老队（1 人）、北美队（1 人）");
r = await editor.get(r.location);
check("回到战队页：说删了几支", r.text.includes("删掉了 2 支队（连同 2 位选手）") && !r.text.includes(">老老队<"));
check("结束了的赛事里写着删掉的队也能删（赛程不动）", env.DATA.json(EVENTS).items.find((e) => e.name === "上个月的比赛").teams.includes("北美队"));
