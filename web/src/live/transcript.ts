/**
 * 实时转写的累积：约 200 行的窗口、识别失败缺口、心跳中断的断口。
 *
 * **只攒服务推来的行**：`transcript` 帧每次给**一行**，服务不重放、重连以快照重同步 ——
 * 中途打开的窗口只能从那一刻起显示新行，界面不请求历史行、不做本地历史副本、不落 localStorage。
 */

import type { SourceId, TranscriptData } from '../contract';
import { formatWallClock, sourceDisplayName } from './format';

/** 窗口里保留的行数。 */
export const TRANSCRIPT_WINDOW = 200;

export interface TranscriptRow {
  at: string;
  /** 哪一路说的；断口行是 `null`。 */
  source: SourceId | null;
  /** 路的显示名；断口行是 `数据已停`。 */
  route: string;
  text: string;
  gap: boolean;
  /** 这一行是心跳中断的断口（整行走缺口画法）。 */
  interlude: boolean;
  /** 时刻那一格的原文（含括号），只有断口行带它：`[20:57–20:58]` —— 断口画的是**起止区间**。 */
  timeText?: string;
}

/**
 * 新的一行。
 *
 * `at` 用**这一行到达界面的时刻**：契约的 `transcript` 帧只给 `{ts, source, text, kind}`，
 * `ts` 是转写后端给的秒数，没有墙钟 —— 到点即记是这份「临时稿」唯一能如实给出的时刻。
 */
export function transcriptRow(data: TranscriptData, atIso: string): TranscriptRow {
  return {
    at: atIso,
    source: data.line.source,
    route: sourceDisplayName(data.line.source),
    text: data.line.text,
    gap: data.line.kind === 'gap',
    interlude: false,
  };
}

/**
 * 心跳中断留下的那一格断口。
 *
 * `toIso` 给出恢复那一刻时画成起止区间（时刻格 `[20:57–20:58]`、路名格 `采集中断`）；
 * 只有 `fromIso` 时退回单时刻。`at` 取中断**开始**的那一刻。
 */
export function gapRow(fromIso: string, note: string, toIso?: string): TranscriptRow {
  const from = formatWallClock(fromIso);
  const to = toIso === undefined ? null : formatWallClock(toIso);
  const range = from !== '—' && to !== null && to !== '—' ? `[${from}–${to}]` : undefined;
  return {
    at: fromIso,
    source: null,
    route: '采集中断',
    text: note,
    gap: true,
    interlude: true,
    timeText: range,
  };
}

/**
 * 把一行并进窗口（纯函数）：新的排在最前，超过 200 行就把尾巴切掉。**不做去重** ——
 * 界面没有帧号可比对，硬做去重得给每一行编一个身份，那反而是界面自己造的东西。
 */
export function appendRow(rows: TranscriptRow[], row: TranscriptRow): TranscriptRow[] {
  const next = [row, ...rows];
  return next.length > TRANSCRIPT_WINDOW ? next.slice(0, TRANSCRIPT_WINDOW) : next;
}

export function rowTime(row: TranscriptRow): string {
  return row.timeText ?? `[${formatWallClock(row.at)}]`;
}

export interface GapPlacement {
  /** 断口排在第几行之前（0 表示排在最前面）；`rows.length` 表示排在最后。 */
  index: number;
}

/**
 * 断口该插在哪：行是从新到旧排的，断口排在**第一条比它旧的行之前**。
 *
 * 拿开始时刻当界，是因为断口那一行显示的正是开始时刻：让 `20:57` 还留在断口上面、
 * `20:52` 落到断口下面，读起来才是顺着时间走的。
 */
export function placeGap(rows: TranscriptRow[], startIso: string): GapPlacement {
  const start = Date.parse(startIso);
  if (Number.isNaN(start)) return { index: 0 };
  for (let i = 0; i < rows.length; i += 1) {
    const row = rows[i];
    if (row === undefined) continue;
    const at = Date.parse(row.at);
    if (!Number.isNaN(at) && at < start) return { index: i };
  }
  return { index: rows.length };
}

/** 把断口按时间轴插进去：`fromIso` 是中断开始那一刻，`toIso` 给了就带起止区间。 */
export function insertGap(
  rows: TranscriptRow[],
  fromIso: string,
  note: string,
  toIso?: string,
): TranscriptRow[] {
  const { index } = placeGap(rows, fromIso);
  const next = [...rows];
  next.splice(index, 0, gapRow(fromIso, note, toIso));
  return next.length > TRANSCRIPT_WINDOW ? next.slice(0, TRANSCRIPT_WINDOW) : next;
}

/** 「回到最新」要不要露脸：**用户往上翻了**就露出来，贴到底部就收掉（剩余距离比一行高还多就算翻上去了）。 */
export function showJumpToLatest(
  scrollTop: number,
  scrollHeight: number,
  clientHeight: number,
  rowHeight = 34,
): boolean {
  const remaining = scrollHeight - scrollTop - clientHeight;
  return remaining > rowHeight;
}
