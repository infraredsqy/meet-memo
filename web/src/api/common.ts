import type { ZodType } from 'zod';

/** 「取不到」的公共基类：各端点的 Unavailable 从它伸出。 */
export class UnavailableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'UnavailableError';
  }
}

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

export class HttpRejectError extends Error {
  readonly status: number;
  readonly code: string | null;
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

export function readFailureEnvelope(text: string): { code: string | null; note: string | null } {
  let code: string | null = null;
  let note: string | null = null;
  try {
    const body = JSON.parse(text) as { code?: unknown; message?: unknown };
    if (typeof body.code === 'string') code = body.code;
    if (typeof body.message === 'string' && body.message !== '') note = body.message;
  } catch {
    // 非 JSON：两格都留在 null。
  }
  return { code, note };
}

export interface GetJsonIo<T> {
  url: string;
  schema: ZodType<T>;
  doFetch: typeof fetch;
  unavailable: (cause: unknown) => Error;
  protocol: (status: number, text: string, cause: unknown) => Error;
  /**
   * HTTP 非 2xx 要先走的端点自带错误档（如目录浏览的 `DirsHttpError`、
   * 单场状态的 `SessionStatusHttpError`）；返回 `null`（或省略）按「形状不对」报。
   */
  httpError?: (status: number, text: string) => Error | null;
}

/**
 * 读端点不带那两个必需头（三道防线是给「改状态」的请求设的）。
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
