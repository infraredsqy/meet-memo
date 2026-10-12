import { describe, expect, it } from 'vitest';

import {
  alertsHiddenByHeartbeat,
  applyHeartbeatReading,
  captureGapNote,
  captureStalled,
  CAPTURE_STOP_CONFIRMATIONS,
  clockLabelWhileLost,
  frozenClockValue,
  initialHeartbeatState,
  lostBanner,
  recoveredNoteText,
  recoveredNoteVisible,
  transcriptGap,
  RECOVERED_NOTE_SECONDS,
} from '../src/live/heartbeat';
import { makeHeartbeat, T } from './live-fixtures';

/**
 * 心跳四级：正常（不显示）/ 迟到（一行弱提示）/ 失联（全屏横幅）/ 已恢复（30 秒短记录）。
 *
 * 判据是**应答里的 `server_time` 与本地时钟的差**，所以这里都以「本地现在几毫秒」
 * 作参数显式传进去 —— 不读系统时钟，用例不随机器上的时间飘。
 */

const MS = (hhmmss: string): number => Date.parse(T(hhmmss));

function reply(serverTime: string, status: '正常' | '迟到' | '失联', lagSeconds = 0) {
  return {
    kind: 'reply' as const,
    heartbeat: makeHeartbeat({ server_time: serverTime }),
    status,
    lagSeconds,
  };
}

/** `recording_alive === false` 的一次读数（「采集已停」那一档的输入）。 */
function stoppedReply(serverTime: string, seconds: number | null) {
  return {
    kind: 'reply' as const,
    heartbeat: makeHeartbeat({
      server_time: serverTime,
      recording_alive: false,
      seconds_since_last_tick: seconds,
    }),
    status: '正常' as const,
    lagSeconds: 0,
  };
}

describe('applyHeartbeatReading：四级迁移', () => {
  it('正常：什么都不显示', () => {
    const state = applyHeartbeatReading(initialHeartbeatState(), reply(T('20:57:00'), '正常'), MS('20:57:00'));
    expect(state.level).toBe('正常');
    expect(state.lateText).toBeNull();
    expect(state.lastReplyAt).toBe(T('20:57:00'));
  });

  it('迟到：一行字，带「几秒没收到应答」', () => {
    const state = applyHeartbeatReading(
      initialHeartbeatState(),
      reply(T('20:56:52'), '迟到', 8),
      MS('20:57:00'),
    );
    expect(state.level).toBe('迟到');
    expect(state.lateText).toBe('心跳迟到 · 8 秒没收到应答');
    // 迟到这一档不出声、无横幅 —— 它连告警区都不碰
    expect(alertsHiddenByHeartbeat(state)).toBe(false);
  });

  it('失联：记下起点，冻结时刻停在最后一次应答', () => {
    const first = applyHeartbeatReading(initialHeartbeatState(), reply(T('20:57:31'), '正常'), MS('20:57:32'));
    const lost = applyHeartbeatReading(
      first,
      { kind: 'error', error: new Error('HTTP 502'), status: '失联' },
      MS('20:58:05'),
    );
    expect(lost.level).toBe('失联');
    expect(lost.lostSinceMs).toBe(MS('20:58:05'));
    expect(lost.frozenAt).toBe(T('20:57:31'));
    expect(lost.lastReplyAt).toBe(T('20:57:31'));
    expect(alertsHiddenByHeartbeat(lost)).toBe(true);
  });

  it('一次应答都没成功过时不算失联：我还没问到，别把真实存在的告警藏起来', () => {
    const never = applyHeartbeatReading(
      initialHeartbeatState(),
      { kind: 'error', error: new Error('502'), status: '失联' },
      MS('20:57:40'),
    );
    expect(never.level).toBe('失联');
    expect(never.lastReplyAt).toBeNull();
    expect(alertsHiddenByHeartbeat(never)).toBe(false);
  });

  it('失联持续：起点不动，冻结时刻还是最后那次应答', () => {
    const first = applyHeartbeatReading(initialHeartbeatState(), reply(T('20:57:31'), '正常'), MS('20:57:32'));
    const lost = applyHeartbeatReading(first, { kind: 'error', error: new Error('502'), status: '失联' }, MS('20:58:05'));
    const later = applyHeartbeatReading(lost, { kind: 'error', error: new Error('502'), status: '失联' }, MS('20:58:45'));
    expect(later.lostSinceMs).toBe(MS('20:58:05'));
    expect(later.frozenAt).toBe(T('20:57:31'));
  });

  it('已恢复：算出中断多少秒，并开始露那条短记录', () => {
    const first = applyHeartbeatReading(initialHeartbeatState(), reply(T('20:57:31'), '正常'), MS('20:57:32'));
    const lost = applyHeartbeatReading(first, { kind: 'error', error: new Error('502'), status: '失联' }, MS('20:57:40'));
    const back = applyHeartbeatReading(lost, reply(T('20:58:22'), '正常'), MS('20:58:22'));
    expect(back.level).toBe('已恢复');
    expect(back.gapSeconds).toBe(42);
    expect(recoveredNoteText(back.gapSeconds!)).toBe('已恢复，中断 42 秒');
    expect(recoveredNoteVisible(back, MS('20:58:50'))).toBe(true);
  });

  it('恢复记录 30 秒到点自己消失（不靠服务再推一帧）', () => {
    const first = applyHeartbeatReading(initialHeartbeatState(), reply(T('20:57:31'), '正常'), MS('20:57:32'));
    const lost = applyHeartbeatReading(first, { kind: 'error', error: new Error('502'), status: '失联' }, MS('20:57:40'));
    const back = applyHeartbeatReading(lost, reply(T('20:58:22'), '正常'), MS('20:58:22'));
    const after = applyHeartbeatReading(
      back,
      reply(T('20:58:52'), '正常'),
      MS('20:58:22') + RECOVERED_NOTE_SECONDS * 1000,
    );
    expect(recoveredNoteVisible(after, MS('20:58:22') + RECOVERED_NOTE_SECONDS * 1000)).toBe(false);
    expect(after.level).toBe('正常');
  });

  it('失联之后回到迟到：也算「已恢复」，同时带着迟到那一行', () => {
    const lost = applyHeartbeatReading(
      initialHeartbeatState(),
      { kind: 'error', error: new Error('502'), status: '失联' },
      MS('20:57:40'),
    );
    const back = applyHeartbeatReading(lost, reply(T('20:58:10'), '迟到', 12), MS('20:58:22'));
    expect(back.level).toBe('已恢复');
    expect(back.gapSeconds).toBe(42);
  });
});

describe('lostBanner：失联横幅的正文', () => {
  it('写出周期、次数与最后一次应答（带秒）', () => {
    const state = applyHeartbeatReading(initialHeartbeatState(), reply(T('20:57:31'), '正常'), MS('20:57:32'));
    const view = lostBanner(state, 10);
    expect(view.body).toContain('10 秒');
    expect(view.body).toContain('20:57:31');
    expect(view.lastReply).toBe('20:57:31');
  });

  it('一次都没成功过：最后一次应答写 `—`', () => {
    expect(lostBanner(initialHeartbeatState(), 10).lastReply).toBe('—');
  });

  it('周期从快照来（clamp 过的那一个）', () => {
    expect(lostBanner(initialHeartbeatState(), 30).body).toContain('30 秒一次的应答');
  });
});

describe('captureStalled：采集已停那一档', () => {
  it('录音中 + recording_alive 为假 + 连续计数够两拍：算得出最后一次收到帧的时刻', () => {
    const view = captureStalled(
      makeHeartbeat({ server_time: T('20:57:31'), recording_alive: false, seconds_since_last_tick: 42.5 }),
      'recording',
      CAPTURE_STOP_CONFIRMATIONS,
    );
    expect(view).toEqual({ seconds: 42.5, lastFrameAt: '20:56' });
  });

  it('两拍规则（de899128）：单次 false 不摆这一档，计数够两次才判停', () => {
    const beat = makeHeartbeat({ recording_alive: false, seconds_since_last_tick: 42.5 });
    expect(captureStalled(beat, 'recording', 0)).toBeNull();
    expect(captureStalled(beat, 'recording', 1)).toBeNull();
    expect(captureStalled(beat, 'recording', CAPTURE_STOP_CONFIRMATIONS)).toEqual({
      seconds: 42.5,
      lastFrameAt: '20:56',
    });
  });

  it('recording_alive 为真：不在这一档', () => {
    expect(captureStalled(makeHeartbeat({ recording_alive: true }), 'recording', CAPTURE_STOP_CONFIRMATIONS)).toBeNull();
  });

  it('recording_alive 为 null（还没有第一帧）：不在这一档', () => {
    expect(
      captureStalled(makeHeartbeat({ recording_alive: null, seconds_since_last_tick: null }), 'recording', CAPTURE_STOP_CONFIRMATIONS),
    ).toBeNull();
  });

  it('空闲与已结束不摆这一档（服务给的 recording_alive 本来就是 null）', () => {
    expect(captureStalled(makeHeartbeat({ recording_alive: null }), 'idle', CAPTURE_STOP_CONFIRMATIONS)).toBeNull();
    expect(captureStalled(makeHeartbeat({ recording_alive: false }), 'ended', CAPTURE_STOP_CONFIRMATIONS)).toBeNull();
  });

  it('一帧都没收到过（超窗饿死档）：判停之后仍不编一个时刻', () => {
    const view = captureStalled(
      makeHeartbeat({ recording_alive: false, seconds_since_last_tick: null }),
      'recording',
      CAPTURE_STOP_CONFIRMATIONS,
    );
    expect(view).toEqual({ seconds: 0, lastFrameAt: '—' });
  });

  it('还没拿到应答时不出现', () => {
    expect(captureStalled(null, 'recording', CAPTURE_STOP_CONFIRMATIONS)).toBeNull();
  });
});

describe('两拍规则的连续计数（de899128，归并点在 applyHeartbeatReading）', () => {
  it('false 连着数：一拍 1、两拍 2', () => {
    const one = applyHeartbeatReading(initialHeartbeatState(), stoppedReply(T('20:57:10'), 3), MS('20:57:10'));
    expect(one.stalledBeats).toBe(1);
    const two = applyHeartbeatReading(one, stoppedReply(T('20:57:20'), 13), MS('20:57:20'));
    expect(two.stalledBeats).toBe(2);
  });

  it('true 把计数清零', () => {
    const one = applyHeartbeatReading(initialHeartbeatState(), stoppedReply(T('20:57:10'), 3), MS('20:57:10'));
    const back = applyHeartbeatReading(one, reply(T('20:57:20'), '正常'), MS('20:57:20'));
    expect(back.stalledBeats).toBe(0);
  });

  it('null（还没有第一帧的未知档）把计数清零 —— 开场不误报的关键一格', () => {
    const one = applyHeartbeatReading(initialHeartbeatState(), stoppedReply(T('20:57:10'), 3), MS('20:57:10'));
    const unknown = applyHeartbeatReading(
      one,
      {
        kind: 'reply' as const,
        heartbeat: makeHeartbeat({ server_time: T('20:57:20'), recording_alive: null, seconds_since_last_tick: null }),
        status: '正常',
        lagSeconds: 0,
      },
      MS('20:57:20'),
    );
    expect(unknown.stalledBeats).toBe(0);
  });

  it('失联（读不到应答）也把计数清零：任何非 false 读数都重置', () => {
    const one = applyHeartbeatReading(initialHeartbeatState(), stoppedReply(T('20:57:10'), 3), MS('20:57:10'));
    const lost = applyHeartbeatReading(
      one,
      { kind: 'error', error: new Error('HTTP 502'), status: '失联' },
      MS('20:57:20'),
    );
    expect(lost.stalledBeats).toBe(0);
  });
});

describe('失联档的几处读数', () => {
  it('时长改标签「最后读数」，数字沿用最后一份快照', () => {
    expect(frozenClockValue(4354)).toBe('01:12:34');
    expect(frozenClockValue(null)).toBe('00:00:00');
  });

  it('标签与数字一起冻（返工轮 m1）：失联中标签 clockLabelWhileLost()、数字 frozenClockValue()，都不再走', () => {
    expect(clockLabelWhileLost()).toBe('最后读数');
  });

  it('转写断口：`[20:57–20:58] 采集中断`', () => {
    expect(transcriptGap(T('20:57:31'), T('20:58:13'))).toEqual({
      from: '20:57',
      to: '20:58',
      text: '采集中断',
    });
  });

  it('没有冻结时刻就没有断口', () => {
    expect(transcriptGap(null, T('20:58:13'))).toBeNull();
  });
});

describe('captureGapNote：帧空洞记录的提示行（gil-stall-hole）', () => {
  it('一次空洞：`曾中断 X 秒，该段内容未记上`', () => {
    const beat = makeHeartbeat({
      capture_gaps: {
        total_count: 1,
        total_seconds: 2.4,
        recent: [{ at: T('21:03:20'), seconds: 2.4 }],
      },
    });
    expect(captureGapNote(beat)).toBe('曾中断 2.4 秒，该段内容未记上');
  });

  it('多次空洞：次数与总秒数一句说清', () => {
    const beat = makeHeartbeat({
      capture_gaps: {
        total_count: 3,
        total_seconds: 7.5,
        recent: [{ at: T('21:03:20'), seconds: 2.4 }],
      },
    });
    expect(captureGapNote(beat)).toBe('曾中断 3 次共 7.5 秒，该段内容未记上');
  });

  it('空记录、缺应答、缺字段（旧服务）：都是 null，null 安全', () => {
    expect(captureGapNote(null)).toBeNull();
    expect(captureGapNote(makeHeartbeat())).toBeNull();
    expect(
      captureGapNote(makeHeartbeat({ capture_gaps: { total_count: 0, total_seconds: 0, recent: [] } })),
    ).toBeNull();
  });

  it('空洞只在提示行说话：不弹红横幅、不进两拍计数', () => {
    const beat = makeHeartbeat({
      capture_gaps: {
        total_count: 1,
        total_seconds: 2.4,
        recent: [{ at: T('21:03:20'), seconds: 2.4 }],
      },
    });
    // 两拍计数的单一归并点：`recording_alive` 为 true 的读数清零，空洞记录不投任何一拍
    const state = applyHeartbeatReading(
      initialHeartbeatState(),
      { kind: 'reply', heartbeat: beat, status: '正常', lagSeconds: 0 },
      MS('21:03:30'),
    );
    expect(state.stalledBeats).toBe(0);
    // 现在时的判档不认历史事实：`captureStalled` 只看 `recording_alive`
    expect(captureStalled(beat, 'recording', state.stalledBeats)).toBeNull();
  });
});
