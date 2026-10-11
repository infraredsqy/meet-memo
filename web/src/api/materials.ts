import {
  materialDeleteOkSchema,
  materialUploadOkSchema,
  materialsListSchema,
  type MaterialDeleteOk,
  type MaterialUploadOk,
  type MaterialsList,
} from '../contract/index';

/**
 * 会议资料的四条端点（`docs/contract.md` 4.12，资料节点 285bf58c）与选择器文件模式
 * 的盘上拷入（`?from=`，160d1a78）。
 *
 * **两条写端点必须带 `X-Meeting-Service: 1`**（契约第 6 章第 4 条）：缺了服务回 403
 * 且**不带失败信封**（中间件那一道就挡回去了，请求没走到端点）——这一档自成一
 * 个错误类，界面的 403 处理与失败信封分开。清单与取原文件是纯读，与其余 `GET /api/*`
 * 同档，不带头。
 *
 * `{id}` 是 URL 编码的场次目录绝对路径；`{name}` 同样按段编码。名目清洗在服务端做
 * （穿越段剥掉、非法字符换 `_`、重名自动加序号），客户端交原始名即可。
 */

export const MATERIALS_BASE = '/api/sessions';

/** 资料两条写端点的必需头（缺了 403，不带失败信封 —— 契约第 6 章第 4 条）。 */
export const MATERIALS_WRITE_HEADERS: Readonly<Record<string, string>> = {
  'X-Meeting-Service': '1',
};

/** 场次的资料段地址：`/api/sessions/{id}/materials`。 */
export function materialsUrl(dir: string): string {
  return `${MATERIALS_BASE}/${encodeURIComponent(dir)}/materials`;
}

/** 一份资料的原文件地址（图片缩略图与原图查看的 `img src` 都指到它——纯读不带头）。 */
export function materialFileUrl(dir: string, name: string): string {
  return `${materialsUrl(dir)}/${encodeURIComponent(name)}`;
}

/** 应答体形状不对。 */
export class MaterialsProtocolError extends Error {
  readonly status: number;
  readonly raw: string;

  constructor(what: string, status: number, raw: string, cause: unknown) {
    const reason = cause instanceof Error ? cause.message : String(cause);
    super(`${what}的应答形状不符合契约（HTTP ${status}）：${reason}`);
    this.name = 'MaterialsProtocolError';
    this.status = status;
    this.raw = raw;
  }
}

/** 取不到（网络层失败、服务没应答）。 */
export class MaterialsUnavailableError extends Error {
  constructor(what: string, cause: unknown) {
    super(`取不到${what}：${cause instanceof Error ? cause.message : String(cause)}`);
    this.name = 'MaterialsUnavailableError';
  }
}

/** 服务回了失败信封的 HTTP 拒答（4.12 的 `material_missing` / 寻址与盘上两档等）。 */
export class MaterialsHttpError extends Error {
  readonly status: number;
  /** 失败信封的 `code`；读不出信封就是 `null`。 */
  readonly code: string | null;
  /** 失败信封的 `message` 原话；读不出信封就是 `null`。 */
  readonly note: string | null;
  readonly raw: string;

  constructor(status: number, code: string | null, note: string | null, raw: string) {
    super(note ?? `会议资料被拒：HTTP ${status}`);
    this.name = 'MaterialsHttpError';
    this.status = status;
    this.code = code;
    this.note = note;
    this.raw = raw;
  }
}

/** 请求被中间件挡下（缺 `X-Meeting-Service: 1`）：403、纯文本、没有失败信封。 */
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

/** 4xx/5xx 信封读法：`code` / `message` 读得出就带走，整份信封读不出就都 `null`。 */
function materialsHttpError(status: number, text: string): MaterialsHttpError {
  let code: string | null = null;
  let note: string | null = null;
  try {
    const body = JSON.parse(text) as { code?: unknown; message?: unknown };
    if (typeof body.code === 'string') code = body.code;
    if (typeof body.message === 'string' && body.message !== '') note = body.message;
  } catch {
    // 非 JSON：两格都留在 null，message 走固定话术
  }
  return new MaterialsHttpError(status, code, note, text);
}

export interface MaterialsRequestOptions {
  fetchImpl?: typeof fetch;
}

/** 两只写端点共用的送信骨架：带头、403 自成一档、成功体按各自 schema 收。 */
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

/** 取一份清单（`{id}` 是这一场的目录路径；任何状态下都 200，寻址失败 404）。 */
export async function fetchMaterials(
  dir: string,
  options: MaterialsRequestOptions = {},
): Promise<MaterialsList> {
  const doFetch = options.fetchImpl ?? fetch;
  let response: Response;
  try {
    response = await doFetch(materialsUrl(dir), { method: 'GET' });
  } catch (cause) {
    throw new MaterialsUnavailableError('资料清单', cause);
  }
  const text = await response.text();
  if (!response.ok) {
    throw materialsHttpError(response.status, text);
  }
  let json: unknown;
  try {
    json = JSON.parse(text);
  } catch (cause) {
    throw new MaterialsProtocolError('资料清单', response.status, text, cause);
  }
  const parsed = materialsListSchema.safeParse(json);
  if (!parsed.success) {
    throw new MaterialsProtocolError('资料清单', response.status, text, parsed.error);
  }
  return parsed.data;
}

/**
 * 上传一份（`name` 交原始名，服务侧清洗；`body` 就是文件的原样字节）。
 * 成功体里的 `material.name` 是服务清洗 / 让位之后的**存盘名**。
 */
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
 * 从盘上拷一份进资料（选择器文件模式，160d1a78）：`from` 是选择器给的那个文件
 * 绝对路径，字节由服务从盘上流式拷入——界面上没有那些字节。成功体与正文上传同一形状。
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

/** 删除那一份（只删这一个文件；删不到回 404 `material_missing`）。 */
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
