/**
 * 四态的状态映射：顶层状态的胶囊（字 + 图形双通道）、两路健康徽标、每路健康那一行字。
 *
 * 颜色只是一条**冗余**通道，所以这里每一档都同时给出**图形类名**（`.glyph--*`）与**整行
 * 文字**，颜色交给 CSS。
 *
 * 状态全部是从服务推的快照上**读**出来的，这一层不做任何判定：某一路到底丢没丢，是服务说了算。
 */

import type { Source, TopState } from '../contract';
import { formatLostMinutes, formatWallClock } from './format';
import { lostSeconds, type LossWindow } from './loss';

export type StateGlyph =
  | 'glyph--idle'
  | 'glyph--rec'
  | 'glyph--alert'
  | 'glyph--draining'
  | 'glyph--done';

export interface StatusView {
  name: string;
  glyph: StateGlyph;
  tone: 'off' | 'on' | 'alert';
}

/** 顶层状态 → 胶囊：收尾中 / 已完成按 `ended.phase` 分成两副面孔。顶栏只说状态名 —— 哪一路在采、哪一路没了由通道卡头行的健康徽标逐路说。 */
export function statusView(
  topState: TopState,
  endedPhase: 'draining' | 'done' | null,
): StatusView {
  switch (topState) {
    case 'idle':
      return { name: '空闲', glyph: 'glyph--idle', tone: 'off' };
    case 'recording':
      return { name: '录音中', glyph: 'glyph--rec', tone: 'on' };
    case 'alert':
      return {
        name: '告警',
        glyph: 'glyph--alert',
        tone: 'alert',
      };
    case 'ended':
      return endedPhase === 'done'
        ? {
            name: '已结束 · 已完成',
            glyph: 'glyph--done',
            tone: 'off',
          }
        : {
            name: '已结束 · 收尾中',
            glyph: 'glyph--draining',
            tone: 'off',
          };
  }
}

export type HealthGlyph = 'glyph--check' | 'glyph--cross' | 'glyph--dashed' | 'glyph--idle';

export type HealthTone = 'ok' | 'ready' | 'dead' | 'restored' | 'stopped' | 'untouched' | 'fail';

export interface HealthView {
  text: string;
  glyph: HealthGlyph;
  tone: HealthTone;
  chip: string;
  /** 这一路死了：包络塌成直线、床面压斜纹、读数打叉。 */
  dead: boolean;
  /** 这一路的数据已停（心跳失联）：冻结压灰、读数退成 `—`。 */
  frozen: boolean;
  restored: boolean;
  /** 还没开始采（空闲 / 已结束）：整块退到后排。 */
  idle: boolean;
}

export function healthView(source: Source, window?: LossWindow): HealthView {
  const label = source.label;
  const since = formatWallClock(source.lost_since);
  switch (source.health) {
    case 'unprobed':
      return chip(label, '未探测', 'glyph--idle', 'untouched', { idle: true });
    case 'ready':
      return chip(label, '已就绪', 'glyph--check', 'ready', { idle: true });
    case 'probe_failed':
      return chip(label, '不可用 · 没有能打开的端点', 'glyph--cross', 'fail', { idle: true });
    case 'capturing':
      return chip(label, '采样中', 'glyph--check', 'ok', {});
    case 'lost':
      return chip(label, `信号丢失，从 ${since} 起`, 'glyph--cross', 'dead', {
        dead: true,
        frozen: true,
      });
    case 'restored':
      return chip(label, restoredText(window), 'glyph--dashed', 'restored', { restored: true });
    case 'stopped':
      return chip(label, '采集已停止', 'glyph--dashed', 'stopped', { idle: true });
    default: {
      // 契约的七取值之外：不猜，按「未探测」这一档如实显示。
      const never: never = source.health;
      void never;
      return chip(label, '未探测', 'glyph--idle', 'untouched', { idle: true });
    }
  }
}

/**
 * 「已恢复」那一档的整句。三个数各有各的来路，**缺哪个就少说哪个**，一个都不编：
 * 丢的时刻界面在还看得见 `lost_since` 的时候记下；接回来的时刻是看到它离开 `lost` 的那一刻；
 * 丢了几分钟是两个时刻之差。页面刷新之后这份观察记录没了，那时照实只说一句「丢过一段」。
 */
export function restoredText(window?: LossWindow): string {
  const from = window?.lostSince ?? null;
  const to = window?.restoredAt ?? null;
  const seconds = window === undefined ? null : lostSeconds(window);
  if (from !== null && to !== null && seconds !== null) {
    const minutes = formatLostMinutes(seconds);
    return `已恢复（${formatWallClock(from)}–${formatWallClock(to)} 丢过 ${minutes} 分钟）`;
  }
  if (from !== null) {
    return `已恢复（从 ${formatWallClock(from)} 起丢过一段）`;
  }
  return '已恢复（丢过一段）';
}

function chip(
  label: string,
  text: string,
  glyph: HealthGlyph,
  tone: HealthTone,
  flags: Partial<Pick<HealthView, 'dead' | 'frozen' | 'restored' | 'idle'>>,
): HealthView {
  return {
    text,
    glyph,
    tone,
    chip: `${label} · ${text}`,
    dead: flags.dead ?? false,
    frozen: flags.frozen ?? false,
    restored: flags.restored ?? false,
    idle: flags.idle ?? false,
  };
}
