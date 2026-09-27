import { describe, expect, it } from 'vitest';

import {
  alertIdentity,
  decideAlarmSound,
  fatalBanner,
  hintLines,
  initialAlarmSound,
  muteButtonLabel,
  HINT_LIMIT,
} from '../src/live/alarm';
import { makeAlert, makeAlerts, makeSettings, T } from './live-fixtures';

/**
 * 告警区与发声判定。
 *
 * 发声这一批要逐条对上任务书「验证口径」里那四种局面：
 *
 * | 局面 | 该不该响 |
 * |---|---|
 * | (a) 新的致命级、`muted === false` | 恰好响一声 |
 * | (b) 同一条再来一帧（服务把它压成 `muted: true`） | 不再响 |
 * | (c) 致命级但 `muted === true` | 不响 |
 * | (d) `muted` 由 `true` 回到 `false`（`since` 换成新事件） | 再响一声 |
 */

describe('fatalBanner：哪一路 + 从何时起 + 已丢多久', () => {
  it('远端那一路', () => {
    expect(fatalBanner(makeAlert({ source: 'loopback', since: T('20:57:00'), duration_seconds: 780 })))
      .toEqual({ where: '远端这一路信号丢失', since: '从 20:57 起', elapsed: '已丢 13 分钟' });
  });

  it('本地那一路', () => {
    const view = fatalBanner(makeAlert({ source: 'mic', since: T('09:05:00'), duration_seconds: 180 }));
    expect(view?.where).toBe('本地这一路信号丢失');
    expect(view?.since).toBe('从 09:05 起');
    expect(view?.elapsed).toBe('已丢 3 分钟');
  });

  it('没有致命级告警时整条横幅不出现', () => {
    expect(fatalBanner(null)).toBeNull();
  });
});

describe('hintLines：提示级细条', () => {
  it('时刻 + 服务给的那句话', () => {
    const lines = hintLines(
      makeAlerts({
        hints: [
          makeAlert({
            level: 'hint',
            source: 'transcript',
            since: T('21:10:00'),
            message: '实时转写落后了 · 约 2 分钟',
          }),
        ],
      }),
    );
    expect(lines).toEqual([{ since: '21:10', text: '实时转写落后了 · 约 2 分钟' }]);
  });

  it('上限 3 条（服务侧已经合并限过一道，这里照契约兜底）', () => {
    const many = Array.from({ length: 5 }, (_, i) =>
      makeAlert({
        level: 'hint',
        source: 'transcript',
        since: T('21:1' + i + ':00'),
        message: `第 ${i} 条`,
      }),
    );
    const lines = hintLines(makeAlerts({ hints: many }));
    expect(lines).toHaveLength(HINT_LIMIT);
    expect(lines.map((l) => l.text)).toEqual(['第 0 条', '第 1 条', '第 2 条']);
  });

  it('没有提示级就是空数组', () => {
    expect(hintLines(makeAlerts())).toEqual([]);
  });
});

describe('decideAlarmSound：四种局面', () => {
  const firstFrame = makeAlerts({ critical: makeAlert({ muted: false, since: T('20:57:00') }) });
  const secondFrame = makeAlerts({ critical: makeAlert({ muted: true, since: T('20:57:00') }) });
  const mutedFrame = makeAlerts({ critical: makeAlert({ muted: true, since: T('20:57:00') }) });
  const reApproved = makeAlerts({ critical: makeAlert({ muted: false, since: T('21:10:00') }) });

  it('(a) 新的致命级、muted 为假：恰好响一声', () => {
    const first = decideAlarmSound(initialAlarmSound(), firstFrame, makeSettings());
    expect(first.play).toBe(true);
  });

  it('(b) 同一条再来一帧（服务把它压成 muted: true）：不再响', () => {
    const state = decideAlarmSound(initialAlarmSound(), firstFrame, makeSettings()).state;
    const again = decideAlarmSound(state, secondFrame, makeSettings());
    expect(again.play).toBe(false);
    // 就算服务那一帧仍然是 muted: false，同一条的身份没变也不响
    const stillOpen = decideAlarmSound(state, firstFrame, makeSettings());
    expect(stillOpen.play).toBe(false);
  });

  it('(c) 致命级但 muted 为真：不响', () => {
    expect(decideAlarmSound(initialAlarmSound(), mutedFrame, makeSettings()).play).toBe(false);
  });

  it('(d) muted 由真回到假（since 换成新事件）：再响一声', () => {
    const afterFirst = decideAlarmSound(initialAlarmSound(), mutedFrame, makeSettings()).state;
    const replayed = decideAlarmSound(afterFirst, reApproved, makeSettings());
    expect(replayed.play).toBe(true);
  });

  it('持久化开关关掉时一律不响 —— 这就是「两个都放行才响」那一半', () => {
    const off = makeSettings({ alarm_sound: false });
    expect(decideAlarmSound(initialAlarmSound(), firstFrame, off).play).toBe(false);
    // 它照样把这一版记下来：开关再打开时，同一条不会被当成新的
    const state = decideAlarmSound(initialAlarmSound(), firstFrame, off).state;
    expect(decideAlarmSound(state, firstFrame, makeSettings()).play).toBe(false);
  });

  it('没有致命级告警时不响，也不把上一次的身份抹掉', () => {
    const state = decideAlarmSound(initialAlarmSound(), firstFrame, makeSettings()).state;
    const quiet = decideAlarmSound(state, makeAlerts(), makeSettings());
    expect(quiet.play).toBe(false);
    expect(quiet.state.identity).toBe(state.identity);
  });

  it('一条完整的过程：首发响、压住不响、恢复后新事件再响', () => {
    let state = initialAlarmSound();
    const frames = [firstFrame, secondFrame, secondFrame, makeAlerts(), reApproved, secondFrame];
    const played = frames.map((frame) => {
      const decision = decideAlarmSound(state, frame, makeSettings());
      state = decision.state;
      return decision.play;
    });
    expect(played).toEqual([true, false, false, false, true, false]);
  });
});

describe('alertIdentity', () => {
  it('四样值都进身份：哪一路、那句话、since、muted', () => {
    const alert = makeAlert();
    expect(alertIdentity(alert)).toBe(
      `loopback\u0000${alert.message}\u0000${alert.since}\u0000false`,
    );
    expect(alertIdentity(null)).toBeNull();
  });
});

describe('muteButtonLabel', () => {
  it('没静音与静音两档给出不同的措辞，没有告警时按未静音那一档', () => {
    const muted = muteButtonLabel(makeAlert({ muted: true }));
    const sounding = muteButtonLabel(makeAlert({ muted: false }));
    expect(muted).not.toBe(sounding);
    expect(muteButtonLabel(null)).toBe(sounding);
  });
});
