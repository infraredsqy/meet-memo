import { describe, expect, it, vi } from 'vitest';
import {
  ActionFailedError,
  ActionForbiddenError,
  ActionProtocolError,
  ContractParseError,
  DevicesProtocolError,
  DevicesUnavailableError,
  REQUIRED_HEADERS,
  SessionStatusHttpError,
  DirsHttpError,
  DirCreateHttpError,
  browseDirs,
  createDir,
  SessionsProtocolError,
  SessionsUnavailableError,
  SummariesProtocolError,
  SummariesUnavailableError,
  clampHeartbeatInterval,
  createEventsClient,
  createHeartbeatClient,
  createSessionStatusPoller,
  fetchDevices,
  fetchSessionStatus,
  fetchSessions,
  fetchSummaries,
  judgeHeartbeat,
  lateHeartbeatText,
  postAction,
} from '../src/api';
import { recoveredNoteText } from '../src/live/heartbeat';
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

  it('summarize 成功体返 summary 那一格（八格逐键都在）', async () => {
    const payload = readFixture('actions', 'summarize-ok') as { body: unknown };
    const fetchImpl = vi.fn(async () => jsonResponse(200, payload.body)) as unknown as typeof fetch;

    const result = await postAction('summarize', { window: '15m' }, { fetchImpl });

    expect(result.kind).toBe('summarize');
    if (result.kind !== 'summarize') throw new Error('该是 summarize 那一档');
    expect(result.revision).toBe(2);
    expect(Object.keys(result.summary).sort()).toEqual([
      'at',
      'elapsed_seconds',
      'lines',
      'lost',
      'what',
      'where',
      'window',
      'window_label',
    ]);
    expect(result.summary.window_label).toBe('15 分钟');
    expect(result.summary.lines).toBe(12);

    const [, init] = (fetchImpl as unknown as { mock: { calls: [string, RequestInit][] } }).mock
      .calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      action: 'summarize',
      args: { window: '15m' },
    });
  });

  it('十个失败样本都抛 ActionFailedError，带 code / message / detail', async () => {
    const names = [
      'fail-already-recording',
      'fail-draining',
      'fail-capture-unavailable',
      'fail-not-recording',
      'fail-invalid-setting',
      'fail-invalid-setting-unknown-action',
      'fail-persist-failed',
      'fail-action-unavailable',
      'fail-transcript-unavailable',
      'fail-summary-failed-missing',
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
 * `GET /api/summaries`（契约 4.7）：本场总结记录。
 *
 * 它有两条与动作端点不一样的地方，各有一条用例钉住：**不带那两个必需头**（动作那三个
 * 防线是给改状态的请求设的），以及**非 2xx 如实报**（契约说它任何状态下都 200，
 * 因此非 2xx 是协议外的一档 —— 不许把取不到画成「这一场还没总结过」）。
 */
describe('fetchSummaries 的读法', () => {
  it('取记录那一条：GET、不带必需头、应答体逐键交回来', async () => {
    const payload = readFixture('summaries', 'after-one');
    const fetchImpl = vi.fn(async () => jsonResponse(200, payload)) as unknown as typeof fetch;

    const listing = await fetchSummaries({ fetchImpl });

    expect(listing.session_id).toBe('<session-id>');
    expect(listing.entries).toHaveLength(1);
    expect(listing.entries[0]?.window_label).toBe('15 分钟');
    expect(Object.hasOwn(listing.entries[0] ?? {}, 'lines')).toBe(false);

    const [url, init] = (fetchImpl as unknown as { mock: { calls: [string, RequestInit][] } }).mock
      .calls[0] as [string, RequestInit];
    expect(url).toBe('/api/summaries');
    expect(init.method).toBe('GET');
    // 动作端点那两个必需头不在这条路上
    expect(init.headers).toBeUndefined();
  });

  it('空记录那一档：session_id 为 null、entries 空数组也照收', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(200, { session_id: null, entries: [] }),
    ) as unknown as typeof fetch;

    const listing = await fetchSummaries({ fetchImpl });
    expect(listing.session_id).toBeNull();
    expect(listing.entries).toEqual([]);
  });

  it('形状不对抛 SummariesProtocolError（顶层多一个键就红）', async () => {
    const payload = readFixture('summaries', 'after-one') as Record<string, unknown>;
    const fetchImpl = vi.fn(async () =>
      jsonResponse(200, { ...payload, extra: 1 }),
    ) as unknown as typeof fetch;

    await expect(fetchSummaries({ fetchImpl })).rejects.toBeInstanceOf(SummariesProtocolError);
  });

  it('非 2xx 也抛 SummariesProtocolError：取不到不冒充空记录', async () => {
    const fetchImpl = vi.fn(async () =>
      fakeResponse(500, 'Not Found'),
    ) as unknown as typeof fetch;

    const error = await fetchSummaries({ fetchImpl }).then(
      () => null,
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(SummariesProtocolError);
    expect((error as SummariesProtocolError).status).toBe(500);
  });

  it('连接层失败抛 SummariesUnavailableError', async () => {
    const fetchImpl = vi.fn(async () => {
      throw new Error('连接断了');
    }) as unknown as typeof fetch;

    await expect(fetchSummaries({ fetchImpl })).rejects.toBeInstanceOf(
      SummariesUnavailableError,
    );
  });
});

describe('fetchSessionStatus 的读法（会后进度界面节点 52b24392）', () => {
  it('取单场那一条：dir URL 编码后进路径、GET、应答体逐键交回来', async () => {
    const payload = readFixture('sessions', 'status-after-run');
    const fetchImpl = vi.fn(async () => jsonResponse(200, payload)) as unknown as typeof fetch;

    const parsed = await fetchSessionStatus('C:\\会议\\跟老王聊周会', { fetchImpl });

    expect(parsed.offline.state).toBe('done');
    expect(parsed.offline.sources).toHaveLength(2);
    const [url, init] = (fetchImpl as unknown as { mock: { calls: [string, RequestInit][] } }).mock
      .calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/sessions/${encodeURIComponent('C:\\会议\\跟老王聊周会')}`);
    expect(init.method).toBe('GET');
    expect(init.headers).toBeUndefined();
  });

  it('形状不对抛 SessionsProtocolError（offline 那格多一个键就红）', async () => {
    const payload = readFixture('sessions', 'status-after-run') as Record<string, unknown>;
    const offline = structuredClone(payload['offline'] as Record<string, unknown>);
    offline['extra'] = 1;
    const fetchImpl = vi.fn(async () =>
      jsonResponse(200, { ...payload, offline }),
    ) as unknown as typeof fetch;

    const error = await fetchSessionStatus('C:\\x', { fetchImpl }).then(
      () => null,
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(SessionsProtocolError);
    expect((error as SessionsProtocolError).message).toContain('单场状态');
  });

  it('404 失败信封里的 code 原样转手：SessionStatusHttpError 带 status 与 code', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(404, {
        ok: false,
        code: 'session_dir_missing',
        message: '这一场的目录已不在原地',
        detail: { dir: '<path>', kind: 'missing' },
      }),
    ) as unknown as typeof fetch;

    const error = await fetchSessionStatus('C:\\x', { fetchImpl }).then(
      () => null,
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(SessionStatusHttpError);
    expect((error as SessionStatusHttpError).status).toBe(404);
    expect((error as SessionStatusHttpError).code).toBe('session_dir_missing');
  });

  it('连接层失败抛 SessionsUnavailableError', async () => {
    const fetchImpl = vi.fn(async () => {
      throw new Error('连接断了');
    }) as unknown as typeof fetch;

    await expect(fetchSessionStatus('C:\\x', { fetchImpl })).rejects.toBeInstanceOf(
      SessionsUnavailableError,
    );
  });
});

describe('browseDirs 的读法（目录选择器 57ab5255）', () => {
  it('dir 进查询串、GET、应答体逐键交回来', async () => {
    const payload = {
      home: 'C:\\Users\\<user>',
      cwd: 'C:\\会议',
      up: 'C:\\Users\\<user>',
      here: { name: '会议', dir: 'C:\\会议', kind: 'unknown', suspected: false },
      dirs: [{ name: '新场', dir: 'C:\\会议\\新场', kind: 'empty', suspected: false }],
    };
    const fetchImpl = vi.fn(async () => jsonResponse(200, payload)) as unknown as typeof fetch;

    const parsed = await browseDirs('C:\\会议', { fetchImpl });

    expect(parsed.cwd).toBe('C:\\会议');
    expect(parsed.dirs).toHaveLength(1);
    const [url] = (fetchImpl as unknown as { mock: { calls: [string][] } }).mock.calls[0] as [string];
    expect(url).toBe(`/api/dirs?dir=${encodeURIComponent('C:\\会议')}`);
  });

  it('400 失败信封：DirsHttpError 的 message 就是信封原话，协议话术不顶上来', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(400, {
        ok: false,
        code: 'invalid_setting',
        message: '要浏览的目录不存在：C:\\不存在',
        detail: { key: 'dir' },
      }),
    ) as unknown as typeof fetch;

    const error = await browseDirs('C:\\不存在', { fetchImpl }).then(
      () => null,
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(DirsHttpError);
    expect((error as DirsHttpError).status).toBe(400);
    expect((error as DirsHttpError).code).toBe('invalid_setting');
    expect((error as DirsHttpError).message).toBe('要浏览的目录不存在：C:\\不存在');
  });

  it('400 且读不出信封：DirsHttpError 落固定话术，不冒充原话', async () => {
    const fetchImpl = vi.fn(async () => fakeResponse(500, '网关炸了')) as unknown as typeof fetch;

    const error = await browseDirs('C:\\x', { fetchImpl }).then(
      () => null,
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(DirsHttpError);
    expect((error as DirsHttpError).note).toBeNull();
    expect((error as DirsHttpError).message).toContain('HTTP 500');
  });
});

describe('createDir 的读法（新建目录，目录选择器节点）', () => {
  it('POST + dir/name 进查询串 + 写端点头哨；成功体 directory 逐键交回来', async () => {
    const payload = {
      ok: true,
      revision: 1,
      directory: { name: '新会', dir: 'C:\\会议\\新会', kind: 'empty', suspected: false },
    };
    const fetchImpl = vi.fn(async () => jsonResponse(200, payload)) as unknown as typeof fetch;

    const parsed = await createDir('C:\\会议', '新会', { fetchImpl });

    expect(parsed.directory.name).toBe('新会');
    expect(parsed.directory.kind).toBe('empty');
    const [url, init] = (fetchImpl as unknown as { mock: { calls: [string, RequestInit][] } })
      .mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/dirs?dir=${encodeURIComponent('C:\\会议')}&name=${encodeURIComponent('新会')}`);
    expect(init.method).toBe('POST');
    expect((init.headers as Record<string, string>)['X-Meeting-Service']).toBe('1');
  });

  it('409 同名已存在：DirCreateHttpError 的 message 就是信封原话，code 带着走', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(409, {
        ok: false,
        code: 'dir_exists',
        message: '这一层里已经有叫「新会」的目录或文件：C:\\会议\\新会；换个名字，或者双击进去直接用它',
        detail: { dir: 'C:\\会议', name: '新会' },
      }),
    ) as unknown as typeof fetch;

    const error = await createDir('C:\\会议', '新会', { fetchImpl }).then(
      () => null,
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(DirCreateHttpError);
    expect((error as DirCreateHttpError).status).toBe(409);
    expect((error as DirCreateHttpError).code).toBe('dir_exists');
    expect((error as DirCreateHttpError).message).toContain('已经有叫「新会」');
  });

  it('400 且读不出信封：DirCreateHttpError 落固定话术，不冒充原话', async () => {
    const fetchImpl = vi.fn(async () => fakeResponse(500, '网关炸了')) as unknown as typeof fetch;

    const error = await createDir('C:\\x', 'y', { fetchImpl }).then(
      () => null,
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(DirCreateHttpError);
    expect((error as DirCreateHttpError).note).toBeNull();
    expect((error as DirCreateHttpError).message).toContain('HTTP 500');
  });
});

describe('单场轮询器（心跳的样板：setTimeout 自排、周期可换）', () => {
  interface FakeDeadline {
    fn: () => void;
  }

  /** 一只手动走的 setTimeout 替身：不计时，用例自己喊「到点了」。 */
  function manualTimers(): {
    setTimeout: typeof setTimeout;
    clearTimeout: typeof clearTimeout;
    fire: () => void;
    pending: () => FakeDeadline | null;
  } {
    let current: FakeDeadline | null = null;
    const setTimeoutImpl = ((fn: () => void, _ms?: number) => {
      current = { fn };
      return 0 as unknown as ReturnType<typeof setTimeout>;
    }) as typeof setTimeout;
    const clearTimeoutImpl = (() => {
      current = null;
    }) as typeof clearTimeout;
    return {
      setTimeout: setTimeoutImpl,
      clearTimeout: clearTimeoutImpl,
      pending: () => current,
      fire: () => {
        const deadline = current;
        current = null;
        deadline?.fn();
      },
    };
  }

  it('start 之后自排一跳；tick 把读数交给回调并排下一跳；stop 收干净', async () => {
    const payload = readFixture('sessions', 'status-after-run');
    const fetchImpl = vi.fn(async () => jsonResponse(200, payload)) as unknown as typeof fetch;
    const timers = manualTimers();
    const seen: unknown[] = [];
    const originalSetTimeout = globalThis.setTimeout;
    const originalClearTimeout = globalThis.clearTimeout;
    globalThis.setTimeout = timers.setTimeout;
    globalThis.clearTimeout = timers.clearTimeout;
    try {
      const poller = createSessionStatusPoller((tick) => seen.push(tick), { fetchImpl });
      poller.start('C:\\x');
      expect(timers.pending()).not.toBeNull();

      timers.fire();
      await vi.waitFor(() => expect(seen).toHaveLength(1));
      // 第一跳之后排了下一跳（自排的另一半）
      expect(timers.pending()).not.toBeNull();

      await poller.tick('C:\\x');
      expect(seen).toHaveLength(2);
      expect((seen[0] as { kind: string }).kind).toBe('status');

      poller.stop();
      expect(timers.pending()).toBeNull();
      expect(poller.running()).toBe(false);
    } finally {
      globalThis.setTimeout = originalSetTimeout;
      globalThis.clearTimeout = originalClearTimeout;
    }
  });

  it('周期随快照换（clamp 1–60）；失败那一跳也交回调、不停表', async () => {
    const fetchImpl = vi.fn(async () => {
      throw new Error('连接断了');
    }) as unknown as typeof fetch;
    const seen: unknown[] = [];
    const poller = createSessionStatusPoller((tick) => seen.push(tick), { fetchImpl });

    expect(poller.intervalSeconds()).toBe(10);
    poller.setIntervalFromSnapshot(2);
    expect(poller.intervalSeconds()).toBe(2);
    // 非正数按心跳同款落回默认 10；越界夹回 1–60
    poller.setIntervalFromSnapshot(0);
    expect(poller.intervalSeconds()).toBe(10);
    poller.setIntervalFromSnapshot(0.5);
    expect(poller.intervalSeconds()).toBe(1);
    poller.setIntervalFromSnapshot(600);
    expect(poller.intervalSeconds()).toBe(60);

    const tick = await poller.tick('C:\\x');
    expect(tick.kind).toBe('error');
    expect(seen).toHaveLength(1);
    expect((seen[0] as { kind: string }).kind).toBe('error');
  });
});

describe('fetchDevices 的读法（设置抽屉节点 311f84f5）', () => {
  it('取清单那一条：GET、不带必需头、两路各一份原文', async () => {
    const payload = readFixture('devices', 'all');
    const fetchImpl = vi.fn(async () => jsonResponse(200, payload)) as unknown as typeof fetch;

    const listing = await fetchDevices({ fetchImpl });

    expect(Object.keys(listing).sort()).toEqual(['loopback', 'mic']);
    expect(listing.mic[0]?.name).toBe('假-Focusrite Analogue 1 + 2');
    expect(listing.mic[0]?.is_default).toBe(true);

    const [url, init] = (fetchImpl as unknown as { mock: { calls: [string, RequestInit][] } }).mock
      .calls[0] as [string, RequestInit];
    expect(url).toBe('/api/devices');
    expect(init.method).toBe('GET');
    expect(init.headers).toBeUndefined();
  });

  it('形状不对抛 DevicesProtocolError（顶层多一个键就红）', async () => {
    const payload = readFixture('devices', 'all') as Record<string, unknown>;
    const fetchImpl = vi.fn(async () =>
      jsonResponse(200, { ...payload, extra: 1 }),
    ) as unknown as typeof fetch;

    await expect(fetchDevices({ fetchImpl })).rejects.toBeInstanceOf(DevicesProtocolError);
  });

  it('非 2xx 也抛 DevicesProtocolError：取不到不冒充空清单', async () => {
    const fetchImpl = vi.fn(async () =>
      fakeResponse(503, '{"ok": false}'),
    ) as unknown as typeof fetch;

    const error = await fetchDevices({ fetchImpl }).then(
      () => null,
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(DevicesProtocolError);
    expect((error as DevicesProtocolError).status).toBe(503);
  });

  it('连接层失败抛 DevicesUnavailableError', async () => {
    const fetchImpl = vi.fn(async () => {
      throw new Error('连接断了');
    }) as unknown as typeof fetch;

    await expect(fetchDevices({ fetchImpl })).rejects.toBeInstanceOf(DevicesUnavailableError);
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
    // 返回值就是这一帧的载荷 —— 顶层九键齐、top_state 读得出来。
    expect(Object.keys(snapshot).sort()).toHaveLength(9);
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
    expect(recoveredNoteText(42)).toBe('已恢复，中断 42 秒');
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
      capture_gaps: { total_count: 0, total_seconds: 0, recent: [] },
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
      capture_gaps: { total_count: 0, total_seconds: 0, recent: [] },
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
          capture_gaps: { total_count: 0, total_seconds: 0, recent: [] },
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
          capture_gaps: { total_count: 0, total_seconds: 0, recent: [] },
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
