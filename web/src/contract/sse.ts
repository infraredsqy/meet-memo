import { z } from 'zod';
import { DBFS_PLACEHOLDER, ENVELOPE_BAR_COUNT } from './primitives';
import { sourceIdSchema, transcriptLineKindSchema } from './enums';
import { dbfsSchema, nullableDbfsSchema, resyncSnapshotSchema, snapshotSchema } from './snapshot';

/**
 * SSE 四事件的帧形状（`docs/contract.md` 第 3 章）。
 *
 * 帧一律 `event: <名字>` + `data: <一个 JSON 对象>`；`state` / `snapshot` 多一行 `id:`，
 * 放在 `event:` 之前，值 = 快照的 `revision`。`levels` / `transcript` 不带 `id:`，
 * 只在 `data` 里带当前 `revision` 作参照 —— 这两路高频，**不占 `revision`**。
 */

/** 四事件名。 */
export const SSE_EVENT_NAMES = ['snapshot', 'state', 'levels', 'transcript'] as const;
export const sseEventNameSchema = z.enum(SSE_EVENT_NAMES);
export type SseEventName = z.infer<typeof sseEventNameSchema>;

/**
 * 包络长度是否恰为 15 —— 结构不变量（`contract/README.md` 2.5 的「包络数组只保结构与长度」，
 * 契约正文在 `docs/contract.md` 3.2）。
 *
 * 它是 `levelsFrameSchema` 长度那一条判据的**唯一出处**：schema 与用例都从这里取，
 * 因此「15 根」这个数只写一次，改一处两处一起跟着变。
 *
 * **适用面只到 levels 帧**：快照里的 `sources[*].envelope` 空闲态合法为空数组
 * （0 根），拿这个函数去卡快照会误伤空闲态。
 */
export function isFullEnvelope(envelope: readonly unknown[]): boolean {
  return envelope.length === ENVELOPE_BAR_COUNT;
}

/**
 * `levels` 帧的每路五键。
 *
 * `ts` 恒为 `null` —— 这一格写死了（服务抽帧时不带帧时间戳），因此这里是 `z.null()`
 * 而不是可空 number：它一旦出值就说明契约漂了。界面要显示时间就用 `data.revision`
 * 或自己那一侧的到达时刻。
 *
 * 3.2 把两条结构不变量写成了契约的一部分，这里在 schema 层把它们咬住
 * （`docs/contract.md` 3.2「`bar == envelope[-1]` 是结构不变量」「包络长度 15 同理」）：
 *
 * - 包络长度恰 15 根（`isFullEnvelope`）；
 * - `bar` 与 `envelope[-1]` 是同一个数的两种读法。
 *
 * 两条都用 `refine` 写在帧这一层，因为它们是**跨键**的关系，单元 schema 表达不了。
 *
 * **只管 levels 帧**：快照里的 `sources[*].envelope` 在空闲态合法为空数组，
 * 录音中心才长到 15 根，因此那边的 `envelopeSchema` 保持只校验元素形状。
 */
export const levelsFrameSchema = z
  .object({
    ts: z.null(),
    dbfs: nullableDbfsSchema,
    peak: nullableDbfsSchema,
    /** 恒等于该路 `envelope[-1]`（同一个数的两种读法）。结构不变量，服务侧算好。 */
    bar: nullableDbfsSchema,
    envelope: z.array(dbfsSchema).refine(isFullEnvelope, {
      message: `levels 帧的包络长度恰 ${ENVELOPE_BAR_COUNT} 根（docs/contract.md 3.2）`,
    }),
  })
  .strict()
  .refine((frame) => frame.bar === frame.envelope[frame.envelope.length - 1], {
    message: 'levels 帧的 bar 恒等于 envelope[-1]（docs/contract.md 3.2 结构不变量）',
    path: ['bar'],
  });

/** `frames` 恰好两键 `mic` / `loopback`。 */
export const levelsFramesSchema = z
  .object({
    mic: levelsFrameSchema,
    loopback: levelsFrameSchema,
  })
  .strict();

/** `levels` 帧：`{revision, frames}`。 */
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

/** `transcript` 帧：`{revision, line}`。 */
export const transcriptDataSchema = z
  .object({
    revision: z.number().int(),
    line: transcriptLineSchema,
  })
  .strict();

/**
 * `snapshot` / `state` 两路携带完整快照。
 *
 * 同一个事件名 `snapshot` 有两个去处：建连首帧给十键快照，慢消费者重同步给十二键快照。
 * 因此解析要按「先试扩展形状、再退十键形状」的顺序走 —— 反过来会让带 `resync` 的帧
 * 因为多出两个键被 `strict()` 拒掉。
 */
export const statefulDataSchema = z.union([resyncSnapshotSchema, snapshotSchema]);
export type StatefulData = z.infer<typeof statefulDataSchema>;

/**
 * SSE 帧样本形状（`contract/fixtures/sse/*.json`）：`{event, id, data}`。
 * `id` 保留线上形状的字符串；`levels` / `transcript` 不带 `id:`，样本里就是 `null`。
 */
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

/** 重导一下占位符常量，方便调用方只 import 这一个模块。 */
export { DBFS_PLACEHOLDER };
