import { describe, expect, it } from 'vitest';
import {
  SPEAKER_RENAME_ACTION,
  duplicateNameHolders,
  identityTitle,
  namesRows,
} from '../src/home/names';
import type { SessionStatus } from '../src/contract';

/** 一格「没跑过」的纪要读数（`minutes` 格的空档形状）。 */
function minutesIdle(): SessionStatus['minutes'] {
  return { state: 'idle', started_at: null, finished_at: null, error: null };
}

/** 一格「没跑过」的转码工序读数（`transcode` 格的空档形状）。 */
function transcodeIdle(): SessionStatus['transcode'] {
  return {
    state: 'idle',
    in_process: false,
    started_at: null,
    finished_at: null,
    elapsed_seconds: null,
    current_source: null,
    sources: [],
  };
}


/**
 * 「说话人名字」纯逻辑的用例（人名节点 ``）：行怎么读、撞名怎么判。
 * 判据全部来自服务给的 `speakers` 一格——这一层只转手，不造数据。
 */

function statusWith(speakers: SessionStatus['speakers']): SessionStatus {
  // 只借 `speakers` 那一格的形状：其余格填合法的占位值，读数不参与这里的判据
  return {
    dir: 'C:/会议/250919',
    session_id: '250919',
    kind: 'meeting',
    state: 'ended',
    date: '2025-09-19',
    mode: 'both',
    segments: [],
    has_offline: false,
    post: [],
    transcode: transcodeIdle(),
    minutes: minutesIdle(),
    offline: {
      ledger: 'meeting_offline_progress.jsonl',
      state: 'idle',
      in_process: false,
      started_at: null,
      finished_at: null,
      elapsed_seconds: null,
      current_source: null,
      sources: [],
    },
    speakers,
  };
}

describe('namesRows：单场读数 → 弹窗的行', () => {
  it('每个身份一行：路级身份只有标签，带编号的身份附编号', () => {
    const rows = namesRows(
      statusWith([
        { key: 'mic', source: 'mic', label: '本地', name: null, segments: null },
        {
          key: 'loopback:S01',
          source: 'loopback',
          label: '远端',
          name: '小李',
          segments: 3,
        },
      ]),
);

    expect(rows).toEqual([
      { key: 'mic', title: '本地', name: null },
      { key: 'loopback:S01', title: '远端 · S01', name: '小李' },
    ]);
  });

  it('读数还没到手或盘上没有材料时给空数组（空态由弹窗如实画，不编身份）', () => {
    expect(namesRows(null)).toEqual([]);
    expect(namesRows(statusWith([]))).toEqual([]);
  });

  it('identityTitle：编号取键里路前缀之后的那一段', () => {
    expect(
      identityTitle({ key: 'mic:S02', source: 'mic', label: '本地', name: null, segments: 1 }),
).toBe('本地 · S02');
    expect(
      identityTitle({ key: 'loopback', source: 'loopback', label: '远端', name: null, segments: null }),
).toBe('远端');
  });
});

describe('duplicateNameHolders：撞名只提示、不拦', () => {
  const rows = namesRows(
    statusWith([
      { key: 'mic:S01', source: 'mic', label: '本地', name: '老王', segments: 3 },
      { key: 'mic:S02', source: 'mic', label: '本地', name: null, segments: 1 },
      { key: 'loopback', source: 'loopback', label: '远端', name: '老王', segments: null },
    ]),
);

  it('与其它行的现名同名时报出那些行（自己不算自己的撞名）', () => {
    expect(duplicateNameHolders(rows, 'mic:S02', '老王')).toEqual(['mic:S01', 'loopback']);
    expect(duplicateNameHolders(rows, 'mic:S01', '老王')).toEqual(['loopback']);
  });

  it('没有撞名时给空数组', () => {
    expect(duplicateNameHolders(rows, 'mic:S02', '小李')).toEqual([]);
  });

  it('空名字不算撞名（空 = 不起名）', () => {
    expect(duplicateNameHolders(rows, 'mic:S02', '   ')).toEqual([]);
  });

  it('动作名与契约逐字对齐', () => {
    expect(SPEAKER_RENAME_ACTION).toBe('speaker_rename');
  });
});
