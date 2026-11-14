import { postAction, type ActionResult } from '../api';
import { failureFrom, type ActionFailureView } from '../failure';

/**
 * `onReject` 接走的那一拍（如打断重试）与 `rethrow` 抛出都不落尾段——忙闩保持、
 * 不画失败块、不重取，收尾只落在成功或没被接走的失败上。
 */
const TAIL_SKIP = Symbol('runAction-tail-skip');

export function runAction(io: {
  post?: typeof postAction;
  name: string;
  args?: Record<string, unknown>;
  onFail: (failure: ActionFailureView | null) => void;
  setBusy?: (busy: boolean) => void;
  before?: () => void;
  onOk?: (result: ActionResult) => void;
  onReject?: (failure: ActionFailureView) => boolean;
  rethrow?: boolean;
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
