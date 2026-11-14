import type { Snapshot } from '../contract';
import { stepsSettled } from './drain';

/**
 * 两档回工作台：
 *
 *   - `idle` 且没有场次：还没开过会的那种空闲；
 *   - `ended`——**任意相都回**：停止录音那一刻就能回工作台，哪怕落盘还在跑
 *     （结束录音不弹窗、工作区回到「当前没有进行中的会议」；
 *     「已结束·收尾中」只是详情里那一小块落盘进度，不构成留在会中的理由）。
 *
 * 会中那一屏只剩 `recording` / `alert`（以及判不出场的空闲带场次这一档兜底）。
 */
export function showsWorkspaceHome(snapshot: Snapshot): boolean {
  if (snapshot.top_state === 'idle' && snapshot.session === null) return true;
  return snapshot.top_state === 'ended';
}

export function drainSettled(snapshot: Snapshot): boolean {
  const ended = snapshot.ended;
  if (ended === null) return false;
  return stepsSettled(ended.steps);
}
