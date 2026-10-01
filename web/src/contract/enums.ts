import { z } from 'zod';
import {
  ACTION_NAMES,
  DRAIN_STEP_IDS,
  ENDED_PHASES,
  ERROR_CODES,
  SOURCE_HEALTHS,
  SOURCE_IDS,
  SOURCE_MODES,
  TOP_STATES,
  TRANSCRIPT_HEALTHS,
  TRANSCRIPT_LINE_KINDS,
} from './primitives';

/**
 * 取值域枚举。这些字面量是契约的骨架（`docs/contract.md` 第 2 / 3 / 4 章），
 * 掩码规则第 2.4 节点明它们**逐字保留** —— 掩掉就看不出形状了。
 */

/** 快照四态。 */
export const topStateSchema = z.enum(TOP_STATES);

/** 那一路的 id，同时也是两路的固定次序。 */
export const sourceIdSchema = z.enum(SOURCE_IDS);

/** `health` 七取值：前三档由启动时的设备探测直取，后四档由状态迁移写值。 */
export const sourceHealthSchema = z.enum(SOURCE_HEALTHS);

/** 要录哪几路，开场前定死。 */
export const sourceModeSchema = z.enum(SOURCE_MODES);

/** 收尾四步的 id。 */
export const drainStepIdSchema = z.enum(DRAIN_STEP_IDS);

/** `ended.phase`，只有 `draining` / `done` 两取值。 */
export const endedPhaseSchema = z.enum(ENDED_PHASES);

/** 转写后端的健康位。 */
export const transcriptHealthSchema = z.enum(TRANSCRIPT_HEALTHS);

/** 转写行的 `kind`。 */
export const transcriptLineKindSchema = z.enum(TRANSCRIPT_LINE_KINDS);

/** 告警分级：致命级同时只留一条，提示级上限 3。 */
export const alertLevelSchema = z.enum(['critical', 'hint']);

/** 七个动作名。 */
export const actionNameSchema = z.enum(ACTION_NAMES);

/** 失败信封的九个 `code`。 */
export const errorCodeSchema = z.enum(ERROR_CODES);
