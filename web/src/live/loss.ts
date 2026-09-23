/**
 * 本场丢过的窗口（界面自己记的那一小份观察记录）。
 *
 * **为什么要记**：设计章 4.3 要求「已恢复」那一档的徽标写出
 * `已恢复（20:57–21:10 丢过 13 分钟）`，而契约的 `sources[*]` 九键里**没有**这个窗口
 * —— `lost_since` 只在这一路还丢着的时候有值，服务侧一恢复就把它清成 `null`、
 * 并把那条致命告警一起收掉（`meeting/state.py` 的 `restore_source()`：
 * `item.lost_since = None` / `snapshot.alerts.critical = None`）。
 * 所以这个窗口只能**由界面在还看得见它的时候记下来**。
 *
 * **这不违反「前端不造派生数据」（契约章 6 第 6 条）**：两个时刻（丢的那一刻、
 * 恢复的那一刻）都是观察到的服务事实，这里只记下它们、不下任何结论 ——
 * 丢没丢、恢没恢复，判据全在 `sources[*].health` 上。它也**不落任何本地存储**：
 * 只活在这一份内存里，刷新页面就没了（章 6 第 2 条）。刷新之后重新看到的是服务
 * 推来的现状，那份窗口不再有 —— 界面照实少说一个数字，而不是编一个。
 *
 * 换场次（`session.id` 变了）或回到空闲（没有场次）时整份清掉。
 */

import type { Snapshot } from '../contract';

export interface LossWindow {
  /** 最后一次**看到**这一路处于 `lost` 时，服务给的那个 `lost_since`。 */
  lostSince: string | null;
  /** 第一次看到它离开 `lost` 的时刻 —— 界面自己的观察时刻（ISO）。 */
  restoredAt: string | null;
  /** 这一路丢过（或正丢着）。 */
  everLost: boolean;
}

/** 观察记录：哪一场 + 两路各自的窗口。 */
export interface LossRecord {
  sessionId: string | null;
  windows: Record<'mic' | 'loopback', LossWindow>;
}

const EMPTY_WINDOW: LossWindow = { lostSince: null, restoredAt: null, everLost: false };

/** 空的一份。 */
export function emptyLossRecord(): LossRecord {
  return {
    sessionId: null,
    windows: { mic: { ...EMPTY_WINDOW }, loopback: { ...EMPTY_WINDOW } },
  };
}

/**
 * 按这一份快照更新观察记录（纯函数：给旧的 + 快照 + 「现在」，返回新的一份）。
 *
 * 三件事：
 *
 * 1. **换场次或回到空闲** → 整份清掉（上一场的窗口不该挂在新场上）；
 * 2. 某一路 `health === 'lost'` → 记下服务给的 `lost_since`；
 * 3. 某一路丢过、现在不在 `lost` 上、又还没记过恢复时刻 → 记下**现在**。
 *    只有**非 `capturing`** 才算「恢复」：刚开录的那一帧也是 `capturing`，
 *    把它当恢复会凭空给每一路安上一个「丢过」的标记。
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

/** 丢过的秒数（丢的那一刻 → 恢复的那一刻）。任一时刻缺失就是 `null`：不编数字。 */
export function lostSeconds(window: LossWindow): number | null {
  if (!window.everLost || window.lostSince === null || window.restoredAt === null) return null;
  const from = Date.parse(window.lostSince);
  const to = Date.parse(window.restoredAt);
  if (Number.isNaN(from) || Number.isNaN(to)) return null;
  const seconds = (to - from) / 1000;
  return seconds >= 0 ? seconds : null;
}
