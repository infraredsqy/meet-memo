import {
  actionBodySchema,
  actionOkSchema,
  postOkSchema,
  settingsUpdateOkSchema,
  speakerOkSchema,
  summarizeOkSchema,
  transcodeOkSchema,
  workspaceOkSchema,
  type ActionArgs,
  type ActionDetail,
  type ActionOk,
  type ErrorCode,
  type PostOk,
  type SettingsUpdateOk,
  type SpeakerOk,
  type SummarizeOk,
  type TranscodeOk,
  type WorkspaceOk,
} from '../contract';

/**
 * `POST /api/actions`。
 *
 * **两个必需请求头**，缺任一条服务回 403：
 *
 * ```
 * Content-Type: application/json
 * X-Meeting-Service: 1
 * ```
 *
 * 这道校验在中间件里、先于路由匹配生效。403 的 body 是一行**纯文本**，
 * 走的是中间件自己那条出口 —— 请求压根没走到端点，因此**失败信封在这里用不上**。
 * 界面的 403 处理要与失败块分开。
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

export type ActionResult =
  | ({ kind: 'ok' } & ActionOk)
  | ({ kind: 'settings_update' } & SettingsUpdateOk)
  | ({ kind: 'summarize' } & SummarizeOk)
  | ({ kind: 'post' } & PostOk)
  | ({ kind: 'transcode' } & TranscodeOk)
  | ({ kind: 'workspace' } & WorkspaceOk)
  | ({ kind: 'speaker' } & SpeakerOk);

export interface PostActionOptions {
  fetchImpl?: typeof fetch;
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
    // 各条分支的 schema 都是 `.strict()`，因此认错顺序也不会误收
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
    const transcode = transcodeOkSchema.safeParse(body);
    if (transcode.success) {
      return { kind: 'transcode', ...transcode.data };
    }
    const speaker = speakerOkSchema.safeParse(body);
    if (speaker.success) {
      return { kind: 'speaker', ...speaker.data };
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
