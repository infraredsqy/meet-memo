import {
  materialDeleteOkSchema,
  materialUploadOkSchema,
  materialsListSchema,
  type MaterialDeleteOk,
  type MaterialUploadOk,
  type MaterialsList,
} from '../contract/index';
import { HttpRejectError, ProtocolError, UnavailableError, getJsonOk, readFailureEnvelope } from './common';

/**
 * 会议资料的四条端点与选择器文件模式的盘上拷入（`?from=`）。
 *
 * **两条写端点必须带 `X-Meeting-Service: 1`**：缺了服务回 403
 * 且**不带失败信封**（中间件那一道就挡回去了，请求没走到端点）——这一档自成一
 * 个错误类，界面的 403 处理与失败信封分开。清单与取原文件是纯读，与其余 `GET /api/*`
 * 同档，不带头。
 *
 * `{id}` 是 URL 编码的场次目录绝对路径；`{name}` 同样按段编码。名目清洗在服务端做
 * （穿越段剥掉、非法字符换 `_`、重名自动加序号），客户端交原始名即可。
 */

export const MATERIALS_BASE = '/api/sessions';

export const MATERIALS_WRITE_HEADERS: Readonly<Record<string, string>> = {
  'X-Meeting-Service': '1',
};

export function materialsUrl(dir: string): string {
  return `${MATERIALS_BASE}/${encodeURIComponent(dir)}/materials`;
}

export function materialFileUrl(dir: string, name: string): string {
  return `${materialsUrl(dir)}/${encodeURIComponent(name)}`;
}

export class MaterialsProtocolError extends ProtocolError {
  constructor(what: string, status: number, raw: string, cause: unknown) {
    const reason = cause instanceof Error ? cause.message : String(cause);
    super(`${what}的应答形状不符合契约（HTTP ${status}）：${reason}`, status, raw);
    this.name = 'MaterialsProtocolError';
  }
}

export class MaterialsUnavailableError extends UnavailableError {
  constructor(what: string, cause: unknown) {
    super(`取不到${what}：${cause instanceof Error ? cause.message : String(cause)}`);
    this.name = 'MaterialsUnavailableError';
  }
}

export class MaterialsHttpError extends HttpRejectError {
  constructor(status: number, code: string | null, note: string | null, raw: string) {
    super(note ?? `会议资料被拒：HTTP ${status}`, status, code, note, raw);
    this.name = 'MaterialsHttpError';
  }
}

export class MaterialsForbiddenError extends Error {
  readonly status: number;
  readonly text: string;

  constructor(status: number, text: string) {
    super(`资料请求被中间件拦下（${status}，缺少必需请求头）：${text}`);
    this.name = 'MaterialsForbiddenError';
    this.status = status;
    this.text = text;
  }
}

function materialsHttpError(status: number, text: string): MaterialsHttpError {
  const { code, note } = readFailureEnvelope(text);
  return new MaterialsHttpError(status, code, note, text);
}

export interface MaterialsRequestOptions {
  fetchImpl?: typeof fetch;
}

async function sendWrite(
  what: string,
  url: string,
  init: RequestInit,
  options: MaterialsRequestOptions,
): Promise<unknown> {
  const doFetch = options.fetchImpl ?? fetch;
  let response: Response;
  try {
    response = await doFetch(url, {
      ...init,
      headers: { ...MATERIALS_WRITE_HEADERS, ...(init.headers as Record<string, string>) },
    });
  } catch (cause) {
    throw new MaterialsUnavailableError(what, cause);
  }
  const text = await response.text();
  if (response.status === 403) {
    throw new MaterialsForbiddenError(response.status, text);
  }
  if (!response.ok) {
    throw materialsHttpError(response.status, text);
  }
  let json: unknown;
  try {
    json = JSON.parse(text);
  } catch (cause) {
    throw new MaterialsProtocolError(what, response.status, text, cause);
  }
  return json;
}

/** 任何状态下都 200，寻址失败 404。 */
export async function fetchMaterials(
  dir: string,
  options: MaterialsRequestOptions = {},
): Promise<MaterialsList> {
  return getJsonOk({
    url: materialsUrl(dir),
    schema: materialsListSchema,
    doFetch: options.fetchImpl ?? fetch,
    unavailable: (cause) => new MaterialsUnavailableError('资料清单', cause),
    protocol: (status, text, cause) => new MaterialsProtocolError('资料清单', status, text, cause),
    httpError: (status, text) => materialsHttpError(status, text),
  });
}

/** 成功体里的 `material.name` 是服务清洗 / 让位之后的**存盘名**。 */
export async function uploadMaterial(
  dir: string,
  name: string,
  body: BodyInit,
  options: MaterialsRequestOptions = {},
): Promise<MaterialUploadOk> {
  const url = `${materialsUrl(dir)}?name=${encodeURIComponent(name)}`;
  const json = await sendWrite(
    '资料上传',
    url,
    { method: 'POST', body },
    options,
);
  const parsed = materialUploadOkSchema.safeParse(json);
  if (!parsed.success) {
    throw new MaterialsProtocolError('资料上传', 200, JSON.stringify(json), parsed.error);
  }
  return parsed.data;
}

/**
 * 从盘上拷一份进资料（选择器文件模式）：`from` 是选择器给的那个文件
 * 绝对路径，字节由服务从盘上流式拷入——界面上没有那些字节。
 */
export async function uploadMaterialFromDisk(
  dir: string,
  name: string,
  fromPath: string,
  options: MaterialsRequestOptions = {},
): Promise<MaterialUploadOk> {
  const url =
    `${materialsUrl(dir)}?name=${encodeURIComponent(name)}` +
    `&from=${encodeURIComponent(fromPath)}`;
  const json = await sendWrite('资料拷入', url, { method: 'POST' }, options);
  const parsed = materialUploadOkSchema.safeParse(json);
  if (!parsed.success) {
    throw new MaterialsProtocolError('资料拷入', 200, JSON.stringify(json), parsed.error);
  }
  return parsed.data;
}

export async function deleteMaterial(
  dir: string,
  name: string,
  options: MaterialsRequestOptions = {},
): Promise<MaterialDeleteOk> {
  const json = await sendWrite(
    '资料删除',
    materialFileUrl(dir, name),
    { method: 'DELETE' },
    options,
);
  const parsed = materialDeleteOkSchema.safeParse(json);
  if (!parsed.success) {
    throw new MaterialsProtocolError('资料删除', 200, JSON.stringify(json), parsed.error);
  }
  return parsed.data;
}
