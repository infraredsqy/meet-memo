/**
 * 心跳本地态：四级（正常 / 迟到 / 失联 / 已恢复）、失联横幅的文案、以及「采集已停」那一档。
 *
 * **两件事不得同屏**：失联横幅说的是「界面看不见服务、整场录音已经中断」，`远端这一路采不
 * 到了` 那条致命横幅说的是「服务还在、某一路没了」。所以失联时整块告警区不出现；而「采集
 * 已停」那一档与致命级横幅也**不得同屏**。
 */

import { lateHeartbeatText, type HeartbeatTick } from '../api/heartbeat';
import type { Heartbeat, TopState } from '../contract';
import { formatElapsed, formatWallClock, formatWallClockSeconds } from './format';

export type HeartbeatLevel = '正常' | '迟到' | '失联' | '已恢复';

/** 恢复记录留多久。 */
export const RECOVERED_NOTE_SECONDS = 30;

/**
 * 「采集已停」要**连续这么多次读数**是 `false` 才判（两拍规则）。
 *
 * 开场那一拍服务报的是 `null`（还没有第一帧），单次 `false` 也可能是竞态窗口里的误读 ——
 * 一惊一乍的告警会被人关掉。计数是**界面层**的确认计数：任何非 `false` 读数
 * （`true` / `null` / 失联）把它清零。
 */
export const CAPTURE_STOP_CONFIRMATIONS = 2;

export interface HeartbeatState {
  level: HeartbeatLevel;
  lostSinceMs: number | null;
  gapSeconds: number | null;
  noticeSinceMs: number | null;
  lastReplyAt: string | null;
  frozenAt: string | null;
  lateText: string | null;
  /**
   * 连续多少次读数里 `recording_alive === false`（两拍规则的计数）。
   *
   * **界面层的单一归并点就是 `applyHeartbeatReading()`**：它在这里登记，冻结判据与横幅判据
   * 都只读这一个数 —— 两处各记各的就会漂。
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

/**
 * 把一次读数并进本地态（纯函数）。
 *
 * 「正常」这一档在界面上**什么都不显示**，所以进入正常就等于把迟到那一行收掉。
 */
export function applyHeartbeatReading(
  state: HeartbeatState,
  reading: HeartbeatTick,
  nowMs: number,
): HeartbeatState {
  const lastReplyAt = reading.kind === 'reply' ? reading.heartbeat.server_time : state.lastReplyAt;

  // 两拍规则的计数（单一归并点）：`false` 连着数，`true` / `null` / 失联一并清零。
  // `null` 是「还没有第一帧」的未知档，它重置计数正是开场不误报的关键。
  const stalledBeats =
    reading.kind === 'reply' && reading.heartbeat.recording_alive === false
      ? state.stalledBeats + 1
      : 0;

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
      // 冻结时刻取**最后一次成功应答**的 `server_time`，拿不到就沿用上一次的。
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

export function recoveredNoteVisible(state: HeartbeatState, nowMs: number): boolean {
  if (state.gapSeconds === null || state.noticeSinceMs === null) return false;
  return nowMs - state.noticeSinceMs < RECOVERED_NOTE_SECONDS * 1000;
}

export function recoveredNoteText(gapSeconds: number): string {
  return `已恢复，中断 ${Math.round(gapSeconds)} 秒`;
}

export interface LostBannerView {
  body: string;
  /** 最后一次应答的墙钟（带秒）；一次都没成功过就是 `—`。 */
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
 * —— 采集线程已经停了。它与失联横幅互斥（HTTP 不通才是失联），与致命级横幅也不同屏
 * —— 失联那条判据见 `lostPredicate()`。
 *
 * **两拍规则**：`stalledBeats` 要够 `CAPTURE_STOP_CONFIRMATIONS` 才判停 —— 开场「还没有第一帧」
 * 那一档服务报 `null`（未知），不进计数；单次 `false` 不弹横幅。
 */
export interface CaptureStalledView {
  /** 已经多少秒没收到任何一路的音频帧。 */
  seconds: number;
  lastFrameAt: string;
}

export function captureStalled(
  heartbeat: Heartbeat | null,
  topState: TopState,
  stalledBeats: number,
): CaptureStalledView | null {
  if (heartbeat === null) return null;
  // `idle` / `ended` 态说「这场还没在录」，摆出「采集已停」的横幅会吓人一跳。
  if (topState !== 'recording' && topState !== 'alert') return null;
  // `null` 说的是「第一帧还没到」：先按未知等一等，别急着弹横幅。
  if (heartbeat.recording_alive !== false) return null;
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
 * 数据来自心跳的 `capture_gaps`（帧空洞记录）：冻结冻的是整个进程，两路一起丢，提示行因此
 * **不点名哪一路**；一路单独断流是另一件事，由致命级横幅在报。
 *
 * 它是**历史事实**的提示，与「采集已停」那一档（现在时）分工明确：恢复之后停的那一档收掉，
 * 这条提示照旧摆着。级别 = 提示级：只上一行字，**不出声、不弹红横幅、不进两拍计数**。
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
 * 失联那条判据的单一出处：「观察过一次应答之后」才算数（`lastReplyAt` 有值），
 * 诚实的「我还没问到」不冒充「我问不到」。
 */
export function lostPredicate(state: HeartbeatState): boolean {
  return state.level === '失联' && state.lastReplyAt !== null;
}

/**
 * 时长那一格在失联时改标签 `最后读数`，数字停在最后一次应答那一刻 —— 界面不把估出来的
 * 时间继续走。
 */
export function clockLabelWhileLost(): string {
  return '最后读数';
}

/** 沿用最后一份快照的已录时长。 */
export function frozenClockValue(elapsedSeconds: number | null | undefined): string {
  return formatElapsed(elapsedSeconds ?? 0);
}
