import { describe, expect, it } from 'vitest';
import { z } from 'zod';
import {
  ACTION_DETAIL_KEYS,
  ACTION_DETAIL_VARIANTS,
  ACTION_NAMES,
  DRAIN_STEP_IDS,
  ENVELOPE_BAR_COUNT,
  ERROR_CODES,
  actionErrorSchema,
  actionFixtureSchema,
  actionNameSchema,
  describeFailure,
  drainStepIdSchema,
  dbfsSchema,
  envelopeSchema,
  isoStringSchema,
  isFullEnvelope,
  levelsDataSchema,
  levelsFrameSchema,
  pathStringSchema,
  portSchema,
  postOkSchema,
  sessionIdSchema,
  sessionSchema,
  snapshotSchema,
  sseFrameSchema,
  summarizeOkSchema,
  summariesResponseSchema,
  urlSchema,
} from '../src/contract';
import { ACTION_FIXTURES, readFixture } from './fixtures';

/**
 * 契约层「收得对不对」的看守用例。
 *
 * `contract.test.ts` 管的是「样本必须过」；这里管另一半：**schema 得真的咬人**。
 * 每一组都配一对用例 —— 合法形态照过、明显畸形被拒 —— 只有两半都在，
 * 才算证明这条 schema 在干活。
 */

/** 断言某个 schema 恰好拒掉这些值、放行那些值。 */
function expectVerdicts(
  schema: z.ZodTypeAny,
  rejected: readonly unknown[],
  accepted: readonly unknown[],
): void {
  for (const value of rejected) {
    expect(schema.safeParse(value).success, `该拒：${JSON.stringify(value)}`).toBe(false);
  }
  for (const value of accepted) {
    expect(schema.safeParse(value).success, `该收：${JSON.stringify(value)}`).toBe(true);
  }
}

/** 造一个合法的 levels 帧，`over` 用来覆盖要打破的那一格。 */
function levelsFrame(over: Record<string, unknown> = {}): Record<string, unknown> {
  const envelope = Array.from({ length: ENVELOPE_BAR_COUNT }, (_, i) => i * 0.5);
  return {
    ts: null,
    dbfs: -12,
    peak: -6,
    bar: envelope[envelope.length - 1],
    envelope,
    ...over,
  };
}

/** 把一路帧塞进完整的 levels 帧数据里。 */
function levelsData(over: Record<string, unknown>): unknown {
  return {
    revision: 2,
    frames: { mic: levelsFrame(over), loopback: levelsFrame() },
  };
}

describe('掩码串 schema 咬得住（C1）', () => {
  it('ISO 时间戳：真时间戳与 <iso> 过，畸形串与空串被拒', () => {
    expectVerdicts(
      isoStringSchema,
      ['not-a-timestamp', '', '2025-09-06', '<session-id>', '   ', 'yesterday'],
      [
        '<iso>',
        '2025-09-06T12:00:00+08:00',
        // 秒精度之外的两种合法写法：带小数、带 Z（与 tests/_contract.py 的 _ISO_RE 同口径）。
        '2025-09-06T12:00:00.123Z',
        '2025-09-06T12:00:00',
      ],
    );
  });

  it('路径：绝对路径与 <path> 过，无路径形状的串与空串被拒', () => {
    expectVerdicts(
      pathStringSchema,
      ['', 'hello', 'lolwut', 'not-a-path', '<iso>', '<url>', '   ', '260921', '<session-id>'],
      [
        '<path>',
        'C:\\meeting\\out',
        'C:/meeting/out',
        '\\\\server\\share\\session',
        '/var/log/meeting.log',
      ],
    );
  });

  it('URL：http(s) 形态与 <url> 过，别的串与空串被拒', () => {
    expectVerdicts(
      urlSchema,
      ['lolwut', '', 'not-a-url', 'ftp://127.0.0.1', '<session-id>'],
      ['<url>', 'http://127.0.0.1:8765', 'https://127.0.0.1:8765/'],
    );
  });

  it('每一条真值分支都留着 —— 数值型掩码没被这条修法碰过', () => {
    // 这两个本来就是对的写法（number ∪ 占位符），修 C1 时只动了三条 string 型掩码。
    expectVerdicts(portSchema, ['<iso>', '8765', 8765.5], ['<port>', 8765]);
    expectVerdicts(dbfsSchema, ['<iso>', '0.5'], ['<dbfs>', 0.5, -12]);
  });

  it('场次目录名：人给的工作区目录过，路径与别的占位符被拒', () => {
    const sessionId = sessionIdSchema;
    expectVerdicts(
      sessionId,
      [
        // 空串、含分隔符（它不是名称而是路径）与带 `<` 的串都不过。
        '',
        'out/260921',
        'C:\\out\\260921',
        '<path>',
        '<iso>',
        '<url>',
        // 其他占位符串一律不是目录名。
        '<whatever>',
      ],
      [
        '<session-id>',
        // 真值就是用户给的目录名 —— 形状任意，日期命名已退场。
        '260921',
        '260921-2',
        '跟老王聊周会',
        '会议-2026-09-21',
        'ws1',
      ],
    );
  });

  it('真服务产出的 session.id 真的过得了快照：不靠占位符冒充', () => {
    // 现场旧照：真值就是裸目录名（如今是人给的名字，目录拒绝做语义解析）。
    const snap = readFixture('snapshot', 'recording') as Record<string, unknown>;
    const session = { ...(snap['session'] as Record<string, unknown>) };
    session['id'] = '260921';
    expect(sessionSchema.safeParse(session).success).toBe(true);

    session['id'] = '跟老王聊周会';
    expect(sessionSchema.safeParse(session).success).toBe(true);

    // 空串进不了快照；带路径分隔符的名称（它就不是名称了）也进不了。
    const withBad = structuredClone(snap);
    (withBad['session'] as Record<string, unknown>)['id'] = '';
    expect(
      sseFrameSchema.safeParse({ event: 'state', id: '2', data: withBad }).success,
    ).toBe(false);
    const withPathish = structuredClone(snap);
    (withPathish['session'] as Record<string, unknown>)['id'] = 'out\\260921';
    expect(
      sseFrameSchema.safeParse({ event: 'state', id: '2', data: withPathish }).success,
    ).toBe(false);

    // `dir` 那一格照旧走绝对路径判据 —— 两条 schema 各管各的。
    const withBadDir = structuredClone(snap);
    (withBadDir['session'] as Record<string, unknown>)['dir'] = '260921';
    expect(
      sseFrameSchema.safeParse({ event: 'state', id: '2', data: withBadDir }).success,
    ).toBe(false);
  });

  it('placeholder 只在占位符那半放行：串里的别的 <xxx> 不被顺带收下', () => {
    // 「放行占位符」这件事是逐字段的，一份 schema 只认自己那一个占位符。
    expect(isoStringSchema.safeParse('<session-id>').success).toBe(false);
    expect(pathStringSchema.safeParse('<iso>').success).toBe(false);
    expect(urlSchema.safeParse('<iso>').success).toBe(false);
  });

  it('畸形值真的进不了快照：service / session 两处都被拦', () => {
    const snap = readFixture('snapshot', 'recording') as Record<string, unknown>;
    const withBadUrl = structuredClone(snap);
    (withBadUrl['service'] as Record<string, unknown>)['url'] = 'lolwut';
    expect(
      sseFrameSchema.safeParse({ event: 'state', id: '2', data: withBadUrl }).success,
    ).toBe(false);

    const withBadIso = structuredClone(snap);
    (withBadIso['service'] as Record<string, unknown>)['started_at'] = 'not-a-timestamp';
    expect(
      sseFrameSchema.safeParse({ event: 'state', id: '2', data: withBadIso }).success,
    ).toBe(false);

    const withBadPath = structuredClone(snap);
    (withBadPath['session'] as Record<string, unknown>)['dir'] = 'hello';
    expect(
      sseFrameSchema.safeParse({ event: 'state', id: '2', data: withBadPath }).success,
    ).toBe(false);

    // 同一份快照原样是过的 —— 上面三条红的是被改坏的那一格。
    expect(sseFrameSchema.safeParse({ event: 'state', id: '2', data: snap }).success).toBe(true);
  });
});

/**
 * 嵌套层收严：**契约对象每一层多一个键都要红**。
 *
 * 顶层快照早先收了严、嵌套那些对象没有，于是「往 `settings.asr` 里塞一个契约外的键」
 * 能一路过到界面上 —— TS 侧对嵌套层的漂移一声不响。这里逐层钉住：每一格都拿
 * `contract/fixtures/snapshot/recording.json` 的真样本改一处，改完全份快照必须被拒。
 *
 * **唯一的例外是失败信封的 `detail`**（6.7 要界面「有哪个键显示哪个键」，运行时宽容、
 * 样本层精确），它的用例在下面那一组里，这里不碰。
 */
describe('嵌套契约对象逐层收严（N1）', () => {
  /** 取一份真样本，按 `patch` 改一处，返回「这份快照还过不过」。 */
  function snapshotAccepts(mutate: (snap: Record<string, unknown>) => void): boolean {
    const snap = structuredClone(
      readFixture('snapshot', 'recording') as Record<string, unknown>,
    );
    mutate(snap);
    return snapshotSchema.safeParse(snap).success;
  }

  /** 往 `target` 里加一个契约外的键。 */
  function injectExtra(target: Record<string, unknown>, key = '契约外的键'): void {
    target[key] = '漂进来了';
  }

  it('样本原样是过的 —— 下面每一条红的都只改了那一处', () => {
    expect(snapshotAccepts(() => {})).toBe(true);
  });

  it('service / session / settings / settings.asr 逐层收严', () => {
    const cases: Array<[string, (snap: Record<string, unknown>) => void]> = [
      ['service', (s) => injectExtra(s['service'] as Record<string, unknown>)],
      ['session', (s) => injectExtra(s['session'] as Record<string, unknown>)],
      ['settings', (s) => injectExtra(s['settings'] as Record<string, unknown>)],
      [
        'settings.asr',
        (s) =>
          injectExtra((s['settings'] as Record<string, unknown>)['asr'] as Record<string, unknown>),
      ],
    ];
    for (const [label, mutate] of cases) {
      expect(snapshotAccepts(mutate), `${label} 多一个键该被拒`).toBe(false);
    }
  });

  it('settings.devices 的两个子键已定名 —— 加进来的键照收，取值不挑形状', () => {
    // 子键此刻是 `{"mic": null, "loopback": null}`（`dca4539b` 定的名，契约正文见
    // `docs/contract.md`），`mic` 的值是设备名字符串、没配就是 `null`。这一格有意**不收严
    // 取值的形状**：`z.record(z.string(), z.unknown())` 只保证键是字符串，服务把某个子键
    // 换成对象 / 数字 / 数组都属于加性变更，前端不该在那一刻开始拒收整份快照。这里把这条
    // 边界钉住，免得日后被顺手改成 strict。
    expect(
      snapshotAccepts((s) => {
        const devices = (s['settings'] as Record<string, unknown>)['devices'] as Record<
          string,
          unknown
        >;
        devices['mic'] = { name: '麦克风', index: 0 };
      }),
    ).toBe(true);
  });

  it('sources[*] / alerts / transcript 逐层收严', () => {
    const cases: Array<[string, (snap: Record<string, unknown>) => void]> = [
      [
        'sources[0]',
        (s) =>
          injectExtra((s['sources'] as Array<Record<string, unknown>>)[0] as Record<string, unknown>),
      ],
      [
        'alerts',
        (s) => injectExtra(s['alerts'] as Record<string, unknown>),
      ],
      [
        'transcript',
        (s) => injectExtra(s['transcript'] as Record<string, unknown>),
      ],
    ];
    for (const [label, mutate] of cases) {
      expect(snapshotAccepts(mutate), `${label} 多一个键该被拒`).toBe(false);
    }
  });

  it('ended 与它的 drainStep 逐层收严（notice 槽位已随默认输出根退场）', () => {
    // recording 样本的 ended 是 null —— 往 null 上加键是没意义的，
    // 这里拿 ended-done 样本改 ended 与它的 drainStep。
    const endedSnap = structuredClone(
      readFixture('snapshot', 'ended-done') as Record<string, unknown>,
    );
    injectExtra(endedSnap['ended'] as Record<string, unknown>);
    expect(snapshotSchema.safeParse(endedSnap).success, 'ended 多一个键该被拒').toBe(false);

    const stepSnap = structuredClone(
      readFixture('snapshot', 'ended-done') as Record<string, unknown>,
    );
    const steps = (stepSnap['ended'] as Record<string, unknown>)['steps'] as Array<
      Record<string, unknown>
    >;
    injectExtra(steps[0] as Record<string, unknown>);
    expect(snapshotSchema.safeParse(stepSnap).success, 'drainStep 多一个键该被拒').toBe(false);
  });

  it('alert 六字段那一层也收严（critical 与 hints 各试一次）', () => {
    const alertSnap = structuredClone(
      readFixture('snapshot', 'alert') as Record<string, unknown>,
    );
    const alerts = alertSnap['alerts'] as Record<string, unknown>;
    const critical = alerts['critical'] as Record<string, unknown> | null;
    // 告警态样本必带 critical 那一条（提示级 hints 可能空着）。
    expect(critical, 'alert 样本该带一条 critical').not.toBeNull();
    injectExtra(critical as Record<string, unknown>);
    expect(snapshotSchema.safeParse(alertSnap).success, 'alert 多一个键该被拒').toBe(false);
  });
});

describe('describeFailure 的收放按契约来（M2）', () => {
  it('契约已列的键照旧逐个校验取值', () => {
    expectVerdicts(
      describeFailure,
      [
        { session_id: 123 },
        { elapsed_seconds: '0' },
        { failed_steps: 'nope' },
        { known: 'start' },
        { failed_steps: [{ id: 'post' }] },
        // 会中总结那两台新键（4.4 的两行）同样逐个咬：形状不对就拒
        { health: 0 },
        { missing: 'summary.base_url' },
        { attempts: 1.5 },
      ],
      [
        {},
        { session_id: '<session-id>', dir: '<path>', elapsed_seconds: 0 },
        { key: 'mode', value: 'burst', legal: ['both', 'mic', 'loopback'] },
        { failed_steps: [{ id: 'post', error: '起不来' }] },
        { health: 'stopped' },
        { missing: ['summary.base_url', 'summary.api_key'] },
        { elapsed_seconds: 25, attempts: 3 },
      ],
    );
  });

  it('契约之外的键原样放行：服务将来补一个 detail 键不会打断前端', () => {
    // 6.7 的读法是「detail 有哪个键显示哪个键」，多出来的键安静地不理。
    const extended = {
      session_id: '<session-id>',
      dir: '<path>',
      elapsed_seconds: 0,
      // 4.4 的表里现在没有这一把 —— 服务后续长出来的补充信息。
      retry_after_seconds: 5,
      hint: { nested: ['任意形状'] },
    };
    const parsed = describeFailure.safeParse(extended);
    expect(parsed.success).toBe(true);
    if (parsed.success) {
      // 已列的键照旧拿到手，多出来的键不丢。
      expect(parsed.data.session_id).toBe('<session-id>');
      expect((parsed.data as Record<string, unknown>)['retry_after_seconds']).toBe(5);
    }
  });

  it('多键不误杀：失败信封整份仍然解得开', () => {
    const envelope = {
      ok: false,
      code: 'already_recording',
      message: '已经有一场在录',
      detail: { session_id: '<session-id>', dir: '<path>', elapsed_seconds: 0, extra_note: '新键' },
    };
    expect(actionErrorSchema.safeParse(envelope).success).toBe(true);
  });

  it('信封主体照旧严格：顶层多一个键仍然要红', () => {
    const envelope = {
      ok: false,
      code: 'not_recording',
      message: '现在没有在录的场次',
      detail: {},
      unexpected_top_level: 1,
    };
    expect(actionErrorSchema.safeParse(envelope).success).toBe(false);
  });

  it('十五份动作样本全部照旧过', () => {
    for (const name of ACTION_FIXTURES) {
      expect(actionFixtureSchema.safeParse(readFixture('actions', name)).success, name).toBe(true);
    }
  });});

/**
 * M1：**漂移检测落在样本层**。
 *
 * 运行时 parse 对契约外的 detail 键宽容（`catchall`，理由与 6.7 的引用见
 * `src/contract/actions.ts` 的 `describeFailure`）；精确那一半在这里 ——
 * 拿 `docs/contract.md` 4.4 的表逐条核对 `contract/fixtures/actions/` 里的样本，
 * **多一个键、少一个键都要红**。
 *
 * 样本文件本身归另一个单元的漂移测试看守，这里只读不改。
 */
describe('detail 键集合与 4.4 表逐条相等（M1，样本层精确）', () => {
  /** 取一份失败样本的 `{code, detail}`。 */
  function failureOf(name: string): { code: string; detail: Record<string, unknown> } {
    const fixture = actionFixtureSchema.parse(readFixture('actions', name));
    if (fixture.body.ok) throw new Error(`${name} 该是失败档`);
    return { code: fixture.body.code, detail: fixture.body.detail as Record<string, unknown> };
  }

  it('九个 code 各自对应的样本都在，code 表一个不多一个不少', () => {
    const seen = new Set<string>();
    for (const name of ACTION_FIXTURES) {
      const fixture = actionFixtureSchema.parse(readFixture('actions', name));
      if (fixture.body.ok) continue;
      seen.add(fixture.body.code);
    }
    expect([...seen].sort()).toEqual([...ERROR_CODES].sort());
  });

  it('独档的五个 code：样本的 detail 键集合与 4.4 表精确相等', () => {
    const samples: Record<string, string> = {
      already_recording: 'fail-already-recording',
      not_recording: 'fail-not-recording',
      persist_failed: 'fail-persist-failed',
      action_unavailable: 'fail-action-unavailable',
      transcript_unavailable: 'fail-transcript-unavailable',
    };
    for (const [code, fixtureName] of Object.entries(samples)) {
      const { code: actualCode, detail } = failureOf(fixtureName);
      expect(actualCode, fixtureName).toBe(code);
      const expected = ACTION_DETAIL_KEYS[code as keyof typeof ACTION_DETAIL_KEYS];
      expect(expected, `${code} 该有精确键集合`).not.toBeNull();
      // 逐键精确相等：多一个键、少一个键都红（顺序无关）。
      expect(Object.keys(detail).sort(), `${code} 的 detail 键集合`).toEqual(
        [...(expected ?? [])].sort(),
      );
    }
  });

  it('draining 五档：样本落在 4.4 写的那一档上，重复按「直接出纪要」那一档的键也在表里', () => {
    const { detail } = failureOf('fail-draining');
    const variants = ACTION_DETAIL_VARIANTS['draining'] ?? [];
    const keys = Object.keys(detail).sort();
    const matched = variants.some((variant) => variant.slice().sort().join(',') === keys.join(','));
    expect(matched, `draining 样本的键 ${keys.join(',')} 不在 4.4 的五档里`).toBe(true);

    // 五档的键并集就是 4.4 表里列出来的那九个（另三档 —— {kind, dir, started_at} /
    // {top_state, session_id, dir} / 空对象 —— 由 tests/test_post_actions.py 实测，
    // 键集合照样要在表里）：多一个键就落不进任何一档。
    const union = new Set(variants.flat());
    expect([...union].sort()).toEqual([
      'dir',
      'failed_steps',
      'kind',
      'log_path',
      'reason',
      'session_id',
      'started_at',
      'status_path',
      'top_state',
    ]);
  });

  it('capture_unavailable 四档：每个样本各落在 4.4 写的某一档上', () => {
    const { detail } = failureOf('fail-capture-unavailable');
    const variants = ACTION_DETAIL_VARIANTS['capture_unavailable'] ?? [];
    const keys = Object.keys(detail).sort();
    // 样本必须恰好等于其中一档 —— 不在任何一档里就说明形状漂了。
    const matched = variants.some((variant) => variant.slice().sort().join(',') === keys.join(','));
    expect(matched, `capture_unavailable 样本的键 ${keys.join(',')} 不在 4.4 的四档里`).toBe(true);

    // 四档的键并集就是 4.4 表里列出来的那五个：多一个键就落不进任何一档。
    const union = new Set(variants.flat());
    expect([...union].sort()).toEqual(['dir', 'error', 'reason', 'session_id', 'source']);
  });

  it('invalid_setting 四档：两个样本各落在 4.4 写的某一档上', () => {
    const variants = ACTION_DETAIL_VARIANTS['invalid_setting'] ?? [];
    for (const name of ['fail-invalid-setting', 'fail-invalid-setting-unknown-action'] as const) {
      const { detail } = failureOf(name);
      const keys = Object.keys(detail).sort();
      const matched = variants.some(
        (variant) => variant.slice().sort().join(',') === keys.join(','),
      );
      expect(matched, `${name} 的键 ${keys.join(',')} 不在 4.4 的四档里`).toBe(true);
    }

    // 四档的键并集就是 4.4 表里列出来的那八个（`source` / `fault` 那一档现在没有样本，
    // 但它在表里，因此并集里必须有它）。
    const union = new Set(variants.flat());
    expect([...union].sort()).toEqual([
      'action',
      'error',
      'fault',
      'key',
      'known',
      'legal',
      'source',
      'value',
    ]);
  });

  it('summary_failed 两档：缺键那一档各落在 4.4 写的某一档上', () => {
    const variants = ACTION_DETAIL_VARIANTS['summary_failed'] ?? [];
    const { detail } = failureOf('fail-summary-failed-missing');
    const keys = Object.keys(detail).sort();
    const matched = variants.some(
      (variant) => variant.slice().sort().join(',') === keys.join(','),
    );
    expect(matched, `summary_failed 样本的键 ${keys.join(',')} 不在 4.4 的两档里`).toBe(true);

    // 两档的键并集就是 4.4 表里列出来的那三个（`elapsed_seconds` / `attempts` 那一档要一个
    // 真等满预算的假端点，由 `tests/test_summary_action.py` 逐档实测，样本目录里没有它）。
    const union = new Set(variants.flat());
    expect([...union].sort()).toEqual(['attempts', 'elapsed_seconds', 'missing']);
  });

  it('五个成功样本没有 detail 这把键（成功体是另一套形状）', () => {
    for (const name of [
      'start-ok',
      'stop-ok',
      'mute-session-ok',
      'settings-update-ok',
      'summarize-ok',
    ] as const) {
      const fixture = actionFixtureSchema.parse(readFixture('actions', name));
      expect(fixture.body.ok, name).toBe(true);
      expect(Object.hasOwn(fixture.body, 'detail'), name).toBe(false);
    }
  });

  it('failed_steps 每条恰好 {id, error} 两键', () => {
    const { detail } = failureOf('fail-draining');
    const steps = detail['failed_steps'];
    expect(Array.isArray(steps)).toBe(true);
    for (const step of steps as Record<string, unknown>[]) {
      expect(Object.keys(step).sort()).toEqual(['error', 'id']);
    }
  });
});

/**
 * 动作名清单本身也有一道看守。
 *
 * 这一层原先没有任何用例钉住 `ACTION_NAMES` / `actionNameSchema`：`web/src/api/actions.ts`
 * 的请求 schema 用的是 `z.string()`，于是契约层少一名也照样 `vitest` / `typecheck` 全绿
 * （`reprobe` 落地时就漏在这里）。下面两条一条钉枚举、一条钉它**覆盖得住服务报的 `known`**。
 */
describe('动作名清单与样本里的 known 对齐（看守）', () => {
  it('`ACTION_NAMES` 逐字十九个，`actionNameSchema` 就是这一份', () => {
    expect([...ACTION_NAMES]).toEqual([
      'start',
      'stop',
      'mute_session',
      'settings_update',
      'reprobe',
      'dev_inject',
      'summarize',
      'workspace_open',
      'workspace_remove',
      'post_offline',
      'post_minutes',
      'post_stop',
      'transcode_run',
      'transcode_pause',
      'transcode_resume',
      'pause',
      'resume',
      'set_source',
      'speaker_rename',
    ]);
    expect(actionNameSchema.options).toEqual([...ACTION_NAMES]);
  });

  it('十九个名字全过 schema，清单外的名字与大小写变体被拒', () => {
    expectVerdicts(
      actionNameSchema,
      ['PAUSE', 'START', 'Summarize', 'setSource', ''],
      [...ACTION_NAMES],
    );
  });

  it('样本 `known` 的每一个名字都在本层枚举里：清单少一名就红', () => {
    const unknown = actionFixtureSchema.parse(
      readFixture('actions', 'fail-invalid-setting-unknown-action'),
    );
    if (unknown.body.ok) throw new Error('样本该是失败档');
    const known = unknown.body.detail.known ?? [];
    // 服务报的每一个名字逐个必须是本层认得的动作名（少一名当场红）。
    for (const name of known) {
      expect(actionNameSchema.safeParse(name).success, `known 里的 ${name}`).toBe(true);
    }
    expect(new Set(known)).toEqual(new Set(ACTION_NAMES));
  });
});

/**
 * 会后回执（`post_minutes` 的 `inputs` / `note`，contract 4.3）的看守：两档真形状
 * （普通成功、带 `inputs`+`note` 的说破档）都要过，档位值写错或多出键要咬。
 */
describe('postOkSchema 收真回执、咬假回执（看守）', () => {
  const base = { ok: true, revision: 9 } as const;

  it('三份真形状照过：offline 只带 {kind, dir}；minutes 恒带 inputs，降级档再加 note', () => {
    expectVerdicts(
      postOkSchema,
      [],
      [
        { ...base, post: { kind: 'offline', dir: '<path>' } },
        { ...base, post: { kind: 'minutes', dir: '<path>', inputs: 'live_only' } },
        {
          ...base,
          post: {
            kind: 'minutes',
            dir: '<path>',
            inputs: 'live_only',
            note: '会后的精细稿还在跑：这一趟拿到的是会中稿；等它跑完再出纪要能对上会后稿',
          },
        },
      ],
    );
  });

  it('inputs 四档之外的取值与 post 格里的多余键都拒（.strict() 照旧咬人）', () => {
    expectVerdicts(
      postOkSchema,
      [
        { ...base, post: { kind: 'minutes', dir: '<path>', inputs: 'both_plus' } },
        { ...base, post: { kind: 'minutes', dir: '<path>', inputs: null } },
        { ...base, post: { kind: 'offline', dir: '<path>', started: true } },
      ],
      [],
    );
  });
});

/**
 * 收尾步 id 清单（`DRAIN_STEP_IDS`）的看守：清单与服务的样本**双向对齐** —— 样本里
 * 冒出清单外的 id、或清单里躺着服务永远不发的 id（比如已摘出收尾序列的 `transcode`），
 * 两边都要红。`drainStepIdSchema` 是枚举成员判定，子集照过，清单自己的多余没人盯：
 * 下一个照它画「收尾明细」的人会画出一行服务永远不发的步骤。
 */
describe('收尾步 id 清单与样本对齐（看守）', () => {
  it('ended-done 样本的 steps id 与 DRAIN_STEP_IDS 逐一对齐（次序与成员都钉住）', () => {
    const snap = snapshotSchema.parse(readFixture('snapshot', 'ended-done'));
    const ids = (snap.ended?.steps ?? []).map((step) => step.id);
    expect(ids).toEqual([...DRAIN_STEP_IDS]);
  });

  it('transcode 不是收尾步：schema 拒它，清单里也没有它', () => {
    expect(drainStepIdSchema.safeParse('transcode').success).toBe(false);
    expect([...DRAIN_STEP_IDS]).not.toContain('transcode');
  });
});

describe('levels 帧的结构不变量（M4）', () => {
  it('包络长度恰 15：3 根这种帧要红', () => {
    // 复审实测的那一帧：长度 3 的包络原本直接通过。
    expect(levelsFrameSchema.safeParse(levelsFrame({ envelope: [1, 2, 3], bar: 3 })).success).toBe(
      false,
    );
    expect(levelsDataSchema.safeParse(levelsData({ envelope: [1, 2, 3], bar: 3 })).success).toBe(
      false,
    );
  });

  it('长度多一根、少一根都不行', () => {
    const fifteen = Array.from({ length: ENVELOPE_BAR_COUNT }, () => -3);
    expect(levelsFrameSchema.safeParse(levelsFrame({ envelope: fifteen, bar: -3 })).success).toBe(
      true,
    );
    expect(
      levelsFrameSchema.safeParse(levelsFrame({ envelope: [...fifteen, -3], bar: -3 })).success,
    ).toBe(false);
    expect(
      levelsFrameSchema.safeParse(levelsFrame({ envelope: fifteen.slice(1), bar: -3 })).success,
    ).toBe(false);
  });

  it('bar == envelope[-1]：对不上要红，对上才过', () => {
    // 默认帧的最后一根是 7（`(ENVELOPE_BAR_COUNT - 1) * 0.5`），bar 默认就取它。
    expect(levelsFrameSchema.safeParse(levelsFrame({ bar: 999 })).success).toBe(false);
    expect(levelsFrameSchema.safeParse(levelsFrame({ bar: -6 })).success).toBe(false);
    expect(levelsFrameSchema.safeParse(levelsFrame()).success).toBe(true);
    // 这根柱是掩码占位符时同样逐步比对。
    const masked = Array.from({ length: ENVELOPE_BAR_COUNT }, () => '<dbfs>');
    expect(levelsFrameSchema.safeParse(levelsFrame({ envelope: masked, bar: '<dbfs>' })).success).toBe(
      true,
    );
    expect(levelsFrameSchema.safeParse(levelsFrame({ envelope: masked, bar: -6 })).success).toBe(
      false,
    );
  });

  it('两条不变量合起来咬住复审给的那一帧', () => {
    const frame = { ts: null, dbfs: 1, peak: 1, bar: 999, envelope: [1, 2, 3] };
    expect(levelsFrameSchema.safeParse(frame).success).toBe(false);
    // 把长度补到 15、bar 对上最后一根之后就合法了 —— 红的是那两条不变量本身。
    const fixed = Array.from({ length: ENVELOPE_BAR_COUNT }, (_, i) => i);
    expect(
      levelsFrameSchema.safeParse({ ...frame, envelope: fixed, bar: fixed[fixed.length - 1] })
        .success,
    ).toBe(true);
  });

  it('isFullEnvelope 是长度判据的唯一出处，而且真的被 schema 用着', () => {
    expect(isFullEnvelope([])).toBe(false);
    expect(isFullEnvelope(Array.from({ length: ENVELOPE_BAR_COUNT }))).toBe(true);
    // schema 里那条 refine 就是它：长度 15 过、14 拒。
    expect(
      levelsFrameSchema.safeParse(levelsFrame({ envelope: Array(ENVELOPE_BAR_COUNT).fill(0), bar: 0 }))
        .success,
    ).toBe(true);
    expect(
      levelsFrameSchema.safeParse(
        levelsFrame({ envelope: Array(ENVELOPE_BAR_COUNT - 1).fill(0), bar: 0 }),
      ).success,
    ).toBe(false);
  });

  it('只对 levels 帧收紧：快照侧的空包络（空闲态）照旧合法', () => {
    // 空闲态快照里两路 envelope 是空数组 —— 拿 15 去卡快照会误伤这一档。
    expect(envelopeSchema.safeParse([]).success).toBe(true);
    for (const name of ['idle'] as const) {
      const snap = readFixture('snapshot', name) as {
        sources: { envelope: unknown[] }[];
      };
      for (const source of snap.sources) {
        expect(source.envelope, name).toEqual([]);
      }
    }
    // levels 帧那一侧就不放行空包络了。
    expect(levelsFrameSchema.safeParse(levelsFrame({ envelope: [], bar: null })).success).toBe(false);
  });

  it('golden 样本 levels.json 照旧过（两路都过）', () => {
    const frame = sseFrameSchema.parse(readFixture('sse', 'levels'));
    expect(frame.event).toBe('levels');
    if (frame.event !== 'levels') throw new Error('样本事件名不对');
    for (const id of ['mic', 'loopback'] as const) {
      expect(isFullEnvelope(frame.data.frames[id].envelope), id).toBe(true);
    }
  });

  it('样本里把长度改坏一格，整帧就过不了', () => {
    const frame = readFixture('sse', 'levels') as {
      data: { frames: { mic: { envelope: unknown[]; bar: unknown } } };
    };
    frame.data.frames.mic.envelope = frame.data.frames.mic.envelope.slice(0, 3);
    expect(sseFrameSchema.safeParse(frame).success).toBe(false);
  });
});

describe('会中总结那两套 schema 咬得住（S1）', () => {
  /** 一份合法的成功体（照 `contract/fixtures/actions/summarize-ok.json` 的形状造）。 */
  function okBody(over: Record<string, unknown> = {}): Record<string, unknown> {
    return {
      ok: true,
      revision: 2,
      summary: {
        window: '15m',
        window_label: '15 分钟',
        at: '2026-09-21T21:03:00+08:00',
        elapsed_seconds: 9.4,
        lines: 37,
        what: ['在过排期'],
        where: ['说到第三项'],
        lost: ['有一处缺口'],
        ...over,
      },
    };
  }

  /** 拿一份改过的 `summary` 子对象装出整份应答体。 */  function bodyWithSummary(summary: Record<string, unknown>): Record<string, unknown> {
    return { ok: true, revision: 2, summary };
  }

  it('合法成功体照过，三个数组空着也合法（B15 的「空数组合法」）', () => {
    expect(summarizeOkSchema.safeParse(okBody()).success).toBe(true);
    expect(
      summarizeOkSchema.safeParse(
        okBody({ what: [], where: [], lost: [], lines: 0 }),
      ).success,
    ).toBe(true);
  });

  it('八格一格都不能少、多一格也拒（`.strict()`）', () => {
    for (const key of [
      'window',
      'window_label',
      'at',
      'elapsed_seconds',
      'lines',
      'what',
      'where',
      'lost',
    ]) {
      const summary: Record<string, unknown> = {
        ...(okBody()['summary'] as Record<string, unknown>),
      };
      delete summary[key];
      expect(summarizeOkSchema.safeParse(bodyWithSummary(summary)).success, `少 ${key}`).toBe(
        false,
      );
    }
    expect(summarizeOkSchema.safeParse(okBody({ 契约外的键: 1 })).success).toBe(false);
    // 顶层也收严：多一个键就红
    expect(summarizeOkSchema.safeParse({ ...okBody(), extra: 1 }).success).toBe(false);
    // `ok` 必须是字面量 true
    expect(summarizeOkSchema.safeParse({ ...okBody(), ok: false }).success).toBe(false);
  });

  it('三小段的取值形状照咬：字符串数组，不是字符串', () => {
    expect(summarizeOkSchema.safeParse(okBody({ what: '在过排期' })).success).toBe(false);
    expect(summarizeOkSchema.safeParse(okBody({ where: [1, 2] })).success).toBe(false);
    expect(summarizeOkSchema.safeParse(okBody({ lines: 3.5 })).success).toBe(false);
    // 时刻那一格走 ISO 判据（真时间戳与 `<iso>` 两样放行，别的形状拒）
    expect(summarizeOkSchema.safeParse(okBody({ at: '<iso>' })).success).toBe(true);
    expect(summarizeOkSchema.safeParse(okBody({ at: 'not-a-timestamp' })).success).toBe(false);
  });

  it('本场记录那条：七格不多不少，多了 `lines` 就拒', () => {
    const listing = {
      session_id: '260921',
      entries: [
        {
          window: '15m',
          window_label: '15 分钟',
          at: '2026-09-21T21:03:00+08:00',
          elapsed_seconds: 9.4,
          what: ['在过排期'],
          where: ['说到第三项'],
          lost: ['有一处缺口'],
        },
      ],
    };
    expect(summariesResponseSchema.safeParse(listing).success).toBe(true);
    // 应答体那一份多一格 `lines` —— 记录这一份收的是七格，多一格就拒
    const withLines = structuredClone(listing);
    (withLines.entries[0] as Record<string, unknown>)['lines'] = 37;
    expect(summariesResponseSchema.safeParse(withLines).success).toBe(false);
    // 空记录那一档照过
    expect(summariesResponseSchema.safeParse({ session_id: null, entries: [] }).success).toBe(true);
  });

  it('本场记录端点的 session_id 走目录名那一档：路径占位符进不来', () => {
    const base = { session_id: '260921', entries: [] };
    expect(summariesResponseSchema.safeParse(base).success).toBe(true);
    expect(summariesResponseSchema.safeParse({ ...base, session_id: '<path>' }).success).toBe(false);
    expect(summariesResponseSchema.safeParse({ ...base, session_id: '' }).success).toBe(false);
  });
});

/**
 * 会中总结这一轮**把那道快照硬门留在原处**（`1987cb20/phase3/task.md` 的边界第 3 条）。
 *
 * 结果走动作的应答体、记录走 `GET`，快照里一个 `summary` 键都没有 —— 这条断言在
 * `contract.test.ts` 里（顶层恰好十键、不许多 `summary`），这里补的是**反向**那一半：
 * 新加的两套 schema 都收不下十键快照，于是「结果误进了快照」这件事在两端都会红。
 */
describe('快照那道门不许撬（S2）', () => {
  it('快照过不了总结那两套 schema：十键里没有 `summary`，也没有 `entries`', () => {
    const snap = readFixture('snapshot', 'idle');
    expect(summarizeOkSchema.safeParse(snap).success).toBe(false);
    expect(summariesResponseSchema.safeParse(snap).success).toBe(false);
  });

  it('给快照强塞一个 `summary` 键，快照 schema 当场拒（`.strict()` 那道门还在）', () => {
    const snap = readFixture('snapshot', 'idle') as Record<string, unknown>;
    expect(snapshotSchema.safeParse(snap).success).toBe(true);
    expect(snapshotSchema.safeParse({ ...snap, summary: null }).success).toBe(false);
    expect(snapshotSchema.safeParse({ ...snap, summary: { window: '15m' } }).success).toBe(false);
  });
});

