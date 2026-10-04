import type { SessionRow } from '../contract';
import type { ProbeDecision } from '../api/sessions';
import { probeDecision as probeDecisionFromKind } from '../api/sessions';
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

/** 「开始 / 接着录」在这一行上能不能按；按不动时给一句为什么（`null` = 可按）。 */
export function startWhy(row: SessionRow): string | null {
  if (!row.dir_exists) return '目录已不在原地，录不了';
  return null;
}

/**
 * 顶栏那只动作钮在这一行上的名字：录过的接着录，没录过的开始录。
 *
 * 盘上有 `session.json` 的目录对着它按 start 就是续接（同一场会再录一段，
 * 服务把段账合并着写盘），按钮的名字跟着这件事走——入口直说，不绕词。
 */
export function startButtonLabel(row: SessionRow): string {
  return row.state !== null ? '接着录' : '开始录音';
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

/** 盘上段账 → 详情区那几行。没有段账（还没开过会）时交回 `null`。 */
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

/** 行状态那一格的人话（契约字段 → 文案，一处定义）。 */
export function statusLabel(row: SessionRow): string {
  if (!row.dir_exists) return '路径已不在';
  switch (row.state) {
    case 'recording':
      return '正在录';
    case 'draining':
      return '收尾中';
    case 'done':
      return '已完成';
    case 'failed':
      return '收尾失败';
    default:
      break;
  }
  if (row.kind === 'empty') return '空白工作区';
  if (row.suspected) return '疑似会议目录 · 缺元信息';
  if (row.kind === 'meeting') return '会议目录';
  return '普通目录';
}

/** 空到只剩 logo + 「新会议」的那张空态。 */
export function emptyStateLines(): { title: string; body: string } {
  return {
    title: '还没有任何一场会',
    body: '按「新会议」挑一个已有的目录——空目录当一场新会，有会议材料的当场收编。',
  };
}

export type { ProbeDecision };

/** 名目 → 三档处理（`api/sessions.ts` 的单一出处，这里只转手给视图层）。 */
export function decisionFor(kind: 'empty' | 'meeting' | 'unknown' | 'missing'): ProbeDecision {
  return probeDecisionFromKind(kind);
}
