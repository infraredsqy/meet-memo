import { describe, expect, it } from 'vitest';

import {
  clockView,
  dbfsToPercent,
  formatDbfs,
  formatElapsed,
  formatLostMinutes,
  formatNumber,
  formatWallClock,
  formatWallClockSeconds,
  MINUS,
} from '../src/live/format';

/**
 * 时长与各种格式化的口径。
 *
 * 两件反复验的事：
 *
 * 1. **减号是 U+2212**（`−35.2`），与设计稿刻度尺上的 `−20` / `−40` / `−60` 同一个字；
 *    它同时是「读数只在这里被解析成柱高」那件事的前提。
 * 2. **归档转抄一律分钟级**（门槛 10）：`formatWallClock` 只给到 `HH:MM`，
 *    带秒的那一个只留给心跳失联那一档。
 */

describe('formatNumber / formatDbfs', () => {
  it('负数用 U+2212，定长一位小数', () => {
    expect(formatNumber(-35.25)).toBe(`${MINUS}35.3`);
    expect(formatNumber(-35.24)).toBe(`${MINUS}35.2`);
    expect(formatNumber(0)).toBe('0.0');
  });

  it('读数是 `−35.2 dBFS` 这一形；没有读数退成 `—`', () => {
    expect(formatDbfs(-35.2)).toBe(`${MINUS}35.2 dBFS`);
    expect(formatDbfs(null)).toBe('—');
    expect(formatDbfs(undefined)).toBe('—');
  });

  it('减号就是 U+2212 那个字符', () => {
    expect(formatDbfs(-96).charCodeAt(0)).toBe(0x2212);
  });
});

describe('dbfsToPercent：唯一的换算（−60…0 → 0…100）', () => {
  it('端点与中线都对得上', () => {
    expect(dbfsToPercent(0)).toBeCloseTo(100, 6);
    expect(dbfsToPercent(-60)).toBeCloseTo(0, 6);
    expect(dbfsToPercent(-30)).toBeCloseTo(50, 6);
    expect(dbfsToPercent(-20)).toBeCloseTo(66.667, 3);
  });

  it('超出区间就夹住，不许出现负高度或超高的柱', () => {
    expect(dbfsToPercent(-96)).toBe(0);
    expect(dbfsToPercent(12)).toBe(100);
  });

  it('没有读数、或者不是有限数：0%', () => {
    expect(dbfsToPercent(null)).toBe(0);
    expect(dbfsToPercent(undefined)).toBe(0);
    expect(dbfsToPercent(Number.NaN)).toBe(0);
    expect(dbfsToPercent(Number.POSITIVE_INFINITY)).toBe(0);
  });
});

describe('formatElapsed：已录时长', () => {
  it('补零到 HH:MM:SS', () => {
    expect(formatElapsed(0)).toBe('00:00:00');
    expect(formatElapsed(59)).toBe('00:00:59');
    expect(formatElapsed(60)).toBe('00:01:00');
    expect(formatElapsed(4354)).toBe('01:12:34');
    expect(formatElapsed(8363)).toBe('02:19:23');
  });

  it('服务给的重复小数向下取整（别在第一秒就显示 00:00:01）', () => {
    expect(formatElapsed(0.999)).toBe('00:00:00');
    expect(formatElapsed(1.0)).toBe('00:00:01');
  });

  it('超过 24 小时继续往上数，不进位到天', () => {
    expect(formatElapsed(90061)).toBe('25:01:01');
  });

  it('负数与缺值都当 0', () => {
    expect(formatElapsed(-5)).toBe('00:00:00');
    expect(formatElapsed(null)).toBe('00:00:00');
    expect(formatElapsed(undefined)).toBe('00:00:00');
    expect(formatElapsed(Number.NaN)).toBe('00:00:00');
  });
});

describe('formatWallClock：归档转抄的分钟级时刻', () => {
  it('ISO → HH:MM', () => {
    expect(formatWallClock('2026-09-21T20:57:00+08:00')).toBe('20:57');
    expect(formatWallClock('2026-09-21T09:05:59+08:00')).toBe('09:05');
  });

  it('带秒的写法也只给到分钟', () => {
    expect(formatWallClock('2026-09-21T20:57:31+08:00')).toBe('20:57');
  });

  it('解析不了但形状在：从串上截，不猜', () => {
    expect(formatWallClock('<iso>')).toBe('—');
    expect(formatWallClock('')).toBe('—');
    expect(formatWallClock(null)).toBe('—');
    expect(formatWallClock(undefined)).toBe('—');
  });

  it('带秒的那一个只给心跳失联那一档用', () => {
    expect(formatWallClockSeconds('2026-09-21T20:57:31+08:00')).toBe('20:57:31');
    expect(formatWallClockSeconds(null)).toBe('—');
  });
});

describe('formatLostMinutes：已丢 N 分钟', () => {
  it('分钟级、四舍五入', () => {
    expect(formatLostMinutes(780)).toBe(13);
    expect(formatLostMinutes(180)).toBe(3);
  });

  it('不足一分钟也写 1 分钟，不写「已丢 0 分钟」', () => {
    expect(formatLostMinutes(0)).toBe(1);
    expect(formatLostMinutes(20)).toBe(1);
    expect(formatLostMinutes(null)).toBe(1);
  });
});

describe('clockView：时长那一格的三档', () => {
  it('录音中 / 告警：已录，秒在走', () => {
    expect(clockView('recording', 4354)).toEqual({
      label: '已录',
      value: '01:12:34',
      faint: false,
    });
    expect(clockView('alert', 4354).value).toBe('01:12:34');
    expect(clockView('alert', 4354).label).toBe('已录');
  });

  it('「秒在走」＝服务给的读数 + 界面从收到这份快照起走过的时间', () => {
    // 服务那一刻给的是 4354（01:12:34），界面又走了 2.4 秒 → 01:12:36
    expect(clockView('recording', 4354, 2.4).value).toBe('01:12:36');
    expect(clockView('recording', 4354, 0).value).toBe('01:12:34');
  });

  it('已结束：标签改成本场时长，数字取自服务冻结的那一个（本地那一秒不参与）', () => {
    expect(clockView('ended', 8363)).toEqual({
      label: '本场时长',
      value: '02:19:23',
      faint: false,
    });
    expect(clockView('ended', 8363, 5).value).toBe('02:19:23');
  });

  it('空闲：00:00:00，灰', () => {
    expect(clockView('idle', null)).toEqual({ label: '已录', value: '00:00:00', faint: true });
    // 空闲态照设计固定在 00:00:00 —— 这一场还没开始录，服务给什么数都不往上顶
    expect(clockView('idle', 12).value).toBe('00:00:00');
  });
});
