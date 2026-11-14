/**
 * 会中视图的格式化：时长、墙钟时刻、dBFS 读数与柱高。组件只摆位置，不做任何格式化判断。
 *
 * **一条贯穿全篇的口径**：从归档转抄来的时间一律**分钟级**（界面自己的心跳时刻才是秒级）。
 */

/** 读数的减号用 U+2212。 */
export const MINUS = '\u2212';

/**
 * 路的显示名（`本地` / `远端`）：告警横幅、转写行、会后进度三处同一套叫法，唯一出处在这里
 * （与会中状态格读契约给的 `source.label` 是两回事，那一格界面不造名）。
 */
export function sourceDisplayName(source: string): string {
  return source === 'mic' ? '本地' : source === 'loopback' ? '远端' : source;
}

/** 电平表的刻度区间：低于 −60 dBFS 视为这一路在静默。 */
export const DBFS_FLOOR = -60;
export const DBFS_CEIL = 0;

export function formatNumber(value: number): string {
  if (!Number.isFinite(value)) return `${MINUS}0.0`;
  const fixed = Math.abs(value).toFixed(1);
  return value < 0 ? `${MINUS}${fixed}` : fixed;
}

/** **没有读数时退成 `—`**，不把估出来的数字顶着。 */
export function formatDbfs(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return `${formatNumber(value)} dBFS`;
}

/**
 * dBFS → 电平表高度百分比：这是**唯一**允许前端做的换算（设计稿给的刻度区间是 −60…0 dBFS，
 * 界面只把服务给的读数线性映射到 0–100%）。超出区间就夹住 —— 映射之外一根柱都不造。
 */
export function dbfsToPercent(value: number | null | undefined): number {
  if (value === null || value === undefined || !Number.isFinite(value)) return 0;
  const raw = ((value - DBFS_FLOOR) / (DBFS_CEIL - DBFS_FLOOR)) * 100;
  if (raw < 0) return 0;
  if (raw > 100) return 100;
  return raw;
}

function two(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

/**
 * `已录 HH:MM:SS` 的那个数字：按秒向下取整（向上取整会让「刚开始」的第一秒就显示 `00:00:01`），
 * 负数（时钟回拨之类）夹成 0。
 */
export function formatElapsed(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) {
    return '00:00:00';
  }
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return `${two(h)}:${two(m)}:${two(s)}`;
}

/**
 * ISO 时间戳 → 墙钟 `HH:MM`（分钟级）：`Date.parse` 拿本机时区；它不认的写法直接从字符串上
 * 截 `T` 后面那一段；两样都不行就给 `—`，不猜。
 */
export function formatWallClock(iso: string | null | undefined): string {
  if (iso === null || iso === undefined || iso === '') return '—';
  const ms = Date.parse(iso);
  if (!Number.isNaN(ms)) {
    const at = new Date(ms);
    return `${two(at.getHours())}:${two(at.getMinutes())}`;
  }
  const m = /T(\d{2}):(\d{2})/.exec(iso);
  if (m !== null) return `${m[1]}:${m[2]}`;
  return '—';
}

/** ISO 时间戳 → 带秒的墙钟 `HH:MM:SS`（只给心跳失联那一档用）。 */
export function formatWallClockSeconds(iso: string | null | undefined): string {
  if (iso === null || iso === undefined || iso === '') return '—';
  const ms = Date.parse(iso);
  if (!Number.isNaN(ms)) {
    const at = new Date(ms);
    return `${two(at.getHours())}:${two(at.getMinutes())}:${two(at.getSeconds())}`;
  }
  const m = /T(\d{2}):(\d{2}):(\d{2})/.exec(iso);
  if (m !== null) return `${m[1]}:${m[2]}:${m[3]}`;
  return '—';
}

/** 「已丢 N 分钟」里的那个数：不足一分钟按 1 分钟说（别写「已丢 0 分钟」）。 */
export function formatLostMinutes(seconds: number | null | undefined): number {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds <= 0) {
    return 1;
  }
  return Math.max(1, Math.round(seconds / 60));
}

/**
 * 时长那一格。录音中 / 告警：`已录 01:12:34`，**秒在走**；已结束：标签改成 `本场时长`，数字
 * 冻结；空闲：`已录 00:00:00`，灰。
 *
 * **「秒在走」是谁在走**：界面在服务给的读数上**加自己从收到这一份快照起走过的时间** ——
 * 两个数都是可核的事实，界面没有另造一条时间轴。快照一换新，`liveElapsed` 归零重算。
 */
export interface ClockView {
  label: string;
  value: string;
  faint: boolean;
}

export function clockView(
  topState: 'idle' | 'recording' | 'alert' | 'ended',
  elapsedSeconds: number | null | undefined,
  liveElapsedSeconds = 0,
): ClockView {
  if (topState === 'ended') {
    return { label: '本场时长', value: formatElapsed(elapsedSeconds ?? 0), faint: false };
  }
  if (topState === 'idle') {
    return { label: '已录', value: '00:00:00', faint: true };
  }
  const base = elapsedSeconds ?? 0;
  const live = Number.isFinite(liveElapsedSeconds) && liveElapsedSeconds > 0 ? liveElapsedSeconds : 0;
  return { label: '已录', value: formatElapsed(base + live), faint: false };
}
