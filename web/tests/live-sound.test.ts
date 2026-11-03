import { describe, expect, it } from 'vitest';

import {
  BEEP_SECONDS,
  createAlarmSound,
  type AudioContextLike,
  type GainLike,
  type OscillatorLike,
} from '../src/live/sound';
import { deadEnvelope, hasAnyReading, holdFrameWhileDataStopped, meterView, releaseHeldFrame, type StoredLevels } from '../src/live/levels';
import { dbfsToPercent } from '../src/live/format';
import { liveView } from '../src/live/view';
import { CAPTURE_STOP_CONFIRMATIONS, initialHeartbeatState } from '../src/live/heartbeat';
import {
  makeAlert,
  makeAlerts,
  makeHeartbeat,
  makeLevels,
  makeSettings,
  makeSnapshot,
  makeSource,
  T,
} from './live-fixtures';

/**
 * 发声与电平表取数。
 *
 * 发声用**假的 `AudioContext`** 数振荡器启动次数（任务书「验证口径」指定的手法）：
 * 真机上那一下听不见，但「启动了几次振荡器」数得清清楚楚。
 */

interface FakeContext extends AudioContextLike {
  readonly starts: number[];
  readonly stops: number[];
  readonly resumed: number;
}

function fakeContext(state = 'running'): FakeContext {
  const starts: number[] = [];
  const stops: number[] = [];
  let resumed = 0;
  let clock = 0;
  let oscillators = 0;
  const context: FakeContext = {
    get state() {
      return state;
    },
    get currentTime() {
      clock += 0.001;
      return clock;
    },
    destination: {},
    get starts() {
      return starts;
    },
    get stops() {
      return stops;
    },
    get resumed() {
      return resumed;
    },
    resume() {
      resumed += 1;
      return Promise.resolve();
    },
    createOscillator(): OscillatorLike {
      oscillators += 1;
      const index = oscillators;
      return {
        type: 'sine',
        frequency: { value: 0 },
        connect: () => undefined,
        start: () => starts.push(index),
        stop: () => stops.push(index),
      };
    },
    createGain(): GainLike {
      return {
        gain: {
          value: 0,
          setValueAtTime: () => undefined,
          linearRampToValueAtTime: () => undefined,
        },
        connect: () => undefined,
      };
    },
  };
  return context;
}

describe('createAlarmSound：单次短促一声', () => {
  it('响一声就是启动一次振荡器，并在定长之后停掉它（不循环）', () => {
    const ctx = fakeContext();
    const handle = createAlarmSound(() => ctx);
    expect(handle.play()).toBe(true);
    expect(ctx.starts).toHaveLength(1);
    expect(ctx.stops).toHaveLength(1);
    expect(BEEP_SECONDS).toBeGreaterThan(0);
    expect(BEEP_SECONDS).toBeLessThan(1);
  });

  it('响两次就是两次独立的振荡器（每次都是新的一个）', () => {
    const ctx = fakeContext();
    const handle = createAlarmSound(() => ctx);
    handle.play();
    handle.play();
    expect(ctx.starts).toEqual([1, 2]);
  });

  it('被自动播放策略按住（suspended）：这一次不放声，并去补一次 resume', () => {
    const ctx = fakeContext('suspended');
    const handle = createAlarmSound(() => ctx);
    expect(handle.play()).toBe(false);
    expect(ctx.starts).toHaveLength(0);
    expect(ctx.resumed).toBe(1);
  });

  it('用户点过之后 unlock()：补一次授权，之后放得出来', () => {
    const ctx = fakeContext('suspended');
    const handle = createAlarmSound(() => ctx);
    handle.unlock();
    expect(handle.unlocked()).toBe(true);
    expect(ctx.resumed).toBe(1);
  });

  it('这台机器没有 WebAudio：不出声，别的照旧（不抛）', () => {
    const handle = createAlarmSound(null);
    expect(handle.play()).toBe(false);
    expect(handle.unlocked()).toBe(false);
  });

  it('造 AudioContext 当场抛：吞掉，不让它把整页带下去', () => {
    const handle = createAlarmSound(() => {
      throw new Error('没有可用的音频设备');
    });
    expect(handle.play()).toBe(false);
  });
});

describe('meterView：柱高来自服务推的读数', () => {
  it('快照上的 dBFS 线性映射成百分比', () => {
    const view = meterView(makeSource('mic', { level_dbfs: -30, peak_dbfs: -15 }), null, 7);
    expect(view.levelPercent).toBeCloseTo(50, 6);
    expect(view.holdPercent).toBeCloseTo(75, 6);
    expect(view.readout).toBe(-30);
    expect(view.fromFrame).toBe(false);
  });

  it('新鲜的 levels 帧优先（高频通道），包络也取帧里那一份', () => {
    const frames = { revision: 8, frames: makeLevels(-20, -48, 8).frames };
    const view = meterView(makeSource('mic', { level_dbfs: -35.2 }), frames, 8);
    // 填充取帧里的 −20 dBFS（= 66.7%），不取快照那个 −35.2
    expect(view.levelPercent).toBeCloseTo(66.667, 3);
    expect(view.fromFrame).toBe(true);
    // 包络同样是帧里那一份、按同一个区间映射：−20 dBFS → 66.7%
    expect(view.envelope).toHaveLength(15);
    expect(view.envelope[0]).toBeCloseTo(66.667, 3);
  });

  it('包络优先取帧里那一份（录音途中快照的 envelope 还是空的）', () => {
    const frames = { revision: 7, frames: makeLevels(-20, -48, 7).frames };
    const view = meterView(makeSource('mic', { envelope: [] }), frames, 7);
    expect(view.envelope).toHaveLength(15);
    expect(view.envelope[0]).toBeCloseTo(dbfsToPercent(-20), 3);
  });

  it('手上没有新鲜帧时才退回快照那一份', () => {
    const view = meterView(makeSource('mic', { envelope: [-60, -30] }), null, 7);
    expect(view.envelope).toEqual([0, 50]);
  });

  it('比快照还旧的帧不用（一场结束后最后那帧会一直躺在手上）', () => {
    const frames = { revision: 6, frames: makeLevels(-20, -48, 6).frames };
    const view = meterView(makeSource('mic', { level_dbfs: -35.2 }), frames, 7);
    expect(view.fromFrame).toBe(false);
    expect(view.readout).toBe(-35.2);
  });

  it('包络一根不多一根不少，逐根都按同一个区间映射', () => {
    const source = makeSource('mic', { envelope: [-60, -30, 0, -96, 12] });
    const view = meterView(source, null, 7);
    expect(view.envelope).toEqual([0, 50, 100, 0, 100]);
  });

  it('死掉的那一路：包络塌成 15 根零位线', () => {
    expect(deadEnvelope(15)).toEqual(new Array(15).fill(0));
  });

  it('没有读数时读数是 null（由组件退成 `—`）', () => {
    expect(meterView(makeSource('mic', { level_dbfs: null }), null, 7).readout).toBeNull();
  });
});

describe('hasAnyReading', () => {
  it('两路都没读数时是假（空闲态「未探测」那一档）', () => {
    const snapshot = makeSnapshot({
      session: null,
      sources: [
        makeSource('mic', { health: 'unprobed', level_dbfs: null }),
        makeSource('loopback', { health: 'unprobed', level_dbfs: null }),
      ],
    });
    expect(hasAnyReading(snapshot)).toBe(false);
    expect(hasAnyReading(null)).toBe(false);
  });
});

describe('holdFrameWhileDataStopped：失联/采集已停时电平冻在停住那一刻', () => {
  it('冻结期间又来的帧不动画面：按住的还是停住那一份（App 停止消费新帧，C1）', () => {
    const atStop = makeLevels(-20, -48, 8); // 停住那一刻手上的帧
    const laterArrival = makeLevels(-3, -55, 9); // 冻结期间服务还在推的帧
    // 冻结开始：按住当下这份
    let held: StoredLevels | null = holdFrameWhileDataStopped(null, atStop);
    expect(held).toBe(atStop);
    // 之后无论来多少帧，按住的那一份不变 —— 电平不许被画成还在动
    expect(holdFrameWhileDataStopped(held, laterArrival)).toBe(atStop);
    // 画面吃的是按住那份：--level 停在 −20 dBFS 的 66.7%，不随新帧走
    const view = meterView(makeSource('mic'), { revision: 9, frames: held!.frames }, 9);
    expect(view.levelPercent).toBeCloseTo(66.667, 3);
    expect(view.readout).toBe(-20);
    expect(view.fromFrame).toBe(true);
  });

  it('恢复后回到实时：松手（held 清空）之后，新到的帧重新顶上来（App 恢复路径，C1）', () => {
    const atStop = makeLevels(-20, -48, 8);
    const afterRecovery = makeLevels(-6, -51, 10);
    let held: StoredLevels | null = holdFrameWhileDataStopped(null, atStop);
    // 冻结期间停在停住那一份
    expect(holdFrameWhileDataStopped(held, afterRecovery)).toBe(atStop);
    // 失联解除：松手，held 归空
    held = releaseHeldFrame();
    expect(held).toBeNull();
    // 恢复后的画面取新到的帧：−6 dBFS → 90%
    const view = meterView(makeSource('mic'), afterRecovery, 10);
    expect(view.levelPercent).toBeCloseTo(90, 6);
    expect(view.readout).toBe(-6);
  });
});

describe('liveView：一份快照摊成主视图', () => {
  it('录音中：时长走秒、两路读数、包络时间戳', () => {
    const snapshot = makeSnapshot({ revision: 7 });
    const view = liveView(snapshot, initialHeartbeatState(), null, null, T('20:57:10'));
    expect(view.status.name).toBe('录音中');
    expect(view.clock).toEqual({ label: '已录', value: '01:12:34', faint: false });
    expect(view.channels.map((c) => c.name)).toEqual(['本地 · 麦克风', '远端 · loopback']);
    expect(view.channels[0]!.readout).toBe('\u221235.2 dBFS');
    expect(view.channels[0]!.meter.envelope).toHaveLength(15);
    expect(view.channels[0]!.stamp).toBe('20:57');
    expect(view.fatal).toBeNull();
    expect(view.hints).toEqual([]);
    expect(view.alertsHidden).toBe(false);
  });

  it('告警：横幅三处 + 出事那一路打叉、另一路照旧', () => {
    const snapshot = makeSnapshot({
      top_state: 'alert',
      sources: [
        makeSource('mic'),
        makeSource('loopback', {
          health: 'lost',
          lost_since: T('20:57:00'),
          level_dbfs: -96,
          envelope: new Array(15).fill(-96),
        }),
      ],
      alerts: makeAlerts({ critical: makeAlert({ duration_seconds: 780 }) }),
      settings: makeSettings(),
    });
    const view = liveView(snapshot, initialHeartbeatState(), null, null, T('21:10:00'));
    expect(view.status.name).toBe('告警');
    expect(view.fatal).toEqual({
      where: '远端这一路信号丢失',
      since: '从 20:57 起',
      elapsed: '已丢 13 分钟',
    });
    expect(view.channels[0]!.health.dead).toBe(false);
    expect(view.channels[0]!.readout).toBe('\u221235.2 dBFS');
    expect(view.channels[1]!.health.dead).toBe(true);
    expect(view.channels[1]!.health.chip).toBe('远端 · 信号丢失，从 20:57 起');
    expect(view.channels[1]!.meter.envelope.every((h) => h === 0)).toBe(true);
  });

  it('失联：读数退成 `—`、包络时间戳停在最后应答、告警区整块收掉', () => {
    const snapshot = makeSnapshot({
      top_state: 'alert',
      sources: [makeSource('mic'), makeSource('loopback', { health: 'lost', lost_since: T('20:57:00') })],
      alerts: makeAlerts({ critical: makeAlert() }),
    });
    const lost = {
      ...initialHeartbeatState(),
      level: '失联' as const,
      frozenAt: T('20:57:31'),
      lastReplyAt: T('20:57:31'),
    };
    const view = liveView(snapshot, lost, null, makeHeartbeat(), T('20:58:20'));
    expect(view.alertsHidden).toBe(true);
    expect(view.channels[0]!.readout).toBe('—');
    expect(view.channels[0]!.meter.frozen).toBe(true);
    expect(view.channels[0]!.stamp).toBe('20:57');
  });

  it('刚打开页面、还没收到过一次应答：不按失联画（读数照给、告警区照在）', () => {
    const snapshot = makeSnapshot({
      top_state: 'alert',
      sources: [makeSource('mic'), makeSource('loopback', { health: 'lost', lost_since: T('20:57:00') })],
      alerts: makeAlerts({ critical: makeAlert() }),
    });
    const notYet = { ...initialHeartbeatState(), level: '失联' as const };
    const view = liveView(snapshot, notYet, null, null, T('20:45:22'));
    expect(view.alertsHidden).toBe(false);
    expect(view.channels[0]!.readout).toBe('\u221235.2 dBFS');
    expect(view.channels[0]!.meter.frozen).toBe(false);
    // 出事那一路照旧按它自己的档画（这一条与服务在不在无关）
    expect(view.channels[1]!.health.dead).toBe(true);
  });

  it('采集已停：不在失联那一档，但读数与包络也冻住、健康位写「采集已停止」', () => {
    const snapshot = makeSnapshot();
    const reply = makeHeartbeat({ recording_alive: false, seconds_since_last_tick: 42.5 });
    // 两拍规则的计数已经攒够（de899128）：这一档只画「确认过停了」的现场
    const confirmed = { ...initialHeartbeatState(), stalledBeats: CAPTURE_STOP_CONFIRMATIONS };
    const view = liveView(snapshot, confirmed, null, reply, T('20:57:40'));
    expect(view.stalled).toEqual({ seconds: 42.5, lastFrameAt: '20:56' });
    expect(view.alertsHidden).toBe(false);
    // 与失联同一套冻结处置：读数退成 `—`、健康位写「采集已停止」、电平冻结、时间戳停住
    expect(view.channels[0]!.readout).toBe('—');
    expect(view.channels[0]!.health.text).toBe('采集已停止');
    expect(view.channels[0]!.health.chip).toBe('本地 · 采集已停止');
    expect(view.channels[0]!.health.tone).toBe('stopped');
    expect(view.channels[0]!.meter.frozen).toBe(true);
    expect(view.channels[0]!.stamp).toBe('20:56');
    expect(view.channels[1]!.readout).toBe('—');
    expect(view.channels[1]!.health.chip).toBe('远端 · 采集已停止');
  });

  it('单次 false 还没攒够两拍：横幅判据不成立，画面照常实时', () => {
    const snapshot = makeSnapshot();
    const reply = makeHeartbeat({ recording_alive: false, seconds_since_last_tick: 42.5 });
    const view = liveView(snapshot, initialHeartbeatState(), null, reply, T('20:57:40'));
    expect(view.stalled).toBeNull();
    expect(view.channels[0]!.readout).toBe('\u221235.2 dBFS');
    expect(view.channels[0]!.health.tone).not.toBe('stopped');
  });

  it('帧空洞留痕（gil-stall-hole）：gapNote 上视图，但提示级 —— 不弹红、不进判停、发声结构不碰', () => {
    const snapshot = makeSnapshot();
    const reply = makeHeartbeat({
      capture_gaps: {
        total_count: 1,
        total_seconds: 2.4,
        recent: [{ at: T('21:03:20'), seconds: 2.4 }],
      },
    });
    const view = liveView(snapshot, initialHeartbeatState(), null, reply, T('21:03:30'));
    expect(view.gapNote).toBe('曾中断 2.4 秒，该段内容未记上');
    // 历史事实不进任何现在时的判档：致命横幅不弹、采集已停不判、告警区照在
    expect(view.fatal).toBeNull();
    expect(view.stalled).toBeNull();
    expect(view.alertsHidden).toBe(false);
    expect(view.hints).toEqual([]);
    // 读数照常实时：空洞是过去的事，画面不为此冻结
    expect(view.channels[0]!.readout).toBe('\u221235.2 dBFS');
  });

  it('采集停了：健康位说的是「采集已停止」，不把它画成「这一路死了」', () => {
    const snapshot = makeSnapshot();
    const reply = makeHeartbeat({ recording_alive: false, seconds_since_last_tick: 3 });
    const confirmed = { ...initialHeartbeatState(), stalledBeats: CAPTURE_STOP_CONFIRMATIONS };
    const view = liveView(snapshot, confirmed, null, reply, T('20:57:40'));
    for (const channel of view.channels) {
      expect(channel.health.dead).toBe(false);
      expect(channel.health.text).toBe('采集已停止');
    }
  });

  it('采集没停（recording_alive 为真）：读数照给、健康位照旧', () => {
    const snapshot = makeSnapshot();
    const reply = makeHeartbeat({ recording_alive: true, seconds_since_last_tick: 0.4 });
    const view = liveView(snapshot, initialHeartbeatState(), null, reply, T('20:57:40'));
    expect(view.stalled).toBeNull();
    expect(view.channels[0]!.readout).toBe('\u221235.2 dBFS');
    expect(view.channels[0]!.health.text).toBe('采样中');
    expect(view.channels[0]!.meter.frozen).toBe(false);
  });

  it('失联与采集已停互斥：这一档是失联，采集已停那条横幅的判据不成立', () => {
    const snapshot = makeSnapshot();
    const reply = makeHeartbeat({ recording_alive: false, seconds_since_last_tick: 42.5 });
    const lost = {
      ...initialHeartbeatState(),
      level: '失联' as const,
      frozenAt: T('20:57:31'),
      lastReplyAt: T('20:57:31'),
    };
    const view = liveView(snapshot, lost, null, reply, T('20:58:20'));
    // 数据冻结那一套两档共用（屏幕上确实没有真数据了），
    // 但「出事了」的那句话只有失联那一条全屏横幅
    expect(view.alertsHidden).toBe(true);
    expect(view.channels[0]!.readout).toBe('—');
    // 失联这一档写「数据已停止」，与「采集已停止」分开（原因不同，处置同一套）
    expect(view.channels[0]!.health.chip).toBe('本地 · 数据已停止');
  });

  it('空闲：复位档（`notice` 槽位已随默认输出根退场，顶部不再有重启提示行）', () => {
    const snapshot = makeSnapshot({
      top_state: 'idle',
      session: null,
      sources: [
        makeSource('mic', { health: 'ready', level_dbfs: null, envelope: [] }),
        makeSource('loopback', { health: 'ready', level_dbfs: null, envelope: [] }),
      ],
    });
    expect('notice' in snapshot).toBe(false);
    const view = liveView(snapshot, initialHeartbeatState(), null, null, T('09:00:00'));
    expect(view.clock).toEqual({ label: '已录', value: '00:00:00', faint: true });
    expect(view.channels.every((c) => c.stamp === '—')).toBe(true);
    // 顶部那一行「服务重启过」没有这回事，视图层不再产生该格
    expect('restartNote' in view).toBe(false);
    expect(view.fatal).toBeNull();
  });

  it('已结束·收尾中：时长改标签并冻结', () => {
    const snapshot = makeSnapshot({
      top_state: 'ended',
      session: { ...makeSnapshot().session!, elapsed_seconds: 8363, frozen: true },
      ended: {
        phase: 'draining',
        steps: [
          { id: 'flush', status: 'running', detail: '在写盘', error: null },
          { id: 'log', status: 'done', detail: '', error: null },
          { id: 'post', status: 'waiting', detail: '', error: null },
        ],
        error: null,
      },
    });
    const view = liveView(snapshot, initialHeartbeatState(), null, null, T('22:19:00'));
    expect(view.status.name).toBe('已结束 · 收尾中');
    expect(view.clock).toEqual({ label: '本场时长', value: '02:19:23', faint: false });
  });

  it('提示级告警只出现在细条里，不产生横幅', () => {
    const snapshot = makeSnapshot({
      alerts: makeAlerts({
        hints: [
          makeAlert({ level: 'hint', source: 'transcript', since: T('21:10:00'), message: '实时转写落后了 · 约 2 分钟' }),
        ],
      }),
    });
    const view = liveView(snapshot, initialHeartbeatState(), null, null, T('21:10:30'));
    expect(view.hints).toHaveLength(1);
    expect(view.fatal).toBeNull();
  });
});
