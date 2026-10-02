/**
 * 暂停与「会中开关某一路」两处入口的取数与文案（照 `alarm.ts` 的形状写：纯函数、逐档可验）。
 *
 * 三条口径：
 *
 * 1. **文案就叫「暂停 / 继续」**，字面就是这两个词（`docs/design-intent.md` 第 8 条：
 *    「休息」这类词是在绕，越绕越像偷）。这一层不另造近义词。
 * 2. **状态只从服务推来的快照上读**：暂停看 `session.paused`，某一路关没关看那一路的
 *    `health`（关掉的那一路如实落进现成的 `stopped` 档，服务侧不新增取值）。
 *    界面不派生、不推断、不自己记一份（契约章 6 第 1 条）。
 * 3. **「暂停中」与「哪一路死了」一个级别**：横幅的取数在这里给，画法交给 `App.tsx`
 *    的告警区那一块（与致命横幅同一档的 `.alert--paused`）。
 */

import type { Source, TopState } from '../contract';

/** 暂停 / 继续那颗按钮上的字。 */
export function pauseButtonLabel(paused: boolean): string {
  return paused ? '继续' : '暂停';
}

/** 暂停横幅的两句话（画在告警区里，与致命横幅同一档）。 */
export interface PauseBannerView {
  /** `本场已暂停`。 */
  title: string;
  /** 这一段时间是什么样、按哪里接着录。 */
  body: string;
}

/**
 * 「暂停中」那条横幅；不在这一档时给 `null`。
 *
 * 只在 `recording` / `alert` 两态出现 —— `paused` 说的就是「正在录的这一场此刻停着」，
 * 空闲与已结束两态没有可暂停的东西（收尾一开始服务就把这一格复位）。
 */
export function pauseBanner(paused: boolean, topState: TopState): PauseBannerView | null {
  if (!paused) return null;
  if (topState !== 'recording' && topState !== 'alert') return null;
  return {
    title: '本场已暂停',
    body: '暂停的这截不录内容，音频里写的是静音，转写也不出字。按「继续」接着录，还是同一场、同一段。',
  };
}

/** 一条通道上那颗开关按钮的样子（`null` = 这一档不摆这颗按钮）。 */
export interface SourceToggleView {
  /** `关掉` / `打开`。 */
  label: string;
  /** 这一路现在是不是已经被关掉（决定按下去发的是 `active: true` 还是 `false`）。 */
  closed: boolean;
}

/**
 * 某一路那颗开关按钮；不在录音 / 告警态时给 `null`。
 *
 * 判据是**那一路的 `health`**：`stopped` = 已经被关掉（服务侧如实落进这一档），
 * 于是按钮的字是「打开」、按下去发 `active: true`；其余各档按钮写作「关掉」。
 * 按钮与 `mode` 的关系由服务判：本场没采的那一路会被服务如实拒掉（`invalid_setting`）。
 */
export function sourceToggle(source: Source, topState: TopState): SourceToggleView | null {
  if (topState !== 'recording' && topState !== 'alert') return null;
  const closed = source.health === 'stopped';
  return { label: closed ? '打开' : '关掉', closed };
}
