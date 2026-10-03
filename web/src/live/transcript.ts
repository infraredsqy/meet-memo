/**
 * 实时转写的累积：约 200 行的窗口、识别失败缺口、心跳中断的断口。
 *
 * 几条口径：
 *
 * - **只攒服务推来的行**。`transcript` 帧每次给**一行**（`{revision, line}`，
 *   契约 3.3），服务不重放、重连以快照重同步 —— 所以中途打开的窗口只能从那一刻起
 *   显示新行（契约章 7「转写历史回看」那一格：**现状是拿不到**）。界面不请求历史行、
 *   不做本地历史副本、不落 localStorage（任务书「边界」第 3 条）。
 * - **约 200 行窗口**，新行在最上面（设计章 2.3：向上滚动可看更早，
 *   滚动时不自动跳回底部，底部给一个「回到最新」的钮）。
 * - **识别失败的缺口**照服务给的 `kind === 'gap'` 画成缺口样式，不让它看起来像正常文本。
 * - **断口**是心跳失联留下的那一格（设计章 4.5：转写区在中断那一格留一条
 *   `[20:57–20:58] 数据已停` 的断口）。它由界面自己插，插在**它发生的那一刻**在时间轴上
 *   的位置 —— 也就是紧挨着它前后那两行之间（新行在上，所以断口排在「比它晚的行」下面）。
 */

import type { SourceId, TranscriptData } from '../contract';
import { formatWallClock, sourceDisplayName } from './format';

/** 窗口里保留的行数（设计章 2.3：「窗口内保留最近约 200 行」）。 */
export const TRANSCRIPT_WINDOW = 200;

export interface TranscriptRow {
  /** 这一行到达界面的墙钟（ISO，本地时区）。 */
  at: string;
  /** 哪一路说的：`mic` / `loopback`；断口行是 `null`。 */
  source: SourceId | null;
  /** 路的显示名（`本地` / `远端`）；断口行是 `数据已停`。 */
  route: string;
  /** 正文。 */
  text: string;
  /** 正文这一格是缺口（识别失败）还是正常文本。 */
  gap: boolean;
  /** 这一行是心跳中断的断口（它整行走缺口画法）。 */
  interlude: boolean;
  /**
   * 时刻那一格的原文（含括号）。只有断口行带它：`[20:57–20:58]` —— 设计章 4.5 的
   * 断口画的是**起止区间**，与普通行的 `[HH:MM]` 不同；缺省（普通行）按 `at` 取分钟。
   */
  timeText?: string;
}

/**
 * 新的一行。
 *
 * `at` 用**这一行到达界面的时刻**：契约的 `transcript` 帧只给 `{ts, source, text, kind}`，
 * `ts` 是转写后端给的秒数（语义归后端、服务只透传），没有墙钟。到点即记是本机读数的
 * 精度，也是这份「临时稿」唯一能如实给出的时刻（设计章 8.10 允许秒级的三处里没有它，
 * 所以显示时取到分钟级）。
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
 * `toIso` 给出恢复那一刻时，这一行就是设计章 4.5 的形状（与 `heartbeat.ts` 的
 * `transcriptGap()` 同一口径）：时刻格 `[20:57–20:58]`、路名格 `采集中断` ——
 * 起于中断开始、止于恢复。只有 `fromIso`（恢复时刻不可知）时退回单时刻。
 * `at` 取中断**开始**的那一刻：断口标的是它发生的时间。
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
 * 把一行并进窗口（纯函数）。
 *
 * 新的排在最前，超过 200 行就把尾巴切掉。**同一行推两遍会被去重吗？** 不会 ——
 * 服务每出一行推一帧，界面没有帧号可比对，硬做去重得给每一行编一个身份，
 * 那反而是界面自己造的东西。同一行重复到达属于服务侧的毛病，界面照实显示。
 */
export function appendRow(rows: TranscriptRow[], row: TranscriptRow): TranscriptRow[] {
  const next = [row, ...rows];
  return next.length > TRANSCRIPT_WINDOW ? next.slice(0, TRANSCRIPT_WINDOW) : next;
}

/** 窗口里那一行的时刻：普通行 `[HH:MM]`；断口行带起止区间（`gapRow` 摆好的）。 */
export function rowTime(row: TranscriptRow): string {
  return row.timeText ?? `[${formatWallClock(row.at)}]`;
}

/** 一次心跳中断之后，要把断口插在时间轴上的哪一格。 */
export interface GapPlacement {
  /** 断口排在第几行之前（0 表示排在最前面）；`rows.length` 表示排在最后。 */
  index: number;
}

/**
 * 断口该插在哪。
 *
 * 行是从新到旧排的（`rows[0]` 最新）。断口标的是中断**开始**的那一刻 `startIso`，
 * 所以它排在**第一条比它旧的行之前** —— 也就是「最新的那几条不早于它的行」之后。
 *
 * 拿开始时刻当界，是因为断口那一行显示的正是开始时刻（`[20:57]`）：
 * 让 `20:57` 还留在断口上面、`20:52` 落到断口下面，读起来才是顺着时间走的。
 * 窗口里每一行都比断口新时，它排在最旧那一格；每一行都比断口旧时，它排在最前。
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

/**
 * 把断口按时间轴插进去。
 *
 * `fromIso` 是中断开始的那一刻（断口那一行标的就是它）；`toIso` 是恢复那一刻，
 * 给了它断口就带起止区间（`[20:57–20:58] 数据已停`，设计章 4.5 的形状）。
 */
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

/**
 * 「回到最新」要不要露脸。
 *
 * 判据只有一个：**用户往上翻了**（滚动位置离底部还有一截）就露出来，
 * 贴到底部就收掉。滚到底这件事用 `scrollHeight - scrollTop - clientHeight`
 * 这个剩余距离判，剩得比一行高还多就算翻上去了。
 */
export function showJumpToLatest(
  scrollTop: number,
  scrollHeight: number,
  clientHeight: number,
  rowHeight = 34,
): boolean {
  const remaining = scrollHeight - scrollTop - clientHeight;
  return remaining > rowHeight;
}
