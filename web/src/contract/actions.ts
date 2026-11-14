import { z } from 'zod';
import { SUMMARY_WINDOW_CHOICES } from './primitives';
import { errorCodeSchema } from './enums';
import { isoStringSchema } from './snapshot';
import { workspaceOkSchema } from './sessions';

/**
 * 九个 `code` 各自的 `detail` **键集合**。
 *
 * 三档 / 四档 / 五档 / 两档的 `code` 给 `null`：**逐档的键集合不是唯一的一份**，由各档的样本
 * 自己说话。独档的六个 `code` 给精确集合，多一个少一个都红。
 */
export const ACTION_DETAIL_KEYS: Readonly<
  Record<z.infer<typeof errorCodeSchema>, readonly string[] | null>
> = {
  already_recording: ['session_id', 'dir', 'elapsed_seconds'],
  draining: null,
  capture_unavailable: null,
  not_recording: [],
  invalid_setting: null,
  persist_failed: ['error'],
  action_unavailable: [],
  transcript_unavailable: ['health'],
  summary_failed: null,
};

/**
 * 多档 `code` 的**逐档键集合**。
 *
 * 样本与这里逐档对齐，于是「服务少产出一档」或「某一档换了键」都会红。
 */
export const ACTION_DETAIL_VARIANTS: Readonly<
  Record<string, readonly (readonly string[])[]>
> = {
  capture_unavailable: [
    [],
    ['session_id', 'dir', 'error'],
    ['session_id', 'dir', 'reason'],
    ['source', 'error'],
  ],
  draining: [
    ['session_id', 'dir', 'failed_steps', 'log_path'],
    ['kind', 'dir', 'status_path', 'reason'],
    ['kind', 'dir', 'started_at'],
    ['top_state', 'session_id', 'dir'],
    [],
  ],
  invalid_setting: [
    ['key', 'value', 'legal'],
    ['source', 'fault', 'error'],
    [],
    ['action', 'known'],
  ],
  summary_failed: [['missing'], ['elapsed_seconds', 'attempts']],
};

export const actionArgsSchema = z.record(z.unknown());
export type ActionArgs = z.infer<typeof actionArgsSchema>;

export const actionOkSchema = z
  .object({
    ok: z.literal(true),
    revision: z.number().int(),
  })
  .strict();

/** 两个数组里是同一套点号键名，次序照请求里的键逐条走。 */
export const settingsUpdateOkSchema = z
  .object({
    ok: z.literal(true),
    revision: z.number().int(),
    applied: z.array(z.string()),
    pending_restart: z.array(z.string()),
  })
  .strict();

/**
 * **空数组合法**：窗口里一行都没有时三段都是空数组、`ok` 照旧为真，空态文案由界面给。
 * 因此这里必须是 `z.array()` 而不是「至少一条」。
 */
export const summarySectionsSchema = z.object({
  what: z.array(z.string()),
  where: z.array(z.string()),
  lost: z.array(z.string()),
});

/**
 * `summarize` 成功体里那个 `summary` 子对象。
 *
 * 比 `GET /api/summaries` 的记录条目多一格 `lines`。两处是**两个形状**，别合并成一个。
 */
export const summarizeResultSchema = z
  .object({
    window: z.string(),
    window_label: z.string(),
    at: isoStringSchema,
    elapsed_seconds: z.number(),
    lines: z.number().int(),
    what: z.array(z.string()),
    where: z.array(z.string()),
    lost: z.array(z.string()),
  })
  .strict();

/**
 * `summarize` 的成功体：通用信封 + 一个 `summary` 键。
 *
 * `revision` 是**当前**版本号 —— 总结是只读的，它不涨、不广播。
 */
export const summarizeOkSchema = z
  .object({
    ok: z.literal(true),
    revision: z.number().int(),
    summary: summarizeResultSchema,
  })
  .strict();

/**
 * 会后两个按钮（`post_offline` / `post_minutes`）与停掉那一趟（`post_stop`）的成功体：
 * 通用信封 + 一个 `post` 键。
 *
 * 回执里**没有**「跑完了没有」这一格 —— 那一趟可能跑一小时，跑成什么样由这一场盘上的
 * 状态文件回答。`stopping` 只有 `post_stop` 的受理回执才带。
 *
 * `inputs` 是**受理那一刻**读到的输入档位，**恒在**；`note` 是「同场的会后精细稿还在跑」
 * 时的说破句，只在那一档出现。一个 schema 罩两个 `kind`，两格因此都是 `optional()`。
 */
export const postOkSchema = z
  .object({
    ok: z.literal(true),
    revision: z.number().int(),
    post: z
      .object({
        kind: z.enum(['offline', 'minutes']),
        dir: z.string(),
        stopping: z.boolean().optional(),
        inputs: z.enum(['both', 'live_only', 'offline_only', 'none']).optional(),
        note: z.string().optional(),
      })
      .strict(),
  })
  .strict();

/**
 * 转码三动作（`transcode_run` / `transcode_pause` / `transcode_resume`）的成功体：
 * 通用信封 + 一个 `transcode` 键。
 *
 * `started` / `pausing` / `resumed` 是三档各自的旗标，哪个在就是哪一档；服务不摆没有数的
 * 空格。跑成什么样由场次目录里的 `meeting_transcode_status.json` 回答。
 */
export const transcodeOkSchema = z
  .object({
    ok: z.literal(true),
    revision: z.number().int(),
    transcode: z
      .object({
        kind: z.literal('transcode'),
        dir: z.string(),
        started: z.boolean().optional(),
        pausing: z.boolean().optional(),
        resumed: z.boolean().optional(),
      })
      .strict(),
  })
  .strict();

/**
 * 说话人改名（`speaker_rename`）的成功体：通用信封 + 一个 `speaker` 键。
 *
 * `revision` 保持现值——这只动作改的只有这一场目录里的人名表，快照一个字不动；界面上
 * 当场生效走重问单场状态那一跳。
 */
export const speakerOkSchema = z
  .object({
    ok: z.literal(true),
    revision: z.number().int(),
    speaker: z
      .object({
        dir: z.string(),
        key: z.string(),
        name: z.string(),
      })
      .strict(),
  })
  .strict();

/**
 * 逐档 `detail` 形状。`detail` **恒是对象** —— 没有补充信息时给**空对象 `{}`**，所以基档就是
 * `{}`，各档在它上面补键，补出来的键一律 `optional()`。
 *
 * **契约已列的键收可选校验，契约之外的键原样放行**（`catchall(z.unknown())`）：`.strict()`
 * 在这里收错了地方 —— 它把「将来某个 `code` 补一个新 detail 键」变成前端的 parse 失败，
 * 而契约对那种情况的期望恰恰是「多出来的键安静地不理，已列的键照画」。
 *
 * 两层分工：运行时宽容（这个 schema），样本层精确（键集合用例对契约漂移要红）。
 */
export const describeFailure = z
  .object({
    session_id: z.string().optional(),
    dir: z.string().optional(),
    elapsed_seconds: z.number().optional(),
    failed_steps: z
      .array(
        z
          .object({
            id: z.string(),
            error: z.string(),
          })
          .strict(),
)
      .optional(),
    log_path: z.string().optional(),
    error: z.string().optional(),
    reason: z.string().optional(),
    kind: z.enum(['offline', 'minutes']).optional(),
    status_path: z.string().optional(),
    key: z.string().optional(),
    value: z.unknown().optional(),
    legal: z.array(z.unknown()).optional(),
    source: z.string().optional(),
    fault: z.string().optional(),
    action: z.string().optional(),
    known: z.array(z.string()).optional(),
    health: z.string().optional(),
    missing: z.array(z.string()).optional(),
    attempts: z.number().int().optional(),
  })
  .catchall(z.unknown());

/** 失败信封 `{ok: false, code, message, detail}`：`code` 固定九个，界面**不按 `message` 做字符串匹配**。 */
export const actionErrorSchema = z
  .object({
    ok: z.literal(false),
    code: errorCodeSchema,
    message: z.string(),
    detail: describeFailure,
  })
  .strict();

export const actionBodySchema = z.union([
  summarizeOkSchema,
  workspaceOkSchema,
  postOkSchema,
  transcodeOkSchema,
  speakerOkSchema,
  settingsUpdateOkSchema,
  actionOkSchema,
  actionErrorSchema,
]);

export const actionFixtureSchema = z
  .object({
    action: z.string(),
    args: z.record(z.unknown()),
    status: z.number().int(),
    body: actionBodySchema,
  })
  .strict();

export type ActionOk = z.infer<typeof actionOkSchema>;
export type PostOk = z.infer<typeof postOkSchema>;
export type TranscodeOk = z.infer<typeof transcodeOkSchema>;
export type SpeakerOk = z.infer<typeof speakerOkSchema>;
export type SettingsUpdateOk = z.infer<typeof settingsUpdateOkSchema>;
export type SummarySections = z.infer<typeof summarySectionsSchema>;
export type SummarizeResult = z.infer<typeof summarizeResultSchema>;
export type SummarizeOk = z.infer<typeof summarizeOkSchema>;
export type ActionDetail = z.infer<typeof describeFailure>;
export type ActionError = z.infer<typeof actionErrorSchema>;
export type ActionBody = z.infer<typeof actionBodySchema>;
export type ActionFixture = z.infer<typeof actionFixtureSchema>;
export type ErrorCode = z.infer<typeof errorCodeSchema>;
export type SummaryWindowChoice = (typeof SUMMARY_WINDOW_CHOICES)[number];
