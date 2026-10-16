import { z } from 'zod';
import { SUMMARY_WINDOW_CHOICES } from './primitives';
import { errorCodeSchema } from './enums';
import { isoStringSchema } from './snapshot';
import { workspaceOkSchema } from './sessions';

/**
 * 动作信封（`docs/contract.md` 第 4 章）。
 *
 * `contract/fixtures/actions/*.json` 每份是「请求三件套 + 应答」：
 * `{action, args, status, body}`。`action` / `args` 是打过去的那份请求，
 * `status` 是 HTTP 状态码，`body` 是掩码后的应答体。
 */

/** 请求信封：`{action, args}`。`args` 是对象，省略时按 `{}` 处理。 */
export const actionRequestSchema = z
  .object({
    action: z.string(),
    args: z.record(z.unknown()).default({}),
  })
  .strict();

/**
 * 九个 `code` 各自的 `detail` **键集合**（`docs/contract.md` 4.4 的表逐条抄下来）。
 *
 * 这是**样本层漂移检测的判据**（见 `describeFailure` 上「两层分工」那一段）：运行时
 * parse 对契约外的键宽容，漂移由这里盯着。集合里的名字一个不多一个不少 ——
 * 样本多一个键、少一个键都要红。
 *
 * 逐条对着 4.4 的表：
 *
 * - `already_recording`：`{session_id, dir, elapsed_seconds}`；
 * - `draining` **五档**：`{session_id, dir, failed_steps, log_path}`（`start` 那一档）、
 *   `{kind, dir, status_path, reason}`（纪要那一层的状态停在 `running`：重复按「直接出纪要」
 *   拿到的是这一档，见 `docs/contract.md` 4.3）、`{kind, dir, started_at}`（已经有一趟
 *   会后活在跑）、`{top_state, session_id, dir}`（还在录音中）、空对象
 *   （`post_stop` 没有在飞的会后可停）—— 五档并起来是这九个键；
 * - `capture_unavailable` **四档**：空对象；`{session_id, dir, error}`（第一版
 *   `session.json` 写不进去）；`{session_id, dir, reason}`（采集起不来）；
 *   `{source, error}`（`set_source` 重新打开某一路时那个设备打不开）**并起来**是这五个键
 *   （`dir` / `error` / `reason` / `session_id` / `source`）—— 样本按它自己那一档写，
 *   各档的键都是这五个的子集；
 * - `not_recording`：空对象；
 * - `invalid_setting` 四档（`{key, value, legal}` / `{source, fault, error}` / 空 /
 *   `{action, known}`）并起来是这八个键；
 * - `persist_failed`：`{error}`；
 * - `action_unavailable`：空对象；
 * - `transcript_unavailable`：`{health}`（恒这一格）；
 * - `summary_failed` 两档（`{missing}` / `{elapsed_seconds, attempts}`）并起来是这三个键。
 *
 * 三档 / 四档 / 五档 / 两档的 `code` 给 `null`：**逐档的键集合不是唯一的一份**，由各档的样本自己
 * 说话（用例因此对这几档只断言「样本的键 ⊆ 并集、且每档各是哪一个子集都写死在用例里」）。
 * 独档的六个 `code` 给精确集合，多一个少一个都红。
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
 * 多档 `code` 的**逐档键集合**（4.4 表里那些「**三档**」「**四档**」「**两档**」的分档）。
 *
 * 键是 `code`，值是一串档位 —— 每一档一个键集合。样本与这里逐档对齐，
 * 于是「服务少产出一档」或「某一档换了键」都会红。
 */
export const ACTION_DETAIL_VARIANTS: Readonly<
  Record<string, readonly (readonly string[])[]>
> = {
  // 四档：空对象；第一版 session.json 写不进去；采集起不来；set_source 重开时设备打不开。
  capture_unavailable: [
    [],
    ['session_id', 'dir', 'error'],
    ['session_id', 'dir', 'reason'],
    ['source', 'error'],
  ],
  // 照 docs/contract.md 4.4 的五档逐条抄：start 那一档（没有失败步时 failed_steps 是空数组）；
  // 纪要那一层的状态停在 running（重复按「直接出纪要」）；已经有一趟会后活在跑（post_stop
  // 撞上纪要在飞也是这一档）；还在录音中；post_stop 没有在飞的会后可停（空对象）。
  draining: [
    ['session_id', 'dir', 'failed_steps', 'log_path'],
    ['kind', 'dir', 'status_path', 'reason'],
    ['kind', 'dir', 'started_at'],
    ['top_state', 'session_id', 'dir'],
    [],
  ],
  // 值非法；dev_inject 的取值非法；空；未知动作名。
  invalid_setting: [
    ['key', 'value', 'legal'],
    ['source', 'fault', 'error'],
    [],
    ['action', 'known'],
  ],
  // 端点没配齐（点名缺哪几个键）；预算用尽 / 端点报错 / 解析连试带败（等了多久、试了几次）。
  summary_failed: [['missing'], ['elapsed_seconds', 'attempts']],
};

/** 动作名 → 参数值的扁平映射（`settings_update` 用的就是这一层）。 */
export const actionArgsSchema = z.record(z.unknown());
export type ActionArgs = z.infer<typeof actionArgsSchema>;

/**
 * 成功体 `{ok: true, revision}`。
 *
 * `settings_update` 例外：它的成功体多 `applied` / `pending_restart` 两数组、
 * **没有 `message` 字段**（4.6），因此单独给一个 schema。
 */
export const actionOkSchema = z
  .object({
    ok: z.literal(true),
    revision: z.number().int(),
  })
  .strict();

/** `settings_update` 的成功体。两个数组里是同一套点号键名，次序照请求里的键逐条走。 */
export const settingsUpdateOkSchema = z
  .object({
    ok: z.literal(true),
    revision: z.number().int(),
    applied: z.array(z.string()),
    pending_restart: z.array(z.string()),
  })
  .strict();

/**
 * 三小段：一段几条字符串（`docs/contract.md` 4.7）。
 *
 * **空数组合法**（B15 的口径）：窗口里一行都没有时三段都是空数组、`ok` 照旧为真，
 * 空态文案由界面给。因此这里必须是 `z.array()` 而不是「至少一条」。
 */
export const summarySectionsSchema = z.object({
  /** 刚才在讲什么。 */
  what: z.array(z.string()),
  /** 讲到哪儿了。 */
  where: z.array(z.string()),
  /** 丢掉了什么。 */
  lost: z.array(z.string()),
});

/**
 * `summarize` 成功体里那个 `summary` 子对象 —— **八格**（4.7）。
 *
 * 比 `GET /api/summaries` 的记录条目多一格 `lines`（窗口里的行数）：应答体这一份多报一个
 * 「我读了 N 行」，记录那一份按 4.7 写死的形状没有它。两处是**两个形状**，别合并成一个。
 */
export const summarizeResultSchema = z
  .object({
    window: z.string(),
    /** 人话标签：`5 分钟` / `15 分钟` / `30 分钟` / `整场`（服务给，界面照原样用）。 */
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
 * `summarize` 的成功体：通用信封 + 一个 `summary` 键（先例是 `settings_update` 的
 * `applied` / `pending_restart`）。`revision` 是**当前**版本号 —— 总结是只读的，
 * 它不涨、不广播（4.7）。
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
 * 这一格说的是「这一趟跑的是**哪一场**、跑的**哪一趟**」（`{kind, dir}`），回执里
 * **没有**「跑完了没有」这一格 —— 那一趟可能跑一小时，跑成什么样由这一场盘上的
 * 状态文件回答（`meeting_offline_status.json` / `meeting_minutes_status.json`，
 * 见 `docs/requirements.md` R26 与 R27）。`stopping` 只有 `post_stop` 的受理回执才带
 * （`true` = 旗标已立，线程在下一块开跑前收场；起跑那两只动作没有这一格）。
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
      })
      .strict(),
  })
  .strict();

/**
 * 说话人改名（`speaker_rename`，4.11）的成功体：通用信封 + 一个 `speaker` 键。
 *
 * 回执写的是「写了哪一场、哪一个键、现在叫什么」。`revision` 保持现值——这只动作
 * 改的只有这一场目录里的人名表，快照一个字不动；界面上当场生效走重问单场状态那一跳
 * （`sessions.ts` 的 `sessionStatusSchema.speakers`）。
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
 * 逐档 `detail` 形状（4.4）。`detail` **恒是对象** —— 没有补充信息时给**空对象 `{}`**，
 * 所以基档就是 `{}`，各档在它上面补键。补出来的键一律 `optional()`：
 * `capture_unavailable` 有四档、`invalid_setting` 有四档，界面按**有哪个键就显示哪个键**来画。
 *
 * **契约已列的键收可选校验，契约之外的键原样放行**（`catchall(z.unknown())`）。
 * 这样收放的理由来自 6.7 与 4.4 的配合：
 *
 * - 6.7 是界面的读法 —— 「按 `detail` **有哪个键就显示哪个键**」。它把 `detail` 描述成
 *   一份「有补充信息就逐键带着」的开放袋子，界面只认自己会画的那几把键。
 * - 4.4 的档位表记的是**当前**形状 —— 这一轮服务产出的就是表里那几档。表随后跟着
 *   服务一起长，它是此刻的快照。
 *
 * 于是 `.strict()` 在这里收错了地方：它把「将来某个 `code` 补一个新 detail 键」变成
 * 前端的 parse 失败（`ActionProtocolError`），而契约对那种情况的期望恰恰是「多出来的键
 * 安静地不理，已列的键照画」。假故障会盖住真故障，这是 6.7 明说不要的。
 *
 * `catchall(z.unknown())` 正好把这条边界画在契约上：
 *
 * - **已列的 17 个键照旧逐个可选取值校验** —— `session_id: 123` 这类错型仍然被拒
 *   （`catchall` 只管 `shape` 之外的键，碰不到列出来的那些）；
 * - **契约外的键不论值是什么都放过**，不再因为服务多带一个字段就整帧报废。
 *
 * 注意这与本文件其它 schema 的 `.strict()` 分工不同：`actionOkSchema` /
 * `settingsUpdateOkSchema` / `actionErrorSchema` 的主体是**封闭形状**（4.2、4.6 逐键写明，
 * 多一个键就说明契约漂了），那几处继续严格。
 *
 * ## 两层分工：运行时宽容，样本层精确
 *
 * 「放行契约外的键」与「盯住漂移」是**两件事**，它们落在**两层**上，谁也替不了谁：
 *
 * | 层 | 在哪 | 收放 | 为什么 |
 * |---|---|---|---|
 * | **运行时** | 这个 schema | 契约外键**放行** | 服务给某个 `code` 补一个 detail 键属于**加性变更**，前端不该因此整条报错（6.7：有哪个键显示哪个键） |
 * | **样本层** | `tests/contract-guards.test.ts` 的键集合用例 | 契约外键**要红** | 4.4 的表是**此刻**的形状；样本是采集产物，它与表对不上就说明契约漂了 —— 这件事必须有人喊 |
 *
 * 先前只有上一层，于是运行时的宽容把漂移一起吞了（多一个键、少一个键都静悄悄地过）。
 * 把精确那一半挪到样本层，运行时保住宽容、漂移检测也回来了：**样本多一个键或
 * 少一个键都红**，而真服务多带一个字段时界面照常画。
 *
 * 样本层这条断言的判据来自 4.4 的表格，落点在 `ACTION_DETAIL_KEYS`（见下）。
 */
export const describeFailure = z
  .object({
    // already_recording: {session_id, dir, elapsed_seconds}
    // capture_unavailable 第一/三档: {session_id, dir, error|reason}
    // draining 第一档: {session_id, dir, failed_steps, log_path}
    session_id: z.string().optional(),
    dir: z.string().optional(),
    elapsed_seconds: z.number().optional(),
    /** draining 档：没有失败步时是空数组。 */
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
    // capture_unavailable 第二档（第一版 session.json 写不进去）
    error: z.string().optional(),
    // capture_unavailable 第三档（采集起不来）与 draining 第二档（纪要那一层的状态停在 running）
    // 共用这一格
    reason: z.string().optional(),
    // draining 第二档：这一趟跑的是哪一层、那一层的状态文件落在哪、既有那一版为什么拒跑
    kind: z.enum(['offline', 'minutes']).optional(),
    status_path: z.string().optional(),
    // invalid_setting 第一档（值非法）
    key: z.string().optional(),
    value: z.unknown().optional(),
    legal: z.array(z.unknown()).optional(),
    // invalid_setting 第二档（dev_inject 的取值非法）
    source: z.string().optional(),
    fault: z.string().optional(),
    // invalid_setting 第四档（未知动作名）
    action: z.string().optional(),
    known: z.array(z.string()).optional(),
    // transcript_unavailable（转写停了 / 没起过）：恒这一格
    health: z.string().optional(),
    // summary_failed 第一档（模型端点没配齐，点名缺哪几个配置键）
    missing: z.array(z.string()).optional(),
    // summary_failed 第二档（预算用尽 / 端点报错 / 解析连试带败）
    attempts: z.number().int().optional(),
  })
  .catchall(z.unknown());

/**
 * 失败信封 `{ok: false, code, message, detail}`。
 *
 * `code` 固定九个；`message` 直接能显示，但界面**不按它做字符串匹配**，
 * 选画法的依据是 `code`、补细节的依据是 `detail` 里有哪些键（第 6 章第 7 条）。
 */
export const actionErrorSchema = z
  .object({
    ok: z.literal(false),
    code: errorCodeSchema,
    message: z.string(),
    detail: describeFailure,
  })
  .strict();

/** 应答体：成功或失败，二选一。`workspace_open` / `workspace_remove` 的成功体带 `workspace` 键、`speaker_rename` 带 `speaker` 键（4.8 / 4.11）。 */
export const actionBodySchema = z.union([
  summarizeOkSchema,
  workspaceOkSchema,
  postOkSchema,
  speakerOkSchema,
  settingsUpdateOkSchema,
  actionOkSchema,
  actionErrorSchema,
]);

/**
 * 动作样本整份：`{action, args, status, body}`（`contract/README.md` 第 1 章）。
 * `status` 是 HTTP 状态码 —— 与 `code` 一起看，同一个 `code` 的状态码是固定的。
 */
export const actionFixtureSchema = z
  .object({
    action: z.string(),
    args: z.record(z.unknown()),
    status: z.number().int(),
    body: actionBodySchema,
  })
  .strict();

export type ActionRequest = z.infer<typeof actionRequestSchema>;
export type ActionOk = z.infer<typeof actionOkSchema>;
export type PostOk = z.infer<typeof postOkSchema>;
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
