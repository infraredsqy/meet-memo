/**
 * 心跳本地态：四级（正常 / 迟到 / 失联 / 已恢复）、失联横幅的文案、以及「采集已停」那一档。
 *
 * 三态判定本身在 `api/heartbeat.ts`（`judgeHeartbeat`：用应答里的 `server_time`
 * 与本地时钟的差，< 1 个周期正常、≥1 且 <2 迟到、≥2 失联，解析失败一律失联）。
 * 这一层管的是**展示**：迟到的弱提示、失联的全屏横幅、恢复后留 30 秒的那条短记录，
 * 以及「服务在应答、但采集已经停了」那一档（设计章 4.5：应答里 `recording_alive`
 * 为假时按失联同样的形状处置，但说的是另一件事）。
 *
 * **两件事不得同屏**（设计章 4.5 末段）：失联横幅说的是「界面看不见服务、整场录音
 * 已经中断」，`远端这一路采不到了` 那条致命横幅说的是「服务还在、某一路没了」。
 * 所以失联时整块告警区不出现；而「采集已停」那一档与致命级横幅也**不得同屏**
 * （任务书「边界」第 6 条）——它的处置在下面 `captureStalled` 里写清楚了。
 */

import type { HeartbeatStatus } from '../api/heartbeat';
import { lateHeartbeatText } from '../api/heartbeat';
import type { Heartbeat, TopState } from '../contract';
import { formatElapsed, formatWallClock, formatWallClockSeconds } from './format';

/** 四级：正常时不显示任何东西；已恢复是失联回来之后留 30 秒的短记录。 */
export type HeartbeatLevel = '正常' | '迟到' | '失联' | '已恢复';

/** 恢复记录留多久（设计章 4.5：「状态区旁留一条短记录 30 秒」）。 */
export const RECOVERED_NOTE_SECONDS = 30;

/**
 * 「采集已停」要**连续这么多次读数**是 `false` 才判（两拍规则，de899128）。
 *
 * 开场那一拍服务报的是 `null`（还没有第一帧），单次 `false` 也可能是竞态窗口里的
 * 误读 —— 一惊一乍的告警会被人关掉（design-intent 第 17 条）。计数是**界面层**的
 * 确认计数：任何非 `false` 读数（`true` / `null` / 失联）把它清零；服务侧的
 * `recording_alive` 计算照旧不看界面的心跳周期（契约第 5 章：两个「心跳」不串线）。
 */
export const CAPTURE_STOP_CONFIRMATIONS = 2;

export interface HeartbeatState {
  level: HeartbeatLevel;
  /** 这一次失联是从什么时候开始的（本地时钟，毫秒）。 */
  lostSinceMs: number | null;
  /** 中断了多少秒（恢复那一刻算出来的）。 */
  gapSeconds: number | null;
  /** 恢复记录从哪一刻开始露脸（本地时钟，毫秒）。 */
  noticeSinceMs: number | null;
  /** 最后一次**成功**应答的 `server_time`（失联横幅要写它）。 */
  lastReplyAt: string | null;
  /** 失联期间那一格数据停在什么时候 —— 包络时间戳、转写断口都用它。 */
  frozenAt: string | null;
  /** 迟到那一行的字：`心跳迟到 · 8 秒没收到应答`。 */
  lateText: string | null;
  /**
   * 连续多少次读数里 `recording_alive === false`（两拍规则的计数）。
   *
   * **界面层的单一归并点就是 `applyHeartbeatReading()`**：它在这里登记，冻结判据
   * （`App.tsx` 的 `dataStopped`）与横幅判据（`view.ts` 的 `stalled`）都只读这一个数
   * —— 两处各记各的就会漂。任何非 `false` 读数（`true` / `null` / 失联）清零。
   */
  stalledBeats: number;
}

export function initialHeartbeatState(): HeartbeatState {
  return {
    level: '正常',
    lostSinceMs: null,
    gapSeconds: null,
    noticeSinceMs: null,
    lastReplyAt: null,
    frozenAt: null,
    lateText: null,
    stalledBeats: 0,
  };
}

/** 一次应答或一次失败的读数（与 `api/heartbeat.ts` 的 `HeartbeatTick` 同形）。 */
export type HeartbeatReading =
  | { kind: 'reply'; heartbeat: Heartbeat; status: HeartbeatStatus; lagSeconds: number }
  | { kind: 'error'; error: Error; status: HeartbeatStatus };

/**
 * 把一次读数并进本地态（纯函数）。
 *
 * 四条迁移：
 *
 * | 从 → 到 | 做什么 |
 * |---|---|
 * | 任何 → 失联 | 记下失联起点；`frozenAt` 停在最后一次应答的时刻 |
 * | 失联 → 正常/迟到 | 算出中断秒数、开始露那条 30 秒的短记录 |
 * | 正常 ↔ 迟到 | 迟到写一行字；正常把那一行收掉 |
 * | 恢复记录过期 | 到点自己消失（不靠服务再推一帧） |
 *
 * 「正常」这一档在界面上**什么都不显示**（设计章 4.5），所以进入正常就等于把
 * 迟到那一行收掉。
 */
export function applyHeartbeatReading(
  state: HeartbeatState,
  reading: HeartbeatReading,
  nowMs: number,
): HeartbeatState {
  const lastReplyAt = reading.kind === 'reply' ? reading.heartbeat.server_time : state.lastReplyAt;

  // 两拍规则的计数（单一归并点）：`false` 连着数，`true` / `null` / 失联一并清零。
  // `null` 是「还没有第一帧」的未知档（de899128），它重置计数正是开场不误报的关键。
  const stalledBeats =
    reading.kind === 'reply' && reading.heartbeat.recording_alive === false
      ? state.stalledBeats + 1
      : 0;

  // 恢复记录到期：先按「现在」把它收掉，再判这一次读数
  const noticeExpired =
    state.noticeSinceMs !== null &&
    nowMs - state.noticeSinceMs >= RECOVERED_NOTE_SECONDS * 1000;
  const wasLost = state.level === '失联';

  if (reading.status === '失联') {
    return {
      level: '失联',
      lostSinceMs: wasLost ? state.lostSinceMs : nowMs,
      gapSeconds: null,
      noticeSinceMs: null,
      lastReplyAt,
      // 冻结时刻取**最后一次成功应答**的 `server_time`：包络时间戳与转写断口
      // 都停在那一刻（设计章 4.5）。拿不到就沿用上一次的。
      frozenAt: lastReplyAt ?? state.frozenAt,
      lateText: null,
      stalledBeats,
    };
  }

  if (wasLost) {
    const lostSince = state.lostSinceMs ?? nowMs;
    return {
      level: '已恢复',
      lostSinceMs: null,
      gapSeconds: Math.max(0, Math.round((nowMs - lostSince) / 1000)),
      noticeSinceMs: nowMs,
      lastReplyAt,
      frozenAt: state.frozenAt,
      lateText: null,
      stalledBeats,
    };
  }

  if (reading.status === '迟到') {
    return {
      ...state,
      level: '迟到',
      noticeSinceMs: noticeExpired ? null : state.noticeSinceMs,
      lastReplyAt,
      lateText: lateHeartbeatText(reading.kind === 'reply' ? reading.lagSeconds : 0),
      stalledBeats,
    };
  }

  return {
    ...state,
    level: noticeExpired ? '正常' : state.level === '已恢复' ? '已恢复' : '正常',
    noticeSinceMs: noticeExpired ? null : state.noticeSinceMs,
    lastReplyAt,
    lateText: null,
    stalledBeats,
  };
}

/** 恢复记录还在不在露（30 秒）。 */
export function recoveredNoteVisible(state: HeartbeatState, nowMs: number): boolean {
  if (state.gapSeconds === null || state.noticeSinceMs === null) return false;
  return nowMs - state.noticeSinceMs < RECOVERED_NOTE_SECONDS * 1000;
}

/** `已恢复，中断 42 秒`。 */
export function recoveredNoteText(gapSeconds: number): string {
  return `已恢复，中断 ${Math.round(gapSeconds)} 秒`;
}

/** 失联横幅的正文几处。 */
export interface LostBannerView {
  /** `10 秒一次的应答，连续 2 次没收到（最后一次应答 20:57:31）。` */
  body: string;
  /** 最后一次应答的墙钟（带秒）：`20:57:31`；一次都没成功过就是 `—`。 */
  lastReply: string;
}

export function lostBanner(state: HeartbeatState, intervalSeconds: number): LostBannerView {
  const period = Math.max(1, Math.round(intervalSeconds));
  const lastReply = state.lastReplyAt === null ? '—' : formatWallClockSeconds(state.lastReplyAt);
  return {
    body: `${period} 秒一次的应答，连续 2 次没收到（最后一次应答 ${lastReply}）。`,
    lastReply,
  };
}

/**
 * 「采集已停」那一档。
 *
 * 服务**在应答**（HTTP 通、`server_time` 也新），但应答里的 `recording_alive` 为假
 * —— 采集线程已经停了（契约第 5 章：窗口 `max(2.0s, 3 × frame_interval)`，
 * 两秒没收到一帧事实就算停）。设计章 4.5 只规定「应答必须带录音存活字段」，
 * 没给这一档的成品文案；任务书「边界」第 6 条指定照失联横幅的结构写。
 *
 * **两拍规则**（de899128）：`stalledBeats`（`applyHeartbeatReading()` 记的连续
 * `false` 计数）要够 `CAPTURE_STOP_CONFIRMATIONS` 才判停 —— 开场「还没有第一帧」
 * 那一档服务报 `null`（未知），不进计数；单次 `false` 不弹横幅。真的饿死时
 * 计数照样能攒够，不会报不出来。
 *
 * `seconds_since_last_tick` 是「多久没收到帧」的原始读数；据此倒推出**最后一次收到帧**
 * 的墙钟时刻（分钟级）。它与失联横幅互斥（HTTP 不通才是失联），与致命级横幅也不同屏
 * —— 判据见 `alertsHiddenByHeartbeat()`。
 */
export interface CaptureStalledView {
  /** 已经多少秒没收到任何一路的音频帧。 */
  seconds: number;
  /** `HH:MM` —— 最后一次收到帧的墙钟时刻。 */
  lastFrameAt: string;
}

export function captureStalled(
  heartbeat: Heartbeat | null,
  topState: TopState,
  stalledBeats: number,
): CaptureStalledView | null {
  if (heartbeat === null) return null;
  // `idle` / `ended` 态服务给的 `recording_alive` 恒为 `null`（没有在录的场次），
  // 那一档说的是「这场还没在录」，摆出「采集已停」的横幅会吓人一跳。
  if (topState !== 'recording' && topState !== 'alert') return null;
  // `null` 说的是「第一帧还没到」：先按未知等一等，别急着弹横幅。
  if (heartbeat.recording_alive !== false) return null;
  // 两拍规则：单次 `false` 不判停（连续计数与这一格同源，见 HeartbeatState.stalledBeats）。
  if (stalledBeats < CAPTURE_STOP_CONFIRMATIONS) return null;
  const seconds = heartbeat.seconds_since_last_tick;
  if (seconds === null || !Number.isFinite(seconds)) {
    // `recording_alive` 为假但连「多久没收到帧」都还没记上：一帧都没收到过（超窗饿死档）。
    // 那一档的「最后一次收到帧」是「还没有过」，不编一个时刻。
    return { seconds: 0, lastFrameAt: '—' };
  }
  const lastFrameMs = Date.parse(heartbeat.server_time) - seconds * 1000;
  const lastFrameAt = Number.isNaN(lastFrameMs)
    ? '—'
    : formatWallClock(new Date(lastFrameMs).toISOString());
  return { seconds, lastFrameAt };
}

/**
 * 「本场曾中断」那条提示行的字（gil-stall-hole）。
 *
 * 数据来自心跳的 `capture_gaps`（帧空洞记录）：录中整进程冻结解冻后，发帧线程把真实
 * 空洞时长报上来，记录按整档粒度记 —— 冻结冻的是整个进程，两路一起丢，提示行因此
 * **不点名哪一路**；一路单独断流是另一件事，由致命级横幅在报。
 *
 * 它是**历史事实**的提示，与「采集已停」那一档（`captureStalled`，现在时）分工明确：
 * 恢复之后 `recording_alive` 回 `true`、停的那一档收掉，这条提示照旧摆着 ——
 * 「曾中断 X 秒，该段内容未记上」说的正是 `recording_alive` 说不出的那句话。
 *
 * 级别 = 提示级（design-intent 第 17 条）：只上一行字，**不出声、不弹红横幅、
 * 不进两拍计数**（`stalledBeats` 只看 `recording_alive`，与这一格无关）。
 * 服务没给这一格（旧服务 / 应答被截断）时按空记录处理，null 安全。
 */
export function captureGapNote(reply: Heartbeat | null): string | null {
  const gaps = reply?.capture_gaps;
  if (!gaps || gaps.total_count <= 0) return null;
  if (gaps.total_count === 1) {
    const only = gaps.recent[gaps.recent.length - 1]?.seconds ?? gaps.total_seconds;
    return `曾中断 ${Math.max(0, only).toFixed(1)} 秒，该段内容未记上`;
  }
  return `曾中断 ${gaps.total_count} 次共 ${Math.max(0, gaps.total_seconds).toFixed(1)} 秒，该段内容未记上`;
}

/**
 * 失联那条判据的单一出处（web-lost-stalled-predicate-duplication）：
 * 判定体与原先三处内联逐字一致 —— 「观察过一次应答之后」才算数（`lastReplyAt`
 * 有值），诚实的「我还没问到」不冒充「我问不到」（设计章 4.5）。
 */
export function lostPredicate(state: HeartbeatState): boolean {
  return state.level === '失联' && state.lastReplyAt !== null;
}

/**
 * 心跳失联时整块告警区收不收。
 *
 * 收。两句话不得同屏（设计章 4.5）：这时候屏幕上说「出事了」的地方只有全屏横幅。
 *
 * **一次应答都没成功过时不算**（`lastReplyAt` 还是空）：那一档「我还没问到」，
 * 收掉告警区会把一条真实存在的致命告警藏起来。判据 = `lostPredicate()`。
 */
export function alertsHiddenByHeartbeat(state: HeartbeatState): boolean {
  return lostPredicate(state);
}

/**
 * 时长那一格在失联时改标签 `最后读数`，数字停在最后一次应答那一刻。
 *
 * 停的那个数取本次**会话已录的秒数**（`session.elapsed_seconds`），服务侧一进
 * `ended` 就冻结了它；失联期间不再往前推 —— 界面不把估出来的时间继续走。
 */
export function clockLabelWhileLost(): string {
  return '最后读数';
}

/** 失联时那条「最后读数」的数字：沿用最后一份快照的已录时长。 */
export function frozenClockValue(elapsedSeconds: number | null | undefined): string {
  return formatElapsed(elapsedSeconds ?? 0);
}

/** 转写区在中断那一格留的断口：`[20:57–20:58] 采集中断`。 */
export interface TranscriptGap {
  from: string;
  to: string;
  text: string;
}

export function transcriptGap(frozenAt: string | null, recoveredAtIso: string): TranscriptGap | null {
  if (frozenAt === null) return null;
  return {
    from: formatWallClock(frozenAt),
    to: formatWallClock(recoveredAtIso),
    text: '采集中断',
  };
}
