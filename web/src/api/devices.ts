import {
  devicesResponseSchema,
  type DevicesResponse,
} from '../contract/index';
import { ProtocolError, UnavailableError, getJsonOk } from './common';

/**
 * 设备清单端点的客户端（`docs/contract.md` 4.10，设置抽屉节点）。
 *
 * 与 `POST /api/actions` 那条不同，它**不带那两个必需头**（三道防线是给「改状态」的
 * 请求设的），也不写任何东西：读它不涨 `revision`、不广播。界面拿到的就是服务给的
 * 原文，不派生、不缓存、不落本地存储（照 `api/sessions.ts` 的先例）。
 */

export const DEVICES_URL = '/api/devices';

/** 应答体形状不对。 */
export class DevicesProtocolError extends ProtocolError {
  constructor(what: string, status: number, raw: string, cause: unknown) {
    const reason = cause instanceof Error ? cause.message : String(cause);
    super(`${what}的应答形状不符合契约（HTTP ${status}）：${reason}`, status, raw);
    this.name = 'DevicesProtocolError';
  }
}

/** 取不到（网络层失败、服务没应答）。 */
export class DevicesUnavailableError extends UnavailableError {
  constructor(what: string, cause: unknown) {
    super(`取不到${what}：${cause instanceof Error ? cause.message : String(cause)}`);
    this.name = 'DevicesUnavailableError';
  }
}

export interface FetchDevicesOptions {
  fetchImpl?: typeof fetch;
  url?: string;
}

/** 取两路各自的候选端点表；形状不对/取不到分别抛 Protocol / Unavailable。 */
export async function fetchDevices(
  options: FetchDevicesOptions = {},
): Promise<DevicesResponse> {
  return getJsonOk({
    url: options.url ?? DEVICES_URL,
    schema: devicesResponseSchema,
    doFetch: options.fetchImpl ?? fetch,
    unavailable: (cause) => new DevicesUnavailableError('设备清单', cause),
    protocol: (status, text, cause) => new DevicesProtocolError('设备清单', status, text, cause),
  });
}
