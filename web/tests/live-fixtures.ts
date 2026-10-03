import type {
  Alert,
  Alerts,
  Heartbeat,
  LevelsData,
  Settings,
  Snapshot,
  Source,
  SourceId,
} from '../src/contract';

/**
 * 用例用的快照构造器。
 *
 * **为什么不用 `contract/fixtures/` 的 golden 样本**：那 25 份是**掩码过**的
 * （`<iso>` / `<dbfs>` / `<session-id>`），它们的用处是「形状对不对」，
 * 而这一批用例验的是「拿到真值之后算出来的字对不对」——`<iso>` 算不出 `20:57`。
 * 所以这里从零造一份**形状与契约逐键相同、值是真值**的快照，仅用于用例。
 *
 * 形状的权威在 `docs/contract.md` 第 2 章，逐键对着写。
 */

export const T = (hhmmss: string): string => `2026-09-21T${hhmmss}+08:00`;

export function makeSettings(overrides: Partial<Settings> = {}): Settings {
  return {
    alarm_sound: true,
    heartbeat_interval_seconds: 10,
    summary_default_window: '15m',
    summary_preset_names: [],
    summary_active_preset: '',
    asr: {
      max_segment_seconds: 10,
      timeout_seconds: 20,
      max_new_tokens: 400,
      repeat_max_unit: 40,
      repeat_min_coverage: 0.5,
    },
    devices: { mic: null, loopback: null },
    ...overrides,
  };
}

export function makeSource(id: SourceId, overrides: Partial<Source> = {}): Source {
  const label = id === 'mic' ? '本地' : '远端';
  return {
    id,
    label,
    device_name: id === 'mic' ? '麦克风 (USB Audio)' : '扬声器 (VB-CABLE 2ch)',
    health: 'capturing',
    lost_since: null,
    restored: false,
    level_dbfs: id === 'mic' ? -35.2 : -48.0,
    peak_dbfs: id === 'mic' ? -30.1 : -44.0,
    // 包络是**真实 dBFS 读数**（契约第 2 章：一根柱 = 那 2 秒窗口里 peak 的最大值），
    // 所以这里给的是 −37.5 … −20.9 这一档的负数值，不是 0–100 的高度。
    envelope: [-37.5, -35.2, -38.4, -34.4, -32.9, -36.4, -33.6, -34.7, -39.1, -36.9, -34.2, -35.2, -38, -36, -37.1],
    ...overrides,
  };
}

export function makeAlert(overrides: Partial<Alert> = {}): Alert {
  return {
    level: 'critical',
    source: 'loopback',
    since: T('20:57:00'),
    duration_seconds: 780,
    message: 'loopback 这一路静默已连续达 180 秒（门限 -60 dBFS）',
    muted: false,
    ...overrides,
  };
}

export function makeAlerts(overrides: Partial<Alerts> = {}): Alerts {
  return { critical: null, hints: [], ...overrides };
}

export function makeSnapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    revision: 7,
    service: {
      started_at: T('20:00:00'),
      version: '0.1.0',
      port: 8791,
      url: 'http://127.0.0.1:8791',
      log_path: 'C:\\Users\\<user>\\.config\\meeting\\logs\\meeting.log',
    },
    top_state: 'recording',
    session: {
      id: '跟老王聊周会',
      dir: 'C:\\会议\\跟老王聊周会',
      started_at: T('20:45:20'),
      mode: 'both',
      elapsed_seconds: 4354,
      frozen: false,
      paused: false,
    },
    sources: [makeSource('mic'), makeSource('loopback')],
    alerts: makeAlerts(),
    transcript: {
      health: 'normal',
      last_line_at: T('20:57:00'),
      buffered_lines: 0,
      failed_segments: 0,
    },
    ended: null,
    settings: makeSettings(),
    ...overrides,
  };
}

export function makeHeartbeat(overrides: Partial<Heartbeat> = {}): Heartbeat {
  return {
    service: 'meeting',
    server_time: T('20:57:31'),
    revision: 7,
    top_state: 'recording',
    recording_alive: true,
    seconds_since_last_tick: 0.4,
    ...overrides,
  };
}

export function makeLevels(
  micDbfs: number,
  loopDbfs: number,
  revision = 7,
): LevelsData {
  const frame = (dbfs: number) => ({
    ts: null,
    dbfs,
    peak: dbfs + 5,
    bar: dbfs,
    envelope: new Array<number>(15).fill(dbfs),
  });
  return {
    revision,
    frames: { mic: frame(micDbfs), loopback: frame(loopDbfs) },
  };
}
