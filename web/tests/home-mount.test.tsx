import { describe, expect, it, vi, afterEach } from 'vitest';
import { createElement } from 'react';
import TestRenderer, { act } from 'react-test-renderer';
import { WorkspaceHome } from '../src/home/WorkspaceHome';
import { fetchSessions, browseDirs } from '../src/api/sessions';
import { postAction } from '../src/api';
// 这一条**不经 `../src/api`**（那个模块在本文件里被 mock 掉了，类拿到的会是替身）
import { ActionFailedError } from '../src/api/actions';
import type { SessionRow, SessionsResponse } from '../src/contract';

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
 */

vi.mock('../src/api/sessions', () => ({
  fetchSessions: vi.fn(),
  browseDirs: vi.fn(),
}));

vi.mock('../src/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api')>();
  return { ...actual, postAction: vi.fn() };
});

const fetchSessionsMock = vi.mocked(fetchSessions);
const browseDirsMock = vi.mocked(browseDirs);
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
    has_offline: false,
    added_at: '2026-09-21T21:03:00+08:00',
    last_opened_at: '2026-09-21T21:03:00+08:00',
    ...over,
  };
}

function list(over: Partial<SessionsResponse> = {}): SessionsResponse {
  return { sessions: [], recent: null, ...over };
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
    (node) => node.props.role === 'option' && node.parent?.props['aria-label'] === '这些会',
  );
}

afterEach(() => {
  fetchSessionsMock.mockReset();
  browseDirsMock.mockReset();
  postActionMock.mockReset();
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

  it('空注册表：出的是空态（logo + 新会议指引），不再是卡死的「正在取列表…」', async () => {
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
  it('点「生成会后精细稿」发出 post_offline、点「直接出纪要」发出 post_minutes，都带上选中那一行的 dir', async () => {
    fetchSessionsMock.mockResolvedValue(
      list({ sessions: [row({ dir: 'C:\\会议\\这一场', session_id: '这一场' })], recent: 'C:\\会议\\这一场' }),
    );
    postActionMock.mockResolvedValue({ kind: 'ok', ok: true, revision: 7 });
    const renderer = await mountHome();

    await click(renderer, buttonByText(renderer, '生成会后精细稿'));
    expect(postActionMock).toHaveBeenLastCalledWith('post_offline', { dir: 'C:\\会议\\这一场' });

    await click(renderer, buttonByText(renderer, '直接出纪要'));
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

    expect(postLines(renderer).join('\n')).toContain('这一场有会后精细稿');

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
    expect(postLines(renderer).join('\n')).toContain('这一场还没有会后精细稿');
  });

  it('按钮被拒（409 draining）时失败块出列，回执那一行不留假成功', async () => {
    fetchSessionsMock.mockResolvedValue(
      list({ sessions: [row({ dir: 'C:\\会议\\这一场' })], recent: 'C:\\会议\\这一场' }),
    );
    postActionMock.mockRejectedValue(
      new ActionFailedError(409, 'draining', '纪要这一层已经在飞', { kind: 'minutes' }),
    );
    const renderer = await mountHome();

    await click(renderer, buttonByText(renderer, '直接出纪要'));

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
    expect(buttonByText(renderer, '直接出纪要').props.disabled).toBe(true);
    const whys = renderer.root
      .findAll((node) => node.props.className === 'wspane__why')
      .map((node) => `${node.props.children}`);
    expect(whys.join('\n')).toContain('目录已不在原地');
    // 「路径已不在」是**路径那一格**（`wslist__state`）的说法，不摆到会后那一格上（review-2 m1）
    expect(postLines(renderer).join('\n')).not.toContain('路径已不在');
    expect(postLines(renderer).join('\n')).toContain('这一场跑不了会后：目录不在原地');
    const stateCells = renderer.root
      .findAll((node) => `${node.props.className}`.includes('wslist__state'))
      .map((node) => textOf(node));
    expect(stateCells.join('\n'), '那一行还是按路径那一格说「路径已不在」').toContain('路径已不在');
  });

  it('还在录那一档：两个按钮都按不动，那一格把「还在录」与「盘上会后稿：有」并列说（review-2 C1/m2）', async () => {
    fetchSessionsMock.mockResolvedValue(
      list({
        sessions: [row({ dir: 'C:\\会议\\在录', state: 'recording', has_offline: true })],
        recent: 'C:\\会议\\在录',
      }),
    );
    const renderer = await mountHome();

    expect(buttonByText(renderer, '生成会后精细稿').props.disabled).toBe(true);
    expect(buttonByText(renderer, '直接出纪要').props.disabled).toBe(true);
    const whys = renderer.root
      .findAll((node) => node.props.className === 'wspane__why')
      .map((node) => `${node.props.children}`);
    expect(whys.join('\n')).toContain('这一场还在录 / 还在收尾，等它落定再按');
    const lines = postLines(renderer).join('\n');
    // 两件事都在：这一场还没落定 + 盘上此刻那一份的读数（不许哪一句盖住另一句）
    expect(lines).toContain('这一场还在录 / 还在收尾');
    expect(lines).toContain('盘上会后稿：有');
    // 也不许单方面断言「这一场有会后精细稿」（那是落定之后那一档的话）
    expect(lines).not.toContain('这一场有会后精细稿');
  });

  it('还在收尾那一档：两个按钮都按不动，那一格照实说「盘上会后稿：没有」（review-2 C1/m2）', async () => {
    fetchSessionsMock.mockResolvedValue(
      list({
        sessions: [row({ dir: 'C:\\会议\\在收尾', state: 'draining', has_offline: false })],
        recent: 'C:\\会议\\在收尾',
      }),
    );
    const renderer = await mountHome();

    expect(buttonByText(renderer, '生成会后精细稿').props.disabled).toBe(true);
    expect(buttonByText(renderer, '直接出纪要').props.disabled).toBe(true);
    const lines = postLines(renderer).join('\n');
    expect(lines).toContain('这一场还在录 / 还在收尾');
    expect(lines).toContain('盘上会后稿：没有');
    expect(lines).not.toContain('这一场还没有会后精细稿');
  });
});
