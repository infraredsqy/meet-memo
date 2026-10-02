import { describe, expect, it } from 'vitest';
import {
  ACTION_FIXTURES,
  DEVICES_FIXTURES,
  HEARTBEAT_FIXTURES,
  SESSIONS_FIXTURES,
  SNAPSHOT_FIXTURES,
  SSE_FIXTURES,
  SUMMARIES_FIXTURES,
  listFixtures,
  readFixture,
} from './fixtures';
import {
  ACTION_NAMES,
  ENVELOPE_BAR_COUNT,
  ERROR_CODES,
  ERROR_CODE_STATUS,
  actionFixtureSchema,
  devicesResponseSchema,
  heartbeatSchema,
  resyncSnapshotSchema,
  sessionStatusSchema,
  sessionsResponseSchema,
  snapshotSchema,
  sseFrameSchema,
  summariesResponseSchema,
  type Snapshot,
} from '../src/contract';

/**
 * 契约层与 golden 样本的对齐测试。
 *
 * 每个 `contract/fixtures/*.json` 都拿对应的 schema 过一遍 —— schema 漏一个键、
 * 多一个键、把某一格收得太紧，这里就红。
 *
 * 样本里的掩码占位值（`<iso>` / `<path>` / `<port>` / `<url>` / `<dbfs>`）**就是真实值**，
 * schema 照实放行；这几条用例同时也是「放行没放过头」的看守。
 */

describe('快照样本', () => {
  it.each(SNAPSHOT_FIXTURES)('snapshot/%s.json 过 snapshotSchema', (name) => {
    const fixture = readFixture('snapshot', name);
    const parsed = snapshotSchema.parse(fixture);
    expect(parsed.sources).toHaveLength(2);
    expect(parsed.sources[0]?.id).toBe('mic');
    expect(parsed.sources[1]?.id).toBe('loopback');
  });

  it('六个样本一个不少：目录里没有多余的快照文件', () => {
    expect(listFixtures('snapshot')).toEqual([...SNAPSHOT_FIXTURES].sort());
  });

  it('idle 样本的三处 null：session / ended 各看一次（`notice` 槽位已退场）', () => {
    const idle = snapshotSchema.parse(readFixture('snapshot', 'idle'));
    expect(idle.session).toBeNull();
    expect(idle.ended).toBeNull();
    expect(idle.top_state).toBe('idle');
  });

  it('四态样本各落在该有的那一态', () => {
    const expected: Record<(typeof SNAPSHOT_FIXTURES)[number], Snapshot['top_state']> = {
      idle: 'idle',
      recording: 'recording',
      alert: 'alert',
      'ended-done': 'ended',
      'ended-draining': 'ended',
    };
    for (const name of SNAPSHOT_FIXTURES) {
      const snap = snapshotSchema.parse(readFixture('snapshot', name));
      expect(snap.top_state, name).toBe(expected[name]);
    }
  });

  it('录音态与 ended 两档的包络长度恰 15；空闲态是空数组', () => {
    for (const name of ['recording', 'alert', 'ended-done', 'ended-draining'] as const) {
      const snap = snapshotSchema.parse(readFixture('snapshot', name));
      for (const source of snap.sources) {
        expect(source.envelope, `${name}·${source.id}`).toHaveLength(ENVELOPE_BAR_COUNT);
      }
    }
    for (const name of ['idle'] as const) {
      const snap = snapshotSchema.parse(readFixture('snapshot', name));
      for (const source of snap.sources) {
        expect(source.envelope, `${name}·${source.id}`).toEqual([]);
      }
    }
  });

  it('alert 样本：致命级一条、hints 空、告警路是 lost', () => {
    const snap = snapshotSchema.parse(readFixture('snapshot', 'alert'));
    expect(snap.alerts.critical?.level).toBe('critical');
    expect(snap.alerts.critical?.source).toBe('loopback');
    // muted 由服务判定，界面只读布尔值。
    expect(typeof snap.alerts.critical?.muted).toBe('boolean');
    expect(snap.alerts.hints).toEqual([]);
    expect(snap.sources.find((s) => s.id === 'loopback')?.health).toBe('lost');
    expect(snap.sources.find((s) => s.id === 'mic')?.health).toBe('capturing');
  });

  it('ended-done：四步次序 flush/transcode/log/post，status 是 skipped/skipped/done/waiting', () => {
    const snap = snapshotSchema.parse(readFixture('snapshot', 'ended-done'));
    expect(snap.ended?.phase).toBe('done');
    expect(snap.ended?.error).toBeNull();
    expect(snap.ended?.steps.map((step) => step.id)).toEqual([
      'flush',
      'transcode',
      'log',
      'post',
    ]);
    // 第 4 步 `post` 停在 `waiting`（＝等你动手）：按「结束录音」之后会后一个动作都不自动跑（R26）
    expect(snap.ended?.steps.map((step) => step.status)).toEqual([
      'skipped',
      'skipped',
      'done',
      'waiting',
    ]);
    // error 键恒在：没有错误时它填 null，键本身照旧留着。
    for (const step of snap.ended?.steps ?? []) {
      expect(Object.hasOwn(step, 'error'), step.id).toBe(true);
    }
    expect(snap.session?.frozen).toBe(true);
    expect(snap.sources.map((s) => s.health)).toEqual(['unprobed', 'unprobed']);
  });

  it('ended-draining：log 步 failed、ended.error 有值、post 停在 waiting、两路停 stopped', () => {
    const snap = snapshotSchema.parse(readFixture('snapshot', 'ended-draining'));
    expect(snap.ended?.phase).toBe('draining');
    expect(snap.ended?.error).toContain('场次日志');
    const failed = snap.ended?.steps.find((step) => step.status === 'failed');
    expect(failed?.id).toBe('log');
    expect(failed?.error).toBe(snap.ended?.error);
    // 第 4 步那一格与成败无关：它照旧停在「等你动手」
    const post = snap.ended?.steps.find((step) => step.id === 'post');
    expect(post?.status).toBe('waiting');
    expect(post?.error).toBeNull();
    expect(snap.sources.map((s) => s.health)).toEqual(['stopped', 'stopped']);
  });

  it('settings 与 session 的掩码格逐键对上', () => {
    const snap = snapshotSchema.parse(readFixture('snapshot', 'recording'));
    expect(snap.service.started_at).toBe('<iso>');
    expect(snap.service.port).toBe('<port>');
    expect(snap.service.url).toBe('<url>');
    expect(snap.service.log_path).toBe('<path>');
    expect(snap.session?.id).toBe('<session-id>');
    expect(snap.session?.dir).toBe('<path>');
    expect(snap.session?.started_at).toBe('<iso>');
    expect(snap.session?.elapsed_seconds).toBe(0);
    expect(snap.sources[0]?.level_dbfs).toBe('<dbfs>');
    expect(snap.settings.heartbeat_interval_seconds).toBe(10);
    expect(snap.settings.summary_default_window).toBe('15m');
    expect(snap.settings.devices).toEqual({ mic: null, loopback: null });
    // 心跳周期的唯一来源就是这一个键；缺省档的来源也是快照这一格（311f84f5）。
    expect(Object.keys(snap.settings)).toEqual([
      'alarm_sound',
      'heartbeat_interval_seconds',
      'summary_default_window',
      'asr',
      'devices',
    ]);
  });

  it('顶层恰好九键，多一个键就要红（第 7 章那些键位、连同退场的 notice 都不该冒出来）', () => {
    const snap = readFixture('snapshot', 'idle') as Record<string, unknown>;
    expect(Object.keys(snap).sort()).toEqual(
      [
        'revision',
        'service',
        'top_state',
        'session',
        'sources',
        'alerts',
        'transcript',
        'ended',
        'settings',
      ].sort(),
    );
    expect(snap).not.toHaveProperty('summary');
    expect(snap).not.toHaveProperty('notice');
    const extra = { ...snap, summary: null, notice: null };
    expect(snapshotSchema.safeParse(extra).success).toBe(false);
  });

  it('revision 保留真实值（掩码不盖它）', () => {
    expect(snapshotSchema.parse(readFixture('snapshot', 'idle')).revision).toBe(1);
    expect(snapshotSchema.parse(readFixture('snapshot', 'recording')).revision).toBe(2);
    expect(snapshotSchema.parse(readFixture('snapshot', 'ended-done')).revision).toBe(11);
  });
});

describe('SSE 帧样本', () => {
  it.each(SSE_FIXTURES)('sse/%s.json 过 sseFrameSchema', (name) => {
    const frame = sseFrameSchema.parse(readFixture('sse', name));
    expect(frame.event).toBeTruthy();
  });

  it('五个样本一个不少', () => {
    expect(listFixtures('sse')).toEqual([...SSE_FIXTURES].sort());
  });

  it('首帧是 snapshot、带 id、data 就是九键快照、id == data.revision', () => {
    const frame = sseFrameSchema.parse(readFixture('sse', 'first-snapshot'));
    expect(frame.event).toBe('snapshot');
    expect(frame.id).toBe('2');
    const snap = snapshotSchema.parse(frame.data);
    expect(snap.revision).toBe(2);
    expect(frame.id).toBe(String(snap.revision));
  });

  it('state 帧是全量快照、id 与 data.revision 逐值相等', () => {
    const frame = sseFrameSchema.parse(readFixture('sse', 'state'));
    expect(frame.event).toBe('state');
    expect(frame.id).toBe('3');
    expect(snapshotSchema.parse(frame.data).revision).toBe(3);
    expect(frame.id).toBe(String((frame.data as Snapshot).revision));
  });

  it('levels 帧不带 id：两路五键、ts 恒 null、包络 15、bar == envelope[-1]', () => {
    const frame = sseFrameSchema.parse(readFixture('sse', 'levels'));
    expect(frame.event).toBe('levels');
    expect(frame.id).toBeNull();
    if (frame.event !== 'levels') throw new Error('样本事件名不对');
    expect(Object.keys(frame.data.frames).sort()).toEqual(['loopback', 'mic']);
    for (const id of ['mic', 'loopback'] as const) {
      const road = frame.data.frames[id];
      expect(road.ts, id).toBeNull();
      expect(road.envelope, id).toHaveLength(ENVELOPE_BAR_COUNT);
      expect(road.bar, id).toBe(road.envelope[road.envelope.length - 1]);
      expect(Object.keys(road).sort(), id).toEqual(['bar', 'dbfs', 'envelope', 'peak', 'ts']);
    }
  });

  it('levels 的 ts 一旦出值就不符合契约', () => {
    const frame = readFixture('sse', 'levels') as { data: { frames: { mic: { ts: unknown } } } };
    frame.data.frames.mic.ts = 1.5;
    expect(sseFrameSchema.safeParse(frame).success).toBe(false);
  });

  it('transcript 帧不带 id：line 四键、ts 是后端给的秒数', () => {
    const frame = sseFrameSchema.parse(readFixture('sse', 'transcript'));
    expect(frame.event).toBe('transcript');
    expect(frame.id).toBeNull();
    if (frame.event !== 'transcript') throw new Error('样本事件名不对');
    expect(Object.keys(frame.data.line).sort()).toEqual(['kind', 'source', 'text', 'ts']);
    expect(frame.data.line.ts).toBe(1.5);
    expect(frame.data.line.kind).toBe('line');
  });

  it('resync 帧走带扩展键的快照形状：必须过 resyncSnapshotSchema', () => {
    const frame = sseFrameSchema.parse(readFixture('sse', 'resync-snapshot'));
    expect(frame.event).toBe('snapshot');
    if (frame.event !== 'snapshot') throw new Error('样本事件名不对');
    const resync = resyncSnapshotSchema.parse(frame.data);
    expect(resync.resync).toBe(true);
    expect(resync.dropped).toBeGreaterThanOrEqual(1);
    // 顶层十一键 = 九键 + resync + dropped。
    expect(Object.keys(frame.data).sort()).toHaveLength(11);
    expect(frame.id).toBe(String(resync.revision));
    // 九键形状认不得它 —— 两个键是多余的。
    expect(snapshotSchema.safeParse(frame.data).success).toBe(false);
  });

  it('resync 恒 true、dropped 至少 1：改这两格就过不了', () => {
    const base = readFixture('sse', 'resync-snapshot') as Record<string, unknown>;
    const data = { ...(base['data'] as Record<string, unknown>) };
    // resync 只认字面量 true。
    expect(resyncSnapshotSchema.safeParse({ ...data, resync: false }).success).toBe(false);
    // dropped 下限是 1：丢了 0 条就不该走重同步这条路。
    expect(resyncSnapshotSchema.safeParse({ ...data, dropped: 0 }).success).toBe(false);
    // 两个扩展键缺一不可。
    const { resync: _r, ...withoutResync } = data;
    const { dropped: _d, ...withoutDropped } = data;
    expect(resyncSnapshotSchema.safeParse(withoutResync).success).toBe(false);
    expect(resyncSnapshotSchema.safeParse(withoutDropped).success).toBe(false);
  });
});

describe('动作样本', () => {
  it.each(ACTION_FIXTURES)('actions/%s.json 过 actionFixtureSchema', (name) => {
    const fixture = actionFixtureSchema.parse(readFixture('actions', name));
    expect(fixture.status).toBeGreaterThanOrEqual(200);
  });

  it('十五个样本一个不少', () => {
    expect(listFixtures('actions')).toEqual([...ACTION_FIXTURES].sort());
  });

  it('成功体是 {ok, revision}；settings_update 与 summarize 各多带自己那几格', () => {
    for (const name of ['start-ok', 'stop-ok', 'mute-session-ok'] as const) {
      const fixture = actionFixtureSchema.parse(readFixture('actions', name));
      expect(fixture.status, name).toBe(200);
      expect(fixture.body).toEqual({ ok: true, revision: expect.any(Number) });
      expect(Object.keys(fixture.body).sort(), name).toEqual(['ok', 'revision']);
    }

    const settings = actionFixtureSchema.parse(readFixture('actions', 'settings-update-ok'));
    expect(settings.body).toEqual({
      ok: true,
      revision: 3,
      applied: ['asr.timeout_seconds'],
      pending_restart: [],
    });
    expect(settings.body).not.toHaveProperty('message');
  });

  it('summarize 成功体 = 通用信封 + 一个 summary 键，summary 恰好八格且三小段是数组', () => {
    const fixture = actionFixtureSchema.parse(readFixture('actions', 'summarize-ok'));
    expect(fixture.status).toBe(200);
    expect(fixture.action).toBe('summarize');
    expect(fixture.args).toEqual({ window: '15m' });
    expect(Object.keys(fixture.body).sort()).toEqual(['ok', 'revision', 'summary']);
    if (fixture.body.ok !== true) throw new Error('样本该是成功档');
    expect(Object.hasOwn(fixture.body, 'summary')).toBe(true);
    const summary = (fixture.body as { summary: Record<string, unknown> }).summary;
    // 应答体这一份**八格**（比 GET /api/summaries 的记录条目多一格 lines）
    expect(Object.keys(summary).sort()).toEqual([
      'at',
      'elapsed_seconds',
      'lines',
      'lost',
      'what',
      'where',
      'window',
      'window_label',
    ]);
    expect(summary['window']).toBe('15m');
    expect(summary['window_label']).toBe('15 分钟');
    expect(summary['lines']).toBe(12);
    for (const section of ['what', 'where', 'lost'] as const) {
      expect(Array.isArray(summary[section]), section).toBe(true);
      expect((summary[section] as unknown[]).length, section).toBeGreaterThan(0);
    }
  });

  it('九个 code 各有一个失败样本，status 与 code 表逐条对上', () => {
    const seen = new Map<string, number>();
    for (const name of ACTION_FIXTURES) {
      const fixture = actionFixtureSchema.parse(readFixture('actions', name));
      if (fixture.body.ok) continue;
      // 9 个 `code` 各有一个样本，其中 `summary_failed` 那一档（盘面上目前只有缺键那一份）
      const previous = seen.get(fixture.body.code);
      if (previous !== undefined) expect(fixture.status, name).toBe(previous);
      seen.set(fixture.body.code, fixture.status);
    }
    for (const code of ERROR_CODES) {
      expect(seen.get(code), code).toBe(ERROR_CODE_STATUS[code]);
    }
    expect(seen.size).toBe(ERROR_CODES.length);
  });

  it('失败信封恒四键，detail 恒是对象（空档给空对象而不是 null）', () => {
    for (const name of ACTION_FIXTURES) {
      const fixture = actionFixtureSchema.parse(readFixture('actions', name));
      if (fixture.body.ok) continue;
      expect(Object.keys(fixture.body).sort(), name).toEqual(['code', 'detail', 'message', 'ok']);
      expect(typeof fixture.body.detail, name).toBe('object');
      expect(fixture.body.detail, name).not.toBeNull();
      expect(fixture.body.message.length, name).toBeGreaterThan(0);
    }
  });

  it('already_recording 档：detail 三键，走秒量掩码成 0，场次目录名走 <session-id>', () => {
    const fixture = actionFixtureSchema.parse(readFixture('actions', 'fail-already-recording'));
    if (fixture.body.ok) throw new Error('样本该是失败档');
    expect(fixture.body.detail).toEqual({
      session_id: '<session-id>',
      dir: '<path>',
      elapsed_seconds: 0,
    });
  });

  it('draining 档：failed_steps 每条恰好 {id, error}、另有 log_path', () => {
    const fixture = actionFixtureSchema.parse(readFixture('actions', 'fail-draining'));
    if (fixture.body.ok) throw new Error('样本该是失败档');
    const steps = fixture.body.detail.failed_steps ?? [];
    expect(steps).toHaveLength(1);
    for (const step of steps) {
      expect(Object.keys(step).sort()).toEqual(['error', 'id']);
      // 样本里失败的是 `log` 那一步（第 4 步 `post` 现在停在 `waiting`：会后归两个按钮，R26）
      expect(step.id).toBe('log');
    }
    expect(fixture.body.detail.log_path).toBe('<path>');
  });

  it('capture_unavailable 档：{session_id, dir, reason}', () => {
    const fixture = actionFixtureSchema.parse(readFixture('actions', 'fail-capture-unavailable'));
    if (fixture.body.ok) throw new Error('样本该是失败档');
    expect(Object.keys(fixture.body.detail).sort()).toEqual(['dir', 'reason', 'session_id']);
  });

  it('not_recording 与 action_unavailable 两档是空对象 detail', () => {
    for (const name of ['fail-not-recording', 'fail-action-unavailable'] as const) {
      const fixture = actionFixtureSchema.parse(readFixture('actions', name));
      if (fixture.body.ok) throw new Error('样本该是失败档');
      expect(fixture.body.detail, name).toEqual({});
    }
  });

  it('invalid_setting 四档：{key, value, legal} 与 {action, known} 两个样本各落一档', () => {
    const value = actionFixtureSchema.parse(readFixture('actions', 'fail-invalid-setting'));
    if (value.body.ok) throw new Error('样本该是失败档');
    expect(value.body.detail.key).toBe('mode');
    expect(value.body.detail.value).toBe('burst');
    expect(value.body.detail.legal).toEqual(['both', 'mic', 'loopback']);

    const unknown = actionFixtureSchema.parse(
      readFixture('actions', 'fail-invalid-setting-unknown-action'),
    );
    if (unknown.body.ok) throw new Error('样本该是失败档');
    expect(unknown.body.detail.action).toBe('definitely_not_an_action');
    // 会中总结那一个已经落地，`known` 数组里就有它（次序与服务回的一致）
    expect(unknown.body.detail.known).toEqual([...ACTION_NAMES]);
    expect(unknown.body.detail.known).toContain('summarize');
  });

  it('transcript_unavailable 档：detail 恰好 {health}', () => {
    const fixture = actionFixtureSchema.parse(
      readFixture('actions', 'fail-transcript-unavailable'),
    );
    expect(fixture.status).toBe(503);
    expect(fixture.action).toBe('summarize');
    if (fixture.body.ok) throw new Error('样本该是失败档');
    expect(fixture.body.code).toBe('transcript_unavailable');
    expect(fixture.body.detail).toEqual({ health: 'stopped' });
  });

  it('summary_failed 缺键档：detail 恰好 {missing}，点名的键是那一串配置键', () => {
    const fixture = actionFixtureSchema.parse(
      readFixture('actions', 'fail-summary-failed-missing'),
    );
    expect(fixture.status).toBe(502);
    if (fixture.body.ok) throw new Error('样本该是失败档');
    expect(fixture.body.code).toBe('summary_failed');
    expect(Object.keys(fixture.body.detail)).toEqual(['missing']);
    expect(fixture.body.detail.missing).toEqual(['summary.base_url', 'summary.api_key']);
  });

  it('persist_failed 档：detail 只有 error 一键', () => {
    const fixture = actionFixtureSchema.parse(readFixture('actions', 'fail-persist-failed'));
    if (fixture.body.ok) throw new Error('样本该是失败档');
    expect(Object.keys(fixture.body.detail)).toEqual(['error']);
  });
});

describe('本场总结记录样本', () => {
  it.each(SUMMARIES_FIXTURES)('summaries/%s.json 过 summariesResponseSchema', (name) => {
    const listing = summariesResponseSchema.parse(readFixture('summaries', name));
    expect(listing.entries.length).toBeGreaterThan(0);
  });

  it('一个样本一个不少：目录里没有多余的本场记录文件', () => {
    expect(listFixtures('summaries')).toEqual([...SUMMARIES_FIXTURES].sort());
  });

  it('记录那一条**七格**：比应答体里那份少一格 `lines`', () => {
    const listing = summariesResponseSchema.parse(readFixture('summaries', 'after-one'));
    expect(listing.session_id).toBe('<session-id>');
    const entry = listing.entries[0];
    expect(entry).toBeDefined();
    expect(Object.keys(entry ?? {}).sort()).toEqual([
      'at',
      'elapsed_seconds',
      'lost',
      'what',
      'where',
      'window',
      'window_label',
    ]);
    expect(Object.hasOwn(entry ?? {}, 'lines')).toBe(false);
    expect(entry?.window).toBe('15m');
    expect(entry?.window_label).toBe('15 分钟');
  });

  it('时刻与三小段逐字来自服务：掩码只把时间换成 <iso>，正文一个字符没动', () => {
    const listing = summariesResponseSchema.parse(readFixture('summaries', 'after-one'));
    const entry = listing.entries[0];
    expect(entry?.at).toBe('<iso>');
    expect(entry?.what).toEqual(['在过下周的排期']);
    expect(entry?.where).toEqual(['说到第三项']);
    expect(entry?.lost).toEqual(['有一处识别失败的缺口']);
  });

  it('本场记录与动作成功体的三小段是同一份内容的两种形状', () => {
    const listing = summariesResponseSchema.parse(readFixture('summaries', 'after-one'));
    const action = actionFixtureSchema.parse(readFixture('actions', 'summarize-ok'));
    if (action.body.ok !== true) throw new Error('样本该是成功档');
    const summary = (action.body as { summary: Record<string, unknown> }).summary;
    const entry = listing.entries[0] as unknown as Record<string, unknown>;
    for (const key of ['window', 'window_label', 'at', 'elapsed_seconds', 'what', 'where', 'lost']) {
      expect(entry[key], key).toEqual(summary[key]);
    }
    expect(summary['lines']).toBe(12);
    expect(Object.hasOwn(entry, 'lines')).toBe(false);
  });

  it('其余键一个不多一个不少：顶层两键、session_id 走 <session-id> 那一档', () => {
    const listing = summariesResponseSchema.parse(readFixture('summaries', 'after-one'));
    expect(Object.keys(listing).sort()).toEqual(['entries', 'session_id']);
    // 目录名占位符与路径占位符是两档：串了就是漂
    const wrong = { ...listing, session_id: '<path>' };
    expect(summariesResponseSchema.safeParse(wrong).success).toBe(false);
    // 一次都没按过那一档：session_id 为 null、entries 空
    expect(summariesResponseSchema.safeParse({ session_id: null, entries: [] }).success).toBe(true);
  });
});

describe('心跳样本', () => {
  it.each(HEARTBEAT_FIXTURES)('heartbeat/%s.json 过 heartbeatSchema', (name) => {
    const beat = heartbeatSchema.parse(readFixture('heartbeat', name));
    expect(beat.service).toBe('meeting');
    expect(Object.keys(beat).sort()).toEqual([
      'recording_alive',
      'revision',
      'seconds_since_last_tick',
      'server_time',
      'service',
      'top_state',
    ]);
  });

  it('两个样本一个不少', () => {
    expect(listFixtures('heartbeat')).toEqual([...HEARTBEAT_FIXTURES].sort());
  });

  it('idle 档两字段 null（没有在录的场次，问采集活没活没有意义）', () => {
    const beat = heartbeatSchema.parse(readFixture('heartbeat', 'idle'));
    expect(beat.top_state).toBe('idle');
    expect(beat.recording_alive).toBeNull();
    expect(beat.seconds_since_last_tick).toBeNull();
    expect(beat.server_time).toBe('<iso>');
  });

  it('recording 档 recording_alive 为 true、走秒量掩码成 0', () => {
    const beat = heartbeatSchema.parse(readFixture('heartbeat', 'recording'));
    expect(beat.top_state).toBe('recording');
    expect(beat.recording_alive).toBe(true);
    expect(beat.seconds_since_last_tick).toBe(0);
  });

  it('少一个字段就不符合契约', () => {
    const beat = readFixture('heartbeat', 'idle') as Record<string, unknown>;
    const { recording_alive: _dropped, ...rest } = beat;
    expect(heartbeatSchema.safeParse(rest).success).toBe(false);
  });
});

describe('设备清单样本', () => {
  it.each(DEVICES_FIXTURES)('devices/%s.json 过 devicesResponseSchema', (name) => {
    const listing = devicesResponseSchema.parse(readFixture('devices', name));
    expect(Object.keys(listing).sort()).toEqual(['loopback', 'mic']);
    for (const rows of [listing.mic, listing.loopback]) {
      expect(rows.length).toBeGreaterThan(0);
      for (const item of rows) {
        expect(Object.keys(item).sort()).toEqual([
          'channels',
          'host_api',
          'index',
          'is_default',
          'name',
          'role',
        ]);
      }
    }
  });

  it('样本一个不少', () => {
    expect(listFixtures('devices')).toEqual([...DEVICES_FIXTURES].sort());
  });

  it('两路的 role 各归各档；系统默认输入带着 is_default 标记', () => {
    const listing = devicesResponseSchema.parse(readFixture('devices', 'all'));
    for (const item of listing.mic) expect(item.role).toBe('input');
    for (const item of listing.loopback) expect(item.role).toBe('render');
    expect(listing.mic.some((item) => item.is_default)).toBe(true);
  });

  it('多一格/少一格都不符合契约', () => {
    const listing = readFixture('devices', 'all') as Record<string, unknown>;
    expect(devicesResponseSchema.safeParse({ ...listing, extra: [] }).success).toBe(false);
    const { loopback: _dropped, ...rest } = listing;
    expect(devicesResponseSchema.safeParse(rest).success).toBe(false);
  });
});

describe('工作区样本（4.8 / 4.9，会后进度界面节点 52b24392 接的对齐用例）', () => {
  it.each(SESSIONS_FIXTURES)('sessions/%s.json 过对应的 schema', (name) => {
    if (name === 'status-after-run') {
      const parsed = sessionStatusSchema.parse(readFixture('sessions', name));
      expect(parsed.offline.state).toBe('done');
    } else {
      const parsed = sessionsResponseSchema.parse(readFixture('sessions', name));
      // 两份采集里都没有会后的活儿在飞 → 顶层第三格如实 null
      expect(parsed.post_job).toBeNull();
    }
  });

  it('三份样本一个不少：目录里没有多余的工作区样本', () => {
    expect(listFixtures('sessions')).toEqual([...SESSIONS_FIXTURES].sort());
  });

  it('应答体顶层恰好三键；每行恰好十一键，post_job 不进任何一行', () => {
    const listing = sessionsResponseSchema.parse(readFixture('sessions', 'after-open'));
    expect(Object.keys(listing).sort()).toEqual(['post_job', 'recent', 'sessions']);
    expect(listing.sessions.length).toBeGreaterThan(0);
    for (const row of listing.sessions) {
      expect(Object.keys(row), row.dir).toHaveLength(11);
    }
  });

  it('顶层多一格/少一格、行里多一格，全都不符合契约（两道硬门都在）', () => {
    const empty = readFixture('sessions', 'empty') as Record<string, unknown>;
    expect(sessionsResponseSchema.safeParse({ ...empty, extra: 1 }).success).toBe(false);
    const { post_job: _dropped, ...withoutPostJob } = empty;
    expect(sessionsResponseSchema.safeParse(withoutPostJob).success).toBe(false);

    const afterOpen = structuredClone(
      readFixture('sessions', 'after-open') as Record<string, unknown>,
    );
    (afterOpen['sessions'] as Array<Record<string, unknown>>)[0]!['post_job'] = null;
    expect(sessionsResponseSchema.safeParse(afterOpen).success).toBe(false);
  });

  it('单场状态样本：前七格与列表同源，post / offline 两格回答「跑到哪儿了」', () => {
    const parsed = sessionStatusSchema.parse(readFixture('sessions', 'status-after-run'));
    expect(Object.keys(parsed).sort()).toEqual([
      'date', 'dir', 'has_offline', 'kind', 'mode', 'offline', 'post', 'session_id', 'state',
    ]);
    expect(parsed.post).toBeNull();
    // 跨进程读盘那份账：走秒量如实 null，四档计数两路各一份
    expect(parsed.offline.in_process).toBe(false);
    expect(parsed.offline.elapsed_seconds).toBeNull();
    expect(parsed.offline.sources.map((road) => road.source)).toEqual(['mic', 'loopback']);
    for (const road of parsed.offline.sources) {
      expect(Object.keys(road).sort()).toEqual([
        'counts', 'current_chunk', 'done', 'remaining', 'source', 'total',
      ]);
      expect(Object.keys(road.counts).sort()).toEqual(['failed', 'silent', 'transcribed', 'unjudged']);
    }
    const mic = parsed.offline.sources.find((road) => road.source === 'mic');
    expect(mic?.done).toBe(2);
    expect(mic?.counts).toEqual({ transcribed: 1, silent: 1, failed: 0, unjudged: 0 });
  });

  it('单场状态多一格/少一格都不符合契约', () => {
    const fixture = readFixture('sessions', 'status-after-run') as Record<string, unknown>;
    expect(sessionStatusSchema.safeParse({ ...fixture, extra: 1 }).success).toBe(false);
    const { offline: _dropped, ...withoutOffline } = fixture;
    expect(sessionStatusSchema.safeParse(withoutOffline).success).toBe(false);
  });
});
