import { SUMMARY_DEFAULT_WINDOW, type Snapshot, type SummaryWindowChoice } from '../contract';
import type { FailureDetail } from '../failure';
import { formatWallClock } from './format';

/**
 * 总结区那份**纯逻辑**（设计章 2.3 的「总结区」与 5.1 的两条交互）。
 *
 * 这一层不碰 DOM、不碰 React，也不持有任何权威状态：它认两样输入 —— **服务推来的那份快照**
 * （只用来算「这个入口现在能不能按、按不了时旁边写什么」）与**服务给的那一次应答原文**
 * （三小段、时刻、窗口标签、记录条目）。组件只负责把它算出来的东西摆到位置上。
 *
 * 三件事分开写在三块里：
 *
 * 1. **档位与请求**（`SummarySession`）—— 四个档位、按下去那一刻的请求状态机、
 *    30 秒客户端兜底超时；
 * 2. **可用性与原因**（`summaryWhy`）—— 照设计章 2.5 与章 4.5 那两处现成文案；
 * 3. **结果与失败怎么读**（`summaryResultView` / `summaryFailureView` / `summaryLogView`）——
 *    三小段的取值与空态、五档失败的分派、记录条目的排版。
 *
 * **一条贯穿的规矩**：界面不造派生数据。窗口标签取服务给的 `window_label`、时刻取服务给的
 * `at`、三小段取服务给的三个数组、次数取服务给的 `entries.length` —— 这一层一处都不自己算
 * 窗口边界、不自己拼时刻、不按 `message` 做字符串匹配（契约章 6 第 1 / 7 条）。
 */

/** 四个档位（设计章 2.3 的四档，逐字对应契约 4.7 的取值域）。 */
export interface SummaryWindow {
  /** 请求里 `args.window` 的取值。 */
  choice: SummaryWindowChoice;
  /** 四个档位上写的字（设计章 2.3 逐字）。 */
  label: string;
}

export const SUMMARY_WINDOWS: readonly SummaryWindow[] = [
  { choice: '5m', label: '5 分钟' },
  { choice: '15m', label: '15 分钟' },
  { choice: '30m', label: '30 分钟' },
  { choice: 'full', label: '全场' },
];

/** 缺省选中的那一档（设计章 2.3：默认为 15 分钟）。 */
export const DEFAULT_SUMMARY_WINDOW: SummaryWindowChoice = SUMMARY_DEFAULT_WINDOW;

/** 取某一档上的字；取值域外（服务将来加档）退回取值本身，不猜一个人话标签。 */
export function windowLabel(choice: SummaryWindowChoice | string): string {
  return SUMMARY_WINDOWS.find((item) => item.choice === choice)?.label ?? String(choice);
}

/**
 * **客户端兜底超时**：超过它还没回就按超时那一句画（`/设计/task.md` 的「口径」）。
 *
 * 它与服务侧那条墙钟预算（`summary.deadline_seconds`，默认 25 秒）是**两条线**：服务那条
 * 管「这一次调用等多久」，这一条管「界面等多久还没接到任何应答」。两条都留着 —— 服务真卡在
 * 网络层没回话时，只有客户端这一条能把人从「进行中」里放出来。
 */
export const SUMMARY_CLIENT_TIMEOUT_SECONDS = 30;

/** 三小段的段标题（设计章 2.3 逐字）。 */
export const SUMMARY_SECTION_TITLES = ['讲了什么', '讲到哪里', '丢掉了什么'] as const;
/** 一次总结的请求状态。 */
export type SummaryPhase = 'idle' | 'running' | 'done' | 'failure';

/** 失败那一档：`code` 是服务给的，`message` 是信封上那句人话，`fields` 是 `detail` 的原文。 */
export interface SummaryFailure {
  code: string;
  /** 服务给的那句人话（`not_recording` 那一档直接用它 —— 沿用既有的失败画法）。 */
  message: string;
  fields: FailureDetail;
}

/** 一次总结的结果面板要画的东西（三小段 + 标题 + 那一次的时刻与耗时）。 */
export interface SummaryResultView {
  /** 面板标题（`刚才的 15 分钟`）。 */
  title: string;
  /** 四格报告用：窗口取值、窗口标签、时刻、耗时秒数（都是服务给的原文）。 */
  window: string;
  windowLabel: string;
  at: string;
  elapsedSeconds: number;
  /** 窗口里的行数（应答体那一格；记录那一份没有它）。 */
  lines: number;
  /** 三小段，次序恒为讲什么 / 讲到哪 / 丢掉什么；**可能是空数组**（合法）。 */
  sections: SummarySection[];
  /** 三份都空时的空态那一句；只要有一段有内容就是 `null`。 */
  emptyNote: string | null;
}

/** 一小段：段标题 + 那几条。 */
export interface SummarySection {
  title: string;
  items: string[];
}

/** 一次失败要画的东西。 */
export interface SummaryFailureView {
  /** 原因那一行（人话）。 */
  text: string;
  /** 补充行（有就画，逐条列出）。 */
  lines: string[];
}

/**
 * 一次总结请求的客户端状态机：闲 / 跑 / 有结果 / 失败。
 *
 * **只有界面这一侧的东西**（当前选中的档位、这一次请求的状态、客户端自己的兜底计时），
 * 服务给的权威快照一个字都不进来 —— 快照只参与「能不能按」那道判断，那件事在 `summaryWhy()`
 * 里，与这里的状态无关。
 */
export interface SummaryState {
  phase: SummaryPhase;
  /** 当前选中的档位（缺省 15 分钟）。 */
  window: SummaryWindowChoice;
  /** 这一次的结果（`phase === 'done'` 时非空）。 */
  result: SummaryResultView | null;
  /** 这一次的失败（`phase === 'failure'` 时非空）。 */
  failure: SummaryFailure | null;
  /** 走到终态那一刻客户端自己量的秒数（`done` / `failure` 时非空）。 */
  elapsedSeconds: number | null;
}

/** 初值：闲、缺省档、没有结果也没有失败。 */
export function initialSummaryState(): SummaryState {
  return {
    phase: 'idle',
    window: DEFAULT_SUMMARY_WINDOW,
    result: null,
    failure: null,
    elapsedSeconds: null,
  };
}

/** 跑起来那一刻的状态（旧结果与旧失败一起清掉 —— 面板画的是**这一次**）。 */
export function startSummary(state: SummaryState): SummaryState {
  return { ...state, phase: 'running', result: null, failure: null, elapsedSeconds: null };
}

/** 换一个档位（只改选择；正在跑的这一次不受影响，下一次请求才带上新档位）。 */
export function selectSummaryWindow(
  state: SummaryState,
  window: SummaryWindowChoice,
): SummaryState {
  return { ...state, window };
}

/**
 * 一次总结的往返：发请求 → 等应答，**过了客户端兜底线还没回就按超时画**。
 *
 * 三条口径：
 *
 * - **不提前宣布失败**：`SUMMARY_CLIENT_TIMEOUT_SECONDS` 之前一直画进行中（设计章 2.3：
 *   「界面不显示倒计时，也不提前宣布失败」）；
 * - **兜底那一档的秒数由客户端自己实测**（不是服务给的 `elapsed_seconds` —— 那一次压根
 *   没有应答）；
 * - **结果与失败都原样交给状态**，不改一个字（三小段的空态、五档失败的分派都在
 *   读的那一层，见 `summaryResultView` / `summaryFailureView`）。
 */
export interface SummaryExchangeDeps {
  /** 打那一次动作（成功给 `summary` 子对象，失败抛带 `code` / `detail` 的错误）。 */
  request: (window: SummaryWindowChoice) => Promise<SummaryResultView>;
  /** 现取一个毫秒时钟（默认 `Date.now`）。 */
  now: () => number;
  /** 兜底秒数（默认 30）。 */
  timeoutSeconds?: number;
}

export type SummaryOutcome =
  | { kind: 'ok'; result: SummaryResultView; elapsedSeconds: number }
  | { kind: 'failure'; failure: SummaryFailure; elapsedSeconds: number }
  | { kind: 'timeout'; elapsedSeconds: number };

/**
 * 跑一次请求并把它收成一档结果（**收口**，不抛）。
 *
 * 归到 `failure` 的两条来路：服务回了失败信封（`code` + `detail` 原文）、以及应答形状
 * 不对 / 网络层出错那一类「没拿到可用结果」。后一类在这里换成一句能读懂的话，
 * **不冒充成某个服务侧的 `code`** —— 分派画法的依据是 `code`，编一个出来就画错了档。
 */
export async function runSummaryExchange(
  window: SummaryWindowChoice,
  deps: SummaryExchangeDeps,
): Promise<SummaryOutcome> {
  const timeoutSeconds = deps.timeoutSeconds ?? SUMMARY_CLIENT_TIMEOUT_SECONDS;
  const started = deps.now();

  let timer: ReturnType<typeof setTimeout> | null = null;
  const timeout = new Promise<SummaryOutcome>((resolve) => {
    timer = setTimeout(() => {
      resolve({ kind: 'timeout', elapsedSeconds: (deps.now() - started) / 1000 });
    }, Math.max(0, timeoutSeconds * 1000));
  });

  const attempt = (async (): Promise<SummaryOutcome> => {
    try {
      const result = await deps.request(window);
      return { kind: 'ok', result, elapsedSeconds: (deps.now() - started) / 1000 };
    } catch (error) {
      return {
        kind: 'failure',
        failure: clientFailureOf(error),
        elapsedSeconds: (deps.now() - started) / 1000,
      };
    }
  })();

  try {
    return await Promise.race([attempt, timeout]);
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

/**
 * 客户端这一侧收到的东西 → 失败那一档。
 *
 * 服务回的失败信封（有 `code`）原样用它；其余（形状不对、连接断了、403 那一类）**不编
 * `code`**：`code` 留空串，靠 `fields.subject` 里那句「没接住」的原话说明白。
 * 于是分派画法时它自然落到兜底那一档，不会被误认成 `not_recording` 这类服务侧的原因。
 */
export function clientFailureOf(error: unknown): SummaryFailure {
  const coded = error as { code?: unknown; detail?: unknown; message?: unknown } | null;
  if (coded !== null && typeof coded === 'object' && typeof coded.code === 'string') {
    const detail = coded.detail;
    return {
      code: coded.code,
      message: typeof coded.message === 'string' ? coded.message : '',
      fields: detail !== null && typeof detail === 'object' ? (detail as FailureDetail) : {},
    };
  }
  return {
    code: '',
    message: '',
    fields: { subject: error instanceof Error ? error.message : String(error) },
  };
}

/**
 * 三小段：把服务给的 `what` / `where` / `lost` 按固定次序摆成三段。
 *
 * **三份都空**时给一句空态文案并说明「这个窗口里没有可用的转写内容」（任务书「口径」）——
 * 那是 `ok: true` + 三段空数组那一档（窗口里一行都没有，B15 的「空数组合法」），
 * 不是失败。
 */
export function summarySections(result: {
  what: string[];
  where: string[];
  lost: string[];
}): SummarySection[] {
  return [
    { title: SUMMARY_SECTION_TITLES[0], items: [...result.what] },
    { title: SUMMARY_SECTION_TITLES[1], items: [...result.where] },
    { title: SUMMARY_SECTION_TITLES[2], items: [...result.lost] },
  ];
}

/** 三份都空时那一句。 */
export const SUMMARY_EMPTY_NOTE = '这个窗口里没有可用的转写内容。';

/** 三份都空就给空态那一句，否则 `null`。 */
export function summaryEmptyNote(result: {
  what: string[];
  where: string[];
  lost: string[];
}): string | null {
  const total = result.what.length + result.where.length + result.lost.length;
  return total === 0 ? SUMMARY_EMPTY_NOTE : null;
}

/** 服务给的 `summary` 子对象 → 结果面板要画的东西（不改一个字，只摆次序）。 */
export function summaryResultView(result: {
  window: string;
  window_label: string;
  at: string;
  elapsed_seconds: number;
  lines: number;
  what: string[];
  where: string[];
  lost: string[];
}): SummaryResultView {
  return {
    title: `近 ${result.window_label}摘要`,
    window: result.window,
    windowLabel: result.window_label,
    at: result.at,
    elapsedSeconds: result.elapsed_seconds,
    lines: result.lines,
    sections: summarySections(result),
    emptyNote: summaryEmptyNote(result),
  };
}

// ---------------------------------------------------------------------------
// 失败分派：按 `code` 与 `detail` 里有哪几把键（**不按 `message` 做字符串匹配**）
// ---------------------------------------------------------------------------

/** 认不出的那一档的兜底：把服务给的原话端出来，不编一个原因。 */
function unrecognizedFailure(failure: SummaryFailure): SummaryFailureView {
  const subject = failure.fields['subject'];
  const raw = typeof subject === 'string' && subject !== '' ? subject : failure.code;
  return {
    text: raw === '' ? '这次生成没成。' : `这次生成没成：${raw}`,
    lines: [],
  };
}

/** 四舍五入到整数秒（超时那一档要写出等了多久）。 */
function roundedSeconds(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? Math.round(value) : null;
}

/**
 * 五档失败各画一遍（`/设计/task.md` 的「失败分派」逐条）：
 *
 * | `code` + `detail` 的键 | 画法 |
 * |---|---|
 * | `not_recording` | 沿用既有的失败画法（服务那句 `message` 端出来） |
 * | `invalid_setting` 且 `detail.key === "window"` | 按合法取值数组说 |
 * | `transcript_unavailable` | `转写已停止，没有可用的内容` |
 * | `summary_failed` 且 `detail.missing` 在 | 点名缺哪几个配置键 |
 * | `summary_failed` 且 `detail.elapsed_seconds` 在 | `超时了，等待 <N> 秒仍未返回结果，请重试。` |
 *
 * `code` 认不出来（客户端自己兜的那一类）就落到兜底那一档 —— 依据是 `code` 与键的存在性，
 * 一个字都不看 `message`。
 */
export function summaryFailureView(
  failure: SummaryFailure,
  elapsedSeconds: number | null,
): SummaryFailureView {
  const fields = failure.fields;

  if (failure.code === SUMMARY_CLIENT_TIMEOUT) {
    // 界面自己兜的那一档：秒数由客户端量出来，服务那一次压根没有应答
    const waited = roundedSeconds(fields['elapsed_seconds']) ?? Math.round(elapsedSeconds ?? 0);
    return clientTimeoutView(waited);
  }

  if (failure.code === 'not_recording') {
    // 沿用既有的失败画法：这一档服务自己那句话就说得清（`stop` / `mute_session` 同款）
    return {
      text: failure.message === '' ? '当前没有进行中的录音场次。' : failure.message,
      lines: [],
    };
  }

  if (failure.code === 'invalid_setting') {
    if (fields['key'] !== 'window') {
      // `invalid_setting` 另外几档（`dev_inject` 取值非法 / 空 / 未知动作名）不属于总结这条路
      return unrecognizedFailure(failure);
    }
    const legal = Array.isArray(fields['legal']) ? fields['legal'].map(String) : [];
    const value = fields['value'];
    return {
      text: '窗口档位不合法。',
      lines: [
        `这次带的是 ${typeof value === 'string' ? `「${value}」` : String(value)}。`,
        legal.length === 0 ? '合法的档位是四个。' : `合法取值：${legal.join(' / ')}。`,
      ],
    };
  }

  if (failure.code === 'transcript_unavailable') {
    return { text: '转写已停止，没有可用的内容', lines: [] };
  }

  if (failure.code === 'summary_failed') {
    const missing = fields['missing'];
    if (Array.isArray(missing)) {
      const keys = missing.map(String);
      return {
        text: '模型端点没配齐，生成摘要要的几个配置键还缺着。',
        lines: keys.length === 0 ? [] : [`缺这些键：${keys.join('、')}。`],
      };
    }
    if (typeof fields['elapsed_seconds'] === 'number') {
      // 服务自己等满预算的那一档：写出**服务实测**的秒数
      const waited = roundedSeconds(fields['elapsed_seconds']);
      return {
        text: `超时了，等待 ${waited ?? 0} 秒仍未返回结果，请重试。`,
        lines: [],
      };
    }
    return unrecognizedFailure(failure);
  }

  return unrecognizedFailure(failure);
}

/**
 * **客户端兜底超时**那一档：与服务的超时同一句，`N` 取客户端自己实测的秒数。
 *
 * 与 `summary_failed` + `elapsed_seconds` 那一档是**两句话不是一句**（口径来源不同：
 * 一个由服务报、一个由界面报），但写法逐字一致 —— 用户这边看到的是同一件事。
 */
export function clientTimeoutView(elapsedSeconds: number): SummaryFailureView {
  return {
    text: `超时了，等待 ${Math.round(elapsedSeconds)} 秒仍未返回结果，请重试。`,
    lines: [],
  };
}

/**
 * 客户端兜底那一档的**代号**（服务侧不会有这个 `code`）。
 *
 * 兜底与服务的 `summary_failed` + `elapsed_seconds` 那一档长得一样，但**秒数的来路**
 * 不同：一个由界面自己量、一个由服务报。留一个独立的代号，面板分派时一眼分得清是谁写的
 * （这个串只活在界面这一侧，不上线路）。
 */
export const SUMMARY_CLIENT_TIMEOUT = 'client_timeout';

/** 进行中那一行（设计章 2.3：`正在生成摘要：近 15 分钟…`；不带倒计时）。 */
export function summaryRunningNote(window: SummaryWindowChoice | string): string {
  return `正在生成摘要：近 ${windowLabel(window)}…`;
}

// ---------------------------------------------------------------------------
// 可用性与禁用原因
// ---------------------------------------------------------------------------

/** 算「能不能按」要的那几样（全是从服务推来的那一份快照上读的）。 */
export interface SummaryAvailabilityInput {
  topState: Snapshot['top_state'];
  /** 心跳失联（界面本地态，章 4.5）。 */
  lost: boolean;
  /** 采集已停（服务在应答但没有音频帧）。 */
  stalled: boolean;
  /** 转写健康位（服务给的 `transcript.health`）。 */
  transcriptHealth: Snapshot['transcript']['health'];
  /** 这一次请求正在跑（界面自己的状态）。 */
  running: boolean;
}

/**
 * 总结入口现在能不能按（设计章 2.5 那一行 + 章 4.5 的转写健康那一档）。
 *
 * **可用** ⇔ `top_state ∈ {recording, alert}` 且转写健康位不是 `stopped` 且心跳没失联。
 * **正在跑**时按钮也按不动，那一格画的是进行中那一句（界面自己按「进行中」把按钮禁掉，
 * 契约 4.7 的并发口径）—— 它与禁用档不是同一幅画：禁用档旁边写原因是给「这一档现在
 * 做不了总结」，跑起来那一档写的是「这一次总结正在做」。
 */
export function summaryAvailable(input: SummaryAvailabilityInput): boolean {
  if (input.running) return false;
  if (input.topState !== 'recording' && input.topState !== 'alert') return false;
  if (input.lost || input.stalled) return false;
  return input.transcriptHealth !== 'stopped';
}

/**
 * 按不了时旁边那句原因（设计章 2.5 与章 4.5 的现成文案，逐条照抄）。
 *
 * 次序是**从外面往里**：心跳失联（界面看不见服务了）压在任何一档之上（章 4.5 明文），
 * 其次是采集已停，然后是四态本身，最后才是转写健康位。**正在跑**时不写原因 ——
 * 那一格换成进行中那一句（`summaryRunningNote`）。
 */
export function summaryWhy(input: SummaryAvailabilityInput): string | null {
  if (input.running) return null;
  if (input.lost) return '心跳失联，先看服务那边';
  if (input.stalled) return '采集已停止，先看服务那边';
  if (input.topState === 'idle') return '开始录音后才能生成摘要';
  if (input.topState === 'ended') return '本场已结束，仅录音中可用';
  if (input.transcriptHealth === 'stopped') return '转写已停止，没有可用的内容';
  return null;
}

// ---------------------------------------------------------------------------
// 本场摘要记录
// ---------------------------------------------------------------------------

/** 记录里的一次总结（`GET /api/summaries` 的条目 + 序号）。 */
export interface SummaryLogEntry {
  /** 稳定 key（列表里没有第二条一模一样的时刻，用序号最省事）。 */
  key: string;
  /** 服务给的时刻（`at`）→ 分钟级墙钟 `HH:MM`。 */
  time: string;
  /** 那一次取的窗口（服务给的 `window_label` 原文）。 */
  windowLabel: string;
  /** 那一次报告用的耗时秒数（服务给的 `elapsed_seconds`）。 */
  elapsedSeconds: number;
  /** 三小段（讲什么 / 讲到哪 / 丢掉什么）。 */
  sections: SummarySection[];
  /** 三份都空时的空态那一句；有内容就是 `null`。 */
  emptyNote: string | null;
}

/**
 * 服务给的一条记录 → 记列表里那一条。
 *
 * 时刻按**归档的精度**画（分钟级 `HH:MM`，设计章 8.10 门槛 10）；窗口那一格用服务给的
 * `window_label` 原文；三小段照讲什么 / 讲到哪 / 丢掉什么的次序摆。
 */
export function summaryLogEntry(
  entry: {
    window: string;
    window_label: string;
    at: string;
    elapsed_seconds: number;
    what: string[];
    where: string[];
    lost: string[];
  },
  index: number,
): SummaryLogEntry {
  return {
    key: `${index}-${entry.at}`,
    time: formatWallClock(entry.at),
    windowLabel: entry.window_label,
    elapsedSeconds: entry.elapsed_seconds,
    sections: summarySections(entry),
    emptyNote: summaryEmptyNote(entry),
  };
}

/** 记录回看那一块要画的东西。 */
export interface SummaryLogView {
  /** 入口上那一行：`本场总结记录（3 次）` / `本场总结记录（还没有）`。 */
  entryLabel: string;
  /** 面板标题（与入口同一句）。 */
  title: string;
  /** 这一场按过几次（**服务给的条目数**）。 */
  count: number;
  entries: SummaryLogEntry[];
  /** 一次都没按过时那一句（`本场还没有摘要记录。`）；否则 `null`。 */
  emptyNote: string | null;
}

/** 一次都没按过时，记录面板里那一句（设计章 5.1 逐字）。 */
export const SUMMARY_LOG_EMPTY = '本场还没有摘要记录。';

/**
 * 入口在**还没取到服务那一份**时写的那一行。
 *
 * 取记录的那一次 GET 只在用到的时候发（面板第一次展开时），所以在取回来之前界面上得有个
 * 字：写「还没有」是诚实的 —— 这一刻手上确实一条记录都没有，服务那边有几条要等取回来才知道
 * （那时整行换成 `本场摘要记录（<N> 次）`，N 取服务给的条目数）。
 */
export function summaryLogLabel(count: number | null): string {
  if (count === null) return '本场摘要记录（还没有）';
  return count === 0 ? '本场摘要记录（还没有）' : `本场摘要记录（${count} 次）`;
}

/**
 * 记录回看。
 *
 * **条数不自己记**：这一场按过几次由服务给的 `entries` 长度说话（界面自己数按下次数会
 * 在刷新页面、另一个标签页按过、请求失败时对不上）。时刻取服务给的 `at`。
 */
export function summaryLogView(
  entries: readonly {
    window: string;
    window_label: string;
    at: string;
    elapsed_seconds: number;
    what: string[];
    where: string[];
    lost: string[];
  }[],
): SummaryLogView {
  const rows = entries.map((entry, index) => summaryLogEntry(entry, index));
  const label = summaryLogLabel(rows.length);
  return {
    entryLabel: label,
    title: label,
    count: rows.length,
    entries: rows,
    emptyNote: rows.length === 0 ? SUMMARY_LOG_EMPTY : null,
  };
}
