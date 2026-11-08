import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest';
import type { SessionStatus } from '../src/contract';
import { createElement } from 'react';
import TestRenderer, { act } from 'react-test-renderer';

/** 一格「没跑过」的转码工序读数（`transcode` 格的空档形状）。 */
function transcodeIdle(): SessionStatus['transcode'] {
  return {
    state: 'idle',
    in_process: false,
    started_at: null,
    finished_at: null,
    elapsed_seconds: null,
    current_source: null,
    sources: [],
  };
}


import {
  createEventsClient,
  createHeartbeatClient,
  postAction,
  type EventsClientHandlers,
  type HeartbeatClient,
} from '../src/api';
import { fetchSessions } from '../src/api/sessions';
import { LiveView } from '../src/live/liveview/LiveView';
import type { Snapshot } from '../src/contract';
import { makeSnapshot, makeSource, T } from './live-fixtures';

/**
 * 会中那一屏的**渲染层**断言：暂停横带照旧极其显眼，收音的四组合直切
 * （`c9f5d04d`）收在顶栏一处 —— 旧的两颗入口（顶栏暂停按钮、电平卡上的
 * 「关掉 / 打开」）不再以原形出现，按下组合的受理反馈即时点亮。
 *
 * 手法照 `settings-drawer.test.tsx` / `home-mount.test.tsx`：`react-test-renderer`
 * 在 node 环境里真挂载，**不起浏览器**（vitest.config.ts 的 environment 就是 node）。
 * 服务那两侧（SSE / 心跳 / 动作端点）在这一档里是替身 —— 用例自己把快照塞进
 * `onSnapshot`、数 `postAction` 的调用，这正是「界面画什么只由服务推来的那一份决定」
 * 的验证方式。
 *
 * 挂载 `LiveView` 要给它两样 node 里没有的全局：`window`（自动播放授权那两个监听）
 * 与 `document.body`（页身类名）。这里只补这两样空壳 —— 不引 jsdom、不装 DOM 环境，
 * 别的东西（音频、事件流）一律走替身：**这个文件不发声、不连网**。
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

// 会后在飞读数（review-1 m2）：LiveView 挂上就按心跳节奏取 `GET /api/sessions`，
// 替身打在同一条边界上 —— 这个文件不连网。
vi.mock('../src/api/sessions', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/sessions')>();
  return { ...actual, fetchSessions: vi.fn() };
});

// 会议资料区（160d1a78）：会中那块面板挂上即取清单 —— 替身给空清单，这个文件不连网。
vi.mock('../src/api/materials', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/materials')>();
  return {
    ...actual,
    fetchMaterials: vi.fn(async () => ({ dir: '', materials_dir: '', items: [] })),
  };
});

const createEventsClientMock = vi.mocked(createEventsClient);
const createHeartbeatClientMock = vi.mocked(createHeartbeatClient);
const fetchSessionsMock = vi.mocked(fetchSessions);
const postActionMock = vi.mocked(postAction);

/** 捕获 SSE 那一侧的 handlers（`LiveView` 在 effect 里建连，回调就在这一刻交出来）。 */
const captured: { handlers: EventsClientHandlers | null } = { handlers: null };
let mounted: TestRenderer.ReactTestRenderer | null = null;

/** 一份「暂停中」的快照（值照 `live-fixtures` 那一份，只把 `paused` 翻过来）。 */
function pausedSnapshot(): Snapshot {
  return makeSnapshot({
    session: {
      id: '跟老王聊周会',
      dir: 'C:\\会议\\跟老王聊周会',
      started_at: T('20:45:20'),
      mode: 'both',
      elapsed_seconds: 4354,
      frozen: false,
      paused: true,
    },
  });
}

/** 一份「远端被关掉」的快照（仅本地那一档）。 */
function localOnlySnapshot(): Snapshot {
  return makeSnapshot({
    sources: [makeSource('mic'), makeSource('loopback', { health: 'stopped' })],
  });
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

/**
 * 挂载会中那一屏。SSE 那条唯一连接现在在 `App` 顶层（web-homegate-dual-sse），
 * `LiveView` 只吃 `events` 转手源 —— 用例给一份订阅即收快照的假源，把快照喂进去。
 */
async function mountLive(snapshot: Snapshot): Promise<TestRenderer.ReactTestRenderer> {
  (globalThis as unknown as { window: unknown }).window = {
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  };
  (globalThis as unknown as { document: unknown }).document = { body: {} };
  createHeartbeatClientMock.mockImplementation(() => fakeHeartbeatClient());

  const source = {
    use(handlers: EventsClientHandlers): () => void {
      captured.handlers = handlers;
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

/** 一棵子树里的全部文字（嵌套字符串拼起来），照 `settings-drawer.test.tsx` 那一份。 */
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
  return renderer.root.findAll((node) => `${node.props.className ?? ''}` === className);
}

/** 顶栏那组四组合直切（`aria-label="收音组合"`）。 */
function comboGroup(
  renderer: TestRenderer.ReactTestRenderer,
): TestRenderer.ReactTestInstance {
  const groups = renderer.root.findAll(
    (node) => node.props.role === 'group' && node.props['aria-label'] === '收音组合',
  );
  expect(groups, '顶栏该有一组「收音组合」').toHaveLength(1);
  return groups[0] as TestRenderer.ReactTestInstance;
}

/** 组里的四个档位按钮（渲染次序 = `COMBO_ORDER`）。 */
function comboItems(group: TestRenderer.ReactTestInstance): TestRenderer.ReactTestInstance[] {
  return group.findAll((node) => `${node.props.className ?? ''}`.startsWith('seg__item'));
}

/** 某一档此刻亮不亮。 */
function comboLit(
  group: TestRenderer.ReactTestInstance,
  label: string,
): boolean {
  const item = comboItems(group).find((node) => textOf(node) === label);
  expect(item, `四档里该有「${label}」这一档`).toBeDefined();
  return `${(item as TestRenderer.ReactTestInstance).props.className}`.includes('seg__item--on');
}

/** 按下某一档（真按：走 onClick，受理反馈与动作调用都发生在里面）。 */
async function pressCombo(
  renderer: TestRenderer.ReactTestRenderer,
  label: string,
): Promise<void> {
  const group = comboGroup(renderer);
  const item = comboItems(group).find((node) => textOf(node) === label);
  expect(item, `四档里该有「${label}」这一档`).toBeDefined();
  await act(async () => {
    (item as TestRenderer.ReactTestInstance).props.onClick();
  });
}

afterEach(async () => {
  if (mounted !== null) {
    const renderer = mounted;
    mounted = null;
    await act(async () => renderer.unmount());
  }
  captured.handlers = null;
  createEventsClientMock.mockReset();
  createHeartbeatClientMock.mockReset();
  fetchSessionsMock.mockReset();
  postActionMock.mockReset();
});

beforeEach(() => {
  // 会后在飞读数的缺省替身：没有会后的活儿（要特定读数自己覆盖）。
  // LiveView 挂上就取 `GET /api/sessions`（review-1 m2），不能让它在替身空转时炸掉。
  fetchSessionsMock.mockResolvedValue({ sessions: [], recent: null, post_jobs: [] });
  // 动作端点的缺省替身：成功体（pause / resume / set_source 的成功形状）。
  postActionMock.mockResolvedValue({ kind: 'ok', ok: true, revision: 8 });
});

describe('暂停态那一屏（渲染层）', () => {
  it('告警区里真有「本场已暂停」那条横带', async () => {
    const renderer = await mountLive(pausedSnapshot());

    const banner = byClass(renderer, 'alert alert--paused');
    expect(banner, '暂停态下该有一条 .alert--paused').toHaveLength(1);
    const text = textOf(banner[0] as TestRenderer.ReactTestInstance);
    expect(text).toContain('本场已暂停');
    expect(text).toContain('继续');
  });

  it('四组合直切亮在「全停」，整组 data-combo 也落在那一档', async () => {
    const renderer = await mountLive(pausedSnapshot());

    const group = comboGroup(renderer);
    expect(group.props['data-combo']).toBe('paused');
    expect(comboItems(group).map(textOf)).toEqual(['两路都录', '仅本地', '仅远端', '全停']);
    expect(comboLit(group, '全停')).toBe(true);
    expect(comboLit(group, '两路都录')).toBe(false);
    // 亮着的那一档，按下它一个请求都不发（幂等重按）
    postActionMock.mockClear();
    await pressCombo(renderer, '全停');
    expect(postActionMock).not.toHaveBeenCalled();
  });

  it('旧的顶栏暂停按钮不再出现（职能全由四组合承担）', async () => {
    const renderer = await mountLive(pausedSnapshot());
    expect(byClass(renderer, 'btn btn--pause'), '.btn--pause 已随四组合收编退场').toHaveLength(0);
  });
});

describe('录音态那一屏（渲染层）', () => {
  it('横带不出现；四组合亮在「两路都录」，重按它一个请求都不发', async () => {
    const renderer = await mountLive(makeSnapshot());

    expect(byClass(renderer, 'alert alert--paused')).toHaveLength(0);
    const group = comboGroup(renderer);
    expect(group.props['data-combo']).toBe('both');
    expect(comboLit(group, '两路都录')).toBe(true);

    postActionMock.mockClear();
    await pressCombo(renderer, '两路都录');
    expect(postActionMock).not.toHaveBeenCalled();
  });

  it('按下「仅本地」：立即只发一条 set_source(loopback, false)，受理反馈当下点亮那一档', async () => {
    const renderer = await mountLive(makeSnapshot());

    await pressCombo(renderer, '仅本地');

    expect(postActionMock).toHaveBeenCalledTimes(1);
    expect(postActionMock).toHaveBeenCalledWith('set_source', {
      source: 'loopback',
      active: false,
    });
    // 快照还没回来：高亮已经在目标档上（按下即有反馈，不读成「没反应」）
    const group = comboGroup(renderer);
    expect(comboLit(group, '仅本地')).toBe(true);
    expect(comboLit(group, '两路都录')).toBe(false);
  });

  it('推来的快照真落到那一档：高亮继续亮在「仅本地」（落地收摊）', async () => {
    const renderer = await mountLive(makeSnapshot());
    await pressCombo(renderer, '仅本地');

    await act(async () => {
      captured.handlers?.onSnapshot?.(localOnlySnapshot(), { resync: false, dropped: 0 });
    });

    const group = comboGroup(renderer);
    expect(group.props['data-combo']).toBe('local');
    expect(comboLit(group, '仅本地')).toBe(true);
  });

  it('电平卡上不再有「关掉 / 打开」那颗按钮：通道卡里一颗按钮都没有', async () => {
    const renderer = await mountLive(localOnlySnapshot());

    const cards = renderer.root.findAll((node) =>
      `${node.props.className ?? ''}`.split(' ').includes('m-ch'),
    );
    expect(cards, '两路各一张通道卡').toHaveLength(2);
    for (const card of cards) {
      const buttons = (card as TestRenderer.ReactTestInstance).findAll(
        (node) => node.type === 'button',
      );
      expect(buttons, '通道卡里不该有任何按钮（开关收进了顶栏四组合）').toHaveLength(0);
    }
    // 快照自己仍然是「仅本地」的那一档：健康位照旧从快照读
    const group = comboGroup(renderer);
    expect(comboLit(group, '仅本地')).toBe(true);
  });
});

describe('全局一格的会后一半（review-1 m2）：post 在飞读的是 GET /api/sessions', () => {
  /**
   * d61829f1 起「收尾窗口」不再挂在会中这一屏（ended 任意相都回工作台，
   * 落盘的进度归工作台详情里那一小块）—— 这一屏上 gpucell 的忙闲判据照旧：
   * 录着（含告警）就是忙，`post_jobs` 的在飞趟补上会后那一半读数；
   * 常驻工作区栏的栏底还有一只同款（两处读同一条 `GET /api/sessions`）。
   */
  it('post 在飞时全局格答「忙碌」，小注说清跑的是哪一趟（顶栏与栏底两处一致）', async () => {
    fetchSessionsMock.mockResolvedValue({
      sessions: [],
      recent: null,
      post_jobs: [{ kind: 'offline', dir: 'C:\\会议\\跑着', started_at: T('22:10:00') }],
    });
    const renderer = await mountLive(makeSnapshot());

    const cells = renderer.root.findAll((node) =>
      `${node.props.className ?? ''}`.startsWith('gpucell'),
    );
    expect(cells, '顶栏一只 + 常驻工作区栏栏底一只').toHaveLength(2);
    for (const cell of cells) {
      expect((cell as TestRenderer.ReactTestInstance).props['data-busy']).toBe('true');
      expect(String((cell as TestRenderer.ReactTestInstance).props.title)).toContain(
        '会后那趟在跑',
      );
    }
  });
});
