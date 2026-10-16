import type { Snapshot } from '../contract';

/**
 * 收尾那几步的读法（web-drain-view-logic-in-app：从 `App.tsx` 搬出来 ——
 * 「怎么读服务的收尾状态」与「怎么画」是两件事，且不带 JSX 才进得了用例）。
 *
 * 「收尾走到哪」的正本读数在详情那一小块（`home/drain.ts`）；这里的行只进
 * 会中失败块的 `extraLines`，与其他来源的明细行按逐字等价线去重（`failure-ui.tsx`）。
 */

/** 收尾里 `status = 'failed'` 的那几步各摊一行。 */
export function drainFailedSteps(snapshot: Snapshot | null): string[] {
  const steps = snapshot?.ended?.steps ?? [];
  return steps
    .filter((step) => step.status === 'failed')
    .map((step) => `收尾第 ${step.id} 步失败：${step.error ?? step.detail}`);
}
