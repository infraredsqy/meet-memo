/**
 * 本场丢过的窗口。
 *
 * `lost_since` 只在这一路还丢着的时候有值，服务侧一恢复就把它清成 `null`、
 * 并把那条致命告警一起收掉。所以这个窗口只能**由界面在还看得见它的时候记下来**。
 *
 * 刷新之后重新看到的是服务推来的现状，那份窗口不再有 —— 界面照实少说一个数字，
 * 而不是编一个。
 */

import type { Snapshot } from '../contract';

export interface LossWindow {
  /** 最后一次**看到**这一路处于 `lost` 时，服务给的那个 `lost_since`。 */
  lostSince: string | null;
  /** 第一次看到它离开 `lost` 的时刻 —— 界面自己的观察时刻（ISO）。 */
  restoredAt: string | null;
  everLost: boolean;
}

export interface LossRecord {
  sessionId: string | null;
  windows: Record<'mic' | 'loopback', LossWindow>;
}

const EMPTY_WINDOW: LossWindow = { lostSince: null, restoredAt: null, everLost: false };

export function emptyLossRecord(): LossRecord {
  return {
    sessionId: null,
    windows: { mic: { ...EMPTY_WINDOW }, loopback: { ...EMPTY_WINDOW } },
  };
}

/**
 * 只有**非 `capturing`** 才算「恢复」：刚开录的那一帧也是 `capturing`，
 * 把它当恢复会凭空给每一路安上一个「丢过」的标记。
 */
export function observeLosses(
  previous: LossRecord,
  snapshot: Snapshot,
  nowIso: string,
): LossRecord {
  const sessionId = snapshot.session?.id ?? null;
  const fresh = sessionId === null || sessionId !== previous.sessionId;
  const base = fresh ? emptyLossRecord().windows : previous.windows;
  const next: LossRecord = {
    sessionId,
    windows: { mic: { ...base.mic }, loopback: { ...base.loopback } },
  };

  for (const source of snapshot.sources) {
    const key: 'mic' | 'loopback' = source.id === 'mic' ? 'mic' : 'loopback';
    const item = next.windows[key];
    if (source.health === 'lost') {
      item.everLost = true;
      item.lostSince = source.lost_since ?? item.lostSince;
      item.restoredAt = null;
      continue;
    }
    if (item.everLost && item.restoredAt === null && source.health !== 'capturing') {
      item.restoredAt = nowIso;
    }
  }
  return next;
}

export function lostSeconds(window: LossWindow): number | null {
  if (!window.everLost || window.lostSince === null || window.restoredAt === null) return null;
  const from = Date.parse(window.lostSince);
  const to = Date.parse(window.restoredAt);
  if (Number.isNaN(from) || Number.isNaN(to)) return null;
  const seconds = (to - from) / 1000;
  return seconds >= 0 ? seconds : null;
}
