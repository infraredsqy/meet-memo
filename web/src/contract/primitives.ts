/**
 * 契约层的公共形状：**掩码占位符**。
 *
 * 样本出自真实服务路径，采集时把墙钟、路径、端口、电平一律换成了自描述占位符
 * （`<iso>` / `<path>` / `<session-id>` / `<port>` / `<url>` / `<dbfs>`）。这些占位符**就是
 * fixtures 里的真实值**，所以 schema 必须在 parse 层照实放行；真值那一档由带正则的
 * `z.string()` 钉住最小形状，形状不对的照样要被拒。
 */

export const ISO_PLACEHOLDER = '<iso>';
/** 路径类的占位符（含自由文本里内嵌的路径）。 */
export const PATH_PLACEHOLDER = '<path>';
/**
 * **场次目录名**的占位符（`session.id`，与动作失败 `detail.session_id`）：真服务产出的这一格
 * 是**裸目录名**，路径与目录名是两种形状，所以与 `<path>` 分开。
 */
export const SESSION_ID_PLACEHOLDER = '<session-id>';
export const PORT_PLACEHOLDER = '<port>';
export const URL_PLACEHOLDER = '<url>';
/** 电平值的占位符（含 `envelope` 的逐根）。 */
export const DBFS_PLACEHOLDER = '<dbfs>';

/** 快照四态。**不新增第五个**。 */
export const TOP_STATES = ['idle', 'recording', 'alert', 'ended'] as const;

/** 十九个动作名，一个不多一个不少。**次序就是服务回的次序**：`known` 数组逐个报的就是这一串。 */
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
  'post_stop',
  'transcode_run',
  'transcode_pause',
  'transcode_resume',
  'pause',
  'resume',
  'set_source',
  'speaker_rename',
] as const;

/** 失败信封的九个 `code`，一个不多一个不少。 */
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

/** `code` → HTTP 状态码：同一个 `code` 的状态码是固定的。 */
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

/** `summarize` 的四个窗口档位，次序就是界面上那四个档位的次序。**人话标签由服务在 `window_label` 里给** —— 界面不自造一份映射。 */
export const SUMMARY_WINDOW_CHOICES = ['5m', '15m', '30m', 'full'] as const;

export const SUMMARY_DEFAULT_WINDOW = '15m';

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

/** 收尾三步的次序恒为 `flush` / `log` / `post`。 */
export const DRAIN_STEP_IDS = ['flush', 'log', 'post'] as const;

/** `ended.phase` 两取值。**`failed` 不是这个字段的取值** —— 它属于盘上 `session.json` 的 `state`。 */
export const ENDED_PHASES = ['draining', 'done'] as const;

export const TRANSCRIPT_HEALTHS = ['normal', 'behind', 'stopped'] as const;

/** 转写行的 `kind`：`line` 是正文、`gap` 是断口。 */
export const TRANSCRIPT_LINE_KINDS = ['line', 'gap'] as const;

/** 包络柱数（最近 30 秒、一根 2 秒）。 */
export const ENVELOPE_BAR_COUNT = 15;

/** 心跳周期的 clamp 区间与默认值。 */
export const HEARTBEAT_INTERVAL_MIN_SECONDS = 1;
export const HEARTBEAT_INTERVAL_MAX_SECONDS = 60;
export const HEARTBEAT_INTERVAL_DEFAULT_SECONDS = 10;
