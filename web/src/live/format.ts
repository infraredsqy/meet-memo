/**
 * 会中视图的格式化：时长、墙钟时刻、dBFS 读数与柱高。
 *
 * 全在这一份纯函数里：组件只摆位置，不做任何格式化判断（交付要求第 3 条）。
 *
 * **一条贯穿全篇的口径（设计章 8.10 门槛 10）**：从归档转抄来的时间一律**分钟级**
 * —— 转写行的 `[HH:MM]`、包络床右侧的时间戳，这些地方不出现秒。界面对服务发心跳、
 * 自己读自己的时钟，那两个时刻的精度是界面自己的，秒级在那儿才是对的（见 `heartbeat.ts`）。
 */

/** 读数的减号用 U+2212（`−35.2 dBFS`），与设计稿刻度尺上的 `−20` / `−40` / `−60` 同一个字。 */
export const MINUS = '\u2212';

/** 电平表的刻度区间：低于 −60 dBFS 视为这一路在静默（设计章 4.4）。 */
export const DBFS_FLOOR = -60;
export const DBFS_CEIL = 0;

/** `0` → `0.0`、`−35.25` → `−35.3`：定长一位小数，负数用 U+2212。 */
export function formatNumber(value: number): string {
  if (!Number.isFinite(value)) return `${MINUS}0.0`;
  const fixed = Math.abs(value).toFixed(1);
  return value < 0 ? `${MINUS}${fixed}` : fixed;
}

/** `−35.2 dBFS`。**没有读数时退成 `—`**，不把估出来的数字顶着（设计章 4.5 失联档）。 */
export function formatDbfs(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return `${formatNumber(value)} dBFS`;
}

/**
 * dBFS → 电平表高度百分比。
 *
 * 这是**唯一**允许前端做的换算：设计稿给的刻度区间是 −60…0 dBFS，
 * 界面只把服务给的读数线性映射到 0–100%（`docs/ui-design.md` 章 7.7 的 `--level`）。
 * 超出区间就夹住 —— 映射之外一根柱都不造。
 */
export function dbfsToPercent(value: number | null | undefined): number {
  if (value === null || value === undefined || !Number.isFinite(value)) return 0;
  const raw = ((value - DBFS_FLOOR) / (DBFS_CEIL - DBFS_FLOOR)) * 100;
  if (raw < 0) return 0;
  if (raw > 100) return 100;
  return raw;
}

/** `630` → `10:30`；跨过 24 小时往下走（`90061` → `25:01:01` 是时长那一格的事，这里只管进位）。 */
function two(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

/**
 * `已录 HH:MM:SS` 的那个数字。
 *
 * 服务给的 `session.elapsed_seconds` 是**重复小数**（服务侧 `round(…, 3)`），
 * 这里按秒向下取整 —— 向上取整会让「刚开始」的第一秒就显示 `00:00:01`。
 * 负数（时钟回拨之类）夹成 0，不显示 `-1:59:59` 这种没有意义的读数。
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
 * ISO 时间戳 → 墙钟 `HH:MM`（分钟级）。
 *
 * 三条路，按可靠程度排：`Date.parse` 拿本机时区；`Date.parse` 不认的写法
 * 直接从字符串上截 `T` 后面那一段（服务产出的就是本地时区、秒精度，
 * 截出来的正是那一刻的钟点）；两样都不行就给 `—`，不猜。
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

/** ISO 时间戳 → 带秒的墙钟 `HH:MM:SS`（只给心跳失联那一档用，见门槛 10 的例外表）。 */
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

/** ISO 时间戳 → 本地日期时间的输入框取值（会后的元信息行用得到；设置抽屉的设备格是下拉）。 */
export function formatDateInput(iso: string | null | undefined): string {
  if (iso === null || iso === undefined || iso === '') return '';
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return '';
  const at = new Date(ms);
  return `${at.getFullYear()}-${two(at.getMonth() + 1)}-${two(at.getDate())}T${two(at.getHours())}:${two(at.getMinutes())}`;
}

/** `已丢 13 分钟` 里的那个数：分钟向下取整，不足一分钟按 1 分钟说（别写「已丢 0 分钟」）。 */
export function formatLostMinutes(seconds: number | null | undefined): number {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds <= 0) {
    return 1;
  }
  return Math.max(1, Math.round(seconds / 60));
}

/**
 * 时长那一格。
 *
 * - 录音中 / 告警：`已录 01:12:34`，**秒在走**；
 * - 已结束（收尾中 / 已完成 / 收尾失败）：标签改成 `本场时长`，数字冻结；
 * - 空闲：`已录 00:00:00`，灰。
 *
 * **「秒在走」是谁在走**：`session.elapsed_seconds` 是服务在**每一次提交快照**那一刻
 * 现算的读数，而录音途中快照并不每秒提交一次（`levels` / `transcript` 两路增量不占
 * `revision`）。所以界面在这一个读数上**加自己从收到这一份快照起走过的时间**
 * （`liveElapsed`）—— 两个数都是可核的事实，界面没有另造一条时间轴。
 * 快照一换新（含 `ended` 那次提交），`liveElapsed` 归零重算。
 */
export interface ClockView {
  label: string;
  value: string;
  /** 空闲态：数字退到后排（`.clock--faint`）。 */
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
