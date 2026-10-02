import { describe, expect, it } from 'vitest';
import type { DevicesResponse } from '../src/contract';
import {
  DEVICE_LOCKED_HINT,
  DRAWER_ROW_IDS,
  HEARTBEAT_RANGE_HINT,
  SUMMARY_DEFAULT_WINDOW_OPTIONS,
  deviceChangeLocked,
  deviceOptionsOf,
  normalizeDefaultWindow,
  parseHeartbeatInput,
} from '../src/live/settings';

/**
 * 设置抽屉那份纯逻辑的用例（`311f84f5`）。
 *
 * 三条边界各有一条判据钉在这里：ASR 五参数一个都不进抽屉（`DRAWER_ROW_IDS` 的穷举）、
 * 心跳 1–60 越界当场拒（不发请求）、会中两路设备格禁用（`docs/contract.md` 第 7 章）。
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

describe('抽屉的行清单（ASR 五参数一个都不进这扇窗）', () => {
  it('全部行恰好五条：设备两行 + 告警提示音 + 心跳频率 + 快速总结缺省档', () => {
    expect([...DRAWER_ROW_IDS]).toEqual([
      'device-mic',
      'device-loopback',
      'alarm-sound',
      'heartbeat-interval',
      'summary-default-window',
    ]);
  });

  it('行清单与缺省档四档里都找不出那五个 ASR 键或它们的人话（设计意图第 14 条）', () => {
    const asrKeys = [
      'asr.max_segment_seconds',
      'asr.timeout_seconds',
      'asr.max_new_tokens',
      'asr.repeat_max_unit',
      'asr.repeat_min_coverage',
    ];
    const everyWord = [
      ...DRAWER_ROW_IDS,
      ...SUMMARY_DEFAULT_WINDOW_OPTIONS.map((item) => `${item.value}${item.label}`),
    ].join('\n');
    for (const key of asrKeys) {
      expect(everyWord, key).not.toContain(key);
      expect(everyWord, key).not.toContain(key.split('.')[1]);
    }
    expect(everyWord).not.toContain('ASR');
    expect(everyWord).not.toContain('段长');
    expect(everyWord).not.toContain('覆盖率');
  });
});

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

describe('设备格的会中闸（contract 第 7 章：会中换设备不做）', () => {
  it('recording 与 alert 禁用，idle 与 ended 可改', () => {
    expect(deviceChangeLocked('idle')).toBe(false);
    expect(deviceChangeLocked('recording')).toBe(true);
    expect(deviceChangeLocked('alert')).toBe(true);
    expect(deviceChangeLocked('ended')).toBe(false);
  });

  it('禁用旁边那句把「开场前定下来」说清', () => {
    expect(DEVICE_LOCKED_HINT).toContain('会中不换设备');
    expect(DEVICE_LOCKED_HINT).toContain('开场前定下来');
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

  it('四档的人话标签与会中总结区同源（5 分钟 / 15 分钟 / 30 分钟 / 全场）', () => {
    expect(SUMMARY_DEFAULT_WINDOW_OPTIONS.map((item) => item.label)).toEqual([
      '5 分钟',
      '15 分钟',
      '30 分钟',
      '全场',
    ]);
  });
});
