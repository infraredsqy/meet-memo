import { describe, expect, it, vi, afterEach } from 'vitest';
import { createElement } from 'react';
import TestRenderer, { act } from 'react-test-renderer';
import { SettingsDrawer } from '../src/settings/SettingsDrawer';
import { snapshotSchema, type DevicesResponse, type Snapshot } from '../src/contract';
import { readFixture } from './fixtures';
import { DEVICE_LOCKED_HINT, HEARTBEAT_RANGE_HINT } from '../src/live/settings';

/**
 * 设置抽屉的挂载用例（`311f84f5`，免 DOM 跑法，手法照 `home-mount.test.tsx`）。
 *
 * 两条承重断言：
 * 1. **负面验收**：抽屉整棵子树里找不出 ASR 那五个参数的任何一个（键名或人话都不行）——
 *    spec 完成判定的原文「ASR 那五个参数不出现在这扇窗里」；
 * 2. **设备那一格**：空闲态选一个设备 = `settings_update` + `reprobe` 各打一次；
 *    会中（recording 快照）两路下拉禁用并写明原因。
 */

vi.mock('../src/api/devices', () => ({
  fetchDevices: vi.fn(),
  DEVICES_URL: '/api/devices',
}));

const fetchDevicesMock = vi.mocked(
  (await import('../src/api/devices')).fetchDevices,
);

function snapshotOf(name: 'idle' | 'recording'): Snapshot {
  return snapshotSchema.parse(readFixture('snapshot', name));
}

function devicesPayload(): DevicesResponse {
  return {
    mic: [
      {
        name: '假-Focusrite Analogue 1 + 2',
        host_api: 'MME',
        role: 'input',
        index: 0,
        channels: 4,
        is_default: true,
      },
    ],
    loopback: [
      {
        name: '假-CABLE Input (VB-Audio Virtual Cable)',
        host_api: '',
        role: 'render',
        index: 1,
        channels: 0,
        is_default: false,
      },
    ],
  };
}

async function mountDrawer(
  snapshot: Snapshot,
  handlers: {
    onSetting?: (key: string, value: unknown) => void;
    onReprobe?: () => void;
  } = {},
): Promise<TestRenderer.ReactTestRenderer> {
  fetchDevicesMock.mockResolvedValue(devicesPayload());
  let renderer!: TestRenderer.ReactTestRenderer;
  await act(async () => {
    renderer = TestRenderer.create(
      createElement(SettingsDrawer, {
        snapshot,
        onClose: () => undefined,
        onSetting: handlers.onSetting ?? (() => undefined),
        onReprobe: handlers.onReprobe ?? (() => undefined),
      }),
    );
  });
  return renderer;
}

/** 一棵子树里的全部文字（嵌套字符串拼起来）。 */
function textOf(node: TestRenderer.ReactTestInstance): string {
  const parts: string[] = [];
  const walk = (current: TestRenderer.ReactTestInstance): void => {
    for (const child of current.children) {
      if (typeof child === 'string') {
        parts.push(child);
        continue;
      }
      walk(child);
    }
  };
  walk(node);
  return parts.join(' ');
}

function findByTestId(
  renderer: TestRenderer.ReactTestRenderer,
  testId: string,
): TestRenderer.ReactTestInstance {
  const found = renderer.root.findAll(
    (node) => node.props['data-testid'] === testId,
  );
  expect(found, `该有一只 data-testid=${testId}`).toHaveLength(1);
  return found[0] as TestRenderer.ReactTestInstance;
}

afterEach(() => {
  fetchDevicesMock.mockReset();
});

describe('抽屉里摆了什么（负面验收：ASR 五参数不进这扇窗）', () => {
  it('四项都在：设备两路、告警提示音、心跳频率、快速总结默认窗口', async () => {
    const renderer = await mountDrawer(snapshotOf('idle'));
    const drawer = findByTestId(renderer, 'settings-drawer');
    const text = textOf(drawer);
    for (const phrase of [
      '本地（麦克风）',
      '远端（loopback）',
      '告警提示音',
      '心跳频率',
      '快速总结默认窗口',
      '15 分钟',
    ]) {
      expect(text, phrase).toContain(phrase);
    }
  });

  it('ASR 那五个参数一个都不出现（键名与它们的人话一起查）', async () => {
    const renderer = await mountDrawer(snapshotOf('idle'));
    const text = textOf(findByTestId(renderer, 'settings-drawer'));
    for (const banned of [
      'asr.max_segment_seconds',
      'asr.timeout_seconds',
      'asr.max_new_tokens',
      'asr.repeat_max_unit',
      'asr.repeat_min_coverage',
      'max_segment_seconds',
      'timeout_seconds',
      'max_new_tokens',
      'repeat_max_unit',
      'repeat_min_coverage',
      'ASR',
      '段长',
      '生成 token',
      '覆盖率',
    ]) {
      expect(text, `不该出现：${banned}`).not.toContain(banned);
    }
  });
});

describe('换设备那一格（选完重新探测，结果交给快照渲染）', () => {
  it('空闲态选一个设备：settings_update 带上键与值，紧跟着一次 reprobe', async () => {
    const onSetting = vi.fn();
    const onReprobe = vi.fn();
    const renderer = await mountDrawer(snapshotOf('idle'), { onSetting, onReprobe });

    const mic = findByTestId(renderer, 'device-mic');
    expect(mic.props.disabled).toBe(false);
    expect(mic.props.value).toBe('');
    await act(async () => {
      mic.props.onChange({ target: { value: '假-Focusrite Analogue 1 + 2' } });
    });

    expect(onSetting).toHaveBeenCalledWith('devices.mic', '假-Focusrite Analogue 1 + 2');
    expect(onReprobe).toHaveBeenCalledTimes(1);
  });

  it('选「按默认策略挑」交回的是 null（配置键上的没配），reprobe 照打', async () => {
    const onSetting = vi.fn();
    const onReprobe = vi.fn();
    const snap = snapshotOf('idle');
    snap.settings.devices.mic = '假-Focusrite Analogue 1 + 2';
    const renderer = await mountDrawer(snap, { onSetting, onReprobe });

    const mic = findByTestId(renderer, 'device-mic');
    expect(mic.props.value).toBe('假-Focusrite Analogue 1 + 2');
    await act(async () => {
      mic.props.onChange({ target: { value: '' } });
    });
    expect(onSetting).toHaveBeenCalledWith('devices.mic', null);
    expect(onReprobe).toHaveBeenCalledTimes(1);
  });

  it('会中（recording）两路下拉禁用，旁边那句写明「开场前定下来」', async () => {
    const onSetting = vi.fn();
    const renderer = await mountDrawer(snapshotOf('recording'), { onSetting });

    expect(findByTestId(renderer, 'device-mic').props.disabled).toBe(true);
    expect(findByTestId(renderer, 'device-loopback').props.disabled).toBe(true);
    const text = textOf(findByTestId(renderer, 'settings-drawer'));
    expect(text).toContain(DEVICE_LOCKED_HINT);
  });
});

describe('心跳那一格（越界当场拒，不发请求）', () => {
  it('合法值在 blur 那一刻交出去', async () => {
    const onSetting = vi.fn();
    const renderer = await mountDrawer(snapshotOf('idle'), { onSetting });
    const input = findByTestId(renderer, 'heartbeat-interval');

    await act(async () => {
      input.props.onChange({ target: { value: '42' } });
    });
    expect(onSetting).not.toHaveBeenCalled();
    await act(async () => {
      input.props.onBlur();
    });
    expect(onSetting).toHaveBeenCalledWith('heartbeat.interval_seconds', 42);
  });

  it('61 被当场拒绝：旁边那句写明范围，onSetting 一次都不打', async () => {
    const onSetting = vi.fn();
    const renderer = await mountDrawer(snapshotOf('idle'), { onSetting });
    const input = findByTestId(renderer, 'heartbeat-interval');

    await act(async () => {
      input.props.onChange({ target: { value: '61' } });
    });
    await act(async () => {
      input.props.onBlur();
    });

    expect(onSetting).not.toHaveBeenCalled();
    const text = textOf(findByTestId(renderer, 'settings-drawer'));
    expect(text).toContain(HEARTBEAT_RANGE_HINT);
  });
});

describe('告警提示音与缺省档', () => {
  it('勾选框读快照、改动交 settings_update', async () => {
    const onSetting = vi.fn();
    const renderer = await mountDrawer(snapshotOf('idle'), { onSetting });
    const checkbox = findByTestId(renderer, 'alarm-sound');
    expect(checkbox.props.checked).toBe(true);

    await act(async () => {
      checkbox.props.onChange({ target: { checked: false } });
    });
    expect(onSetting).toHaveBeenCalledWith('alarm.sound', false);
  });

  it('缺省档四档的预选跟快照走；点一档交 settings_update', async () => {
    const onSetting = vi.fn();
    const snap = snapshotOf('idle');
    snap.settings.summary_default_window = '30m';
    const renderer = await mountDrawer(snap, { onSetting });

    const pressed = renderer.root.findAll(
      (node) =>
        node.type === 'button' &&
        node.props['aria-pressed'] === true &&
        node.parent?.props['aria-label'] === '快速总结默认窗口',
    );
    expect(pressed).toHaveLength(1);
    expect(textOf(pressed[0] as TestRenderer.ReactTestInstance)).toBe('30 分钟');

    const full = renderer.root.findAll(
      (node) =>
        node.type === 'button' &&
        node.parent?.props['aria-label'] === '快速总结默认窗口' &&
        textOf(node) === '全场',
    );
    await act(async () => {
      (full[0] as TestRenderer.ReactTestInstance).props.onClick();
    });
    expect(onSetting).toHaveBeenCalledWith('summary.default_window', 'full');
  });
});
