import { describe, expect, it, vi } from 'vitest';
import {
  ActionFailedError,
  ActionForbiddenError,
  ActionProtocolError,
  ContractParseError,
  REQUIRED_HEADERS,
  clampHeartbeatInterval,
  createEventsClient,
  createHeartbeatClient,
  judgeHeartbeat,
  lateHeartbeatText,
  postAction,
  recoveredHeartbeatText,
} from '../src/api';
import { readFixture } from './fixtures';
import { heartbeatSchema } from '../src/contract';

/** 造一个最小的假 Response：只要 status / ok / text() / json()。 */
function fakeResponse(status: number, body: string): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    text: async () => body,
    json: async () => JSON.parse(body) as unknown,
  } as unknown as Response;
}

function jsonResponse(status: number, payload: unknown): Response {
  return fakeResponse(status, JSON.stringify(payload));
}

/**
 * 取一份 SSE 样本的 `data` 那一格（帧里真正要被解析的载荷）。
 *
 * 帧样本的形状是 `{event, id, data}`，`parseFrame()` 的入参是 `data` 序列化之后的那一行。
 */
function sseData(name: string): unknown {
  const frame = readFixture('sse', name) as Record<string, unknown>;
  return frame['data'];
}

describe('postAction 的请求头', () => {
  it('两个必需头一起带上，body 是 {action, args}', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(200, { ok: true, revision: 2 }),
    ) as unknown as typeof fetch;

    const result = await postAction('start', { mode: 'both' }, { fetchImpl });

    expect(result).toEqual({ kind: 'ok', ok: true, revision: 2 });
    expect(fetchImpl).toHaveBeenCalledTimes(1);

    const [url, init] = (fetchImpl as unknown as { mock: { calls: [string, RequestInit][] } }).mock
      .calls[0] as [string, RequestInit];
    expect(url).toBe('/api/actions');
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual(REQUIRED_HEADERS);
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json');
    expect((init.headers as Record<string, string>)['X-Meeting-Service']).toBe('1');
    expect(JSON.parse(String(init.body))).toEqual({ action: 'start', args: { mode: 'both' } });
  });

  it('省略 args 时按空对象发出去', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(200, { ok: true, revision: 3 }),
    ) as unknown as typeof fetch;

    await postAction('stop', undefined, { fetchImpl });

    const [, init] = (fetchImpl as unknown as { mock: { calls: [string, RequestInit][] } }).mock
      .calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({ action: 'stop', args: {} });
  });
});

describe('postAction 的信封解析', () => {
  it('settings_update 成功体返两个数组', async () => {
    const payload = readFixture('actions', 'settings-update-ok') as { body: unknown };
    const fetchImpl = vi.fn(async () => jsonResponse(200, payload.body)) as unknown as typeof fetch;

    const result = await postAction('settings_update', { 'asr.timeout_seconds': 30 }, { fetchImpl });

    expect(result).toEqual({
      kind: 'settings_update',
      ok: true,
      revision: 3,
      applied: ['asr.timeout_seconds'],
      pending_restart: [],
    });
  });

  it('七个失败样本都抛 ActionFailedError，带 code / message / detail', async () => {
    const names = [
      'fail-already-recording',
      'fail-draining',
      'fail-capture-unavailable',
      'fail-not-recording',
      'fail-invalid-setting',
      'fail-invalid-setting-unknown-action',
      'fail-persist-failed',
      'fail-action-unavailable',
    ] as const;

    for (const name of names) {
      const fixture = readFixture('actions', name) as {
        action: string;
        status: number;
        body: { code: string; message: string; detail: Record<string, unknown> };
      };
      const fetchImpl = vi.fn(async () =>
        jsonResponse(fixture.status, fixture.body),
      ) as unknown as typeof fetch;

      const error = await postAction(fixture.action, {}, { fetchImpl }).then(
        () => null,
        (caught: unknown) => caught,
      );

      expect(error, name).toBeInstanceOf(ActionFailedError);
      const failed = error as ActionFailedError;
      expect(failed.code, name).toBe(fixture.body.code);
      expect(failed.status, name).toBe(fixture.status);
      expect(failed.message, name).toBe(fixture.body.message);
      expect(failed.detail, name).toEqual(fixture.body.detail);
    }
  });

  it('403 走中间件那条出口：抛 ActionForbiddenError，不当失败信封解', async () => {
    const fetchImpl = vi.fn(async () =>
      fakeResponse(403, '缺少必需请求头'),
    ) as unknown as typeof fetch;

    const error = await postAction('start', {}, { fetchImpl }).then(
      () => null,
      (caught: unknown) => caught,
    );

    expect(error).toBeInstanceOf(ActionForbiddenError);
    expect((error as ActionForbiddenError).status).toBe(403);
    expect((error as ActionForbiddenError).text).toBe('缺少必需请求头');
  });

  it('应答既不是成功体也不是失败信封时抛 ActionProtocolError', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(200, { ok: true, revision: 2, message: '多出来的字段' }),
    ) as unknown as typeof fetch;

    const error = await postAction('start', {}, { fetchImpl }).then(
      () => null,
      (caught: unknown) => caught,
    );

    expect(error).toBeInstanceOf(ActionProtocolError);
  });

  it('非 JSON 应答也抛 ActionProtocolError', async () => {
    const fetchImpl = vi.fn(async () =>
      fakeResponse(500, '<html>oops</html>'),
    ) as unknown as typeof fetch;

    await expect(postAction('start', {}, { fetchImpl })).rejects.toBeInstanceOf(
      ActionProtocolError,
    );
  });
});

/**
 * `parseFrame` 的返回值是**真的在用**（复审抓到的那一格：五个分支恒 `return null`，
 * `FramePayloads` 成了死类型）。事件流的常规消费方式是回调；`client.parse()` 是回调之外
 * 的另一条同步读法，返回值就是这一帧的载荷，类型随之收准（不再是 `| null`）。
 */
describe('SSE 帧的同步解析（parse 返回真实载荷）', () => {
  it('四个通道各自返回对应类型的载荷，且照旧推给订阅者', () => {
    const seen: string[] = [];
    const client = createEventsClient({
      onSnapshot: () => seen.push('snapshot'),
      onLevels: () => seen.push('levels'),
      onTranscript: () => seen.push('transcript'),
    });

    const snapRaw = JSON.stringify(sseData('first-snapshot'));
    const snapshot = client.parse('snapshot', snapRaw);
    // 返回值就是这一帧的载荷 —— 顶层十键齐、top_state 读得出来。
    expect(Object.keys(snapshot).sort()).toHaveLength(10);
    expect(snapshot.top_state).toBe('recording');

    const levelsRaw = JSON.stringify(sseData('levels'));
    const levels = client.parse('levels', levelsRaw);
    expect(levels.frames['mic']?.envelope).toHaveLength(15);

    const transcriptRaw = JSON.stringify(sseData('transcript'));
    const transcript = client.parse('transcript', transcriptRaw);
    expect(transcript.line.source).toBe('mic');
    expect(transcript.line.text.length).toBeGreaterThan(0);

    const stateRaw = JSON.stringify(sseData('state'));
    const state = client.parse('state', stateRaw);
    expect(state.revision).toBeGreaterThan(0);

    // 回调照旧收到 —— 返回值这条路没有把订阅者吃掉。
    expect(seen).toEqual(['snapshot', 'levels', 'transcript', 'snapshot']);
  });

  it('重同步帧：返回扩展快照，并且 resync 计数跟着走', () => {
    const client = createEventsClient();
    const raw = JSON.stringify(sseData('resync-snapshot'));
    const parsed = client.parse('snapshot', raw) as unknown as Record<string, unknown>;
    expect(parsed['resync']).toBe(true);
    expect(client.resyncCount()).toBe(1);
  });

  it('形状不对时抛带通道名的 ContractParseError（返回值不是 null 了，该抛就抛）', () => {
    const client = createEventsClient();
    expect(() => client.parse('levels', '{"revision":1}')).toThrow(ContractParseError);
    expect(() => client.parse('transcript', '不是 JSON')).toThrow(ContractParseError);
    try {
      client.parse('levels', '{"revision":1}');
    } catch (error) {
      expect((error as ContractParseError).channel).toContain('levels');
    }
  });
});

describe('心跳周期', () => {
  it('clamp 到 1–60 秒，拿不到值就用默认 10 秒', () => {
    expect(clampHeartbeatInterval(10)).toBe(10);
    expect(clampHeartbeatInterval(0.5)).toBe(1);
    expect(clampHeartbeatInterval(0)).toBe(10);
    expect(clampHeartbeatInterval(-3)).toBe(10);
    expect(clampHeartbeatInterval(600)).toBe(60);
    expect(clampHeartbeatInterval(Number.NaN)).toBe(10);
    expect(clampHeartbeatInterval('10')).toBe(10);
    expect(clampHeartbeatInterval(undefined)).toBe(10);
  });

  it('周期从快照的 settings.heartbeat_interval_seconds 跟着走', () => {
    const client = createHeartbeatClient(() => undefined, { now: () => 0 });
    client.setIntervalFromSnapshot(30);
    expect(client.intervalSeconds()).toBe(30);
    client.setIntervalFromSnapshot(3);
    expect(client.intervalSeconds()).toBe(3);
    // 快照给的 0 或负数按默认 10 秒兜底，界面不自造第二份配置源。
    client.setIntervalFromSnapshot(0);
    expect(client.intervalSeconds()).toBe(10);
  });
});

describe('心跳三态判定', () => {
  const t0 = Date.parse('2025-09-06T12:00:00+08:00');

  it('时间差 < 1 个周期 → 正常', () => {
    expect(judgeHeartbeat('2025-09-06T12:00:00+08:00', 10, t0)).toBe('正常');
    expect(judgeHeartbeat('2025-09-06T12:00:00+08:00', 10, t0 + 9_999)).toBe('正常');
  });

  it('时间差 ≥ 1 个周期且 < 2 个周期 → 迟到', () => {
    expect(judgeHeartbeat('2025-09-06T12:00:00+08:00', 10, t0 + 10_000)).toBe('迟到');
    expect(judgeHeartbeat('2025-09-06T12:00:00+08:00', 10, t0 + 19_999)).toBe('迟到');
  });

  it('时间差 ≥ 2 个周期 → 失联', () => {
    expect(judgeHeartbeat('2025-09-06T12:00:00+08:00', 10, t0 + 20_000)).toBe('失联');
    expect(judgeHeartbeat('2025-09-06T12:00:00+08:00', 10, t0 + 600_000)).toBe('失联');
  });

  it('判定用的周期是界面发心跳的周期，clamp 后照算', () => {
    // 周期 0.5 被夹到 1 秒：1 秒整就算迟到。
    expect(judgeHeartbeat('2025-09-06T12:00:00+08:00', 0.5, t0 + 1_000)).toBe('迟到');
    // 周期 600 被夹到 60 秒：50 秒仍算正常。
    expect(judgeHeartbeat('2025-09-06T12:00:00+08:00', 600, t0 + 50_000)).toBe('正常');
  });

  it('时间戳读不出来按失联处理', () => {
    expect(judgeHeartbeat('不是时间', 10, t0)).toBe('失联');
  });

  it('两行提示文案照契约的措辞', () => {
    expect(lateHeartbeatText(8)).toBe('心跳迟到 · 8 秒没收到应答');
    expect(recoveredHeartbeatText(42)).toBe('已恢复，中断 42 秒');
  });
});

describe('心跳轮询', () => {
  it('拿一份真样本喂进去：正常档、活着的判据照服务给的字段', async () => {
    const payload = readFixture('heartbeat', 'recording');
    const beat = heartbeatSchema.parse(payload);
    const now = Date.parse(beat.server_time === '<iso>' ? '' : beat.server_time);

    // 样本里 server_time 是掩码，用一个真时间戳替进本地应答来喂用例。
    const live = { ...beat, server_time: '2025-09-06T12:00:00+08:00' };
    const t0 = Date.parse('2025-09-06T12:00:00+08:00');
    expect(Number.isNaN(now)).toBe(true);

    const fetchImpl = vi.fn(async () => jsonResponse(200, live)) as unknown as typeof fetch;
    const client = createHeartbeatClient(() => undefined, { fetchImpl, now: () => t0 });

    const tick = await client.tick();
    expect(tick.kind).toBe('reply');
    if (tick.kind !== 'reply') throw new Error('该拿到应答');
    expect(tick.status).toBe('正常');
    expect(tick.heartbeat.recording_alive).toBe(true);
    expect(tick.heartbeat.seconds_since_last_tick).toBe(0);
  });

  it('idle 样本的两处 null 原样穿过：界面不替服务下结论', async () => {
    const payload = readFixture('heartbeat', 'idle') as Record<string, unknown>;
    const live = { ...payload, server_time: '2025-09-06T12:00:00+08:00' };
    const t0 = Date.parse('2025-09-06T12:00:00+08:00');

    const fetchImpl = vi.fn(async () => jsonResponse(200, live)) as unknown as typeof fetch;
    const client = createHeartbeatClient(() => undefined, { fetchImpl, now: () => t0 });

    const tick = await client.tick();
    if (tick.kind !== 'reply') throw new Error('该拿到应答');
    expect(tick.heartbeat.recording_alive).toBeNull();
    expect(tick.heartbeat.seconds_since_last_tick).toBeNull();
  });

  it('服务报 recording_alive: false 而 HTTP 200：应答照收，失联与否只看时间差', async () => {
    const live = {
      service: 'meeting',
      server_time: '2025-09-06T12:00:00+08:00',
      revision: 2,
      top_state: 'recording',
      recording_alive: false,
      seconds_since_last_tick: 4.5,
    };
    const fetchImpl = vi.fn(async () => jsonResponse(200, live)) as unknown as typeof fetch;
    const t0 = Date.parse('2025-09-06T12:00:00+08:00');
    const client = createHeartbeatClient(() => undefined, { fetchImpl, now: () => t0 });

    const tick = await client.tick();
    if (tick.kind !== 'reply') throw new Error('该拿到应答');
    // 采集停了不会让心跳失败 —— 它只是一份数据。
    expect(tick.status).toBe('正常');
    expect(tick.heartbeat.recording_alive).toBe(false);
    expect(tick.heartbeat.seconds_since_last_tick).toBe(4.5);
  });

  it('应答迟到 20 秒以上按失联', async () => {
    const live = {
      service: 'meeting',
      server_time: '2025-09-06T12:00:00+08:00',
      revision: 2,
      top_state: 'recording',
      recording_alive: true,
      seconds_since_last_tick: 0,
    };
    const t0 = Date.parse('2025-09-06T12:00:00+08:00');
    const fetchImpl = vi.fn(async () => jsonResponse(200, live)) as unknown as typeof fetch;
    const client = createHeartbeatClient(() => undefined, { fetchImpl, now: () => t0 + 25_000 });

    const tick = await client.tick();
    expect(tick.status).toBe('失联');
  });

  it('HTTP 不通按失联，形状不对也按失联并带原因', async () => {
    const t0 = Date.parse('2025-09-06T12:00:00+08:00');

    const downImpl = vi.fn(async () => fakeResponse(500, 'boom')) as unknown as typeof fetch;
    const down = createHeartbeatClient(() => undefined, { fetchImpl: downImpl, now: () => t0 });
    const downTick = await down.tick();
    expect(downTick.kind).toBe('error');
    expect(downTick.status).toBe('失联');

    const badImpl = vi.fn(async () =>
      jsonResponse(200, { service: 'meeting' }),
    ) as unknown as typeof fetch;
    const bad = createHeartbeatClient(() => undefined, { fetchImpl: badImpl, now: () => t0 });
    const badTick = await bad.tick();
    expect(badTick.kind).toBe('error');
    expect(badTick.status).toBe('失联');
    if (badTick.kind === 'error') {
      expect(badTick.error.message).toContain('心跳应答形状不符合契约');
    }
  });

  it('start 之后**一跳接一跳**地轮询（每跳排下一次；这一格丢过：整套轮询只发一次请求）', async () => {
    vi.useFakeTimers();
    try {
      const ticks: unknown[] = [];
      const fetchImpl = vi.fn(async () =>
        jsonResponse(200, {
          service: 'meeting',
          server_time: new Date().toISOString(),
          revision: 1,
          top_state: 'idle',
          recording_alive: null,
          seconds_since_last_tick: null,
        }),
      ) as unknown as typeof fetch;

      const client = createHeartbeatClient((tick) => ticks.push(tick), { fetchImpl });
      client.setIntervalFromSnapshot(1); // 周期 1 秒，省得推 60 秒的假时钟
      client.start();

      // 连着走过三个周期：每一个周期都该发一次请求、给一次回调。
      for (let i = 1; i <= 3; i += 1) {
        await vi.advanceTimersByTimeAsync(1000);
        expect(fetchImpl).toHaveBeenCalledTimes(i);
        expect(ticks).toHaveLength(i);
      }

      client.stop();
      await vi.advanceTimersByTimeAsync(5000);
      expect(fetchImpl).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it('start / stop 管住轮询（用假定时器，不起真定时器）', () => {
    vi.useFakeTimers();
    try {
      const ticks: unknown[] = [];
      const fetchImpl = vi.fn(async () =>
        jsonResponse(200, {
          service: 'meeting',
          server_time: new Date().toISOString(),
          revision: 1,
          top_state: 'idle',
          recording_alive: null,
          seconds_since_last_tick: null,
        }),
      ) as unknown as typeof fetch;

      const client = createHeartbeatClient((tick) => ticks.push(tick), { fetchImpl });
      expect(client.running()).toBe(false);
      client.start();
      expect(client.running()).toBe(true);
      client.stop();
      expect(client.running()).toBe(false);

      // 停了之后走完假时钟也不该再发请求。
      vi.advanceTimersByTime(60_000);
      expect(fetchImpl).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});
