import type {
  SessionTranscripts,
  TranscriptKindCell,
} from '../contract';
import { sourceDisplayName } from '../live/format';

/**
 * 会后区那块**稿子正文**的纯逻辑（`GET /api/sessions/{id}/transcripts`，契约 4.13）。
 *
 * 这一层只做「服务给的原文 → 界面要摆的读数」，不碰组件、不碰取数 —— 与 `home/post.ts`
 * 同一套分层。判据全部落在服务给的原文上：
 *
 * 1. **两份稿平级**（`docs/design-intent.md` 第 11 条）：标签就叫「会中稿 / 会后稿」，
 *    界面上不出现"权威"这类字样；默认落在会中稿（它一定有——会中那条链逐行落盘）。
 * 2. **每一份里两路都显示**，不做按路筛选：两路的正文上下各一块；缺的那一路照常占位，
 *    给一句读得懂的缺稿话。
 * 3. **缺稿话按 `reason` 翻**（契约章 6：句子归界面，服务只给档位）：
 *    `unrecorded`（这一场没采这一路）与 `absent`（采了但盘上没有这一份）各说各的话。
 */

/** 两份稿的取值（契约 4.13 的 `kind`）：`live` 会中稿 / `offline` 会后稿。 */
export type TranscriptKind = TranscriptKindCell['kind'];

/** 标签一处定义：两份稿**平级**，谁也不叫"权威"。 */
export const TRANSCRIPT_KIND_LABELS: Readonly<Record<TranscriptKind, string>> = {
  live: '会中稿',
  offline: '会后稿',
};

/** 一路那一块：在的给正文，缺的给那一句缺稿话（`present: false` 时 `note` 非空）。 */
export interface TranscriptRoadView {
  source: 'mic' | 'loopback';
  /** 路标签（`本地` / `远端`，与会中电平同一套叫法）。 */
  label: string;
  present: boolean;
  text: string | null;
  /** 缺稿那一句话（服务给的 `reason` 翻成人话；`present: true` 时是 `null`）。 */
  note: string | null;
}

export interface TranscriptKindView {
  kind: TranscriptKind;
  label: string;
  /** 两路都没有时给一句整块的话（`null` = 逐路各说各的）。 */
  headline: string | null;
  roads: TranscriptRoadView[];
}

/** 一路的缺稿话（`reason` → 人话；出处只此一处，组件不拼句子）。 */
export function transcriptMissingNote(kind: TranscriptKind, source: string, reason: string): string {
  const road = sourceDisplayName(source);
  const name = TRANSCRIPT_KIND_LABELS[kind];
  if (reason === 'unrecorded') {
    return `这一场没有采${road}这一路，没有它的${name}。`;
  }
  if (kind === 'offline') {
    return `会后稿还没有${road}这一路：按「生成会后精细稿」之后才有。`;
  }
  return `会中稿里没有${road}这一路。`;
}

/** 一份稿（服务给的 `kinds` 里的一项）→ 那一块的读数。 */
export function transcriptKindView(cell: TranscriptKindCell): TranscriptKindView {
  const kind = cell.kind;
  const roads = cell.sources.map((road) => {
    const present = typeof road.text === 'string';
    return {
      source: road.source,
      label: sourceDisplayName(road.source),
      present,
      text: present ? road.text ?? null : null,
      note: present ? null : transcriptMissingNote(kind, road.source, road.reason ?? 'absent'),
    };
  });
  // 两路都没有 → 一句整块的话（两行一模一样的缺稿话不摆两遍）
  const headline =
    roads.every((road) => !road.present)
      ? kind === 'offline'
        ? '会后稿还没有：按「生成会后精细稿」之后才有。'
        : '会中稿还没有：这一场没留下正文。'
      : null;
  return { kind, label: TRANSCRIPT_KIND_LABELS[kind], headline, roads };
}

/** 应答体里那一份稿的读数；服务没给这一份（契约外）时 `null`，界面不编。 */
export function kindViewOf(
  data: SessionTranscripts,
  kind: TranscriptKind,
): TranscriptKindView | null {
  const cell = data.kinds.find((item) => item.kind === kind);
  return cell === undefined ? null : transcriptKindView(cell);
}

/**
 * 默认落在哪一份：**会中稿**（它一定有——会中那条链逐行落盘）。切换是纯界面偏好：
 * 换选中那一场就回到默认，不落本地存储。
 */
export const DEFAULT_TRANSCRIPT_KIND: TranscriptKind = 'live';
