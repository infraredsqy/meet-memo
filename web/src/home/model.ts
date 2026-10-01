import type { SessionRow } from '../contract';
import type { ProbeDecision } from '../api/sessions';
import { probeDecision as probeDecisionFromKind } from '../api/sessions';

/**
 * 工作台首页（空闲态那一张）的纯逻辑：选哪一行、每一行说什么、目录判了之后走哪一档。
 *
 * 全部判据都落在**服务给的字段**上（`GET /api/sessions` 的 `recent` / `dir_exists` /
 * `kind` / `state`），这里不派生数据、不算时序 —— 与会中视图同一口径（契约章 6 第 1 条）。
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

/** 「开始录音」在这一行上能不能按；按不动时给一句为什么（`null` = 可按）。 */
export function startWhy(row: SessionRow): string | null {
  if (!row.dir_exists) return '目录已不在原地，只显示，录不了';
  if (row.state !== null) return '已有会议记录；续接归下一个节点';
  return null;
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
