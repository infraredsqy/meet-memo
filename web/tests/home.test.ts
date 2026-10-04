import { describe, expect, it, vi } from 'vitest';
import {
  initialSelection,
  segmentsView,
  startButtonLabel,
  startWhy,
  statusLabel,
} from '../src/home/model';
import { confirmWorkspaceOpen } from '../src/home/confirmWorkspaceOpen';
import { drainSettled, showsWorkspaceHome } from '../src/home/gate';
import { offlineNoticeOf, postButtonsView, postStopView } from '../src/home/post';
import { failureFrom } from '../src/failure';
import { probeDecision } from '../src/api/sessions';
import type { PostJobCell, SessionRow, Snapshot } from '../src/contract';

/** 造一份 post_job 读数（顶层第三格的形状，用例只翻自己要翻的那几格）。 */
function job(over: Partial<PostJobCell> = {}): PostJobCell {
  return {
    kind: 'offline',
    dir: 'C:\\会议\\跟老王聊周会',
    started_at: '2026-09-21T22:10:00+08:00',
    ...over,
  };
}

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
    segments: [],
    has_offline: false,
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
  it('目录被移走/删掉的那一行如实写「目录不存在」', () => {
    expect(statusLabel(row({ dir_exists: false }))).toBe('目录不存在');
  });

  it('盘上 session.json 的四档 state 各有话', () => {
    expect(statusLabel(row({ state: 'recording' }))).toBe('录音中');
    expect(statusLabel(row({ state: 'draining' }))).toBe('收尾中');
    expect(statusLabel(row({ state: 'done' }))).toBe('已完成');
    expect(statusLabel(row({ state: 'failed' }))).toBe('收尾失败');
  });

  it('没有记录的三档：空白工作区 / 疑似会议目录（兜底不当杂物）/ 会议目录 / 普通目录', () => {
    expect(statusLabel(row({ kind: 'empty' }))).toBe('空白工作区');
    expect(statusLabel(row({ kind: 'meeting', suspected: true }))).toBe('疑似会议目录（缺少元数据）');
    expect(statusLabel(row({ kind: 'meeting' }))).toBe('会议目录');
    expect(statusLabel(row({ kind: 'unknown' }))).toBe('普通目录');
  });
});

describe('「开始 / 继续录音」在这一行上能不能按', () => {
  it('空白工作区可按；录过的目录照样可按——对着它按就是续接（第 9 条翻案）', () => {
    expect(startWhy(row({ kind: 'empty', state: null }))).toBeNull();
    expect(startWhy(row({ kind: 'meeting', state: 'done' }))).toBeNull();
    expect(startWhy(row({ kind: 'meeting', state: 'failed' }))).toBeNull();
    expect(startWhy(row({ dir_exists: false }))).toBe('目录不存在，无法录音');
  });

  it('动作钮的名字跟着行走：录过的叫「继续录音」，没录过的叫「开始录音」', () => {
    expect(startButtonLabel(row({ kind: 'empty', state: null }))).toBe('开始录音');
    expect(startButtonLabel(row({ kind: 'meeting', state: 'done' }))).toBe('继续录音');
    expect(startButtonLabel(row({ kind: 'meeting', state: 'recording' }))).toBe('继续录音');
  });
});

describe('详情区的段读数（续接过的会看得出接过）', () => {
  it('盘上段账原文 → 每段一行 + 合计（合计只数写完的段，第 7 条）', () => {
    const view = segmentsView(
      row({
        state: 'done',
        segments: [
          { index: 1, started_at: '2026-09-21T10:02:00+08:00', ended_at: '2026-09-21T10:19:00+08:00', duration_seconds: 61.5 },
          { index: 2, started_at: '2026-09-21T11:30:00+08:00', ended_at: '2026-09-21T11:32:00+08:00', duration_seconds: 122.3 },
        ],
      }),
    );
    expect(view).not.toBeNull();
    expect(view?.total).toBe('共 2 段 · 3 分 4 秒');
    expect(view?.lines.map((line) => line.label)).toEqual(['段 1', '段 2']);
    expect(view?.lines[0]).toMatchObject({ time: '10:02–10:19', seconds: '1 分 2 秒' });
    expect(view?.lines[1]).toMatchObject({ time: '11:30–11:32', seconds: '2 分 2 秒' });
  });

  it('还在录的那段：时刻写到「起」，时长如实写「没写完」，合计不计它', () => {
    const view = segmentsView(
      row({
        state: 'recording',
        segments: [
          { index: 1, started_at: '2026-09-21T10:02:00+08:00', ended_at: '2026-09-21T10:19:00+08:00', duration_seconds: 61.5 },
          { index: 2, started_at: '2026-09-21T11:30:00+08:00', ended_at: null, duration_seconds: null },
        ],
      }),
    );
    expect(view?.total).toBe('共 2 段 · 1 分 2 秒');
    expect(view?.lines[1]).toMatchObject({ time: '11:30 起', seconds: '没写完' });
  });

  it('没有段账（还没开过会）时交回 null，详情区不摆这一格', () => {
    expect(segmentsView(row({ kind: 'empty', state: null }))).toBeNull();
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

describe('会后那两个按钮（R26 / R29）', () => {
  it('收尾落定的那一场两个都能按', () => {
    const view = postButtonsView(row({ kind: 'meeting', state: 'done', has_offline: true }));
    expect(view.canPostOffline).toBe(true);
    expect(view.canPostMinutes).toBe(true);
    expect(view.why).toBeNull();
  });

  it('还在录 / 还在收尾：两个都按不动，并给一句为什么', () => {
    for (const state of ['recording', 'draining'] as const) {
      const view = postButtonsView(row({ kind: 'meeting', state }));
      expect(view.canPostOffline).toBe(false);
      expect(view.canPostMinutes).toBe(false);
      expect(view.why).toContain('收尾');
    }
  });

  it('还在录 / 还在收尾那一格：两件事并列说，不拿「盘上有稿」盖住「还没落定」（review-2 C1）', () => {
    for (const state of ['recording', 'draining'] as const) {
      for (const has_offline of [true, false]) {
        const view = postButtonsView(row({ kind: 'meeting', state, has_offline }));
        expect(view.offlineNotice).not.toBeNull();
        expect(view.offlineNotice).toContain('该工作区正在录音或收尾中');
        expect(view.offlineNotice).toContain(`磁盘上的会后精细稿：${has_offline ? '有' : '无'}`);
        // 这一档的那一句**就是**这一格的全部：不许换成落定之后那一句
        expect(view.offlineNotice).not.toBe(offlineNoticeOf(row({ has_offline })));
      }
    }
  });

  it('目录不存在：两个都按不动，原因句只有一处（不冒充会后稿读数）', () => {
    const view = postButtonsView(row({ dir_exists: false }));
    expect(view.canPostOffline).toBe(false);
    expect(view.why).toBe('目录不存在，无法运行会后任务');
    // 上面那句已经把「跑不了」说完了：会后那一格不再复述，也不把「目录不存在」搬过来
    expect(view.offlineNotice).toBeNull();
  });

  it('收尾失败（failed）那一场照样能按：会后跑不跑与收尾那一步的成败无关', () => {
    expect(postButtonsView(row({ state: 'failed' })).canPostMinutes).toBe(true);
  });

  it('「生成纪要」按钮上那一格按 has_offline 如实写（R29）', () => {
    expect(offlineNoticeOf(row({ has_offline: true }))).toBe('会后精细稿：有');
    expect(offlineNoticeOf(row({ has_offline: false }))).toBe('会后精细稿：无');
  });
});

describe('停止入口（R28 / b1c6a58e）', () => {
  const dir = 'C:\\会议\\跟老王聊周会';

  it('这一场的 offline 在飞：入口出现', () => {
    expect(postStopView(job({ kind: 'offline', dir }), dir)).toEqual({ visible: true });
  });

  it('在飞的是纪要那一趟：入口不出现（停的动作只接精细稿，服务对纪要如实拒）', () => {
    expect(postStopView(job({ kind: 'minutes', dir }), dir)).toEqual({ visible: false });
  });

  it('在飞的是另一场：入口不出现（它长在这一场的进度区头行上）', () => {
    expect(postStopView(job({ kind: 'offline', dir: 'C:\\会议\\另一场' }), dir)).toEqual({
      visible: false,
    });
  });

  it('没有在飞的会后 / 没选中：入口不出现', () => {
    expect(postStopView(null, dir)).toEqual({ visible: false });
    expect(postStopView(job(), null)).toEqual({ visible: false });
  });
});

describe('闸的分流（R26：结束录音之后回到工作台）', () => {
  /** 造一份只填这一档判据的快照（其余格子给无害值）。 */
  function snapshot(over: {
    top_state?: Snapshot['top_state'];
    hasSession?: boolean;
    phase?: 'draining' | 'done';
    statuses?: string[];
  }): Snapshot {
    const statuses = over.statuses ?? ['skipped', 'skipped', 'done', 'waiting'];
    return {
      revision: 1,
      service: {
        version: '0.0.0',
        started_at: '2026-09-21T21:00:00+08:00',
        server_time: '2026-09-21T21:00:00+08:00',
      },
      top_state: over.top_state ?? 'ended',
      session:
        over.hasSession === false
          ? null
          : {
              id: '260921',
              dir: 'C:\\会议\\260921',
              mode: 'both',
              frozen: true,
              elapsed_seconds: 100,
              started_at: '2026-09-21T21:00:00+08:00',
            },
      sources: [],
      alerts: { critical: null, hints: [] },
      transcript: { health: 'stopped', backlog: 0, last_line_at: null },
      ended: {
        phase: over.phase ?? 'done',
        error: null,
        steps: ['flush', 'transcode', 'log', 'post'].map((id, index) => ({
          id,
          status: statuses[index],
          detail: '',
          error: null,
        })),
      },
      settings: {
        alarm_sound: false,
        heartbeat_interval_seconds: 10,
        asr: {
          segment_seconds: 30,
          timeout_seconds: 30,
          max_tokens: 256,
          max_cycles: 2,
          coverage_ratio: 0.5,
        },
        devices: { mic: null, loopback: null },
      },
    } as unknown as Snapshot;
  }

  it('空闲态（没有场次）→ 工作台', () => {
    expect(showsWorkspaceHome(snapshot({ top_state: 'idle', hasSession: false }))).toBe(true);
  });

  it('收尾四步落定（post 停在 waiting）→ 回工作台', () => {
    expect(showsWorkspaceHome(snapshot({ phase: 'done' }))).toBe(true);
    expect(drainSettled(snapshot({ phase: 'done' }))).toBe(true);
  });

  it('收尾有失败步、phase 停在 draining、post 停在 waiting → 也回工作台', () => {
    const snap = snapshot({ phase: 'draining', statuses: ['skipped', 'skipped', 'failed', 'waiting'] });
    expect(showsWorkspaceHome(snap)).toBe(true);
  });

  it('收尾还在跑（post 停在 running）→ 留在会中那一屏', () => {
    const snap = snapshot({ phase: 'draining', statuses: ['done', 'done', 'done', 'running'] });
    expect(showsWorkspaceHome(snap)).toBe(false);
  });

  it('前三步还没轮到（waiting）→ 留在会中那一屏', () => {
    const snap = snapshot({
      phase: 'draining',
      statuses: ['waiting', 'waiting', 'waiting', 'waiting'],
    });
    expect(showsWorkspaceHome(snap)).toBe(false);
  });

  it('录音中 / 告警 → 留在会中那一屏', () => {
    for (const top of ['recording', 'alert'] as const) {
      expect(showsWorkspaceHome(snapshot({ top_state: top }))).toBe(false);
    }
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
