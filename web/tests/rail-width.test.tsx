import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { createElement } from 'react';
import TestRenderer, { act } from 'react-test-renderer';

import { WorkspaceRail } from '../src/home/WorkspaceRail';
import { gpuBusyView } from '../src/home/progress';
import {
  clampRailWidth,
  railWidthBoundsPx,
  RAIL_WIDTH_STORAGE_KEY,
  remPixelSize,
  storedRailWidth,
} from '../src/home/railWidth';

/**
 * 左栏宽度（可拖，两屏共用一条）的不变量（`home/railWidth.ts` + `WorkspaceRail`）：
 *
 * - 拖柄只在分栏的宽度档出现（窄档没有；从窄回到宽，拖柄回来、记住的宽度照旧生效）；
 * - 拖动改宽度（松手才写盘，拖动过程不折腾 localStorage）；
 * - 上 / 下界各夹一条：拖过头停在边界上；
 * - 双击回默认；
 * - 记住的宽度在新一次挂载生效（刷新 / 重开浏览器 = 再挂载一次）；
 * - 两屏读到同一条宽度（两屏各自挂载读同一份存档，谁后挂载都是同一档）；
 * - localStorage 只记宽度这一件事，不记任何会话状态。
 *
 * 手法照 `live-wsbar.test.tsx`：`react-test-renderer` 在 node 环境真挂载，
 * window / document / localStorage 全是替身 —— 不发声、不连网、不起任何真进程。
 */

const REM_PX = 16; // 替身环境没有真 DOM，remPixelSize() 照 16 算
const BOUNDS = railWidthBoundsPx(REM_PX);

/** localStorage 替身：一个 Map。 */
function fakeStorage(seed: Record<string, string> = {}) {
  const map = new Map(Object.entries(seed));
  return {
    getItem: (key: string): string | null => (map.has(key) ? (map.get(key) as string) : null),
    setItem: (key: string, value: string): void => void map.set(key, String(value)),
    removeItem: (key: string): void => void map.delete(key),
    keys: (): string[] => Array.from(map.keys()),
  };
}

type MqlFake = {
  matches: boolean;
  listeners: Array<() => void>;
};

/** window 替身：capture 事件、matchMedia（窄档 = matches 真）、localStorage。 */
function fakeWindow(storage: ReturnType<typeof fakeStorage>, mql: MqlFake) {
  const listeners = new Map<string, Array<(event: unknown) => void>>();
  return {
    addEventListener: (type: string, fn: (event: unknown) => void): void => {
      listeners.set(type, [...(listeners.get(type) ?? []), fn]);
    },
    removeEventListener: (type: string, fn: (event: unknown) => void): void => {
      listeners.set(type, (listeners.get(type) ?? []).filter((kept) => kept !== fn));
    },
    localStorage: storage,
    matchMedia: (query: string) => ({
      get matches() {
        return mql.matches; // 活读：翻档之后已造出的查询对象跟着翻
      },
      media: query,
      addEventListener: (_type: string, fn: () => void): void => void mql.listeners.push(fn),
      removeEventListener: (_type: string, fn: () => void): void => {
        mql.listeners = mql.listeners.filter((kept) => kept !== fn);
      },
    }),
    fire: (type: string, event: unknown): void => {
      for (const fn of listeners.get(type) ?? []) fn(event);
    },
  };
}

/** document 替身：根节点的 style 记下写进来的每一条变量。 */
function fakeDocument() {
  const vars = new Map<string, string>();
  const rootEl = {
    getAttribute: (): string | null => null, // ThemeToggle 挂载要读 data-theme
    style: {
      setProperty: (key: string, value: string): void => void vars.set(key, value),
      removeProperty: (key: string): void => void vars.delete(key),
    },
  };
  return {
    body: {},
    documentElement: rootEl,
    vars,
  };
}

type FakeWorld = {
  storage: ReturnType<typeof fakeStorage>;
  mql: MqlFake;
  win: ReturnType<typeof fakeWindow>;
  doc: ReturnType<typeof fakeDocument>;
};

let world: FakeWorld | null = null;
let mounted: TestRenderer.ReactTestRenderer | null = null;

async function mountRail(): Promise<TestRenderer.ReactTestRenderer> {
  let renderer!: TestRenderer.ReactTestRenderer;
  await act(async () => {
    renderer = TestRenderer.create(
      createElement(WorkspaceRail, {
        sessions: null,
        selection: null,
        postJobs: [],
        gpu: gpuBusyView([], false),
        busy: false,
        onRowActivate: () => undefined,
        onReload: () => undefined,
        onFailure: () => undefined,
      }),
);
  });
  mounted = renderer;
  return renderer;
}

function gripsOf(renderer: TestRenderer.ReactTestRenderer): TestRenderer.ReactTestInstance[] {
  return renderer.root.findAll((node) => node.props.role === 'separator');
}

/** 那一只拖柄；不在场就抛 —— 调用处不用再判 undefined。 */
function gripOf(renderer: TestRenderer.ReactTestRenderer): TestRenderer.ReactTestInstance {
  const grip = gripsOf(renderer)[0];
  if (grip === undefined) throw new Error('拖柄不在场');
  return grip;
}

function railWidthOf(renderer: TestRenderer.ReactTestRenderer): string {
  expect(world, '替身世界应在挂载前备好').not.toBeNull();
  const vars = (world as FakeWorld).doc.vars;
  const value = vars.get('--rail-w');
  expect(value, '宽度该写在根节点的 --rail-w 上').toBeDefined();
  return value as string;
}

/** 一次完整的拖：按下 → 横移一档 → 松手。 */
async function drag(
  renderer: TestRenderer.ReactTestRenderer,
  fromX: number,
  toX: number,
): Promise<void> {
  const grip = gripOf(renderer);
  await act(async () => {
    grip.props.onPointerDown({
      button: 0,
      clientX: fromX,
      pointerId: 1,
      preventDefault: () => undefined,
      currentTarget: { setPointerCapture: () => undefined },
    });
  });
  await act(async () => {
    (world as FakeWorld).win.fire('pointermove', { clientX: toX });
  });
  await act(async () => {
    (world as FakeWorld).win.fire('pointerup', {});
  });
}

beforeEach(() => {
  const storage = fakeStorage();
  const mql: MqlFake = { matches: false, listeners: [] }; // 缺省按宽屏（分栏）那档
  world = { storage, mql, win: fakeWindow(storage, mql), doc: fakeDocument() };
  (globalThis as unknown as { window: unknown }).window = world.win;
  (globalThis as unknown as { document: unknown }).document = world.doc;
});

afterEach(async () => {
  if (mounted !== null) {
    const renderer = mounted;
    mounted = null;
    await act(async () => renderer.unmount());
  }
  world = null;
  delete (globalThis as unknown as { window?: unknown }).window;
  delete (globalThis as unknown as { document?: unknown }).document;
});

describe('左栏宽度的夹法（纯函数）', () => {
  it('上下限各夹一条：拖过头停在边界上', () => {
    expect(clampRailWidth(10, REM_PX)).toBe(BOUNDS.min);
    expect(clampRailWidth(99_999, REM_PX)).toBe(BOUNDS.max);
    expect(clampRailWidth(BOUNDS.def, REM_PX)).toBe(BOUNDS.def);
  });

  it('存档里读不懂的值按没存过算，越界的值夹回界内', () => {
    expect(storedRailWidth(() => null, REM_PX)).toBeNull();
    for (const raw of ['', 'abc', '-5', '0', 'NaN']) {
      expect(storedRailWidth(() => raw, REM_PX)).toBeNull();
    }
    expect(storedRailWidth(() => '99999', REM_PX)).toBe(BOUNDS.max);
    expect(storedRailWidth(() => '1', REM_PX)).toBe(BOUNDS.min);
    expect(storedRailWidth(() => '300', REM_PX)).toBe(300);
  });

  it('remPixelSize 在没有真 DOM 的环境照 16 算', () => {
    expect(remPixelSize()).toBe(16);
  });
});

describe('左栏拖柄（WorkspaceRail）', () => {
  it('宽屏分栏那档拖柄在，窄档没有；跨回宽档拖柄回来', async () => {
    const renderer = await mountRail();
    expect(gripsOf(renderer), '分栏档该有拖柄').toHaveLength(1);

    (world as FakeWorld).mql.matches = true; // ≤840：摞成一栏
    await act(async () => {
      for (const fn of [...(world as FakeWorld).mql.listeners]) fn();
    });
    expect(gripsOf(renderer), '窄档没有拖柄').toHaveLength(0);

    (world as FakeWorld).mql.matches = false; // 从窄回到宽屏
    await act(async () => {
      for (const fn of [...(world as FakeWorld).mql.listeners]) fn();
    });
    expect(gripsOf(renderer), '回到分栏档拖柄回来').toHaveLength(1);
  });

  it('窄档记住的宽度照旧生效：窄档挂着 --rail-w 也已是记住的那一档', async () => {
    world?.storage.setItem(RAIL_WIDTH_STORAGE_KEY, '312');
    if (world !== null) world.mql.matches = true; // ≤840：摞成一栏
    const renderer = await mountRail();
    expect(gripsOf(renderer), '窄档不摆拖柄').toHaveLength(0);
    expect(railWidthOf(renderer)).toBe('312px');
  });

  it('拖动改宽度，松手才写盘', async () => {
    const renderer = await mountRail();
    expect(railWidthOf(renderer)).toBe(`${BOUNDS.def}px`);

    const grip = gripOf(renderer);
    await act(async () => {
      grip.props.onPointerDown({
        button: 0,
        clientX: 300,
        pointerId: 1,
        preventDefault: () => undefined,
        currentTarget: { setPointerCapture: () => undefined },
      });
    });
    await act(async () => {
      (world as FakeWorld).win.fire('pointermove', { clientX: 340 });
    });
    expect(railWidthOf(renderer), '拖动中宽度就跟着走').toBe(`${BOUNDS.def + 40}px`);
    expect(world?.storage.keys(), '松手前不写盘').not.toContain(RAIL_WIDTH_STORAGE_KEY);

    await act(async () => {
      (world as FakeWorld).win.fire('pointerup', {});
    });
    expect(world?.storage.getItem(RAIL_WIDTH_STORAGE_KEY)).toBe(String(BOUNDS.def + 40));
  });

  it('上界夹住：一路往外拖停在最大那一档', async () => {
    const renderer = await mountRail();
    await drag(renderer, 300, 5_000);
    expect(railWidthOf(renderer)).toBe(`${BOUNDS.max}px`);
    expect(world?.storage.getItem(RAIL_WIDTH_STORAGE_KEY)).toBe(String(BOUNDS.max));
  });

  it('下界夹住：一路往里拖停在最小那一档', async () => {
    const renderer = await mountRail();
    await drag(renderer, 300, -5_000);
    expect(railWidthOf(renderer)).toBe(`${BOUNDS.min}px`);
    expect(world?.storage.getItem(RAIL_WIDTH_STORAGE_KEY)).toBe(String(BOUNDS.min));
  });

  it('双击回默认：宽度回缺省档，存档清掉', async () => {
    const renderer = await mountRail();
    await drag(renderer, 300, 340);
    expect(railWidthOf(renderer)).not.toBe(`${BOUNDS.def}px`);

    const grip = gripOf(renderer);
    await act(async () => {
      grip.props.onDoubleClick();
    });
    expect(railWidthOf(renderer)).toBe(`${BOUNDS.def}px`);
    expect(world?.storage.keys()).not.toContain(RAIL_WIDTH_STORAGE_KEY);
  });

  it('记住的宽度在新一次挂载生效（刷新 / 重开浏览器的替身）', async () => {
    const first = await mountRail();
    await drag(first, 300, 340);
    const width = railWidthOf(first);
    await act(async () => first.unmount());
    mounted = null;

    const second = await mountRail();
    expect(railWidthOf(second)).toBe(width);
  });

  it('两屏读到同一条宽度：各自挂载，谁后挂载都是同一档', async () => {
    world?.storage.setItem(RAIL_WIDTH_STORAGE_KEY, '312');

    const workbench = await mountRail(); // 工作台那一屏
    expect(railWidthOf(workbench)).toBe('312px');
    await act(async () => workbench.unmount());
    mounted = null;

    const live = await mountRail(); // 会中那一屏：同一条栏的另一次挂载
    expect(railWidthOf(live)).toBe('312px');
    expect(world?.storage.getItem(RAIL_WIDTH_STORAGE_KEY)).toBe('312');
  });

  it('localStorage 只记宽度这一件事：整趟拖完不多出任何别的键', async () => {
    world?.storage.setItem('meeting.theme', 'dark');
    const renderer = await mountRail();
    await drag(renderer, 300, 340);
    await drag(renderer, 340, 300);
    expect(world?.storage.keys()).toEqual(['meeting.theme', RAIL_WIDTH_STORAGE_KEY]);
  });
});
