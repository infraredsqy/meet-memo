import { describe, expect, it } from 'vitest';

import type { Ended, SessionStatus } from '../src/contract';
import { chainView } from '../src/home/chain';

/**
 * 事后工序链纯逻辑的用例（本节点）：链上四段各答各的现状、「能不能走」四档（按次序判：落盘出岔 / 落盘没走完 / 有活在跑 / 可以走）、
 * 转码那一段的暂停 / 继续判据。判据全部来自服务给的原文（`status.transcode` /
 * `offline` / `minutes` / `post` 清单与快照 `ended`）——这一层只转手，不造数据。
 */

/** 一格「没跑过」的转码工序读数。 */
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

/** 一格「没跑过」的纪要读数。 */
function minutesIdle(): SessionStatus['minutes'] {
  return { state: 'idle', started_at: null, finished_at: null, error: null };
}

/** 一格「没跑过」的会后离线读数。 */
function offlineIdle(): SessionStatus['offline'] {
  return {
    ledger: 'meeting_offline_progress.jsonl',
    state: 'idle',
    in_process: false,
    started_at: null,
    finished_at: null,
    elapsed_seconds: null,
    current_source: null,
    sources: [],
  };
}

function baseStatus(overrides: Partial<SessionStatus> = {}): SessionStatus {
  return {
    dir: 'C:\\会议\\250906',
    session_id: '250906',
    kind: 'meeting',
    state: 'ended',
    date: '2025-09-06',
    mode: 'both',
    segments: [],
    has_offline: true,
    post: [],
    offline: offlineIdle(),
    transcode: transcodeIdle(),
    minutes: minutesIdle(),
    speakers: [],
    ...overrides,
  };
}

/** 快照 `ended`：收尾三步各给各的状态（`post` 停在 waiting 也算落定）。 */
function endedWith(statuses: Array<[string, string]>): Ended {
  return {
    phase: 'draining',
    reason: 'user',
    ended_at: '2025-09-06T22:00:00+08:00',
    error: null,
    steps: statuses.map(([id, status]) => ({
      id,
      status,
      detail: '',
      error: null,
    })),
  } as unknown as Ended;
}

const DIR = 'C:\\会议\\250906';

function seg(view: ReturnType<typeof chainView>, id: string) {
  return view.segments.find((item) => item.id === id)!;
}

describe('事后工序链', () => {
  it('全 Idle 的已结束场次：四段各答各的，尚未跑过的那两段把入口写在句子里', () => {
    const view = chainView({
      drain: null,
      status: baseStatus(),
      selectedDir: DIR,
    });
    expect(view.segments.map((item) => item.name)).toEqual(['落盘', '转码', '会后转写', '纪要']);
    // 快照上没有这一场的收尾（旧场次）：落盘只说「已结束」，不编三步明细
    expect(seg(view, 'drain')).toMatchObject({ line: '已结束', running: false, failed: false });
    expect(seg(view, 'transcode')).toMatchObject({ line: '还没跑过', running: false });
    expect(seg(view, 'offline').line).toBe('还没跑过（按「生成会后精细稿」起）');
    expect(seg(view, 'minutes').line).toBe('还没跑过（按「生成纪要」起）');
    expect(view.walk).toEqual({
      line: '可以走了：这一场眼下没有在跑的活。',
      mark: 'glyph--idle',
    });
  });

  it('快照里那一场收尾还在跑：落盘那一段在跑，「能不能走」说先别关机', () => {
    const view = chainView({
      drain: { dir: DIR, ended: endedWith([['flush', 'running'], ['log', 'waiting'], ['post', 'waiting']]) },
      status: baseStatus(),
      selectedDir: DIR,
    });
    expect(seg(view, 'drain')).toMatchObject({
      line: '还差最后一块没进盘',
      running: true,
      failed: false,
    });
    expect(view.walk).toEqual({
      line: '先别关机：落盘还没走完。',
      mark: 'glyph--draining',
    });
  });

  it('收尾落定：落盘说「录音已落盘」；别场的收尾不进这一格', () => {
    const settled = { drain: { dir: DIR, ended: endedWith([['flush', 'done'], ['log', 'done'], ['post', 'waiting']]) } };
    expect(seg(chainView({ ...settled, status: baseStatus(), selectedDir: DIR }), 'drain').line).toBe(
      '录音已落盘',
    );
    // 换了选中那一场：快照说的不是它，落盘只说「已结束」
    expect(seg(chainView({ ...settled, status: baseStatus(), selectedDir: 'C:\\会议\\别的' }), 'drain').line).toBe(
      '已结束',
    );
  });

  it('落盘失败：「能不能走」点名失败、指向落盘那一小块', () => {
    const view = chainView({
      drain: {
        dir: DIR,
        ended: {
          ...endedWith([['flush', 'failed'], ['log', 'waiting'], ['post', 'waiting']]),
          error: '磁盘只剩 120 MB',
        },
      },
      status: baseStatus(),
      selectedDir: DIR,
    });
    expect(seg(view, 'drain').failed).toBe(true);
    expect(view.walk.line).toContain('落盘出了岔子');
  });

  it('转码在飞：那一段在跑、带读数（分母按路各算各的），暂停可按、继续不可按', () => {
    const status = baseStatus({
      transcode: {
        state: 'running',
        in_process: true,
        started_at: '2025-09-06T22:01:00',
        finished_at: null,
        elapsed_seconds: 3.2,
        current_source: 'mic',
        sources: [
          { source: 'mic', done: 1, total: 8, remaining: 7, current_segment: 2 },
          { source: 'loopback', done: 0, total: 8, remaining: 8, current_segment: null },
        ],
      },
    });
    const view = chainView({ drain: null, status, selectedDir: DIR });
    const segment = seg(view, 'transcode');
    expect(segment.running).toBe(true);
    expect(segment.line).toBe('正在转：本地 第 2/8 段');
    expect(segment.pause).toEqual({ canPause: true, canResume: false });
    expect(view.walk.line).toBe('先别关机：还有活没跑完；关掉页面不影响，服务照跑。');
  });

  it('转码停在手里的两档：盘上停着一版 running 如实分层；paused 给继续', () => {
    const stale = baseStatus({
      transcode: { ...transcodeIdle(), state: 'running', in_process: false },
    });
    expect(seg(chainView({ drain: null, status: stale, selectedDir: DIR }), 'transcode')).toMatchObject({
      line: '盘上记着在跑，此刻没东西在跑',
      running: false,
    });
    const paused = baseStatus({
      transcode: {
        ...transcodeIdle(),
        state: 'paused',
        in_process: true,
        sources: [{ source: 'mic', done: 3, total: 8, remaining: 5, current_segment: null }],
      },
    });
    const segment = seg(chainView({ drain: null, status: paused, selectedDir: DIR }), 'transcode');
    expect(segment).toMatchObject({ waiting: true, running: false });
    expect(segment.pause).toEqual({ canPause: false, canResume: true });
    expect(segment.line).toContain('已暂停');
  });

  it('会后转写：在飞那一句与进度区同源；被打断自成一相（等人决定，不是失败）', () => {
    const flying = baseStatus({
      offline: {
        ...offlineIdle(),
        state: 'running',
        in_process: true,
        current_source: 'mic',
        sources: [
          { source: 'mic', done: 1, total: 5, remaining: 4, current_chunk: 2, counts: { transcribed: 1, silent: 0, failed: 0, unjudged: 0 } },
          { source: 'loopback', done: 0, total: 5, remaining: 5, current_chunk: null, counts: { transcribed: 0, silent: 0, failed: 0, unjudged: 0 } },
        ],
      },
    });
    const segment = seg(chainView({ drain: null, status: flying, selectedDir: DIR }), 'offline');
    expect(segment.running).toBe(true);
    expect(segment.line).toBe('正在运行：本地 第 2/5 块');

    const interrupted = baseStatus({ offline: { ...offlineIdle(), state: 'interrupted' } });
    const stopped = seg(chainView({ drain: null, status: interrupted, selectedDir: DIR }), 'offline');
    expect(stopped).toMatchObject({ line: '已中断，可以重按', waiting: true, failed: false });
  });

  it('纪要：在飞优先答「正在生成」；盘上停着一版 running 如实分层；三档终态各说各的', () => {
    const flying = baseStatus({
      post: [{ running: true, kind: 'minutes', dir: DIR, started_at: '2025-09-06T23:00:00', finished_at: null }],
    });
    expect(seg(chainView({ drain: null, status: flying, selectedDir: DIR }), 'minutes')).toMatchObject({
      line: '正在生成',
      running: true,
    });
    const stale = baseStatus({ minutes: { ...minutesIdle(), state: 'running' } });
    expect(seg(chainView({ drain: null, status: stale, selectedDir: DIR }), 'minutes').line).toBe(
      '盘上记着在跑，此刻没东西在跑',
    );
    const done = baseStatus({ minutes: { ...minutesIdle(), state: 'done' } });
    expect(seg(chainView({ drain: null, status: done, selectedDir: DIR }), 'minutes').line).toBe('已完成');
    const delivered = baseStatus({ minutes: { ...minutesIdle(), state: 'delivered' } });
    expect(seg(chainView({ drain: null, status: delivered, selectedDir: DIR }), 'minutes').line).toBe(
      '已交给 DSH 生成',
    );
    const failed = baseStatus({ minutes: { ...minutesIdle(), state: 'failed', error: 'DSH 没应答' } });
    const segment = seg(chainView({ drain: null, status: failed, selectedDir: DIR }), 'minutes');
    expect(segment).toMatchObject({ line: '失败', failed: true });
  });

  it('「能不能走」按在飞判：盘上停着的一版 running 不算在飞，post 清单里有才算', () => {
    // 盘上停着一版 running、本进程没有在飞的：没有活儿在跑 → 可以走
    const stale = baseStatus({ minutes: { ...minutesIdle(), state: 'running' } });
    expect(chainView({ drain: null, status: stale, selectedDir: DIR }).walk.line).toBe(
      '可以走了：这一场眼下没有在跑的活。',
    );
    // post 清单里有 minutes 在飞：先别关机
    const flying = baseStatus({
      post: [{ running: true, kind: 'minutes', dir: DIR, started_at: '2025-09-06T23:00:00', finished_at: null }],
    });
    expect(chainView({ drain: null, status: flying, selectedDir: DIR }).walk.line).toBe(
      '先别关机：还有活没跑完；关掉页面不影响，服务照跑。',
    );
  });
});
