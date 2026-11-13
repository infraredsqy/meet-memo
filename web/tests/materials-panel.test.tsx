import { describe, expect, it, vi, afterEach } from 'vitest';
import { createElement } from 'react';
import TestRenderer, { act } from 'react-test-renderer';
import { MaterialsPanel } from '../src/materials/MaterialsPanel';
import { browseDirs } from '../src/api/sessions';
import { materialFileUrl } from '../src/api/materials';
import { MATERIALS_MAX_BYTES } from '../src/materials/model';
import type { MaterialItem } from '../src/contract';

/**
 * 资料区的挂载用例（免 DOM 跑法，手法照 `home-mount.test.tsx`）。
 *
 * `fetch` 全部注入（`fetchImpl` prop），选择器的取数打在 `browseDirs` 替身上——
 * 不起服务、不发真请求。spec 完成判定点名的几样在这里**真的点**：删除确认、
 * 大文件确认框、拖拽上传、提交文字、选择器文件模式。页面级粘贴的筛子与让位判据
 * 在 `materials-model.test.ts`（node 没有 document，挂载用例不碰事件监听）。
 */

vi.mock('../src/api/sessions', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/sessions')>();
  return { ...actual, browseDirs: vi.fn() };
});
const browseDirsMock = vi.mocked(browseDirs);

const DIR = 'C:\\会议\\跟老王聊周会';

/** 一份清单应答体（目录就是清单）。 */
function listing(items: MaterialItem[]): string {
  return JSON.stringify({ dir: DIR, materials_dir: `${DIR}\\meeting_materials`, items });
}

const TWO_ITEMS: MaterialItem[] = [
  { name: '截图.png', size_bytes: 15, added_at: '2026-09-21T21:03:00+08:00' },
  { name: '纪要.md', size_bytes: 32, added_at: '2026-09-21T21:05:00+08:00' },
];

interface SpyOptions {
  get?: () => FakeResponse;
  post?: () => FakeResponse;
  del?: () => FakeResponse;
}
interface FakeResponse {
  status: number;
  text: string;
}

/** 登记替身 fetch：按方法分派应答，逐个请求登记（URL 与 init 都留着给断言）。 */
function spyFetch(options: SpyOptions = {}): {
  fetchImpl: typeof fetch;
  calls: Array<{ url: string; init: RequestInit }>;
} {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = init ?? {};
    const method = request.method ?? 'GET';
    calls.push({ url: String(input), init: request });
    const handler =
      method === 'GET' ? options.get : method === 'POST' ? options.post : options.del;
    const response = handler
      ? handler()
      : method === 'POST'
        ? {
            status: 200,
            text: JSON.stringify({
              ok: true,
              revision: 1,
              material: { name: '存盘名', size_bytes: 3, added_at: '2026-09-21T21:03:00+08:00' },
            }),
          }
        : {
            status: 200,
            text: JSON.stringify({ ok: true, revision: 1, material: { name: '存盘名' } }),
          };
    return {
      ok: response.status >= 200 && response.status < 300,
      status: response.status,
      text: async () => response.text,
    } as Response;
  }) as unknown as typeof fetch;
  return { fetchImpl, calls };
}

function countBy(calls: Array<{ init: RequestInit }>, method: string): number {
  return calls.filter((call) => (call.init.method ?? 'GET') === method).length;
}

async function mountPanel(fetchImpl: typeof fetch): Promise<TestRenderer.ReactTestRenderer> {
  let renderer!: TestRenderer.ReactTestRenderer;
  await act(async () => {
    renderer = TestRenderer.create(
      createElement(MaterialsPanel, { dir: DIR, fetchImpl }),
);
  });
  return renderer;
}

function textOf(node: TestRenderer.ReactTestInstance): string {
  const parts: string[] = [];
  const walk = (current: TestRenderer.ReactTestInstance): void => {
    for (const child of current.children) {
      if (typeof child === 'string') {
        parts.push(child);
        continue;
      }
      walk(child);
    }
  };
  walk(node);
  return parts.join(' ');
}

function buttonByText(
  renderer: TestRenderer.ReactTestRenderer,
  label: string,
): TestRenderer.ReactTestInstance {
  const found = renderer.root.findAll(
    (node) =>
      node.type === 'button' &&
      (Array.isArray(node.props.children)
        ? node.props.children.join('')
        : `${node.props.children}`) === label,
);
  expect(found, `界面上该有「${label}」这只按钮`).toHaveLength(1);
  return found[0] as TestRenderer.ReactTestInstance;
}

async function click(
  renderer: TestRenderer.ReactTestRenderer,
  node: TestRenderer.ReactTestInstance,
): Promise<void> {
  await act(async () => {
    node.props.onClick();
  });
}

/** 把点击 / 拖拽之后那串 promise 链（上传 → 反馈行 → 重取清单）冲到落定。 */
async function flush(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  });
}

function rows(renderer: TestRenderer.ReactTestRenderer): TestRenderer.ReactTestInstance[] {
  return renderer.root.findAll(
    (node) =>
      node.props.role === 'option' &&
      node.parent?.props['aria-label'] === '资料清单',
);
}

/** 一只改过 size 的 File（node 的 File 全局有；size 是 Blob 的 getter，逐个覆盖）。 */
function sizedFile(name: string, size: number): File {
  const file = new File(['x'], name);
  Object.defineProperty(file, 'size', { value: size });
  return file;
}

function dropOnto(
  renderer: TestRenderer.ReactTestRenderer,
  files: File[],
): Promise<void> {
  const panel = renderer.root.find(
    (node) => node.props['aria-label'] === '资料' && typeof node.props.onDrop === 'function',
);
  return act(async () => {
    panel.props.onDrop({ preventDefault() {}, dataTransfer: { files } });
  });
}

afterEach(() => {
  browseDirsMock.mockReset();
});

describe('清单与行', () => {
  it('挂载即取清单：图片行给缩略图（src 指到取原文件端点）与可点开的原图，非图片给类型标签', async () => {
    const { fetchImpl } = spyFetch({ get: () => ({ status: 200, text: listing(TWO_ITEMS) }) });
    const renderer = await mountPanel(fetchImpl);

    const list = rows(renderer);
    expect(list).toHaveLength(2);
    const thumb = renderer.root.find(
      (node) => node.type === 'img' && `${node.props.className}`.includes('mat__thumb'),
);
    expect(thumb.props.src).toBe(materialFileUrl(DIR, '截图.png'));
    // 图片行的名字是颗按钮（点开看原图）；非图片行是纯文本
    expect(
      renderer.root.findAll(
        (node) => node.type === 'button' && textOf(node) === '截图.png',
),
).toHaveLength(1);
    expect(
      renderer.root.findAll((node) => `${node.props.className}`.includes('mat__icon')),
).toHaveLength(1);
    const text = textOf(list[1] as TestRenderer.ReactTestInstance);
    expect(text).toContain('纪要.md');
    expect(text).toContain('32 B');
    expect(text).toContain('2026-09-21 21:05');
  });

  it('清单取失败：失败行如实画 + 重试一颗；重试成功后行收起、清单出列', async () => {
    let status = 404;
    let body = JSON.stringify({
      ok: false,
      code: 'session_dir_missing',
      message: '这一场的目录已不在原地',
      detail: { dir: DIR, kind: 'missing' },
    });
    const { fetchImpl } = spyFetch({
      get: () => ({ status, text: body }),
    });
    const renderer = await mountPanel(fetchImpl);
    expect(textOf(renderer.root).includes('session_dir_missing')).toBe(true);
    expect(rows(renderer)).toHaveLength(0);

    status = 200;
    body = listing(TWO_ITEMS);
    await click(renderer, buttonByText(renderer, '重试'));
    expect(rows(renderer)).toHaveLength(2);
  });
});

describe('多选删除（一次确认整批删）', () => {
  it('勾两行 → 「删除所选(2)」 → 确认弹窗写明份数 → 确认后逐文件 DELETE 且带头，清单重取', async () => {
    const { fetchImpl, calls } = spyFetch({
      get: () => ({ status: 200, text: listing(TWO_ITEMS) }),
      del: () => ({
        status: 200,
        text: JSON.stringify({ ok: true, revision: 2, material: { name: 'x' } }),
      }),
    });
    const renderer = await mountPanel(fetchImpl);
    const checkboxes = renderer.root.findAll(
      (node) => node.type === 'input' && node.props.type === 'checkbox',
);
    expect(checkboxes).toHaveLength(2);
    await act(async () => {
      checkboxes.forEach((box) => box.props.onChange());
    });

    await click(renderer, buttonByText(renderer, '删除所选(2)'));
    // 确认弹窗写明份数与「删了就找不回来」（硬删的知情同意）
    const dialog = renderer.root.find((node) => node.props.role === 'dialog');
    expect(textOf(dialog).includes('2 份')).toBe(true);
    expect(textOf(dialog).includes('找不回来')).toBe(true);
    expect(countBy(calls, 'DELETE')).toBe(0);

    await click(renderer, buttonByText(renderer, '删除'));
    await flush();
    expect(countBy(calls, 'DELETE')).toBe(2);
    for (const call of calls.filter((item) => item.init.method === 'DELETE')) {
      expect((call.init.headers as Record<string, string>)['X-Meeting-Service']).toBe('1');
    }
    // 一次 GET 挂载 + 一次清单刷新
    expect(countBy(calls, 'GET')).toBe(2);
    expect(renderer.root.findAll((node) => node.props.role === 'dialog')).toHaveLength(0);
    // 结果行如实写
    expect(textOf(renderer.root).includes('已删除 2 份。')).toBe(true);
  });

  it('有一份没删成：结果行写明哪一份、原因带上（material_missing 的原话），不悄悄吞', async () => {
    // 第一份删成、第二份 404：结果行两半都要如实
    const outcomes = [
      () => ({
        status: 200,
        text: JSON.stringify({ ok: true, revision: 2, material: { name: '截图.png' } }),
      }),
      () => ({
        status: 404,
        text: JSON.stringify({
          ok: false,
          code: 'material_missing',
          message: '资料里没有这一份：纪要.md',
          detail: { name: '纪要.md' },
        }),
      }),
    ];
    const { fetchImpl, calls } = spyFetch({
      get: () => ({ status: 200, text: listing(TWO_ITEMS) }),
      del: () => outcomes.shift()!(),
    });
    const renderer = await mountPanel(fetchImpl);
    const checkboxes = renderer.root.findAll(
      (node) => node.type === 'input' && node.props.type === 'checkbox',
);
    await act(async () => {
      checkboxes.forEach((box) => box.props.onChange());
    });
    await click(renderer, buttonByText(renderer, '删除所选(2)'));
    await click(renderer, buttonByText(renderer, '删除'));
    await flush();
    expect(countBy(calls, 'DELETE')).toBe(2);
    expect(textOf(renderer.root).includes('删除了 1 份')).toBe(true);
    expect(textOf(renderer.root).includes('纪要.md')).toBe(true);
    expect(textOf(renderer.root).includes('material_missing')).toBe(true);
  });
});

describe('大文件确认框（100M 口径）', () => {
  it('拖入超 100M 的那批：先弹「确认要放进资料吗」，取消一个请求都不发；确认后整批照传', async () => {
    const { fetchImpl, calls } = spyFetch({
      get: () => ({ status: 200, text: listing([]) }),
      post: () => ({
        status: 200,
        text: JSON.stringify({
          ok: true,
          revision: 1,
          material: { name: '大.mov', size_bytes: MATERIALS_MAX_BYTES + 1, added_at: '<iso>' },
        }),
      }),
    });
    const renderer = await mountPanel(fetchImpl);
    const big = sizedFile('大.mov', MATERIALS_MAX_BYTES + 1);

    await dropOnto(renderer, [big]);
    const dialog = renderer.root.find((node) => node.props.role === 'dialog');
    expect(textOf(dialog).includes('确认要放进资料吗')).toBe(true);
    expect(textOf(dialog).includes('大.mov')).toBe(true);
    expect(countBy(calls, 'POST')).toBe(0);

    await click(renderer, buttonByText(renderer, '取消'));
    expect(renderer.root.findAll((node) => node.props.role === 'dialog')).toHaveLength(0);
    expect(countBy(calls, 'POST')).toBe(0);

    await dropOnto(renderer, [big]);
    await click(renderer, buttonByText(renderer, '照传'));
    await flush();
    expect(countBy(calls, 'POST')).toBe(1);
    const post = calls.find((call) => call.init.method === 'POST') as {
      url: string;
      init: RequestInit;
    };
    expect((post.init.headers as Record<string, string>)['X-Meeting-Service']).toBe('1');
    expect(post.init.body).toBe(big);
  });

  it('不超线的文件拖进来直接传，不走确认框', async () => {
    const { fetchImpl, calls } = spyFetch({ get: () => ({ status: 200, text: listing([]) }) });
    const renderer = await mountPanel(fetchImpl);
    await dropOnto(renderer, [sizedFile('小.pdf', 15)]);
    await flush();
    expect(countBy(calls, 'POST')).toBe(1);
    expect(renderer.root.findAll((node) => node.props.role === 'dialog')).toHaveLength(0);
    // 逐文件反馈：完成那一行带上大小
    expect(textOf(renderer.root).includes('已放进资料（15 B）')).toBe(true);
  });
});

describe('提交文字', () => {
  function openDialog(
    renderer: TestRenderer.ReactTestRenderer,
): TestRenderer.ReactTestInstance {
    return renderer.root.find((node) => node.props.role === 'dialog');
  }

  it('正文必填：空正文按不下（写明原因、零请求）；填了标题按标题落名，正文原样字节交上去', async () => {
    const { fetchImpl, calls } = spyFetch({
      get: () => ({ status: 200, text: listing([]) }),
      post: () => ({
        status: 200,
        text: JSON.stringify({
          ok: true,
          revision: 1,
          material: { name: '会议备注.md', size_bytes: 12, added_at: '<iso>' },
        }),
      }),
    });
    const renderer = await mountPanel(fetchImpl);
    await click(renderer, buttonByText(renderer, '提交文字'));
    const dialog = openDialog(renderer);

    await click(renderer, buttonByText(renderer, '放进资料'));
    expect(textOf(dialog).includes('正文必填')).toBe(true);
    expect(countBy(calls, 'POST')).toBe(0);

    const title = dialog.find(
      (node) => node.type === 'input' && node.props['aria-label'] === '标题（可不填）',
);
    const body = dialog.find(
      (node) => node.type === 'textarea' && node.props['aria-label'] === '正文（必填）',
);
    await act(async () => {
      title.props.onChange({ target: { value: '会议备注' } });
      body.props.onChange({ target: { value: '第一段正文' } });
    });
    await click(renderer, buttonByText(renderer, '放进资料'));
    await flush();
    expect(countBy(calls, 'POST')).toBe(1);
    const post = calls.find((call) => call.init.method === 'POST') as {
      url: string;
      init: RequestInit;
    };
    expect(post.url).toContain(`name=${encodeURIComponent('会议备注.md')}`);
    expect(new TextDecoder().decode(post.init.body as Uint8Array)).toBe('第一段正文');
    // 成功即关弹窗，任务行给完成反馈
    expect(renderer.root.findAll((node) => node.props.role === 'dialog')).toHaveLength(0);
    expect(textOf(renderer.root).includes('已放进资料')).toBe(true);
  });

  it('标题没填：文件名用本地时间戳 YYYYMMDD-HHMMSS.md', async () => {
    const { fetchImpl, calls } = spyFetch({
      get: () => ({ status: 200, text: listing([]) }),
    });
    const renderer = await mountPanel(fetchImpl);
    await click(renderer, buttonByText(renderer, '提交文字'));
    const dialog = openDialog(renderer);
    const body = dialog.find(
      (node) => node.type === 'textarea' && node.props['aria-label'] === '正文（必填）',
);
    await act(async () => {
      body.props.onChange({ target: { value: '没有标题的那段' } });
    });
    await click(renderer, buttonByText(renderer, '放进资料'));
    await flush();
    const post = calls.find((call) => call.init.method === 'POST') as { url: string };
    expect(post.url).toMatch(/name=\d{8}-\d{6}\.md/);
  });
});

describe('图片查看原图', () => {
  it('点图片名字打开 dlg--viewer（img 指到取原文件端点），关闭即收', async () => {
    const { fetchImpl } = spyFetch({ get: () => ({ status: 200, text: listing(TWO_ITEMS) }) });
    const renderer = await mountPanel(fetchImpl);
    await click(
      renderer,
      renderer.root.find((node) => node.type === 'button' && textOf(node) === '截图.png'),
);
    const viewer = renderer.root.find(
      (node) =>
        node.props.role === 'dialog' &&
        `${node.props.className}`.includes('dlg--viewer'),
);
    const img = viewer.find((node) => node.type === 'img');
    expect(img.props.src).toBe(materialFileUrl(DIR, '截图.png'));
    await click(renderer, buttonByText(renderer, '关闭'));
    expect(renderer.root.findAll((node) => node.props.role === 'dialog')).toHaveLength(0);
  });
});

describe('文件选择器（文件模式）', () => {
  /** 打开选择器、选中那一份文件、按「加入资料」（大小口径用例复用这套动作）。 */
  async function openPickerAndPick(
    renderer: TestRenderer.ReactTestRenderer,
    fileName: string,
): Promise<void> {
    await click(renderer, buttonByText(renderer, '选文件…'));
    const picker = renderer.root.find(
      (node) => node.props.role === 'dialog' && `${node.props.className}`.includes('dlg--picker'),
);
    const row = picker.findAll(
      (node) => node.props.role === 'option' && textOf(node).includes(fileName),
);
    expect(row, `选择器里该有「${fileName}」这一行`).toHaveLength(1);
    await act(async () => {
      (row[0] as TestRenderer.ReactTestInstance).props.onClick();
    });
    await click(renderer, buttonByText(renderer, '加入资料'));
  }

  it('选文件…打开文件模式的选择器（标题与确认钮都换了），文件行带大小；挑一条 → 加入资料 → 服务按 from 拷', async () => {
    browseDirsMock.mockResolvedValue({
      home: 'C:\\Users\\<user>',
      cwd: 'C:\\资料',
      up: 'C:\\Users\\<user>',
      here: { name: '资料', dir: 'C:\\资料', kind: 'unknown', suspected: false },
      dirs: [{ name: '子目录', dir: 'C:\\资料\\子目录', kind: 'unknown', suspected: false }],
      files: [
        { name: '报告.pdf', dir: 'C:\\资料\\报告.pdf', size_bytes: 10 },
      ],
    });
    const { fetchImpl, calls } = spyFetch({
      get: () => ({ status: 200, text: listing([]) }),
      post: () => ({
        status: 200,
        text: JSON.stringify({
          ok: true,
          revision: 1,
          material: { name: '报告.pdf', size_bytes: 10, added_at: '<iso>' },
        }),
      }),
    });
    const renderer = await mountPanel(fetchImpl);
    await click(renderer, buttonByText(renderer, '选文件…'));

    const picker = renderer.root.find(
      (node) => node.props.role === 'dialog' && `${node.props.className}`.includes('dlg--picker'),
);
    expect(textOf(picker).includes('选择要放进资料的文件')).toBe(true);
    // 老用途的「选定这个目录」在文件模式不出现
    expect(
      renderer.root.findAll(
        (node) => node.type === 'button' && textOf(node) === '选定这个目录',
),
).toHaveLength(0);
    // 文件行带大小；目录行还是老两格（名目判读）
    const fileRow = picker.findAll(
      (node) => node.props.role === 'option' && textOf(node).includes('报告.pdf'),
);
    expect(fileRow).toHaveLength(1);
    expect(textOf(fileRow[0] as TestRenderer.ReactTestInstance).includes('10 B')).toBe(true);
    // 没选中之前确认置灰
    expect((buttonByText(renderer, '加入资料').props as { disabled: boolean }).disabled).toBe(true);

    await act(async () => {
      (fileRow[0] as TestRenderer.ReactTestInstance).props.onClick();
    });
    await click(renderer, buttonByText(renderer, '加入资料'));
    await flush();
    const post = calls.find((call) => call.init.method === 'POST') as {
      url: string;
      init: RequestInit;
    };
    expect(post.url).toContain(`name=${encodeURIComponent('报告.pdf')}`);
    expect(post.url).toContain(`from=${encodeURIComponent('C:\\资料\\报告.pdf')}`);
    expect((post.init.headers as Record<string, string>)['X-Meeting-Service']).toBe('1');
    // 拷入完成给逐文件反馈
    expect(textOf(renderer.root).includes('已放进资料（10 B）')).toBe(true);
  });

  it('挑的文件超过 100M：同一只「确认要放进资料吗」先弹（正文点明文件名与大小），取消零请求；确认照传且 from= 原样', async () => {
    browseDirsMock.mockResolvedValue({
      home: 'C:\\Users\\<user>',
      cwd: 'C:\\资料',
      up: 'C:\\Users\\<user>',
      here: { name: '资料', dir: 'C:\\资料', kind: 'unknown', suspected: false },
      dirs: [],
      files: [
        { name: '录像.mkv', dir: 'C:\\资料\\录像.mkv', size_bytes: MATERIALS_MAX_BYTES + 1 },
      ],
    });
    const { fetchImpl, calls } = spyFetch({
      get: () => ({ status: 200, text: listing([]) }),
      post: () => ({
        status: 200,
        text: JSON.stringify({
          ok: true,
          revision: 1,
          material: {
            name: '录像.mkv',
            size_bytes: MATERIALS_MAX_BYTES + 1,
            added_at: '<iso>',
          },
        }),
      }),
    });
    const renderer = await mountPanel(fetchImpl);

    // 挑中超线文件、按了「加入资料」：弹的是确认框，不是直接传
    await openPickerAndPick(renderer, '录像.mkv');
    const dialog = renderer.root.find((node) => node.props.role === 'dialog');
    expect(textOf(dialog).includes('确认要放进资料吗')).toBe(true);
    expect(textOf(dialog).includes('录像.mkv')).toBe(true);
    expect(countBy(calls, 'POST')).toBe(0);

    // 取消：一个请求都不发（选择器已收，什么都不落）
    await click(renderer, buttonByText(renderer, '取消'));
    expect(renderer.root.findAll((node) => node.props.role === 'dialog')).toHaveLength(0);
    expect(countBy(calls, 'POST')).toBe(0);

    // 再挑一次、确认：照传，name 与 from= 都是挑中的那一份
    await openPickerAndPick(renderer, '录像.mkv');
    await click(renderer, buttonByText(renderer, '照传'));
    await flush();
    expect(countBy(calls, 'POST')).toBe(1);
    const post = calls.find((call) => call.init.method === 'POST') as { url: string };
    expect(post.url).toContain(`name=${encodeURIComponent('录像.mkv')}`);
    expect(post.url).toContain(`from=${encodeURIComponent('C:\\资料\\录像.mkv')}`);
    expect(textOf(renderer.root).includes('已放进资料')).toBe(true);
  });
});
