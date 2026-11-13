import type { Snapshot } from '../contract';
import { stepsSettled } from './drain';

/**
 * 「这一屏给谁」的**纯判据**（`home/HomeGate.tsx` 的那两个条件逐字搬到这里，单独可测）。
 *
 * 分成两份的理由：这两个条件要能在**不 import 会中那一屏**（`App.tsx`）的前提下被逐档验穿
 * —— `App.tsx` 那个模块要用 DOM，`web/tests` 平时跑在 node 环境（`vitest.config.ts`）。
 * 闸本身只做一件笨事：照这里的判据在两个视图之间选一个。
 *
 * 两档回工作台（`docs/requirements.md` R26 / R18； 起停下不锁屏）：
 *
 *   - `idle` 且没有场次：还没开过会的那种空闲；
 *   - `ended`——**任意相都回**：停止录音那一刻就能回工作台，哪怕落盘还在跑
 *     （设计意图第 10 条：结束录音不弹窗、工作区回到「当前没有进行中的会议」；
 *     「已结束·收尾中」只是详情里那一小块落盘进度，不构成留在会中的理由）。
 *
 * 会中那一屏只剩 `recording` / `alert`（以及判不出场的空闲带场次这一档兜底）。
 */
export function showsWorkspaceHome(snapshot: Snapshot): boolean {
  if (snapshot.top_state === 'idle' && snapshot.session === null) return true;
  return snapshot.top_state === 'ended';
}

/**
 * 收尾三步是否都落定（判据本体在 `drain.ts` 的 `stepsSettled()`：`waiting` 是
 * `post` 那一格的终态 —— 它停在那里等人按按钮）。
 *
 * 闸不再用它拦人（ended 一律回工作台），它现在的去处是**详情里那一小块落盘进度的
 * 两档判据**（ui-design 4.2 二）：没落定写「还差最后一块没进盘」，落定写「录音已落盘」。
 */
export function drainSettled(snapshot: Snapshot): boolean {
  const ended = snapshot.ended;
  if (ended === null) return false;
  return stepsSettled(ended.steps);
}
