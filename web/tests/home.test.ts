import { describe, expect, it, vi } from 'vitest';
import { initialSelection, startWhy, statusLabel } from '../src/home/model';
import { confirmWorkspaceOpen } from '../src/home/confirmWorkspaceOpen';
import { failureFrom } from '../src/failure';
import { probeDecision } from '../src/api/sessions';
import type { SessionRow } from '../src/contract';

/** 造一行注册表（空缺全给 null / 无害值，用例只翻自己要翻的那几格）。 */
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

describe('打开这一页时选哪一行', () => {
  it('服务给的 recent 恒有对应的一行：直接选它（最近一场只从注册表读）', () => {
    const rows = [row({ dir: 'C:\\a', session_id: 'a' }), row({ dir: 'C:\\b', session_id: 'b' })];
    expect(initialSelection(rows, 'C:\\b')).toBe('C:\\b');
  });

  it('recent 没给出或对不上行时兜底第一行；空注册表给 null（交给空态）', () => {
    const rows = [row({ dir: 'C:\\a', session_id: 'a' }), row({ dir: 'C:\\b', session_id: 'b' })];
    expect(initialSelection(rows, null)).toBe('C:\\a');
    expect(initialSelection([], null)).toBeNull();
    expect(initialSelection(rows, 'C:\\ghost')).toBe('C:\\a');
  });
});

describe('行状态一眼可读', () => {
  it('目录被移走/删掉的那一行如实写「路径已不在」', () => {
    expect(statusLabel(row({ dir_exists: false }))).toBe('路径已不在');
  });

  it('盘上 session.json 的四档 state 各有话', () => {
    expect(statusLabel(row({ state: 'recording' }))).toBe('正在录');
    expect(statusLabel(row({ state: 'draining' }))).toBe('收尾中');
    expect(statusLabel(row({ state: 'done' }))).toBe('已完成');
    expect(statusLabel(row({ state: 'failed' }))).toBe('收尾失败');
  });

  it('没有记录的三档：空白工作区 / 疑似会议目录（兜底不当杂物）/ 会议目录 / 普通目录', () => {
    expect(statusLabel(row({ kind: 'empty' }))).toBe('空白工作区');
    expect(statusLabel(row({ kind: 'meeting', suspected: true }))).toBe('疑似会议目录 · 缺元信息');
    expect(statusLabel(row({ kind: 'meeting' }))).toBe('会议目录');
    expect(statusLabel(row({ kind: 'unknown' }))).toBe('普通目录');
  });
});

describe('「开始录音」在这一行上能不能按', () => {
  it('空白工作区可按；有会议记录的按不动（续接归下一个节点）', () => {
    expect(startWhy(row({ kind: 'empty', state: null }))).toBeNull();
    expect(startWhy(row({ kind: 'meeting', state: 'done' }))).toBe('已有会议记录；续接归下一个节点');
    expect(startWhy(row({ dir_exists: false }))).toBe('目录已不在原地，只显示，录不了');
  });
});

describe('目录名目 → 三档处理', () => {
  it('空目录当新会；会议材料（含疑似档）收编；非空无材料要确认', () => {
    expect(probeDecision('empty')).toBe('empty');
    expect(probeDecision('meeting')).toBe('meeting');
    expect(probeDecision('unknown')).toBe('need-confirm');
    expect(probeDecision('missing')).toBe('need-confirm');
  });
});

describe('确认弹窗 → 收编的接线（review-1 C1）', () => {
  const dir = 'C:\\杂物\\not-a-meeting';

  it('确认后直接发 workspace_open 且带 confirm:true，成功后刷一遍列表', async () => {
    let reloaded = 0;
    let onFail: unknown = undefined;
    const post = vi.fn(async () => ({ kind: 'ok', ok: true, revision: 3 }));
    await confirmWorkspaceOpen(dir, {
      post: post as unknown as typeof import('../src/api').postAction,
      onFail: (failure) => {
        onFail = failure;
      },
      reload: () => {
        reloaded += 1;
      },
    });

    expect(post).toHaveBeenCalledTimes(1);
    const [action, args] = post.mock.calls[0] ?? [undefined, undefined];
    expect(action).toBe('workspace_open');
    expect(args).toEqual({ dir, confirm: true });
    expect(onFail).toBeUndefined();
    expect(reloaded).toBe(1);
  });

  it('服务回失败信封：不冒成功，失败画失败块、列表照样刷（口径照 reloadAfterAction）', async () => {
    let reloaded = 0;
    let failure: unknown = null;
    const post = vi.fn(async () => {
      throw new Error('收不动：目录突然没了');
    });
    await confirmWorkspaceOpen(dir, {
      post: post as unknown as typeof import('../src/api').postAction,
      onFail: (view) => {
        failure = view;
      },
      reload: () => {
        reloaded += 1;
      },
    });

    expect(failure).toEqual(failureFrom(new Error('收不动：目录突然没了')));
    expect(reloaded).toBe(1);
  });
});
