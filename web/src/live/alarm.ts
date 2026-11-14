/**
 * 告警区的取数与文案：致命级横幅、提示级细条，以及「响不响」那一条判定。
 *
 * 1. **界面不算抑制窗口**：响不响只看 `settings.alarm_sound`（持久化开关）与这一条告警的
 *    `muted`（服务侧算好的「这一条现在会不会出声」），两个都放行才响。
 * 2. **界面不判告警级别与条数**：照单显示，不自己合并、不自己排序。
 * 3. **同一条不许重复响**：记住「上一次看到的那一版」，值与上一帧相同就不响。
 */

import type { Alert, Alerts, Settings } from '../contract';
import { formatLostMinutes, formatWallClock, sourceDisplayName } from './format';

/** 提示级细条的条数上限（服务侧已经合并并限到 3，这里兜一道底）。 */
export const HINT_LIMIT = 3;

export interface FatalBannerView {
  /** 哪一路：`远端这一路信号丢失`。 */
  where: string;
  since: string;
  elapsed: string;
}

export interface HintView {
  since: string;
  text: string;
}

export function fatalBanner(alert: Alert | null): FatalBannerView | null {
  if (alert === null) return null;
  const name = sourceDisplayName(alert.source);
  return {
    where: `${name}这一路信号丢失`,
    since: `从 ${formatWallClock(alert.since)} 起`,
    elapsed: `已丢 ${formatLostMinutes(alert.duration_seconds)} 分钟`,
  };
}

/** 正文是服务给的 `message`（界面不翻译、不拼装），左边那个时刻取 `since`。 */
export function hintLines(alerts: Alerts): HintView[] {
  return alerts.hints.slice(0, HINT_LIMIT).map((hint) => ({
    since: formatWallClock(hint.since),
    text: hint.message,
  }));
}

export interface AlarmSoundState {
  identity: string | null;
}

export function initialAlarmSound(): AlarmSoundState {
  return { identity: null };
}

/** 一条致命告警的身份串（四样值）。 */
export function alertIdentity(alert: Alert | null): string | null {
  if (alert === null) return null;
  return `${alert.source}\u0000${alert.message}\u0000${alert.since}\u0000${alert.muted}`;
}

export interface AlarmSoundDecision {
  play: boolean;
  state: AlarmSoundState;
}

/**
 * 发声判定（纯函数：给上一次的状态 + 这一帧的两样事实，返回响不响与新的状态）。
 *
 * **它自己不判断场次**：本场静音是服务体现在 `muted` 里的事，界面不再另判一次。
 */
export function decideAlarmSound(
  state: AlarmSoundState,
  alerts: Alerts,
  settings: Settings,
): AlarmSoundDecision {
  const identity = alertIdentity(alerts.critical);
  const shouldPlay =
    settings.alarm_sound && alerts.critical !== null && alerts.critical.muted === false;

  if (!shouldPlay || identity === null || identity === state.identity) {
    // 不该响、或与上一次看到的是同一版：**记住这一版**然后什么都不做。
    // 记住很重要 —— 否则「同一条的第二帧」每次都会被当成新的。
    return { play: false, state: { identity: identity ?? state.identity } };
  }
  return { play: true, state: { identity } };
}

/**
 * 本场静音按钮上的字：`alerts.critical.muted` 为真就是「已经静音了」，它由两种来路造成
 * —— 本场静音，或这一条还在抑制窗口里（界面分不出来、契约也不要求分）。
 */
export function muteButtonLabel(alert: Alert | null): string {
  return alert !== null && alert.muted ? '本场已静音' : '本场告警提示音';
}
