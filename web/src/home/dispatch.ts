import { postAction, type ActionResult } from '../api';
import { failureFrom, type ActionFailureView } from '../failure';

/**
 * 动作派发的公共骨架（web-actions-dispatch-skeleton-duplicated）：清失败块 →
 * （before）→ 起忙 → 打动作（失败画失败块）→ 收忙 → 收尾（重取列表 / 重问读数）。
 * `onReject` 接走的那一拍（如打断重试）与 `rethrow` 抛出都不落尾段——忙闩保持、
 * 不画失败块、不重取，收尾只落在成功或没被接走的失败上。
 *
 * 收编档的 `confirmWorkspaceOpen` 是同一副链的先例；WorkspaceHome / WorkspaceRail
 * 的八处调用方（stopPost / transcodeAct / startNow / startAfterInterrupt /
 * postSelected / renameSpeaker / openDirNow / 移除确认）各带自己的 before /
 * onOk / settle，链的次序与原先逐处一致。
 */
/** 尾段的跳过哨兵：`onReject` 接走的那一拍自己善后，尾段不收忙、不收尾。 */
const TAIL_SKIP = Symbol('runAction-tail-skip');

export function runAction(io: {
  post?: typeof postAction;
  name: string;
  args?: Record<string, unknown>;
  /** 失败画到哪（setFailure / onFailure）；派发前清失败块也走它（传 `null`）。 */
  onFail: (failure: ActionFailureView | null) => void;
  /** 忙的收放（调用方自己的 setBusy / setRailBusy）；不占忙的调用方省略。 */
  setBusy?: (busy: boolean) => void;
  /** 派发前的那一下（清回执句等）；排在清失败块之后、起忙之前。 */
  before?: () => void;
  /** 成功那一跳的读法（回执句在这里拼）。 */
  onOk?: (result: ActionResult) => void;
  /**
   * 失败先问这一步：回 `true` = 这一拍自己善后（如重试），不画失败块、不收忙不收尾；
   * 回 `false`（或省略）照常画失败块、收忙、收尾。
   */
  onReject?: (failure: ActionFailureView) => boolean;
  /** 失败要不要照原样抛回去（弹窗那一侧要据此收回自己的忙）；抛出时同样不收尾。 */
  rethrow?: boolean;
  /** 落定（成功或没被接走的失败）之后的收尾：重取列表、重问读数。 */
  settle?: () => void;
}): Promise<void> {
  io.onFail(null);
  io.before?.();
  io.setBusy?.(true);
  return (io.post ?? postAction)(io.name, io.args ?? {}).then(
    (result) => {
      io.onOk?.(result);
    },
    (error) => {
      const failure = failureFrom(error);
      if (io.onReject?.(failure) === true) return TAIL_SKIP;
      io.onFail(failure);
      if (io.rethrow) throw error;
    },
  ).then((fallthrough) => {
    if (fallthrough === TAIL_SKIP) return;
    io.setBusy?.(false);
    io.settle?.();
  });
}
