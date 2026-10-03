import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest';
import { createElement } from 'react';
import TestRenderer, { act } from 'react-test-renderer';
import { failureFrom } from '../src/failure';
import { ActionFailedError, ActionForbiddenError, ActionProtocolError } from '../src/api';

/**
 * 壳页把「动作没成」分几档画 —— `docs/contract.md` 6.4 要求 403 与失败信封**分开**。
 *
 * 复审抓到的那一格：403 曾被打成 `code: 'invalid_setting'` 塞进失败块里显示，
 * 而那个 `code` 服务在中间件那条路径上根本不会回 —— 画出来是界面自己编的一档。
 * 这里把分档口径钉住：403 有自己的 `kind`、自己带状态码与原文，**不冒充任何 `code`**。
 *
 * 同一口径的另一半（web-client-failure-fabricated-code）：兜底档也**没有信封** ——
 * 服务根本没给，界面不替它编 `code: 'invalid_setting'`；SSE onError 的错误同理。
 */

describe('动作失败的画法分档（6.4：403 与失败块分开）', () => {
  it('403：走 forbidden 那一档，不折成任何 ErrorCode', () => {
    const view = failureFrom(new ActionForbiddenError(403, '缺少必需请求头'));
    expect(view.kind).toBe('forbidden');
    if (view.kind !== 'forbidden') throw new Error('分支错了');
    expect(view.status).toBe(403);
    expect(view.text).toBe('缺少必需请求头');
    // 这一档**没有** code 这个字段 —— 有的话就说明它又被折回了失败块。
    expect('code' in view).toBe(false);
    expect('detail' in view).toBe(false);
  });

  it('失败信封：走 envelope 那一档，code 与 detail 按第 4 章读', () => {
    const view = failureFrom(
      new ActionFailedError(409, 'already_recording', '已经有一场在录', {
        session_id: '260921',
        dir: 'C:\\out\\260921',
        elapsed_seconds: 12,
      } as never),
    );
    expect(view.kind).toBe('envelope');
    if (view.kind !== 'envelope') throw new Error('分支错了');
    expect(view.code).toBe('already_recording');
    expect(view.message).toBe('已经有一场在录');
    // 三个 detail 键按「有哪个键显示哪个键」逐条摊平。
    expect(view.detail).toHaveLength(3);
    expect(view.detail.join('\n')).toContain('session_id：260921');
  });

  it('失败信封的 failed_steps 逐条列出来（数组里有对象那一支）', () => {
    const view = failureFrom(
      new ActionFailedError(409, 'draining', '还在收尾', {
        failed_steps: [{ id: 'post', error: '钩子抛错' }],
      } as never),
    );
    if (view.kind !== 'envelope') throw new Error('分支错了');
    expect(view.detail).toEqual(['failed_steps.post：钩子抛错']);
  });

  it('别的异常兜底归 unclassified：没有失败信封、没有 code、没有 status（6.4）', () => {
    for (const error of [
      new ActionProtocolError(200, '<html>', new Error('不是 JSON')),
      new Error('网络断了'),
      '不是 Error 的抛出物',
    ]) {
      const view = failureFrom(error);
      expect(view.kind).toBe('unclassified');
      if (view.kind !== 'unclassified') throw new Error('分支错了');
      expect(view.text.length).toBeGreaterThan(0);
      // 这一档**没有** code / status —— 有的话就说明界面又替服务编了信封。
      expect('code' in view).toBe(false);
      expect('status' in view).toBe(false);
    }
  });
});

// ---------------------------------------------------------------------------
// SSE onError → 失败块那条转手链（web-client-failure-fabricated-code）
// ---------------------------------------------------------------------------

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

/** 捕获 SSE 那一侧的 handlers（LiveView 在 effect 里建连，回调就在这一刻交出来）。 */
const captured: { handlers: import('../src/api').EventsClientHandlers | null } = { handlers: null };

afterEach(async () => {
  const api = await import('../src/api');
  const sessions = await import('../src/api/sessions');
  vi.mocked(api.createEventsClient).mockReset();
  vi.mocked(api.createHeartbeatClient).mockReset();
  vi.mocked(api.fetchSummaries).mockReset();
  vi.mocked(api.postAction).mockReset();
  vi.mocked(sessions.fetchSessions).mockReset();
  captured.handlers = null;
});

describe('SSE onError 的失败块（渲染层转手）', () => {
  it('onError 的错误没有失败信封，失败块如实显示原文并说明这一档没有 code', async () => {
    const { LiveView } = await import('../src/App');
    const { createHeartbeatClient } = await import('../src/api');
    const { fetchSessions } = await import('../src/api/sessions');
    const { makeSnapshot } = await import('./live-fixtures');

    vi.mocked(createHeartbeatClient).mockImplementation(
      () =>
        ({
          setIntervalFromSnapshot: () => undefined,
          intervalSeconds: () => 10,
          tick: async () => ({ kind: 'error', error: new Error('不轮询'), status: '失联' }),
          start: () => undefined,
          stop: () => undefined,
          running: () => false,
        }) as unknown as import('../src/api').HeartbeatClient,
    );
    (globalThis as unknown as { window: unknown }).window = {
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    };
    (globalThis as unknown as { document: unknown }).document = { body: {} };
    vi.mocked(fetchSessions).mockResolvedValue({
      sessions: [],
      recent: null,
      post_job: null,
    });

    const snapshot = makeSnapshot();
    // 源侧转手（`App` 顶层持连接）：用例给一份订阅即交 handlers + 快照的假源。
    const source = {
      use(handlers: import('../src/api').EventsClientHandlers): () => void {
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
    await act(async () => {
      captured.handlers?.onError?.(new Error('通道「levels」的帧形状不符合契约：别这样'));
    });

    const fails = renderer.root.findAll(
      (node) => `${node.props.className ?? ''}` === 'fail',
    );
    expect(fails.length).toBeGreaterThan(0);
    const walk = (current: TestRenderer.ReactTestInstance): string => {
      const parts: string[] = [];
      for (const child of current.children) {
        if (typeof child === 'string') {
          parts.push(child);
          continue;
        }
        parts.push(walk(child));
      }
      return parts.join(' ');
    };
    const text = walk(fails[0] as TestRenderer.ReactTestInstance);
    expect(text).toContain('通道「levels」的帧形状不符合契约：别这样');
    // 这一档没有失败信封、也没有 code：失败块不冒牌出 `code` 芯片。
    expect(text).not.toContain('invalid_setting');
    expect(text).toContain('这一档没有失败信封、也没有 code');

    await act(async () => renderer.unmount());
    vi.mocked(createHeartbeatClient).mockReset();
    vi.mocked(fetchSessions).mockReset();
  });
});
