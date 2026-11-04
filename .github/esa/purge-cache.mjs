#!/usr/bin/env node
/**
 * 刷新 ESA 缓存（按 URL，type=file）。Evo 发版把安装包传到 OSS 后，用它清掉 ESA 边缘旧缓存。
 * 做法同 ApexPacks 的 scripts/esa/purge-cache.mjs，走官方 OpenAPI 的 PurgeCaches。
 *
 * 用法：
 *   node purge-cache.mjs --url https://paimiaoesa.skycraft.cn/evo/Evo-0.5.1-win64.zip --url https://paimiaoesa.skycraft.cn/evo/latest.zip
 *   node purge-cache.mjs ... --dry-run
 *
 * 环境变量：
 *   ESA_SITE_ID            站点 ID（ESA 控制台站点概览可查；和派喵图片站同一个 skycraft.cn 站点就是 1048353729088000）
 *   ESA_ACCESS_KEY_ID / ESA_ACCESS_KEY_SECRET   RAM 子账号（要有该站点的 esa:PurgeCaches 权限）；没配则回退用 OSS_ACCESS_KEY_ID/SECRET
 *   ESA_ENDPOINT           可选，默认 esa.cn-hangzhou.aliyuncs.com
 *
 * 依赖安装（一次性）：npm install --prefix .github/esa
 */
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);

function loadSdk() {
  const ESA = require('@alicloud/esa20240910');
  const OpenApi = require('@alicloud/openapi-client');
  const Client = ESA.default || ESA;
  const Config = OpenApi.Config || (OpenApi.default && OpenApi.default.Config);
  const reqMod = require('@alicloud/esa20240910/dist/models/PurgeCachesRequest');
  const contentMod = require('@alicloud/esa20240910/dist/models/PurgeCachesRequestContent');
  const PurgeCachesRequest = reqMod.default || reqMod.PurgeCachesRequest || reqMod;
  const PurgeCachesRequestContent = contentMod.default || contentMod.PurgeCachesRequestContent || contentMod;
  return { Client, Config, PurgeCachesRequest, PurgeCachesRequestContent };
}

const args = process.argv.slice(2);
const urls = [];
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--url' && args[i + 1]) urls.push(args[++i]);
}
const DRY = args.includes('--dry-run');
const SITE_ID = Number(process.env.ESA_SITE_ID || 0);
const ENDPOINT = process.env.ESA_ENDPOINT || 'esa.cn-hangzhou.aliyuncs.com';
const AK = process.env.ESA_ACCESS_KEY_ID || process.env.OSS_ACCESS_KEY_ID || '';
const SK = process.env.ESA_ACCESS_KEY_SECRET || process.env.OSS_ACCESS_KEY_SECRET || '';

(async () => {
  const list = [...new Set(urls.filter(Boolean))];
  if (!list.length) throw new Error('没有要刷新的 URL（用 --url）');
  console.log(`[purge] site=${SITE_ID || '(dry)'} endpoint=${ENDPOINT} urls=${list.length}`);
  list.forEach((u) => console.log('  -', u));
  if (DRY) {
    console.log('[purge] dry-run，不调 API');
    return;
  }
  if (!SITE_ID) throw new Error('缺少 ESA_SITE_ID');
  if (!AK || !SK) throw new Error('缺少 ESA_ACCESS_KEY_ID / ESA_ACCESS_KEY_SECRET（或 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET）');

  const { Client, Config, PurgeCachesRequest, PurgeCachesRequestContent } = loadSdk();
  const config = new Config({ accessKeyId: AK, accessKeySecret: SK });
  config.endpoint = ENDPOINT;
  const client = new Client(config);

  // ESA 单次提交有上限，按 500 一批分批刷新
  const CHUNK = 500;
  for (let i = 0; i < list.length; i += CHUNK) {
    const batch = list.slice(i, i + CHUNK);
    const content = new PurgeCachesRequestContent({ files: batch });
    const req = new PurgeCachesRequest({ siteId: SITE_ID, type: 'file', content });
    const res = await client.purgeCaches(req);
    console.log(`[purge] batch ${Math.floor(i / CHUNK) + 1} (${batch.length} URLs) →`, JSON.stringify(res.body || res));
  }
})().then(() => {
  // 阿里云 SDK 可能留着 keep-alive 连接，不显式退出的话 node 会一直挂着、这一步永远不结束
  console.log('[purge] done');
  process.exit(0);
}).catch((e) => {
  console.error('[purge] FAIL', e && (e.message || e));
  process.exit(1);
});
