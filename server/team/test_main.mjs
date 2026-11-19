// 主舞台的测试（2026-11-18）。由 test.mjs 运行。和 tools/esports.py 逐项对照的在 tests/test_team_publish.py。
// 走一遍：赛事表单里加几路（抖音号转小写、空行不管）、各种写错（超过 8 路、没写全、名字空着、重复、平台写字母）、出错时留着、
// 老页面没有主舞台那几格时照旧、改了记下来、粘贴导入认第 7 列（没有这一列照旧）、发布时带上（编号、放在 teams 后面）、
// 和选手同一个直播间时提醒、名字带"示例"不发。

import { hash, newToken, update } from "./auth.js";
import { EVENTS } from "./data.js";
import { FakeBucket, check, makeBrowser } from "./testkit.mjs";

const MINUTE = 60 * 1000;
const now = Date.UTC(2026, 10, 18, 4, 0);  // 北京 11-18 12:00
const env = { DATA: new FakeBucket(), PUBLIC: new FakeBucket() };
const browser = () => makeBrowser(env, () => now);
const events = () => env.DATA.json(EVENTS, '{"items":[]}').items;
const logs = () => env.DATA.json("log.json", '{"items":[]}').items.map((x) => x.text);

async function join(name, role, publish = false) {
  const token = newToken();
  const h = await hash(token);
  await update(env.DATA, (data) => {
    data.invites.push({ id: Math.random().toString(16).slice(2, 10).padEnd(8, "0"), h, name, role, publish, created: now,
                        createdBy: "命令行", expires: now + 7 * 24 * 60 * MINUTE });
  });
  const b = browser();
  await b.post(`/join/${token}`);
  return b;
}

const admin = await join("黄指导", "admin");
const editor = await join("Momona", "editor");
let room = 1000;
for (const [team, names] of [["猎鹰", ["A1", "A2", "A3"]], ["狼队", ["B1", "B2", "B3"]]]) {
  for (const name of names) await editor.post("/players/add", { name, team, platform: "bilibili", room: String(room++) });
}

const form = (name, main, extra = []) => [["name", name], ["type", "联赛"], ["stage", ""], ["start", "2026-11-18T19:00"],
  ["end", "2026-11-18T23:00"], ["teams", "猎鹰"], ["teams", "狼队"],
  ...main.flatMap(([platform, roomText, label]) => [["main_platform", platform], ["main_room", roomText], ["main_name", label]]), ...extra];

// ---------- 加 ----------
let r = await editor.get("/events");
check("赛事表单里有主舞台", r.text.includes("主舞台（可以空着）") && r.text.includes('name="main_platform"'));
r = await editor.post("/events/add", form("联赛 第 3 周", [["bilibili", "7734200", "官方直播"], ["douyin", "XXFPS", "某某 解说"], ["", "", ""]]));
let e = events()[0];
check("加两路主舞台：抖音号转小写、空行不管", r.location === "/events?ok=added"
  && JSON.stringify(e.main) === JSON.stringify([{ platform: "bilibili", room: "7734200", name: "官方直播" }, { platform: "douyin", room: "xxfps", name: "某某 解说" }]));
check("改动记录写着几路", logs()[0].endsWith("主舞台 2 路"));
r = await editor.get("/events");
check("赛程上写着主舞台几路", r.text.includes("主舞台 2 路"));

// ---------- 写错 ----------
const nine = Array.from({ length: 9 }, (_, i) => ["bilibili", String(9000 + i), `第 ${i + 1} 路`]);
r = await editor.post("/events/add", form("太多", nine));
check("超过 8 路 → 提示", r.status === 400 && r.text.includes("主舞台最多 8 路（现在 9 路）"));
r = await editor.post("/events/add", form("没写全", [["bilibili", "", "官方直播"]]));
check("房间号空着 → 提示", r.status === 400 && r.text.includes("主舞台的房间号「」只能是数字"));
r = await editor.post("/events/add", form("没名字", [["bilibili", "1", ""]]));
check("名字空着 → 提示", r.status === 400 && r.text.includes("主舞台的名字不能空着"));
r = await editor.post("/events/add", form("重复", [["douyin", "XXFPS", "一"], ["douyin", "xxfps", "二"]]));
check("同一个抖音号大小写不同 → 写了两次", r.status === 400 && r.text.includes("主舞台里 抖音 房间 xxfps 写了两次"));
r = await editor.post("/events/add", form("斗鱼字母", [["douyu", "abc", "一"]]));
check("斗鱼写字母 → 提示", r.status === 400 && r.text.includes("只能是数字"));
r = await editor.post("/events/add", form("留着", [["douyu", "abc", "某解说"]]));
check("出错时留着主舞台填的", r.text.includes('value="某解说"') && r.text.includes('value="abc"'));
check("都没存", events().length === 1);

// ---------- 改 ----------
r = await editor.post(`/events/${e.id}/save`, form("联赛 第 3 周", [], [["rev", "1"]]).filter(([k]) => !k.startsWith("main_")));
check("老页面没有主舞台那几格 → 照旧", events()[0].main.length === 2);
r = await editor.post(`/events/${e.id}/save`, form("联赛 第 3 周", [["bilibili", "7734200", "官方直播"]], [["rev", String(events()[0].rev)]]));
check("改成一路 → 记下来", events()[0].main.length === 1 && logs()[0] === "修改赛事「联赛 第 3 周」：主舞台：去掉了 某某 解说（抖音 xxfps）");

// ---------- 导入 ----------
r = await editor.post("/events/import", { table: "名称\t类型\t阶段\t开始\t结束\t队伍\t主舞台\n杯赛\t杯赛\t\t2026-11-19 19:00\t2026-11-19 22:00\t猎鹰、狼队\tB站 1 官方、抖音 Abc_1 解说 二号" });
const cup = () => events().find((x) => x.name === "杯赛");
check("粘贴导入认第 7 列（名字里可以有空格）", r.location === "/events?ok=imported&added=1&changed=0"
  && JSON.stringify(cup().main) === JSON.stringify([{ platform: "bilibili", room: "1", name: "官方" }, { platform: "douyin", room: "abc_1", name: "解说 二号" }]));
r = await editor.post("/events/import", { table: "杯赛\t杯赛\t决赛\t2026-11-19 19:00\t2026-11-19 22:00\t猎鹰、狼队" });
check("粘贴的表没有第 7 列 → 主舞台照旧", r.location === "/events?ok=imported&added=0&changed=1" && cup().main.length === 2 && cup().stage === "决赛");
r = await editor.post("/events/import", { table: "坏的\t杯赛\t\t2026-11-20 19:00\t2026-11-20 22:00\t猎鹰\t快手 1 某某" });
check("第 7 列写错 → 说第几行", r.status === 400 && r.text.includes("第 1 行：主舞台的平台「快手」不认识"));

// ---------- 发布 ----------
await editor.post(`/events/${e.id}/save`, form("联赛 第 3 周", [["bilibili", "7734200", "官方直播"], ["bilibili", "1000", "A1 的直播间"]],
  [["rev", String(events()[0].rev)]]));
r = await admin.get("/publish");
check("主舞台和选手是同一个直播间 → 提醒", r.text.includes("「联赛 第 3 周」的主舞台「A1 的直播间」和选手「A1」是同一个直播间"));
await admin.post("/publish/test");
const file = JSON.parse(env.PUBLIC.objects.get("esports_test.json").body);
const league = file.events.find((x) => x.name === "联赛 第 3 周");
check("发布：主舞台带编号（平台-房间号）、放在 teams 后面", JSON.stringify(league.main) === JSON.stringify([
  { id: "bilibili-7734200", name: "官方直播", platform: "bilibili", room: "7734200" },
  { id: "bilibili-1000", name: "A1 的直播间", platform: "bilibili", room: "1000" }]) && Object.keys(league).join() === "id,name,type,stage,start,end,teams,main");
check("没有主舞台的赛事不带 main", file.events.every((x) => x.name === "联赛 第 3 周" || x.name === "杯赛" || !("main" in x)));
await editor.post(`/events/${e.id}/save`, form("联赛 第 3 周", [["bilibili", "7734200", "示例直播"]], [["rev", String(events()[0].rev)]]));
r = await admin.post("/publish/test");
check("主舞台名字带示例 → 不发", r.status === 400 && r.text.includes("还有示例数据（示例直播）"));
