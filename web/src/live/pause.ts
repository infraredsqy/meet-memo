/**
 * 「暂停中」那条横幅的取数与文案（照 `alarm.ts` 的形状写：纯函数、逐档可验）。
 *
 * 暂停与逐路开关两颗旧按钮的取数（`pauseButtonLabel` / `sourceToggle`）随
 * `c9f5d04d` 退场：会中收音收成顶栏一处四组合直切（`live/combo.ts`），这一层
 * 只留下横幅 —— 「暂停中」与「哪一路死了」一个级别，画法交给 `App.tsx`
 * 的告警区那一块（与致命横幅同一档的 `.alert--paused`）。
 *
 * 两条口径：
 *
 * 1. **文案就叫「暂停 / 全停」这一族**，字面就是这两个词（`docs/design-intent.md`
 *    第 8 条：「休息」这类词是在绕，越绕越像偷）。这一层不另造近义词。
 * 2. **状态只从服务推来的快照上读**：暂停看 `session.paused`。界面不派生、
 *    不推断、不自己记一份（契约章 6 第 1 条）。
 */

import type { TopState } from '../contract';

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
