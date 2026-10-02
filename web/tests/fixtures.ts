import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * golden 样本的读取工具。
 *
 * 路径**从本文件的位置往上找**，不去猜当前工作目录 —— 这样从仓库任意位置跑
 * `npx vitest run`、`npm test`、还是 IDE 里点单条用例，读到的都是同一批样本。
 */

/** `web/tests/fixtures.ts` → `web/tests` → `web` → 仓库根。 */
const here = dirname(fileURLToPath(import.meta.url));
export const FIXTURES_DIR = resolve(here, '..', '..', 'contract', 'fixtures');

export type FixtureGroup =
  | 'snapshot'
  | 'sse'
  | 'actions'
  | 'summaries'
  | 'sessions'
  | 'heartbeat'
  | 'devices';

/** 读一份样本的原文。 */
export function readFixtureText(group: FixtureGroup, name: string): string {
  return readFileSync(join(FIXTURES_DIR, group, `${name}.json`), 'utf8');
}

/**
 * 读一份样本并解析成 JSON。
 *
 * 返回值的顶层按键取 `unknown`：样本是**没被 schema 校验过**的盘上原文，用例通常拿它
 * 直接喂 `schema.parse()`。想按键取值的用例自己收窄（`as Record<string, unknown>`），
 * 这样「哪些地方按未校验的形状在读数」在用例里是显式的。
 */
export function readFixture(group: FixtureGroup, name: string): unknown {
  return JSON.parse(readFixtureText(group, name));
}

/** 列出某个目录里的全部样本名（去掉 `.json`）。 */
export function listFixtures(group: FixtureGroup): string[] {
  return readdirSync(join(FIXTURES_DIR, group))
    .filter((file) => file.endsWith('.json'))
    .map((file) => file.slice(0, -'.json'.length))
    .sort();
}

/** 五个快照样本的名字。 */
export const SNAPSHOT_FIXTURES = [
  'idle',
  'recording',
  'alert',
  'ended-done',
  'ended-draining',
] as const;

/** 五帧 SSE 样本的名字。 */
export const SSE_FIXTURES = [
  'first-snapshot',
  'state',
  'levels',
  'transcript',
  'resync-snapshot',
] as const;

/**
 * 十七个动作样本：**七个成功**（五个如上 + 工作台收编节点的 `workspace_open` /
 * `workspace_remove` 两份带 `workspace` 的）与**十个失败**（九个 `code` 各一 +
 * 未知动作名那一档，其中 `summary_failed` 有两个样本：端点缺键那一档与预算用尽那一档）。
 */
export const ACTION_FIXTURES = [
  'start-ok',
  'stop-ok',
  'mute-session-ok',
  'settings-update-ok',
  'summarize-ok',
  'workspace-open-ok',
  'workspace-remove-ok',
  'fail-already-recording',
  'fail-draining',
  'fail-capture-unavailable',
  'fail-not-recording',
  'fail-invalid-setting',
  'fail-invalid-setting-unknown-action',
  'fail-persist-failed',
  'fail-action-unavailable',
  'fail-transcript-unavailable',
  'fail-summary-failed-missing',
] as const;

/**
 * 会中总结那四份相关联的样本：三份动作（成功体与两档失败）与一份本场记录。
 *
 * 记录那一份不在 `actions/` 里，它是 `GET /api/summaries` 的应答 —— 读它用
 * `readFixture('summaries', 'after-one')`，`readFixtureText` / `listFixtures` 那两条
 * 工具的 `FixtureGroup` 已经认得这个目录。
 */
export const SUMMARY_FIXTURES = [
  'summarize-ok',
  'fail-transcript-unavailable',
  'fail-summary-failed-missing',
] as const;

/** 本场总结记录那一份样本的名字（`GET /api/summaries`）。 */
export const SUMMARIES_FIXTURES = ['after-one'] as const;

/** 两个心跳样本。 */
export const HEARTBEAT_FIXTURES = ['idle', 'recording'] as const;

/** 设备清单那一份样本（`GET /api/devices`，设置抽屉节点 `311f84f5`）。 */
export const DEVICES_FIXTURES = ['all'] as const;

/**
 * 工作区三份样本（4.8 / 4.9）：空注册表、登记三档目录之后的清单 + `recent`、
 * 单场状态（两份块账 + 状态文件落好之后的那一读）。
 */
export const SESSIONS_FIXTURES = ['empty', 'after-open', 'status-after-run'] as const;

