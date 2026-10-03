import { z } from 'zod';
import { isoStringSchema, sessionIdSchema } from './snapshot';

/**
 * `GET /api/summaries`：**本场总结记录**（`docs/contract.md` 4.7）。
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
 * 两处是**两个形状**（出处：`ServiceRuntime._summary_result()` / `_summary_entry()`，
 * 前者多一格窗口行数）。`lines` 是给这一次应答看的「我读了 N 行」，记录那一份按 4.7
 * 写死的形状没有它 —— 别把两者合并成一个 schema，合并了就有一边要放行多余的键。
 */
export const summaryEntrySchema = z
  .object({
    window: z.string(),
    /** 人话标签：`5 分钟` / `15 分钟` / `30 分钟` / `整场`（服务给，界面照原样用）。 */
    window_label: z.string(),
    at: isoStringSchema,
    elapsed_seconds: z.number(),
    what: z.array(z.string()),
    where: z.array(z.string()),
    lost: z.array(z.string()),
  })
  .strict();

/**
 * 端点应答体：`{session_id, entries}`。
 *
 * `session_id` 是**场次目录名**（人给的工作区目录名，形状任意）或 `null` —— 与快照里
 * `session.id` 同一档，因此复用 `sessionIdSchema`（那条 `.strict()` 的面板口径见
 * `docs/contract.md` 第 2 章）。
 */
export const summariesResponseSchema = z
  .object({
    session_id: sessionIdSchema.nullable(),
    entries: z.array(summaryEntrySchema),
  })
  .strict();

export type SummaryEntry = z.infer<typeof summaryEntrySchema>;
export type SummariesResponse = z.infer<typeof summariesResponseSchema>;
