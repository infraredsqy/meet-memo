import { describe, expect, it } from 'vitest';

import {
  alertsHiddenByHeartbeat,
  applyHeartbeatReading,
  captureStalled,
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
  it('录音中 + recording_alive 为假：算得出最后一次收到帧的时刻', () => {
    const view = captureStalled(
      makeHeartbeat({ server_time: T('20:57:31'), recording_alive: false, seconds_since_last_tick: 42.5 }),
      'recording',
    );
    expect(view).toEqual({ seconds: 42.5, lastFrameAt: '20:56' });
  });

  it('recording_alive 为真：不在这一档', () => {
    expect(captureStalled(makeHeartbeat({ recording_alive: true }), 'recording')).toBeNull();
  });

  it('空闲与已结束不摆这一档（服务给的 recording_alive 本来就是 null）', () => {
    expect(captureStalled(makeHeartbeat({ recording_alive: null }), 'idle')).toBeNull();
    expect(captureStalled(makeHeartbeat({ recording_alive: false }), 'ended')).toBeNull();
  });

  it('一帧都没收到过：不编一个时刻', () => {
    const view = captureStalled(
      makeHeartbeat({ recording_alive: false, seconds_since_last_tick: null }),
      'recording',
    );
    expect(view).toEqual({ seconds: 0, lastFrameAt: '—' });
  });

  it('还没拿到应答时不出现', () => {
    expect(captureStalled(null, 'recording')).toBeNull();
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
