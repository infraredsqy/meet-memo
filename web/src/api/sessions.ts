import {
  HEARTBEAT_INTERVAL_DEFAULT_SECONDS,
  dirCreateOkSchema,
  dirsResponseSchema,
  sessionStatusSchema,
  sessionsResponseSchema,
  type DirCreateOk,
  type DirKind,
  type DirsResponse,
  type SessionsResponse,
  type SessionStatus,
} from '../contract/index';
import { clampHeartbeatInterval } from './heartbeat';

/**
 * 工作区注册表、目录选择器与单场状态的读端点（`docs/contract.md` 4.8 / 4.9），
 * 以及选择器那只新建目录的写入口（`POST /api/dirs`）。
 *
 * 读端点**不带那两个必需头**（三道防线是给「改状态」的请求设的），也不写任何东西：
 * 读它们不涨 `revision`、不广播。界面拿到的就是服务给的原文，不派生、不缓存、
 * 不落本地存储（注册表只能住服务端，前端只读）。写入口按头哨规矩带
 * `X-Meeting-Service: 1`；它同样不进四态快照。
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
  /** `?files=1`（资料界面的选择器文件模式要的，160d1a78）：应答体多一个 `files` 键。 */
  withFiles?: boolean;
}

/**
 * 取一层目录；起点（`dir` 省略时）就是用户主目录。
 * `dir` 不存在时服务回 400 —— 这里照契约如实拒（`DirsHttpError`，信封 `message`
 * 原话在 `.message` 上），不编一个空层。
 */
/**
 * `GET /api/dirs` 的 HTTP 拒答（400：要浏览的目录不存在 / 读不动）。信封读得出时
 * `message` 就是信封 `message` 的原话（「要浏览的目录不存在：…」）——地址栏跳层
 * 失败要当场说清人话（57ab5255），协议话术不顶到选择器上。
 */
export class DirsHttpError extends Error {
  readonly status: number;
  readonly code: string | null;
  /** 失败信封的 `message` 原话；读不出信封就是 `null`。 */
  readonly note: string | null;

  constructor(status: number, code: string | null, note: string | null, raw: string) {
    super(note ?? (code !== null ? `目录浏览被拒：HTTP ${status} · ${code}` : `目录浏览被拒：HTTP ${status}`));
    this.name = 'DirsHttpError';
    this.status = status;
    this.code = code;
    this.note = note;
    this.raw = raw;
  }

  readonly raw: string;
}

/** 4xx/5xx 信封读法：`code` / `message` 读得出就带走，整份信封读不出就都 `null`。 */
function dirsHttpError(status: number, text: string): DirsHttpError {
  let code: string | null = null;
  let note: string | null = null;
  try {
    const body = JSON.parse(text) as { code?: unknown; message?: unknown };
    if (typeof body.code === 'string') code = body.code;
    if (typeof body.message === 'string' && body.message !== '') note = body.message;
  } catch {
    // 非 JSON：两格都留在 null，message 走固定话术
  }
  return new DirsHttpError(status, code, note, text);
}

export async function browseDirs(
  dir: string | null = null,
  options: BrowseDirsOptions = {},
): Promise<DirsResponse> {
  const doFetch = options.fetchImpl ?? fetch;
  // `?files=1` 只在文件模式那档带上（`BrowseDirsOptions.withFiles`）：老路径的请求
  // 串一个字节不动，服务也就照旧不摆 `files` 键。
  const filesParam = options.withFiles === true ? 'files=1' : null;
  const query = [dir === null ? null : `dir=${encodeURIComponent(dir)}`, filesParam]
    .filter((part): part is string => part !== null)
    .join('&');
  const url = query === '' ? DIRS_URL : `${DIRS_URL}?${query}`;
  let response: Response;
  try {
    response = await doFetch(url, { method: 'GET' });
  } catch (cause) {
    throw new SessionsUnavailableError('目录浏览', cause);
  }
  const text = await response.text();
  if (!response.ok) {
    throw dirsHttpError(response.status, text);
  }
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

// ---------------------------------------------------------------------------
// 新建目录（`POST /api/dirs`）：选择器里就地建一个，建完直接选中
// ---------------------------------------------------------------------------

/** `POST /api/dirs` 的 HTTP 拒答（400 非法名 / 409 同名已存在 / 500 盘上出岔等）。 */
export class DirCreateHttpError extends Error {
  readonly status: number;
  readonly code: string | null;
  /** 失败信封的 `message` 原话；读不出信封就是 `null`（弹窗里画固定话术）。 */
  readonly note: string | null;

  constructor(status: number, code: string | null, note: string | null, raw: string) {
    super(
      note ??
        (code !== null
          ? `新建目录被拒：HTTP ${status} · ${code}`
          : `新建目录被拒：HTTP ${status}`),
    );
    this.name = 'DirCreateHttpError';
    this.status = status;
    this.code = code;
    this.note = note;
    this.raw = raw;
  }

  readonly raw: string;
}

export interface CreateDirOptions {
  fetchImpl?: typeof fetch;
}

/**
 * 在 `parent` 那一层里新建一个名字叫 `name` 的目录（服务不清洗不改写：名字必须
 * 用户亲手起，非法的服务给一句人话拒回来）。写端点头哨 `X-Meeting-Service: 1`
 * 由这里带；不涨 `revision`（选择器的现场独立于四态快照）。
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
    let code: string | null = null;
    let note: string | null = null;
    try {
      const body = JSON.parse(text) as { code?: unknown; message?: unknown };
      if (typeof body.code === 'string') code = body.code;
      if (typeof body.message === 'string' && body.message !== '') note = body.message;
    } catch {
      // 非 JSON：两格都留在 null，message 走固定话术
    }
    throw new DirCreateHttpError(response.status, code, note, text);
  }
  const raw = parseOk('新建目录')(response, text);
  const parsed = dirCreateOkSchema.safeParse(raw);
  if (!parsed.success) {
    throw new SessionsProtocolError('新建目录', response.status, text, parsed.error);
  }
  return parsed.data;
}

// ---------------------------------------------------------------------------
// 单场状态（4.9）：进度区的取数与轮询（会后进度界面节点 52b24392）
// ---------------------------------------------------------------------------

/** 单场状态那一档服务回的错误（404：目录不在原地 / 不在清单里；失败体是通用信封）。 */
export class SessionStatusHttpError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(status: number, code: string | null, raw: string) {
    super(
      code !== null
        ? `取不到单场状态：HTTP ${status} · ${code}`
        : `取不到单场状态：HTTP ${status}`,
    );
    this.name = 'SessionStatusHttpError';
    this.status = status;
    this.code = code;
    this.raw = raw;
  }

  readonly raw: string;
}

export interface FetchSessionStatusOptions {
  fetchImpl?: typeof fetch;
}

/**
 * 取一份单场状态（`{id}` 是这一场的目录路径，URL 编码后原样指到这一场）。
 *
 * 失败分两档：网络层失败 / 形状不对照 `fetchSessions` 同一套
 * （`SessionsUnavailableError` / `SessionsProtocolError`）；HTTP 非 200（4.9 的
 * 两档 404）回 `SessionStatusHttpError`，`code` 从失败信封里读、读不出信封就是 `null`
 * —— 界面按「有没有 `code`」分两句写，不按 `message` 做字符串匹配（契约 6.7）。
 */
export async function fetchSessionStatus(
  dir: string,
  options: FetchSessionStatusOptions = {},
): Promise<SessionStatus> {
  const doFetch = options.fetchImpl ?? fetch;
  const url = `${SESSIONS_URL}/${encodeURIComponent(dir)}`;
  let response: Response;
  try {
    response = await doFetch(url, { method: 'GET' });
  } catch (cause) {
    throw new SessionsUnavailableError('单场状态', cause);
  }
  const text = await response.text();
  if (!response.ok) {
    let code: string | null = null;
    try {
      const body = JSON.parse(text) as { code?: unknown };
      if (typeof body?.code === 'string') code = body.code;
    } catch {
      // 失败体读不出信封：code 保持 null，只报 HTTP 状态。
    }
    throw new SessionStatusHttpError(response.status, code, text);
  }
  const raw = parseOk('单场状态')(response, text);
  const parsed = sessionStatusSchema.safeParse(raw);
  if (!parsed.success) {
    throw new SessionsProtocolError('单场状态', response.status, text, parsed.error);
  }
  return parsed.data;
}

/** 进度区轮询一次的读数（成功那一跳带 `status`，失败那一跳带 `error`）。 */
export type SessionStatusTick =
  | { kind: 'status'; dir: string; status: SessionStatus }
  | { kind: 'error'; dir: string; error: Error };

export interface SessionStatusPollerOptions {
  fetchImpl?: typeof fetch;
}

/**
 * 按心跳的样板自排轮询单场状态（`api/heartbeat.ts` 的同款骨架：`setTimeout`
 * 自我重排 —— 周期改了即时生效，不必等当前这一轮走完）。
 *
 * **它只管「问一次、交一次」**：什么时候开、什么时候停由组件按读数决定
 * （有活动才轮询，不空转）。周期随快照的 `settings.heartbeat_interval_seconds` 走
 * （与心跳同一个可配节奏，clamp 1–60）。
 */
export interface SessionStatusPoller {
  /** 用快照里的 `settings.heartbeat_interval_seconds` 设周期（clamp 1–60 秒）。 */
  setIntervalFromSnapshot(seconds: number): void;
  /** 当前周期（秒）。 */
  intervalSeconds(): number;
  /** 立刻问一次（不经 `start()` 的排程），读数交给回调。 */
  tick(dir: string): Promise<SessionStatusTick>;
  /** 开始轮询这一场；已在轮询时先换目标再继续。 */
  start(dir: string): void;
  /** 停止轮询。 */
  stop(): void;
  /** 是否在轮询。 */
  running(): boolean;
}

export function createSessionStatusPoller(
  onTick: (tick: SessionStatusTick) => void,
  options: SessionStatusPollerOptions = {},
): SessionStatusPoller {
  const doFetch = options.fetchImpl ?? fetch;
  let interval = HEARTBEAT_INTERVAL_DEFAULT_SECONDS;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let stopped = true;
  let target: string | null = null;

  const schedule = (): void => {
    if (stopped || target === null) return;
    timer = setTimeout(() => {
      // 与心跳同款：tick() 是一次性的，排下一次是这里的活儿。
      void tick(target as string).then((result) => {
        onTick(result);
        schedule();
      });
    }, interval * 1000);
  };

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

  return {
    setIntervalFromSnapshot(seconds: number): void {
      const next = clampHeartbeatInterval(seconds);
      if (next === interval) return;
      interval = next;
      // 周期变了就重排下一次（心跳同款）。
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
        schedule();
      }
    },

    intervalSeconds(): number {
      return interval;
    },

    async tick(dir: string): Promise<SessionStatusTick> {
      const result = await tick(dir);
      onTick(result);
      return result;
    },

    start(dir: string): void {
      target = dir;
      if (!stopped) return;
      stopped = false;
      schedule();
    },

    stop(): void {
      stopped = true;
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    },

    running(): boolean {
      return !stopped;
    },
  };
}
