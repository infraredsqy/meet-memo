import { describe, expect, it, vi, afterEach } from 'vitest';
import { createElement } from 'react';
import TestRenderer, { act } from 'react-test-renderer';
import { WorkspaceHome } from '../src/home/WorkspaceHome';
import { fetchSessions, browseDirs } from '../src/api/sessions';
import type { SessionRow, SessionsResponse } from '../src/contract';

/**
 * review-2 m2：「挂载即取列表」接线用例（免 DOM 跑法）。
 *
 * 老毛病是纯函数全绿、组件挂载那一次 `fetchSessions()` 没人发（review-2 C1）——
 * 这里用 react-test-renderer 在 node 环境真挂载 `WorkspaceHome`，替身打在
 * `src/api/sessions` 这条边界上，断言「挂载 → 请求发出 → 行/空态出列」，
 * 不只测纯函数。vitest 跑在 node 环境（vitest.config.ts），react-test-renderer
 * 正好不需要 DOM；JSX 不落盘（createElement 直造），测试不碰 jsx 配置。
 */

vi.mock('../src/api/sessions', () => ({
  fetchSessions: vi.fn(),
  browseDirs: vi.fn(),
}));

const fetchSessionsMock = vi.mocked(fetchSessions);
const browseDirsMock = vi.mocked(browseDirs);

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
