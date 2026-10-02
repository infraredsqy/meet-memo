import {
  actionBodySchema,
  actionOkSchema,
  postOkSchema,
  settingsUpdateOkSchema,
  summarizeOkSchema,
  workspaceOkSchema,
  type ActionArgs,
  type ActionDetail,
  type ActionOk,
  type ErrorCode,
  type PostOk,
  type SettingsUpdateOk,
  type SummarizeOk,
  type WorkspaceOk,
} from '../contract';

/**
 * `POST /api/actions`（`docs/contract.md` 第 4 章）。
 *
 * **两个必需请求头**，缺任一条服务回 403：
 *
 * ```
 * Content-Type: application/json
 * X-Meeting-Service: 1
 * ```
 *
 * 这道校验在中间件里、先于路由匹配生效。403 的 body 是一行**纯文本**，
 * 走的是中间件自己那条出口 —— 请求压根没走到端点，因此**第 4.2 节的失败信封在这里用不上**。
 * 界面的 403 处理要与失败块分开（第 6 章第 4 条）。
 */

export const ACTIONS_URL = '/api/actions';
export const REQUIRED_HEADERS: Readonly<Record<string, string>> = {
  'Content-Type': 'application/json',
  'X-Meeting-Service': '1',
};

/** 服务侧把请求挡在了中间件那一道：没走到端点，因此没有失败信封。 */
export class ActionForbiddenError extends Error {
  readonly status: number;
  readonly text: string;

  constructor(status: number, text: string) {
    super(`动作请求被中间件拦下（${status}，缺少必需请求头）：${text}`);
    this.name = 'ActionForbiddenError';
    this.status = status;
    this.text = text;
  }
}

/** 服务回了失败信封：`{ok: false, code, message, detail}`。 */
export class ActionFailedError extends Error {
  readonly code: ErrorCode;
  readonly detail: ActionDetail;
  readonly status: number;

  constructor(status: number, code: ErrorCode, message: string, detail: ActionDetail) {
    super(message);
    this.name = 'ActionFailedError';
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

/** 应答体不符合契约（既不是成功体也不是失败信封）。 */
export class ActionProtocolError extends Error {
  readonly status: number;
  readonly raw: string;

  constructor(status: number, raw: string, cause: unknown) {
    const reason = cause instanceof Error ? cause.message : String(cause);
    super(`动作应答形状不符合契约（HTTP ${status}）：${reason}`);
    this.name = 'ActionProtocolError';
    this.status = status;
    this.raw = raw;
  }
}

/**
 * 动作结果：成功给 `revision`（`settings_update` 另给两个数组、`summarize` 另给一个
 * `summary` 子对象、会后那两个按钮另给一个 `post` 子对象），失败按 `code` 分派。
 */
export type ActionResult =
  | ({ kind: 'ok' } & ActionOk)
  | ({ kind: 'settings_update' } & SettingsUpdateOk)
  | ({ kind: 'summarize' } & SummarizeOk)
  | ({ kind: 'post' } & PostOk)
  | ({ kind: 'workspace' } & WorkspaceOk);

export interface PostActionOptions {
  /** 覆盖 fetch（测试注入用）。 */
  fetchImpl?: typeof fetch;
  /** 覆盖动作端点（测试注入用）。 */
  url?: string;
}

function parseEnvelope(status: number, text: string): ActionResult {
  let json: unknown;
  try {
    json = JSON.parse(text);
  } catch (cause) {
    throw new ActionProtocolError(status, text, cause);
  }

  const parsed = actionBodySchema.safeParse(json);
  if (!parsed.success) {
    throw new ActionProtocolError(status, text, parsed.error);
  }

  const body = parsed.data;
  if (body.ok) {
    // 三份「多几个键」的成功体先认：`summarize` 多一个 `summary`、`settings_update`
    // 多两个数组、`workspace_open` / `workspace_remove` 多一个 `workspace`。四条分支
    // 各自的 schema 都是 `.strict()`，因此认错顺序也不会误收
    // （带 `summary` 的体过不了另几条，反之亦然）。
    const summary = summarizeOkSchema.safeParse(body);
    if (summary.success) {
      return { kind: 'summarize', ...summary.data };
    }
    const workspace = workspaceOkSchema.safeParse(body);
    if (workspace.success) {
      return { kind: 'workspace', ...workspace.data };
    }
    const post = postOkSchema.safeParse(body);
    if (post.success) {
      return { kind: 'post', ...post.data };
    }
    const settings = settingsUpdateOkSchema.safeParse(body);
    if (settings.success) {
      return { kind: 'settings_update', ...settings.data };
    }
    const plain = actionOkSchema.parse(body);
    return { kind: 'ok', ...plain };
  }
  throw new ActionFailedError(status, body.code, body.message, body.detail);
}

/**
 * 打一个动作。成功返 `{kind:'ok', ok, revision}`（`settings_update` 另带
 * `applied` / `pending_restart`，`summarize` 另带 `summary`，会后那两个按钮另带 `post`，
 * `workspace_open` / `workspace_remove` 另带 `workspace`），失败抛 `ActionFailedError`
 * （带 `code` / `message` / `detail`）。
 */
export async function postAction(
  name: string,
  args: ActionArgs = {},
  options: PostActionOptions = {},
): Promise<ActionResult> {
  const doFetch = options.fetchImpl ?? fetch;
  const response = await doFetch(options.url ?? ACTIONS_URL, {
    method: 'POST',
    headers: { ...REQUIRED_HEADERS },
    body: JSON.stringify({ action: name, args }),
  });

  const text = await response.text();

  if (response.status === 403) {
    throw new ActionForbiddenError(403, text);
  }

  return parseEnvelope(response.status, text);
}
