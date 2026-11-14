import { z } from 'zod';
import { isoStringSchema, sessionIdSchema } from './snapshot';

/**
 * 工作区注册表、目录选择器与单场状态的读端点。
 *
 * 三条都是**纯读**：不涨 `revision`、不广播、不写任何东西。
 */

export const dirKindSchema = z.enum(['empty', 'meeting', 'unknown', 'missing']);

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

/** 注册表里的一行：`dir` 是规范化的绝对路径（去重判据）。 */
export const sessionRowSchema = z
  .object({
    dir: z.string(),
    /** 盘上 `session.json` 的目录名；没开过会时是 `null`，界面那一句才拿 `dir` 兜。 */
    session_id: sessionIdSchema.nullable(),
    dir_exists: z.boolean(),
    kind: dirKindSchema,
    suspected: z.boolean(),
    state: z.enum(['recording', 'ended']).nullable(),
    date: z.string().nullable(),
    mode: z.string().nullable(),
    segments: z.array(sessionSegmentCellSchema),
    /** 这一场有没有会后精细稿；界面照它渲染「出纪要」按钮上那一格，**不自己猜、不去试文件**。 */
    has_offline: z.boolean(),
    added_at: isoStringSchema,
    last_opened_at: isoStringSchema,
  })
  .strict();

/**
 * 服务进程里在飞的那一趟活（`GET /api/sessions` 顶层第三格的成员）：
 * `offline` / `minutes` / `transcode`。
 *
 * 全局「显卡忙不忙」的会后一半读数；**不进任何一行**。多槽登记（`(kind, dir)` 一趟一格）：
 * 可以同时不止一趟在飞。
 */
export const postJobCellSchema = z
  .object({
    kind: z.enum(['offline', 'minutes', 'transcode']),
    dir: z.string(),
    started_at: isoStringSchema,
  })
  .strict();

/** `sessions` 按「最近在用」倒序 —— **服务排好给出**，界面照应答次序渲染、自己不重排。 */
export const sessionsResponseSchema = z
  .object({
    sessions: z.array(sessionRowSchema),
    recent: z.string().nullable(),
    post_jobs: z.array(postJobCellSchema),
  })
  .strict();

export const dirRowSchema = z
  .object({
    name: z.string(),
    dir: z.string(),
    kind: dirKindSchema,
    suspected: z.boolean(),
  })
  .strict();

/**
 * 目录选择器现场的一项**普通文件**（`?files=1` 才有）。`dir` 是文件的绝对路径
 * （选中它 = 把这份盘上文件拷进资料）。不带那个参数时服务不摆这个键。
 */
export const dirFileRowSchema = z
  .object({
    name: z.string(),
    dir: z.string(),
    size_bytes: z.number().int(),
  })
  .strict();

/** `up` **到盘根才**为 `null`；`here` 是正在浏览这一层自己的名目判读 —— 这一层本身可选，
 *  不必退到上一层再点那一行。`files` 只在 `?files=1` 那一档出现。 */
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

/** `POST /api/dirs`（新建目录）的成功体：通用信封 + `directory` 键。写入独立于四态快照：
 *  `revision` 只是当前版本号。 */
export const dirCreateOkSchema = z
  .object({
    ok: z.literal(true),
    revision: z.number().int(),
    directory: dirRowSchema,
  })
  .strict();

/**
 * `workspace_open` / `workspace_remove` 的成功体：通用信封 + 一个 `workspace` 键。
 *
 * 两档形状并成一个 schema：`open` 给 `kind` / `suspected` / `first_time`，`remove` 给
 * `removed` —— 对面的格子在另一档缺席（服务不摆没有数的空格），所以这四格全部 `optional()`。
 * 分派的读法：`removed` 在 → 是 `remove` 的回执；`kind` 在 → 是 `open` 的判读。
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

/** 这一场的一趟在飞/已收工的活：`running` 按线程活没活着答。 */
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
    ledger: z.string(),
    /** `interrupted` 是「跑到块边界被人停」那一档的终态：照盘上如实答。 */
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
 * 这一场盘上的一个说话人身份。
 *
 * `key` 带着路（`mic:S01` 这种限定名，或没跑过聚类时的路本身），两路的「1 号」写不出同一个
 * 键；`name` 来自这一场目录里的人名表 —— 没起过名或人名表读不出时如实 `null`；`segments`
 * 是离线对照表里可得的次数读数，路级身份如实 `null`。
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
    /** 这一路共几段；工序还没排出的那一场如实 `null`。 */
    total: z.number().int().nullable(),
    remaining: z.number().int().nullable(),
    /** 此刻在转第几段（这一路转完、或还没轮到它时是 `null`）。 */
    current_segment: z.number().int().nullable(),
  })
  .strict();

/**
 * 转码独立工序的进度读数：`state` 照盘上那份状态文件答、`in_process` 另答「本进程里此刻
 * 有没有这道工序」—— 与 `offline` 那一格同一套读法。**判据读盘**（凭据 +
 * `meeting_transcode_status.json`）：服务重启之后照旧读得到。
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
 * 纪要那一层的盘上读数：与 `transcode` 同一套读法 —— **判据读盘**
 * （`meeting_minutes_status.json`，读方逐字段校验），服务重启之后照旧读得到。
 *
 * 「本进程里此刻有没有这一趟」不另立一格：`post` 清单里 `kind: 'minutes'` 的那一项带着
 * `running`。状态文件不在、读不动或形状不合契约时服务如实答 `idle`。
 */
export const minutesProgressSchema = z
  .object({
    /** `delivered` 是「已交付 DSH web 服务端生成」那一档（`done` 说的是产物校验过、在盘上）。 */
    state: z.enum(['running', 'done', 'failed', 'delivered', 'idle']),
    started_at: isoStringSchema.nullable(),
    finished_at: isoStringSchema.nullable(),
    /** 盘上 `running` 那一版落的是空串收尾戳，服务折成 `null`（不摆没有数的空格）。 */
    error: z.string().nullable(),
  })
  .strict();

/**
 * 一路的一份稿：**只回答两件事之一** —— 文件在 → `text` 带正文原样；文件不在 → `reason`
 * 带缺稿的那一档。`unrecorded` = 这一场没采这一路；`absent` = 采了这一路但盘上没有这一份。
 */
export const transcriptSourceCellSchema = z
  .object({
    source: z.enum(['mic', 'loopback']),
    text: z.string().optional(),
    reason: z.enum(['unrecorded', 'absent']).optional(),
  })
  .strict();

export const transcriptKindCellSchema = z
  .object({
    /** `live` = 会中稿、`offline` = 会后稿 —— 两份**平级**。 */
    kind: z.enum(['live', 'offline']),
    sources: z.array(transcriptSourceCellSchema),
  })
  .strict();

export const sessionTranscriptsSchema = z
  .object({
    dir: z.string(),
    kinds: z.array(transcriptKindCellSchema),
  })
  .strict();

/**
 * `GET /api/sessions/{id}` 的应答体：**它不进快照、也不进列表行** —— 快照九键与行十二键
 * 两道 `.strict()` 硬门一个字没动。
 */
export const sessionStatusSchema = z
  .object({
    dir: z.string(),
    session_id: sessionIdSchema.nullable(),
    kind: z.enum(['empty', 'meeting', 'unknown']),
    state: z.enum(['recording', 'ended']).nullable(),
    date: z.string().nullable(),
    mode: z.string().nullable(),
    segments: z.array(sessionSegmentCellSchema),
    has_offline: z.boolean(),
    /** `(kind, dir)` 多槽登记里这一场的在飞清单（空数组 = 没有这一趟的活）。 */
    post: z.array(sessionPostViewSchema),
    offline: offlineProgressSchema,
    transcode: transcodeProgressSchema,
    minutes: minutesProgressSchema,
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
export type MinutesProgress = z.infer<typeof minutesProgressSchema>;
export type TranscriptSourceCell = z.infer<typeof transcriptSourceCellSchema>;
export type TranscriptKindCell = z.infer<typeof transcriptKindCellSchema>;
export type SessionTranscripts = z.infer<typeof sessionTranscriptsSchema>;
export type SpeakerIdentity = z.infer<typeof speakerIdentitySchema>;
export type SessionStatus = z.infer<typeof sessionStatusSchema>;
