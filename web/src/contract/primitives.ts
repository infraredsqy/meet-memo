/**
 * 契约层的公共形状。
 *
 * 这里只有一件事：**掩码占位符**。`contract/fixtures/` 里的 golden 样本出自真实服务路径，
 * 采集时把墙钟、路径、端口、电平一律换成了自描述占位符（规则见 `contract/README.md` 第 2 章）：
 *
 *   `<iso>` / `<path>` / `<session-id>` / `<port>` / `<url>` / `<dbfs>`
 *
 * 这些占位符**就是 fixtures 里的真实值**，所以 schema 必须在 parse 层照实放行：
 * 一个 ISO 时间戳字段接受的取值是「ISO 时间戳 **或** `<iso>`」这两样。
 * 真值里不可能出现带尖括号的串，因此放行占位符不会把畸形数据混进来。
 *
 * 「这两样」是一份**穷举清单**：真值那一档在 `snapshot.ts` 里由带正则的
 * `z.string()` 钉住最小形状，占位符也只在 `literal` 那一半放行。
 * 放行占位符的同时，形状不对的值（`not-a-timestamp`、空串、别的 `<xxx>`）
 * 照样要在 parse 层被拒。
 */

/** ISO 时间戳的占位符（`started_at` / `since` / `lost_since` / `last_line_at` / `server_time` …）。 */
export const ISO_PLACEHOLDER = '<iso>';
/** 路径类的占位符（`log_path` / `out_root` / `dir` / `session_dir`，以及自由文本里内嵌的路径）。 */
export const PATH_PLACEHOLDER = '<path>';
/**
 * **场次目录名**的占位符（`session.id`，与动作失败 `detail.session_id`）。
 *
 * 它自成一档（与 `<path>` 分开）的理由见 `snapshot.ts` 的 `sessionIdSchema`：
 * 真服务产出的这一格是**裸目录名**（`260921`），路径与目录名是两种形状。
 */
export const SESSION_ID_PLACEHOLDER = '<session-id>';
/** 端口的占位符。 */
export const PORT_PLACEHOLDER = '<port>';
/** 服务 URL 的占位符。 */
export const URL_PLACEHOLDER = '<url>';
/** 电平值的占位符（`level_dbfs` / `peak_dbfs` / `dbfs` / `peak` / `bar`，以及 `envelope` 的逐根）。 */
export const DBFS_PLACEHOLDER = '<dbfs>';

/** 快照四态。**不新增第五个**（`docs/contract.md` 第 2 章）。 */
export const TOP_STATES = ['idle', 'recording', 'alert', 'ended'] as const;

/**
 * 十一个动作名，一个不多一个不少（`docs/contract.md` 4.3）。
 *
 * **次序就是服务回的次序**：`known` 数组（未知动作名那一档的 `detail`）逐个报的就是这一串，
 * 末尾那两个 `workspace_open` / `workspace_remove` 是工作台收编节点（e2f6166b）落进来的
 * （正文见 4.8）；最后那两个 `post_offline` / `post_minutes` 是会后两个按钮
 * （「生成会后精细稿」/「直接出纪要」，R26）。
 */
export const ACTION_NAMES = [
  'start',
  'stop',
  'mute_session',
  'settings_update',
  'reprobe',
  'dev_inject',
  'summarize',
  'workspace_open',
  'workspace_remove',
  'post_offline',
  'post_minutes',
] as const;

/** 失败信封的九个 `code`，一个不多一个不少（`docs/contract.md` 4.4）。 */
export const ERROR_CODES = [
  'already_recording',
  'draining',
  'capture_unavailable',
  'not_recording',
  'invalid_setting',
  'persist_failed',
  'action_unavailable',
  'transcript_unavailable',
  'summary_failed',
] as const;

/** `code` → HTTP 状态码。同一个 `code` 的状态码是固定的（`docs/contract.md` 4.4）。 */
export const ERROR_CODE_STATUS: Readonly<Record<(typeof ERROR_CODES)[number], number>> = {
  already_recording: 409,
  draining: 409,
  capture_unavailable: 503,
  not_recording: 409,
  invalid_setting: 400,
  persist_failed: 500,
  action_unavailable: 404,
  transcript_unavailable: 503,
  summary_failed: 502,
};

/**
 * `summarize` 的四个窗口档位，次序就是界面上那四个档位的次序（`docs/contract.md` 4.7）。
 *
 * 服务只认这四个**取值**；人话标签（`5 分钟` / `15 分钟` / `30 分钟` / `整场`）由服务在
 * `window_label` 里给 —— 界面不自造一份映射（契约章 6 第 1 条：界面不造派生数据）。
 */
export const SUMMARY_WINDOW_CHOICES = ['5m', '15m', '30m', 'full'] as const;

/** 缺省档（`summary.default_window` 的默认值，`docs/contract.md` 4.6）。 */
export const SUMMARY_DEFAULT_WINDOW = '15m';

/** `health` 七取值（`docs/contract.md` 第 2 章）。 */
export const SOURCE_HEALTHS = [
  'unprobed',
  'ready',
  'probe_failed',
  'capturing',
  'lost',
  'restored',
  'stopped',
] as const;

/** `session.mode` 三取值，开场前定死、录音期间不切。 */
export const SOURCE_MODES = ['both', 'mic', 'loopback'] as const;

/** 两路的次序恒为 `mic`、`loopback`（`sources[0]` / `sources[1]`）。 */
export const SOURCE_IDS = ['mic', 'loopback'] as const;

/** 收尾四步的次序恒为 `flush` / `transcode` / `log` / `post`。 */
export const DRAIN_STEP_IDS = ['flush', 'transcode', 'log', 'post'] as const;

/** `ended.phase` 两取值。**`failed` 不是这个字段的取值** —— 它属于盘上 `session.json` 的 `state`。 */
export const ENDED_PHASES = ['draining', 'done'] as const;

/** `transcript.health` 三取值。 */
export const TRANSCRIPT_HEALTHS = ['normal', 'behind', 'stopped'] as const;

/** 转写行的 `kind`：`line` 是正文、`gap` 是断口。 */
export const TRANSCRIPT_LINE_KINDS = ['line', 'gap'] as const;

/** （`notice` 槽位已随「默认输出根」退场，见 `docs/design-intent.md` 第 4 条。） */

/** 包络柱数：最近 30 秒、15 根、一根 2 秒（`docs/contract.md` 第 2 章）。 */
export const ENVELOPE_BAR_COUNT = 15;

/** 心跳周期的 clamp 区间与默认值（`docs/contract.md` 第 5 章：默认 10 秒、可配 1–60 秒）。 */
export const HEARTBEAT_INTERVAL_MIN_SECONDS = 1;
export const HEARTBEAT_INTERVAL_MAX_SECONDS = 60;
export const HEARTBEAT_INTERVAL_DEFAULT_SECONDS = 10;
