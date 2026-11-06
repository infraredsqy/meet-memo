import type { SessionRow } from '../contract';
import { formatWallClock } from '../live/format';

/**
 * 工作台首页（空闲态那一张）的纯逻辑：选哪一行、每一行说什么、目录判了之后走哪一档。
 *
 * 全部判据都落在**服务给的字段**上（`GET /api/sessions` 的 `recent` / `dir_exists` /
 * `kind` / `state` / `segments`），这里不派生数据、不算时序 —— 与会中视图同一口径
 * （契约章 6 第 1 条）。
 */

/**
 * 打开这一页时选中的那一行：最近一次被打开/切换的那一场。
 *
 * `recent` 由服务从注册表给出（**不扫任何场次目录**）；它在注册表里恒有对应的一行，
 * 兜底才是第一行（两条判据都只读服务给的原文）。
 */
export function initialSelection(sessions: SessionRow[], recent: string | null): string | null {
  if (recent !== null && sessions.some((row) => row.dir === recent)) return recent;
  return sessions[0]?.dir ?? null;
}

/** 「开始 / 继续录音」在这一行上能不能按；按不动时给一句为什么（`null` = 可按）。 */
export function startWhy(row: SessionRow): string | null {
  if (!row.dir_exists) return '目录不存在，无法录音';
  return null;
}

/**
 * 那只动作钮在这一行上的名字：盘上还没有段的叫「开始录音」，已经录过的叫「继续录音」。
 *
 * 对着一个录过的目录按 start 就是同一场会再录一段（服务把段记录合并着写盘）。名字跟着
 * 这件事走，用的是录音这件事自己的说法 —— 「继续录音」直说接着往下录。
 */
export function startButtonLabel(row: SessionRow): string {
  return row.state !== null ? '继续录音' : '开始录音';
}

/**
 * 清单行末尾那一格时长：盘上各段读数的和（只数写完的段 —— 权威时长就是它们的和，
 * 设计意图第 7 条）。一段都没有时交回 `null`，那一格就不占位置。
 */
export function rowDurationLabel(row: SessionRow): string | null {
  const done = row.segments.filter((segment) => segment.duration_seconds !== null);
  if (done.length === 0) return null;
  const total = done.reduce((sum, segment) => sum + (segment.duration_seconds ?? 0), 0);
  return secondsLabel(total);
}

/** 一段的人话：段号、起止时刻、在录时长。 */
export interface SegmentLineView {
  /** 「段 1」这一格的名。 */
  label: string;
  /** 「10:02–10:19」（还在录的那段是「10:30 起」）。 */
  time: string;
  /** 「61.5 秒」；还在录的那段如实写「没写完」。 */
  seconds: string;
}

/** 详情区「录音」那格的读数：每段一行 + 权威时长的合计。 */
export interface SessionSegmentsView {
  lines: SegmentLineView[];
  /** 合计的人话（只数写完的段——权威时长是各段在录时长之和，第 7 条）。 */
  total: string;
}

function secondsLabel(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total} 秒`;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  if (hours > 0) return `${hours} 时 ${minutes} 分`;
  return rest > 0 ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`;
}

/** 盘上段记录 → 详情区那几行。没有段记录（还没开过会）时交回 `null`。 */
export function segmentsView(row: SessionRow): SessionSegmentsView | null {
  if (row.segments.length === 0) return null;
  let finished = 0;
  const lines = row.segments.map((segment) => {
    const label = `段 ${segment.index ?? '?'}`;
    const start = formatWallClock(segment.started_at);
    const time =
      segment.ended_at === null ? `${start} 起` : `${start}–${formatWallClock(segment.ended_at)}`;
    const seconds =
      segment.duration_seconds === null ? '没写完' : secondsLabel(segment.duration_seconds);
    if (segment.duration_seconds !== null) finished += segment.duration_seconds;
    return { label, time, seconds };
  });
  return { lines, total: `共 ${lines.length} 段 · ${secondsLabel(finished)}` };
}

/**
 * 行状态那一格的人话（契约字段 → 文案，一处定义；`state` 是三档口径）。
 *
 * 列表行只说**录音走到哪了**的四档（d61829f1：会后工序的进度不进这一格）——
 * 「空白工作区 / 尚未开始」的分界按名目 `kind` 判：`empty`（目录里什么都没有）读
 * 「空白工作区」，其余没开过会的（有音频没元数据的疑似目录、收编的普通目录）读
 * 「尚未开始」。颜色不是唯一通道：字由这里给，形状由 `rowStatusView()` 的 glyph 给。
 */
export function statusLabel(row: SessionRow): string {
  if (!row.dir_exists) return '目录不存在';
  switch (row.state) {
    case 'recording':
      return '录音中';
    case 'ended':
      return '录音结束';
    default:
      break;
  }
  if (row.kind === 'empty') return '空白工作区';
  return '尚未开始';
}

/** 注册表空着时右栏那张空态：一句状态 + 一句怎么开始。 */
export function emptyStateLines(): { title: string; body: string } {
  return {
    title: '当前工作区为空',
    body: '通过「添加工作区」选择目录：空目录将建立新工作区，含会议材料的目录将被收录。',
  };
}
