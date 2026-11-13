import { describe, expect, it } from 'vitest';

import { pauseBanner } from '../src/live/pause';
import { liveView } from '../src/live/view';
import { initialHeartbeatState } from '../src/live/heartbeat';
import { makeSnapshot, makeSource, T } from './live-fixtures';

/**
 * 「暂停中」那条横幅与会中收音四组合的派生（`` 起，`` 收编）。
 *
 * 判据三条：
 *
 * 1. 文案就叫「暂停 / 全停」这一族（设计意图第 8 条：不许用「休息」这类绕开的词）；
 * 2. 「暂停中」只在录音 / 告警两态出现，而且它是一条**与致命横幅同级**的横幅 ——
 *    这一层给出它的取数，画法在 `App.tsx` 的告警区；
 * 3. 此刻处在四个收音组合中的哪一档，从**快照**派生（暂停一格 + 两路的
 *    `stopped`），界面不自己记状态；旧的两颗入口按钮（顶栏暂停、电平卡上的
 *    「关掉 / 打开」）已由四组合控件收编，取数不再存在。
 */

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

describe('liveView：暂停、组合与逐路关闭都从快照读', () => {
  const heartbeat = initialHeartbeatState();

  it('session.paused 为真 → paused、组合「全停」与那条横幅都在', () => {
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
    expect(view.combo).toBe('paused');
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

  it('没在暂停时 false、组合「两路都录」，横幅不出现', () => {
    const view = liveView(makeSnapshot(), heartbeat, null, null, T('20:57:31'));
    expect(view.paused).toBe(false);
    expect(view.combo).toBe('both');
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
    expect(view.combo).toBe('both');
  });

  it('关掉的那一路：closed 为真、组合跟着落到「仅本地 / 仅远端」，另一路照旧', () => {
    const localOnly = makeSnapshot({
      sources: [makeSource('mic'), makeSource('loopback', { health: 'stopped' })],
    });
    const local = liveView(localOnly, heartbeat, null, null, T('20:57:31'));
    expect(local.channels[0]!.closed).toBe(false);
    expect(local.channels[1]!.closed).toBe(true);
    expect(local.combo).toBe('local');

    const remoteOnly = makeSnapshot({
      sources: [makeSource('mic', { health: 'stopped' }), makeSource('loopback')],
    });
    expect(liveView(remoteOnly, heartbeat, null, null, T('20:57:31')).combo).toBe('remote');
  });
});
