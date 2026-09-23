/**
 * 告警区的取数与文案：致命级横幅、提示级细条，以及「响不响」那一条判定。
 *
 * 三条口径（契约章 6 与设计章 4.4）：
 *
 * 1. **界面不算抑制窗口**。响不响只看两样：快照的 `settings.alarm_sound`
 *    （持久化开关）与这一条告警的 `muted`（服务侧算好的「这一条现在会不会出声」）。
 *    两个都放行才响 —— 这句话就是 `docs/contract.md` 6.8 补的那一条读法。
 * 2. **界面不判告警级别与条数**：致命级是 `alerts.critical` 那一条（服务侧同时只留
 *    一条，`null` 就是没有）；提示级是 `alerts.hints`（服务侧同类合并、上限 3），
 *    界面照单显示，不自己合并、不自己排序。
 * 3. **同一条不许重复响**：服务在首发那一帧把 `muted` 放成 `false`，此后同一条压成
 *    `true`。界面记住「上一次看到的那一版」，值与上一帧相同就不响 —— 于是天然只响一次。
 */

import type { Alert, Alerts, Settings } from '../contract';
import { formatLostMinutes, formatWallClock } from './format';

/** 提示级细条的条数上限。服务侧已经合并并限到 3，这里是照契约兜一道底。 */
export const HINT_LIMIT = 3;

/** 致命级横幅的几处文案。 */
export interface FatalBannerView {
  /** `远端这一路采不到了` —— 哪一路。`source` 是那一路的 id（`mic` / `loopback`）。 */
  where: string;
  /** `从 20:57 起`。 */
  since: string;
  /** `已丢 13 分钟`。 */
  elapsed: string;
}

/** 提示级一条：时刻 + 正文。 */
export interface HintView {
  /** `21:10`。 */
  since: string;
  text: string;
}

/** 致命级告警的六字段 → 横幅要的四处。`null` 表示现在没有致命级告警。 */
export function fatalBanner(alert: Alert | null): FatalBannerView | null {
  if (alert === null) return null;
  const name = alert.source === 'mic' ? '本地' : alert.source === 'loopback' ? '远端' : alert.source;
  return {
    where: `${name}这一路采不到了`,
    since: `从 ${formatWallClock(alert.since)} 起`,
    elapsed: `已丢 ${formatLostMinutes(alert.duration_seconds)} 分钟`,
  };
}

/**
 * 提示级细条：上限 3 条，按服务推来的次序显示。
 *
 * 正文是服务给的 `message`（界面不翻译、不拼装 —— 那句话归服务），
 * 左边那个时刻取 `since`。
 */
export function hintLines(alerts: Alerts): HintView[] {
  return alerts.hints.slice(0, HINT_LIMIT).map((hint) => ({
    since: formatWallClock(hint.since),
    text: hint.message,
  }));
}

/**
 * 「现在这一屏响不响」的判据三样。
 *
 * `identity` 是告警事件的身份：`since` 变了就是新事件（恢复后再次发生会换一个
 * `since`），`muted` 变了说明服务侧的放行状态变了。把这两样与那一路、那句话一起串成
 * 一个串，比较「与上一帧是否相同」就够了。
 */
export interface AlarmSoundState {
  /** 上一次看到的那条告警的身份；没见过就是 `null`。 */
  identity: string | null;
}

/** 会话刚开始时的初始状态。 */
export function initialAlarmSound(): AlarmSoundState {
  return { identity: null };
}

/** 一条致命告警的身份串（四样值，见交付要求第 5 条）。 */
export function alertIdentity(alert: Alert | null): string | null {
  if (alert === null) return null;
  return `${alert.source}\u0000${alert.message}\u0000${alert.since}\u0000${alert.muted}`;
}

export interface AlarmSoundDecision {
  /** 这一帧该不该响一声。 */
  play: boolean;
  /** 交给下一次调用的状态。 */
  state: AlarmSoundState;
}

/**
 * 发声判定（纯函数：给上一次的状态 + 这一帧的两样事实，返回响不响与新的状态）。
 *
 * 响的四个条件，一个都不多：
 *
 * 1. 快照的 `settings.alarm_sound` 为真（持久化开关放行）；
 * 2. 存在一条致命级告警（`alerts.critical` 不是 `null`）；
 * 3. 这一条的 `muted === false`（服务侧放行：既不在抑制窗口里，本场也没被静音）；
 * 4. **与上一次看到的那一版不同**（刚刚从「不该响」变成「该响」）。
 *
 * 第 4 条靠服务侧的 `muted` 首发语义天然成立：同一条告警的第二帧起 `muted` 变成
 * `true`，身份串跟着变，但第 3 条已经拦住了它；等到 `since` 换成新事件、或 `muted`
 * 由 `true` 回到 `false`（服务重新放行），身份串又是新的，于是再响一声。
 *
 * **它自己不判断场次**：本场静音是服务体现在 `muted` 里的事，界面不再另判一次
 * （交付要求第 5 条末句）。
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
 * 本场静音按钮上的字。
 *
 * `alerts.critical.muted` 为真就是「已经静音了」：它由两种来路造成 —— 本场静音，
 * 或这一条还在抑制窗口里。按钮只在致命横幅上出现，而横幅出现的前提就是有一条
 * 致命告警；本场静音之后服务侧把 `muted` 放成 `true`，界面据此换字。
 * 抑制窗口那一档也读同一个字段（界面分不出来、契约也不要求分），
 * 所以字面上说「本场已静音」——它是这一档里唯一会发生的那件事。
 */
export function muteButtonLabel(alert: Alert | null): string {
  return alert !== null && alert.muted ? '本场已静音' : '本次会不再出声';
}
