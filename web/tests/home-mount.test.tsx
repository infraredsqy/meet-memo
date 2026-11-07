import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest';
import { createElement } from 'react';
import TestRenderer, { act } from 'react-test-renderer';
import { WorkspaceHome } from '../src/home/WorkspaceHome';
import {
  fetchSessions,
  browseDirs,
  fetchSessionStatus,
  fetchSessionTranscripts,
} from '../src/api/sessions';
import { postAction, type ActionResult } from '../src/api';
// 这一条**不经 `../src/api`**（那个模块在本文件里被 mock 掉了，类拿到的会是替身）
import { ActionFailedError } from '../src/api/actions';
import type {
  DirsResponse,
  Ended,
  SessionRow,
  SessionStatus,
  SessionTranscripts,
  SessionsResponse,
} from '../src/contract';

/** 一格「没跑过」的纪要读数（`minutes` 格的空档形状，38817ecf）。 */
function minutesIdle(): SessionStatus['minutes'] {
  return { state: 'idle', started_at: null, finished_at: null, error: null };
}

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


/**
 * review-2 m2：「挂载即取列表」接线用例（免 DOM 跑法）。
 *
 * 老毛病是纯函数全绿、组件挂载那一次 `fetchSessions()` 没人发（review-2 C1）——
 * 这里用 react-test-renderer 在 node 环境真挂载 `WorkspaceHome`，替身打在
 * `src/api/sessions` 这条边界上，断言「挂载 → 请求发出 → 行/空态出列」，
 * 不只测纯函数。vitest 跑在 node 环境（vitest.config.ts），react-test-renderer
 * 正好不需要 DOM；JSX 不落盘（createElement 直造），测试不碰 jsx 配置。
 *
 * review-1 m7 往同一个文件里补了会后那一块：**真的点**那两个按钮，断言发出去的动作名
 * 与 `dir`、`has_offline` 两档画出来的那一格、以及 409 落进失败块。手法同源
 * （`react-test-renderer`，不给 node 侧开 DOM）。
 *
 * 会后进度界面节点（52b24392）起的轮询也打在同一条边界上：替身里给一只**手动排程**
 * 的轮询器（tick 立刻转手 `fetchSessionStatus`、start/stop 只登记，不挂任何真定时器），
 * 用例因此不必等真实周期。
 */

vi.mock('../src/api/sessions', async (importOriginal) => {
  // 名目分流（`probeDecision`）走真实现：选择器流程的用例要按它走三档。
  const actual = await importOriginal<typeof import('../src/api/sessions')>();
  const fetchSessionStatus = vi.fn();
  // 组件注册上来的读数回调（轮询循环会把每一跳交给它；替身同样交，路线一致）。
  let onTick: ((tick: unknown) => void) | null = null;
  const deliver = (dir: string): Promise<unknown> =>
    fetchSessionStatus(dir).then((status: SessionStatus) => {
      const result = { kind: 'status', dir, status };
      if (onTick !== null) onTick(result);
      return result;
    });
  const poller = {
    setIntervalFromSnapshot: vi.fn(),
    intervalSeconds: vi.fn(() => 10),
    tick: vi.fn((dir: string) => deliver(dir)),
    start: vi.fn(),
    stop: vi.fn(),
    running: vi.fn(() => false),
  };
  return {
    ...actual,
    fetchSessions: vi.fn(),
    browseDirs: vi.fn(),
    fetchSessionStatus,
    // 稿子那一跳（38817ecf）：本文件的用例不核它的内容，替身给一份「两份稿都缺」的
    // 空应答，别让挂载用例摸真 fetch。
    fetchSessionTranscripts: vi.fn(async () => ({ dir: '', kinds: [] })),
    createSessionStatusPoller: vi.fn((registered: unknown) => {
      onTick = registered as (tick: unknown) => void;
      return poller;
    }),
  };
});

vi.mock('../src/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api')>();
  return { ...actual, postAction: vi.fn() };
});

// 会议资料区（160d1a78）：详情里那块面板挂载即取清单 —— 替身给空清单，这条文件不连网。
vi.mock('../src/api/materials', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/materials')>();
  return {
    ...actual,
    fetchMaterials: vi.fn(async () => ({ dir: '', materials_dir: '', items: [] })),
  };
});

const fetchSessionsMock = vi.mocked(fetchSessions);
const browseDirsMock = vi.mocked(browseDirs);
const fetchSessionStatusMock = vi.mocked(fetchSessionStatus);
const fetchSessionTranscriptsMock = vi.mocked(fetchSessionTranscripts);
const postActionMock = vi.mocked(postAction);

/** 造一行注册表（空缺全给 null / 无害值）。 */
function row(over: Partial<SessionRow> = {}): SessionRow {
  return {
    dir: 'C:\\会议\\跟老王聊周会',
    session_id: '跟老王聊周会',
    dir_exists: true,
    kind: 'empty',
    suspected: false,
    state: null,
    date: null,
    mode: null,
    segments: [],
    has_offline: false,
    added_at: '2026-09-21T21:03:00+08:00',
    last_opened_at: '2026-09-21T21:03:00+08:00',
    ...over,
  };
}

function list(over: Partial<SessionsResponse> = {}): SessionsResponse {
  return { sessions: [], recent: null, post_jobs: [], ...over };
}

/** 一份「这一场从没跑过会后」的单场读数（进度区读数的缺省替身）。 */
export function neverRunStatus(dir: string): SessionStatus {
  return {
    dir,
    session_id: null,
    kind: 'empty',
    state: null,
    date: null,
    mode: null,
    segments: [],
    has_offline: false,
    post: [],
    transcode: transcodeIdle(),
    minutes: minutesIdle(),
    speakers: [],
    offline: {
      ledger: 'meeting_offline_progress.jsonl',
      state: 'idle',
      in_process: false,
      started_at: null,
      finished_at: null,
      elapsed_seconds: null,
      current_source: null,
      sources: [
        { source: 'mic', done: 0, total: null, remaining: null, current_chunk: null,
          counts: { transcribed: 0, silent: 0, failed: 0, unjudged: 0 } },
        { source: 'loopback', done: 0, total: null, remaining: null, current_chunk: null,
          counts: { transcribed: 0, silent: 0, failed: 0, unjudged: 0 } },
      ],
    },
  };
}

/** node 环境下挂载组件，等 effect 与它带的 promise（loadList）落定。 */
async function mountHome(): Promise<TestRenderer.ReactTestRenderer> {
  let renderer!: TestRenderer.ReactTestRenderer;
  await act(async () => {
    await TestRenderer.act(() => {
      renderer = TestRenderer.create(createElement(WorkspaceHome));
    });
  });
  return renderer;
}

function optionsOf(renderer: TestRenderer.ReactTestRenderer): TestRenderer.ReactTestInstance[] {
  return renderer.root.findAll(
    (node) => node.props.role === 'option' && node.parent?.props['aria-label'] === '工作区列表',
  );
}

afterEach(() => {
  fetchSessionsMock.mockReset();
  browseDirsMock.mockReset();
  fetchSessionStatusMock.mockReset();
  fetchSessionTranscriptsMock.mockReset();
  postActionMock.mockReset();
});

beforeEach(() => {
  // 进度区那一跳的缺省替身：按请求的 dir 给「没跑过会后」的读数；要特定读数自己覆盖。
  fetchSessionStatusMock.mockImplementation(async (dir: string) => neverRunStatus(dir));
  // 稿子那一跳的缺省替身：给一份「两份稿都缺」的空应答（要特定读数自己覆盖）。
  fetchSessionTranscriptsMock.mockResolvedValue({ dir: '', kinds: [] });
});

describe('挂载即取列表（review-2 C1/m2）', () => {
  it('挂载就发一次 GET /api/sessions；列表有行时行出列、选择落在 recent', async () => {
    fetchSessionsMock.mockResolvedValue(
      list({
        sessions: [row({ dir: 'C:\\a', session_id: 'a' }), row({ dir: 'C:\\b', session_id: 'b' })],
        recent: 'C:\\b',
      }),
    );
    const renderer = await mountHome();

    expect(fetchSessionsMock).toHaveBeenCalledTimes(1);
    const options = optionsOf(renderer);
    expect(options).toHaveLength(2);
    // recent 恒有对应行时直接选它（最近一场只从注册表读）
    const onRow = options.filter((node) => `${node.props.className}`.includes('wslist__row--on'));
    expect(onRow).toHaveLength(1);
    expect(
      (onRow[0] as TestRenderer.ReactTestInstance).props.children.some(
        (child: { props: { className?: string } }) => child?.props?.className === 'wslist__name',
      ),
    ).toBe(true);
  });

  it('空注册表：出的是空态（logo + 添加工作区指引），不再是卡死的「正在取列表…」', async () => {
    fetchSessionsMock.mockResolvedValue(list());
    const renderer = await mountHome();

    expect(fetchSessionsMock).toHaveBeenCalledTimes(1);
    expect(renderer.root.findAll((node) => node.props.className === 'ws-empty')).toHaveLength(1);
    // 旧病画像：应答回来之后这条占位不该再挂着
    expect(
      renderer.root.findAll((node) => node.props.children === '正在取列表…'),
    ).toHaveLength(0);
  });

  it('取失败：失败块如实画出来，不冒成功', async () => {
    fetchSessionsMock.mockRejectedValue(new Error('取不到工作区列表：服务没应答'));
    const renderer = await mountHome();

    expect(renderer.root.findAll((node) => node.props.className === 'fail')).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// 会后那一块：真的挂载、真的点（review-1 m7）
// ---------------------------------------------------------------------------

/** 按按钮上的字找一只 `<button>`。 */
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

/** 会后那一块里那几行小字（`wspane__postline`）。 */
function postLines(renderer: TestRenderer.ReactTestRenderer): string[] {
  return renderer.root
    .findAll((node) => node.props.className === 'wspane__postline')
    .map((node) => `${node.props.children}`);
}

/** 一棵子树里的全部文字（把嵌套的字符串拼起来）。 */
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

/** 点一下那只按钮，并把随后的 promise（动作 + 重取列表）都放完。 */
async function click(
  renderer: TestRenderer.ReactTestRenderer,
  node: TestRenderer.ReactTestInstance,
): Promise<void> {
  await act(async () => {
    node.props.onClick();
  });
}

describe('会后那两个按钮（review-1 m7）', () => {
  it('点「生成会后精细稿」发出 post_offline、点「生成纪要」发出 post_minutes，都带上选中那一行的 dir', async () => {
    fetchSessionsMock.mockResolvedValue(
      list({ sessions: [row({ dir: 'C:\\会议\\这一场', session_id: '这一场' })], recent: 'C:\\会议\\这一场' }),
    );
    postActionMock.mockResolvedValue({ kind: 'ok', ok: true, revision: 7 });
    const renderer = await mountHome();

    await click(renderer, buttonByText(renderer, '生成会后精细稿'));
    expect(postActionMock).toHaveBeenLastCalledWith('post_offline', { dir: 'C:\\会议\\这一场' });

    await click(renderer, buttonByText(renderer, '生成纪要'));
    expect(postActionMock).toHaveBeenLastCalledWith('post_minutes', { dir: 'C:\\会议\\这一场' });
    // 起成功之后那一块如实写回执（不宣称跑完了）
    expect(postLines(renderer).join('\n')).toContain('这一趟已起');
  });

  it('`has_offline` 两档画出来的那一格不同', async () => {
    const withOffline = row({ dir: 'C:\\会议\\有稿', has_offline: true });
    const withoutOffline = row({ dir: 'C:\\会议\\没稿', has_offline: false });
    fetchSessionsMock.mockResolvedValue(
      list({ sessions: [withOffline, withoutOffline], recent: withOffline.dir }),
    );
    const renderer = await mountHome();

    expect(postLines(renderer).join('\n')).toContain('会后精细稿：有');

    // 选中另一行（没有会后稿那一行）之后，那一格跟着换成另一句
    const rows = optionsOf(renderer);
    const other = rows.find((node) =>
      `${node.props.children}`.includes('没稿') ||
      textOf(node).includes('没稿'),
    );
    expect(other, '列表里该有「没稿」那一行').toBeDefined();
    await act(async () => {
      other?.props.onClick();
    });
    expect(postLines(renderer).join('\n')).toContain('会后精细稿：无');
  });

  it('纪要回执带 inputs / note 时照实画进回执那一行（说破不静默）', async () => {
    fetchSessionsMock.mockResolvedValue(
      list({ sessions: [row({ dir: 'C:\\会议\\这一场' })], recent: 'C:\\会议\\这一场' }),
    );
    // 形状照 `meeting/service/runtime_post.py` 的 `post_minutes()` 回执（contract 4.3）：
    // `inputs` 恒在、`note` 只在同场会后精细稿还在跑那档出现（原文同 MINUTES_DEGRADED_NOTE）。
    postActionMock.mockResolvedValue({
      kind: 'post',
      ok: true,
      revision: 7,
      post: {
        kind: 'minutes',
        dir: 'C:\\会议\\这一场',
        inputs: 'live_only',
        note: '会后的精细稿还在跑：这一趟拿到的是会中稿；等它跑完再出纪要能对上会后稿',
      },
    });
    const renderer = await mountHome();

    await click(renderer, buttonByText(renderer, '生成纪要'));

    const line = postLines(renderer).join('\n');
    expect(line).toContain('这一趟已起');
    expect(line).toContain('这次读到的输入档位：只有会中稿');
    expect(line).toContain('会后的精细稿还在跑：这一趟拿到的是会中稿');
  });

  it('按钮被拒（409 draining）时失败块出列，回执那一行不留假成功', async () => {
    fetchSessionsMock.mockResolvedValue(
      list({ sessions: [row({ dir: 'C:\\会议\\这一场' })], recent: 'C:\\会议\\这一场' }),
    );
    postActionMock.mockRejectedValue(
      new ActionFailedError(409, 'draining', '纪要这一层已经在飞', { kind: 'minutes' }),
    );
    const renderer = await mountHome();

    await click(renderer, buttonByText(renderer, '生成纪要'));

    const failures = renderer.root.findAll((node) => node.props.className === 'fail');
    expect(failures).toHaveLength(1);
    const text = textOf(failures[0] as TestRenderer.ReactTestInstance);
    expect(text).toContain('draining');
    expect(text).toContain('纪要这一层已经在飞');
    expect(postLines(renderer).join('\n')).not.toContain('这一趟已起');
  });

  it('目录不在原地那一行：两个按钮都按不动，原因句写在脸上', async () => {
    fetchSessionsMock.mockResolvedValue(
      list({ sessions: [row({ dir: 'C:\\会议\\搬走了', dir_exists: false })], recent: 'C:\\会议\\搬走了' }),
    );
    const renderer = await mountHome();

    expect(buttonByText(renderer, '生成会后精细稿').props.disabled).toBe(true);
    expect(buttonByText(renderer, '生成纪要').props.disabled).toBe(true);
    const whys = renderer.root
      .findAll((node) => node.props.className === 'wspane__why')
      .map((node) => `${node.props.children}`);
    expect(whys.join('\n')).toContain('目录不存在');
    // 原因句只有一处（`wspane__why`）：会后那一格不复述，也不把「目录不存在」搬过来
    expect(postLines(renderer).join('\n')).not.toContain('目录不存在');
    expect(postLines(renderer).join('\n')).not.toContain('跑不了会后');
    const stateCells = renderer.root
      .findAll((node) => `${node.props.className}`.includes('wslist__state'))
      .map((node) => textOf(node));
    expect(stateCells.join('\n'), '那一行还是按路径那一格说「目录不存在」').toContain('目录不存在');
  });

  it('还在录音中那一档：两个按钮都按不动，那一格把「还在录」与「磁盘上的会后精细稿：有」并列说（review-2 C1/m2）', async () => {
    fetchSessionsMock.mockResolvedValue(
      list({
        sessions: [row({ dir: 'C:\\会议\\在录', state: 'recording', has_offline: true })],
        recent: 'C:\\会议\\在录',
      }),
    );
    const renderer = await mountHome();

    expect(buttonByText(renderer, '生成会后精细稿').props.disabled).toBe(true);
    expect(buttonByText(renderer, '生成纪要').props.disabled).toBe(true);
    const whys = renderer.root
      .findAll((node) => node.props.className === 'wspane__why')
      .map((node) => `${node.props.children}`);
    expect(whys.join('\n')).toContain('该工作区正在录音中');
    const lines = postLines(renderer).join('\n');
    // 两件事都在：这一场还在录 + 盘上此刻那一份的读数（不许哪一句盖住另一句）
    expect(lines).toContain('该工作区正在录音中');
    expect(lines).toContain('磁盘上的会后精细稿：有');
    // 也不许换成录音结束之后那一句（那一行列的是录音结束之后的话）
    expect(lines.split('\n')).not.toContain('会后精细稿：有');
  });

  it('被强杀在收尾半路的那一场（三档口径就是 ended）：两个按钮照常能按，那一格照实说「磁盘上的会后精细稿：没有」', async () => {
    fetchSessionsMock.mockResolvedValue(
      list({
        sessions: [row({ dir: 'C:\\会议\\强杀现场', state: 'ended', has_offline: false })],
        recent: 'C:\\会议\\强杀现场',
      }),
    );
    const renderer = await mountHome();

    // 「落盘没落定就拒」判据作废：ended 的那一场按钮照常可按
    expect(buttonByText(renderer, '生成会后精细稿').props.disabled).toBe(false);
    expect(buttonByText(renderer, '生成纪要').props.disabled).toBe(false);
    const lines = postLines(renderer).join('\n');
    expect(lines).toContain('会后精细稿：无');
  });
});

// ---------------------------------------------------------------------------
// 说话人名字那只弹窗（人名节点 91b6752e）：入口在会后区，点开、确认、当场生效
// ---------------------------------------------------------------------------

/** 一份带说话人身份的单场读数（`speakers` 一格照服务的形状给）。 */
function statusWithSpeakers(
  dir: string,
  speakers: SessionStatus['speakers'],
): SessionStatus {
  return { ...neverRunStatus(dir), speakers };
}

/** 弹窗里某一行的那只输入框（按 aria-label 找）。 */
function inputOf(
  renderer: TestRenderer.ReactTestRenderer,
  label: string,
): TestRenderer.ReactTestInstance {
  const found = renderer.root.findAll(
    (node) => node.type === 'input' && node.props['aria-label'] === label,
  );
  expect(found, `弹窗里该有「${label}」这只输入框`).toHaveLength(1);
  return found[0] as TestRenderer.ReactTestInstance;
}

describe('说话人名字（R10 第四条 / R30）', () => {
  it('点开弹窗一行一个身份；写上名字点确认，发出 speaker_rename、带上选中那一行的 dir，并重问一次单场状态', async () => {
    const dir = 'C:\\会议\\这一场';
    fetchSessionsMock.mockResolvedValue(list({ sessions: [row({ dir })], recent: dir }));
    fetchSessionStatusMock.mockImplementation(async (asked: string) =>
      statusWithSpeakers(asked, [
        { key: 'mic:S01', source: 'mic', label: '本地', name: null, segments: 3 },
      ]),
    );
    postActionMock.mockResolvedValue({ kind: 'speaker', ok: true, revision: 7, speaker: { dir, key: 'mic:S01', name: '老王' } });
    const renderer = await mountHome();
    const ticksBefore = fetchSessionStatusMock.mock.calls.length;

    await click(renderer, buttonByText(renderer, '说话人名字…'));

    const dialog = renderer.root.findAll(
      (node) => node.props.role === 'dialog' && node.props['aria-label'] === '说话人名字',
    );
    expect(dialog).toHaveLength(1);
    expect(textOf(dialog[0] as TestRenderer.ReactTestInstance)).toContain('本地 · S01');
    expect(textOf(dialog[0] as TestRenderer.ReactTestInstance)).toContain('待确认');

    const input = inputOf(renderer, '本地 · S01 的名字');
    await act(async () => {
      input.props.onChange({ target: { value: '老王' } });
    });
    await click(renderer, buttonByText(renderer, '确认'));

    expect(postActionMock).toHaveBeenLastCalledWith('speaker_rename', {
      dir,
      key: 'mic:S01',
      name: '老王',
    });
    // 当场生效走的读数那一跳：改名之后重问了一次单场状态
    expect(fetchSessionStatusMock.mock.calls.length).toBeGreaterThan(ticksBefore);
  });

  it('撞名只提示一句、提交照走（编号是分开的，可以重名）', async () => {
    const dir = 'C:\\会议\\重名';
    fetchSessionsMock.mockResolvedValue(list({ sessions: [row({ dir })], recent: dir }));
    fetchSessionStatusMock.mockImplementation(async (asked: string) =>
      statusWithSpeakers(asked, [
        { key: 'mic:S01', source: 'mic', label: '本地', name: '老王', segments: 3 },
        { key: 'loopback', source: 'loopback', label: '远端', name: null, segments: null },
      ]),
    );
    postActionMock.mockResolvedValue({ kind: 'speaker', ok: true, revision: 8, speaker: { dir, key: 'loopback', name: '老王' } });
    const renderer = await mountHome();

    await click(renderer, buttonByText(renderer, '说话人名字…'));
    const input = inputOf(renderer, '远端 的名字');
    await act(async () => {
      input.props.onChange({ target: { value: '老王' } });
    });
    // 两行各有一只「确认」：点「远端」那一行自己的那一只
    const rowButton = (input.parent as TestRenderer.ReactTestInstance).find(
      (node) => node.type === 'button' && node.props.children === '确认',
    );
    await click(renderer, rowButton);

    const dialogs = renderer.root.findAll(
      (node) => node.props.role === 'dialog' && node.props['aria-label'] === '说话人名字',
    );
    expect(textOf(dialogs[0] as TestRenderer.ReactTestInstance)).toContain(
      '编号相互独立，允许重名',
    );
    expect(postActionMock).toHaveBeenLastCalledWith('speaker_rename', {
      dir,
      key: 'loopback',
      name: '老王',
    });
  });

  it('动作失败那一档：撞名提示清掉、失败块如实出列，弹窗里不留「已提交」', async () => {
    const dir = 'C:\\会议\\重名失败';
    fetchSessionsMock.mockResolvedValue(list({ sessions: [row({ dir })], recent: dir }));
    fetchSessionStatusMock.mockImplementation(async (asked: string) =>
      statusWithSpeakers(asked, [
        { key: 'mic:S01', source: 'mic', label: '本地', name: '老王', segments: 3 },
        { key: 'loopback', source: 'loopback', label: '远端', name: null, segments: null },
      ]),
    );
    postActionMock.mockRejectedValue(
      new ActionFailedError(400, 'invalid_setting', '人名表在盘上但读不出来，改名拒写', {
        key: 'key',
      }),
    );
    const renderer = await mountHome();

    await click(renderer, buttonByText(renderer, '说话人名字…'));
    const input = inputOf(renderer, '远端 的名字');
    await act(async () => {
      input.props.onChange({ target: { value: '老王' } });
    });
    // 两行各有一只「确认」：点「远端」那一行自己的那一只
    const rowButton = (input.parent as TestRenderer.ReactTestInstance).find(
      (node) => node.type === 'button' && node.props.children === '确认',
    );
    await click(renderer, rowButton);

    expect(postActionMock).toHaveBeenLastCalledWith('speaker_rename', {
      dir,
      key: 'loopback',
      name: '老王',
    });
    // 弹窗里不留成功话术：撞名提示（含「已照你写的名字提交」）在失败时清掉（review-1 m3）
    const dialogs = renderer.root.findAll(
      (node) => node.props.role === 'dialog' && node.props['aria-label'] === '说话人名字',
    );
    const dialogText = textOf(dialogs[0] as TestRenderer.ReactTestInstance);
    expect(dialogText).not.toContain('已照你写的名字提交');
    expect(dialogText).not.toContain('允许重名');
    // 失败块如实出列：code 与服务那句拒写的话都在（不冒成功）
    const failures = renderer.root.findAll((node) => node.props.className === 'fail');
    expect(failures).toHaveLength(1);
    const failureText = textOf(failures[0] as TestRenderer.ReactTestInstance);
    expect(failureText).toContain('invalid_setting');
    expect(failureText).toContain('拒写');
  });

  it('这一场还在录的时候入口按不动（会中不提供改名），弹窗不出现', async () => {
    fetchSessionsMock.mockResolvedValue(
      list({
        sessions: [row({ dir: 'C:\\会议\\在录', state: 'recording' })],
        recent: 'C:\\会议\\在录',
      }),
    );
    const renderer = await mountHome();

    expect(buttonByText(renderer, '说话人名字…').props.disabled).toBe(true);
    expect(
      renderer.root.findAll(
        (node) => node.props.role === 'dialog' && node.props['aria-label'] === '说话人名字',
      ),
    ).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// 停掉会后与「添加工作区会打断」确认（R28 / b1c6a58e）
// ---------------------------------------------------------------------------

const stopDir = 'C:\\会议\\正在运行会后';

/** 一份「这一场的 offline 正在飞」的列表应答（顶层第三格 + 盘上 done 的行）。 */
function flyingList(
  postJobs: SessionsResponse['post_jobs'] = [
    {
      kind: 'offline',
      dir: stopDir,
      started_at: '2026-09-21T22:10:00+08:00',
    },
  ],
): SessionsResponse {
  return list({
    sessions: [row({ dir: stopDir, state: 'ended' })],
    recent: stopDir,
    post_jobs: postJobs,
  });
}

/** 目录选择器里的一行（57ab5255 起行本体不是按钮：单击选中、双击进下层）。 */
function pickerRow(
  renderer: TestRenderer.ReactTestRenderer,
  name: string,
): TestRenderer.ReactTestInstance {
  const found = renderer.root.findAll(
    (node) =>
      node.props.role === 'option' &&
      node.parent?.props['aria-label'] === '这一层的子目录' &&
      textOf(node).includes(name),
  );
  expect(found, `选择器里该有「${name}」这一项`).toHaveLength(1);
  return found[0] as TestRenderer.ReactTestInstance;
}

/** 目录选择器的地址栏（当前层路径，可输入回车跳转）。 */
function pickerAddr(renderer: TestRenderer.ReactTestRenderer): TestRenderer.ReactTestInstance {
  return renderer.root.find(
    (node) =>
      node.type === 'input' &&
      node.props['aria-label'] === '当前层路径，可输入路径回车跳转',
  );
}

const emptyDirs: DirsResponse = {
  home: 'C:\\Users\\<user>',
  cwd: 'C:\\Users\\<user>',
  up: 'C:\\Users',
  here: { name: 'PC', dir: 'C:\\Users\\<user>', kind: 'unknown', suspected: false },
  dirs: [{ name: '新场', dir: 'C:\\会议\\新场', kind: 'empty', suspected: false }],
};

/** 选择器里摆一个「非空、无会议材料」的目录（`probeDecision` → need-confirm 那一档）。 */
const nonEmptyDirs: DirsResponse = {
  home: 'C:\\Users\\<user>',
  cwd: 'C:\\Users\\<user>',
  up: 'C:\\Users',
  here: { name: 'PC', dir: 'C:\\Users\\<user>', kind: 'unknown', suspected: false },
  dirs: [{ name: '旧文件夹', dir: 'C:\\杂物\\旧文件夹', kind: 'unknown', suspected: false }],
};

/** 选中再确认那组用例的现场：同一层里摆空目录与非空目录各一个。 */
const twoKindDirs: DirsResponse = {
  home: 'C:\\Users\\<user>',
  cwd: 'C:\\Users\\<user>',
  up: 'C:\\Users',
  here: { name: 'PC', dir: 'C:\\Users\\<user>', kind: 'unknown', suspected: false },
  dirs: [
    { name: '新场', dir: 'C:\\会议\\新场', kind: 'empty', suspected: false },
    { name: '旧文件夹', dir: 'C:\\杂物\\旧文件夹', kind: 'unknown', suspected: false },
  ],
};

describe('停掉会后与「添加工作区会打断」确认（R28 / b1c6a58e）', () => {
  it('这一场的 offline 在飞：进度区头行有「停止」，点了发出 post_stop 并如实写受理那一行', async () => {
    fetchSessionsMock.mockResolvedValue(flyingList());
    postActionMock.mockResolvedValue({
      kind: 'post',
      ok: true,
      revision: 8,
      post: { kind: 'offline', dir: stopDir, stopping: true },
    });
    const renderer = await mountHome();

    await click(renderer, buttonByText(renderer, '停止'));

    expect(postActionMock).toHaveBeenLastCalledWith('post_stop', { dir: stopDir });
    expect(postLines(renderer).join('\n')).toContain('停止已受理');
  });

  it('在飞的是纪要那一趟：停止入口不出现（停的动作只接精细稿那一趟）', async () => {
    fetchSessionsMock.mockResolvedValue(
      flyingList([{
        kind: 'minutes',
        dir: stopDir,
        started_at: '2026-09-21T22:10:00+08:00',
      }])
    );
    const renderer = await mountHome();

    expect(
      renderer.root.findAll(
        (node) => node.type === 'button' && `${node.props.children}` === '停止',
      ),
    ).toHaveLength(0);
  });

  it('会后在跑时按「添加工作区」挑目录：直接登记、零 stop（登记不打断任何在飞的活）', async () => {
    fetchSessionsMock.mockResolvedValue(flyingList());
    browseDirsMock.mockResolvedValue(emptyDirs);
    postActionMock.mockResolvedValue({ kind: 'ok', ok: true, revision: 9 });
    const renderer = await mountHome();

    await click(renderer, buttonByText(renderer, '添加工作区'));
    await act(async () => {
      pickerRow(renderer, '新场').props.onClick();
    });
    await click(renderer, buttonByText(renderer, '选定这个目录'));

    // 一刀切已废：登记只是写一行名单，不问打断、不发 stop
    expect(renderer.root.findAll((node) => node.props.role === 'dialog')).toHaveLength(0);
    expect(postActionMock).toHaveBeenLastCalledWith('workspace_open', { dir: 'C:\\会议\\新场' });
    expect(postActionMock.mock.calls.map(([name]) => name)).not.toContain('post_stop');
  });

  it('会后精细稿在飞时按「继续录音」：先弹打断确认（逐字那句）；确认 = 先 post_stop 再 start', async () => {
    fetchSessionsMock.mockResolvedValue(flyingList());
    postActionMock.mockResolvedValue({ kind: 'ok', ok: true, revision: 9 });
    const renderer = await mountHome();

    await click(renderer, buttonByText(renderer, '继续录音'));

    const dialogs = renderer.root.findAll((node) => node.props.role === 'dialog');
    const dialogText = dialogs.map((node) => textOf(node as TestRenderer.ReactTestInstance)).join('\n');
    // 设计意图第 13 条的那句原话，逐字
    expect(dialogText).toContain('启动新会议会打断会后总结，是否继续');
    // 确认之前什么都不发（不确认就不打断）
    expect(postActionMock).not.toHaveBeenCalled();

    await click(renderer, buttonByText(renderer, '打断并开始录音'));

    const calls = postActionMock.mock.calls.map(([name]) => name);
    expect(calls).toContain('post_stop');
    expect(calls).toContain('start');
    expect(calls.indexOf('post_stop')).toBeLessThan(calls.indexOf('start'));
  });

  it('不确认就不打断：在打断弹窗上取消，一个动作都不发', async () => {
    fetchSessionsMock.mockResolvedValue(flyingList());
    const renderer = await mountHome();

    await click(renderer, buttonByText(renderer, '继续录音'));
    await click(renderer, buttonByText(renderer, '取消'));

    expect(postActionMock).not.toHaveBeenCalled();
  });

  it('会后没在跑：按「添加工作区」挑目录直接登记（不打扰）', async () => {
    fetchSessionsMock.mockResolvedValue(list({ sessions: [row({ dir: stopDir })], recent: stopDir }));
    browseDirsMock.mockResolvedValue(emptyDirs);
    postActionMock.mockResolvedValue({ kind: 'ok', ok: true, revision: 10 });
    const renderer = await mountHome();

    await click(renderer, buttonByText(renderer, '添加工作区'));
    await act(async () => {
      pickerRow(renderer, '新场').props.onClick();
    });
    await click(renderer, buttonByText(renderer, '选定这个目录'));

    expect(postActionMock).toHaveBeenLastCalledWith('workspace_open', { dir: 'C:\\会议\\新场' });
    expect(postActionMock.mock.calls.map(([name]) => name)).not.toContain('post_stop');
  });

  it('在飞的是 minutes：按「添加工作区」挑目录无确认、零 stop、直接登记（review-1 m1）', async () => {
    fetchSessionsMock.mockResolvedValue(
      flyingList([{ kind: 'minutes', dir: stopDir, started_at: '2026-09-21T22:10:00+08:00' }])
    );
    browseDirsMock.mockResolvedValue(emptyDirs);
    postActionMock.mockResolvedValue({ kind: 'ok', ok: true, revision: 11 });
    const renderer = await mountHome();

    await click(renderer, buttonByText(renderer, '添加工作区'));
    await act(async () => {
      pickerRow(renderer, '新场').props.onClick();
    });
    await click(renderer, buttonByText(renderer, '选定这个目录'));

    // 没有东西会已中断：不弹任何确认（minutes 在飞不摆打断那只）
    expect(renderer.root.findAll((node) => node.props.role === 'dialog')).toHaveLength(0);
    expect(postActionMock).toHaveBeenLastCalledWith('workspace_open', { dir: 'C:\\会议\\新场' });
    expect(postActionMock.mock.calls.map(([name]) => name)).not.toContain('post_stop');
  });

  it('minutes 在飞 × 非空目录：只问收编、不问打断，登记带 confirm 且零 stop（review-1 m1/m2）', async () => {
    fetchSessionsMock.mockResolvedValue(
      flyingList([{ kind: 'minutes', dir: stopDir, started_at: '2026-09-21T22:10:00+08:00' }])
    );
    browseDirsMock.mockResolvedValue(nonEmptyDirs);
    postActionMock.mockResolvedValue({ kind: 'ok', ok: true, revision: 12 });
    const renderer = await mountHome();

    await click(renderer, buttonByText(renderer, '添加工作区'));
    await act(async () => {
      pickerRow(renderer, '旧文件夹').props.onClick();
    });
    await click(renderer, buttonByText(renderer, '选定这个目录'));

    // 先按 R20 问收编；打断那一问要等收编过了、且 offline 在飞才轮到
    const dialogs = renderer.root.findAll((node) => node.props.role === 'dialog');
    expect(dialogs).toHaveLength(1);
    expect(textOf(dialogs[0] as TestRenderer.ReactTestInstance)).toContain('将该目录添加为工作区？');
    await click(renderer, buttonByText(renderer, '确认添加'));

    // 收编过了直接登记：不再有第二问（minutes 在飞没有东西会已中断）
    expect(renderer.root.findAll((node) => node.props.role === 'dialog')).toHaveLength(0);
    expect(postActionMock).toHaveBeenLastCalledWith('workspace_open', {
      dir: 'C:\\杂物\\旧文件夹',
      confirm: true,
    });
    expect(postActionMock.mock.calls.map(([name]) => name)).not.toContain('post_stop');
  });

  it('非空目录 × offline 在飞：只问收编、零打断，登记带 confirm 且零 stop（一刀切已废）', async () => {
    fetchSessionsMock.mockResolvedValue(flyingList());
    browseDirsMock.mockResolvedValue(nonEmptyDirs);
    postActionMock.mockResolvedValue({ kind: 'ok', ok: true, revision: 13 });
    const renderer = await mountHome();

    await click(renderer, buttonByText(renderer, '添加工作区'));
    await act(async () => {
      pickerRow(renderer, '旧文件夹').props.onClick();
    });
    await click(renderer, buttonByText(renderer, '选定这个目录'));

    // 只有一问：收编（R20）；此刻零发送
    const dialogs = renderer.root.findAll((node) => node.props.role === 'dialog');
    expect(dialogs).toHaveLength(1);
    expect(textOf(dialogs[0] as TestRenderer.ReactTestInstance)).toContain('将该目录添加为工作区？');
    expect(postActionMock).not.toHaveBeenCalled();

    await click(renderer, buttonByText(renderer, '确认添加'));

    // 收编过了直接登记：没有第二问（登记不打断任何在飞的活）
    expect(renderer.root.findAll((node) => node.props.role === 'dialog')).toHaveLength(0);
    expect(postActionMock).toHaveBeenLastCalledWith('workspace_open', {
      dir: 'C:\\杂物\\旧文件夹',
      confirm: true,
    });
    expect(postActionMock.mock.calls.map(([name]) => name)).not.toContain('post_stop');
  });

  it('非空目录 × offline 在飞：收编问上取消——零发送', async () => {
    fetchSessionsMock.mockResolvedValue(flyingList());
    browseDirsMock.mockResolvedValue(nonEmptyDirs);
    const renderer = await mountHome();

    await click(renderer, buttonByText(renderer, '添加工作区'));
    await act(async () => {
      pickerRow(renderer, '旧文件夹').props.onClick();
    });
    await click(renderer, buttonByText(renderer, '选定这个目录'));
    await click(renderer, buttonByText(renderer, '取消'));

    expect(postActionMock).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 目录选择器「选中再确认」（节点 57ab5255）：spec 完成判定的四条 + 换层清选中
// ---------------------------------------------------------------------------

describe('目录选择器「选中再确认」（57ab5255）', () => {
  it('地址栏：输入存在的路径回车跳过去——地址栏与清单按新层渲染', async () => {
    fetchSessionsMock.mockResolvedValue(list());
    browseDirsMock.mockResolvedValue(twoKindDirs);
    const renderer = await mountHome();
    await click(renderer, buttonByText(renderer, '添加工作区'));

    const deeper: DirsResponse = {
      home: 'C:\\Users\\<user>',
      cwd: 'C:\\会议',
      up: 'C:\\Users\\<user>',
      here: { name: '会议', dir: 'C:\\会议', kind: 'unknown', suspected: false },
      dirs: [
        { name: '跟老王聊周会', dir: 'C:\\会议\\跟老王聊周会', kind: 'meeting', suspected: false },
      ],
    };
    browseDirsMock.mockResolvedValue(deeper);
    // onChange 与回车分两拍：同一拍里拿到的是旧渲染的闭包，addr 还没进到 onKeyDown
    await act(async () => {
      pickerAddr(renderer).props.onChange({ target: { value: 'C:\\会议' } });
    });
    await act(async () => {
      pickerAddr(renderer).props.onKeyDown({ key: 'Enter' });
    });

    expect(browseDirsMock).toHaveBeenLastCalledWith('C:\\会议');
    expect(pickerAddr(renderer).props.value).toBe('C:\\会议');
    expect(textOf(pickerRow(renderer, '跟老王聊周会'))).toContain('会议目录');
    // 旧层的那一行不跟着过来
    expect(
      renderer.root.findAll(
        (node) => node.props.role === 'option' && textOf(node).includes('旧文件夹'),
      ),
    ).toHaveLength(0);
  });

  it('地址栏：输入不存在的路径回车——画出「跳不过去」，层不动、输入还在', async () => {
    fetchSessionsMock.mockResolvedValue(list());
    browseDirsMock.mockResolvedValue(twoKindDirs);
    const renderer = await mountHome();
    await click(renderer, buttonByText(renderer, '添加工作区'));

    browseDirsMock.mockRejectedValueOnce(new Error('要浏览的目录不存在：C:\\没有这个地方'));
    await act(async () => {
      pickerAddr(renderer).props.onChange({ target: { value: 'C:\\没有这个地方' } });
    });
    await act(async () => {
      pickerAddr(renderer).props.onKeyDown({ key: 'Enter' });
    });

    expect(browseDirsMock).toHaveBeenLastCalledWith('C:\\没有这个地方');
    expect(textOf(renderer.root)).toContain('跳不过去');
    // 层不动：清单还是原来那一层；地址栏值留在用户输入上
    pickerRow(renderer, '新场');
    pickerRow(renderer, '旧文件夹');
    expect(pickerAddr(renderer).props.value).toBe('C:\\没有这个地方');
  });

  it('单击只选中：行上出选中样式、零动作请求；双击同条目进下层', async () => {
    fetchSessionsMock.mockResolvedValue(list());
    browseDirsMock.mockResolvedValue(twoKindDirs);
    const renderer = await mountHome();
    await click(renderer, buttonByText(renderer, '添加工作区'));

    await act(async () => {
      pickerRow(renderer, '新场').props.onClick();
    });

    const pickedRow = pickerRow(renderer, '新场');
    expect(pickedRow.props['aria-selected']).toBe(true);
    expect(`${pickedRow.props.className}`).toContain('picker__row--on');
    expect(postActionMock).not.toHaveBeenCalled();
    // 另一行不背选中样式
    expect(`${pickerRow(renderer, '旧文件夹').props.className}`).not.toContain('picker__row--on');

    browseDirsMock.mockResolvedValue({
      home: 'C:\\Users\\<user>',
      cwd: 'C:\\会议\\新场',
      up: 'C:\\Users\\<user>',
      here: { name: '新场', dir: 'C:\\会议\\新场', kind: 'empty', suspected: false },
      dirs: [],
    });
    await act(async () => {
      pickerRow(renderer, '新场').props.onDoubleClick();
    });
    expect(browseDirsMock).toHaveBeenLastCalledWith('C:\\会议\\新场');
  });

  it('「向上一层」住在地址栏旁：盘根置灰；主目录不再置灰（点了退上一层）', async () => {
    fetchSessionsMock.mockResolvedValue(list());
    browseDirsMock.mockResolvedValue(twoKindDirs);
    const renderer = await mountHome();
    await click(renderer, buttonByText(renderer, '添加工作区'));

    // 主目录不再是天花板：从 home 也能向上（目录选择器节点的新口径）
    const upAtHome = buttonByText(renderer, '向上一层');
    expect(upAtHome.props.disabled).toBe(false);
    // 位置：与地址栏输入框同一个容器行（地址栏右边那颗）
    expect(upAtHome.parent?.findAll((node) => node.type === 'input')).toHaveLength(1);
    browseDirsMock.mockResolvedValue({
      home: 'C:\\Users\\<user>',
      cwd: 'C:\\Users',
      up: 'C:\\',
      here: { name: 'Users', dir: 'C:\\Users', kind: 'unknown', suspected: false },
      dirs: [{ name: 'PC', dir: 'C:\\Users\\<user>', kind: 'unknown', suspected: false }],
    });
    await click(renderer, upAtHome);
    expect(browseDirsMock).toHaveBeenLastCalledWith('C:\\Users');

    // 盘根那一档 up 如实 null：按钮置灰（「向上一层」唯一的到顶档）
    browseDirsMock.mockResolvedValue({
      home: 'C:\\Users\\<user>',
      cwd: 'C:\\',
      up: null,
      here: { name: 'C:\\', dir: 'C:\\', kind: 'unknown', suspected: false },
      dirs: [{ name: 'Users', dir: 'C:\\Users', kind: 'unknown', suspected: false }],
    });
    await act(async () => {
      pickerAddr(renderer).props.onChange({ target: { value: 'C:\\' } });
    });
    await act(async () => {
      pickerAddr(renderer).props.onKeyDown({ key: 'Enter' });
    });

    const upAtRoot = buttonByText(renderer, '向上一层');
    expect(upAtRoot.props.disabled).toBe(true);
  });

  it('底部「选定这个目录」：未选中置灰零请求；选中 empty 后点它才发 workspace_open', async () => {
    fetchSessionsMock.mockResolvedValue(list());
    browseDirsMock.mockResolvedValue(twoKindDirs);
    postActionMock.mockResolvedValue({ kind: 'ok', ok: true, revision: 21 });
    const renderer = await mountHome();
    await click(renderer, buttonByText(renderer, '添加工作区'));

    const confirmBtn = buttonByText(renderer, '选定这个目录');
    expect(confirmBtn.props.disabled).toBe(true);
    expect(postActionMock).not.toHaveBeenCalled();

    await act(async () => {
      pickerRow(renderer, '新场').props.onClick();
    });
    const armed = buttonByText(renderer, '选定这个目录');
    expect(armed.props.disabled).toBe(false);
    await click(renderer, armed);

    expect(postActionMock).toHaveBeenLastCalledWith('workspace_open', { dir: 'C:\\会议\\新场' });
    // 确认即收弹层
    expect(renderer.root.findAll((node) => node.props.role === 'dialog')).toHaveLength(0);
  });

  it('选中 unknown 点确认：走收编确认弹窗，确认后带 confirm 登记——单击依旧零请求', async () => {
    fetchSessionsMock.mockResolvedValue(list());
    browseDirsMock.mockResolvedValue(twoKindDirs);
    postActionMock.mockResolvedValue({ kind: 'ok', ok: true, revision: 22 });
    const renderer = await mountHome();
    await click(renderer, buttonByText(renderer, '添加工作区'));

    await act(async () => {
      pickerRow(renderer, '旧文件夹').props.onClick();
    });
    expect(postActionMock).not.toHaveBeenCalled();

    await click(renderer, buttonByText(renderer, '选定这个目录'));

    const dialogs = renderer.root.findAll((node) => node.props.role === 'dialog');
    expect(dialogs).toHaveLength(1);
    expect(textOf(dialogs[0] as TestRenderer.ReactTestInstance)).toContain('将该目录添加为工作区？');
    expect(postActionMock).not.toHaveBeenCalled();

    await click(renderer, buttonByText(renderer, '确认添加'));
    expect(postActionMock).toHaveBeenLastCalledWith('workspace_open', {
      dir: 'C:\\杂物\\旧文件夹',
      confirm: true,
    });
  });

  it('选中 meeting 目录点确认：直接 workspace_open 登记，不弹收编确认', async () => {
    fetchSessionsMock.mockResolvedValue(list());
    browseDirsMock.mockResolvedValue({
      home: 'C:\\Users\\<user>',
      cwd: 'C:\\Users\\<user>',
      up: 'C:\\Users',
      here: { name: 'PC', dir: 'C:\\Users\\<user>', kind: 'unknown', suspected: false },
      dirs: [
        { name: '旧会议', dir: 'C:\\会议\\旧会议', kind: 'meeting', suspected: false },
      ],
    });
    postActionMock.mockResolvedValue({ kind: 'ok', ok: true, revision: 23 });
    const renderer = await mountHome();
    await click(renderer, buttonByText(renderer, '添加工作区'));

    await act(async () => {
      pickerRow(renderer, '旧会议').props.onClick();
    });
    expect(postActionMock).not.toHaveBeenCalled();

    await click(renderer, buttonByText(renderer, '选定这个目录'));

    expect(renderer.root.findAll((node) => node.props.role === 'dialog')).toHaveLength(0);
    expect(postActionMock).toHaveBeenLastCalledWith('workspace_open', { dir: 'C:\\会议\\旧会议' });
  });

  it('换层后选中不跨层带走：确认钮回到置灰', async () => {
    fetchSessionsMock.mockResolvedValue(list());
    browseDirsMock.mockResolvedValue(twoKindDirs);
    const renderer = await mountHome();
    await click(renderer, buttonByText(renderer, '添加工作区'));

    await act(async () => {
      pickerRow(renderer, '新场').props.onClick();
    });
    expect(buttonByText(renderer, '选定这个目录').props.disabled).toBe(false);

    browseDirsMock.mockResolvedValue({
      home: 'C:\\Users\\<user>',
      cwd: 'C:\\会议',
      up: 'C:\\Users\\<user>',
      here: { name: '会议', dir: 'C:\\会议', kind: 'unknown', suspected: false },
      dirs: [],
    });
    await act(async () => {
      pickerRow(renderer, '新场').props.onDoubleClick();
    });

    expect(buttonByText(renderer, '选定这个目录').props.disabled).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 落盘那一小块（d61829f1）：停止录音就回工作台，收尾还在跑也回 ——
// 详情里那一小块如实说「还差最后一块没进盘」，且不阻塞会后那两个按钮。
// （闸的判据「ended 任意相都回」在 home.test.ts 单独锁；这里锁回得来之后的画。）
// ---------------------------------------------------------------------------

describe('落盘那一小块（d61829f1，ui-design 4.2 二）', () => {
  /** 一份「收尾还在跑」的快照 ended（HomeGate 从快照上摘下来转手的那几格）。 */
  function drainingEnded(): Ended {
    return {
      phase: 'draining',
      error: null,
      steps: [
        { id: 'flush', status: 'running', detail: '', error: null },
        { id: 'log', status: 'done', detail: '', error: null },
        { id: 'post', status: 'waiting', detail: '', error: null },
      ],
    };
  }

  it('停止录音就回工作台（落盘仍在跑那一档）：详情打得开，小块写「还差最后一块没进盘」', async () => {
    fetchSessionsMock.mockResolvedValue(
      list({ sessions: [row({ state: 'ended' })], recent: 'C:\\会议\\跟老王聊周会' }),
    );
    let renderer!: TestRenderer.ReactTestRenderer;
    await act(async () => {
      renderer = TestRenderer.create(
        createElement(WorkspaceHome, {
          heartbeatSeconds: 10,
          drain: {
            ended: drainingEnded(),
            dir: 'C:\\会议\\跟老王聊周会',
            logPath: 'C:\\log\\meeting.log',
          },
        }),
      );
    });

    const blocks = renderer.root.findAll((node) => node.props['aria-label'] === '落盘进度');
    expect(blocks, '快照 ended 转手进来后小块现身').toHaveLength(1);
    const text = textOf(blocks[0] as TestRenderer.ReactTestInstance);
    expect(text).toContain('还差最后一块没进盘');
    expect(text).toContain('落盘：进行中');
    expect(text).toContain('启动会后：等待');
    // 不阻塞任何东西：会后那两个按钮照常可按（家务进度不拦人）
    expect(buttonByText(renderer, '生成会后精细稿').props.disabled).toBe(false);
    await act(async () => {
      renderer.unmount();
    });
  });

  it('收尾落定那一拍：小块翻成「录音已落盘」', async () => {
    /** 一份「收尾落定」的快照 ended（post 停在 waiting 也算落定）。 */
    function settledEnded(): Ended {
      return {
        phase: 'done',
        error: null,
        steps: [
          { id: 'flush', status: 'done', detail: '', error: null },
          { id: 'log', status: 'done', detail: '', error: null },
          { id: 'post', status: 'waiting', detail: '', error: null },
        ],
      };
    }
    fetchSessionsMock.mockResolvedValue(
      list({ sessions: [row({ state: 'ended' })], recent: 'C:\\会议\\跟老王聊周会' }),
    );
    let renderer!: TestRenderer.ReactTestRenderer;
    await act(async () => {
      renderer = TestRenderer.create(
        createElement(WorkspaceHome, {
          heartbeatSeconds: 10,
          drain: {
            ended: settledEnded(),
            dir: 'C:\\会议\\跟老王聊周会',
            logPath: null,
          },
        }),
      );
    });

    const blocks = renderer.root.findAll((node) => node.props['aria-label'] === '落盘进度');
    expect(textOf(blocks[0] as TestRenderer.ReactTestInstance)).toContain('录音已落盘');
    await act(async () => {
      renderer.unmount();
    });
  });
});



// ---------------------------------------------------------------------------
// 稿子正文与事后工序链（38817ecf）：默认会中稿、切换换正文、缺稿给那一句；
// 链上四段与「能不能走」那一行。mount 级接线：取数打在 api/sessions 边界的替身上。
// ---------------------------------------------------------------------------

/** 一份「录音已结束」的单场读数（链与稿子两块的门槛读数）。 */
function endedStatus(dir: string): SessionStatus {
  return {
    ...neverRunStatus(dir),
    session_id: '这一场',
    kind: 'meeting',
    state: 'ended',
    date: '2025-09-06',
    mode: 'both',
  };
}

/** 两路正文都在场的稿子应答体（形状照契约 4.13）。 */
function bothRoads(dir: string): SessionTranscripts {
  return {
    dir,
    kinds: [
      {
        kind: 'live',
        sources: [
          { source: 'mic', text: '[20:01] 本地一行\n' },
          { source: 'loopback', text: '[20:02] 远端一行\n' },
        ],
      },
      {
        kind: 'offline',
        sources: [
          { source: 'mic', text: '[00:00] 会后本地一行\n' },
          { source: 'loopback', reason: 'absent' },
        ],
      },
    ],
  };
}

/** 那一块「稿子」（aria-label 定位）的全文。 */
function srcText(renderer: TestRenderer.ReactTestRenderer): string {
  const blocks = renderer.root.findAll((node) => node.props['aria-label'] === '稿子');
  expect(blocks, '界面上该有「稿子」那一块').toHaveLength(1);
  return textOf(blocks[0] as TestRenderer.ReactTestInstance);
}

describe('稿子正文与事后工序链（38817ecf）', () => {
  const dir = 'C:\会议\这一场';

  function mountEnded(transcripts: SessionTranscripts): Promise<TestRenderer.ReactTestRenderer> {
    fetchSessionsMock.mockResolvedValue(
      list({ sessions: [row({ dir, state: 'ended', session_id: '这一场' })], recent: dir }),
    );
    fetchSessionStatusMock.mockImplementation(async (asked: string) => endedStatus(asked));
    fetchSessionTranscriptsMock.mockResolvedValue(transcripts);
    return mountHome();
  }

  it('默认落在会中稿、两路正文都在场；点「会后稿」之后正文换掉', async () => {
    const renderer = await mountEnded(bothRoads(dir));

    const first = srcText(renderer);
    expect(first).toContain('会中稿');
    expect(first).toContain('[20:01] 本地一行');
    expect(first).toContain('[20:02] 远端一行');
    expect(first).not.toContain('会后本地一行');

    await click(renderer, buttonByText(renderer, '会后稿'));
    const second = srcText(renderer);
    expect(second).toContain('会后稿');
    expect(second).toContain('[00:00] 会后本地一行');
    expect(second).not.toContain('[20:01] 本地一行');
    // 缺的那一路照常占位，给那句读得懂的缺稿话
    expect(second).toContain('会后稿还没有远端这一路：按「生成会后精细稿」之后才有。');
    await act(async () => {
      renderer.unmount();
    });
  });

  it('缺一路（unrecorded）：缺稿那一档照常给出「没有」这一句', async () => {
    const renderer = await mountEnded({
      dir,
      kinds: [
        {
          kind: 'live',
          sources: [
            { source: 'mic', text: '[20:01] 本地一行\n' },
            { source: 'loopback', reason: 'unrecorded' },
          ],
        },
        { kind: 'offline', sources: [] },
      ],
    });

    const text = srcText(renderer);
    expect(text).toContain('这一场没有采远端这一路，没有它的会中稿。');
    await act(async () => {
      renderer.unmount();
    });
  });

  it('工序链：已结束的场次摆出四段与「能不能走」那一行；稿子取失败只在那一块里说', async () => {
    fetchSessionsMock.mockResolvedValue(
      list({ sessions: [row({ dir, state: 'ended', session_id: '这一场' })], recent: dir }),
    );
    fetchSessionStatusMock.mockImplementation(async (asked: string) => endedStatus(asked));
    fetchSessionTranscriptsMock.mockRejectedValue(new Error('取不到这一场的稿子：服务没应答'));
    const renderer = await mountHome();

    const chains = renderer.root.findAll((node) => node.props['aria-label'] === '事后工序');
    expect(chains).toHaveLength(1);
    const chainText = textOf(chains[0] as TestRenderer.ReactTestInstance);
    expect(chainText).toContain('可以走了：这一场眼下没有在跑的活。');
    expect(chainText).toContain('落盘');
    expect(chainText).toContain('转码');
    expect(chainText).toContain('会后转写');
    expect(chainText).toContain('纪要');
    // 取稿失败只在稿子那一块里如实说，不顶掉首页的失败块
    expect(srcText(renderer)).toContain('取不到这一场的稿子');
    expect(renderer.root.findAll((node) => node.props.className === 'fail')).toHaveLength(0);
    await act(async () => {
      renderer.unmount();
    });
  });

  it('转码在飞：链上那一段给出「暂停」，点了发出 transcode_pause 并带上 dir', async () => {
    fetchSessionsMock.mockResolvedValue(
      list({ sessions: [row({ dir, state: 'ended', session_id: '这一场' })], recent: dir }),
    );
    fetchSessionStatusMock.mockImplementation(async (asked: string) => ({
      ...endedStatus(asked),
      transcode: {
        state: 'running' as const,
        in_process: true,
        started_at: '2025-09-06T22:01:00',
        finished_at: null,
        elapsed_seconds: 3.2,
        current_source: 'mic' as const,
        sources: [{ source: 'mic' as const, done: 1, total: 8, remaining: 7, current_segment: 2 }],
      },
    }));
    fetchSessionTranscriptsMock.mockResolvedValue(bothRoads(dir));
    postActionMock.mockResolvedValue({ kind: 'transcode', ok: true, revision: 9, transcode: { kind: 'transcode', dir, pausing: true } });
    const renderer = await mountHome();

    await click(renderer, buttonByText(renderer, '暂停'));
    expect(postActionMock).toHaveBeenLastCalledWith('transcode_pause', { dir });
    await act(async () => {
      renderer.unmount();
    });
  });

  it('还在录的那一场：链与稿子两块都不摆（会后的事还没开始）', async () => {
    fetchSessionsMock.mockResolvedValue(
      list({ sessions: [row({ dir, state: 'recording', session_id: '这一场' })], recent: dir }),
    );
    fetchSessionStatusMock.mockImplementation(
      async (asked: string) => ({ ...endedStatus(asked), state: 'recording' as const }),
    );
    fetchSessionTranscriptsMock.mockResolvedValue(bothRoads(dir));
    const renderer = await mountHome();

    expect(renderer.root.findAll((node) => node.props['aria-label'] === '事后工序')).toHaveLength(0);
    expect(renderer.root.findAll((node) => node.props['aria-label'] === '稿子')).toHaveLength(0);
    await act(async () => {
      renderer.unmount();
    });
  });
});

// ---------------------------------------------------------------------------
// 开始钮的「起录在飞」一档（spec per-session-model-reload-starves-capture 修法二）：
// 点开始 → 在飞期间画「正在启动…」并保持禁用（现状的病：禁用但文案不变）；落定
// （成功 / 被拒）收回原文案。「正在录制」的确认时刻是快照翻 recording 后的既有闸
// （HomeGate 切会中屏），本文件不摆 SSE —— 落定之后钮收回原文案就是这里的落点。
// ---------------------------------------------------------------------------

describe('开始钮的「起录在飞」一档（per-session-model-reload-starves-capture）', () => {
  it('点开始录音：在飞期间画「正在启动…」并保持禁用；成功落定后收回原文案、回到可按', async () => {
    fetchSessionsMock.mockResolvedValue(
      list({ sessions: [row({ dir: 'C:\\会议\\这一场' })], recent: 'C:\\会议\\这一场' }),
    );
    // `start` 那一跳挂在手上不放（在飞现场），其余动作照常受理。
    let releaseStart!: () => void;
    postActionMock.mockImplementation((name: string) => {
      if (name !== 'start') return Promise.resolve({ kind: 'ok', ok: true, revision: 1 });
      return new Promise<ActionResult>((resolve) => {
        releaseStart = () => resolve({ kind: 'ok', ok: true, revision: 1 });
      });
    });
    const renderer = await mountHome();

    const idle = buttonByText(renderer, '开始录音');
    expect(idle.props.disabled).toBe(false);
    await click(renderer, idle);

    // 在飞：文案换成「正在启动…」，钮禁用
    const flying = buttonByText(renderer, '正在启动…');
    expect(flying.props.disabled).toBe(true);

    await act(async () => {
      releaseStart();
    });

    // 成功落定：收回原文案、回到可按（真页面上此刻快照翻 recording、闸切会中屏）
    const settled = buttonByText(renderer, '开始录音');
    expect(settled.props.disabled).toBe(false);
  });

  it('起录被拒：失败块出列，钮收回原文案、回到可按（不楔死在「正在启动…」）', async () => {
    fetchSessionsMock.mockResolvedValue(
      list({ sessions: [row({ dir: 'C:\\会议\\这一场' })], recent: 'C:\\会议\\这一场' }),
    );
    postActionMock.mockRejectedValue(
      new ActionFailedError(409, 'already_recording', '已经有一场在录', {
        session_id: '别场',
        dir: 'C:\\会议\\别场',
        elapsed_seconds: 0,
      }),
    );
    const renderer = await mountHome();

    await click(renderer, buttonByText(renderer, '开始录音'));

    const failures = renderer.root.findAll((node) => node.props.className === 'fail');
    expect(failures).toHaveLength(1);
    expect(textOf(failures[0] as TestRenderer.ReactTestInstance)).toContain('已经有一场在录');
    expect(buttonByText(renderer, '开始录音').props.disabled).toBe(false);
  });
});
