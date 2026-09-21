import { z } from 'zod';
import { isoStringSchema } from './snapshot';
import { topStateSchema } from './enums';

/**
 * 心跳应答：`GET /api/heartbeat`，**任何状态下都 200**，应答恰好六字段
 * （`docs/contract.md` 第 5 章）。`contract/fixtures/heartbeat/*.json` 是两份样本。
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
     * 采集还活着吗。`recording` / `alert` 态给 bool，`idle` / `ended` 态给 `null`
     * （没有在录的场次，问这个没有意义）。
     *
     * 判据是服务侧的采集帧时钟：窗口 = `max(2.0s, 3 × frame_interval)`，只认 `LevelFrame`。
     * 后端卡住不报任何东西时它会算成 `false`，而 HTTP 照常 200 ——
     * 这正是「只看 HTTP 通不通会漏掉录音线程已卡死」的判据。
     */
    recording_alive: z.boolean().nullable(),
    /**
     * 距最近一帧事实的秒数；一帧都没收到过就是 `null`。
     * 它与 `recording_alive` **不同源**：前者是「多久没收到帧」这个原始读数，
     * 后者是布尔结论。显示「多久没读到数据」用前者，画「采集停了」的红灯用后者。
     */
    seconds_since_last_tick: z.number().nullable(),
  })
  .strict();

export type Heartbeat = z.infer<typeof heartbeatSchema>;
