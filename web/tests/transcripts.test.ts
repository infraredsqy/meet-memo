import { describe, expect, it } from 'vitest';

import type { SessionTranscripts } from '../src/contract';
import {
  DEFAULT_TRANSCRIPT_KIND,
  TRANSCRIPT_KIND_LABELS,
  kindViewOf,
  transcriptKindView,
} from '../src/home/transcripts';

/**
 * 稿子正文那一块纯逻辑的用例（本节点，契约 4.13）：默认落在会中稿、两份稿平级
 * （不叫"权威"）、每一份里两路都显示、缺的那一路给读得懂的缺稿话。
 * 判据全部来自服务给的原文（`text` 在不在、`reason` 是哪一档）——这一层只转手。
 */

function cell(source: string, rest: Record<string, unknown>): Record<string, unknown> {
  return { source, ...rest };
}

function transcriptsOf(live: Record<string, unknown>[], offline: Record<string, unknown>[]): SessionTranscripts {
  return {
    dir: 'C:\\会议\\250906',
    kinds: [
      { kind: 'live', sources: live as SessionTranscripts['kinds'][number]['sources'] },
      { kind: 'offline', sources: offline as SessionTranscripts['kinds'][number]['sources'] },
    ],
  };
}

const BOTH_PRESENT = transcriptsOf(
  [cell('mic', { text: '[20:01] 本地一行\n' }), cell('loopback', { text: '[20:02] 远端一行\n' })],
  [cell('mic', { text: '[00:00] **S01**: 会后本地\n' }), cell('loopback', { text: '[00:01] **S01**: 会后远端\n' })],
);

describe('稿子正文那一块', () => {
  it('默认落在会中稿（它一定有）；两份稿平级，标签不出现"权威"', () => {
    expect(DEFAULT_TRANSCRIPT_KIND).toBe('live');
    expect(TRANSCRIPT_KIND_LABELS).toEqual({ live: '会中稿', offline: '会后稿' });
    for (const label of Object.values(TRANSCRIPT_KIND_LABELS)) {
      expect(label).not.toContain('权威');
    }
  });

  it('两路正文都在场：正文逐字原样，不编缺稿话', () => {
    const view = kindViewOf(BOTH_PRESENT, 'live')!;
    expect(view.label).toBe('会中稿');
    expect(view.headline).toBeNull();
    expect(view.roads.map((road) => road.label)).toEqual(['本地', '远端']);
    expect(view.roads[0]).toMatchObject({ present: true, text: '[20:01] 本地一行\n', note: null });
    expect(view.roads[1]?.text).toBe('[20:02] 远端一行\n');
  });

  it('切换之后正文换掉：同一份应答体里另一份稿的两路照读', () => {
    const live = kindViewOf(BOTH_PRESENT, 'live')!;
    const offline = kindViewOf(BOTH_PRESENT, 'offline')!;
    expect(live.roads[0]?.text).toBe('[20:01] 本地一行\n');
    expect(offline.roads[0]?.text).toBe('[00:00] **S01**: 会后本地\n');
  });

  it('缺一路（unrecorded）：照常占位，缺稿话点名是没采这一路', () => {
    const data = transcriptsOf(
      [cell('mic', { text: '[20:01] 本地一行\n' }), cell('loopback', { reason: 'unrecorded' })],
      [cell('mic', { reason: 'absent' }), cell('loopback', { reason: 'unrecorded' })],
);
    const live = kindViewOf(data, 'live')!;
    expect(live.headline).toBeNull();
    expect(live.roads[1]).toMatchObject({
      present: false,
      text: null,
      note: '这一场没有采远端这一路，没有它的会中稿。',
    });
    const offline = kindViewOf(data, 'offline')!;
    expect(offline.roads[0]?.note).toBe('会后稿还没有本地这一路：按「生成会后精细稿」之后才有。');
    expect(offline.roads[1]?.note).toBe('这一场没有采远端这一路，没有它的会后稿。');
  });

  it('缺一路（absent）的会中稿那一档：句子不带去处（会中稿没有按钮可按）', () => {
    const data = transcriptsOf(
      [cell('mic', { text: '[20:01] 本地一行\n' }), cell('loopback', { reason: 'absent' })],
      [cell('mic', { text: '[00:00] 会后\n' }), cell('loopback', { reason: 'absent' })],
);
    const view = kindViewOf(data, 'live')!;
    expect(view.roads[1]?.note).toBe('会中稿里没有远端这一路。');
  });

  it('两路都缺：整块给一句话，两行一模一样的缺稿话不摆两遍', () => {
    const data = transcriptsOf(
      [cell('mic', { reason: 'absent' }), cell('loopback', { reason: 'absent' })],
      [cell('mic', { reason: 'absent' }), cell('loopback', { reason: 'absent' })],
);
    const offline = kindViewOf(data, 'offline')!;
    expect(offline.headline).toBe('会后稿还没有：按「生成会后精细稿」之后才有。');
    const live = kindViewOf(data, 'live')!;
    expect(live.headline).toBe('会中稿还没有：这一场没留下正文。');
    // 逐路的缺稿话照算（整块的那一句是额外的）
    expect(offline.roads).toHaveLength(2);
    expect(transcriptKindView({ kind: 'offline', sources: [] }).headline).toBe(
      '会后稿还没有：按「生成会后精细稿」之后才有。',
);
  });

  it('服务没给这一份稿（契约外）→ null，界面不编', () => {
    const data: SessionTranscripts = { dir: 'C:\\会议\\250906', kinds: [] };
    expect(kindViewOf(data, 'live')).toBeNull();
    expect(kindViewOf(data, 'offline')).toBeNull();
  });
});
