/**
 * 会中收音的四组合直切（`c9f5d04d`）：整个收音状态收成一处集中控制，
 * 一次只处在四个组合之一，切换即到位。
 *
 * 四组合 → 三个动作（`pause` / `resume` / `set_source`）的映射表，照 spec 给的
 * 现状语义组装：
 *
 * | 组合 | 语义 | 摆到哪 |
 * |---|---|---|
 * | 两路都录 | 正常录制 | 开着的那几路不动；被关掉的重新打开；暂停中先摆路再 resume |
 * | 仅本地 | 远端关掉 | loopback 关、mic 开；暂停中先摆路再 resume |
 * | 仅远端 | 本地关掉 | mic 关、loopback 开；暂停中先摆路再 resume |
 * | 全停 | 暂停（写静音、设备留着、时长停走） | `pause`；已暂停就一个请求都不发 |
 *
 * 三条口径：
 *
 * 1. **「全停」沿用暂停语义**（`docs/design-intent.md` 第 8 条），文案就叫暂停 / 全停
 *    这一族，不另造「关流」路径，也不用绕开的词。
 * 2. **现状只从快照读**：暂停看 `session.paused`，某一路关没关看那一路的
 *    `health === 'stopped'`（契约现成的落点）。映射表的产出只是「还差的那几步」，
 *    界面不为它记第二份状态。
 * 3. **幂等先算在客户端**：重按当前组合得到空表、一个请求都不发；服务端的幂等
 *    留作并发与迟到请求那最后一道兜底。
 */

import type { Snapshot, SourceId } from '../contract';

/** 四组合之一。`paused` 就是「全停」那一档（沿用暂停语义，见模块头）。 */
export type CaptureCombo = 'both' | 'local' | 'remote' | 'paused';

/** 四组合的界面次序（spec 表的原次序），摆档位、遍历都用它。 */
export const COMBO_ORDER: readonly CaptureCombo[] = ['both', 'local', 'remote', 'paused'];

/** 四组合的大字（spec 表的原词；「休息」「关流」这类绕开的说法不进这张表）。 */
export const COMBO_LABELS: Readonly<Record<CaptureCombo, string>> = {
  both: '两路都录',
  local: '仅本地',
  remote: '仅远端',
  paused: '全停',
};

/** 映射表眼里的「现状」：暂停与否 + 两路各自的关 / 开。 */
export interface ComboState {
  paused: boolean;
  micClosed: boolean;
  loopClosed: boolean;
}

/** 从一份快照读出现状（暂停一格 + 两路各自的 `stopped` 与否）。 */
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
 * 现状 → 此刻处在四个组合中的哪一个。
 *
 * 暂停中恒是「全停」（两路的关 / 开在暂停期间留在原地，继续哪一档由下一次按下的
 * 组合决定）。没暂停时看两路：都开是「两路都录」，关了一路是「仅本地 / 仅远端」；
 * 「两路都关着却没暂停」这一格新控件造不出来（两路全停走的是暂停），只有旧界面
 * 或 dev_inject 能留下它 —— 这里也读作「全停」：从这一格按四个组合中的任何一个
 * 都是说得通的出路。
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

/** 映射表发的一个动作；名与参就是契约动作面原文。 */
export type ComboAction =
  | { name: 'pause' }
  | { name: 'resume' }
  | { name: 'set_source'; source: SourceId; active: boolean };

/**
 * 映射表：从现状到目标组合要发的动作（差几步发几步，次序在下）。
 *
 * 次序有一句讲究：**摆路的 `set_source` 在前、`resume` 在后** —— 暂停中按下的
 * 是「继续之后用哪几路」，先把路摆对再继续，内容恢复那一刻配置已经就位；
 * 而且哪一路打不开（`capture_unavailable`）就停在原地（仍然暂停着），不会录出
 * 半截配置错乱的音频。调用方按次序发、中途失败即停。
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
