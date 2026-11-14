import {
  devicesResponseSchema,
  type DevicesResponse,
} from '../contract/index';
import { ProtocolError, UnavailableError, getJsonOk } from './common';

/**
 * 设备清单端点的客户端。
 *
 * 与 `POST /api/actions` 那条不同，它**不带那两个必需头**（三道防线是给「改状态」的
 * 请求设的），也不写任何东西：读它不涨 `revision`、不广播。
 */

export const DEVICES_URL = '/api/devices';

export class DevicesProtocolError extends ProtocolError {
  constructor(what: string, status: number, raw: string, cause: unknown) {
    const reason = cause instanceof Error ? cause.message : String(cause);
    super(`${what}的应答形状不符合契约（HTTP ${status}）：${reason}`, status, raw);
    this.name = 'DevicesProtocolError';
  }
}

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
