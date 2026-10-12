import { z } from 'zod';
import { isoStringSchema } from './snapshot';
import { topStateSchema } from './enums';

/**
 * 帧空洞记录（gil-stall-hole）：录中整进程冻结（GIL 饿死）解冻后，发帧线程第一拍算出的
 * 真实空洞时长留在这里 —— 空洞是**历史事实**，与 `recording_alive`（「现在采不采得到」）
 * 语义互不覆盖：恢复之后 `recording_alive` 回 `true`，空洞照旧在记录里。
 */
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
 * 心跳应答：`GET /api/heartbeat`，**任何状态下都 200**，应答恰好七字段
 * （`docs/contract.md` 第 5 章）。`contract/fixtures/heartbeat/*.json` 是四份样本
 * （idle / recording / no-first-frame / capture-gap）。
 *
 * 两个「心跳」绝不串线 —— 这是最容易踩的一处：
 *
 * | 名字 | 是谁的 | 管什么 |
 * |---|---|---|
 * | `settings.heartbeat_interval_seconds` | **界面**发心跳的周期 | 界面多久问一次服务（默认 10 秒） |
 * | `max(2.0s, 3 × frame_interval)` = 2.0 秒 | **服务侧**的采集帧时钟窗口 | `recording_alive` 的判据 |
 *
 * 界面的 10 秒周期绝不参与 `recording_alive` 的计算；反过来，
 * `recording_alive == false` 也不会让心跳请求失败 —— 它只是一份数据。
 */
export const heartbeatSchema = z
  .object({
    /** 恒 `"meeting"`。端口探测靠这个字面量认亲，因此必须存在。 */
    service: z.string(),
    /**
     * 服务侧此刻的 ISO 时间戳（本地时区、秒精度）。
     * **迟到/失联判定以它为准** —— 拿本地时钟去减它，别拿本地请求间隔算。
     */
    server_time: isoStringSchema,
    revision: z.number().int(),
    top_state: topStateSchema,
    /**
     * 采集还活着吗。`idle` / `ended` 态给 `null`（没有在录的场次，问这个没有意义）；
     * `recording` / `alert` 态给 bool，**开场那一拍除外**（de899128）：自本场 start
     * 起算的帧时钟窗口（`max(2.0s, 3 × frame_interval)`）内还没收到第一帧时报
     * `null`（未知），超窗仍无首帧才报 `false`。
     *
     * 窗口照旧是服务侧的采集帧时钟：只认 `LevelFrame`。后端卡住不报任何东西时它会
     * 算成 `false`，而 HTTP 照常 200 —— 这正是「只看 HTTP 通不通会漏掉录音线程已
     * 卡死」的判据。界面拿到 `false` 还要**连续两次读数**才判停（见
     * `live/heartbeat.ts` 的两拍规则）。
     */
    recording_alive: z.boolean().nullable(),
    /**
     * 距最近一帧事实的秒数；一帧都没收到过就是 `null`。
     * 它与 `recording_alive` **不同源**：前者是「多久没收到帧」这个原始读数，
     * 后者是布尔结论。显示「多久没读到数据」用前者，画「采集停了」的红灯用后者。
     */
    seconds_since_last_tick: z.number().nullable(),
    /**
     * 本场的帧空洞记录（gil-stall-hole）：录中冻结解冻后由发帧线程报来的历史事实，
     * 按整档粒度记（不点名哪一路 —— 冻结冻的是整个进程，两路一起丢；一路单独断流
     * 由致命级在报）。任何态都在、没有空洞时是空记录（不给 `null`）。界面对它只做
     * 一件事：在告警区摆一条提示行，**不出声、不弹红横幅、不进两拍计数**。
     */
    capture_gaps: captureGapsSchema,
  })
  .strict();

export type Heartbeat = z.infer<typeof heartbeatSchema>;
