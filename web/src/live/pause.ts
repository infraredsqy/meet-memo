/**
 * 「暂停中」那条横幅的取数与文案（纯函数、逐档可验）。
 *
 * 文案就叫「暂停 / 全停」这一族，字面就是这两个词。这一层不另造近义词。
 * 状态只从服务推来的快照上读：暂停看 `session.paused`。界面不派生、
 * 不推断、不自己记一份。
 */

import type { TopState } from '../contract';

export interface PauseBannerView {
  title: string;
  body: string;
}

/**
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
