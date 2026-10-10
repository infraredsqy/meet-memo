import { describe, expect, it } from 'vitest';

import {
  COMBO_LABELS,
  COMBO_ORDER,
  comboActions,
  comboOf,
  comboStateOf,
  snapshotComboState,
  type ComboState,
} from '../src/live/combo';
import { liveView } from '../src/live/view';
import { initialHeartbeatState } from '../src/live/heartbeat';
import { makeSnapshot, makeSource } from './live-fixtures';

/**
 * 会中收音四组合直切的映射表（`c9f5d04d`）。
 *
 * spec 完成判定的第四条钉在这里：每档组合发哪些动作、幂等重按一个请求都不发。
 * 判据三条：
 *
 * 1. **派生**：快照的现状（暂停一格 + 两路各自的 `stopped`）落进四档之一，
 *    一次只处在一档 —— 八种现状组合逐一有归属；
 * 2. **映射**：四档 → 三个动作（`pause` / `resume` / `set_source`）照 spec 的表发，
 *    摆路的在前、`resume` 在后；
 * 3. **幂等**：重按当前档，映射表给空表。
 */

const state = (paused: boolean, micClosed: boolean, loopClosed: boolean): ComboState => ({
  paused,
  micClosed,
  loopClosed,
});

const setSource = (source: 'mic' | 'loopback', active: boolean) => ({
  name: 'set_source',
  source,
  active,
});

describe('comboOf：八种现状逐一落进四档', () => {
  it('没暂停：都开是「两路都录」，关一路是「仅本地 / 仅远端」', () => {
    expect(comboOf(state(false, false, false))).toBe('both');
    expect(comboOf(state(false, false, true))).toBe('local');
    expect(comboOf(state(false, true, false))).toBe('remote');
  });

  it('暂停中恒是「全停」，两路开 / 关留在原处也照读', () => {
    expect(comboOf(state(true, false, false))).toBe('paused');
    expect(comboOf(state(true, false, true))).toBe('paused');
    expect(comboOf(state(true, true, false))).toBe('paused');
    expect(comboOf(state(true, true, true))).toBe('paused');
  });

  it('两路都关着却没暂停：新控件造不出这一格（旧界面残留），也读作「全停」', () => {
    expect(comboOf(state(false, true, true))).toBe('paused');
  });
});

describe('snapshotComboState + comboOf：从用例快照读出的那一档', () => {
  const heartbeat = initialHeartbeatState();

  it('缺省快照（两路都在采、没暂停）是「两路都录」', () => {
    const snapshot = makeSnapshot();
    expect(comboOf(snapshotComboState(snapshot))).toBe('both');
    expect(liveView(snapshot, heartbeat, null, null, '2026-09-21T20:57:31+08:00').combo).toBe(
      'both',
    );
  });

  it('远端被关掉是「仅本地」，本地被关掉是「仅远端」', () => {
    const localOnly = makeSnapshot({
      sources: [makeSource('mic'), makeSource('loopback', { health: 'stopped' })],
    });
    expect(comboOf(snapshotComboState(localOnly))).toBe('local');
    const remoteOnly = makeSnapshot({
      sources: [makeSource('mic', { health: 'stopped' }), makeSource('loopback')],
    });
    expect(comboOf(snapshotComboState(remoteOnly))).toBe('remote');
  });

  it('暂停中是「全停」', () => {
    const snapshot = makeSnapshot({
      session: {
        id: '跟老王聊周会',
        dir: 'C:\\会议\\跟老王聊周会',
        started_at: '2026-09-21T20:45:20+08:00',
        mode: 'both',
        elapsed_seconds: 4354,
        frozen: false,
        paused: true,
      },
    });
    expect(comboOf(snapshotComboState(snapshot))).toBe('paused');
  });

  it('暂停一格缺席（老快照）按没暂停处理', () => {
    const snapshot = makeSnapshot();
    const withoutPaused = { ...snapshot, session: { ...snapshot.session!, paused: undefined } };
    expect(comboOf(snapshotComboState(withoutPaused as unknown as typeof snapshot))).toBe('both');
  });
});

describe('comboActions：每档组合发哪些动作', () => {
  it('从「两路都录」出发：切仅本地只关远端，切仅远端只关本地，切全停只发 pause', () => {
    const current = state(false, false, false);
    expect(comboActions('local', current)).toEqual([setSource('loopback', false)]);
    expect(comboActions('remote', current)).toEqual([setSource('mic', false)]);
    expect(comboActions('paused', current)).toEqual([{ name: 'pause' }]);
  });

  it('从「仅本地」出发：切两路都录只重新打开远端', () => {
    expect(comboActions('both', state(false, false, true))).toEqual([
      setSource('loopback', true),
    ]);
  });

  it('从「仅远端」出发：切仅本地要摆两路（mic 开在前、loopback 关在后）', () => {
    expect(comboActions('local', state(false, true, false))).toEqual([
      setSource('mic', true),
      setSource('loopback', false),
    ]);
  });

  it('暂停中（两路都开）切三档录制：只发 resume，两路不动', () => {
    const current = state(true, false, false);
    expect(comboActions('both', current)).toEqual([{ name: 'resume' }]);
    expect(comboActions('local', current)).toEqual([
      setSource('loopback', false),
      { name: 'resume' },
    ]);
    expect(comboActions('remote', current)).toEqual([
      setSource('mic', false),
      { name: 'resume' },
    ]);
  });

  it('暂停中（暂停前是仅本地）切两路都录：先把远端摆回来，resume 收尾', () => {
    // 暂停前远端被关着：暂停不翻两路的开 / 关，现状里 loopClosed 仍是 true
    expect(comboActions('both', state(true, false, true))).toEqual([
      setSource('loopback', true),
      { name: 'resume' },
    ]);
    expect(comboActions('local', state(true, false, true))).toEqual([{ name: 'resume' }]);
  });

  it('摆路的 set_source 在前、resume 在后：哪一路打不开就停在暂停，不录出半截错配', () => {
    // 暂停前是「仅本地」（远端关着）→ 切「仅远端」：本地关、远端开、再继续
    const actions = comboActions('remote', state(true, false, true));
    expect(actions).toEqual([
      setSource('mic', false),
      setSource('loopback', true),
      { name: 'resume' },
    ]);
    expect(actions.at(-1)).toEqual({ name: 'resume' });
  });
});

describe('comboActions：幂等重按不发多余请求', () => {
  it.each(COMBO_ORDER.map((combo) => ({ combo })))(
    '处在一档再按同一档：动作表为空（$combo）',
    ({ combo }) => {
      expect(comboActions(combo, comboStateOf(combo))).toEqual([]);
    },
  );

  it('「全停」在暂停中重按：连 pause 都不发', () => {
    expect(comboActions('paused', state(true, false, true))).toEqual([]);
  });

  it('已处在目标组合（现状与 comboStateOf 一致）时三档录制也是空表', () => {
    expect(comboActions('local', state(false, false, true))).toEqual([]);
    expect(comboActions('remote', state(false, true, false))).toEqual([]);
    expect(comboActions('both', state(false, false, false))).toEqual([]);
  });
});

describe('组合的名与序：文案就在暂停 / 全停这一族', () => {
  it('四档的界面次序照 spec 表：两路都录 → 仅本地 → 仅远端 → 全停', () => {
    expect(COMBO_ORDER).toEqual(['both', 'local', 'remote', 'paused']);
    expect(COMBO_ORDER.map((combo) => COMBO_LABELS[combo])).toEqual([
      '两路都录',
      '仅本地',
      '仅远端',
      '全停',
    ]);
  });

  it('字面上不出现「休息」这类绕开的说法（design-intent 第 8 条）', () => {
    for (const combo of COMBO_ORDER) {
      expect(COMBO_LABELS[combo]).not.toContain('休息');
    }
  });
});
