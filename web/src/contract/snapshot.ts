import { z } from 'zod';
import {
  DBFS_PLACEHOLDER,
  ISO_PLACEHOLDER,
  PATH_PLACEHOLDER,
  PORT_PLACEHOLDER,
  SESSION_ID_PLACEHOLDER,
  SUMMARY_WINDOW_CHOICES,
  URL_PLACEHOLDER,
} from './primitives';
import {
  alertLevelSchema,
  drainStepIdSchema,
  endedPhaseSchema,
  sourceHealthSchema,
  sourceIdSchema,
  sourceModeSchema,
  topStateSchema,
  transcriptHealthSchema,
} from './enums';

/**
 * 掩码占位符类型的建法。
 *
 * `literal(PLACEHOLDER)` 这一半是给 golden 样本用的：样本里那一格就写着 `<iso>` / `<path>`，
 * 它得能 parse 过；正则那一半是给真服务用的：线上给的是真时间戳、真路径、真 URL。
 *
 * **真值分支必须是带约束的 `z.string().regex(...)`，不能是裸 `z.string()`。**
 * 裸 `z.string()` 会把 union 退化成「一切字符串都过」——占位符 literal 那一条永远走不到，
 * schema 对真实数据零校验（明显的畸形值也照收）。这里三条 string 型掩码
 * （iso / path / url）各自钉住自己那一档的最小形状：
 *
 * - 形状口径与 `tests/_contract.py` 的掩码判据同源（`_ISO_RE` 等），样本与真话两边都认；
 * - 判据只收「明显畸形」，不替服务做语义校验（例如不查日历、不查路径是否存在）；
 * - 占位符**只在占位符那半**放行，别的 `<xxx>` 串一律拒 —— 真值里不会出现 `<...>`，
 *   放行别的占位符只会说明这一格被填错了类型。
 *
 * 数值型的 `portSchema` / `dbfsSchema` 没有这个毛病：它们的真值分支是 `z.number()`，
 * 不会吞掉字符串，占位符 literal 照旧可达。
 */
const maskedString = (placeholder: string, valuePattern: RegExp) =>
  z.union([z.string().regex(valuePattern), z.literal(placeholder)]);

/**
 * 收严的写法：**除 `detail` 之外的每一个契约对象都走它**。
 *
 * 第 7 章那些定名未实现的东西（`summary` 键这一类）不该冒出来，多一个键就说明契约漂了。
 * 顶层快照早先收严了、嵌套那些对象没有 —— 于是「注入一个契约外的嵌套键」能一路过到
 * 界面上，TS 侧对嵌套层的漂移一声不响（复审抓到的正是这一格）。这里把口径铺到每一层。
 *
 * **`describeFailure` 那处是唯一的例外，且是有意的**：契约 6.7 要界面「按 `detail` 有哪个
 * 键就显示哪个键」，服务给某个 `code` 补一个 detail 键属于加性变更，前端不该因此整条报错
 * （运行时宽容、样本层的精确由 `ACTION_DETAIL_KEYS` 那条断言负责，理由见 `actions.ts`）。
 */
const strict = <T extends z.ZodRawShape>(shape: T) => z.object(shape).strict();

/**
 * ISO 时间戳：真值是 `YYYY-MM-DDTHH:MM:SS±HH:MM` 形状（秒精度、本地时区），
 * 样本里是 `<iso>`。正则与 `tests/_contract.py` 的 `_ISO_RE` 逐字对齐
 * （容忍带小数与 `Z` 的写法）——掩码认得出来的值，这边必须放行；
 * 掩码认不出的（`not-a-timestamp`、空串）就是畸形值，要拒。
 *
 * 日历层面仍然不校验（不判断 2 月 30 日这类），时间的语义归服务。
 */
export const isoStringSchema = maskedString(
  ISO_PLACEHOLDER,
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$/,
);

/**
 * 路径：`log_path` / 场次目录 / 场次目录路径，样本里统一是 `<path>`。
 *
 * 真值形状照线上来的那几种绝对路径开端：盘符（`C:\` / `C:/`）、UNC（`\\`）、
 * 或 POSIX 根（`/`）。相对路径不在契约里（服务的 `log_path` / `dir` 都是
 * 绝对路径），因此不放行。
 *
 * **`session.id` 不走这一条** —— 它是场次目录名而不是路径，见 `sessionIdSchema`。
 */
export const pathStringSchema = maskedString(PATH_PLACEHOLDER, /^(?:[A-Za-z]:[\\/]|\\\\|\/)/);

/**
 * 场次目录名（`session.id`）：**人给的工作区目录名**（『目录名就是这一场会的名字』，
 * `docs/design-intent.md` 第 3 条 —— 不再由服务按日期生成），样本里是 `<session-id>`。
 *
 * **它必须与 `pathStringSchema` 分开**，两半的理由各是一条实测事实：
 *
 * - 值这一半：服务产出的 `session.id` 是**裸目录名**（`start_session()` 用
 *   `Path(session_dir).name` 取它；目录由用户显式给、软件不解析它的语义）。
 *   拿 `pathStringSchema` 去卡它**两半都过不了** —— 真服务上 `recording` / `alert` /
 *   `ended` 三态的整份快照会因此解析失败（`idle` 因 `session: null` 不受影响）。
 * - 掩码那一半：`contract/README.md` 2.1 / 2.2 把这一格掩成 `<session-id>`，
 *   golden 样本里写的就是 `<session-id>`。
 *
 * 形状细则（照服务实现定）：
 *
 * - 非空、不含路径分隔符（`/` 与 `\`）与占位符标记 `<` —— 它是名称而不是路径，
 *   真值里不会出现 `<...>` 这种串；
 * - **只做形状校验**：不查日历（旧口径的 `YYMMDD` 形状判据随日期取名一并退场）、
 *   不查目录是否真的存在 —— 语义归服务，schema 只管「这一格的写法对不对」。
 */
export const sessionIdSchema = maskedString(
  SESSION_ID_PLACEHOLDER,
  /^(?:[^<>/\\\r\n])+$/,
);

/** 端口：线上是 int，样本里是 `<port>`（掩码把数字换成了串，两档都得放行）。 */
export const portSchema = z.union([z.number().int(), z.literal(PORT_PLACEHOLDER)]);

/** 服务 URL：线上是 `http://127.0.0.1:<port>`，样本里是 `<url>`。 */
export const urlSchema = maskedString(URL_PLACEHOLDER, /^https?:\/\/[^\s/]+/i);

/** 单个电平读数：线上是 number，样本里是 `<dbfs>`，没有读数时是 `null`。 */
export const dbfsSchema = z.union([z.number(), z.literal(DBFS_PLACEHOLDER)]);

/** 可空的电平读数。 */
export const nullableDbfsSchema = dbfsSchema.nullable();

/**
 * 包络：最近 30 秒的电平包络，15 根柱、一根 2 秒。
 * 会话开场（还没开始录）是空数组 `[]`，录音中（乃至 `ended` 停留档）长度恰为 15。
 * 长度由服务保证，这里只校验元素形状 —— 界面不自己造这根柱。
 */
export const envelopeSchema = z.array(dbfsSchema);

/** ISO 时间戳｜null。 */
export const nullableIsoSchema = isoStringSchema.nullable();

/** `service` 五子键，启动时定一次（`docs/contract.md` 第 2 章）。**没有 `out_root`**：落点由工作区给。 */
export const serviceSchema = strict({
  started_at: isoStringSchema,
  version: z.string().min(1),
  port: portSchema,
  url: urlSchema,
  log_path: pathStringSchema,
});

/** `session` 七子键。`null` 表示现在没有场次。 */
export const sessionSchema = strict({
  /** 场次目录名（人给的工作区目录名，形状任意 —— 见 `sessionIdSchema`）。掩码 `<session-id>`。 */
  id: sessionIdSchema,
  /** 场次目录路径（绝对路径）。掩码 `<path>`。 */
  dir: pathStringSchema,
  started_at: isoStringSchema,
  mode: sourceModeSchema,
  elapsed_seconds: z.number(),
  frozen: z.boolean(),
  /**
   * 这一场此刻是不是**暂停中**（`pause` / `resume` 两个动作翻它）。
   *
   * 「暂停中」的唯一落点：顶层四态不新增第五个（`docs/contract.md` 第 2 章）。
   * 暂停期间那一截写数字静音、不录内容，段不切，时长读数也不走。
   */
  paused: z.boolean(),
});

/** `sources[*]`：九键齐。两路的次序恒为 `mic`、`loopback`。 */
export const sourceSchema = strict({
  /** 取值域字面量（`mic` / `loopback`），逐字保留。 */
  id: sourceIdSchema,
  /** 取值域字面量（`本地` / `远端`），逐字保留。 */
  label: z.string(),
  device_name: z.string().nullable(),
  health: sourceHealthSchema,
  lost_since: nullableIsoSchema,
  restored: z.boolean(),
  level_dbfs: nullableDbfsSchema,
  peak_dbfs: nullableDbfsSchema,
  envelope: envelopeSchema,
});

/** `Alert` 六字段。 */
export const alertSchema = strict({
  level: alertLevelSchema,
  /** 那一路的 id（提示级现在也用 `source = transcript`，所以这里收 string）。 */
  source: z.string(),
  since: isoStringSchema,
  duration_seconds: z.number(),
  message: z.string(),
  /**
   * 这一条现在会不会出声。判定与窗口计算全在服务侧：
   * 界面只看这个布尔值，不算任何抑制窗口（`docs/contract.md` 第 6 章第 6 条）。
   */
  muted: z.boolean(),
});

/** `alerts`：致命级同时只留一条，提示级同类合并、上限 3。 */
export const alertsSchema = strict({
  critical: alertSchema.nullable(),
  hints: z.array(alertSchema),
});

/** `transcript` 四键。两个计数只进 log 与 `/debug`，产品界面不摆它们。 */
export const transcriptSchema = strict({
  health: transcriptHealthSchema,
  last_line_at: nullableIsoSchema,
  buffered_lines: z.number().int(),
  failed_segments: z.number().int(),
});

/**
 * `DrainStep` 四键齐。`error` 键恒在 —— 没有错误时就是 `null`，
 * 所以这里用 `nullable()` 而**不是** `optional()`。缺了这个键的帧要红。
 */
export const drainStepSchema = strict({
  id: drainStepIdSchema,
  status: z.enum(['waiting', 'running', 'done', 'failed', 'skipped']),
  detail: z.string(),
  error: z.string().nullable(),
});

/** `ended`：收尾进行中或跑完的摘要。`null` 表示没有收尾这件事。 */
export const endedSchema = strict({
  phase: endedPhaseSchema,
  steps: z.array(drainStepSchema),
  error: z.string().nullable(),
});

/** `settings.asr` 五子键，与配置键 `asr.*` 逐字同名（只去掉前缀）。 */
export const asrSettingsSchema = strict({
  max_segment_seconds: z.number(),
  timeout_seconds: z.number(),
  max_new_tokens: z.number().int(),
  repeat_max_unit: z.number(),
  repeat_min_coverage: z.number(),
});

/**
 * `settings` 七键。`devices` 的两个子键是 `mic` / `loopback`（`dca4539b` 定名），
 * 取值是设备名字符串，没配就是 `null`。
 *
 * `summary_default_window`（`311f84f5`）是快速总结的缺省档（四档取值域）：
 * 界面的默认预选随它走 —— 服务是唯一来源，前端不自造第二份。
 *
 * `summary_preset_names` / `summary_active_preset`（会中切模型，本节点）：预设的
 * **名字清单**与**当前用哪一套的生效值**。只给名字 —— 预设的 `base_url` / `api_key` /
 * `model` 是配置、密钥更是**不进快照、不在界面回显**；`summary_active_preset` 空串 =
 * 没选预设（总结用平铺四键那一路），指向的名字不在清单里时服务侧也如实报空串。
 *
 * `devices` 用 `z.record(...)` 而不是 `z.object({...})`：子键是加性变更（服务以后多长一个
 * 子键时前端不该开始拒收整份快照），`record` 的写法把「现在两个子键、将来可能多」这件事
 * 如实说清楚。`z.record(z.string(), z.unknown())` 对**值**不收窄：整格照收任意值
 * （数字 / 对象 / 数组都过），本层只保证键是字符串。`strict` 管的是**键集合**，不约束
 * `record` 的值类型。
 */
export const settingsSchema = strict({
  alarm_sound: z.boolean(),
  /** 界面发心跳的周期。**这是心跳周期的唯一来源**，前端不自造第二份配置源。 */
  heartbeat_interval_seconds: z.number().int(),
  /** 快速总结的缺省档（`summary.default_window` 的生效值，四档之一）。 */
  summary_default_window: z.enum(SUMMARY_WINDOW_CHOICES),
  /** 会中切模型：预设名字清单（按配置里的次序；只有名字，没有端点与密钥）。 */
  summary_preset_names: z.array(z.string()),
  /** 当前用哪套预设的生效值；空串 = 没选预设（用平铺四键那一路）。 */
  summary_active_preset: z.string(),
  asr: asrSettingsSchema,
  devices: z.record(z.string(), z.unknown()),
});

/**
 * 快照顶层**恰好九键**（`docs/contract.md` 第 2 章）。
 *
 * `GET /api/state` 的 body、SSE 首帧 `snapshot`、SSE 的 `state` 帧三处是同一个函数出的
 * 同一份东西，因此共用这一个 schema。重同步那一帧在九键之上多 `resync` / `dropped` 两键，
 * 见 `snapshotFrameSchema`。
 *
 * 收严的口径与嵌套对象**同一处**（`strict()`，见文件开头那一段）：每一层多一个键都要红。
 */
export const snapshotSchema = strict({
  /** 进程内单调递增。掩码保留真实值 —— 它由固定的迁移与提交次数决定。 */
  revision: z.number().int(),
  service: serviceSchema,
  top_state: topStateSchema,
  session: sessionSchema.nullable(),
  /** 恰好两项，次序恒为 `mic`、`loopback`。 */
  sources: z.array(sourceSchema).length(2),
  alerts: alertsSchema,
  transcript: transcriptSchema,
  ended: endedSchema.nullable(),
  settings: settingsSchema,
});

/**
 * 重同步帧的快照形状：顶层十一键 = 九键 + `resync` + `dropped`（`docs/contract.md` 3.5）。
 * 见到 `resync: true` 就把手上这份整块换成它，**不把 `dropped` 当错误码**。
 */
export const resyncSnapshotSchema = snapshotSchema.extend({
  resync: z.literal(true),
  dropped: z.number().int().min(1),
});

export type Service = z.infer<typeof serviceSchema>;
export type Session = z.infer<typeof sessionSchema>;
export type Source = z.infer<typeof sourceSchema>;
export type Alert = z.infer<typeof alertSchema>;
export type Alerts = z.infer<typeof alertsSchema>;
export type Transcript = z.infer<typeof transcriptSchema>;
export type DrainStep = z.infer<typeof drainStepSchema>;
export type Ended = z.infer<typeof endedSchema>;
export type AsrSettings = z.infer<typeof asrSettingsSchema>;
export type Settings = z.infer<typeof settingsSchema>;
export type Snapshot = z.infer<typeof snapshotSchema>;
export type ResyncSnapshot = z.infer<typeof resyncSnapshotSchema>;
export type TopState = z.infer<typeof topStateSchema>;
export type SourceId = z.infer<typeof sourceIdSchema>;
export type SourceHealth = z.infer<typeof sourceHealthSchema>;
export type SourceMode = z.infer<typeof sourceModeSchema>;
export type AlertLevel = z.infer<typeof alertLevelSchema>;
