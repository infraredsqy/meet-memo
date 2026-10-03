import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { DevicesResponse, Snapshot } from '../contract';
import { fetchDevices } from '../api/devices';
import {
  DEVICE_LOCKED_HINT,
  SUMMARY_DEFAULT_WINDOW_OPTIONS,
  checkedOf,
  deviceChangeLocked,
  deviceOptionsOf,
  normalizeActivePreset,
  normalizeDefaultWindow,
  parseHeartbeatInput,
  presetOptionsOf,
  stringValueOf,
} from '../live/settings';

/**
 * 设置抽屉（`docs/ui-design.md` §7.10 的 `drawer` / `drawer--open` / `scrim`；
 * R32：一个设置按钮 → 网页内小窗 → 写进 config，节点 `311f84f5`）。
 *
 * **抽屉里只有五项**（`DRAWER_ROW_IDS`，设计意图第 15 条「放」的那一半）：换设备
 * （两路各一个下拉，选完重新探测）、告警提示音、服务器心跳频率（1–60 秒）、快速总结
 * 默认窗口（四档）、会中切模型（多套预设里换当前用哪一套，本节点）。
 * **ASR 那五个参数一个都不进这扇窗**（设计意图第 14 条；`DRAWER_ROW_IDS` 那份清单
 * 就是抽屉的全部行，一个不多）。
 *
 * 每一项的改动**当场写进 config**（`settings_update`；这五项全收在服务的热读档，
 * 写完无需重启），换设备那一格紧跟着打一次 `reprobe`，探测结果由快照两路如实渲染。
 * 心跳输入越界当场拒绝并写明原因（不发请求）；会中两路设备格禁用
 * （`docs/contract.md` 第 7 章：会中换设备不做）。
 *
 * **权威状态在服务的快照上**：勾选框与下拉的当前值都从 `snapshot.settings` 读，
 * 这里不持第二份配置源；设备清单只在抽屉打开（本组件挂载）时取一次。
 * 预设那一格只切**当前用哪一套**（名字清单由快照给）—— 预设内容（端点四件套）在
 * 配置文件 `summary.presets_json` 里配，密钥**不进快照、不在这里回显**。
 */

export interface SettingsDrawerProps {
  /** 服务推来的那一份快照（每一格的当前值都从 `settings` 上读）。 */
  snapshot: Snapshot;
  /** 关抽屉（遮罩点击与右上角那只都走它）。 */
  onClose: () => void;
  /** 写一项设置（组件只报「哪个键、什么值」，动作怎么打归调用方）。 */
  onSetting: (key: string, value: unknown) => void;
  /** 重新探测一次设备（换设备选完之后调用）。 */
  onReprobe: () => void;
  /** 覆盖取数（测试注入用；缺省打真的 `GET /api/devices`）。 */
  fetchDevicesImpl?: () => Promise<DevicesResponse>;
}

type DevicesState =
  | { status: 'loading' }
  | { status: 'ready'; data: DevicesResponse }
  | { status: 'error'; text: string };

/** 一格设置项（`docs/ui-design.md` §7.10 的 `field` 一族：标签 / 控件 / 说明 / 失败原因）。 */
function Field({
  label,
  hint,
  error,
  htmlFor,
  children,
}: {
  label: string;
  hint?: string;
  error?: string | null;
  htmlFor?: string;
  children: ReactNode;
}): ReactNode {
  return (
    <div className="field">
      <label className="field__label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {hint !== undefined && <span className="field__hint">{hint}</span>}
      {error != null && error !== '' && <span className="field__error">{error}</span>}
    </div>
  );
}

export function SettingsDrawer({
  snapshot,
  onClose,
  onSetting,
  onReprobe,
  fetchDevicesImpl,
}: SettingsDrawerProps): ReactNode {
  const settings = snapshot.settings;
  const locked = deviceChangeLocked(snapshot.top_state);

  /** 设备清单：抽屉打开（挂载）时取一次，取不到就如实写一行。 */
  const [devices, setDevices] = useState<DevicesState>({ status: 'loading' });
  useEffect(() => {
    let alive = true;
    const load = fetchDevicesImpl ?? fetchDevices;
    load().then(
      (data) => {
        if (alive) setDevices({ status: 'ready', data });
      },
      (error: unknown) => {
        if (alive) {
          setDevices({
            status: 'error',
            text: error instanceof Error ? error.message : String(error),
          });
        }
      },
    );
    return () => {
      alive = false;
    };
  }, [fetchDevicesImpl]);

  /** 心跳那一格的本地输入：合法值在 blur / Enter 那一刻落盘，越界值当场拒绝。 */
  const [heartbeatText, setHeartbeatText] = useState(() =>
    String(settings.heartbeat_interval_seconds),
  );
  const [heartbeatError, setHeartbeatError] = useState<string | null>(null);
  /** 用户改到一半的输入不动它；没在改的那一份跟着快照走（服务是唯一来源）。 */
  const heartbeatDirty = useRef(false);
  const snapshotHeartbeat = String(settings.heartbeat_interval_seconds);
  useEffect(() => {
    if (!heartbeatDirty.current) setHeartbeatText(snapshotHeartbeat);
  }, [snapshotHeartbeat]);

  const commitHeartbeat = (): void => {
    heartbeatDirty.current = false;
    const verdict = parseHeartbeatInput(heartbeatText);
    if (verdict.kind === 'invalid') {
      setHeartbeatError(verdict.reason);
      setHeartbeatText(snapshotHeartbeat);
      return;
    }
    setHeartbeatError(null);
    setHeartbeatText(String(verdict.value));
    if (verdict.value !== settings.heartbeat_interval_seconds) {
      onSetting('heartbeat.interval_seconds', verdict.value);
    }
  };

  const micOptions = deviceOptionsOf(devices.status === 'ready' ? devices.data : null, 'mic');
  const loopbackOptions = deviceOptionsOf(
    devices.status === 'ready' ? devices.data : null,
    'loopback',
  );

  /** 快照里配的设备名（没配或形状不合就按空值 = 「按默认策略挑」读）。 */
  const configuredDevice = (key: 'mic' | 'loopback'): string => {
    const value = settings.devices[key];
    return typeof value === 'string' ? value : '';
  };

  const changeDevice = (key: 'devices.mic' | 'devices.loopback', value: string): void => {
    if (locked) return;
    // 空值 = 没配（按默认策略挑），配置键上就是 `null`
    onSetting(key, value === '' ? null : value);
    onReprobe();
  };

  return (
    <>
      <div className="scrim" role="presentation" onClick={onClose} />
      <aside
        className="drawer drawer--open"
        role="dialog"
        aria-modal="true"
        aria-label="设置"
        data-testid="settings-drawer"
      >
        <div className="drawer__top">
          <h2 className="title-md">设置</h2>
          <button className="btn btn--small btn--ghost" type="button" onClick={onClose}>
            关闭
          </button>
        </div>

        {/* 一、换设备：两路各一个下拉（会中禁用；选完重新探测，结果由快照如实渲染） */}
        <Field
          label="本地（麦克风）"
          htmlFor="settings-device-mic"
          hint={locked ? DEVICE_LOCKED_HINT : undefined}
        >
          <select
            id="settings-device-mic"
            className="field__input"
            data-testid="device-mic"
            disabled={locked || devices.status !== 'ready'}
            value={configuredDevice('mic')}
            onChange={(event) => changeDevice('devices.mic', stringValueOf(event))}
          >
            {devices.status === 'loading' && <option value="">正在取设备清单…</option>}
            {devices.status === 'error' && <option value="">设备清单取不到</option>}
            {devices.status === 'ready' &&
              micOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
          </select>
        </Field>
        <Field
          label="远端（loopback）"
          htmlFor="settings-device-loopback"
          hint={locked ? DEVICE_LOCKED_HINT : undefined}
        >
          <select
            id="settings-device-loopback"
            className="field__input"
            data-testid="device-loopback"
            disabled={locked || devices.status !== 'ready'}
            value={configuredDevice('loopback')}
            onChange={(event) => changeDevice('devices.loopback', stringValueOf(event))}
          >
            {devices.status === 'loading' && <option value="">正在取设备清单…</option>}
            {devices.status === 'error' && <option value="">设备清单取不到</option>}
            {devices.status === 'ready' &&
              loopbackOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
          </select>
        </Field>
        {devices.status === 'error' && (
          <p className="field__error">{devices.text}</p>
        )}

        {/* 二、告警提示音：与主视图顶栏同一个设置 */}
        <Field label="告警提示音" hint="采不到的时候这台机器的音箱里响一声（跨场次记住）。">
          <span className="kv">
            <input
              type="checkbox"
              id="alarm-switch-drawer"
              data-testid="alarm-sound"
              checked={settings.alarm_sound}
              onChange={(event) => onSetting('alarm.sound', checkedOf(event))}
            />
            <label className="kv__k" htmlFor="alarm-switch-drawer">
              出一声
            </label>
          </span>
        </Field>

        {/* 三、心跳频率：1–60 秒，越界当场拒绝并说明原因 */}
        <Field
          label="心跳频率（秒）"
          htmlFor="settings-heartbeat"
          hint="界面每隔这么多秒问一次服务还在不在。"
          error={heartbeatError}
        >
          <input
            id="settings-heartbeat"
            data-testid="heartbeat-interval"
            className="field__input"
            type="number"
            inputMode="numeric"
            min={1}
            max={60}
            value={heartbeatText}
            onChange={(event) => {
              heartbeatDirty.current = true;
              setHeartbeatText(stringValueOf(event));
              setHeartbeatError(null);
            }}
            onBlur={commitHeartbeat}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                heartbeatDirty.current = false;
                commitHeartbeat();
              }
            }}
          />
        </Field>

        {/* 四、快速总结默认窗口：四档预选随快照走（`settings.summary_default_window`） */}
        <Field label="快速总结默认窗口" hint="总结区预选落在哪一档；跟着这里的设置走。">
          <div className="seg" role="group" aria-label="快速总结默认窗口">
            {SUMMARY_DEFAULT_WINDOW_OPTIONS.map((item) => (
              <button
                key={item.value}
                type="button"
                className={`seg__item ${
                  normalizeDefaultWindow(settings.summary_default_window) === item.value
                    ? 'seg__item--on'
                    : ''
                }`}
                aria-pressed={
                  normalizeDefaultWindow(settings.summary_default_window) === item.value
                }
                onClick={() => onSetting('summary.default_window', item.value)}
              >
                {item.label}
              </button>
            ))}
          </div>
        </Field>

        {/* 五、会中切模型（多套预设）：只换当前用哪一套，写盘即生效不用重启。
            预设内容在配置文件里配（summary.presets_json）；密钥不进快照、不在这里回显。 */}
        <Field
          label="快速总结模型"
          htmlFor="settings-summary-preset"
          hint="预设清单在配置文件里配（summary.presets_json）；这里换当场用哪一套，选完即生效。"
        >
          <select
            id="settings-summary-preset"
            className="field__input"
            data-testid="summary-active-preset"
            value={normalizeActivePreset(settings)}
            onChange={(event) => onSetting('summary.active_preset', stringValueOf(event))}
          >
            {presetOptionsOf(settings).map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </Field>
      </aside>
    </>
  );
}
