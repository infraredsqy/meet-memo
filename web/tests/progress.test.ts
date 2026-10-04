import { describe, expect, it } from 'vitest';
import {
  advanceChunkRows,
  chunkLedgerReset,
  chunkRowLabel,
  gpuBusyView,
  initialChunkRows,
  phaseMark,
  progressView,
  roadPercent,
  rowStatusView,
  shouldWatch,
} from '../src/home/progress';
import type { OfflineSourceProgress, PostJobCell, SessionRow, SessionStatus } from '../src/contract';

/**
 * 会后进度界面的纯逻辑用例（`home/progress.ts`）。
 *
 * **真值构造、不读 golden 样本**：样本是掩码过的（`<path>` / `<iso>`），算不出
 * 「第 9/10 块」「已跑 37 秒」这些字；这里的读数断言全部从零造真值（与
 * `live-fixtures.ts` 同一姿态）。形状的权威在 `docs/contract.md` 4.9，逐键对着写。
 *
 * 四组：
 * 1. 列表行四态（b3：显示层重映射 + `post_job.dir` 联结）；
 * 2. 进度读数（真在跑 / 停着一版 running / 终态 / 没跑过；分母按路各算各的）；
 * 3. 逐块行（每跑完一块落一行；四档归属靠计数差值）与账重置检出（review-2 m2：
 *    重跑一场逐块行整段重播）；
 * 4. 全局「显卡忙不忙」与「要不要轮询」。
 */

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

function job(over: Partial<PostJobCell> = {}): PostJobCell {
  return {
    kind: 'offline',
    dir: 'C:\\会议\\跟老王聊周会',
    started_at: '2026-09-21T22:10:00+08:00',
    ...over,
  };
}

function road(over: Partial<OfflineSourceProgress> = {}): OfflineSourceProgress {
  return {
    source: 'mic',
    done: 0,
    total: null,
    remaining: null,
    current_chunk: null,
    counts: { transcribed: 0, silent: 0, failed: 0, unjudged: 0 },
    ...over,
  };
}

function status(
  over: Partial<SessionStatus> = {},
  offline: Partial<SessionStatus['offline']> = {},
): SessionStatus {
  return {
    dir: 'C:\\会议\\跟老王聊周会',
    session_id: '跟老王聊周会',
    kind: 'meeting',
    state: 'done',
    date: '2026-09-21',
    mode: 'both',
    segments: [],
    has_offline: false,
    post: null,
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
    ...over,
  };
}

// ---------------------------------------------------------------------------
// 1. 列表行四态
// ---------------------------------------------------------------------------

describe('列表行四态（R27，字与图形并存）', () => {
  it('空闲：没开过会的目录各有原话，形状是空心框', () => {
    expect(rowStatusView(row({ kind: 'empty' }), null)).toEqual({
      tone: 'idle', label: '空白工作区', mark: 'glyph--idle', dim: false,
    });
    expect(rowStatusView(row({ kind: 'meeting' }), null)?.label).toBe('会议目录');
    expect(rowStatusView(row({ kind: 'unknown' }), null)?.label).toBe('普通目录');
    expect(rowStatusView(row({ kind: 'meeting', suspected: true }), null)?.label).toBe(
      '疑似会议目录 · 缺元信息',
    );
  });

  it('正在录：state=recording，实心红灯 + 字，两条通道都在', () => {
    const view = rowStatusView(row({ state: 'recording' }), null);
    expect(view.tone).toBe('recording');
    expect(view.label).toBe('正在录');
    expect(view.mark).toBe('glyph--rec');
  });

  it('等着人动手：done / failed 都是 waiting 语义，失败把「收尾失败」说在字里', () => {
    const done = rowStatusView(row({ state: 'done', kind: 'meeting' }), null);
    expect(done.tone).toBe('waiting');
    expect(done.label).toBe('等你动手');
    expect(done.mark).toBe('glyph--dashed');

    const failed = rowStatusView(row({ state: 'failed', kind: 'meeting' }), null);
    expect(failed.tone).toBe('waiting');
    expect(failed.label).toBe('收尾失败 · 等你动手');
    expect(failed.mark).toBe('glyph--cross');
  });

  it('会后跑着：post_job.dir 与这一行联结命中（done 的那一行也被它盖过）', () => {
    const view = rowStatusView(row({ state: 'done', kind: 'meeting' }), job());
    expect(view.tone).toBe('post-running');
    expect(view.label).toBe('会后跑着');
    expect(view.mark).toBeNull(); // 画 spinner（转的），不是静态形状
  });

  it('联结只认同一场：别人的 post_job 点不亮这一行', () => {
    const view = rowStatusView(
      row({ state: 'done', kind: 'meeting' }),
      job({ dir: 'C:\\会议\\另一场' }),
    );
    expect(view.tone).toBe('waiting');
    expect(view.label).toBe('等你动手');
  });

  it('路径已不在：灰档 + 虚线圈，联结与四态都让位', () => {
    const gone = rowStatusView(row({ dir_exists: false }), job());
    expect(gone.tone).toBe('gone');
    expect(gone.label).toBe('路径已不在');
    expect(gone.mark).toBe('glyph--dashed');
    expect(gone.dim).toBe(true);
  });

  it('收尾中如实保留（正在录与落定之间的一瞬，不折进任何一态）', () => {
    const view = rowStatusView(row({ state: 'draining', kind: 'meeting' }), null);
    expect(view.tone).toBe('draining');
    expect(view.label).toBe('收尾中');
  });
});

// ---------------------------------------------------------------------------
// 2. 进度读数
// ---------------------------------------------------------------------------

describe('进度读数（offline 那份 → 每路一条）', () => {
  it('第 1/10 块：真在跑（in_process），阶段行写「正在跑：本地 第 1/10 块」', () => {
    const view = progressView(status({}, {
      state: 'running',
      in_process: true,
      started_at: '2026-09-21T22:10:00+08:00',
      elapsed_seconds: 12.4,
      current_source: 'mic',
      sources: [
        road({ done: 1, total: 10, remaining: 9, current_chunk: 2,
          counts: { transcribed: 1, silent: 0, failed: 0, unjudged: 0 } }),
        road({ source: 'loopback', done: 0, total: 10, remaining: 10 }),
      ],
    }));
    expect(view.phase).toBe('in-flight');
    expect(view.phaseLabel).toBe('正在跑：本地 第 2/10 块');
    expect(view.inProcess).toBe(true);
    expect(view.roads[0]?.percent).toBe(10);
    expect(view.roads[1]?.percent).toBe(0);
    expect(view.elapsedSeconds).toBe(12.4);
  });

  it('第 9/10 块：同一条路换读数，阶段行跟着换、不塌', () => {
    const view = progressView(status({}, {
      state: 'running',
      in_process: true,
      current_source: 'mic',
      sources: [
        road({ done: 9, total: 10, remaining: 1, current_chunk: 10,
          counts: { transcribed: 8, silent: 0, failed: 1, unjudged: 0 } }),
        road({ source: 'loopback', done: 10, total: 10, remaining: 0 }),
      ],
    }));
    expect(view.phaseLabel).toBe('正在跑：本地 第 10/10 块');
    expect(view.roads[0]?.percent).toBe(90);
    expect(view.roads[1]?.percent).toBe(100);
  });

  it('停着一版 running：in_process=false 而 state=running，如实分两层、不立终态', () => {
    const view = progressView(status({}, {
      state: 'running',
      in_process: false,
      elapsed_seconds: null,
      current_source: null,
      sources: [
        road({ done: 1, total: 10, remaining: 9,
          counts: { transcribed: 1, silent: 0, failed: 0, unjudged: 0 } }),
        road({ source: 'loopback' }),
      ],
    }));
    expect(view.phase).toBe('stale-running');
    expect(view.phaseLabel).toBe('盘上停着一版「在跑」，此刻没有任何东西在跑');
    expect(view.onDiskState).toBe('running');
    expect(view.inProcess).toBe(false);
  });

  it('被打断（b1c6a58e）：自成一相，明说「停了、可以重跑」，不许掉进「跑完了」', () => {
    const view = progressView(status({}, {
      state: 'interrupted',
      in_process: false,
      finished_at: '2026-09-21T22:20:00+08:00',
      sources: [
        road({ done: 1, total: 2, remaining: 1,
          counts: { transcribed: 1, silent: 0, failed: 0, unjudged: 0 } }),
        road({ source: 'loopback' }),
      ],
    }));
    expect(view.phase).toBe('interrupted');
    expect(view.phaseLabel).toBe('被打断');
    expect(view.onDiskState).toBe('interrupted');
    expect(view.inProcess).toBe(false);
  });

  it('终态照盘上那份答（partial 不折成 done）；没跑过是另一档', () => {
    const partial = progressView(status({}, { state: 'partial', in_process: false }));
    expect(partial.phase).toBe('finished');
    // 一路整条没交回：不许写成「跑完了」
    expect(partial.phaseLabel).toBe('只跑成一路');
    const failed = progressView(status({}, { state: 'failed', in_process: false }));
    expect(failed.phaseLabel).toBe('跑失败了');
    const done = progressView(status({}, { state: 'done', in_process: false }));
    expect(done.phaseLabel).toBe('跑完了');
    const never = progressView(status({}, { state: 'idle' }));
    expect(never.phase).toBe('never-run');
    expect(never.phaseLabel).toBe('这一场还没跑过会后');
  });

  it('分母按路各算各的：一路切了 10 块、一路计划未知（total=null）就不猜百分比', () => {
    const view = progressView(status({}, {
      state: 'running',
      in_process: true,
      sources: [
        road({ done: 3, total: 10, remaining: 7 }),
        road({ source: 'loopback', done: 2, total: null, remaining: null }),
      ],
    }));
    expect(view.roads[0]?.percent).toBe(30);
    expect(view.roads[1]?.percent).toBeNull();
    expect(roadPercent(0, 0)).toBeNull();
    expect(roadPercent(5, 4)).toBe(100); // 超出的钳在 100
  });
});

// ---------------------------------------------------------------------------
// 3. 逐块行
// ---------------------------------------------------------------------------

describe('逐块行（每跑完一块落一行）', () => {
  it('第一次读：把已跑过的补成 observed 行（此前的不编档位）', () => {
    const rows = initialChunkRows(status({}, {
      sources: [
        road({ done: 2, total: 3,
          counts: { transcribed: 2, silent: 0, failed: 0, unjudged: 0 } }),
        road({ source: 'loopback', done: 1, total: 3,
          counts: { transcribed: 0, silent: 0, failed: 1, unjudged: 0 } }),
      ],
    }));
    expect(rows.map((item) => item.key)).toEqual(['mic-1', 'mic-2', 'loopback-1']);
    expect(rows.every((item) => item.verdict === 'observed')).toBe(true);
    expect(chunkRowLabel(rows[0] as (typeof rows)[number])).toBe('本地 第 1/3 块 · 已跑过');
  });

  it('两次读数之间新跑完的块：四档归属靠计数差值，逐块落行', () => {
    const before = status({}, {
      state: 'running',
      in_process: true,
      sources: [
        road({ done: 1, total: 3, counts: { transcribed: 1, silent: 0, failed: 0, unjudged: 0 } }),
        road({ source: 'loopback', done: 0, total: 3 }),
      ],
    });
    const next = status({}, {
      state: 'running',
      in_process: true,
      sources: [
        road({ done: 3, total: 3,
          counts: { transcribed: 2, silent: 0, failed: 1, unjudged: 0 } }),
        road({ source: 'loopback', done: 0, total: 3 }),
      ],
    });
    const appended = advanceChunkRows([], before, next);
    expect(appended.map((item) => `${item.key}:${item.verdict}`)).toEqual([
      'mic-2:transcribed',
      'mic-3:failed',
    ]);
    expect(chunkRowLabel(appended[1] as (typeof appended)[number])).toBe('本地 第 3/3 块 · 失败');
  });

  it('差值对不上 done 的差值时多出来的块如实记 observed；没动就一行不加', () => {
    const before = status({}, { sources: [road({ done: 1, total: 3 })] });
    const mysterious = status({}, { sources: [road({ done: 2, total: 3 })] });
    expect(advanceChunkRows([], before, mysterious).map((item) => item.verdict)).toEqual(['observed']);
    const same = status({}, { sources: [road({ done: 1, total: 3 })] });
    expect(advanceChunkRows([], before, same)).toEqual([]);
    // 前一份是别的场次（换选中）：不跨场拼行
    const other = status({ dir: 'C:\\会议\\另一场' }, { sources: [road({ done: 5, total: 6 })] });
    expect(advanceChunkRows([], before, other)).toEqual([]);
  });
});

describe('账重置检出（review-2 m2：重跑一场逐块行要整段重播，不接旧账）', () => {
  const OLD_RUN = '2026-09-20T10:00:00+08:00';
  const NEW_RUN = '2026-09-21T22:10:00+08:00';

  /** 上一趟的终读：十块全在手上（这是按下重跑按钮那一刻的 `before`）。 */
  function oldRun(over: Partial<SessionStatus['offline']> = {}): SessionStatus {
    return status({}, {
      state: 'done',
      in_process: false,
      started_at: OLD_RUN,
      finished_at: '2026-09-20T11:00:00+08:00',
      sources: [
        road({ done: 10, total: 10, remaining: 0,
          counts: { transcribed: 9, silent: 0, failed: 1, unjudged: 0 } }),
        road({ source: 'loopback', done: 10, total: 10, remaining: 0,
          counts: { transcribed: 8, silent: 2, failed: 0, unjudged: 0 } }),
      ],
      ...over,
    });
  }

  it('before 是旧趟终读、next.started_at 换新 → 检出重置；差值那一档在这里恰好一行不追加', () => {
    // 新一趟的第一跳：新账 done 从 0 重数（后端每趟新造账、start() 只落表头）
    const fresh = status({}, {
      state: 'running',
      in_process: true,
      started_at: NEW_RUN,
      current_source: 'mic',
      sources: [road({ done: 0, total: 10, remaining: 10 }), road({ source: 'loopback' })],
    });
    expect(chunkLedgerReset(oldRun(), fresh)).toBe(true);
    // 差值 10→0 被钳成 0 —— 不检出重置的话旧趟十行会原样挂着，一行都换不掉
    expect(advanceChunkRows([], oldRun(), fresh)).toEqual([]);
  });

  it('started_at 没换但某路 done 回落 / 四档合计回落 → 也算账重置；正常推进不算', () => {
    const before = status({}, {
      started_at: NEW_RUN,
      state: 'running',
      in_process: true,
      sources: [road({ done: 4, total: 10, counts: { transcribed: 3, silent: 1, failed: 0, unjudged: 0 } })],
    });
    // done 回落
    expect(chunkLedgerReset(
      before,
      status({}, { started_at: NEW_RUN, sources: [road({ done: 2, total: 10 })] }),
    )).toBe(true);
    // 四档合计回落（done 相同、账被换过）
    expect(chunkLedgerReset(
      before,
      status({}, {
        started_at: NEW_RUN,
        sources: [road({ done: 4, total: 10, counts: { transcribed: 2, silent: 0, failed: 0, unjudged: 0 } })],
      }),
    )).toBe(true);
    // 同一趟里正常推进：started_at 不变、done 只增 —— 不重播，走差值
    expect(chunkLedgerReset(
      before,
      status({}, {
        started_at: NEW_RUN,
        sources: [road({ done: 5, total: 10, counts: { transcribed: 4, silent: 1, failed: 0, unjudged: 0 } })],
      }),
    )).toBe(false);
  });

  it('换场不检出（跨场重置由换选中清零负责）；没开跑过（两边 started_at 都 null）也不检出', () => {
    const before = status({}, { started_at: NEW_RUN, sources: [road({ done: 3, total: 5 })] });
    expect(chunkLedgerReset(
      before,
      status({ dir: 'C:\\会议\\另一场' }, { started_at: OLD_RUN, sources: [road({ done: 9, total: 10 })] }),
    )).toBe(false);
    expect(chunkLedgerReset(status({}), status({}))).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 4. 全局忙/闲与要不要轮询
// ---------------------------------------------------------------------------

describe('全局「忙不忙」（R31）', () => {
  it('闲：没有模型开着 —— 空心框 + 字', () => {
    const view = gpuBusyView(null, false);
    expect(view).toEqual({ busy: false, label: '空闲', note: '没有模型开着', mark: 'glyph--idle' });
  });

  it('忙：会后那趟在跑（说清跑的是哪一趟）；正在录也是忙', () => {
    expect(gpuBusyView(job(), false)).toEqual({
      busy: true,
      label: '忙碌',
      note: '会后那趟在跑（会后精细稿）',
      mark: 'glyph--rec',
    });
    expect(gpuBusyView(job({ kind: 'minutes' }), false)?.note).toBe('会后那趟在跑（纪要）');
    expect(gpuBusyView(null, true)?.note).toBe('会中模型开着');
    const both = gpuBusyView(job(), true);
    expect(both.busy).toBe(true);
    expect(both.note).toBe('会中模型开着；会后那趟在跑（会后精细稿）');
  });
});

describe('要不要轮询（有活动才问）', () => {
  it('联结命中就要问；读数说在跑 / 停着 running 也要问', () => {
    const selected = row({ state: 'done', kind: 'meeting' });
    expect(shouldWatch(selected, job(), null)).toBe(true);
    expect(shouldWatch(selected, job(), status({ dir: 'C:\\会议\\另一场' }))).toBe(true);
    expect(
      shouldWatch(selected, job(), status({}, { state: 'running', in_process: true })),
    ).toBe(true);
    expect(shouldWatch(selected, null, status({}, { state: 'running', in_process: true }))).toBe(true);
    expect(shouldWatch(selected, null, status({}, { state: 'running', in_process: false }))).toBe(true);
  });

  it('联结命中但读数已落定（终态那一跳）：不问了 —— watch 要翻 false 才收得了尾（review-1 C1）', () => {
    const selected = row({ state: 'done', kind: 'meeting' });
    expect(shouldWatch(selected, job(), status({}, { state: 'done', in_process: false }))).toBe(false);
    expect(shouldWatch(selected, job(), status({}, { state: 'idle' }))).toBe(false);
    // 被打断也是终态（b1c6a58e）：重按按钮才再跑，轮询照样收尾
    expect(shouldWatch(selected, job(), status({}, { state: 'interrupted', in_process: false }))).toBe(false);
  });

  it('终态、没跑过、没选中：不问', () => {
    const selected = row({ state: 'done', kind: 'meeting' });
    expect(shouldWatch(selected, null, status({}, { state: 'done', in_process: false }))).toBe(false);
    expect(shouldWatch(selected, null, status({}, { state: 'idle' }))).toBe(false);
    expect(shouldWatch(selected, null, status({}, { state: 'interrupted', in_process: false }))).toBe(false);
    expect(shouldWatch(selected, null, null)).toBe(false);
    expect(shouldWatch(null, job(), null)).toBe(false);
  });
});

describe('阶段图形标记（形状通道）', () => {
  it('真在跑是转的（spinner → null）、停着一版与被打断是红叉、终态打勾、没跑过空心框', () => {
    expect(phaseMark('in-flight')).toBeNull();
    expect(phaseMark('stale-running')).toBe('glyph--cross');
    expect(phaseMark('interrupted')).toBe('glyph--cross');
    expect(phaseMark('finished')).toBe('glyph--done');
    expect(phaseMark('never-run')).toBe('glyph--idle');
  });
});
