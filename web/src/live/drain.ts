import type { Snapshot } from '../contract';

export function drainFailedSteps(snapshot: Snapshot | null): string[] {
  const steps = snapshot?.ended?.steps ?? [];
  return steps
    .filter((step) => step.status === 'failed')
    .map((step) => `收尾第 ${step.id} 步失败：${step.error ?? step.detail}`);
}
