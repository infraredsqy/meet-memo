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

export const topStateSchema = z.enum(TOP_STATES);

/** 同时也是两路的固定次序。 */
export const sourceIdSchema = z.enum(SOURCE_IDS);

export const sourceHealthSchema = z.enum(SOURCE_HEALTHS);

/** 开场前定死。 */
export const sourceModeSchema = z.enum(SOURCE_MODES);

export const drainStepIdSchema = z.enum(DRAIN_STEP_IDS);

export const endedPhaseSchema = z.enum(ENDED_PHASES);

export const transcriptHealthSchema = z.enum(TRANSCRIPT_HEALTHS);

export const transcriptLineKindSchema = z.enum(TRANSCRIPT_LINE_KINDS);

/** 告警分级：致命级同时只留一条，提示级上限 3。 */
export const alertLevelSchema = z.enum(['critical', 'hint']);

/** 次序即服务回的次序。 */
export const actionNameSchema = z.enum(ACTION_NAMES);

export const errorCodeSchema = z.enum(ERROR_CODES);
