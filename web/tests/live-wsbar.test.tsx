import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest';
import { createElement } from 'react';
import TestRenderer, { act } from 'react-test-renderer';

import {
  createEventsClient,
  createHeartbeatClient,
  postAction,
  type EventsClientHandlers,
  type HeartbeatClient,
} from '../src/api';
import { fetchSessions } from '../src/api/sessions';
import { LiveView } from '../src/App';
import type { SessionRow, SessionsResponse, Snapshot } from '../src/contract';
import { makeSnapshot, T } from './live-fixtures';

/**
 * 会中那一屏的**常驻工作区栏**（d61829f1）的渲染层断言：
 *
 * - 栏存在（与工作台同一条 `home__list`）：清单、添加工作区、忙闲一格都在；
 * - 点别行发的是**既有**的 `workspace_open` 登记动作，成功后详情切过去；
 * - 在录时按「开始录音」**如实拒**：一个请求都不发，拒的话指出在录的是哪一场，
 *   并给「打开那一场」的去处（`start-rejected` 档口径，不停在「没反应」）；
 *   选区**已**是在录那一行时不摆这只钮——按下只会空转（review-1 m3）。
 *
 * 手法照 `live-pause-screen.test.tsx`：`react-test-renderer` 在 node 环境真挂载，
 * 服务那两侧全是替身 —— **这个文件不发声、不连网、不起任何真进程**。
 */

vi.mock('../src/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api')>();
  return {
    ...actual,
    createEventsClient: vi.fn(),
    createHeartbeatClient: vi.fn(),
    fetchSummaries: vi.fn(),
    postAction: vi.fn(),
  };
});

vi.mock('../src/api/sessions', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/sessions')>();
  return { ...actual, fetchSessions: vi.fn() };
});

vi.mock('../src/api/materials', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/materials')>();
  return {
    ...actual,
    fetchMaterials: vi.fn(async () => ({ dir: '', materials_dir: '', items: [] })),
  };
});

const createHeartbeatClientMock = vi.mocked(createHeartbeatClient);
const fetchSessionsMock = vi.mocked(fetchSessions);
const postActionMock = vi.mocked(postAction);

const SESSION_DIR = 'C:\\会议\\跟老王聊周会';
const OTHER_DIR = 'C:\\会议\\另一场';

let mounted: TestRenderer.ReactTestRenderer | null = null;

/** 两行注册表：在录的那一场 + 另一个工作区（能切过去看详情的那一个）。 */
function wsList(over: Partial<SessionsResponse> = {}): SessionsResponse {
  const row = (dir: string, over2: Partial<SessionRow> = {}): SessionRow => ({
    dir,
    session_id: dir === SESSION_DIR ? '跟老王聊周会' : '另一场',
    dir_exists: true,
    kind: 'empty',
    suspected: false,
    state: dir === SESSION_DIR ? 'recording' : null,
    date: null,
    mode: null,
    segments: [],
    has_offline: false,
    added_at: T('20:00:00'),
    last_opened_at: T('20:00:00'),
    ...over2,
  });
  return {
    sessions: [row(SESSION_DIR), row(OTHER_DIR, { kind: 'meeting', state: 'ended' })],
    recent: SESSION_DIR,
    post_jobs: [],
    ...over,
  };
}

function fakeHeartbeatClient(): HeartbeatClient {
  return {
    setIntervalFromSnapshot: () => undefined,
    intervalSeconds: () => 10,
    tick: async () => ({
      kind: 'error',
      error: new Error('这一档不轮询心跳'),
      status: '失联',
    }),
    start: () => undefined,
    stop: () => undefined,
    running: () => false,
  } as unknown as HeartbeatClient;
}

async function mountLive(snapshot: Snapshot): Promise<TestRenderer.ReactTestRenderer> {
  (globalThis as unknown as { window: unknown }).window = {
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  };
  (globalThis as unknown as { document: unknown }).document = { body: {} };
  createHeartbeatClientMock.mockImplementation(() => fakeHeartbeatClient());
  fetchSessionsMock.mockResolvedValue(wsList());

  const source = {
    use(handlers: EventsClientHandlers): () => void {
      handlers.onSnapshot?.(snapshot, { resync: false, dropped: 0 });
      return () => undefined;
    },
  };
  let renderer!: TestRenderer.ReactTestRenderer;
  await act(async () => {
    renderer = TestRenderer.create(
      createElement(LiveView, { snapshot, events: source as never }),
    );
  });
  mounted = renderer;
  return renderer;
}

/** 一棵子树里的全部文字（嵌套字符串拼起来）。 */
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

function byClass(
  renderer: TestRenderer.ReactTestRenderer,
  className: string,
): TestRenderer.ReactTestInstance[] {
  return renderer.root.findAll((node) =>
    `${node.props.className ?? ''}`.split(' ').includes(className),
  );
}

async function press(node: TestRenderer.ReactTestInstance): Promise<void> {
  await act(async () => {
    node.props.onClick();
  });
}

afterEach(async () => {
  if (mounted !== null) {
    const renderer = mounted;
    mounted = null;
    await act(async () => renderer.unmount());
  }
  createHeartbeatClientMock.mockReset();
  fetchSessionsMock.mockReset();
  postActionMock.mockReset();
});

beforeEach(() => {
  // 动作端点的缺省替身：成功体（workspace_open 的成功形状由断言按名字与参数看）。
  postActionMock.mockResolvedValue({ kind: 'ok', ok: true, revision: 8 });
});

describe('会中屏的常驻工作区栏（d61829f1）', () => {
  it('栏存在：清单、添加工作区、忙闲一格都在（与工作台同一条 home__list）', async () => {
    const renderer = await mountLive(makeSnapshot());

    expect(byClass(renderer, 'home__list'), '会中屏该有那条常驻工作区栏').toHaveLength(1);
    expect(byClass(renderer, 'home__new').map((node) => textOf(node))).toContain('添加工作区');
    expect(renderer.root.findAll((node) => node.props.role === 'option')).toHaveLength(2);
    // 忙闲一格有两处：会中顶栏右上那一只是旧的（R31），栏底那一只是这条栏自带的
    expect(byClass(renderer, 'gpucell')).toHaveLength(2);
  });

  it('点别一行：发出既有的 workspace_open（带那一行的 dir），成功后详情切过去', async () => {
    const renderer = await mountLive(makeSnapshot());

    const otherRow = renderer.root
      .findAll((node) => node.props.role === 'option')
      .find((node) => textOf(node).includes(OTHER_DIR));
    expect(otherRow).toBeDefined();
    await press(otherRow as TestRenderer.ReactTestInstance);

    expect(postActionMock).toHaveBeenCalledWith('workspace_open', { dir: OTHER_DIR });
    // 成功后详情切过去：小详情说的是另一场（选区跟着走）
    const detail = renderer.root.findAll(
      (node) => node.props['aria-label'] === '选中工作区详情',
    );
    expect(detail).toHaveLength(1);
    expect(textOf(detail[0] as TestRenderer.ReactTestInstance)).toContain('另一场');
  });

  it('在录时按「开始录音」：不发任何请求，拒的话指出在录的那一场', async () => {
    const renderer = await mountLive(makeSnapshot());

    // 清单默认选中在录那一场（recent），小详情里那颗「继续录音」按下去
    const startButton = renderer.root.find(
      (node) => node.type === 'button' && textOf(node) === '继续录音',
    );
    await press(startButton);

    expect(postActionMock).not.toHaveBeenCalled();
    const reject = byClass(renderer, 'wsbar__reject');
    expect(reject, '按下即有拒绝的话（不停在「没反应」）').toHaveLength(1);
    const text = textOf(reject[0] as TestRenderer.ReactTestInstance);
    expect(text).toContain('已经有一场在录');
    expect(text).toContain('跟老王聊周会');
    expect(text).toContain('已录 73 分钟');
    expect(text).toContain('同一时刻只录一场');
  });

  it('选中行已是在录行时被拒：拒块不摆「打开那一场」（选区就在这一行，按下只会空转）', async () => {
    const renderer = await mountLive(makeSnapshot());

    // 默认选区即 recent＝在录那一行（review-1 m3 的那一段）：按「继续录音」照旧如实拒
    const startButton = renderer.root.find(
      (node) => node.type === 'button' && textOf(node) === '继续录音',
    );
    await press(startButton);
    expect(byClass(renderer, 'wsbar__reject'), '拒的话照旧现身').toHaveLength(1);

    // 但那只「打开那一场」不摆：选区已在这一行上，按下只会撞 switchWorkspace 的早退
    expect(
      renderer.root.findAll(
        (node) => node.type === 'button' && textOf(node).startsWith('打开那一场'),
      ),
      '选区＝在录行时不摆这只没有真实去处的钮',
    ).toHaveLength(0);
  });

  it('切到别场再按「开始录音」同样如实拒；「打开那一场」把选区切回在录那一行', async () => {
    const renderer = await mountLive(makeSnapshot());

    // 先切到另一场（发出一次 workspace_open），清掉计数再看拒绝那一档
    const otherRow = renderer.root
      .findAll((node) => node.props.role === 'option')
      .find((node) => textOf(node).includes(OTHER_DIR));
    await press(otherRow as TestRenderer.ReactTestInstance);
    postActionMock.mockClear();

    // 另一场的小详情里那颗「继续录音」按下去：拒，一个请求都不发
    const startButton = renderer.root.find(
      (node) => node.type === 'button' && textOf(node) === '继续录音',
    );
    await press(startButton);
    expect(postActionMock).not.toHaveBeenCalled();
    expect(byClass(renderer, 'wsbar__reject')).toHaveLength(1);

    // 「打开那一场」：把选区切回在录那一行（发的是它自己的 workspace_open）
    const openButton = renderer.root.find(
      (node) => node.type === 'button' && textOf(node).startsWith('打开那一场'),
    );
    await press(openButton);
    expect(postActionMock).toHaveBeenCalledWith('workspace_open', { dir: SESSION_DIR });
  });
});
