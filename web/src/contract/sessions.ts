import { z } from 'zod';
import { isoStringSchema, sessionIdSchema } from './snapshot';

/**
 * 工作区注册表、目录选择器与单场状态的读端点（`docs/contract.md` 4.8 / 4.9）。
 *
 * - `GET /api/sessions`：**软件自己记的那份"我这些会"**——住在服务侧配置目录的
 *   `workspaces.json` 里、按路径去重。每一行的"目录还在不在、里面是什么"由服务现场判
 *   （注册表条目只记两个时刻），界面照读不派生；顶层第三格 `post_job` 是服务进程里
 *   在飞的那一趟会后（全局「显卡忙不忙」的会后一半读数，**不进任何一行**）；
 * - `GET /api/dirs`：目录选择器的现场（起点是用户主目录），一层子目录的名目判读；
 * - `GET /api/sessions/{id}`：单场状态（4.9）——这一场此刻跑到哪一档、会后离线
 *   跑到第几块 / 共几块。**不进快照、不进行**：快照九键与列表行十二键两道 `.strict()`
 *   硬门原样。
 *
 * 三条都是**纯读**：不涨 `revision`、不广播、不写任何东西。
 */

/** 一个目录的名目判读：三档 + `missing`。`suspected` 是"疑似会议目录"的兜底档。 */
export const dirKindSchema = z.enum(['empty', 'meeting', 'unknown', 'missing']);

/** 注册表里的一行：`dir` 是规范化的绝对路径（去重判据），时刻两格秒精度。 */
export const sessionSegmentCellSchema = z
  .object({
    /** 段号（一段 = 一次录制，从 1 起）。 */
    index: z.number().int().nullable(),
    started_at: z.string().nullable(),
    ended_at: z.string().nullable(),
    /** 这一段的在录时长（不含暂停）；还在录的那段没有段记录，如实 `null`。 */
    duration_seconds: z.number().nullable(),
  })
  .strict();

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
    /** 工作区状态，三档口径（契约 4.8）：`recording` 仅当这一行是本进程此刻在录的
     *  活跃场次；盘上有 `session.json` 一律 `ended`（收尾走到哪一步不进这个口径）；
     *  没有 `session.json`（空白工作区 / 尚未开过会）是 `null`。 */
    state: z.enum(['recording', 'ended']).nullable(),
    date: z.string().nullable(),
    mode: z.string().nullable(),
    /** 这场会各段的时长与时间（续接过的会看得出接过）；盘上没有段记录时是空数组。 */
    segments: z.array(sessionSegmentCellSchema),
    /** 这一场有没有会后精细稿（`meeting_offline_transcript_<source>.md` 在不在盘上）。
     *  R29：界面照它渲染「出纪要」按钮上那一格，**不自己猜、不去试文件**。 */
    has_offline: z.boolean(),
    added_at: isoStringSchema,
    last_opened_at: isoStringSchema,
  })
  .strict();

/**
 * 服务进程里在飞的那一趟活（`GET /api/sessions` 顶层第三格的成员）。三种：
 * `offline`（会后精细稿）/ `minutes`（纪要）/ `transcode`（转码独立工序）。
 * 全局「显卡忙不忙」的会后一半读数；**不进任何一行**（行十二键一字不动）。
 * 多槽登记（`(kind, dir)` 一趟一格）：可以同时不止一趟在飞。
 */
export const postJobCellSchema = z
  .object({
    kind: z.enum(['offline', 'minutes', 'transcode']),
    dir: z.string(),
    started_at: isoStringSchema,
  })
  .strict();

/** `GET /api/sessions` 的应答体。`recent` = 最近一次被打开/切换那条的目录（或 `null`）；
 *  `post_jobs` = 此刻在飞的那几趟活（多槽登记，空数组 = 没有在跑的）。 */
export const sessionsResponseSchema = z
  .object({
    sessions: z.array(sessionRowSchema),
    recent: z.string().nullable(),
    post_jobs: z.array(postJobCellSchema),
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

/**
 * 目录选择器现场的一项**普通文件**（`?files=1` 才有，资料界面的选择器文件模式要的，
 * 160d1a78）。`dir` 是文件的绝对路径（选中它 = 把这份盘上文件拷进资料）；`size_bytes`
 * 是现读的文件大小。不带那个参数时服务不摆这个键，旧应答体一个字节不动。
 */
export const dirFileRowSchema = z
  .object({
    name: z.string(),
    dir: z.string(),
    size_bytes: z.number().int(),
  })
  .strict();

/** `GET /api/dirs` 的应答体。`up` **到盘根才**为 `null`（向上一层唯一的到顶档；
 *  用户主目录不再是天花板）；`here` 是正在浏览这一层自己的名目判读——这一层本身
 *  可选，不必退到上一层再点那一行。`files` 只在 `?files=1` 那一档出现（一层里的
 *  普通文件，与 `dirs` 分开两列）。 */
export const dirsResponseSchema = z
  .object({
    home: z.string(),
    cwd: z.string(),
    up: z.string().nullable(),
    here: dirRowSchema,
    dirs: z.array(dirRowSchema),
    files: z.array(dirFileRowSchema).optional(),
  })
  .strict();

/**
 * `POST /api/dirs`（新建目录）的成功体：通用信封 + `directory` 键——新建那一行的
 * 名目判读（刚建出来是 `empty`）。界面拿它把新行插进清单并直接置为选定。
 * 写入独立于四态快照：`revision` 只是当前版本号（先例是资料四条端点）。
 */
export const dirCreateOkSchema = z
  .object({
    ok: z.literal(true),
    revision: z.number().int(),
    directory: dirRowSchema,
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

// ---------------------------------------------------------------------------
// 单场状态（`docs/contract.md` 4.9，会后进度界面节点 52b24392 接的取数）
// ---------------------------------------------------------------------------

/**
 * 这一场的一趟在飞/已收工的活（`GET /api/sessions/{id}` 的 `post` 清单成员）：
 * `(kind, dir)` 多槽登记里**这一场**的那几趟 —— `running` 按线程活没活着答。
 */
export const sessionPostViewSchema = z
  .object({
    running: z.boolean(),
    kind: z.enum(['offline', 'minutes']),
    dir: z.string(),
    started_at: isoStringSchema,
    finished_at: isoStringSchema.nullable(),
  })
  .strict();

/** 一路的逐块读数：分母是**这一路**切了几块，两路各算各的、不合成整场分母。 */
export const offlineSourceProgressSchema = z
  .object({
    source: z.enum(['mic', 'loopback']),
    /** 跑过几块（四档都算「跑过一块」）。 */
    done: z.number().int(),
    /** 这一路共几块；切块计划算不出来时如实 `null`。 */
    total: z.number().int().nullable(),
    remaining: z.number().int().nullable(),
    /** 此刻在跑第几块（这一路跑完、或还没轮到它时是 `null`）。 */
    current_chunk: z.number().int().nullable(),
    counts: z
      .object({
        transcribed: z.number().int(),
        silent: z.number().int(),
        failed: z.number().int(),
        unjudged: z.number().int(),
      })
      .strict(),
  })
  .strict();

/** 会后离线的进度读数：`state` 照盘上那份答、`in_process` 另答「本进程在不在跑」。 */
export const offlineProgressSchema = z
  .object({
    /** 这一场那份逐块记录的文件名（`meeting_offline_progress.jsonl`）。 */
    ledger: z.string(),
    /** `interrupted` 是「跑到块边界被人停」那一档的终态（b1c6a58e，R28）：照盘上如实答。 */
    state: z.enum(['running', 'done', 'partial', 'failed', 'interrupted', 'idle', 'ledger']),
    in_process: z.boolean(),
    started_at: isoStringSchema.nullable(),
    finished_at: isoStringSchema.nullable(),
    /** 跨进程算不出单调钟的差值，这一格如实 `null`。 */
    elapsed_seconds: z.number().nullable(),
    current_source: z.enum(['mic', 'loopback']).nullable(),
    sources: z.array(offlineSourceProgressSchema),
  })
  .strict();

/**
 * 这一场盘上的一个说话人身份（4.9 / 4.11，人名节点 `91b6752e` 接的取数）。
 *
 * `key` 带着路（`mic:S01` 这种限定名，或没跑过聚类时的路本身），两路的「1 号」
 * 写不出同一个键；`name` 来自这一场目录里的人名表——没起过名或人名表读不出时
 * 如实 `null`（界面照它画「待确认」）；`segments` 是离线对照表里可得的次数读数，
 * 路级身份如实 `null`。
 */
export const speakerIdentitySchema = z
  .object({
    key: z.string(),
    source: z.enum(['mic', 'loopback']),
    label: z.string(),
    name: z.string().nullable(),
    segments: z.number().int().nullable(),
  })
  .strict();

/** 一路的转码工序读数：分母是**这一路**的段数（一段 = 一次录制），两路各算各的。 */
export const transcodeSourceProgressSchema = z
  .object({
    source: z.enum(['mic', 'loopback']),
    /** 凭据认账的段数（转好的与空段的和）。 */
    done: z.number().int(),
    /** 这一路共几段；工序还没排出的那一场如实 `null`（现行为总给得出计划数）。 */
    total: z.number().int().nullable(),
    remaining: z.number().int().nullable(),
    /** 此刻在转第几段（这一路转完、或还没轮到它时是 `null`）。 */
    current_segment: z.number().int().nullable(),
  })
  .strict();

/**
 * 转码独立工序的进度读数（4.9 的 `transcode` 格）：`state` 照盘上那份状态文件答、
 * `in_process` 另答「本进程里此刻有没有这道工序」—— 与 `offline` 那一格同一套读法。
 * **判据读盘**（凭据 + `meeting_transcode_status.json`）：服务重启之后照旧读得到。
 */
export const transcodeProgressSchema = z
  .object({
    /** `paused` 是「跑到段边界被人暂停」那一档；盘上停着一版 `running` 而没人跑也照它答。 */
    state: z.enum(['running', 'paused', 'done', 'partial', 'failed', 'idle']),
    in_process: z.boolean(),
    started_at: isoStringSchema.nullable(),
    finished_at: isoStringSchema.nullable(),
    /** 跨进程算不出单调钟的差值，这一格如实 `null`（只在本进程在飞时给）。 */
    elapsed_seconds: z.number().nullable(),
    current_source: z.enum(['mic', 'loopback']).nullable(),
    sources: z.array(transcodeSourceProgressSchema),
  })
  .strict();

/**
 * `GET /api/sessions/{id}` 的应答体（4.9）：前八格与列表同一行同源，后四格回答
 * 「这一场此刻跑到哪儿了、这一场的说话人有谁」。**它不进快照、也不进列表行** ——
 * 快照九键与行十二键两道 `.strict()` 硬门一个字没动。
 */
export const sessionStatusSchema = z
  .object({
    dir: z.string(),
    session_id: sessionIdSchema.nullable(),
    kind: z.enum(['empty', 'meeting', 'unknown']),
    /** 三档口径，与 `GET /api/sessions` 那一行同一格（前八格同源，契约 4.9）。 */
    state: z.enum(['recording', 'ended']).nullable(),
    date: z.string().nullable(),
    mode: z.string().nullable(),
    segments: z.array(sessionSegmentCellSchema),
    has_offline: z.boolean(),
    /** `(kind, dir)` 多槽登记里这一场的在飞清单（空数组 = 没有这一趟的活）。 */
    post: z.array(sessionPostViewSchema),
    offline: offlineProgressSchema,
    transcode: transcodeProgressSchema,
    speakers: z.array(speakerIdentitySchema),
  })
  .strict();

export type DirKind = z.infer<typeof dirKindSchema>;
export type SessionSegmentCell = z.infer<typeof sessionSegmentCellSchema>;
export type SessionRow = z.infer<typeof sessionRowSchema>;
export type SessionsResponse = z.infer<typeof sessionsResponseSchema>;
export type DirRow = z.infer<typeof dirRowSchema>;
export type DirFileRow = z.infer<typeof dirFileRowSchema>;
export type DirsResponse = z.infer<typeof dirsResponseSchema>;
export type DirCreateOk = z.infer<typeof dirCreateOkSchema>;
export type WorkspaceOk = z.infer<typeof workspaceOkSchema>;
export type PostJobCell = z.infer<typeof postJobCellSchema>;
export type SessionPostView = z.infer<typeof sessionPostViewSchema>;
export type OfflineSourceProgress = z.infer<typeof offlineSourceProgressSchema>;
export type OfflineProgress = z.infer<typeof offlineProgressSchema>;
export type SpeakerIdentity = z.infer<typeof speakerIdentitySchema>;
export type SessionStatus = z.infer<typeof sessionStatusSchema>;
