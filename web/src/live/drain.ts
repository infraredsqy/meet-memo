import type { Snapshot } from '../contract';
import type { ActionFailureView } from '../failure';

/**
 * 收尾失败那一档的读法（web-drain-view-logic-in-app：从 `App.tsx` 搬出来 ——
 * 「怎么读服务的收尾状态」与「怎么画」是两件事，且不带 JSX 才进得了用例）。
 *
 * 两个来路：动作失败（用户按了 stop 而收尾报错）与服务侧自己的收尾失败
 * （`ended.error` 有值、或某一步 `status = failed`）。
 */

/** 收尾里 `status = 'failed'` 的那几步各摊一行。 */
export function drainFailedSteps(snapshot: Snapshot | null): string[] {
  const steps = snapshot?.ended?.steps ?? [];
  return steps
    .filter((step) => step.status === 'failed')
    .map((step) => `收尾第 ${step.id} 步失败：${step.error ?? step.detail}`);
}

/**
 * 收尾失败那一档要画的那块明细。
 *
 * 这一档**没有任何动作**可怪的（服务自己收尾出了事）：不挂在失败信封那几类
 * `ErrorCode` 上（那几档讲的是「你刚才这一下没成」），走 `drain` 那一档，
 * 无 `code` —— 明细摊出来与旧实现逐条一致。
 */
export function drainFailure(
  failure: ActionFailureView | null,
  snapshot: Snapshot | null,
  failedSteps: string[],
): ActionFailureView | null {
  if (failure !== null) return failure;
  if (snapshot === null) return null;
  const ended = snapshot.ended;
  if (ended === undefined || ended === null) return null;
  const detail = [...(ended.error === null ? [] : [`收尾失败：${ended.error}`]), ...failedSteps];
  if (detail.length === 0) return null;
  return { kind: 'drain', message: '收尾没走完', detail, fields: {} };
}
