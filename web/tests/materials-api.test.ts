import { describe, expect, it } from 'vitest';
import {
  MaterialsForbiddenError,
  MaterialsHttpError,
  MaterialsProtocolError,
  MaterialsUnavailableError,
  deleteMaterial,
  fetchMaterials,
  materialFileUrl,
  materialsUrl,
  uploadMaterial,
  uploadMaterialFromDisk,
} from '../src/api/materials';
import { MATERIALS_FIXTURES, readFixture, readFixtureText } from './fixtures';

/**
 * 资料四条端点的客户端读法（160d1a78）：URL 怎么拼、写端点的头哨带没带、失败
 * 分几档、golden 样本过得了过不了 schema。`fetch` 全部注入，不起服务、不发真请求。
 */

/** 记账用的替身 fetch：按入队的应答逐个交，请求原样记账。 */
function makeFetch(...responses: Array<{ status: number; text: string }>): {
  fetch: typeof fetch;
  calls: Array<{ url: string; init: RequestInit }>;
} {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), init: init ?? {} });
    const next = responses.shift();
    if (next === undefined) throw new Error('替身没有这份应答');
    return {
      ok: next.status >= 200 && next.status < 300,
      status: next.status,
      text: async () => next.text,
    } as Response;
  }) as unknown as typeof fetch;
  return { fetch: fetchImpl, calls };
}

const DIR = 'C:\\会议\\跟老王聊周会';

describe('地址拼法', () => {
  it('场次与文件名各按段编码；文件地址就是清单地址再补一段', () => {
    expect(materialsUrl(DIR)).toBe(
      `/api/sessions/${encodeURIComponent(DIR)}/materials`,
    );
    expect(materialFileUrl(DIR, '报告 (2).pdf')).toBe(
      `${materialsUrl(DIR)}/${encodeURIComponent('报告 (2).pdf')}`,
    );
  });
});

describe('清单（GET，纯读不带头）', () => {
  it('golden 样本 after-upload 过 schema；请求不带 X-Meeting-Service', async () => {
    const { fetch: fetchImpl, calls } = makeFetch({
      status: 200,
      text: readFixtureText('materials', 'after-upload'),
    });
    const listing = await fetchMaterials(DIR, { fetchImpl });
    expect(listing.items).toHaveLength(3);
    expect(listing.items[0]?.name).toBe('假报告_v__.pdf');
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe(materialsUrl(DIR));
    expect(calls[0]?.init.method).toBe('GET');
    expect((calls[0]?.init.headers as Record<string, string> | undefined)?.['X-Meeting-Service']).toBeUndefined();
  });

  it('寻址失败（404 信封）按 MaterialsHttpError 交回，code 与原话都在', async () => {
    const missing = readFixture('materials', 'missing') as Record<string, unknown>;
    const { fetch: fetchImpl } = makeFetch({
      status: missing['status'] as number,
      text: JSON.stringify(missing['body']),
    });
    const error = await fetchMaterials(DIR, { fetchImpl }).then(
      () => null,
      (cause: unknown) => cause,
    );
    expect(error).toBeInstanceOf(MaterialsHttpError);
    if (!(error instanceof MaterialsHttpError)) throw new Error('档错了');
    expect(error.status).toBe(404);
    expect(error.code).toBe('material_missing');
    expect(error.note).toBe('资料里没有这一份：报告 (2).pdf');
  });

  it('网络层失败自成一档（Unavailable），形状不对自成一档（Protocol）', async () => {
    const broken = (async () => {
      throw new TypeError('Failed to fetch');
    }) as unknown as typeof fetch;
    await expect(fetchMaterials(DIR, { fetchImpl: broken })).rejects.toBeInstanceOf(
      MaterialsUnavailableError,
    );

    const { fetch: fetchImpl } = makeFetch({ status: 200, text: '{"dir": 1}' });
    await expect(fetchMaterials(DIR, { fetchImpl })).rejects.toBeInstanceOf(
      MaterialsProtocolError,
    );
  });
});

describe('上传（POST，带头哨）', () => {
  it('头哨必带：X-Meeting-Service: 1；name 走查询串；body 原样转手', async () => {
    const uploadOk = readFixture('materials', 'upload-ok') as Record<string, unknown>;
    const { fetch: fetchImpl, calls } = makeFetch({
      status: 200,
      text: JSON.stringify(uploadOk['body']),
    });
    const body = new File([new Uint8Array([1, 2, 3])], '报告.pdf');
    const ok = await uploadMaterial(DIR, '报告.pdf', body, { fetchImpl });
    expect(ok.material.name).toBe('报告.pdf');
    expect(ok.material.size_bytes).toBe(32);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.init.method).toBe('POST');
    expect(calls[0]?.url).toBe(`${materialsUrl(DIR)}?name=${encodeURIComponent('报告.pdf')}`);
    const headers = calls[0]?.init.headers as Record<string, string>;
    expect(headers['X-Meeting-Service']).toBe('1');
    expect(calls[0]?.init.body).toBe(body);
  });

  it('盘上拷入那一档（from）：from 与 name 都在查询串上，同样带头', async () => {
    const uploadOk = readFixture('materials', 'upload-ok') as Record<string, unknown>;
    const { fetch: fetchImpl, calls } = makeFetch({
      status: 200,
      text: JSON.stringify(uploadOk['body']),
    });
    await uploadMaterialFromDisk(DIR, '报告.pdf', 'C:\\资料\\报告.pdf', { fetchImpl });
    expect(calls[0]?.init.method).toBe('POST');
    expect(calls[0]?.url).toBe(
      `${materialsUrl(DIR)}?name=${encodeURIComponent('报告.pdf')}&from=${encodeURIComponent(
        'C:\\资料\\报告.pdf',
      )}`,
    );
    expect((calls[0]?.init.headers as Record<string, string>)['X-Meeting-Service']).toBe('1');
  });

  it('403（缺头被中间件拦下）自成一档，不走失败信封那半边', async () => {
    const { fetch: fetchImpl } = makeFetch({ status: 403, text: 'missing header' });
    const error = await uploadMaterial(DIR, 'x.txt', 'x', { fetchImpl }).then(
      () => null,
      (cause: unknown) => cause,
    );
    expect(error).toBeInstanceOf(MaterialsForbiddenError);
  });

  it('失败信封（400 invalid_setting）按 MaterialsHttpError 交回', async () => {
    const { fetch: fetchImpl } = makeFetch({
      status: 400,
      text: JSON.stringify({
        ok: false,
        code: 'invalid_setting',
        message: '要拷入的文件不存在：C:\\没有.pdf',
        detail: { key: 'from', value: 'C:\\没有.pdf' },
      }),
    });
    const error = await uploadMaterialFromDisk(DIR, 'x', 'C:\\没有.pdf', { fetchImpl }).then(
      () => null,
      (cause: unknown) => cause,
    );
    expect(error).toBeInstanceOf(MaterialsHttpError);
    if (!(error instanceof MaterialsHttpError)) throw new Error('档错了');
    expect(error.code).toBe('invalid_setting');
  });

  it('成功体形状不对（多了键 / 少了格）按 Protocol 拒', async () => {
    const { fetch: fetchImpl } = makeFetch({
      status: 200,
      text: JSON.stringify({ ok: true, revision: 1, material: { name: 'x' } }),
    });
    await expect(uploadMaterial(DIR, 'x', 'x', { fetchImpl })).rejects.toBeInstanceOf(
      MaterialsProtocolError,
    );
  });
});

describe('删除（DELETE，带头哨）', () => {
  it('golden 样本 delete-ok 过 schema；地址按段编码；头哨必带', async () => {
    const deleteOk = readFixture('materials', 'delete-ok') as Record<string, unknown>;
    const { fetch: fetchImpl, calls } = makeFetch({
      status: 200,
      text: JSON.stringify(deleteOk['body']),
    });
    const ok = await deleteMaterial(DIR, '报告 (2).pdf', { fetchImpl });
    expect(ok.material.name).toBe('报告 (2).pdf');
    expect(calls[0]?.init.method).toBe('DELETE');
    expect(calls[0]?.url).toBe(materialFileUrl(DIR, '报告 (2).pdf'));
    expect((calls[0]?.init.headers as Record<string, string>)['X-Meeting-Service']).toBe('1');
  });

  it('重复删除（404 material_missing）按 MaterialsHttpError 交回', async () => {
    const missing = readFixture('materials', 'missing') as Record<string, unknown>;
    const { fetch: fetchImpl } = makeFetch({
      status: missing['status'] as number,
      text: JSON.stringify(missing['body']),
    });
    await expect(deleteMaterial(DIR, '报告 (2).pdf', { fetchImpl })).rejects.toBeInstanceOf(
      MaterialsHttpError,
    );
  });
});

describe('样本清单', () => {
  it('四份样本一个不少（fixtures 组已在 fixtures.ts 登记）', () => {
    expect([...MATERIALS_FIXTURES].sort()).toEqual([
      'after-upload',
      'delete-ok',
      'missing',
      'upload-ok',
    ]);
  });
});
