import { z } from 'zod';
import { isoStringSchema, sessionIdSchema } from './snapshot';

/**
 * `GET /api/summaries`：**本场总结记录**。
 *
 * 取的是本场按过的、**成功产出的**那几次，按时间先后排；失败的那几次不记
 * （没有「那几条」可记）。**任何状态下都 200** —— 没有场次或一次都没按过时
 * `session_id` 是 `null`、`entries` 是空数组。
 *
 * 记录**活在服务侧内存里、会话级、不落文件**：新的一场 `start` 把它清空，
 * 服务关停一并清掉（界面因此不做任何本地持久化）。
 */

/**
 * 记录里那一条 —— **七格**：应答体那一份（`summarizeResultSchema`）少一格 `lines`。
 *
 * 两处是**两个形状**。`lines` 是给这一次应答看的「我读了 N 行」，记录那一份没有它
 * —— 别把两者合并成一个 schema，合并了就有一边要放行多余的键。
 */
export const summaryEntrySchema = z
  .object({
    window: z.string(),
    window_label: z.string(),
    at: isoStringSchema,
    elapsed_seconds: z.number(),
    what: z.array(z.string()),
    where: z.array(z.string()),
    lost: z.array(z.string()),
  })
  .strict();

export const summariesResponseSchema = z
  .object({
    session_id: sessionIdSchema.nullable(),
    entries: z.array(summaryEntrySchema),
  })
  .strict();

export type SummaryEntry = z.infer<typeof summaryEntrySchema>;
export type SummariesResponse = z.infer<typeof summariesResponseSchema>;
