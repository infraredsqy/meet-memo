import { describe, expect, it } from 'vitest';

import { pauseBanner, pauseButtonLabel, sourceToggle } from '../src/live/pause';
import { liveView } from '../src/live/view';
import { initialHeartbeatState } from '../src/live/heartbeat';
import { makeSnapshot, makeSource, T } from './live-fixtures';

/**
 * 暂停 / 继续与会中开关某一路这两处入口（`10770b41`）。
 *
 * 判据三条：
 *
 * 1. 字就叫「暂停 / 继续」（设计意图第 8 条：不许用「休息」这类绕开的词）；
 * 2. 「暂停中」只在录音 / 告警两态出现，而且它是一条**与致命横幅同级**的横幅 ——
 *    这一层给出它的取数，画法在 `App.tsx` 的告警区；
 * 3. 逐路那颗开关的字与它按下去的方向都由**快照里那一路的 `health`** 决定
 *    （关掉的那一路服务侧落 `stopped` 这一档），界面不自己记状态。
 */

describe('pauseButtonLabel：就这两个词', () => {
  it('没暂停时是「暂停」，暂停中是「继续」', () => {
    expect(pauseButtonLabel(false)).toBe('暂停');
    expect(pauseButtonLabel(true)).toBe('继续');
  });

  it('字面上不出现「休息」这类绕开的说法', () => {
    for (const paused of [true, false]) {
      expect(pauseButtonLabel(paused)).not.toContain('休息');
    }
  });
});

describe('pauseBanner：只在录音 / 告警态给横幅', () => {
  it('录音中暂停：横幅给标题与正文', () => {
    const view = pauseBanner(true, 'recording');
    expect(view?.title).toBe('本场已暂停');
    expect(view?.body).toContain('静音');
    expect(view?.body).toContain('继续');
  });

  it('告警中暂停照样给（两件事同屏，暂停那条排在最前面）', () => {
    expect(pauseBanner(true, 'alert')?.title).toBe('本场已暂停');
  });

  it('没暂停就不给', () => {
    expect(pauseBanner(false, 'recording')).toBeNull();
  });

  it('空闲与已结束两态不给（那两档没有可暂停的东西）', () => {
    expect(pauseBanner(true, 'idle')).toBeNull();
    expect(pauseBanner(true, 'ended')).toBeNull();
  });
});

describe('sourceToggle：字与方向都照那一路的 health', () => {
  it('在采的那一路是「关掉」，按下去发 active=false', () => {
    const view = sourceToggle(makeSource('mic'), 'recording');
    expect(view).toEqual({ label: '关掉', closed: false });
  });

  it('被关掉的那一路是「打开」，按下去发 active=true', () => {
    const view = sourceToggle(makeSource('mic', { health: 'stopped' }), 'alert');
    expect(view).toEqual({ label: '打开', closed: true });
  });

  it('空闲与已结束两态不摆这颗按钮', () => {
    expect(sourceToggle(makeSource('mic'), 'idle')).toBeNull();
    expect(sourceToggle(makeSource('mic', { health: 'stopped' }), 'ended')).toBeNull();
  });
});

describe('liveView：暂停与逐路开关都从快照读', () => {
  const heartbeat = initialHeartbeatState();

  it('session.paused 为真 → paused 与那条横幅都在', () => {
    const snapshot = makeSnapshot({
      session: {
        id: '跟老王聊周会',
        dir: 'C:\\会议\\跟老王聊周会',
        started_at: T('20:45:20'),
        mode: 'both',
        elapsed_seconds: 4354,
        frozen: false,
        paused: true,
      },
    });
    const view = liveView(snapshot, heartbeat, null, null, T('20:57:31'), 40);
    expect(view.paused).toBe(true);
    expect(view.pauseLabel).toBe('继续');
    expect(view.pause?.title).toBe('本场已暂停');
    // 这一条把界面自走的那一项**真**传了进去（复审 C1 就是从这一行穿过去的：
    // 只传到第 5 个参数时 `liveElapsedSeconds` 走缺省 0，时长那一格压根没被碰过）
    expect(view.clock.value).toBe('01:12:34');
  });

  it('暂停态下界面自走的时间不参与：传 40 秒，那一格读数停在快照给的数上（C1 回归）', () => {
    // 快照给的 4354 秒（01:12:34）就是「录到的秒数」，服务侧已经扣掉暂停那几段
    const paused = makeSnapshot({
      session: {
        id: '跟老王聊周会',
        dir: 'C:\\会议\\跟老王聊周会',
        started_at: T('20:45:20'),
        mode: 'both',
        elapsed_seconds: 4354,
        frozen: false,
        paused: true,
      },
    });
    const still = liveView(paused, heartbeat, null, null, T('20:57:31'), 0);
    const walked = liveView(paused, heartbeat, null, null, T('20:57:31'), 40);
    expect(walked.clock.value).toBe(still.clock.value);
    expect(walked.clock.value).toBe('01:12:34');
    // 对照档：没暂停时同一个 40 秒照样走进读数 —— 这一格在录音途中就是要走秒
    const running = liveView(makeSnapshot(), heartbeat, null, null, T('20:57:31'), 40);
    expect(running.clock.value).toBe('01:13:14');
  });

  it('没在暂停时 false，字是「暂停」', () => {
    const view = liveView(makeSnapshot(), heartbeat, null, null, T('20:57:31'));
    expect(view.paused).toBe(false);
    expect(view.pauseLabel).toBe('暂停');
    expect(view.pause).toBeNull();
  });

  it('暂停那一格缺席（老快照）时按没暂停处理，界面不崩', () => {
    const snapshot = makeSnapshot();
    const withoutPaused = { ...snapshot, session: { ...snapshot.session!, paused: undefined } };
    const view = liveView(
      withoutPaused as unknown as typeof snapshot,
      heartbeat,
      null,
      null,
      T('20:57:31'),
    );
    expect(view.paused).toBe(false);
  });

  it('关掉的那一路：closed 为真、按钮写作「打开」，另一路照旧', () => {
    const snapshot = makeSnapshot({
      sources: [makeSource('mic', { health: 'stopped' }), makeSource('loopback')],
    });
    const view = liveView(snapshot, heartbeat, null, null, T('20:57:31'));
    expect(view.channels[0]!.closed).toBe(true);
    expect(view.channels[0]!.toggle?.label).toBe('打开');
    expect(view.channels[1]!.closed).toBe(false);
    expect(view.channels[1]!.toggle?.label).toBe('关掉');
  });
});
