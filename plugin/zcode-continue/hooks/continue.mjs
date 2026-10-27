/**
 * zburn-continue —— ZCode Stop 钩子：额度没烧完就自动续派
 *
 * 机制：
 *   1. ZCode 每结束一个回合都会触发 Stop 钩子，事件负载以 JSON 从 stdin 进来；
 *   2. 本脚本读一次后端剩余额度（HTTP 或本地命令都行，见下面的配置）；
 *   3. 剩余额度 > 阈值 → stdout 输出 {"decision":"block","reason":"<继续指令>"}，
 *      ZCode 收到后不结束回合，而是把 reason 当成新的用户指令接着跑；
 *   4. 额度烧完 / 读不到 / 被人工关闭 → 不输出任何决策，正常放行停止。
 *
 * 配置全部走环境变量（都可以不设，走默认值）：
 *   ZBURN_DIR            状态目录（日志/计数/开关/指令文件都在这）   默认 ~/.zburn
 *   ZBURN_GATEWAY        后端基址                                    默认 http://127.0.0.1:3000
 *   ZBURN_ADMIN_KEY      后端管理 key（Bearer）                      默认空
 *   ZBURN_QUOTA_PATH     额度接口路径                                默认 /admin/api/accounts
 *   ZBURN_STOP_REMAINING 剩余额度低于它就算烧完                      默认 0（=永不因额度停）
 *   ZBURN_ENABLE_FLAG    开关文件名（放在 ZBURN_DIR 下）              默认 continue.enabled
 *   ZBURN_PROMPT_FILE    续派指令文件名（放在 ZBURN_DIR 下）          默认 next-prompt.txt
 *   ZBURN_FALLBACK       没有指令文件时用的兜底指令
 *
 * 安全阀：
 *   - 开关文件不存在 → 只写日志不干预（探针模式，用来确认钩子有没有被加载）；
 *   - 两次续派最小间隔 5 秒；总次数上限 50000（防异常无限续派）；
 *   - 任何异常都放行（绝不把应用卡死在钩子里）。
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const HOME = os.homedir();
const DIR = process.env.ZBURN_DIR || path.join(HOME, '.zburn');
const GATEWAY = (process.env.ZBURN_GATEWAY || 'http://127.0.0.1:3000').replace(/\/$/, '');
const ADMIN_KEY = process.env.ZBURN_ADMIN_KEY || '';
const QUOTA_PATH = process.env.ZBURN_QUOTA_PATH || '/admin/api/accounts';
const STOP_REMAINING = Number(process.env.ZBURN_STOP_REMAINING || 0);
const ENABLE_FLAG = path.join(DIR, process.env.ZBURN_ENABLE_FLAG || 'continue.enabled');
const PROMPT_FILE = path.join(DIR, process.env.ZBURN_PROMPT_FILE || 'next-prompt.txt');
const LOG_FILE = path.join(DIR, 'hook.log');
const STATE_FILE = path.join(DIR, 'hook-state.json');
const MIN_INTERVAL_MS = 5_000;
const MAX_CONTINUATIONS = 50_000;
const FALLBACK_PROMPT =
  process.env.ZBURN_FALLBACK ||
  'Continue exactly where you left off. Do not summarize, do not ask questions, just keep producing.';

function log(msg) {
  try {
    fs.mkdirSync(DIR, { recursive: true });
    fs.appendFileSync(LOG_FILE, `${new Date().toISOString()} ${msg}\n`);
  } catch {
    /* 日志失败不影响钩子 */
  }
}

function readState() {
  try {
    return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
  } catch {
    return { count: 0, lastAt: 0 };
  }
}

function writeState(state) {
  try {
    fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2), 'utf8');
  } catch {
    /* 节流状态写失败只影响节流 */
  }
}

/** 在任意 JSON 里找 remaining/balance 之类字段求和（换后端不用改代码） */
function sumRemaining(node, total = { v: null }) {
  const KEYS = new Set(['remaining', 'remain', 'balance', 'left', 'quota_remaining', 'surplus']);
  if (Array.isArray(node)) {
    for (const v of node) sumRemaining(v, total);
  } else if (node && typeof node === 'object') {
    for (const [k, v] of Object.entries(node)) {
      if (KEYS.has(k.toLowerCase()) && typeof v === 'number') {
        total.v = (total.v || 0) + v;
      } else {
        sumRemaining(v, total);
      }
    }
  }
  return total.v;
}

async function quotaRemaining() {
  const headers = {};
  if (ADMIN_KEY) headers.Authorization = `Bearer ${ADMIN_KEY}`;
  const res = await fetch(`${GATEWAY}${QUOTA_PATH}`, { headers });
  if (!res.ok) throw new Error(`gateway ${res.status}`);
  return sumRemaining(await res.json());
}

function pickPrompt() {
  try {
    const text = fs.readFileSync(PROMPT_FILE, 'utf8').trim();
    if (text) return text;
  } catch {
    /* 没有外部指令就用兜底 */
  }
  return FALLBACK_PROMPT;
}

async function main() {
  let payload = '';
  try {
    payload = fs.readFileSync(0, 'utf8');
  } catch {
    payload = '';
  }
  log(`Stop 触发 len=${payload.length} head=${payload.slice(0, 400).replace(/\s+/g, ' ')}`);

  if (!fs.existsSync(ENABLE_FLAG)) {
    log(`探针模式：${ENABLE_FLAG} 不存在，不干预`);
    return;
  }

  const state = readState();
  const now = Date.now();
  if (now - Number(state.lastAt || 0) < MIN_INTERVAL_MS) {
    log('节流：距上次续派不足 5 秒');
    return;
  }
  if (Number(state.count || 0) >= MAX_CONTINUATIONS) {
    log(`已达续派上限 ${MAX_CONTINUATIONS}，放行`);
    return;
  }

  let remaining = null;
  try {
    remaining = await quotaRemaining();
  } catch (err) {
    log(`读额度失败（${err.message}）→ 放行，交给 GUI 守护兜底`);
    return;
  }
  if (remaining !== null && remaining <= STOP_REMAINING) {
    log(`额度烧完（剩余 ${remaining} ≤ ${STOP_REMAINING}）→ 放行`);
    return;
  }

  const reason = pickPrompt();
  writeState({ count: Number(state.count || 0) + 1, lastAt: now });
  process.stdout.write(JSON.stringify({ decision: 'block', reason }));
  log(`已续派 #${Number(state.count || 0) + 1}（剩余 ${remaining}）`);
}

main().catch((err) => log(`钩子异常（已放行）：${err?.stack || err}`));
