import type { SessionRow } from '../contract';
import { formatWallClock } from '../live/format';

/** 工作台首页的纯逻辑：选哪一行、每一行说什么、目录判了之后走哪一档。全部判据都落在**服务给的字段**上，这里不派生数据、不算时序。 */

/**
 * 打开这一页时选中的那一行：最近一次被打开/切换的那一场。
 *
 * `recent` 由服务从注册表给出（**不扫任何场次目录**）；兜底才是第一行。
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
 * 那只动作钮在这一行上的名字：盘上还没有段的叫「开始录音」，已经录过的叫「继续录音」——
 * 对着一个录过的目录按 start 就是同一场会再录一段。
 */
export function startButtonLabel(row: SessionRow): string {
  return row.state !== null ? '继续录音' : '开始录音';
}

/** 清单行末尾那一格时长：盘上各段读数的和（只数写完的段）。一段都没有时交回 `null`。 */
export function rowDurationLabel(row: SessionRow): string | null {
  const done = row.segments.filter((segment) => segment.duration_seconds !== null);
  if (done.length === 0) return null;
  const total = done.reduce((sum, segment) => sum + (segment.duration_seconds ?? 0), 0);
  return secondsLabel(total);
}

export interface SegmentLineView {
  label: string;
  /** 还在录的那段是「<起> 起」。 */
  time: string;
  /** 还在录的那段如实写「没写完」。 */
  seconds: string;
}

/** 详情区「录音」那格的读数：每段一行 + 权威时长的合计。 */
export interface SessionSegmentsView {
  lines: SegmentLineView[];
  /** 合计的人话（只数写完的段）。 */
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
 * 行状态那一格的人话（一处定义；`state` 是三档口径）。
 *
 * 列表行只说**录音走到哪了**的四档 —— 会后工序的进度不进这一格。「空白工作区 / 尚未开始」的
 * 分界按名目 `kind` 判：`empty` 读「空白工作区」，其余没开过会的读「尚未开始」。
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
