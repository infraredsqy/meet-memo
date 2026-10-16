import { describe, expect, it, vi, afterEach } from 'vitest';
import { createElement, Fragment, useEffect, useState, type ReactNode } from 'react';
import TestRenderer, { act } from 'react-test-renderer';
import { DirPicker, useDirBrowse, type PickerPick } from '../src/home/DirPicker';
import { DirCreateHttpError, browseDirs, createDir } from '../src/api/sessions';
import type { DirsResponse } from '../src/contract';

/**
 * 目录选择器节点的新两样：「当前这一层」行与「新建目录」一行（组件级，免 DOM 跑法，
 * 手法与 `home-mount.test.tsx` 同源——`react-test-renderer` + 替身打在
 * `src/api/sessions` 这条边界上）。
 *
 * 判据照 spec：
 * - 正在浏览的这一层本身可选（清单顶上一行，单击选中），不必退到上一层再点那一行；
 * - 就地新建一个目录（名字必须用户给），建完**直接选中**——新行进清单的选定态，
 *   后续确认走既有「选定这个目录」链路；
 * - 建失败如实说：服务回的那句人话画在弹窗里，层不动、清单不动、零选中。
 *
 * 插行与选中的联动在 `useDirBrowse().createIn` + DirPicker 的组合上，这里挂一只
 * 小马甲（hook + 组件同装），替身只给数据。
 */

vi.mock('../src/api/sessions', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/sessions')>();
  return {
    ...actual,
    browseDirs: vi.fn(),
    createDir: vi.fn(),
  };
});

const browseDirsMock = vi.mocked(browseDirs);
const createDirMock = vi.mocked(createDir);

/** 一层现场：cwd 之下两个子目录，`here` 是这一层自己（unknown）。 */
function listing(dirs: DirsResponse['dirs']): DirsResponse {
  return {
    home: 'C:\\会议',
    cwd: 'C:\\会议',
    up: 'C:\\',
    here: { name: '会议', dir: 'C:\\会议', kind: 'unknown', suspected: false },
    dirs,
  };
}

const baseDirs: DirsResponse['dirs'] = [
  { name: '旧文件夹', dir: 'C:\\会议\\旧文件夹', kind: 'unknown', suspected: false },
];

function textOf(node: TestRenderer.ReactTestInstance): string {
  // 走 test-renderer 的 children（字符串子节点直接给字，元素递归）——比读 props.children 稳
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
  return parts.join('');
}

function optionsOf(
  renderer: TestRenderer.ReactTestRenderer,
): TestRenderer.ReactTestInstance[] {
  return renderer.root.findAll(
    (node) => node.props.role === 'option' && node.parent?.props['aria-label'] === '这一层的子目录',
  );
}

function buttonByText(
  renderer: TestRenderer.ReactTestRenderer,
  text: string,
): TestRenderer.ReactTestInstance {
  return renderer.root.find(
    (node) => node.type === 'button' && textOf(node) === text,
  );
}

function inputByLabel(
  renderer: TestRenderer.ReactTestRenderer,
  label: string,
): TestRenderer.ReactTestInstance {
  return renderer.root.find(
    (node) => node.type === 'input' && node.props['aria-label'] === label,
  );
}

/** 马甲：hook + 弹窗同装，picked 落到一只读数格里供断言。 */
function Harness({ startAt }: { startAt: string | null }): ReactNode {
  const dirBrowse = useDirBrowse(false);
  const [picked, setPicked] = useState<PickerPick | null>(null);
  useEffect(() => {
    dirBrowse.browseInto(startAt);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return createElement(
    Fragment,
    null,
    createElement(DirPicker, {
      browse: dirBrowse.browse,
      onBrowse: dirBrowse.browseInto,
      onJump: dirBrowse.jumpTo,
      onCreate: dirBrowse.createIn,
      onPick: (pick) => setPicked(pick),
      onClose: () => undefined,
    }),
    createElement(
      'div',
      { 'data-picked': picked === null ? 'none' : JSON.stringify(picked) },
    ),
  );
}

async function mountPicker(startAt: string | null = 'C:\\会议'): Promise<TestRenderer.ReactTestRenderer> {
  let renderer!: TestRenderer.ReactTestRenderer;
  await act(async () => {
    await TestRenderer.act(() => {
      renderer = TestRenderer.create(createElement(Harness, { startAt }));
    });
  });
  return renderer;
}

function pickedOf(renderer: TestRenderer.ReactTestRenderer): PickerPick | null {
  const cell = renderer.root.find((node) => 'data-picked' in node.props);
  const raw = cell.props['data-picked'] as string;
  return raw === 'none' ? null : (JSON.parse(raw) as PickerPick);
}

afterEach(() => {
  browseDirsMock.mockReset();
  createDirMock.mockReset();
});

describe('「当前这一层」行（正在浏览的这一层本身可选）', () => {
  it('清单顶上有「当前这一层」一行（虚线档，带这一层的名目）；单击选中，确认走既有 onPick 交接', async () => {
    browseDirsMock.mockResolvedValue(listing(baseDirs));
    const renderer = await mountPicker();

    const options = optionsOf(renderer);
    expect(options).toHaveLength(2);
    const hereRow = options[0]!;
    expect(`${hereRow.props.className}`).toContain('picker__row--here');
    expect(textOf(hereRow)).toContain('当前这一层');
    expect(textOf(hereRow)).toContain('普通目录');

    await act(async () => {
      hereRow.props.onClick();
    });
    expect(`${hereRow.props.className}`).toContain('picker__row--on');
    expect(hereRow.props['aria-selected']).toBe(true);
    // 确认链路是既有的那一条：确认钮可按，点它把当前层交给 onPick（kind 跟着走）
    const confirm = buttonByText(renderer, '选定这个目录');
    expect(confirm.props.disabled).toBe(false);
    await act(async () => {
      confirm.props.onClick();
    });
    expect(pickedOf(renderer)).toEqual({
      what: 'dir',
      dir: 'C:\\会议',
      kind: 'unknown',
    });
  });
});

describe('「新建目录」一行（就地建一个，建完直接选中）', () => {
  it('输入名字点「新建目录」：createDir 打在当前层上；成功后新行进清单选定态、确认钮可按', async () => {
    browseDirsMock.mockResolvedValue(listing(baseDirs));
    createDirMock.mockResolvedValue({
      ok: true,
      revision: 1,
      directory: { name: '新会', dir: 'C:\\会议\\新会', kind: 'empty', suspected: false },
    });
    const renderer = await mountPicker();

    const input = inputByLabel(renderer, '新目录的名字');
    await act(async () => {
      input.props.onChange({ target: { value: '新会' } });
    });
    await act(async () => {
      buttonByText(renderer, '新建目录').props.onClick();
    });

    expect(createDirMock).toHaveBeenCalledWith('C:\\会议', '新会');
    // 新行按名字次序插进清单，并直接进选定态（建完直接选中）
    const names = optionsOf(renderer).map((node) => textOf(node));
    expect(names.some((text) => text.includes('新会'))).toBe(true);
    const createdRow = optionsOf(renderer).find((node) => textOf(node).includes('新会'));
    expect(`${createdRow?.props.className}`).toContain('picker__row--on');
    // 后续确认走既有链路：empty 档确认钮可按，点它交出新建那一行
    const confirm = buttonByText(renderer, '选定这个目录');
    expect(confirm.props.disabled).toBe(false);
    await act(async () => {
      confirm.props.onClick();
    });
    expect(pickedOf(renderer)).toEqual({
      what: 'dir',
      dir: 'C:\\会议\\新会',
      kind: 'empty',
    });
    // 名字框清空，不残留旧输入
    expect(inputByLabel(renderer, '新目录的名字').props.value).toBe('');
  });

  it('建失败如实说：服务那句人话画在弹窗里，层不动、清单不动、零选中', async () => {
    browseDirsMock.mockResolvedValue(listing(baseDirs));
    // 真实失败路径：api 层把失败信封收成 DirCreateHttpError 抛出（message = 信封原话）
    createDirMock.mockRejectedValue(
      new DirCreateHttpError(
        409,
        'dir_exists',
        '这一层里已经有叫「旧文件夹」的目录或文件：C:\\会议\\旧文件夹；换个名字，或者双击进去直接用它',
        '',
      ),
    );
    const renderer = await mountPicker();

    const input = inputByLabel(renderer, '新目录的名字');
    await act(async () => {
      input.props.onChange({ target: { value: '旧文件夹' } });
    });
    await act(async () => {
      buttonByText(renderer, '新建目录').props.onClick();
    });

    expect(createDirMock).toHaveBeenCalledWith('C:\\会议', '旧文件夹');
    const text = textOf(renderer.root);
    expect(text).toContain('已经有叫「旧文件夹」');
    // 清单还是原来那两行（here + 旧文件夹），没有半截新行
    expect(optionsOf(renderer)).toHaveLength(2);
    // 零选中：确认钮置灰，输入留在原处好改
    expect(pickedOf(renderer)).toBeNull();
    expect(buttonByText(renderer, '选定这个目录').props.disabled).toBe(true);
    expect(inputByLabel(renderer, '新目录的名字').props.value).toBe('旧文件夹');
  });
});
