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
 * 1. 列表行四档（d61829f1：空白工作区 / 尚未开始 / 录音中 / 录音结束 + 目录不存在；
 *    会后工序的进度不进行状态）；
 * 2. 进度读数（真在跑 / 停着一版 running / 终态 / 没跑过；分母按路各算各的）；
 * 3. 逐块行（每跑完一块落一行；四档归属靠计数差值）与记录重置检出（review-2 m2：
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

function job(over: Partial<PostJobCell> = {}): PostJobCell[] {
  return [
    {
      kind: 'offline',
      dir: 'C:\\会议\\跟老王聊周会',
      started_at: '2026-09-21T22:10:00+08:00',
      ...over,
    },
  ];
}

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
    state: 'ended',
    date: '2026-09-21',
    mode: 'both',
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
      sources: [road(), road({ source: 'loopback' })],
      ...offline,
    },
    ...over,
  };
}

// ---------------------------------------------------------------------------
// 1. 列表行四档（d61829f1：只说录音，会后工序的进度不进这一格）
// ---------------------------------------------------------------------------

describe('列表行四档（d61829f1，字与图形并存）', () => {
  it('空白工作区 / 尚未开始：按名目 kind 分档，形状是空心框（颜色不是唯一通道）', () => {
    expect(rowStatusView(row({ kind: 'empty' }), [])).toEqual({
      tone: 'blank', label: '空白工作区', mark: 'glyph--idle', dim: false,
    });
    // 有材料没元数据的疑似目录、收编的普通目录：没开过会就一律读「尚未开始」
    expect(rowStatusView(row({ kind: 'meeting', suspected: true }), [])).toEqual({
      tone: 'unstarted', label: '尚未开始', mark: 'glyph--idle', dim: false,
    });
    expect(rowStatusView(row({ kind: 'unknown' }), [])?.label).toBe('尚未开始');
  });

  it('录音中：state=recording，实心红灯 + 字，两条通道都在', () => {
    const view = rowStatusView(row({ state: 'recording' }), []);
    expect(view.tone).toBe('recording');
    expect(view.label).toBe('录音中');
    expect(view.mark).toBe('glyph--rec');
  });

  it('录音结束（ended）：三档口径只答「这场录音走到哪了」，收尾进度不进行状态', () => {
    const ended = rowStatusView(row({ state: 'ended', kind: 'meeting' }), []);
    expect(ended.tone).toBe('ended');
    expect(ended.label).toBe('录音结束');
    expect(ended.mark).toBe('glyph--done');
  });

  it('会后任务在飞不改这一行：post_jobs 只喂忙闲格与进度区，行四档看不见它', () => {
    // 旧口径这里翻「会后任务运行中」；d61829f1 起那一档从行上摘除
    const view = rowStatusView(row({ state: 'ended', kind: 'meeting' }), job());
    expect(view.tone).toBe('ended');
    expect(view.label).toBe('录音结束');
    expect(view.mark).not.toBeNull();
  });

  it('目录不存在：灰档 + 虚线圈，四档都让位（如实保留）', () => {
    const gone = rowStatusView(row({ dir_exists: false }), job());
    expect(gone.tone).toBe('gone');
    expect(gone.label).toBe('目录不存在');
    expect(gone.mark).toBe('glyph--dashed');
    expect(gone.dim).toBe(true);
  });

  it('被强杀在收尾半路的那一场：三档口径里它就是 ended，行读「录音结束」', () => {
    const view = rowStatusView(row({ state: 'ended', kind: 'meeting' }), []);
    expect(view.tone).toBe('ended');
    expect(view.label).toBe('录音结束');
  });
});

// ---------------------------------------------------------------------------
// 2. 进度读数
// ---------------------------------------------------------------------------

describe('进度读数（offline 那份 → 每路一条）', () => {
  it('第 1/10 块：真在跑（in_process），阶段行写「正在运行：本地 第 1/10 块」', () => {
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
    expect(view.phaseLabel).toBe('正在运行：本地 第 2/10 块');
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
    expect(view.phaseLabel).toBe('正在运行：本地 第 10/10 块');
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
    expect(view.phaseLabel).toBe('磁盘记录为运行中，当前没有进程在运行');
    expect(view.onDiskState).toBe('running');
    expect(view.inProcess).toBe(false);
  });

  it('已中断（b1c6a58e）：自成一相，明说「停了、可以重跑」，不许掉进「跑完了」', () => {
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
    expect(view.phaseLabel).toBe('已中断');
    expect(view.onDiskState).toBe('interrupted');
    expect(view.inProcess).toBe(false);
  });

  it('终态照盘上那份答（partial 不折成 done）；没跑过是另一档', () => {
    const partial = progressView(status({}, { state: 'partial', in_process: false }));
    expect(partial.phase).toBe('finished');
    // 一路整条没交回：不许写成「已完成」
    expect(partial.phaseLabel).toBe('部分完成');
    const failed = progressView(status({}, { state: 'failed', in_process: false }));
    expect(failed.phaseLabel).toBe('失败');
    const done = progressView(status({}, { state: 'done', in_process: false }));
    expect(done.phaseLabel).toBe('已完成');
    const never = progressView(status({}, { state: 'idle' }));
    expect(never.phase).toBe('never-run');
    expect(never.phaseLabel).toBe('尚未运行');
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
  it('第一次读：把已处理的补成 observed 行（此前的不编档位）', () => {
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
    expect(chunkRowLabel(rows[0] as (typeof rows)[number])).toBe('本地 第 1/3 块 · 已处理');
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

describe('记录重置检出（review-2 m2：重跑一场逐块行要整段重播，不接旧记录）', () => {
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
    // 新一趟的第一跳：新记录 done 从 0 重数（后端每趟新建记录、start() 只落表头）
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

  it('started_at 没换但某路 done 回落 / 四档合计回落 → 也算记录重置；正常推进不算', () => {
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
    // 四档合计回落（done 相同、记录被换过）
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
    const view = gpuBusyView([], false);
    expect(view).toEqual({ busy: false, label: '空闲', note: '没有模型开着', mark: 'glyph--idle' });
  });

  it('忙：会后那趟在跑（说清跑的是哪一趟）；录音中也是忙', () => {
    expect(gpuBusyView(job(), false)).toEqual({
      busy: true,
      label: '忙碌',
      note: '会后那趟在跑（会后精细稿）',
      mark: 'glyph--rec',
    });
    expect(gpuBusyView(job({ kind: 'minutes' }), false)?.note).toBe('会后那趟在跑（纪要）');
    expect(gpuBusyView([], true)?.note).toBe('会中模型开着');
    const both = gpuBusyView(job(), true);
    expect(both.busy).toBe(true);
    expect(both.note).toBe('会中模型开着；会后那趟在跑（会后精细稿）');
  });
});

describe('要不要轮询（有活动才问）', () => {
  it('联结命中就要问；读数说在跑 / 停着 running 也要问', () => {
    const selected = row({ state: 'ended', kind: 'meeting' });
    expect(shouldWatch(selected, job(), null)).toBe(true);
    expect(shouldWatch(selected, job(), status({ dir: 'C:\\会议\\另一场' }))).toBe(true);
    expect(
      shouldWatch(selected, job(), status({}, { state: 'running', in_process: true })),
    ).toBe(true);
    expect(shouldWatch(selected, [], status({}, { state: 'running', in_process: true }))).toBe(true);
    expect(shouldWatch(selected, [], status({}, { state: 'running', in_process: false }))).toBe(true);
  });

  it('联结命中但读数已落定（终态那一跳）：不问了 —— watch 要翻 false 才收得了尾（review-1 C1）', () => {
    const selected = row({ state: 'ended', kind: 'meeting' });
    expect(shouldWatch(selected, job(), status({}, { state: 'done', in_process: false }))).toBe(false);
    expect(shouldWatch(selected, job(), status({}, { state: 'idle' }))).toBe(false);
    // 已中断也是终态（b1c6a58e）：重按按钮才再跑，轮询照样收尾
    expect(shouldWatch(selected, job(), status({}, { state: 'interrupted', in_process: false }))).toBe(false);
  });

  it('转码在飞 / 纪要在飞也要问（38817ecf：工序链那两段要跟着翻面）', () => {
    const selected = row({ state: 'ended', kind: 'meeting' });
    // 转码那道工序在本进程里飞（transcode.in_process）
    expect(
      shouldWatch(selected, [], status({ transcode: { ...transcodeIdle(), in_process: true } })),
    ).toBe(true);
    // 纪要在飞（post 清单里 minutes 那一项 running）
    expect(
      shouldWatch(
        selected,
        [],
        status({ post: [{ running: true, kind: 'minutes', dir: selected.dir, started_at: '2026-09-21T22:10:00+08:00', finished_at: null }] }),
      ),
    ).toBe(true);
  });

  it('终态、没跑过、没选中：不问', () => {
    const selected = row({ state: 'ended', kind: 'meeting' });
    expect(shouldWatch(selected, [], status({}, { state: 'done', in_process: false }))).toBe(false);
    expect(shouldWatch(selected, [], status({}, { state: 'idle' }))).toBe(false);
    expect(shouldWatch(selected, [], status({}, { state: 'interrupted', in_process: false }))).toBe(false);
    expect(shouldWatch(selected, [], null)).toBe(false);
    expect(shouldWatch(null, job(), null)).toBe(false);
  });
});

describe('阶段图形标记（形状通道）', () => {
  it('真在跑是转的（spinner → null）、停着一版与已中断是红叉、终态打勾、没跑过空心框', () => {
    expect(phaseMark('in-flight')).toBeNull();
    expect(phaseMark('stale-running')).toBe('glyph--cross');
    expect(phaseMark('interrupted')).toBe('glyph--cross');
    expect(phaseMark('finished')).toBe('glyph--done');
    expect(phaseMark('never-run')).toBe('glyph--idle');
  });
});
