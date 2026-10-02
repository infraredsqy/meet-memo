/**
 * 四态的状态映射：顶层状态的胶囊（字 + 图形双通道）、两路健康徽标、每路健康那一行字。
 *
 * 「字 + 图形」是设计章 2.3 与门槛 1/2 的要求：颜色只是一条**冗余**通道，
 * 去色之后仍然要靠字形、整行文案、包络塌成直线这三条说清「哪一路死了」。
 * 所以这里每一档都同时给出**图形类名**（`.glyph--*`，`clip-path` 画的形状）
 * 与**整行文字**，颜色交给 CSS。
 *
 * 文案口径：一行只讲一件事，用仪器面板上说惯了的话。采着的时候写「采样中」，
 * 断了写「信号丢失，从 HH:MM 起」。
 *
 * 状态全部是从服务推的快照上**读**出来的（`top_state`、`sources[*].health`、
 * `restored`、`lost_since`、`ended.phase`），这一层不做任何判定：
 * 某一路到底丢没丢，是服务说了算。
 */

import type { Source, SourceHealth, TopState } from '../contract';
import { formatLostMinutes, formatWallClock } from './format';
import { lostSeconds, type LossWindow } from './loss';

/** 胶囊上的图形标记：空心方框 / 实心圆 / 实心三角 / 半实心方框 / 方框打勾。 */
export type StateGlyph =
  | 'glyph--idle'
  | 'glyph--rec'
  | 'glyph--alert'
  | 'glyph--draining'
  | 'glyph--done';

export interface StatusView {
  /** 状态名，四态固定文案。 */
  name: string;
  /** 状态说明，随状态变。 */
  note: string;
  glyph: StateGlyph;
  /** 胶囊实心块的配色档：`alert` 红、`on` 青、`off` 深墨。 */
  tone: 'off' | 'on' | 'alert';
}

/** 顶层状态 → 胶囊。收尾中 / 已完成按 `ended.phase` 分成两副面孔（设计章 4.2 二）。 */
export function statusView(
  topState: TopState,
  endedPhase: 'draining' | 'done' | null,
): StatusView {
  switch (topState) {
    case 'idle':
      return { name: '待机', note: '未开始录音', glyph: 'glyph--idle', tone: 'off' };
    case 'recording':
      return { name: '录音中', note: '两路均在采样', glyph: 'glyph--rec', tone: 'on' };
    case 'alert':
      return {
        name: '告警',
        note: '一路仍在采样，另一路信号丢失',
        glyph: 'glyph--alert',
        tone: 'alert',
      };
    case 'ended':
      return endedPhase === 'done'
        ? {
            name: '已结束 · 已完成',
            note: '可以关机；会后那两趟回工作台按按钮',
            glyph: 'glyph--done',
            tone: 'off',
          }
        : {
            name: '已结束 · 收尾中',
            note: '正在落盘，先别关机',
            glyph: 'glyph--draining',
            tone: 'off',
          };
  }
}

/** 健康徽标的图形：勾 / 叉 / 虚线环 / 空心方框。 */
export type HealthGlyph = 'glyph--check' | 'glyph--cross' | 'glyph--dashed' | 'glyph--idle';

/** 健康徽标的色档，与 `ui.css` 的 `.m-health--*` 一一对应。 */
export type HealthTone = 'ok' | 'ready' | 'dead' | 'restored' | 'stopped' | 'untouched' | 'fail';

export interface HealthView {
  /** 徽标上的字。 */
  text: string;
  glyph: HealthGlyph;
  tone: HealthTone;
  /** 每路健康那一行的整句（`本地 · 还在采` / `远端 · 采不到了，从 20:57 起`）。 */
  chip: string;
  /** 这一路死了：包络塌成直线、床面压斜纹、读数打叉。 */
  dead: boolean;
  /** 这一路的数据已停（心跳失联）：冻结压灰、读数退成 `—`。 */
  frozen: boolean;
  /** 本场丢过又接回来。 */
  restored: boolean;
  /** 还没开始采（空闲 / 已结束）：整块退到后排。 */
  idle: boolean;
}

/**
 * 一路的健康。
 *
 * 七取值逐档（`docs/contract.md` 第 2 章）：
 *
 * | health | 徽标 | 每路健康 | 通道档 |
 * |---|---|---|---|
 * | `unprobed` | 未探测 | `本地 · 未探测` | idle |
 * | `ready` | 已就绪 | `本地 · 已就绪` | idle |
 * | `probe_failed` | 不可用 · 没有能打开的端点 | 同 | idle（床面空着） |
 * | `capturing` | 采样中 | `本地 · 采样中` | 实时 |
 * | `lost` | 信号丢失，从 HH:MM 起 | 同 | dead |
 * | `restored` | 已恢复（…） | 同 | restored |
 * | `stopped` | 采集已停止 | `本地 · 采集已停止` | idle |
 */
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
 * 「已恢复」那一档的整句。
 *
 * 设计给出的样子是 `已恢复（20:57–21:10 丢过 13 分钟）`。三个数各有各的来路，
 * **缺哪个就少说哪个**，一个都不编：
 *
 * - 丢的时刻：界面在还看得见 `lost_since` 的时候记下的（见 `loss.ts`）；
 * - 接回来的时刻：界面**看到它离开 `lost`** 的那一刻；
 * - 丢了几分钟：上面两个时刻之差，只有两个都在才算得出来。
 *
 * 页面刷新之后这份观察记录没了（它不落本地存储），那时照实只说一句「丢过一段」。
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

/** 一路在不在采 —— 给「两路都在采 / 一路仍在采」这类说明与汇总用。 */
export function isCapturing(health: SourceHealth): boolean {
  return health === 'capturing' || health === 'restored';
}
