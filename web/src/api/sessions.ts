import {
  dirsResponseSchema,
  sessionsResponseSchema,
  type DirKind,
  type DirsResponse,
  type SessionsResponse,
  type SessionRow,
} from '../contract/index';

/**
 * 工作区注册表与目录选择器的两只读端点（`docs/contract.md` 4.8）。
 *
 * 与 `POST /api/actions` 那条不同，它们**不带那两个必需头**（三道防线是给「改状态」的
 * 请求设的），也不写任何东西：读它们不涨 `revision`、不广播。界面拿到的就是服务给的
 * 原文，不派生、不缓存、不落本地存储（注册表只能住服务端，前端只读）。
 */

export const SESSIONS_URL = '/api/sessions';
export const DIRS_URL = '/api/dirs';

/** 应答体形状不对。 */
export class SessionsProtocolError extends Error {
  readonly status: number;
  readonly raw: string;

  constructor(what: string, status: number, raw: string, cause: unknown) {
    const reason = cause instanceof Error ? cause.message : String(cause);
    super(`${what}的应答形状不符合契约（HTTP ${status}）：${reason}`);
    this.name = 'SessionsProtocolError';
    this.status = status;
    this.raw = raw;
  }
}

/** 取不到（网络层失败、服务没应答）。 */
export class SessionsUnavailableError extends Error {
  constructor(what: string, cause: unknown) {
    super(`取不到${what}：${cause instanceof Error ? cause.message : String(cause)}`);
    this.name = 'SessionsUnavailableError';
  }
}

/** 两只只读端点共用的取数骨架（照 `api/summaries.ts` 的先例）。 */
function parseOk(what: string): (response: Response, text: string) => unknown {
  return (response, text) => {
    if (!response.ok) {
      throw new SessionsProtocolError(what, response.status, text, `HTTP ${response.status}`);
    }
    let json: unknown;
    try {
      json = JSON.parse(text);
    } catch (cause) {
      throw new SessionsProtocolError(what, response.status, text, cause);
    }
    return json;
  };
}

export interface FetchSessionsOptions {
  fetchImpl?: typeof fetch;
  url?: string;
}

/** 取一份工作区注册表；形状不对/取不到分别抛 Protocol / Unavailable。 */
export async function fetchSessions(
  options: FetchSessionsOptions = {},
): Promise<SessionsResponse> {
  const doFetch = options.fetchImpl ?? fetch;
  let response: Response;
  try {
    response = await doFetch(options.url ?? SESSIONS_URL, { method: 'GET' });
  } catch (cause) {
    throw new SessionsUnavailableError('工作区列表', cause);
  }
  const text = await response.text();
  const raw = parseOk('工作区列表')(response, text);
  const parsed = sessionsResponseSchema.safeParse(raw);
  if (!parsed.success) {
    throw new SessionsProtocolError('工作区列表', response.status, text, parsed.error);
  }
  return parsed.data;
}

export interface BrowseDirsOptions {
  fetchImpl?: typeof fetch;
  url?: string;
}

/**
 * 取一层目录；起点（`dir` 省略时）就是用户主目录。
 * `dir` 不存在时服务回 400 —— 这里照契约如实抛 `SessionsProtocolError`，不编一个空层。
 */
export async function browseDirs(
  dir: string | null = null,
  options: BrowseDirsOptions = {},
): Promise<DirsResponse> {
  const doFetch = options.fetchImpl ?? fetch;
  const url = dir === null ? DIRS_URL : `${DIRS_URL}?dir=${encodeURIComponent(dir)}`;
  let response: Response;
  try {
    response = await doFetch(url, { method: 'GET' });
  } catch (cause) {
    throw new SessionsUnavailableError('目录浏览', cause);
  }
  const text = await response.text();
  const raw = parseOk('目录浏览')(response, text);
  const parsed = dirsResponseSchema.safeParse(raw);
  if (!parsed.success) {
    throw new SessionsProtocolError('目录浏览', response.status, text, parsed.error);
  }
  return parsed.data;
}

/** 目录判读三档：chooser/确认弹窗都按它走（`suspected` 照收编那一档处理）。 */
export type ProbeDecision = 'empty' | 'meeting' | 'need-confirm';

/**
 * 名目 → 三档处理的判据（spec 的三情形：空 / 会议材料 / 非空无材料；这是唯一的出处）。
 * `missing` 到不了判断层——选择器里只摆存在的目录、不存在的路径由 `workspace_open`
 * 回 400 再画失败块。
 */
export function probeDecision(kind: DirKind): ProbeDecision {
  if (kind === 'empty') return 'empty';
  if (kind === 'meeting') return 'meeting';
  return 'need-confirm';
}

/** 这一行的入口动作文案名字。（服务给的 `session_id` 是目录名或兜底名，直接用。） */
export function rowTitle(row: SessionRow): string {
  const stem = row.session_id ?? row.dir;
  return stem;
}
