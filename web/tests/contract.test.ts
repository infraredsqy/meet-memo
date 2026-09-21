import { describe, expect, it } from 'vitest';
import {
  ACTION_FIXTURES,
  HEARTBEAT_FIXTURES,
  SNAPSHOT_FIXTURES,
  SSE_FIXTURES,
  listFixtures,
  readFixture,
} from './fixtures';
import {
  ENVELOPE_BAR_COUNT,
  ERROR_CODES,
  ERROR_CODE_STATUS,
  actionFixtureSchema,
  heartbeatSchema,
  resyncSnapshotSchema,
  snapshotSchema,
  sseFrameSchema,
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

  it('idle 与 idle-notice 的三处 null 齐：session / ended / notice 各看一次', () => {
    const idle = snapshotSchema.parse(readFixture('snapshot', 'idle'));
    expect(idle.session).toBeNull();
    expect(idle.ended).toBeNull();
    expect(idle.notice).toBeNull();
    expect(idle.top_state).toBe('idle');

    const notice = snapshotSchema.parse(readFixture('snapshot', 'idle-notice'));
    expect(notice.session).toBeNull();
    expect(notice.ended).toBeNull();
    expect(notice.notice?.kind).toBe('restart_unfinished');
    // 掩码把路径换成了 `<path>`，schema 认它。
    expect(notice.notice?.session_dir).toBe('<path>');
    expect(notice.notice?.log_path).toBe('<path>');
    expect(notice.notice?.text).toContain('<path>');
  });

  it('四态样本各落在该有的那一态', () => {
    const expected: Record<(typeof SNAPSHOT_FIXTURES)[number], Snapshot['top_state']> = {
      idle: 'idle',
      'idle-notice': 'idle',
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
    for (const name of ['idle', 'idle-notice'] as const) {
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

  it('ended-done：四步次序 flush/transcode/log/post，status 是 skipped/skipped/done/skipped', () => {
    const snap = snapshotSchema.parse(readFixture('snapshot', 'ended-done'));
    expect(snap.ended?.phase).toBe('done');
    expect(snap.ended?.error).toBeNull();
    expect(snap.ended?.steps.map((step) => step.id)).toEqual([
      'flush',
      'transcode',
      'log',
      'post',
    ]);
    expect(snap.ended?.steps.map((step) => step.status)).toEqual([
      'skipped',
      'skipped',
      'done',
      'skipped',
    ]);
    // error 键恒在：没有错误时它填 null，键本身照旧留着。
    for (const step of snap.ended?.steps ?? []) {
      expect(Object.hasOwn(step, 'error'), step.id).toBe(true);
    }
    expect(snap.session?.frozen).toBe(true);
    expect(snap.sources.map((s) => s.health)).toEqual(['unprobed', 'unprobed']);
  });

  it('ended-draining：post 步 failed、ended.error 有值、两路停 stopped', () => {
    const snap = snapshotSchema.parse(readFixture('snapshot', 'ended-draining'));
    expect(snap.ended?.phase).toBe('draining');
    expect(snap.ended?.error).toContain('会后流程启动失败');
    const post = snap.ended?.steps.find((step) => step.id === 'post');
    expect(post?.status).toBe('failed');
    expect(post?.error).toBe(snap.ended?.error);
    expect(snap.sources.map((s) => s.health)).toEqual(['stopped', 'stopped']);
  });

  it('settings 与 session 的掩码格逐键对上', () => {
    const snap = snapshotSchema.parse(readFixture('snapshot', 'recording'));
    expect(snap.service.started_at).toBe('<iso>');
    expect(snap.service.port).toBe('<port>');
    expect(snap.service.url).toBe('<url>');
    expect(snap.service.log_path).toBe('<path>');
    expect(snap.service.out_root).toBe('<path>');
    expect(snap.session?.id).toBe('<session-id>');
    expect(snap.session?.dir).toBe('<path>');
    expect(snap.session?.started_at).toBe('<iso>');
    expect(snap.session?.elapsed_seconds).toBe(0);
    expect(snap.sources[0]?.level_dbfs).toBe('<dbfs>');
    expect(snap.settings.heartbeat_interval_seconds).toBe(10);
    expect(snap.settings.devices).toEqual({ mic: null, loopback: null });
    // 心跳周期的唯一来源就是这一个键。
    expect(Object.keys(snap.settings)).toEqual([
      'alarm_sound',
      'heartbeat_interval_seconds',
      'asr',
      'devices',
    ]);
  });

  it('顶层恰好十键，多一个键就要红（第 7 章那些键位不该冒出来）', () => {
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
        'notice',
      ].sort(),
    );
    expect(snap).not.toHaveProperty('summary');
    const extra = { ...snap, summary: null };
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

  it('首帧是 snapshot、带 id、data 就是十键快照、id == data.revision', () => {
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
    // 顶层十二键 = 十键 + resync + dropped。
    expect(Object.keys(frame.data).sort()).toHaveLength(12);
    expect(frame.id).toBe(String(resync.revision));
    // 十键形状认不得它 —— 两个键是多余的。
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

  it('十二个样本一个不少', () => {
    expect(listFixtures('actions')).toEqual([...ACTION_FIXTURES].sort());
  });

  it('成功体是 {ok, revision}；settings_update 那档多两个数组且没有 message', () => {
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

  it('七个 code 各有一个失败样本，status 与 code 表逐条对上', () => {
    const seen = new Map<string, number>();
    for (const name of ACTION_FIXTURES) {
      const fixture = actionFixtureSchema.parse(readFixture('actions', name));
      if (fixture.body.ok) continue;
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
      expect(step.id).toBe('post');
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

  it('invalid_setting 两档：{key, value, legal} 与 {action, known}', () => {
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
    expect(unknown.body.detail.known).toEqual([
      'start',
      'stop',
      'mute_session',
      'settings_update',
      'reprobe',
      'dev_inject',
    ]);
    // 保留登记里没有落地的那一个动作名不在 known 里。
    expect(unknown.body.detail.known).not.toContain('summarize');
  });

  it('persist_failed 档：detail 只有 error 一键', () => {
    const fixture = actionFixtureSchema.parse(readFixture('actions', 'fail-persist-failed'));
    if (fixture.body.ok) throw new Error('样本该是失败档');
    expect(Object.keys(fixture.body.detail)).toEqual(['error']);
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
