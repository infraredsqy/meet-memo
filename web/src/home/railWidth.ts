/**
 * 左栏宽度的读、存、夹（两屏共用一条可拖的宽度）。
 *
 * 宽度是**界面偏好**：只记宽度这一件事，不记任何会话状态 —— 写盘只在 localStorage 的一格，
 * 不进服务端配置、不进快照。写法照 `theme.ts` 同一套：纯函数吃注入的读法，node 侧的挂载用例
 * 里 `localStorage` 与真 DOM 都不在场，纯函数照常可测。
 *
 * 三档尺寸都在这里（rem 计，随根字号缩放）：默认档 = 样式层 grid 列宽的缺省；下限档 = 左栏里
 * 场名 / 路径 / 状态三样仍读得出来的最小宽度；上限档 = 保守封顶。
 * 拖过头就停在边界上：夹的动作只有一个出处（`clampRailWidth`）。
 */

export const RAIL_WIDTH_STORAGE_KEY = 'meeting.railwidth';

/** 默认档（rem）：样式层 grid 列宽缺省的那一档。 */
export const RAIL_WIDTH_DEFAULT_REM = 17;

/** 下限档（rem）：场名 / 路径 / 状态三样仍读得出来的最小宽度。 */
export const RAIL_WIDTH_MIN_REM = 13.5;

/** 上限档（rem）：六样底边不被挤动、右边不被挤没的最大宽度。 */
export const RAIL_WIDTH_MAX_REM = 48;

/** 窄屏那一档的上沿（px）：≤840 左栏摞到内容上面、两栏不存在，拖柄不摆。样式管摆法，这里只管拖柄要不要。 */
export const RAIL_STACK_MAX_WIDTH_PX = 840;

/** 拖柄的媒体查询：窄屏那档 = matches。 */
export const RAIL_GRIP_MEDIA_QUERY = `(max-width: ${RAIL_STACK_MAX_WIDTH_PX}px)`;

/** 三档尺寸换算成像素（根字号注入）。 */
export function railWidthBoundsPx(remPx: number): { min: number; max: number; def: number } {
  return {
    min: Math.round(RAIL_WIDTH_MIN_REM * remPx),
    max: Math.round(RAIL_WIDTH_MAX_REM * remPx),
    def: Math.round(RAIL_WIDTH_DEFAULT_REM * remPx),
  };
}

/** 夹在上下限之间 —— 拖动与存档越界都走这一处。 */
export function clampRailWidth(px: number, remPx: number): number {
  const bounds = railWidthBoundsPx(remPx);
  return Math.min(bounds.max, Math.max(bounds.min, px));
}

/** 从存档读上一次的宽度；没存过、读不懂的值交回 `null`，存了越界的值夹回界内。 */
export function storedRailWidth(
  read: (key: string) => string | null,
  remPx: number,
): number | null {
  const raw = read(RAIL_WIDTH_STORAGE_KEY);
  if (raw === null) return null;
  const parsed = Number.parseFloat(raw);
  if (!Number.isFinite(parsed) || parsed <= 0) return null;
  return clampRailWidth(parsed, remPx);
}

/** 把宽度写到根节点的 `--rail-w` 上：两屏的两处 grid 列宽都读这一个变量。 */
export function applyRailWidth(
  px: number,
  style: { setProperty: (key: string, value: string) => void } | null | undefined,
): void {
  style?.setProperty('--rail-w', `${Math.round(px)}px`);
}

/** 根字号（1rem 的像素值）；读不到按 16 算（node 侧没有真 DOM）。 */
export function remPixelSize(): number {
  try {
    const parsed = Number.parseFloat(window.getComputedStyle(document.documentElement).fontSize);
    if (Number.isFinite(parsed) && parsed > 0) return parsed;
  } catch {
    // node 侧挂载用例没有真 DOM
  }
  return 16;
}

/** 此刻摆不摆拖柄：窄屏那一档没有可拖的两栏，拖柄不出现。matchMedia 不在场的环境按窄档算 —— 宁可不摆，不摆错。 */
export function railGripAllowedNow(): boolean {
  try {
    if (typeof window.matchMedia !== 'function') return false;
    return !window.matchMedia(RAIL_GRIP_MEDIA_QUERY).matches;
  } catch {
    return false;
  }
}
