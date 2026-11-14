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
 * 设置抽屉：换设备（两路各一个下拉，选完重新探测）、告警提示音、服务器心跳频率、
 * 快速总结默认窗口、会中切模型。**ASR 那五个参数一个都不进这扇窗**（`DRAWER_ROW_IDS`
 * 那份清单就是抽屉的全部行，一个不多）。
 *
 * 每一项的改动**当场写进 config**（`settings_update`，写完无需重启），换设备那一格
 * 紧跟着打一次 `reprobe`，探测结果由快照两路如实渲染。心跳输入越界当场拒绝并写明原因
 * （不发请求）；会中两路设备格禁用。
 *
 * **权威状态在服务的快照上**：勾选框与下拉的当前值都从 `snapshot.settings` 读，
 * 这里不持第二份配置源；设备清单只在抽屉打开（本组件挂载）时取一次。
 * 预设那一格只切**当前用哪一套**（名字清单由快照给）—— 预设内容在配置文件
 * `summary.presets_json` 里配，密钥**不进快照、不在这里回显**。
 */

export interface SettingsDrawerProps {
  snapshot: Snapshot;
  onClose: () => void;
  onSetting: (key: string, value: unknown) => void;
  onReprobe: () => void;
  fetchDevicesImpl?: () => Promise<DevicesResponse>;
}

type DevicesState =
  | { status: 'loading' }
  | { status: 'ready'; data: DevicesResponse }
  | { status: 'error'; text: string };

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

  const [heartbeatText, setHeartbeatText] = useState(() =>
    String(settings.heartbeat_interval_seconds),
);
  const [heartbeatError, setHeartbeatError] = useState<string | null>(null);
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

  /** 没配或形状不合就按空值 = 「按默认策略挑」读。 */
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

        <Field label="告警提示音" hint="采不到的时候响一声（跨场次记住）。">
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

        <Field
          label="心跳频率（秒）"
          htmlFor="settings-heartbeat"
          hint="每隔这么多秒问一次服务还在不在。"
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

        <Field label="快速总结默认窗口" hint="总结区预选落在哪一档。">
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

        <Field
          label="快速总结模型"
          htmlFor="settings-summary-preset"
          hint="换当场用哪一套，选完即生效；预设清单在配置文件 summary.presets_json 里配。"
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
