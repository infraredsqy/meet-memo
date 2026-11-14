/**
 * 会中收音的四组合直切：整个收音状态收成一处集中控制，一次只处在四个组合之一。
 *
 * 三条口径：
 *
 * 1. **「全停」沿用暂停语义**，文案就叫暂停 / 全停这一族，不另造「关流」路径；
 * 2. **现状只从快照读**：暂停看 `session.paused`，某一路关没关看那一路的
 *    `health === 'stopped'`；映射表的产出只是「还差的那几步」；
 * 3. **幂等先算在客户端**：重按当前组合得到空表、一个请求都不发。
 */

import type { Snapshot, SourceId } from '../contract';

/** 四组合之一。`paused` 就是「全停」那一档（沿用暂停语义）。 */
export type CaptureCombo = 'both' | 'local' | 'remote' | 'paused';

/** 四组合的界面次序（摆档位、遍历都用它）。 */
export const COMBO_ORDER: readonly CaptureCombo[] = ['both', 'local', 'remote', 'paused'];

/** 四组合的大字。 */
export const COMBO_LABELS: Readonly<Record<CaptureCombo, string>> = {
  both: '两路都录',
  local: '仅本地',
  remote: '仅远端',
  paused: '全停',
};

export interface ComboState {
  paused: boolean;
  micClosed: boolean;
  loopClosed: boolean;
}

export function snapshotComboState(snapshot: Snapshot): ComboState {
  const closed = (id: SourceId): boolean =>
    snapshot.sources.find((source) => source.id === id)?.health === 'stopped';
  return {
    paused: snapshot.session?.paused ?? false,
    micClosed: closed('mic'),
    loopClosed: closed('loopback'),
  };
}

/**
 * 现状 → 此刻处在四个组合中的哪一个。暂停中恒是「全停」，两路的关 / 开在暂停期间留在原地。
 *
 * 「两路都关着却没暂停」这一格新控件造不出来（两路全停走的是暂停），只有旧界面或
 * `dev_inject` 能留下它 —— 这里也读作「全停」。
 */
export function comboOf(state: ComboState): CaptureCombo {
  if (state.paused) return 'paused';
  if (!state.micClosed && !state.loopClosed) return 'both';
  if (!state.micClosed) return 'local';
  if (!state.loopClosed) return 'remote';
  return 'paused';
}

/** 一个组合所代表的那份现状（「切换还没落地的半途」用它当现状读）。 */
export function comboStateOf(combo: CaptureCombo): ComboState {
  return {
    paused: combo === 'paused',
    micClosed: combo === 'remote',
    loopClosed: combo === 'local',
  };
}

/** 映射表发的一个动作。 */
export type ComboAction =
  | { name: 'pause' }
  | { name: 'resume' }
  | { name: 'set_source'; source: SourceId; active: boolean };

/**
 * 映射表：从现状到目标组合要发的动作（差几步发几步）。
 *
 * **摆路的 `set_source` 在前、`resume` 在后** —— 先把路摆对再继续，内容恢复那一刻配置已经
 * 就位；而且哪一路打不开（`capture_unavailable`）就停在原地（仍然暂停着），不会录出半截
 * 配置错乱的音频。
 */
export function comboActions(target: CaptureCombo, current: ComboState): ComboAction[] {
  if (target === 'paused') {
    return current.paused ? [] : [{ name: 'pause' }];
  }
  const wantMic = target !== 'remote';
  const wantLoop = target !== 'local';
  const actions: ComboAction[] = [];
  if (wantMic && current.micClosed) {
    actions.push({ name: 'set_source', source: 'mic', active: true });
  }
  if (!wantMic && !current.micClosed) {
    actions.push({ name: 'set_source', source: 'mic', active: false });
  }
  if (wantLoop && current.loopClosed) {
    actions.push({ name: 'set_source', source: 'loopback', active: true });
  }
  if (!wantLoop && !current.loopClosed) {
    actions.push({ name: 'set_source', source: 'loopback', active: false });
  }
  if (current.paused) actions.push({ name: 'resume' });
  return actions;
}
