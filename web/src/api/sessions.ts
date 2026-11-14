import {
  HEARTBEAT_INTERVAL_DEFAULT_SECONDS,
  dirCreateOkSchema,
  dirsResponseSchema,
  sessionStatusSchema,
  sessionTranscriptsSchema,
  sessionsResponseSchema,
  type DirCreateOk,
  type DirKind,
  type DirsResponse,
  type SessionStatus,
  type SessionTranscripts,
  type SessionsResponse,
} from '../contract/index';
import { clampHeartbeatInterval } from './heartbeat';
import {
  HttpRejectError,
  ProtocolError,
  UnavailableError,
  getJsonOk,
  readFailureEnvelope,
} from './common';
import { createPollerLoop } from './polling';

/**
 * 工作区注册表、目录选择器与单场状态的读端点，以及选择器那只新建目录的写入口。
 *
 * 读端点**不带那两个必需头**（三道防线是给「改状态」的请求设的），也不写任何东西：不涨
 * `revision`、不广播。写入口照头哨规矩带 `X-Meeting-Service: 1`。
 */

export const SESSIONS_URL = '/api/sessions';
export const DIRS_URL = '/api/dirs';

export class SessionsProtocolError extends ProtocolError {
  constructor(what: string, status: number, raw: string, cause: unknown) {
    const reason = cause instanceof Error ? cause.message : String(cause);
    super(`${what}的应答形状不符合契约（HTTP ${status}）：${reason}`, status, raw);
    this.name = 'SessionsProtocolError';
  }
}

export class SessionsUnavailableError extends UnavailableError {
  constructor(what: string, cause: unknown) {
    super(`取不到${what}：${cause instanceof Error ? cause.message : String(cause)}`);
    this.name = 'SessionsUnavailableError';
  }
}

export interface FetchSessionsOptions {
  fetchImpl?: typeof fetch;
  url?: string;
}

export async function fetchSessions(
  options: FetchSessionsOptions = {},
): Promise<SessionsResponse> {
  return getJsonOk({
    url: options.url ?? SESSIONS_URL,
    schema: sessionsResponseSchema,
    doFetch: options.fetchImpl ?? fetch,
    unavailable: (cause) => new SessionsUnavailableError('工作区列表', cause),
    protocol: (status, text, cause) => new SessionsProtocolError('工作区列表', status, text, cause),
  });
}

export interface BrowseDirsOptions {
  fetchImpl?: typeof fetch;
  url?: string;
  /** `?files=1`：应答体多一个 `files` 键。 */
  withFiles?: boolean;
}

/**
 * `GET /api/dirs` 的 HTTP 拒答（400：要浏览的目录不存在 / 读不动）。信封读得出时
 * `message` 就是信封 `message` 的原话 —— 地址栏跳层失败要当场说清人话。
 */
export class DirsHttpError extends HttpRejectError {
  constructor(status: number, code: string | null, note: string | null, raw: string) {
    super(
      note ?? (code !== null ? `目录浏览被拒：HTTP ${status} · ${code}` : `目录浏览被拒：HTTP ${status}`),
      status,
      code,
      note,
      raw,
);
    this.name = 'DirsHttpError';
  }
}

function dirsHttpError(status: number, text: string): DirsHttpError {
  const { code, note } = readFailureEnvelope(text);
  return new DirsHttpError(status, code, note, text);
}

export async function browseDirs(
  dir: string | null = null,
  options: BrowseDirsOptions = {},
): Promise<DirsResponse> {
  // `?files=1` 只在文件模式那档带上：老路径的请求串一个字节不动。
  const filesParam = options.withFiles === true ? 'files=1' : null;
  const query = [dir === null ? null : `dir=${encodeURIComponent(dir)}`, filesParam]
    .filter((part): part is string => part !== null)
    .join('&');
  const url = query === '' ? DIRS_URL : `${DIRS_URL}?${query}`;
  return getJsonOk({
    url,
    schema: dirsResponseSchema,
    doFetch: options.fetchImpl ?? fetch,
    unavailable: (cause) => new SessionsUnavailableError('目录浏览', cause),
    protocol: (status, text, cause) => new SessionsProtocolError('目录浏览', status, text, cause),
    httpError: (status, text) => dirsHttpError(status, text),
  });
}

export type ProbeDecision = 'empty' | 'meeting' | 'need-confirm';

/** 名目 → 三档处理的判据（这是唯一的出处）。`missing` 到不了判断层。 */
export function probeDecision(kind: DirKind): ProbeDecision {
  if (kind === 'empty') return 'empty';
  if (kind === 'meeting') return 'meeting';
  return 'need-confirm';
}

/** `POST /api/dirs` 的 HTTP 拒答（400 非法名 / 409 同名已存在 / 500 盘上出岔等）。 */
export class DirCreateHttpError extends HttpRejectError {
  constructor(status: number, code: string | null, note: string | null, raw: string) {
    super(
      note ??
        (code !== null
          ? `新建目录被拒：HTTP ${status} · ${code}`
          : `新建目录被拒：HTTP ${status}`),
      status,
      code,
      note,
      raw,
);
    this.name = 'DirCreateHttpError';
  }
}

export interface CreateDirOptions {
  fetchImpl?: typeof fetch;
}

/**
 * 在 `parent` 那一层里新建一个名字叫 `name` 的目录（服务不清洗不改写：名字必须用户亲手起）。
 * 写端点头哨由这里带；不涨 `revision`。
 */
export async function createDir(
  parent: string | null,
  name: string,
  options: CreateDirOptions = {},
): Promise<DirCreateOk> {
  const doFetch = options.fetchImpl ?? fetch;
  const query = [
    parent === null ? null : `dir=${encodeURIComponent(parent)}`,
    `name=${encodeURIComponent(name)}`,
  ]
    .filter((part): part is string => part !== null)
    .join('&');
  let response: Response;
  try {
    response = await doFetch(`${DIRS_URL}?${query}`, {
      method: 'POST',
      headers: { 'X-Meeting-Service': '1' },
    });
  } catch (cause) {
    // 连接层失败：不走 SessionsUnavailableError（那句固定话术是「取不到…」，读端点的口吻）
    throw new Error(
      `新建目录发不出去：${cause instanceof Error ? cause.message : String(cause)}`,
);
  }
  const text = await response.text();
  if (!response.ok) {
    const { code, note } = readFailureEnvelope(text);
    throw new DirCreateHttpError(response.status, code, note, text);
  }
  let json: unknown;
  try {
    json = JSON.parse(text);
  } catch (cause) {
    throw new SessionsProtocolError('新建目录', response.status, text, cause);
  }
  const parsed = dirCreateOkSchema.safeParse(json);
  if (!parsed.success) {
    throw new SessionsProtocolError('新建目录', response.status, text, parsed.error);
  }
  return parsed.data;
}

/** 单场状态那一档服务回的错误（404 两档；失败体是通用信封）。 */
export class SessionStatusHttpError extends HttpRejectError {
  constructor(status: number, code: string | null, raw: string) {
    super(
      code !== null
        ? `取不到单场状态：HTTP ${status} · ${code}`
        : `取不到单场状态：HTTP ${status}`,
      status,
      code,
      null,
      raw,
);
    this.name = 'SessionStatusHttpError';
  }
}

export interface FetchSessionStatusOptions {
  fetchImpl?: typeof fetch;
}

/**
 * 取一份单场状态（`{id}` 是这一场的目录路径，URL 编码后原样指到这一场）。
 *
 * HTTP 非 200 回 `SessionStatusHttpError`，`code` 从失败信封里读、读不出就是 `null` ——
 * 界面按「有没有 `code`」分两句写，**不按 `message` 做字符串匹配**。
 */
export async function fetchSessionStatus(
  dir: string,
  options: FetchSessionStatusOptions = {},
): Promise<SessionStatus> {
  return getJsonOk({
    url: `${SESSIONS_URL}/${encodeURIComponent(dir)}`,
    schema: sessionStatusSchema,
    doFetch: options.fetchImpl ?? fetch,
    unavailable: (cause) => new SessionsUnavailableError('单场状态', cause),
    protocol: (status, text, cause) => new SessionsProtocolError('单场状态', status, text, cause),
    httpError: (status, text) => {
      const { code } = readFailureEnvelope(text);
      return new SessionStatusHttpError(status, code, text);
    },
  });
}

export type SessionStatusTick =
  | { kind: 'status'; dir: string; status: SessionStatus }
  | { kind: 'error'; dir: string; error: Error };

/** 稿子正文那一档服务回的错误（404 两档；失败体是通用信封）。 */
export class SessionTranscriptsHttpError extends HttpRejectError {
  constructor(status: number, code: string | null, raw: string) {
    super(
      code !== null
        ? `取不到这一场的稿子：HTTP ${status} · ${code}`
        : `取不到这一场的稿子：HTTP ${status}`,
      status,
      code,
      null,
      raw,
);
    this.name = 'SessionTranscriptsHttpError';
  }
}

export interface FetchSessionTranscriptsOptions {
  fetchImpl?: typeof fetch;
}

/**
 * 取一份这一场的稿子正文（`{id}` 是这一场的目录路径，URL 编码后原样指到这一场）。
 *
 * HTTP 非 200 回 `SessionTranscriptsHttpError`，`code` 从失败信封里读、读不出就是 `null` ——
 * 界面按「有没有 `code`」分两句写。
 */
export async function fetchSessionTranscripts(
  dir: string,
  options: FetchSessionTranscriptsOptions = {},
): Promise<SessionTranscripts> {
  return getJsonOk({
    url: `${SESSIONS_URL}/${encodeURIComponent(dir)}/transcripts`,
    schema: sessionTranscriptsSchema,
    doFetch: options.fetchImpl ?? fetch,
    unavailable: (cause) => new SessionsUnavailableError('这一场的稿子', cause),
    protocol: (status, text, cause) =>
      new SessionsProtocolError('这一场的稿子', status, text, cause),
    httpError: (status, text) => {
      const { code } = readFailureEnvelope(text);
      return new SessionTranscriptsHttpError(status, code, text);
    },
  });
}

export interface SessionStatusPollerOptions {
  fetchImpl?: typeof fetch;
}

/**
 * 按心跳的样板自排轮询单场状态（骨架在 `api/polling.ts`：`setTimeout` 自我重排 —— 周期改了
 * 即时生效，不必等当前这一轮走完）。
 *
 * **它只管「问一次、交一次」**：什么时候开、什么时候停由组件按读数决定（有活动才轮询，不空转）。
 * 周期随快照的 `settings.heartbeat_interval_seconds` 走（clamp 1–60）。
 */
export interface SessionStatusPoller {
  setIntervalFromSnapshot(seconds: number): void;
  intervalSeconds(): number;
  tick(dir: string): Promise<SessionStatusTick>;
  /** 开始轮询这一场；已在轮询时先换目标再继续。 */
  start(dir: string): void;
  stop(): void;
  running(): boolean;
}

export function createSessionStatusPoller(
  onTick: (tick: SessionStatusTick) => void,
  options: SessionStatusPollerOptions = {},
): SessionStatusPoller {
  const doFetch = options.fetchImpl ?? fetch;
  /** 正在轮询的那一场（骨架的 `ready` 闸按它放行）。 */
  let target: string | null = null;

  const tick = async (dir: string): Promise<SessionStatusTick> => {
    try {
      const status = await fetchSessionStatus(dir, { fetchImpl: doFetch });
      return { kind: 'status', dir, status };
    } catch (cause) {
      return {
        kind: 'error',
        dir,
        error: cause instanceof Error ? cause : new Error(String(cause)),
      };
    }
  };

  const loop = createPollerLoop({
    defaultSeconds: HEARTBEAT_INTERVAL_DEFAULT_SECONDS,
    clamp: clampHeartbeatInterval,
    // 有目标才排：没 start 过（或还没指到哪一场）时不空转。
    ready: () => target !== null,
    beat: async () => {
      const result = await tick(target as string);
      onTick(result);
    },
  });

  return {
    setIntervalFromSnapshot(seconds: number): void {
      loop.setIntervalSeconds(seconds);
    },

    intervalSeconds(): number {
      return loop.intervalSeconds();
    },

    async tick(dir: string): Promise<SessionStatusTick> {
      const result = await tick(dir);
      onTick(result);
      return result;
    },

    start(dir: string): void {
      target = dir;
      loop.start();
    },

    stop(): void {
      loop.stop();
    },

    running(): boolean {
      return loop.running();
    },
  };
}
