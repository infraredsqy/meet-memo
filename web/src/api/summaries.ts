import { summariesResponseSchema, type SummariesResponse } from '../contract';
import { ProtocolError, UnavailableError, getJsonOk } from './common';

/**
 * `GET /api/summaries`：本场总结记录。
 *
 * 一条**纯读**的取数路径 —— 与 `POST /api/actions` 那一条不同，它**不带那两个必需头**
 * （动作端点那三个防线是给「改状态」的请求设的），也不写任何东西：读它不涨 `revision`、
 * 不广播、任何状态下都 200。
 */

export const SUMMARIES_URL = '/api/summaries';

export class SummariesProtocolError extends ProtocolError {
  constructor(status: number, raw: string, cause: unknown) {
    const reason = cause instanceof Error ? cause.message : String(cause);
    super(`本场总结记录的应答形状不符合契约（HTTP ${status}）：${reason}`, status, raw);
    this.name = 'SummariesProtocolError';
  }
}

export class SummariesUnavailableError extends UnavailableError {
  constructor(cause: unknown) {
    super(`取不到本场总结记录：${cause instanceof Error ? cause.message : String(cause)}`);
    this.name = 'SummariesUnavailableError';
  }
}

export interface FetchSummariesOptions {
  fetchImpl?: typeof fetch;
  url?: string;
}

export async function fetchSummaries(
  options: FetchSummariesOptions = {},
): Promise<SummariesResponse> {
  return getJsonOk({
    url: options.url ?? SUMMARIES_URL,
    schema: summariesResponseSchema,
    doFetch: options.fetchImpl ?? fetch,
    unavailable: (cause) => new SummariesUnavailableError(cause),
    // 非 2xx 是协议外的一档：如实报，不编一个空列表
    // （把取不到画成「这一场还没总结过」会让人以为记录真的没有了）。
    protocol: (status, text, cause) => new SummariesProtocolError(status, text, cause),
  });
}
