import type {
  SessionTranscripts,
  TranscriptKindCell,
} from '../contract';
import { sourceDisplayName } from '../live/format';

/**
 * 会后区那块**稿子正文**的纯逻辑。
 *
 * **两份稿平级**：标签就叫「会中稿 / 会后稿」，界面上不出现"权威"这类字样；
 * 默认落在会中稿（它一定有——会中那条链逐行落盘）。
 */

export type TranscriptKind = TranscriptKindCell['kind'];

export const TRANSCRIPT_KIND_LABELS: Readonly<Record<TranscriptKind, string>> = {
  live: '会中稿',
  offline: '会后稿',
};

export interface TranscriptRoadView {
  source: 'mic' | 'loopback';
  label: string;
  present: boolean;
  text: string | null;
  note: string | null;
}

export interface TranscriptKindView {
  kind: TranscriptKind;
  label: string;
  headline: string | null;
  roads: TranscriptRoadView[];
}

/** 缺稿话的出处只此一处，组件不拼句子。 */
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

export function kindViewOf(
  data: SessionTranscripts,
  kind: TranscriptKind,
): TranscriptKindView | null {
  const cell = data.kinds.find((item) => item.kind === kind);
  return cell === undefined ? null : transcriptKindView(cell);
}

/**
 * 默认落在**会中稿**（它一定有——会中那条链逐行落盘）。切换是纯界面偏好：
 * 换选中那一场就回到默认，不落本地存储。
 */
export const DEFAULT_TRANSCRIPT_KIND: TranscriptKind = 'live';
