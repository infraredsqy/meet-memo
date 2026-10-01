import { z } from 'zod';
import { isoStringSchema, sessionIdSchema } from './snapshot';

/**
 * 工作区注册表与目录选择器的两只读端点（`docs/contract.md` 4.8）。
 *
 * - `GET /api/sessions`：**软件自己记的那份"我这些会"**——住在服务侧配置目录的
 *   `workspaces.json` 里、按路径去重。每一行的"目录还在不在、里面是什么"由服务现场判
 *   （注册表条目只记两个时刻），界面照读不派生；
 * - `GET /api/dirs`：目录选择器的现场（起点是用户主目录），一层子目录的名目判读。
 *
 * 两条都是**纯读**：不涨 `revision`、不广播、不写任何东西（与 `GET /api/summaries`
 * 同一档）。
 */

/** 一个目录的名目判读：三档 + `missing`。`suspected` 是"疑似会议目录"的兜底档。 */
export const dirKindSchema = z.enum(['empty', 'meeting', 'unknown', 'missing']);

/** 注册表里的一行：`dir` 是规范化的绝对路径（去重判据），时刻两格秒精度。 */
export const sessionRowSchema = z
  .object({
    dir: z.string(),
    /** 盘上 `session.json` 的目录名；没开过会（空白工作区 / 疑似会议目录）时是 `null`，
     *  界面那一句才拿 `dir` 兜（`home/WorkspaceHome.tsx`）。 */
    session_id: sessionIdSchema.nullable(),
    dir_exists: z.boolean(),
    kind: dirKindSchema,
    /** 有音频归档名但没有 `session.json` 的疑似会议目录（照收编处理）。 */
    suspected: z.boolean(),
    /** 盘上 `session.json` 的 `state`（四档之外如实 `null`，服务不反查映射）。 */
    state: z.enum(['recording', 'draining', 'done', 'failed']).nullable(),
    date: z.string().nullable(),
    mode: z.string().nullable(),
    added_at: isoStringSchema,
    last_opened_at: isoStringSchema,
  })
  .strict();

/** `GET /api/sessions` 的应答体。`recent` = 最近一次被打开/切换那条的目录（或 `null`）。 */
export const sessionsResponseSchema = z
  .object({
    sessions: z.array(sessionRowSchema),
    recent: z.string().nullable(),
  })
  .strict();

/** 目录选择器现场的一项子目录。 */
export const dirRowSchema = z
  .object({
    name: z.string(),
    dir: z.string(),
    kind: dirKindSchema,
    suspected: z.boolean(),
  })
  .strict();

/** `GET /api/dirs` 的应答体。`up` 到主目录时为 `null`（不再往上给）。 */
export const dirsResponseSchema = z
  .object({
    home: z.string(),
    cwd: z.string(),
    up: z.string().nullable(),
    dirs: z.array(dirRowSchema),
  })
  .strict();

/**
 * `workspace_open` / `workspace_remove` 的成功体：通用信封 + 一个 `workspace` 键
 * （先例是 `summarize` 的 `summary`）。两档形状并成一个 schema：`open` 给判别档位
 * （`kind` / `suspected` / `first_time`），`remove` 给 `removed` —— 各带各的那几格，
 * 对面的格子在 `remove` 档缺席（服务不摆没有数的空格），所以这四格全部 `optional()`。
 * 分派的读法：`removed` 在 → 是 `remove` 的回执；`kind` 在 → 是 `open` 的判读。
 * `kind` 恒 `null` 不会出现（服务不编档位），这里收可选值校验即可。
 */
export const workspaceOkSchema = z
  .object({
    ok: z.literal(true),
    revision: z.number().int(),
    workspace: z
      .object({
        dir: z.string(),
        kind: dirKindSchema.optional(),
        suspected: z.boolean().optional(),
        first_time: z.boolean().optional(),
        removed: z.boolean().optional(),
      })
      .strict(),
  })
  .strict();

export type DirKind = z.infer<typeof dirKindSchema>;
export type SessionRow = z.infer<typeof sessionRowSchema>;
export type SessionsResponse = z.infer<typeof sessionsResponseSchema>;
export type DirRow = z.infer<typeof dirRowSchema>;
export type DirsResponse = z.infer<typeof dirsResponseSchema>;
export type WorkspaceOk = z.infer<typeof workspaceOkSchema>;
