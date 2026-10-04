import { beforeEach, describe, expect, it, vi, afterEach } from 'vitest';
import { createElement } from 'react';
import TestRenderer, { act } from 'react-test-renderer';
import { WorkspaceHome } from '../src/home/WorkspaceHome';
import {
  createSessionStatusPoller,
  fetchSessions,
  fetchSessionStatus,
  SessionStatusHttpError,
  SessionsUnavailableError,
} from '../src/api/sessions';
import type { OfflineSourceProgress, SessionRow, SessionStatus } from '../src/contract';

/**
 * 会后进度区的挂载用例（免 DOM，手法与 `home-mount.test.tsx` 同源）。
 *
 * 完成判定要的那两条在这里钉住：
 * - **造出来的读数换值、渲染随之变化、不塌**：第 1/10 块 → 第 9/10 块 → 停着一版
 *   running，三次换值都换得动页面上的字与进度条，组件一帧不崩；
 * - **四种行状态各有可识别表现**：字 + 图形两条通道并存，不靠颜色单独承载。
 *
 * 另有两块收在本文件末尾：review-2 C1 的**按钮起跑档**（挂载握着旧终态 → 按下
 * 「生成会后精细稿」→ post_job 出现 → 补跳一跳之内轮询起、读数跟走、终局照旧收敛）
 * 与 review-2 m2 的**重跑重播**（`started_at` 换新的那一跳逐块行整段换掉）。
 *
 * 轮询替身是**手动排程**的（`home-mount.test.tsx` 同一只）：tick 立刻把替身读数交给
 * 组件，不挂真定时器；用例直接喊 `tick()`，不必等周期。替身照真轮询器的路线走 ——
 * 失败那一跳也交回调（错误 tick），`SessionStatusHttpError` / `SessionsUnavailableError`
 * 用真类（组件按 `instanceof` 分档，替身给假类就测不出那一档）。
 */

vi.mock('../src/api/sessions', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/sessions')>();
  const fetchSessionStatus = vi.fn();
  // 组件注册上来的读数回调（轮询循环把每一跳交给它；替身同样交，路线一致）。
  let onTick: ((tick: unknown) => void) | null = null;
  return {
    ...actual,
    fetchSessions: vi.fn(),
    browseDirs: vi.fn(),
    fetchSessionStatus,
    // 每只轮询器都是新造的：替身的 start/stop 记账互不串场（用例各拿各的那只）。
    createSessionStatusPoller: vi.fn((registered: unknown) => {
      onTick = registered as (tick: unknown) => void;
      const deliver = (dir: string): Promise<unknown> =>
        fetchSessionStatus(dir).then(
          (status: SessionStatus) => {
            const result = { kind: 'status', dir, status };
            if (onTick !== null) onTick(result);
            return result;
          },
          (cause: unknown) => {
            const error = cause instanceof Error ? cause : new Error(String(cause));
            const result = { kind: 'error', dir, error };
            if (onTick !== null) onTick(result);
            return result;
          },
        );
      return {
        setIntervalFromSnapshot: vi.fn(),
        intervalSeconds: vi.fn(() => 10),
        tick: vi.fn((dir: string) => deliver(dir)),
        start: vi.fn(),
        stop: vi.fn(),
        running: vi.fn(() => false),
      };
    }),
  };
});

const fetchSessionsMock = vi.mocked(fetchSessions);
const fetchSessionStatusMock = vi.mocked(fetchSessionStatus);
const createPollerMock = vi.mocked(createSessionStatusPoller);

const DIR = 'C:\\会议\\这一场';

function row(over: Partial<SessionRow> = {}): SessionRow {
  return {
    dir: DIR,
    session_id: '这一场',
    dir_exists: true,
    kind: 'meeting',
    suspected: false,
    state: 'done',
    date: '2026-09-21',
    mode: 'both',
    segments: [],
    has_offline: false,
    added_at: '2026-09-21T21:03:00+08:00',
    last_opened_at: '2026-09-21T21:03:00+08:00',
    ...over,
  };
}

function road(over: Partial<OfflineSourceProgress> = {}): OfflineSourceProgress {
  return {
    source: 'mic',
    done: 0,
    total: 10,
    remaining: 10,
    current_chunk: null,
    counts: { transcribed: 0, silent: 0, failed: 0, unjudged: 0 },
    ...over,
  };
}

function status(offline: Partial<SessionStatus['offline']>, post: SessionStatus['post'] = null): SessionStatus {
  return {
    dir: DIR,
    session_id: '这一场',
    kind: 'meeting',
    state: 'done',
    date: '2026-09-21',
    mode: 'both',
    segments: [],
    has_offline: false,
    post,
    speakers: [],
    offline: {
      ledger: 'meeting_offline_progress.jsonl',
      state: 'idle',
      in_process: false,
      started_at: null,
      finished_at: null,
      elapsed_seconds: null,
      current_source: null,
      sources: [road(), road({ source: 'loopback' })],
      ...offline,
    },
  };
}

/** 组件最近造的那只轮询器替身（tick 已把读数交给组件）。 */
function pollerOf(): { tick: (dir: string) => Promise<unknown>; start: ReturnType<typeof vi.fn>; stop: ReturnType<typeof vi.fn> } {
  const results = createPollerMock.mock.results;
  const value = results[results.length - 1]?.value;
  if (value === undefined) throw new Error('组件还没有造轮询器');
  return value as { tick: (dir: string) => Promise<unknown>; start: ReturnType<typeof vi.fn>; stop: ReturnType<typeof vi.fn> };
}

async function mountHome(): Promise<TestRenderer.ReactTestRenderer> {
  let renderer!: TestRenderer.ReactTestRenderer;
  await act(async () => {
    renderer = TestRenderer.create(createElement(WorkspaceHome));
  });
  return renderer;
}

function textsOf(renderer: TestRenderer.ReactTestRenderer, className: string): string[] {
  const parts: string[] = [];
  const walk = (node: TestRenderer.ReactTestInstance): void => {
    if (className === '*' || `${node.props.className ?? ''}`.split(' ').includes(className)) {
      const collect = (current: TestRenderer.ReactTestInstance | { children: unknown[] }): void => {
        for (const child of current.children as unknown[]) {
          if (typeof child === 'string') parts.push(child);
          else if (child !== null && typeof child === 'object') collect(child as TestRenderer.ReactTestInstance);
        }
      };
      collect(node);
    }
    for (const child of node.children) {
      if (child !== null && typeof child === 'object') walk(child as TestRenderer.ReactTestInstance);
    }
  };
  walk(renderer.root);
  return parts;
}

async function feed(): Promise<void> {
  await act(async () => {
    await pollerOf().tick(DIR);
  });
}

beforeEach(() => {
  fetchSessionStatusMock.mockImplementation(async () => status({}));
});

afterEach(() => {
  fetchSessionsMock.mockReset();
  fetchSessionStatusMock.mockReset();
  createPollerMock.mockClear();
});

describe('会后进度区：换值渲染不塌', () => {
  it('第 1/10 块：读数一跳进来，阶段行、进度条分数、逐块行跟着换', async () => {
    fetchSessionsMock.mockResolvedValue({ sessions: [row()], recent: DIR, post_job: null });
    fetchSessionStatusMock.mockImplementation(async () =>
      status({
        state: 'running',
        in_process: true,
        started_at: '2026-09-21T22:10:00+08:00',
        elapsed_seconds: 4.2,
        current_source: 'mic',
        sources: [
          road({ done: 1, current_chunk: 2,
            counts: { transcribed: 1, silent: 0, failed: 0, unjudged: 0 } }),
          road({ source: 'loopback' }),
        ],
      }),
    );
    const renderer = await mountHome();
    await feed(); // 用例自己再喂一跳（同一份替身读数）

    expect(textsOf(renderer, 'wsprog__phase').join(' ')).toContain('正在运行：本地 第 2/10 块');
    const frac = textsOf(renderer, 'wsprog__frac').join(' ');
    expect(frac).toContain('1/10');
    expect(frac).toContain('0/10');
    // 第一跳已处理的一块补成 observed 行（事实如实：档位读不着不编）
    expect(textsOf(renderer, 'wsprog__row').join(' ')).toContain('本地 第 1/10 块 · 已处理');
    // 进度条那格真的把 --p 写上了（组件只摆，长短归样式）
    const fills = renderer.root.findAll((node) => node.props.className === 'wsprog__fill');
    expect(fills).toHaveLength(2);
    expect(String(fills[0]?.props.style?.['--p'])).toBe('10%');
  });

  it('第 9/10 块：同一只组件再喂一跳，读数整块替换、逐块行往下长，不塌', async () => {
    fetchSessionsMock.mockResolvedValue({ sessions: [row()], recent: DIR, post_job: null });
    fetchSessionStatusMock.mockImplementation(async () =>
      status({
        state: 'running',
        in_process: true,
        current_source: 'mic',
        sources: [
          road({ done: 1, counts: { transcribed: 1, silent: 0, failed: 0, unjudged: 0 } }),
          road({ source: 'loopback' }),
        ],
      }),
    );
    const renderer = await mountHome();

    fetchSessionStatusMock.mockImplementation(async () =>
      status({
        state: 'running',
        in_process: true,
        current_source: 'mic',
        sources: [
          road({ done: 9, current_chunk: 10,
            counts: { transcribed: 8, silent: 0, failed: 1, unjudged: 0 } }),
          road({ source: 'loopback', done: 2, current_chunk: 3,
            counts: { transcribed: 2, silent: 0, failed: 0, unjudged: 0 } }),
        ],
      }),
    );
    await feed();

    expect(textsOf(renderer, 'wsprog__phase').join(' ')).toContain('正在运行：本地 第 10/10 块');
    const rows = textsOf(renderer, 'wsprog__row').join('\n');
    // 差值归档：上一跳已有 1 成，这一跳差值 = 成 7 + 败 1 → 第 2–8 块成功、第 9 块失败
    expect(rows).toContain('本地 第 8/10 块 · 成功');
    expect(rows).toContain('本地 第 9/10 块 · 失败');
    expect(rows).toContain('远端 第 1/10 块 · 成功');
    expect(rows).toContain('远端 第 2/10 块 · 成功');
  });

  it('停着一版 running：in_process=false 时阶段行换成「没有任何东西在跑」，不冒充在飞', async () => {
    fetchSessionsMock.mockResolvedValue({ sessions: [row()], recent: DIR, post_job: null });
    fetchSessionStatusMock.mockImplementation(async () =>
      status({
        state: 'running',
        in_process: false,
        elapsed_seconds: null,
        sources: [
          road({ done: 1, counts: { transcribed: 1, silent: 0, failed: 0, unjudged: 0 } }),
          road({ source: 'loopback' }),
        ],
      }),
    );
    const renderer = await mountHome();

    expect(textsOf(renderer, 'wsprog__phase').join(' ')).toContain('磁盘记录为运行中，当前没有进程在运行');
    // 阶段格的形状通道画的是红叉（出事了那一档的形状），转圈留给真在跑的那一档
    const phase = renderer.root.find(
      (node) => `${node.props.className ?? ''}`.split(' ').includes('wsprog__phase'),
    );
    const marks = phase.findAll((node) => `${node.props.className ?? ''}`.startsWith('glyph'));
    expect(marks.map((node) => node.props.className)).toContain('glyph glyph--cross');
  });

  it('展开/收缩：按一下收起（body 不见了）、再按一下展开', async () => {
    fetchSessionsMock.mockResolvedValue({ sessions: [row()], recent: DIR, post_job: null });
    const renderer = await mountHome();

    const toggle = () =>
      renderer.root.find(
        (node) =>
          node.type === 'button' &&
          `${node.props.className ?? ''}`.split(' ').includes('wsprog__toggle'),
      );
    expect(renderer.root.findAll((node) => node.props.className === 'wsprog__body')).toHaveLength(1);
    await act(async () => {
      toggle().props.onClick();
    });
    expect(renderer.root.findAll((node) => node.props.className === 'wsprog__body')).toHaveLength(0);
    expect(toggle().props['aria-expanded']).toBe(false);
    await act(async () => {
      toggle().props.onClick();
    });
    expect(renderer.root.findAll((node) => node.props.className === 'wsprog__body')).toHaveLength(1);
  });
});

describe('行四态与全局一格：可识别、不塌', () => {
  it('四种行状态在列表里各有字与形状（颜色不是唯一通道）', async () => {
    fetchSessionsMock.mockResolvedValue({
      sessions: [
        row({ dir: 'C:\\会议\\空闲', session_id: '空闲', kind: 'empty', state: null }),
        row({ dir: 'C:\\会议\\在录', session_id: '在录', state: 'recording' }),
        row({ dir: 'C:\\会议\\跑着', session_id: '跑着' }),
        row({ dir: 'C:\\会议\\等动手', session_id: '等动手', state: 'done' }),
      ],
      recent: DIR,
      post_job: { kind: 'offline', dir: 'C:\\会议\\跑着', started_at: '2026-09-21T22:10:00+08:00' },
    });
    fetchSessionStatusMock.mockImplementation(async (dir: string) => ({
      ...status({}),
      dir,
      state: 'done',
    }));
    const renderer = await mountHome();

    const cells = renderer.root.findAll(
      (node) => `${node.props.className ?? ''}`.startsWith('wslist__state wslist__state--'),
    );
    const joined = cells
      .map((node) => {
        const parts: string[] = [];
        const walk = (current: TestRenderer.ReactTestInstance): void => {
          for (const child of current.children) {
            if (typeof child === 'string') parts.push(child);
            else walk(child as TestRenderer.ReactTestInstance);
          }
        };
        walk(node);
        return { cls: String(node.props.className), text: parts.join('') };
      });

    const find = (tone: string) => joined.filter((item) => item.cls.includes(`--${tone}`));
    expect(find('idle')[0]?.text).toBe('空白工作区');
    expect(find('recording')[0]?.text).toBe('录音中');
    expect(find('post-running')[0]?.text).toBe('会后任务运行中');
    expect(find('waiting')[0]?.text).toBe('待处理');
    // 图形通道逐行都在：空闲空心框、在录实心圆、会后任务运行中转圈、等动手虚线圈
    const marks = cells.map((node) =>
      (node.children as unknown[])
        .filter((child) => child !== null && typeof child === 'object')
        .map((child) => String((child as TestRenderer.ReactTestInstance).props.className)),
    );
    expect(marks[0]).toContain('glyph glyph--idle');
    expect(marks[1]).toContain('glyph glyph--rec');
    expect(marks[2]).toContain('spinner');
    expect(marks[3]).toContain('glyph glyph--dashed');
  });

  it('全局一格：会后那趟在飞时「忙碌」，一只 gpucell 顶起整格', async () => {
    fetchSessionsMock.mockResolvedValue({
      sessions: [row()],
      recent: DIR,
      post_job: { kind: 'minutes', dir: DIR, started_at: '2026-09-21T22:10:00+08:00' },
    });
    const renderer = await mountHome();
    const cell = () => renderer.root.find((node) => `${node.props.className ?? ''}`.startsWith('gpucell'));
    expect(String(cell().props.className)).toContain('gpucell--busy');
    expect(cell().props['data-busy']).toBe('true');
    expect(cell().props.title).toBe('会后那趟在跑（纪要）');
  });

  it('全局一格：没有会后的活儿时「空闲」', async () => {
    fetchSessionsMock.mockResolvedValue({ sessions: [row()], recent: DIR, post_job: null });
    const renderer = await mountHome();
    const cell = () => renderer.root.find((node) => `${node.props.className ?? ''}`.startsWith('gpucell'));
    expect(cell().props['data-busy']).toBe('false');
    expect(String(cell().props.className)).not.toContain('gpucell--busy');
    expect(cell().props.title).toBe('没有模型开着');
  });

  it('轮询只挂在有活动那一档：终态读数下 start 不会被喊', async () => {
    fetchSessionsMock.mockResolvedValue({ sessions: [row()], recent: DIR, post_job: null });
    await mountHome();
    // 终态（done + 不在跑）→ 不该进入持续轮询
    expect(pollerOf().start).not.toHaveBeenCalled();
  });

  it('在飞那一档进入持续轮询：start 被喊、stop 在读数落定后被喊', async () => {
    fetchSessionsMock.mockResolvedValue({ sessions: [row()], recent: DIR, post_job: null });
    fetchSessionStatusMock.mockImplementation(async () =>
      status({ state: 'running', in_process: true }),
    );
    await mountHome();
    expect(pollerOf().start).toHaveBeenCalledWith(DIR);

    // 读数落定（in_process 翻假、盘上 done）→ watch 翻假 → 停表
    fetchSessionStatusMock.mockImplementation(async () => status({ state: 'done', in_process: false }));
    await feed();
    expect(pollerOf().stop).toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 联结档收敛与错误跳（review-1 C1 / m1）
// ---------------------------------------------------------------------------

/** 顶栏那格「忙不忙」（唯一一只 gpucell）。 */
function gpuCellOf(renderer: TestRenderer.ReactTestRenderer): TestRenderer.ReactTestInstance {
  return renderer.root.find((node) => `${node.props.className ?? ''}`.startsWith('gpucell'));
}

/** 列表行的状态格集合（字都拼在一起，方便断言翻没翻）。 */
function rowStatesOf(renderer: TestRenderer.ReactTestRenderer): string {
  return textsOf(renderer, 'wslist__state').join(' ');
}

describe('联结档收敛（review-1 C1）：跑完那一跳之后界面要跟得上', () => {
  it('选中行被联结命中：在飞时 start，终态那一跳 stop + 列表重取 + 行与全局格翻回', async () => {
    fetchSessionsMock.mockResolvedValue({
      sessions: [row({ state: 'done' })],
      recent: DIR,
      post_job: { kind: 'offline', dir: DIR, started_at: '2026-09-21T22:10:00+08:00' },
    });
    fetchSessionStatusMock.mockImplementation(async () =>
      status({ state: 'running', in_process: true }),
    );
    const renderer = await mountHome();
    expect(pollerOf().start).toHaveBeenCalledWith(DIR);
    expect(rowStatesOf(renderer)).toContain('会后任务运行中');
    expect(gpuCellOf(renderer).props['data-busy']).toBe('true');

    // 跑完：读数翻终态、服务把 post_job 翻 null —— 这一次翻账要全跟上（旧行为全部卡死）
    fetchSessionStatusMock.mockImplementation(async () =>
      status({ state: 'done', in_process: false }),
    );
    fetchSessionsMock.mockResolvedValue({
      sessions: [row({ state: 'done' })],
      recent: DIR,
      post_job: null,
    });
    await feed();

    expect(pollerOf().stop).toHaveBeenCalled();
    expect(fetchSessionsMock).toHaveBeenCalledTimes(2);
    expect(rowStatesOf(renderer)).not.toContain('会后任务运行中');
    expect(rowStatesOf(renderer)).toContain('待处理');
    expect(gpuCellOf(renderer).props['data-busy']).toBe('false');
    await act(async () => {
      renderer.unmount();
    });
  });

  it('post 在飞但选中的是别行：列表按心跳重取，翻 null 那一拍行与全局格跟上、心跳即停', async () => {
    vi.useFakeTimers();
    try {
      const rowsFly = (): SessionRow[] => [
        row({ dir: 'C:\\会议\\跑着', session_id: '跑着', state: 'done' }),
        row({ dir: DIR, session_id: '这一场', state: 'done' }),
      ];
      fetchSessionsMock.mockResolvedValue({
        sessions: rowsFly(),
        recent: DIR,
        post_job: { kind: 'offline', dir: 'C:\\会议\\跑着', started_at: '2026-09-21T22:10:00+08:00' },
      });
      const renderer = await mountHome();
      expect(fetchSessionsMock).toHaveBeenCalledTimes(1); // 只有挂载那一次
      expect(rowStatesOf(renderer)).toContain('会后任务运行中');
      expect(gpuCellOf(renderer).props['data-busy']).toBe('true');

      // 跑完：服务翻 null；下一拍心跳重取，A 行翻回、全局格翻闲
      fetchSessionsMock.mockResolvedValue({ sessions: rowsFly(), recent: DIR, post_job: null });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });
      expect(fetchSessionsMock).toHaveBeenCalledTimes(2);
      expect(rowStatesOf(renderer)).not.toContain('会后任务运行中');
      expect(gpuCellOf(renderer).props['data-busy']).toBe('false');

      // post_job 清 null → 心跳停：再走两拍也不再取
      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_000);
      });
      expect(fetchSessionsMock).toHaveBeenCalledTimes(2);

      await act(async () => {
        renderer.unmount();
      });
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('错误跳的分档（review-1 m1）：与轮询器「失败跳照交不停表」的契约对齐', () => {
  it('404 档（SessionStatusHttpError）：停表 —— 重试也是同一句；错误行如实画出', async () => {
    fetchSessionsMock.mockResolvedValue({ sessions: [row()], recent: DIR, post_job: null });
    fetchSessionStatusMock.mockImplementation(async () =>
      status({ state: 'running', in_process: true }),
    );
    const renderer = await mountHome();
    expect(pollerOf().start).toHaveBeenCalledWith(DIR);
    // watch effect 的清理在依赖变化时也会喊 stop（组件既有行为），这里数的是
    // **错误那一跳**有没有多喊一次 —— 那才是 m1 的判据。
    const stopsBefore = (pollerOf().stop as ReturnType<typeof vi.fn>).mock.calls.length;

    fetchSessionStatusMock.mockRejectedValueOnce(
      new SessionStatusHttpError(404, 'session_unknown', '{}'),
    );
    await feed();
    expect(
      (pollerOf().stop as ReturnType<typeof vi.fn>).mock.calls.length,
    ).toBe(stopsBefore + 1);
    expect(textsOf(renderer, 'wsprog__row--bad').join(' ')).toContain('HTTP 404');
  });

  it('瞬态网络错误（SessionsUnavailableError）：不停表、不冻结 —— 下一跳照常进、收起态也看得见', async () => {
    fetchSessionsMock.mockResolvedValue({ sessions: [row()], recent: DIR, post_job: null });
    fetchSessionStatusMock.mockImplementation(async () =>
      status({ state: 'running', in_process: true }),
    );
    const renderer = await mountHome();
    expect(pollerOf().start).toHaveBeenCalledWith(DIR);
    const stopsBefore = (pollerOf().stop as ReturnType<typeof vi.fn>).mock.calls.length;

    fetchSessionStatusMock.mockRejectedValueOnce(
      new SessionsUnavailableError('单场状态', new Error('服务没应答')),
    );
    await feed();
    expect(
      (pollerOf().stop as ReturnType<typeof vi.fn>).mock.calls.length,
    ).toBe(stopsBefore);
    // 收起也看得见（错误行不进 progOpen 那一半）
    const toggle = renderer.root.find(
      (node) =>
        node.type === 'button' &&
        `${node.props.className ?? ''}`.split(' ').includes('wsprog__toggle'),
    );
    await act(async () => {
      toggle.props.onClick();
    });
    expect(textsOf(renderer, 'wsprog__row--bad').join(' ')).toContain('取不到单场状态');

    // 下一跳恢复：错误行清掉、读数照常往下刷（进度区没有被冻死）
    await feed();
    expect(textsOf(renderer, 'wsprog__row--bad')).toHaveLength(0);
    expect(
      (pollerOf().stop as ReturnType<typeof vi.fn>).mock.calls.length,
    ).toBe(stopsBefore);
  });
});

// ---------------------------------------------------------------------------
// 按钮起跑档与重跑重播（review-2 C1 / m2）
// ---------------------------------------------------------------------------

describe('按钮起跑档（review-2 C1）：post_job 出现在选中行上之后，最多一个周期内进度区要跟上', () => {
  it('挂载握着旧终态 → 按「生成会后精细稿」→ post_job 出现 → 补跳一跳 start 被喊、读数跟走；终局照旧收敛', async () => {
    // 挂载：终态读数在手（上一趟的账）、post_job:null —— 轮询不该起
    fetchSessionsMock.mockResolvedValueOnce({
      sessions: [row({ state: 'done', has_offline: true })],
      recent: DIR,
      post_job: null,
    });
    fetchSessionStatusMock.mockImplementation(async () =>
      status({
        state: 'done',
        in_process: false,
        started_at: '2026-09-20T10:00:00+08:00',
        finished_at: '2026-09-20T11:00:00+08:00',
        sources: [
          road({ done: 10, counts: { transcribed: 9, silent: 0, failed: 1, unjudged: 0 } }),
          road({ source: 'loopback', done: 10,
            counts: { transcribed: 8, silent: 2, failed: 0, unjudged: 0 } }),
        ],
      }),
    );
    const renderer = await mountHome();
    expect(pollerOf().start).not.toHaveBeenCalled();
    // 旧趟的逐块行在手（挂载那一跳把已处理的补成 observed 行）
    expect(textsOf(renderer, 'wsprog__row').join(' ')).toContain('本地 第 10/10 块 · 已处理');

    // 按「生成会后精细稿」：node 下真实 postAction 当场拒（fetch 相对路径），失败块出列、
    // 收尾 reloadAfterAction() 照走 —— post_job 从第二份列表应答进来
    fetchSessionsMock.mockResolvedValue({
      sessions: [row({ state: 'done', has_offline: true })],
      recent: DIR,
      post_job: { kind: 'offline', dir: DIR, started_at: '2026-09-21T22:10:00+08:00' },
    });
    // 补跳那一跳读到的是新一趟的在飞读数（新账：done 从 0 重数）
    fetchSessionStatusMock.mockImplementation(async () =>
      status({
        state: 'running',
        in_process: true,
        started_at: '2026-09-21T22:10:00+08:00',
        current_source: 'mic',
        sources: [
          road({ done: 0, total: 10, remaining: 10, current_chunk: 1 }),
          road({ source: 'loopback' }),
        ],
      }),
    );
    await act(async () => {
      renderer.root
        .find((node) => node.type === 'button' && node.props.children === '生成会后精细稿')
        .props.onClick();
    });
    await act(async () => {});

    // 补跳把在飞读数喂进来 → watch 翻真 → start 被喊（一个周期都没用上）
    expect(pollerOf().start).toHaveBeenCalledWith(DIR);
    expect(textsOf(renderer, 'wsprog__phase').join(' ')).toContain('正在运行：本地 第 1/10 块');
    // 旧趟的行整段换掉（m2：账重置重播），新账一块没跑完 → 空态那句
    expect(textsOf(renderer, 'wsprog__row').join(' ')).not.toContain('已处理');
    expect(textsOf(renderer, 'wsprog__row').join(' ')).toContain('暂无逐块记录');
    const fills = renderer.root.findAll((node) => node.props.className === 'wsprog__fill');
    expect(String(fills[0]?.props.style?.['--p'])).toBe('0%');

    // 在飞读数往下走：同一趟内照差值追加，逐块行跟走
    fetchSessionStatusMock.mockImplementation(async () =>
      status({
        state: 'running',
        in_process: true,
        started_at: '2026-09-21T22:10:00+08:00',
        current_source: 'mic',
        sources: [
          road({ done: 1, total: 10, remaining: 9, current_chunk: 2,
            counts: { transcribed: 1, silent: 0, failed: 0, unjudged: 0 } }),
          road({ source: 'loopback' }),
        ],
      }),
    );
    await feed();
    expect(textsOf(renderer, 'wsprog__row').join(' ')).toContain('本地 第 1/10 块 · 成功');

    // 终局：读到终态 → 照 review-1 C1 的既有收敛 —— 停表 + 列表重取 + 行/格翻回
    fetchSessionStatusMock.mockImplementation(async () =>
      status({
        state: 'done',
        in_process: false,
        started_at: '2026-09-21T22:10:00+08:00',
        finished_at: '2026-09-21T23:10:00+08:00',
        sources: [
          road({ done: 10, counts: { transcribed: 10, silent: 0, failed: 0, unjudged: 0 } }),
          road({ source: 'loopback', done: 10,
            counts: { transcribed: 10, silent: 0, failed: 0, unjudged: 0 } }),
        ],
      }),
    );
    fetchSessionsMock.mockResolvedValue({
      sessions: [row({ state: 'done', has_offline: true })],
      recent: DIR,
      post_job: null,
    });
    await feed();
    expect(pollerOf().stop).toHaveBeenCalled();
    // 列表取了三次：挂载、按按钮的收尾重取、跑完翻账那一次
    expect(fetchSessionsMock).toHaveBeenCalledTimes(3);
    expect(gpuCellOf(renderer).props['data-busy']).toBe('false');
    expect(textsOf(renderer, 'wsprog__row').join(' ')).toContain('本地 第 10/10 块 · 成功');
  });
});

describe('重跑一场的逐块行（review-2 m2）：账重置那一跳整段重播，不接旧账往下编', () => {
  it('started_at 换新的那一跳：旧趟十行整段换掉，新账从空起步、往下照常落', async () => {
    fetchSessionsMock.mockResolvedValue({
      sessions: [row({ state: 'done', has_offline: true })],
      recent: DIR,
      post_job: null,
    });
    // 挂载读到的是上一趟的终读（十块全在）
    fetchSessionStatusMock.mockImplementation(async () =>
      status({
        state: 'done',
        in_process: false,
        started_at: '2026-09-20T10:00:00+08:00',
        finished_at: '2026-09-20T11:00:00+08:00',
        sources: [
          road({ done: 10, counts: { transcribed: 9, silent: 0, failed: 1, unjudged: 0 } }),
          road({ source: 'loopback', done: 10,
            counts: { transcribed: 8, silent: 2, failed: 0, unjudged: 0 } }),
        ],
      }),
    );
    const renderer = await mountHome();
    expect(textsOf(renderer, 'wsprog__row').join(' ')).toContain('本地 第 10/10 块 · 已处理');

    // 重跑第一跳：新 started_at、done 从 0 重数
    fetchSessionStatusMock.mockImplementation(async () =>
      status({
        state: 'running',
        in_process: true,
        started_at: '2026-09-21T22:10:00+08:00',
        current_source: 'mic',
        sources: [
          road({ done: 0, total: 10, remaining: 10 }),
          road({ source: 'loopback' }),
        ],
      }),
    );
    await feed();
    // 旧行整段换掉（不是「第 11/10 块」接着旧账编）；新账还没跑完一块 → 空态那句
    const rowsAfterReset = textsOf(renderer, 'wsprog__row').join(' ');
    expect(rowsAfterReset).not.toContain('第 10/10 块');
    expect(rowsAfterReset).toContain('暂无逐块记录');

    // 第二跳照常差值追加（同一趟内 done 1）
    fetchSessionStatusMock.mockImplementation(async () =>
      status({
        state: 'running',
        in_process: true,
        started_at: '2026-09-21T22:10:00+08:00',
        current_source: 'mic',
        sources: [
          road({ done: 1, total: 10, remaining: 9,
            counts: { transcribed: 1, silent: 0, failed: 0, unjudged: 0 } }),
          road({ source: 'loopback' }),
        ],
      }),
    );
    await feed();
    expect(textsOf(renderer, 'wsprog__row').join(' ')).toContain('本地 第 1/10 块 · 成功');
  });
});

// ---------------------------------------------------------------------------
// 补跳遇瞬态失败的自愈与 404 停表（review-3 m1）
// ---------------------------------------------------------------------------

/**
 * m1 的场景（复审探针 B 同形收编）：挂载握着旧终态 → 按「生成会后精细稿」→ post_job
 * 出现在选中行上 → **补跳那一跳**按给定错误失败。返回渲染器与按下那一刻的 tick 基线
 * （挂载定选中那一跳占一跳）；fake timers 已开（postFlying 列表心跳缺省 10s 一拍，
 * 重问点就搭在这条心跳上）。
 */
async function mountWithPostJumpFailing(
  error: Error,
): Promise<{ renderer: TestRenderer.ReactTestRenderer; ticksBefore: number }> {
  vi.useFakeTimers();
  // 挂载：终态读数在手（上一趟的账）、post_job:null —— 轮询不该起
  fetchSessionsMock.mockResolvedValueOnce({
    sessions: [row({ state: 'done', has_offline: true })],
    recent: DIR,
    post_job: null,
  });
  fetchSessionStatusMock.mockImplementation(async () =>
    status({
      state: 'done',
      in_process: false,
      started_at: '2026-09-20T10:00:00+08:00',
      finished_at: '2026-09-20T11:00:00+08:00',
      sources: [
        road({ done: 10, counts: { transcribed: 9, silent: 0, failed: 1, unjudged: 0 } }),
        road({ source: 'loopback', done: 10,
          counts: { transcribed: 8, silent: 2, failed: 0, unjudged: 0 } }),
      ],
    }),
  );
  const renderer = await mountHome();
  expect(pollerOf().start).not.toHaveBeenCalled();
  const ticksBefore = tickCalls();

  // 按「生成会后精细稿」：post_job 从第二份列表应答进来（node 下真实 postAction 当场
  // 拒，收尾 reloadAfterAction() 照走 —— 与 review-2 C1 用例同一手法）；补跳那一跳失败
  fetchSessionsMock.mockResolvedValue({
    sessions: [row({ state: 'done', has_offline: true })],
    recent: DIR,
    post_job: { kind: 'offline', dir: DIR, started_at: '2026-09-21T22:10:00+08:00' },
  });
  fetchSessionStatusMock.mockRejectedValueOnce(error);
  await act(async () => {
    renderer.root
      .find((node) => node.type === 'button' && node.props.children === '生成会后精细稿')
      .props.onClick();
  });
  await act(async () => {});
  return { renderer, ticksBefore };
}

/** 替身轮询器的 tick 计数（补跳一跳 + 心跳重问各记一跳）。 */
function tickCalls(): number {
  return (pollerOf().tick as ReturnType<typeof vi.fn>).mock.calls.length;
}

describe('补跳遇瞬态失败的自愈（review-3 m1）：失败后最多一个心跳周期内自动重问', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('补跳抛瞬态错（错误行如实画出）→ 走一拍心跳：tick 重问、start 恢复、在飞读数喂入、阶段行跟走', async () => {
    const { renderer, ticksBefore } = await mountWithPostJumpFailing(
      new SessionsUnavailableError('单场状态', new Error('服务没应答')),
    );

    // 失败那一跳：错误行在收起态也看得见，start 没被喊、tick 只有补跳那一跳
    expect(tickCalls()).toBe(ticksBefore + 1);
    expect(pollerOf().start).not.toHaveBeenCalled();
    expect(textsOf(renderer, 'wsprog__row--bad').join(' ')).toContain('取不到单场状态');

    // 下一跳读到在飞读数（服务回来了）
    fetchSessionStatusMock.mockImplementation(async () =>
      status({
        state: 'running',
        in_process: true,
        started_at: '2026-09-21T22:10:00+08:00',
        current_source: 'mic',
        sources: [
          road({ done: 0, total: 10, remaining: 10, current_chunk: 1 }),
          road({ source: 'loopback' }),
        ],
      }),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });

    // 心跳那一拍：列表重取照走，重问搭同一拍 —— 在飞读数到手、watch 翻真、轮询起
    expect(fetchSessionsMock).toHaveBeenCalledTimes(3); // 挂载、按按钮收尾、心跳这一拍
    expect(tickCalls()).toBe(ticksBefore + 2);
    expect(pollerOf().start).toHaveBeenCalledWith(DIR);
    expect(textsOf(renderer, 'wsprog__phase').join(' ')).toContain('正在运行：本地 第 1/10 块');
    expect(textsOf(renderer, 'wsprog__row--bad')).toHaveLength(0);

    // watch 已翻真：再走一拍也不重复问（守卫关上，轮询接管）
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(tickCalls()).toBe(ticksBefore + 2);

    await act(async () => {
      renderer.unmount();
    });
  });

  it('404 档同场景：心跳照走（列表重取不停），重问不发生 —— 停表语义维持', async () => {
    const { renderer, ticksBefore } = await mountWithPostJumpFailing(
      new SessionStatusHttpError(404, 'session_unknown', '{}'),
    );

    // 失败那一跳：404 档停表（review-1 m1 语义），错误行如实画出
    expect(tickCalls()).toBe(ticksBefore + 1);
    expect(pollerOf().start).not.toHaveBeenCalled();
    expect((pollerOf().stop as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(0);
    expect(textsOf(renderer, 'wsprog__row--bad').join(' ')).toContain('HTTP 404');

    // 两个心跳周期过去：列表心跳照走（post_job 翻 null 仍要被看见），但不再问单场
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(fetchSessionsMock).toHaveBeenCalledTimes(4); // 挂载、按按钮收尾、两拍心跳
    expect(tickCalls()).toBe(ticksBefore + 1);
    expect(pollerOf().start).not.toHaveBeenCalled();

    await act(async () => {
      renderer.unmount();
    });
  });
});
