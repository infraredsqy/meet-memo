import { summariesResponseSchema, type SummariesResponse } from '../contract';

/**
 * `GET /api/summaries`：本场总结记录（`docs/contract.md` 4.7）。
 *
 * 一条**纯读**的取数路径 —— 与 `POST /api/actions` 那一条不同，它**不带那两个必需头**
 * （动作端点那三个防线是给「改状态」的请求设的），也不写任何东西：读它不涨 `revision`、
 * 不广播、任何状态下都 200。
 *
 * 这是「回看本场总结记录」那个入口的唯一数据来源（设计章 5.1）：界面拿到的就是服务
 * 给的原文，不派生、不缓存、不落本地存储。
 */

export const SUMMARIES_URL = '/api/summaries';

/** 应答体形状不对（不是契约里那份 `{session_id, entries}`）。 */
export class SummariesProtocolError extends Error {
  readonly status: number;
  readonly raw: string;

  constructor(status: number, raw: string, cause: unknown) {
    const reason = cause instanceof Error ? cause.message : String(cause);
    super(`本场总结记录的应答形状不符合契约（HTTP ${status}）：${reason}`);
    this.name = 'SummariesProtocolError';
    this.status = status;
    this.raw = raw;
  }
}

/** 取不到（网络层失败、服务没应答）。 */
export class SummariesUnavailableError extends Error {
  constructor(cause: unknown) {
    super(`取不到本场总结记录：${cause instanceof Error ? cause.message : String(cause)}`);
    this.name = 'SummariesUnavailableError';
  }
}

export interface FetchSummariesOptions {
  /** 覆盖 fetch（测试注入用）。 */
  fetchImpl?: typeof fetch;
  /** 覆盖端点（测试注入用）。 */
  url?: string;
}

/** 取一次本场总结记录；形状不对抛 `SummariesProtocolError`，取不到抛 `SummariesUnavailableError`。 */
export async function fetchSummaries(
  options: FetchSummariesOptions = {},
): Promise<SummariesResponse> {
  const doFetch = options.fetchImpl ?? fetch;

  let response: Response;
  try {
    response = await doFetch(options.url ?? SUMMARIES_URL, { method: 'GET' });
  } catch (cause) {
    throw new SummariesUnavailableError(cause);
  }

  const text = await response.text();
  if (!response.ok) {
    // 契约说它「任何状态下都 200」，因此非 2xx 是协议外的一档：如实报，不编一个空列表
    // （把取不到画成「这一场还没总结过」会让人以为记录真的没有了）。
    throw new SummariesProtocolError(response.status, text, `HTTP ${response.status}`);
  }

  let json: unknown;
  try {
    json = JSON.parse(text);
  } catch (cause) {
    throw new SummariesProtocolError(response.status, text, cause);
  }

  const parsed = summariesResponseSchema.safeParse(json);
  if (!parsed.success) {
    throw new SummariesProtocolError(response.status, text, parsed.error);
  }
  return parsed.data;
}
