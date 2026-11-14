import { z } from 'zod';
import { DBFS_PLACEHOLDER, ENVELOPE_BAR_COUNT } from './primitives';
import { sourceIdSchema, transcriptLineKindSchema } from './enums';
import { dbfsSchema, nullableDbfsSchema, resyncSnapshotSchema, snapshotSchema } from './snapshot';

/**
 * SSE 四事件的帧形状。
 *
 * 帧一律 `event: <名字>` + `data: <一个 JSON 对象>`；`state` / `snapshot` 多一行 `id:`，
 * `levels` / `transcript` 不带 `id:` —— 这两路高频，**不占 `revision`**。
 */

export const SSE_EVENT_NAMES = ['snapshot', 'state', 'levels', 'transcript'] as const;
export const sseEventNameSchema = z.enum(SSE_EVENT_NAMES);
export type SseEventName = z.infer<typeof sseEventNameSchema>;

/**
 * 包络长度是否恰为 15 —— 结构不变量。它是 `levelsFrameSchema` 长度那一条判据的**唯一出处**。
 *
 * **适用面只到 levels 帧**：快照里的 `sources[*].envelope` 空闲态合法为空数组（0 根），
 * 拿这个函数去卡快照会误伤空闲态。
 */
export function isFullEnvelope(envelope: readonly unknown[]): boolean {
  return envelope.length === ENVELOPE_BAR_COUNT;
}

/**
 * `levels` 帧的每路五键。
 *
 * `ts` 恒为 `null` —— 这一格写死了（服务抽帧时不带帧时间戳），因此这里是 `z.null()` 而不是
 * 可空 number：它一旦出值就说明契约漂了。
 *
 * 两条结构不变量用 `refine` 写在帧这一层（**跨键**的关系，单元 schema 表达不了）：包络长度恰
 * 15 根；`bar` 与 `envelope[-1]` 是同一个数的两种读法。
 *
 * **只管 levels 帧**：快照里的 `sources[*].envelope` 在空闲态合法为空数组。
 */
export const levelsFrameSchema = z
  .object({
    ts: z.null(),
    dbfs: nullableDbfsSchema,
    peak: nullableDbfsSchema,
    /** 恒等于该路 `envelope[-1]`，服务侧算好。 */
    bar: nullableDbfsSchema,
    envelope: z.array(dbfsSchema).refine(isFullEnvelope, {
      message: `levels 帧的包络长度恰 ${ENVELOPE_BAR_COUNT} 根`,
    }),
  })
  .strict()
  .refine((frame) => frame.bar === frame.envelope[frame.envelope.length - 1], {
    message: 'levels 帧的 bar 恒等于 envelope[-1]',
    path: ['bar'],
  });

export const levelsFramesSchema = z
  .object({
    mic: levelsFrameSchema,
    loopback: levelsFrameSchema,
  })
  .strict();

export const levelsDataSchema = z
  .object({
    revision: z.number().int(),
    frames: levelsFramesSchema,
  })
  .strict();

/** 转写行四键。`ts` 是后端给的秒数 —— 语义归后端，服务只透传。 */
export const transcriptLineSchema = z
  .object({
    ts: z.number(),
    source: sourceIdSchema,
    text: z.string(),
    kind: transcriptLineKindSchema,
  })
  .strict();

export const transcriptDataSchema = z
  .object({
    revision: z.number().int(),
    line: transcriptLineSchema,
  })
  .strict();

/** 同一个事件名 `snapshot` 有两个去处：建连首帧给十键快照，慢消费者重同步给十二键快照。因此解析要按「先试扩展形状、再退十键形状」的顺序走。 */
export const statefulDataSchema = z.union([resyncSnapshotSchema, snapshotSchema]);

/** SSE 帧样本形状：`{event, id, data}`。`levels` / `transcript` 不带 `id:`，样本里就是 `null`。 */
export const sseFrameSchema = z.discriminatedUnion('event', [
  z.object({ event: z.literal('snapshot'), id: z.string(), data: statefulDataSchema }),
  z.object({ event: z.literal('state'), id: z.string(), data: snapshotSchema }),
  z.object({ event: z.literal('levels'), id: z.null(), data: levelsDataSchema }),
  z.object({ event: z.literal('transcript'), id: z.null(), data: transcriptDataSchema }),
]);
export type SseFrame = z.infer<typeof sseFrameSchema>;

export type LevelsFrame = z.infer<typeof levelsFrameSchema>;
export type LevelsData = z.infer<typeof levelsDataSchema>;
export type TranscriptLine = z.infer<typeof transcriptLineSchema>;
export type TranscriptData = z.infer<typeof transcriptDataSchema>;

export { DBFS_PLACEHOLDER };
