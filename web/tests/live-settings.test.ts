import { describe, expect, it } from 'vitest';
import type { DevicesResponse } from '../src/contract';
import {
  HEARTBEAT_RANGE_HINT,
  NO_PRESET_LABEL,
  deviceChangeLocked,
  deviceOptionsOf,
  normalizeActivePreset,
  normalizeDefaultWindow,
  parseHeartbeatInput,
  presetOptionsOf,
} from '../src/live/settings';
import { makeSettings } from './live-fixtures';

/**
 * 设置抽屉那份纯逻辑的用例（``）。
 *
 * 三条边界各有一条判据钉在这里：ASR 五参数一个都不进抽屉（`DRAWER_ROW_IDS` 的穷举）、
 * 心跳 1–60 越界当场拒（不发请求）、会中两路设备格禁用（`docs/contract.md` 7）。
 */

/** 两路各两三条的假清单（形状与 `contract/fixtures/devices/all.json` 同源）。 */
function devicesResponse(): DevicesResponse {
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
      {
        name: '假-CABLE-Output (VB-Audio Virtual Cable)',
        host_api: 'MME',
        role: 'input',
        index: 2,
        channels: 16,
        is_default: false,
      },
    ],
    loopback: [
      { name: '假-扬声器 (Focusrite USB Audio)', host_api: '', role: 'render', index: 0,
        channels: 0, is_default: false },
      { name: '假-CABLE Input (VB-Audio Virtual Cable)', host_api: '', role: 'render',
        index: 1, channels: 0, is_default: false },
    ],
  };
}

describe('心跳输入（1–60 秒，越界当场拒）', () => {
  it('1 与 60 两端都放行，中间值原样交回数字', () => {
    expect(parseHeartbeatInput('1')).toEqual({ kind: 'ok', value: 1 });
    expect(parseHeartbeatInput('60')).toEqual({ kind: 'ok', value: 60 });
    expect(parseHeartbeatInput(' 42 ')).toEqual({ kind: 'ok', value: 42 });
  });

  it('0、61、负数、小数、非数、空白：一律拒，原因写明范围', () => {
    for (const text of ['0', '61', '-5', '1.5', 'abc', '', '  ']) {
      const verdict = parseHeartbeatInput(text);
      expect(verdict.kind, text).toBe('invalid');
      if (verdict.kind === 'invalid') {
        expect(verdict.reason).toBe(HEARTBEAT_RANGE_HINT);
        expect(verdict.reason).toContain('1 到 60');
      }
    }
  });
});

describe('设备格的会中闸（contract 7：会中换设备不做）', () => {
  it('recording 与 alert 禁用，idle 与 ended 可改', () => {
    expect(deviceChangeLocked('idle')).toBe(false);
    expect(deviceChangeLocked('recording')).toBe(true);
    expect(deviceChangeLocked('alert')).toBe(true);
    expect(deviceChangeLocked('ended')).toBe(false);
  });
});

describe('设备下拉的摆法', () => {
  it('清单没到手时什么都不摆；到手了「按默认策略挑」永远在第一个', () => {
    expect(deviceOptionsOf(null, 'mic')).toEqual([]);
    const options = deviceOptionsOf(devicesResponse(), 'mic');
    expect(options[0]).toEqual({ value: '', label: '按默认策略挑' });
    expect(options.map((item) => item.value)).toEqual([
      '',
      '假-Focusrite Analogue 1 + 2',
      '假-CABLE-Output (VB-Audio Virtual Cable)',
    ]);
  });

  it('系统默认输入带标记；render 那一路没有 host API 就不加括注', () => {
    const mic = deviceOptionsOf(devicesResponse(), 'mic');
    expect(mic[1]?.label).toContain('系统默认');
    expect(mic[1]?.label).toContain('MME');
    const loopback = deviceOptionsOf(devicesResponse(), 'loopback');
    expect(loopback[1]?.label).not.toContain('（');
  });

  it('同名端点在多个 host API 下只摆一项（配置按名字挑）', () => {
    const doubled: DevicesResponse = {
      mic: [
        ...devicesResponse().mic,
        {
          name: '假-Focusrite Analogue 1 + 2',
          host_api: 'Windows WASAPI',
          role: 'input',
          index: 18,
          channels: 2,
          is_default: false,
        },
      ],
      loopback: devicesResponse().loopback,
    };
    const options = deviceOptionsOf(doubled, 'mic');
    const focusrite = options.filter((item) => item.value === '假-Focusrite Analogue 1 + 2');
    expect(focusrite).toHaveLength(1);
  });
});

describe('缺省档的归一', () => {
  it('四档原样过；四档之外落回 15m 那一档', () => {
    for (const choice of ['5m', '15m', '30m', 'full'] as const) {
      expect(normalizeDefaultWindow(choice)).toBe(choice);
    }
    expect(normalizeDefaultWindow('7m')).toBe('15m');
    expect(normalizeDefaultWindow('')).toBe('15m');
  });
});

describe('总结模型预设那一格（会中切模型，本节点）', () => {
  it('「不选预设」恒在第一档，其后按快照里名字的次序；选项里只有名字没有端点', () => {
    const options = presetOptionsOf(
      makeSettings({ summary_preset_names: ['主力', '备用（必败）'] }),
);
    expect(options[0]).toEqual({ value: '', label: NO_PRESET_LABEL });
    expect(options.map((item) => item.value)).toEqual(['', '主力', '备用（必败）']);
    for (const option of options) {
      expect(option.label).not.toContain('http');
      expect(option.label).not.toContain('api_key');
    }
  });

  it('当前生效值在清单里就原样预选；空串与清单外的名字都按「不选预设」读', () => {
    const withPresets = makeSettings({
      summary_preset_names: ['主力', '备用（必败）'],
      summary_active_preset: '备用（必败）',
    });
    expect(normalizeActivePreset(withPresets)).toBe('备用（必败）');
    expect(normalizeActivePreset(makeSettings())).toBe('');
    // 服务侧对「指针指向的名字不在清单里」报空串；就算拿到旧形状也兜回同一档
    expect(
      normalizeActivePreset(
        makeSettings({ summary_preset_names: ['主力'], summary_active_preset: '已删掉' }),
),
).toBe('');
  });
});

