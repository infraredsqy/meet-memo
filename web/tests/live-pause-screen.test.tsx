import { describe, expect, it, vi, afterEach } from 'vitest';
import { createElement } from 'react';
import TestRenderer, { act } from 'react-test-renderer';

import {
  createEventsClient,
  createHeartbeatClient,
  type EventsClient,
  type EventsClientHandlers,
  type HeartbeatClient,
} from '../src/api';
import { LiveView } from '../src/App';
import type { Snapshot } from '../src/contract';
import { makeSnapshot, T } from './live-fixtures';

/**
 * 会中那一屏的**渲染层**断言（复审-1 m2 的第二条）：暂停态下告警区里真有那条横带、
 * 顶栏那颗真写作「继续」。
 *
 * 手法照 `settings-drawer.test.tsx` / `home-mount.test.tsx`：`react-test-renderer`
 * 在 node 环境里真挂载，**不起浏览器**（vitest.config.ts 的 environment 就是 node）。
 * 服务那两侧（SSE / 心跳）在这一档里是替身 —— 用例自己把一份快照塞进 `onSnapshot`，
 * 这正是「界面画什么只由服务推来的那一份决定」的验证方式。
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

const createEventsClientMock = vi.mocked(createEventsClient);
const createHeartbeatClientMock = vi.mocked(createHeartbeatClient);

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

function fakeEventsClient(handlers: EventsClientHandlers = {}): EventsClient {
  captured.handlers = handlers;
  return {
    connect: () => undefined,
    close: () => undefined,
    state: () => 'open',
    resyncCount: () => 0,
    parse: () => {
      throw new Error('这一档不走同步解析那条读法');
    },
  } as unknown as EventsClient;
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

/** 挂载会中那一屏，再把一份快照推给它（首帧之前它只画占位）。 */
async function mountLive(snapshot: Snapshot): Promise<TestRenderer.ReactTestRenderer> {
  (globalThis as unknown as { window: unknown }).window = {
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  };
  (globalThis as unknown as { document: unknown }).document = { body: {} };
  createEventsClientMock.mockImplementation((handlers) => fakeEventsClient(handlers));
  createHeartbeatClientMock.mockImplementation(() => fakeHeartbeatClient());

  let renderer!: TestRenderer.ReactTestRenderer;
  await act(async () => {
    renderer = TestRenderer.create(createElement(LiveView));
  });
  await act(async () => {
    captured.handlers?.onSnapshot?.(snapshot, { resync: false, dropped: 0 });
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

afterEach(async () => {
  if (mounted !== null) {
    const renderer = mounted;
    mounted = null;
    await act(async () => renderer.unmount());
  }
  captured.handlers = null;
  createEventsClientMock.mockReset();
  createHeartbeatClientMock.mockReset();
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

  it('顶栏那颗按钮写作「继续」，按下去发的是 resume 那一档', async () => {
    const renderer = await mountLive(pausedSnapshot());

    const button = byClass(renderer, 'btn btn--pause');
    expect(button, '顶栏该有一颗 .btn--pause').toHaveLength(1);
    expect(textOf(button[0] as TestRenderer.ReactTestInstance)).toBe('继续');
    // `aria-pressed` 就是这一格的「暂停中」身份：界面没有自己记一份状态，读的是快照
    expect((button[0] as TestRenderer.ReactTestInstance).props['aria-pressed']).toBe(true);
  });

  it('没暂停那一档：横带不出现，同一颗按钮写作「暂停」', async () => {
    const renderer = await mountLive(makeSnapshot());

    expect(byClass(renderer, 'alert alert--paused')).toHaveLength(0);
    const button = byClass(renderer, 'btn btn--pause');
    expect(button, '录音态顶栏那颗照样在，只是字不同').toHaveLength(1);
    expect(textOf(button[0] as TestRenderer.ReactTestInstance)).toBe('暂停');
    expect((button[0] as TestRenderer.ReactTestInstance).props['aria-pressed']).toBe(false);
  });
});
