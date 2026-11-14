import { postAction } from '../api';
import { failureFrom, type ActionFailureView } from '../failure';

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
