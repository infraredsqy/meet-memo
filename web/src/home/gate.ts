import type { Snapshot } from '../contract';

/**
 * 「这一屏给谁」的**纯判据**（`home/HomeGate.tsx` 的那两个条件逐字搬到这里，单独可测）。
 *
 * 分成两份的理由：这两个条件要能在**不 import 会中那一屏**（`App.tsx`）的前提下被逐档验穿
 * —— `App.tsx` 那个模块要用 DOM，`web/tests` 平时跑在 node 环境（`vitest.config.ts`）。
 * 闸本身只做一件笨事：照这里的判据在两个视图之间选一个。
 *
 * 两档回工作台（`docs/requirements.md` R26 / R18）：
 *
 *   - `idle` 且没有场次：还没开过会的那种空闲；
 *   - `ended` 且**收尾四步都落定**：按「结束录音」之后会后一个动作都不自动跑，
 *     工作区于是回到「当前没有进行中的会议」——「我这些会」那一行还在，
 *     右边是这一场的详情，会后那两个按钮住在那里。
 *
 * **不等 `ended.phase === 'done'`**：收尾里某一步失败时它停在 `draining`，
 * 而那一场的会后照样该由人按按钮起（这一行的状态由工作区列表自己说）。
 */
export function showsWorkspaceHome(snapshot: Snapshot): boolean {
  if (snapshot.top_state === 'idle' && snapshot.session === null) return true;
  return snapshot.top_state === 'ended' && drainSettled(snapshot);
}

/** 收尾四步是否都落定（`waiting` 是第 4 步 `post` 的终态：它停在那里等人按按钮）。 */
export function drainSettled(snapshot: Snapshot): boolean {
  const ended = snapshot.ended;
  if (ended === null) return false;
  if (ended.steps.some((step) => step.status === 'running')) return false;
  return ended.steps.every((step) => step.status !== 'waiting' || step.id === 'post');
}
