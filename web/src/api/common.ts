import type { ZodType } from 'zod';

/**
 * api 层的公共样板（web-api-fetch-boilerplate-duplicated）：六个 GET 端点共用的
 * 取数骨架、各端点错误类的公共基类、失败信封的一份读法。
 *
 * 收敛的是**样板**，不是口径：请求形状（GET、无头）、失败分档与错误话术与各端点
 * 原先逐字一致 —— 取不到 / 形状不对 / 端点自带的 HTTP 拒答三档照旧分得清。
 */

/** 「取不到」的公共基类（网络层失败、服务没应答）：各端点的 Unavailable 从它伸出。 */
export class UnavailableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'UnavailableError';
  }
}

/** 「应答体形状不对」的公共基类：`status` 与 `raw` 两格各端点共用。 */
export class ProtocolError extends Error {
  readonly status: number;
  readonly raw: string;

  constructor(message: string, status: number, raw: string) {
    super(message);
    this.name = 'ProtocolError';
    this.status = status;
    this.raw = raw;
  }
}

/**
 * 服务回了失败信封（或读不出信封的纯文本）的 HTTP 拒答基类：
 * `status` / `code` / `note` / `raw` 四格各端点共用（目录浏览、新建目录、
 * 单场状态、稿子正文、会议资料五类同构的那一半）。
 */
export class HttpRejectError extends Error {
  readonly status: number;
  /** 失败信封的 `code`；读不出信封就是 `null`。 */
  readonly code: string | null;
  /** 失败信封的 `message` 原话；读不出信封就是 `null`。 */
  readonly note: string | null;
  readonly raw: string;

  constructor(
    message: string,
    status: number,
    code: string | null,
    note: string | null,
    raw: string,
) {
    super(message);
    this.name = 'HttpRejectError';
    this.status = status;
    this.code = code;
    this.note = note;
    this.raw = raw;
  }
}

/** 4xx/5xx 失败信封的读法：`code` / `message` 读得出就带走，整份信封读不出就都 `null`。 */
export function readFailureEnvelope(text: string): { code: string | null; note: string | null } {
  let code: string | null = null;
  let note: string | null = null;
  try {
    const body = JSON.parse(text) as { code?: unknown; message?: unknown };
    if (typeof body.code === 'string') code = body.code;
    if (typeof body.message === 'string' && body.message !== '') note = body.message;
  } catch {
    // 非 JSON：两格都留在 null，message 走固定话术
  }
  return { code, note };
}

/** 一条 GET 端点的取数接线（`getJsonOk` 的入参）。 */
export interface GetJsonIo<T> {
  url: string;
  /** 应答体的契约 schema（`safeParse` 过了才交出去）。 */
  schema: ZodType<T>;
  doFetch: typeof fetch;
  /** 网络层失败（取不到）：端点自己的 Unavailable 造法。 */
  unavailable: (cause: unknown) => Error;
  /** 形状不对（含非 2xx 且没有自带档）：端点自己的 Protocol 造法。 */
  protocol: (status: number, text: string, cause: unknown) => Error;
  /**
   * HTTP 非 2xx 要先走的端点自带错误档（如目录浏览的 `DirsHttpError`、
   * 单场状态的 `SessionStatusHttpError`）；返回 `null`（或省略）按「形状不对」报。
   */
  httpError?: (status: number, text: string) => Error | null;
}

/**
 * 六个 GET 端点共用的取数骨架：`doFetch → text → !ok（端点自带档，没有就按形状
 * 不对立）→ JSON.parse → safeParse`。请求照发 `{ method: 'GET' }`、不带任何头
 * —— 读端点不带那两个必需头（三道防线是给「改状态」的请求设的）。
 */
export async function getJsonOk<T>(io: GetJsonIo<T>): Promise<T> {
  let response: Response;
  try {
    response = await io.doFetch(io.url, { method: 'GET' });
  } catch (cause) {
    throw io.unavailable(cause);
  }
  const text = await response.text();
  if (!response.ok) {
    const rejected = io.httpError?.(response.status, text) ?? null;
    if (rejected !== null) throw rejected;
    throw io.protocol(response.status, text, `HTTP ${response.status}`);
  }
  let json: unknown;
  try {
    json = JSON.parse(text);
  } catch (cause) {
    throw io.protocol(response.status, text, cause);
  }
  const parsed = io.schema.safeParse(json);
  if (!parsed.success) {
    throw io.protocol(response.status, text, parsed.error);
  }
  return parsed.data;
}
