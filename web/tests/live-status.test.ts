import { describe, expect, it } from 'vitest';

import { healthView, restoredText, statusView } from '../src/live/status';
import { emptyLossRecord, lostSeconds, observeLosses } from '../src/live/loss';
import { makeSnapshot, makeSource, T } from './live-fixtures';

/**
 * 四态胶囊与两路健康。
 *
 * 这一批的**主判据是「不靠颜色」**（门槛 1/2）：每一档都同时验两样 ——
 * 图形类名（`clip-path` 画的形状）与整行文字。颜色在用例里一个字都不出现。
 */

describe('statusView：四态各自的字 + 图形', () => {
  it('空闲', () => {
    expect(statusView('idle', null)).toEqual({
      name: '空闲',
      glyph: 'glyph--idle',
      tone: 'off',
    });
  });

  it('录音中', () => {
    expect(statusView('recording', null)).toEqual({
      name: '录音中',
      glyph: 'glyph--rec',
      tone: 'on',
    });
  });

  it('告警', () => {
    const view = statusView('alert', null);
    expect(view.name).toBe('告警');
    expect(view.glyph).toBe('glyph--alert');
    expect(view.tone).toBe('alert');
  });

  it('已结束有两副面孔：收尾中与已完成（ui-design 4.2 二的两档）', () => {
    expect(statusView('ended', 'draining')).toEqual({
      name: '已结束 · 收尾中',
      glyph: 'glyph--draining',
      tone: 'off',
    });
    expect(statusView('ended', 'done')).toEqual({
      name: '已结束 · 已完成',
      glyph: 'glyph--done',
      tone: 'off',
    });
  });

  it('已结束但 phase 缺值：按收尾中那一档说（宁可让人先别关机）', () => {
    expect(statusView('ended', null).name).toBe('已结束 · 收尾中');
  });
});

describe('healthView：七取值逐档', () => {
  it('unprobed / ready / probe_failed 三档（空闲态的设备探测）', () => {
    expect(healthView(makeSource('mic', { health: 'unprobed' }))).toMatchObject({
      text: '未探测',
      glyph: 'glyph--idle',
      tone: 'untouched',
      chip: '本地 · 未探测',
      idle: true,
    });
    expect(healthView(makeSource('mic', { health: 'ready' }))).toMatchObject({
      text: '已就绪',
      glyph: 'glyph--check',
      tone: 'ready',
      idle: true,
    });
    expect(healthView(makeSource('loopback', { health: 'probe_failed' }))).toMatchObject({
      text: '不可用 · 没有能打开的端点',
      glyph: 'glyph--cross',
      tone: 'fail',
      chip: '远端 · 不可用 · 没有能打开的端点',
    });
  });

  it('capturing：采样中', () => {
    expect(healthView(makeSource('mic'))).toMatchObject({
      text: '采样中',
      glyph: 'glyph--check',
      tone: 'ok',
      chip: '本地 · 采样中',
      dead: false,
      frozen: false,
    });
  });

  it('lost：整行写出从何时起，并且标成「死了」', () => {
    const view = healthView(
      makeSource('loopback', { health: 'lost', lost_since: T('20:57:00') }),
    );
    expect(view.text).toBe('信号丢失，从 20:57 起');
    expect(view.chip).toBe('远端 · 信号丢失，从 20:57 起');
    expect(view.glyph).toBe('glyph--cross');
    expect(view.tone).toBe('dead');
    expect(view.dead).toBe(true);
    expect(view.frozen).toBe(true);
  });

  it('stopped：采集已停止（已结束那一档）', () => {
    expect(healthView(makeSource('mic', { health: 'stopped' }))).toMatchObject({
      text: '采集已停止',
      glyph: 'glyph--dashed',
      tone: 'stopped',
      idle: true,
    });
  });

  it('restored：本场丢过又接回来，数值与文字都来自观察到的两个时刻', () => {
    const view = healthView(makeSource('loopback', { health: 'restored' }), {
      everLost: true,
      lostSince: T('20:57:00'),
      restoredAt: T('21:10:00'),
    });
    expect(view.text).toBe('已恢复（20:57–21:10 丢过 13 分钟）');
    expect(view.glyph).toBe('glyph--dashed');
    expect(view.tone).toBe('restored');
    expect(view.restored).toBe(true);
    expect(view.dead).toBe(false);
  });
});

describe('restoredText：缺哪个数就少说哪个，一个都不编', () => {
  it('三个数齐：完整的括号', () => {
    expect(
      restoredText({ everLost: true, lostSince: T('20:57:00'), restoredAt: T('21:10:00') }),
    ).toBe('已恢复（20:57–21:10 丢过 13 分钟）');
  });

  it('只记得丢的那一刻（刷新过、恢复时刻没看着）', () => {
    expect(restoredText({ everLost: true, lostSince: T('20:57:00'), restoredAt: null })).toBe(
      '已恢复（从 20:57 起丢过一段）',
    );
  });

  it('什么都没记下（新开的窗口直接看到「已恢复」）', () => {
    expect(restoredText(undefined)).toBe('已恢复（丢过一段）');
  });
});

describe('lostSeconds', () => {
  it('两个时刻之差', () => {
    expect(
      lostSeconds({ everLost: true, lostSince: T('20:57:00'), restoredAt: T('21:10:23') }),
    ).toBe(803);
  });

  it('缺一个时刻、或没丢过：null', () => {
    expect(lostSeconds({ everLost: true, lostSince: T('20:57:00'), restoredAt: null })).toBeNull();
    expect(lostSeconds({ everLost: false, lostSince: null, restoredAt: null })).toBeNull();
  });
});

describe('observeLosses：界面记下的那一份丢过的窗口', () => {
  const startingLoss = makeSnapshot({
    top_state: 'alert',
    sources: [
      makeSource('mic'),
      makeSource('loopback', { health: 'lost', lost_since: T('20:57:00') }),
    ],
  });

  it('看到 lost 就记下服务给的那一刻', () => {
    const next = observeLosses(emptyLossRecord(), startingLoss, T('20:58:00'));
    expect(next.sessionId).toBe('跟老王聊周会');
    expect(next.windows.loopback).toEqual({
      everLost: true,
      lostSince: T('20:57:00'),
      restoredAt: null,
    });
    expect(next.windows.mic.everLost).toBe(false);
  });

  it('看到它离开 lost 就记下恢复的那一刻，于是算得出中断时长', () => {
    const lost = observeLosses(emptyLossRecord(), startingLoss, T('20:58:00'));
    const recovered = makeSnapshot({
      sources: [
        makeSource('mic'),
        makeSource('loopback', { health: 'restored', restored: true }),
      ],
    });
    const next = observeLosses(lost, recovered, T('21:10:23'));
    expect(next.windows.loopback.lostSince).toBe(T('20:57:00'));
    expect(next.windows.loopback.restoredAt).toBe(T('21:10:23'));
    expect(lostSeconds(next.windows.loopback)).toBe(803);
  });

  it('刚开录那一帧的 capturing 不算「恢复」（别给每一路安上一个丢过的标记）', () => {
    const next = observeLosses(emptyLossRecord(), makeSnapshot(), T('20:45:21'));
    expect(next.windows.mic).toEqual({ everLost: false, lostSince: null, restoredAt: null });
    expect(next.windows.loopback.everLost).toBe(false);
  });

  it('换场次：上一场的窗口整份清掉', () => {
    const lost = observeLosses(emptyLossRecord(), startingLoss, T('20:58:00'));
    const nextSession = makeSnapshot({ session: { ...makeSnapshot().session!, id: '260922' } });
    const next = observeLosses(lost, nextSession, T('22:00:00'));
    expect(next.sessionId).toBe('260922');
    expect(next.windows.loopback).toEqual({ everLost: false, lostSince: null, restoredAt: null });
  });

  it('回到空闲（没有场次）：也整份清掉', () => {
    const lost = observeLosses(emptyLossRecord(), startingLoss, T('20:58:00'));
    const idle = makeSnapshot({ top_state: 'idle', session: null });
    const next = observeLosses(lost, idle, T('23:00:00'));
    expect(next.sessionId).toBeNull();
    expect(next.windows.loopback.everLost).toBe(false);
  });
});
