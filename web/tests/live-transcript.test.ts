import { describe, expect, it } from 'vitest';

import {
  appendRow,
  gapRow,
  insertGap,
  placeGap,
  rowTime,
  showJumpToLatest,
  transcriptRow,
  TRANSCRIPT_WINDOW,
  type TranscriptRow,
} from '../src/live/transcript';
import { T } from './live-fixtures';

/**
 * 实时转写的累积。
 *
 * 三件在用例里反复验的事：**约 200 行的窗口**、**识别失败的缺口照服务给的画**、
 * **心跳中断的断口插在时间轴对得上的那一格**。
 */

function data(text: string, source: 'mic' | 'loopback' = 'mic', kind: 'line' | 'gap' = 'line') {
  return { revision: 7, line: { ts: 0, source, text, kind } };
}

describe('transcriptRow：一行长什么样', () => {
  it('路名、正文、时刻', () => {
    const row = transcriptRow(data('那我们把这一段先记下来', 'loopback'), T('20:52:00'));
    expect(row).toEqual({
      at: T('20:52:00'),
      source: 'loopback',
      route: '远端',
      text: '那我们把这一段先记下来',
      gap: false,
      interlude: false,
    });
  });

  it('本地那一路', () => {
    expect(transcriptRow(data('会上先定两件事'), T('20:55:00')).route).toBe('本地');
  });

  it('识别失败：服务给的 kind 是 gap，界面照它画缺口', () => {
    expect(transcriptRow(data('[[识别失败: 超时 20s]]', 'mic', 'gap'), T('20:54:00')).gap).toBe(true);
  });

  it('`[HH:MM]` 是分钟级（门槛 10：归档转抄不出现秒）', () => {
    const row = transcriptRow(data('一行'), T('20:57:31'));
    expect(rowTime(row)).toBe('[20:57]');
  });
});

describe('appendRow：新行在最上面，窗口约 200 行', () => {
  it('新的排在最前', () => {
    let rows: TranscriptRow[] = [];
    rows = appendRow(rows, transcriptRow(data('第一句'), T('20:55:00')));
    rows = appendRow(rows, transcriptRow(data('第二句'), T('20:56:00')));
    expect(rows.map((r) => r.text)).toEqual(['第二句', '第一句']);
  });

  it('攒到 200 行就不再长', () => {
    let rows: TranscriptRow[] = [];
    for (let i = 0; i < 260; i += 1) {
      rows = appendRow(rows, transcriptRow(data(`第 ${i} 句`), T('20:00:00')));
    }
    expect(rows).toHaveLength(TRANSCRIPT_WINDOW);
    // 留的是最近的那一批：最新的在最前
    expect(rows[0]!.text).toBe('第 259 句');
    expect(rows[TRANSCRIPT_WINDOW - 1]!.text).toBe('第 60 句');
  });
});

describe('断口：插在时间轴对得上的那一格', () => {
  it('老行在下，断口排在它自己的时刻那一格', () => {
    const rows: TranscriptRow[] = [
      transcriptRow(data('21:10 那一句'), T('21:10:00')),
      transcriptRow(data('20:57 那一句'), T('20:57:00')),
      transcriptRow(data('20:52 那一句'), T('20:52:00')),
    ];
    const withGap = insertGap(rows, T('20:57:31'), '（这一段的转写收不上来。）');
    // 新行在上、旧行在下：断口标的是中断**开始**的 20:57:31，所以 20:57:00 与
    // 20:52:00 这两条（都发生在中断之前）都落在断口下面
    expect(withGap.map((r) => r.text)).toEqual([
      '21:10 那一句',
      '（这一段的转写收不上来。）',
      '20:57 那一句',
      '20:52 那一句',
    ]);
    expect(withGap[1]!.interlude).toBe(true);
    expect(withGap[1]!.route).toBe('采集中断');
  });

  it('比窗口里所有行都新：排在最前面', () => {
    const rows = [transcriptRow(data('20:52 那一句'), T('20:52:00'))];
    const withGap = insertGap(rows, T('20:57:31'), 'note');
    expect(withGap[0]!.interlude).toBe(true);
  });

  it('窗口里每一行都比断口新：排在最后（最旧那一格）', () => {
    const rows = [transcriptRow(data('21:10 那一句'), T('21:10:00'))];
    expect(placeGap(rows, T('20:58:13')).index).toBe(1);
  });

  it('断口那一行整行走缺口画法', () => {
    expect(gapRow(T('20:57:31'), 'note')).toMatchObject({
      source: null,
      route: '采集中断',
      gap: true,
      interlude: true,
    });
  });

  it('断口那一行的形状照设计稿 4.5：时刻格 [HH:MM–HH:MM]、路名格 采集中断（返工轮 m2）', () => {
    const row = gapRow(T('20:57:31'), '', T('20:58:13'));
    expect(row.timeText).toBe('[20:57\u201320:58]');
    expect(row.route).toBe('采集中断');
    expect(row.text).toBe('');
    // 时刻格整格就是区间（rowTime 优先给 timeText）
    expect(rowTime(row)).toBe('[20:57\u201320:58]');
  });

  it('恢复时刻不可知时退回单时刻（不编一个区间）', () => {
    const row = gapRow(T('20:57:31'), 'note');
    expect(row.timeText).toBeUndefined();
    expect(rowTime(row)).toBe('[20:57]');
  });

  it('带恢复时刻的断口插进窗口：时刻格带上起止区间', () => {
    const rows = [transcriptRow(data('21:10 那一句'), T('21:10:00'))];
    const withGap = insertGap(rows, T('20:57:31'), '', T('20:58:13'));
    expect(withGap[1]!.interlude).toBe(true);
    expect(withGap[1]!.timeText).toBe('[20:57\u201320:58]');
  });
});

describe('showJumpToLatest：「回到最新」什么时候露脸', () => {
  it('贴着底部：收起来', () => {
    expect(showJumpToLatest(600, 634, 34)).toBe(false);
  });

  it('往上翻了：露出来', () => {
    expect(showJumpToLatest(0, 3400, 600)).toBe(true);
  });
});
