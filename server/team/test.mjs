// 协作网页的测试：node server/team/test.mjs（只要 Node.js，不用 wrangler、不用联网）。
// 用内存里的假存储桶把 worker.js 的每条路走一遍：
//   test_members.mjs   成员、邀请链接、登录（第 1 步）
//   test_esports.mjs   选手库、赛程、从表格导入、发布、平台开关、改动记录（第 2 步）
//   test_lp.mjs        Liquipedia 定时抓取、待确认（第 3 步）
//
// 另外几个用法（tests/test_team_publish.py 用，和 tools/esports.py 对照）：
//   --build 输入.json          输入 {"players", "events", "switches", "now"}，打印生成的 esports.json 和检查结果
//   --check-players 输入.json  一组选手过一遍保存时的检查和整张表的检查（房间号的写法、重复）
//   --platforms                打印平台名单（和 config/watch.json 比）

import { readFileSync } from "node:fs";
import { PLATFORMS, build, checkPlayers, cleanPlayerFields } from "./esports.js";
import { summary } from "./testkit.mjs";

const args = process.argv.slice(2);
if (args[0] === "--build") {
  // 选手先过一遍保存时的检查（平台写中文名也认、抖音房间号转小写），和网页上存下来的一样
  const input = JSON.parse(readFileSync(args[1], "utf-8"));
  const players = input.players.map((p) => ({ ...p, ...cleanPlayerFields(p) }));
  console.log(JSON.stringify(build(players, input.events, input.switches, input.now)));
} else if (args[0] === "--check-players") {
  // 一组选手：每个人保存时的检查 + 整张表的检查 → {errors: [...], rooms: [存下来的房间号]}
  const errors = [];
  const kept = [];
  for (const p of JSON.parse(readFileSync(args[1], "utf-8"))) {
    try {
      kept.push({ ...p, ...cleanPlayerFields(p) });
    } catch (error) {
      errors.push(error.message);
    }
  }
  errors.push(...checkPlayers(kept).errors.filter((e) => e.includes("房间")));
  console.log(JSON.stringify({ errors, rooms: kept.map((p) => p.room) }));
} else if (args[0] === "--platforms") {
  console.log(JSON.stringify(Object.entries(PLATFORMS).map(([key, p]) => [key, p.name, p.room, p.pattern ?? null])));
} else {
  console.log("成员、邀请、登录");
  await import("./test_members.mjs");
  console.log("\n选手库、赛程、发布");
  await import("./test_esports.mjs");
  console.log("\nLiquipedia 定时抓取");
  await import("./test_lp.mjs");
  process.exit(summary());
}
