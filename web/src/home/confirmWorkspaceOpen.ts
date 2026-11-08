import { postAction } from '../api';
import { failureFrom, type ActionFailureView } from '../failure';

/**
 * 「把非空目录当工作区？→ 确认收编」那半边接线（从工作台首页 `home/WorkspaceHome/` 抽出来的一段
 * 纯 async，不带 JSX、免 DOM 可验；修复 C1：原先确认按钮经 `openDir('need-confirm')`
 * 回环把同一只弹窗重新摆出来，`confirm: true` 永远没发出去）。
 *
 * 确认后**直接**发 `POST /api/actions` 的 `workspace_open` 且带 `confirm: true`；
 * 成败照 `reloadAfterAction()` 口径刷一遍列表——失败另画失败块（`onFail`）。
 */
export function confirmWorkspaceOpen(
  dir: string,
  io: {
    post?: typeof postAction;
    onFail: (failure: ActionFailureView) => void;
    reload: () => void | Promise<void>;
  },
): Promise<void> {
  const post = io.post ?? postAction;
  return post('workspace_open', { dir, confirm: true })
    .then(
      () => undefined,
      (error) => io.onFail(failureFrom(error)),
    )
    .then(() => io.reload());
}
