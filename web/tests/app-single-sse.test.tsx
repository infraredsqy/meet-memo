import { describe, expect, it, vi } from 'vitest';
import { createElement, StrictMode } from 'react';
import TestRenderer, { act } from 'react-test-renderer';

import { App } from '../src/App';
import { makeSnapshot } from './live-fixtures';
import type { Snapshot } from '../src/contract';

/**
 * 那条钉子（web-homegate-dual-sse）：任一时刻全页**恰一个** `EventSource`。
 *
 * 手法照 `live-pause-screen.test.tsx`：`react-test-renderer` 在 node 里真挂载
 * `App`（含 gate 工作台 ↔ 会中两屏），把全局 `EventSource` 换成计数替身，
 * 推 idle（工作台）与 recording（会中）快照来回切 —— 子组件挂了换、换了挂，
 * 构造次数必须停在 1：连接本体归 `App` 顶层，跟着子组件生命周期走就是错的。
 *
 * 不发声、不连网：音频走懒建（node 下没有 `AudioContext`，工厂给 null），
 * 取数都打在替身 fetch 上（各端点回 `ok: true` + 空清单）。
 */

type Listener = (event: { data: string }) => void;

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readonly url: string;
  /** 替身存的 close 登记：StrictMode 双挂载用例按「曾 open 的、现在还开着」口径判。 */
  closed = false;
  readonly listeners = new Map<string, Listener[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(name: string, listener: Listener): void {
    const bucket = this.listeners.get(name) ?? [];
    bucket.push(listener);
    this.listeners.set(name, bucket);
  }

  onopen(): void {}
  onerror(): void {}
  close(): void {
    this.closed = true;
  }

  /** 推一帧（照服务侧的 `data:` 正文走同一串监听）。 */
  dispatch(name: string, data: string): void {
    for (const listener of this.listeners.get(name) ?? []) {
      listener({ data });
    }
  }
}

/** 两份判屏快照：idle（→ 工作台）与 recording（→ 会中）。 */
function idleSnapshot(): Snapshot {
  return makeSnapshot({ top_state: 'idle', session: null });
}
function recSnapshot(): Snapshot {
  return makeSnapshot();
}

/** 把全局那几样换空壳 + 替身（`EventSource` 计数、`fetch` 有问必答）。 */
function stubGlobals(): void {
  (globalThis as unknown as { window: unknown }).window = {
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  };
  (globalThis as unknown as { document: unknown }).document = { body: {} };
  (globalThis as unknown as { EventSource?: unknown }).EventSource =
    FakeEventSource as unknown as new (url: string) => EventSource;
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            sessions: [],
            recent: null,
            post_jobs: [],
            service: 'meeting',
            server_time: '2026-09-21T20:00:00+08:00',
            revision: 0,
            top_state: 'idle',
            recording_alive: null,
            seconds_since_last_tick: null,
            capture_gaps: { total_count: 0, total_seconds: 0, recent: [] },
          }),
      }),
    ),
  );
}

describe('唯一一条 SSE 连接（web-homegate-dual-sse 钉子）', () => {
  it('两屏挂载切换来回，EventSource 构造次数 === 1', async () => {
    FakeEventSource.instances = [];
    stubGlobals();

    let renderer!: TestRenderer.ReactTestRenderer;
    await act(async () => {
      renderer = TestRenderer.create(createElement(App));
    });
    // 替身源上的 snapshot 监听就是服务帧的那条路。
    const source = FakeEventSource.instances[0] ?? (() => { throw new Error("App 没建 EventSource"); })();
    const push = async (snapshot: Snapshot): Promise<void> => {
      await act(async () => {
        source.dispatch('snapshot', JSON.stringify(snapshot));
      });
    };
    await push(idleSnapshot()); // → 工作台
    await push(recSnapshot()); // → 会中
    await push(idleSnapshot()); // → 工作台
    await push(recSnapshot()); // → 会中

    expect(FakeEventSource.instances, '连接本体归 App 顶层：切屏不重建').toHaveLength(1);

    await act(async () => renderer.unmount());
    vi.unstubAllGlobals();
  });

  it('首份快照未到之前，gate 恒画「正在接服务…」占位', async () => {
    FakeEventSource.instances = [];
    stubGlobals();

    let renderer!: TestRenderer.ReactTestRenderer;
    await act(async () => {
      renderer = TestRenderer.create(createElement(App));
    });
    expect(FakeEventSource.instances).toHaveLength(1);
    const text = (function walk(current: TestRenderer.ReactTestInstance): string {
      const parts: string[] = [];
      for (const child of current.children) {
        if (typeof child === 'string') {
          parts.push(child);
          continue;
        }
        parts.push(walk(child));
      }
      return parts.join(' ');
    })(renderer.root);
    expect(text).toContain('正在接服务…');

    await act(async () => renderer.unmount());
    vi.unstubAllGlobals();
  });
});

describe('客户端连/断的生命周期（StrictMode 钉子）', () => {
  it('StrictMode 双挂载下：cleanup 后旧 EventSource 已 close，任一时刻 open 的 ≤1', async () => {
    FakeEventSource.instances = [];
    stubGlobals();

    // App 顶层持有 client（useMemo createEventsSource + useEffect connect/close），
    // StrictMode 把「挂在 App 顶层持有 client 的那一层」整个包进去。
    let renderer!: TestRenderer.ReactTestRenderer;
    await act(async () => {
      renderer = TestRenderer.create(createElement(StrictMode, null, createElement(App)));
    });

    const openSources = (): FakeEventSource[] =>
      FakeEventSource.instances.filter((instance) => !instance.closed);
    // StrictMode 的模拟双挂载（dev 行为）：每轮 mount 都 connect 建一条，cleanup 都 close。
    // 判据是处置干净 —— 曾建的每一条要不已 close；任一时刻开着的至多一条。
    // （fake 环境 StrictMode 双行为如有出入，以本例最后的两档断言为口径。）
    const constructed = FakeEventSource.instances.length;
    if (constructed > 1) {
      // 真发生双挂载：第一轮那条必须被 cleanup close 掉，只留最后一条开着。
      const exceptLast = FakeEventSource.instances.slice(0, -1);
      for (const instance of exceptLast) {
        expect(instance.closed, '双挂载的第一条必须被 cleanup close').toBe(true);
      }
      expect(openSources(), '最终 open 的 EventSource 恰 ≤1').toHaveLength(1);
    }
    expect(openSources().length, '任一时刻 open 的 EventSource ≤1').toBeLessThanOrEqual(1);
    expect(constructed, 'StrictMode 下至少建立起一条').toBeGreaterThanOrEqual(1);

    await act(async () => renderer.unmount());
    expect(FakeEventSource.instances.every((instance) => instance.closed)).toBe(true);
    vi.unstubAllGlobals();
  });
});

describe('转手接线的行为不变（失联横幅 / onError 链）', () => {
  it('recording 快照进来画会中那一屏；失联档没有快照历史也不冒进', async () => {
    FakeEventSource.instances = [];
    stubGlobals();

    let renderer!: TestRenderer.ReactTestRenderer;
    await act(async () => {
      renderer = TestRenderer.create(createElement(App));
    });
    const source = FakeEventSource.instances[0] ?? (() => { throw new Error("App 没建 EventSource"); })();
    await act(async () => {
      source.dispatch('snapshot', JSON.stringify(recSnapshot()));
    });
    const classNames = (root: TestRenderer.ReactTestInstance, cls: string): number =>
      root.findAll((node) => `${node.props.className ?? ''}`.split(/\s+/).includes(cls)).length;
    expect(
      classNames(renderer.root, 'transcript-block'),
      'recording 快照 → 会中那一屏有实时转写区',
    ).toBeGreaterThan(0);

    // onError（帧形状漂）→ 失败块走 unclassified 那一档（不是编出来的信封）。
    await act(async () => {
      source.dispatch('transcript', '不是 JSON 的帧');
    });
    expect(classNames(renderer.root, 'fail')).toBeGreaterThan(0);
    const text = (function walk(current: TestRenderer.ReactTestInstance): string {
      const parts: string[] = [];
      for (const child of current.children) {
        if (typeof child === 'string') {
          parts.push(child);
          continue;
        }
        parts.push(walk(child));
      }
      return parts.join(' ');
    })(renderer.root);
    expect(text).toContain('这一档没有失败信封、也没有 code');
    expect(text).not.toContain('invalid_setting');

    await act(async () => renderer.unmount());
    vi.unstubAllGlobals();
  });
});

