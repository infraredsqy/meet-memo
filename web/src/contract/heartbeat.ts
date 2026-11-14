import { z } from 'zod';
import { isoStringSchema } from './snapshot';
import { topStateSchema } from './enums';

/** 帧空洞记录：录中整进程冻结解冻后，发帧线程第一拍算出的真实空洞时长留在这里 —— 空洞是**历史事实**，与 `recording_alive`（「现在采不采得到」）语义互不覆盖。 */
export const captureGapsSchema = z
  .object({
    /** 本场空洞的累计次数（跨路合并过的回合只算一次）。 */
    total_count: z.number().int().nonnegative(),
    /** 本场空洞的总秒数（掩码 0）。 */
    total_seconds: z.number().nonnegative(),
    /** 最近至多 10 条，按发生先后排；`at` 是空洞被记录那一刻的墙钟（≈ 空洞结束）。 */
    recent: z
      .array(
        z
          .object({
            at: isoStringSchema,
            seconds: z.number().nonnegative(),
          })
          .strict(),
)
      .max(10),
  })
  .strict();

export type CaptureGaps = z.infer<typeof captureGapsSchema>;

/**
 * 心跳应答：`GET /api/heartbeat`，**任何状态下都 200**，应答恰好七字段。
 *
 * 两个「心跳」绝不串线：`settings.heartbeat_interval_seconds` 是**界面**发心跳的周期；
 * `max(2.0s, 3 × frame_interval)` 是**服务侧**的采集帧时钟窗口，`recording_alive` 的判据。
 * 界面的周期绝不参与 `recording_alive` 的计算；反过来，`recording_alive == false` 也不会
 * 让心跳请求失败 —— 它只是一份数据。
 */
export const heartbeatSchema = z
  .object({
    /** 恒 `"meeting"`。端口探测靠这个字面量认亲，因此必须存在。 */
    service: z.string(),
    /** 服务侧此刻的 ISO 时间戳。**迟到/失联判定以它为准** —— 拿本地时钟去减它，别拿本地请求间隔算。 */
    server_time: isoStringSchema,
    revision: z.number().int(),
    top_state: topStateSchema,
    /**
     * 采集还活着吗。`idle` / `ended` 态给 `null`（没有在录的场次）；`recording` / `alert` 态
     * 给 bool，**开场那一拍除外**：自本场 start 起算的帧时钟窗口内还没收到第一帧时报 `null`
     * （未知），超窗仍无首帧才报 `false`。
     */
    recording_alive: z.boolean().nullable(),
    /**
     * 距最近一帧事实的秒数；一帧都没收到过就是 `null`。
     * 它与 `recording_alive` **不同源**：前者是「多久没收到帧」的原始读数，后者是布尔结论。
     */
    seconds_since_last_tick: z.number().nullable(),
    /**
     * 本场的帧空洞记录：按整档粒度记，不点名哪一路（冻结冻的是整个进程，两路一起丢）。
     * 没有空洞时是空记录（不给 `null`）。界面对它只摆一条提示行，**不出声、不弹红横幅、不进
     * 两拍计数**。
     */
    capture_gaps: captureGapsSchema,
  })
  .strict();

export type Heartbeat = z.infer<typeof heartbeatSchema>;
