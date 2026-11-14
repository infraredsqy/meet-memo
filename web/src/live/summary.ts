import { SUMMARY_DEFAULT_WINDOW, type Snapshot, type SummaryWindowChoice } from '../contract';
import type { FailureDetail } from '../failure';
import { formatWallClock } from './format';

/** 总结区那份**纯逻辑**。 */

export interface SummaryWindow {
  choice: SummaryWindowChoice;
  label: string;
}

export const SUMMARY_WINDOWS: readonly SummaryWindow[] = [
  { choice: '5m', label: '5 分钟' },
  { choice: '15m', label: '15 分钟' },
  { choice: '30m', label: '30 分钟' },
  { choice: 'full', label: '全场' },
];

export const DEFAULT_SUMMARY_WINDOW: SummaryWindowChoice = SUMMARY_DEFAULT_WINDOW;

export function windowLabel(choice: SummaryWindowChoice | string): string {
  return SUMMARY_WINDOWS.find((item) => item.choice === choice)?.label ?? String(choice);
}

/**
 * **客户端兜底超时**。
 *
 * 它与服务侧那条墙钟预算（`summary.deadline_seconds`）是**两条线**：服务那条管「这一次调用等多久」，
 * 这一条管「界面等多久还没接到任何应答」。两条都留着 —— 服务真卡在网络层没回话时，只有客户端
 * 这一条能把人从「进行中」里放出来。
 */
export const SUMMARY_CLIENT_TIMEOUT_SECONDS = 30;

export const SUMMARY_SECTION_TITLES = ['讲了什么', '讲到哪里', '丢掉了什么'] as const;
export type SummaryPhase = 'idle' | 'running' | 'done' | 'failure';

export interface SummaryFailure {
  code: string;
  message: string;
  fields: FailureDetail;
}

export interface SummaryResultView {
  title: string;
  window: string;
  windowLabel: string;
  at: string;
  elapsedSeconds: number;
  lines: number;
  /** **可能是空数组**（合法）。 */
  sections: SummarySection[];
  emptyNote: string | null;
}

export interface SummarySection {
  title: string;
  items: string[];
}

export interface SummaryFailureView {
  text: string;
  lines: string[];
}

export interface SummaryState {
  phase: SummaryPhase;
  window: SummaryWindowChoice;
  result: SummaryResultView | null;
  failure: SummaryFailure | null;
  /** 客户端自己量的秒数（`done` / `failure` 时非空）。 */
  elapsedSeconds: number | null;
}

export function initialSummaryState(): SummaryState {
  return {
    phase: 'idle',
    window: DEFAULT_SUMMARY_WINDOW,
    result: null,
    failure: null,
    elapsedSeconds: null,
  };
}

export function startSummary(state: SummaryState): SummaryState {
  return { ...state, phase: 'running', result: null, failure: null, elapsedSeconds: null };
}

export function selectSummaryWindow(
  state: SummaryState,
  window: SummaryWindowChoice,
): SummaryState {
  return { ...state, window };
}

/**
 * 一次总结的往返：发请求 → 等应答，**过了客户端兜底线还没回就按超时画**。
 *
 * - **不提前宣布失败**：`SUMMARY_CLIENT_TIMEOUT_SECONDS` 之前一直画进行中；
 * - **兜底那一档的秒数由客户端自己实测**（不是服务给的 `elapsed_seconds` —— 那一次压根
 *   没有应答）；
 * - **结果与失败都原样交给状态**，不改一个字。
 */
export interface SummaryExchangeDeps {
  /** 打那一次动作（成功给 `summary` 子对象，失败抛带 `code` / `detail` 的错误）。 */
  request: (window: SummaryWindowChoice) => Promise<SummaryResultView>;
  /** 现取一个毫秒时钟。 */
  now: () => number;
  timeoutSeconds?: number;
}

export type SummaryOutcome =
  | { kind: 'ok'; result: SummaryResultView; elapsedSeconds: number }
  | { kind: 'failure'; failure: SummaryFailure; elapsedSeconds: number }
  | { kind: 'timeout'; elapsedSeconds: number };

/**
 * 跑一次请求并把它收成一档结果（**收口**，不抛）。
 *
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

export const SUMMARY_EMPTY_NOTE = '这个窗口里没有可用的转写内容。';

export function summaryEmptyNote(result: {
  what: string[];
  where: string[];
  lost: string[];
}): string | null {
  const total = result.what.length + result.where.length + result.lost.length;
  return total === 0 ? SUMMARY_EMPTY_NOTE : null;
}

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

/** 认不出的那一档的兜底：把服务给的原话端出来，不编一个原因。 */
function unrecognizedFailure(failure: SummaryFailure): SummaryFailureView {
  const subject = failure.fields['subject'];
  const raw = typeof subject === 'string' && subject !== '' ? subject : failure.code;
  return {
    text: raw === '' ? '这次生成没成。' : `这次生成没成：${raw}`,
    lines: [],
  };
}

function roundedSeconds(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? Math.round(value) : null;
}

/**
 * 五档失败各画一遍。
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
    const waited = roundedSeconds(fields['elapsed_seconds']) ?? Math.round(elapsedSeconds ?? 0);
    return clientTimeoutView(waited);
  }

  if (failure.code === 'not_recording') {
    return {
      text: failure.message === '' ? '当前没有进行中的录音场次。' : failure.message,
      lines: [],
    };
  }

  if (failure.code === 'invalid_setting') {
    if (fields['key'] !== 'window') {
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
 * 不同：一个由界面自己量、一个由服务报。这个串只活在界面这一侧，不上线路。
 */
export const SUMMARY_CLIENT_TIMEOUT = 'client_timeout';

export function summaryRunningNote(window: SummaryWindowChoice | string): string {
  return `正在生成摘要：近 ${windowLabel(window)}…`;
}

export interface SummaryAvailabilityInput {
  topState: Snapshot['top_state'];
  lost: boolean;
  stalled: boolean;
  transcriptHealth: Snapshot['transcript']['health'];
  running: boolean;
}

/**
 * **正在跑**时按钮也按不动，那一格画的是进行中那一句 —— 它与禁用档不是同一幅画：禁用档旁边
 * 写原因是给「这一档现在做不了总结」，跑起来那一档写的是「这一次总结正在做」。
 */
export function summaryAvailable(input: SummaryAvailabilityInput): boolean {
  if (input.running) return false;
  if (input.topState !== 'recording' && input.topState !== 'alert') return false;
  if (input.lost || input.stalled) return false;
  return input.transcriptHealth !== 'stopped';
}

export function summaryWhy(input: SummaryAvailabilityInput): string | null {
  if (input.running) return null;
  if (input.lost) return '心跳失联，先看服务那边';
  if (input.stalled) return '采集已停止，先看服务那边';
  if (input.topState === 'idle') return '开始录音后才能生成摘要';
  if (input.topState === 'ended') return '本场已结束，仅录音中可用';
  if (input.transcriptHealth === 'stopped') return '转写已停止，没有可用的内容';
  return null;
}

export interface SummaryLogEntry {
  key: string;
  time: string;
  windowLabel: string;
  elapsedSeconds: number;
  sections: SummarySection[];
  emptyNote: string | null;
}

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

export interface SummaryLogView {
  entryLabel: string;
  title: string;
  count: number;
  entries: SummaryLogEntry[];
  emptyNote: string | null;
}

export const SUMMARY_LOG_EMPTY = '本场还没有摘要记录。';

/**
 * 入口在**还没取到服务那一份**时写的那一行。
 *
 * 取记录的那一次 GET 只在用到的时候发（面板第一次展开时），所以在取回来之前界面上得有个
 * 字：写「还没有」是诚实的 —— 这一刻手上确实一条记录都没有。
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
