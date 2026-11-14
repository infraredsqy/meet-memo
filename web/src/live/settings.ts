import {
  HEARTBEAT_INTERVAL_MAX_SECONDS,
  HEARTBEAT_INTERVAL_MIN_SECONDS,
  type DevicesResponse,
  type Settings,
  type Snapshot,
  type SummaryWindowChoice,
} from '../contract';
import { SUMMARY_WINDOWS } from './summary';

/**
 * 设置抽屉那份**纯逻辑**。
 *
 * 它认**服务推来的那份快照**与**服务给的那份设备清单**，算出抽屉该摆什么、哪一格按不动、
 * 按不动时旁边写什么。组件只把它摆到位置上。
 *
 * **三条边界写死在这里**：
 *
 * 1. **ASR 那五个参数一个都不进抽屉**（`DRAWER_ROW_IDS` 是抽屉的全部行，一个不多）；
 * 2. **会中（`recording` / `alert`）不换设备** —— 两路设备格禁用并写明原因；
 * 3. **心跳 1–60 秒**，越界当场拒绝并说明原因（不发请求）。
 */

/** 抽屉的全部行。**一个不多**。 */
export const DRAWER_ROW_IDS = [
  'device-mic',
  'device-loopback',
  'alarm-sound',
  'heartbeat-interval',
  'summary-default-window',
  'summary-active-preset',
] as const;

export type DrawerRowId = (typeof DRAWER_ROW_IDS)[number];

/** 心跳输入越界时旁边那句。 */
export const HEARTBEAT_RANGE_HINT = `只能填 1 到 ${HEARTBEAT_INTERVAL_MAX_SECONDS} 秒。`;

/** 会中两路设备格禁用时旁边那句。 */
export const DEVICE_LOCKED_HINT = '会中不换设备：这一场的设备在开场前定下来。';

/** 心跳输入的判读：1–60 的整数才放行；越界/非数一律当场拒，不发请求。 */
export type HeartbeatInput =
  | { kind: 'ok'; value: number }
  | { kind: 'invalid'; reason: string };

export function parseHeartbeatInput(text: string): HeartbeatInput {
  const trimmed = text.trim();
  if (!/^\d+$/.test(trimmed)) {
    return { kind: 'invalid', reason: HEARTBEAT_RANGE_HINT };
  }
  const value = Number.parseInt(trimmed, 10);
  if (
    value < HEARTBEAT_INTERVAL_MIN_SECONDS ||
    value > HEARTBEAT_INTERVAL_MAX_SECONDS
) {
    return { kind: 'invalid', reason: HEARTBEAT_RANGE_HINT };
  }
  return { kind: 'ok', value };
}

export function deviceChangeLocked(topState: Snapshot['top_state']): boolean {
  return topState === 'recording' || topState === 'alert';
}

/** 设备下拉的一项：值是**设备名字符串**（配置键存的就是它）。 */
export interface DeviceOption {
  /** 送进 `settings_update` 的值（去重靠它）。 */
  value: string;
  label: string;
}

/**
 * 服务给的端点表 → 一路的下拉项：同名端点在多个 host API 下会重复出现，配置按**名字**挑，
 * 选项按名字去重并在标签里把 host API 摆出来。
 */
export function deviceOptionsOf(
  response: DevicesResponse | null,
  source: 'mic' | 'loopback',
): DeviceOption[] {
  if (response === null) return [];
  const rows = source === 'mic' ? response.mic : response.loopback;
  const byName = new Map<string, DeviceOption>();
  for (const item of rows) {
    const suffix = item.host_api === '' ? '' : `（${item.host_api}）`;
    const marker = item.is_default ? ' · 系统默认' : '';
    const existing = byName.get(item.name);
    if (existing === undefined) {
      byName.set(item.name, { value: item.name, label: `${item.name}${suffix}${marker}` });
      continue;
    }
    // 同名多条：标签并上这个 host API（首次见到的不带 host API 标注时补一次）
    if (suffix !== '' && !existing.label.includes(`（${item.host_api}）`)) {
      existing.label = `${item.name}（多路 host API）${marker}`;
    }
  }
  const options = [...byName.values()];
  // 「按默认策略挑」永远在第一个：值 `null` 在 `settings_update` 里就是清空配置
  return [{ value: '', label: '按默认策略挑' }, ...options];
}

/** 抽屉里快速总结缺省档那四档（与会中总结区同源）。 */
export const SUMMARY_DEFAULT_WINDOW_OPTIONS = SUMMARY_WINDOWS.map((item) => ({
  value: item.choice as SummaryWindowChoice,
  label: item.label,
}));

/** 当前生效的缺省档在不在四档里（服务只发四档；形态上兜一道，认不出落 `15m` 那档）。 */
export function normalizeDefaultWindow(value: string): SummaryWindowChoice {
  return (SUMMARY_DEFAULT_WINDOW_OPTIONS.some((item) => item.value === value)
    ? value
    : '15m') as SummaryWindowChoice;
}

/** 「不选预设」那一档的标签（值 `''`：总结用平铺四键那一路）。 */
export const NO_PRESET_LABEL = '不选预设（用平铺四键）';

/** 总结模型下拉的一项：值是**预设名字**（`summary.active_preset` 存的就是它）。 */
export interface PresetOption {
  /** 送进 `settings_update` 的值（空串 = 不选预设）。 */
  value: string;
  /** 下拉里那一行的字（不显示端点，更不显示密钥）。 */
  label: string;
}

/** 快照里的名字清单 → 下拉项：「不选预设」恒在第一档，其后按服务的次序。 */
export function presetOptionsOf(settings: Settings): PresetOption[] {
  return [
    { value: '', label: NO_PRESET_LABEL },
    ...settings.summary_preset_names.map((name) => ({ value: name, label: name })),
  ];
}

/** 当前用哪一套的**生效值**在不在清单里：指向的名字不在清单里时按「不选预设」读，与服务侧的回落口径一致。 */
export function normalizeActivePreset(settings: Settings): string {
  const active = settings.summary_active_preset;
  return settings.summary_preset_names.includes(active) ? active : '';
}

// change 事件的**结构式读法**：设置抽屉组件会被 tests/ 那条 tsconfig（node 线，`lib` 里没有
// DOM）顺着用例 import 进来，那一侧的 `HTMLSelectElement` 摸不着 `.value` —— 事件载荷在这里
// 按**结构**读，两套 lib 下都成立。

/** 不是字符串就按空串读。 */
export function stringValueOf(event: { target?: unknown }): string {
  const target = event.target as { value?: unknown } | null | undefined;
  const value = target?.value;
  return typeof value === 'string' ? value : '';
}

/** 只有明写 `true` 才算勾上。 */
export function checkedOf(event: { target?: unknown }): boolean {
  const target = event.target as { checked?: unknown } | null | undefined;
  return target?.checked === true;
}
