import { describe, expect, it } from 'vitest';

import {
  DEFAULT_SUMMARY_WINDOW,
  SUMMARY_CLIENT_TIMEOUT,
  SUMMARY_EMPTY_NOTE,
  SUMMARY_LOG_EMPTY,
  SUMMARY_SECTION_TITLES,
  SUMMARY_WINDOWS,
  clientFailureOf,
  initialSummaryState,
  runSummaryExchange,
  selectSummaryWindow,
  startSummary,
  summaryAvailable,
  summaryEmptyNote,
  summaryFailureView,
  summaryLogEntry,
  summaryLogLabel,
  summaryLogView,
  summaryResultView,
  summaryRunningNote,
  summarySections,
  summaryWhy,
  windowLabel,
  type SummaryAvailabilityInput,
  type SummaryFailure,
} from '../src/live/summary';
import { formatWallClock } from '../src/live/format';
import { T } from './live-fixtures';

/**
 * 总结区那份纯逻辑：档位、可用性与原因、三小段的取值与空态、五档失败的分派、
 * 记录条目的排版。
 *
 * **这一份不碰 DOM**（`web/tests` 是 node 环境）：带 DOM 的那几条（可见性、矩形、
 * 点击、进度指示）走无头浏览器脚本 + 截图证据，见交付报告的「无头浏览器」那一节。
 */

/** 一次成功的应答（照 `contract/fixtures/actions/summarize-ok.json` 的形状）。 */
function answer(over: Record<string, unknown> = {}) {
  return {
    window: '15m',
    window_label: '15 分钟',
    at: T('21:03:00'),
    elapsed_seconds: 9.4,
    lines: 37,
    what: ['在过下周的排期'],
    where: ['说到第三项'],
    lost: ['有一处识别失败的缺口'],
    ...over,
  };
}

/** 应答原文 → 结果面板那一份（注入点收到的就是它，转换在控制器里做）。 */
function view(over: Record<string, unknown> = {}) {
  return summaryResultView(answer(over));
}

/**
 * 一个「挂住不回」的应答口子：用例握住 `release`，想什么时候放行就什么时候放行。
 *
 * 这样写而不是 `setTimeout` 轮询：这一档量的全是**时序**（兜底线到点没到、迟到的应答
 * 不许覆盖），事件驱动比定时器干净，也不随机器快慢飘。
 */
function held(): {
  request: () => Promise<ReturnType<typeof view>>;
  release: () => void;
} {
  let resolveHeld!: (value: ReturnType<typeof view>) => void;
  const promise = new Promise<ReturnType<typeof view>>((resolve) => {
    resolveHeld = resolve;
  });
  return {
    request: () => promise,
    release: () => resolveHeld(view()),
  };
}

/** 一份「能不能按」的输入，默认是**录着音、转写正常、心跳在、没跑**那一档。 */
function availability(over: Partial<SummaryAvailabilityInput> = {}): SummaryAvailabilityInput {
  return {
    topState: 'recording',
    lost: false,
    stalled: false,
    transcriptHealth: 'normal',
    running: false,
    ...over,
  };
}

/** 一次失败：`code` + `detail` 原文（`message` 只有 `not_recording` 那一档用得上）。 */
function failure(code: string, fields: Record<string, unknown> = {}, message = ''): SummaryFailure {
  return { code, message, fields };
}

describe('四个窗口档位', () => {
  it('四档逐字：5 分钟 / 15 分钟 / 30 分钟 / 整场', () => {
    expect(SUMMARY_WINDOWS.map((item) => item.label)).toEqual([
      '5 分钟',
      '15 分钟',
      '30 分钟',
      '整场',
    ]);
  });

  it('档位 → 取值：5m / 15m / 30m / full', () => {
    expect(SUMMARY_WINDOWS.map((item) => item.choice)).toEqual(['5m', '15m', '30m', 'full']);
  });

  it('缺省选中 15 分钟', () => {
    expect(DEFAULT_SUMMARY_WINDOW).toBe('15m');
    expect(initialSummaryState().window).toBe('15m');
    expect(windowLabel(DEFAULT_SUMMARY_WINDOW)).toBe('15 分钟');
  });

  it('换档位只改选择，别的一格不动', () => {
    const before = startSummary(initialSummaryState());
    const after = selectSummaryWindow(before, 'full');
    expect(after.window).toBe('full');
    expect(after.phase).toBe(before.phase);
    expect(after.result).toBeNull();
  });

  it('取值域外的档位不猜人话标签：取不到就照取值本身写', () => {
    expect(windowLabel('2h')).toBe('2h');
  });
});

describe('可用性与禁用原因', () => {
  it('录音中 / 告警态、转写正常、心跳在 → 可用', () => {
    expect(summaryAvailable(availability({ topState: 'recording' }))).toBe(true);
    expect(summaryAvailable(availability({ topState: 'alert' }))).toBe(true);
    expect(summaryWhy(availability())).toBeNull();
  });

  it('空闲 → 禁用，原因用设计章 2.5 的现成口径', () => {
    expect(summaryAvailable(availability({ topState: 'idle' }))).toBe(false);
    expect(summaryWhy(availability({ topState: 'idle' }))).toBe('开始录音后才能总结');
  });

  it('已结束（收尾中 / 已完成）→ 禁用，说这场已经结束', () => {
    expect(summaryAvailable(availability({ topState: 'ended' }))).toBe(false);
    expect(summaryWhy(availability({ topState: 'ended' }))).toBe(
      '这场已经结束，会中总结只在录音时按。',
    );
  });

  it('转写健康位是「停」→ 禁用，原因用设计章 4.5 那一句', () => {
    const down = availability({ transcriptHealth: 'stopped' });
    expect(summaryAvailable(down)).toBe(false);
    expect(summaryWhy(down)).toBe('转写停了，总结没有材料可读');
  });

  it('转写「落后」照常可用（窗口可以取小一点）', () => {
    expect(summaryAvailable(availability({ transcriptHealth: 'behind' }))).toBe(true);
    expect(summaryWhy(availability({ transcriptHealth: 'behind' }))).toBeNull();
  });

  it('心跳失联压在任何一档之上（章 4.5：它说的是一件更外面的事）', () => {
    const lost = availability({ topState: 'idle', lost: true });
    expect(summaryAvailable(lost)).toBe(false);
    expect(summaryWhy(lost)).toBe('心跳失联，先看服务那边');
  });

  it('采集已停 → 禁用并先说眼前这件事', () => {
    const stalled = availability({ stalled: true });
    expect(summaryAvailable(stalled)).toBe(false);
    expect(summaryWhy(stalled)).toBe('采集已经停了，先看服务那边');
  });

  it('正在跑 → 按不动，那一格留给进行中那句', () => {
    const running = availability({ running: true });
    expect(summaryAvailable(running)).toBe(false);
    expect(summaryWhy(running)).toBeNull();
  });
});

describe('进行中那一句', () => {
  it('`正在总结刚才的 15 分钟…`（跟着所选档位走）', () => {
    expect(summaryRunningNote('15m')).toBe('正在总结刚才的 15 分钟…');
    expect(summaryRunningNote('5m')).toBe('正在总结刚才的 5 分钟…');
    expect(summaryRunningNote('30m')).toBe('正在总结刚才的 30 分钟…');
    expect(summaryRunningNote('full')).toBe('正在总结刚才的 整场…');
  });

  it('那一句里没有倒计时、也没有秒数', () => {
    expect(summaryRunningNote('15m')).not.toMatch(/\d+\s*秒/);
  });
});

describe('三小段与结果面板', () => {
  it('段标题固定，次序恒为 讲什么 / 讲到哪 / 丢掉什么', () => {
    expect(SUMMARY_SECTION_TITLES).toEqual(['刚才在讲什么', '讲到哪儿了', '丢掉了什么']);
    const sections = summarySections(answer());
    expect(sections.map((section) => section.title)).toEqual([...SUMMARY_SECTION_TITLES]);
  });

  it('三小段的正文照服务给的原样摆（一条不多一条不少）', () => {
    const sections = summarySections(answer({ what: ['甲', '乙'], where: ['丙'], lost: [] }));
    expect(sections[0]?.items).toEqual(['甲', '乙']);
    expect(sections[1]?.items).toEqual(['丙']);
    expect(sections[2]?.items).toEqual([]);
  });

  it('面板标题用服务给的 `window_label`，界面不自己拼', () => {
    expect(summaryResultView(answer()).title).toBe('刚才的 15 分钟');
    expect(summaryResultView(answer({ window_label: '整场' })).title).toBe('刚才的 整场');
  });

  it('四格报告用的数都是服务给的原文', () => {
    const view = summaryResultView(answer());
    expect(view.window).toBe('15m');
    expect(view.windowLabel).toBe('15 分钟');
    expect(view.at).toBe(T('21:03:00'));
    expect(view.elapsedSeconds).toBe(9.4);
    expect(view.lines).toBe(37);
  });

  it('三份都空 → 给空态那一句并说明这个窗口里没有可用的转写内容', () => {
    const empty = answer({ what: [], where: [], lost: [], lines: 0 });
    expect(summaryEmptyNote(empty)).toBe(SUMMARY_EMPTY_NOTE);
    expect(SUMMARY_EMPTY_NOTE).toBe('这个窗口里没有可用的转写内容。');
    const view = summaryResultView(empty);
    expect(view.emptyNote).toBe(SUMMARY_EMPTY_NOTE);
  });

  it('只要有一段有内容就不写空态', () => {
    expect(summaryEmptyNote(answer({ what: [], where: [], lost: ['有一处缺口'] }))).toBeNull();
    expect(summaryResultView(answer()).emptyNote).toBeNull();
  });
});

describe('五档失败各画一遍（按 code + detail 的键，不看 message）', () => {
  it('not_recording → 沿用既有的失败画法（服务那句话端出来）', () => {
    const view = summaryFailureView(
      failure('not_recording', {}, '现在没有在录的场次，总结动作无效'),
      0.2,
    );
    expect(view.text).toBe('现在没有在录的场次，总结动作无效');
    expect(view.lines).toEqual([]);
  });

  it('invalid_setting 且 key 是 window → 按合法取值数组说', () => {
    const view = summaryFailureView(
      failure('invalid_setting', {
        key: 'window',
        value: '90m',
        legal: ['5m', '15m', '30m', 'full'],
      }),
      0.1,
    );
    expect(view.text).toBe('窗口档位不合法。');
    expect(view.lines.join(' ')).toContain('90m');
    expect(view.lines.join(' ')).toContain('5m / 15m / 30m / full');
  });

  it('transcript_unavailable → 转写停了，总结没有材料可读', () => {
    const view = summaryFailureView(failure('transcript_unavailable', { health: 'stopped' }), 0.1);
    expect(view.text).toBe('转写停了，总结没有材料可读');
    expect(view.lines).toEqual([]);
  });

  it('summary_failed 且 missing 在 → 点名缺哪几个配置键', () => {
    const view = summaryFailureView(
      failure('summary_failed', { missing: ['summary.base_url', 'summary.api_key'] }),
      0.1,
    );
    expect(view.text).toContain('模型端点没配齐');
    expect(view.lines.join(' ')).toContain('summary.base_url、summary.api_key');
  });

  it('summary_failed 且 elapsed_seconds 在 → 超时那句，N 取四舍五入', () => {
    const view = summaryFailureView(
      failure('summary_failed', { elapsed_seconds: 25.0, attempts: 3 }),
      0.1,
    );
    expect(view.text).toBe('超时了，等了 25 秒还没有结果。再按一次。');
    expect(view.lines).toEqual([]);
    // 四舍五入：24.6 → 25、24.4 → 24
    expect(
      summaryFailureView(failure('summary_failed', { elapsed_seconds: 24.6 }), 0).text,
    ).toBe('超时了，等了 25 秒还没有结果。再按一次。');
    expect(
      summaryFailureView(failure('summary_failed', { elapsed_seconds: 24.4 }), 0).text,
    ).toBe('超时了，等了 24 秒还没有结果。再按一次。');
  });

  it('画法与 `message` 无关：换成一句无关的话，五档一个字都不变', () => {
    const cases: [string, Record<string, unknown>][] = [
      ['not_recording', {}],
      ['invalid_setting', { key: 'window', value: '90m', legal: ['5m'] }],
      ['transcript_unavailable', { health: 'stopped' }],
      ['summary_failed', { missing: ['summary.model'] }],
      ['summary_failed', { elapsed_seconds: 12.0, attempts: 2 }],
    ];
    for (const [code, fields] of cases) {
      const plain = summaryFailureView(failure(code, fields, '原话'), 3);
      const noise = summaryFailureView(failure(code, fields, '完全不相关的一句话！'), 3);
      // 唯一的例外是 `not_recording`：设计口径就是「沿用既有的失败画法」，那一档本来
      // 就用服务那句话。其余四档必须逐字相同。
      if (code === 'not_recording') {
        expect(noise.text).toBe('完全不相关的一句话！');
        continue;
      }
      expect(noise, `${code} 不该看 message`).toEqual(plain);
    }
  });

  it('`invalid_setting` 的其他档不属于总结这条路 → 落到兜底，不冒充窗口那一档', () => {
    const view = summaryFailureView(failure('invalid_setting', { key: 'asr.timeout_seconds' }), 0.1);
    expect(view.text).not.toBe('窗口档位不合法。');
  });

  it('界面自己兜的那一档：秒数取客户端实测的', () => {
    const view = summaryFailureView(
      { code: SUMMARY_CLIENT_TIMEOUT, message: '', fields: { elapsed_seconds: 30.4 } },
      30.4,
    );
    expect(view.text).toBe('超时了，等了 30 秒还没有结果。再按一次。');
  });

  it('认不出的 code → 兜底画法，把服务给的原话端出来', () => {
    const view = summaryFailureView(
      { code: '', message: '', fields: { subject: '动作应答形状不符合契约' } },
      null,
    );
    expect(view.text).toContain('动作应答形状不符合契约');
  });
});

describe('客户端这一侧收到的东西 → 失败那一档', () => {
  it('带 code 的错误原样收下（code 与 detail 都是服务的原文）', () => {
    const view = clientFailureOf({
      code: 'summary_failed',
      message: '预算用尽',
      detail: { elapsed_seconds: 25.0, attempts: 3 },
    });
    expect(view.code).toBe('summary_failed');
    expect(view.message).toBe('预算用尽');
    expect(view.fields).toEqual({ elapsed_seconds: 25.0, attempts: 3 });
  });

  it('不带 code 的错误不冒充服务侧的档：code 留空，原话进 subject', () => {
    const view = clientFailureOf(new Error('连接断了'));
    expect(view.code).toBe('');
    expect(view.fields['subject']).toBe('连接断了');
  });
});

describe('一次请求的往返（含客户端那条 30 秒兜底线）', () => {
  /** 现成的时钟：用例自己往前拨，不读系统时间。 */
  function clock(): { now: () => number; advance: (ms: number) => void } {
    let at = 0;
    return {
      now: () => at,
      advance: (ms: number) => {
        at += ms;
      },
    };
  }

  it('成功：把服务给的应答原文交回来，耗时是客户端自己量的', async () => {
    const tick = clock();
    const outcome = await runSummaryExchange('15m', {
      request: async () => {
        tick.advance(9400);
        return view();
      },
      now: tick.now,
      timeoutSeconds: 30,
    });
    expect(outcome.kind).toBe('ok');
    if (outcome.kind !== 'ok') throw new Error('该是成功那一档');
    expect(outcome.result.windowLabel).toBe('15 分钟');
    expect(outcome.elapsedSeconds).toBeCloseTo(9.4, 3);
  });

  it('服务回失败信封：code 与 detail 原文照收', async () => {
    const tick = clock();
    const outcome = await runSummaryExchange('5m', {
      request: async () => {
        tick.advance(100);
        throw { code: 'transcript_unavailable', message: '转写没在跑', detail: { health: 'stopped' } };
      },
      now: tick.now,
      timeoutSeconds: 30,
    });
    expect(outcome.kind).toBe('failure');
    if (outcome.kind !== 'failure') throw new Error('该是失败那一档');
    expect(outcome.failure.code).toBe('transcript_unavailable');
    expect(outcome.failure.fields).toEqual({ health: 'stopped' });
  });

  it('拖过兜底线还没回 → 按超时那一档收，秒数由客户端实测', async () => {
    const tick = clock();
    const gate = held();
    const outcome = await runSummaryExchange('15m', {
      request: gate.request,
      now: tick.now,
      // 用例把兜底线压到 30 毫秒：量的还是「过线就收」这件事，墙钟上只等一点点
      timeoutSeconds: 0.03,
    });
    expect(outcome.kind).toBe('timeout');
    if (outcome.kind !== 'timeout') throw new Error('该是兜底那一档');
    expect(outcome.elapsedSeconds).toBeGreaterThanOrEqual(0);
    // 放掉那个迟到的应答：它不该把已经收好的结果改写掉
    gate.release();
    await Promise.resolve();
  });

  it('兜底线还没到就不宣布失败：应答一到就按成功收', async () => {
    const gate = held();
    const pending = runSummaryExchange('15m', {
      request: gate.request,
      now: () => 0,
      timeoutSeconds: 5,
    });
    gate.release();
    const outcome = await pending;
    expect(outcome.kind).toBe('ok');
  });
});

describe('本场总结记录', () => {
  function record(index: number, over: Record<string, unknown> = {}) {
    return {
      window: '15m',
      window_label: '15 分钟',
      at: T(index % 2 === 0 ? '20:41:00' : '21:03:00'),
      elapsed_seconds: 9.4,
      what: [`第 ${index} 次的讲什么`],
      where: [`第 ${index} 次的讲到哪`],
      lost: [],
      ...over,
    };
  }

  it('没按过时入口写 `（还没有）`，展开给空态那句', () => {
    const view = summaryLogView([]);
    expect(view.entryLabel).toBe('本场总结记录（还没有）');
    expect(view.title).toBe('本场总结记录（还没有）');
    expect(view.count).toBe(0);
    expect(view.emptyNote).toBe(SUMMARY_LOG_EMPTY);
    expect(SUMMARY_LOG_EMPTY).toBe('这一场还没总结过。');
    expect(view.entries).toEqual([]);
  });

  it('入口那一行：还没取过服务那份时写「还没有」，取回 0 条也是同一句', () => {
    expect(summaryLogLabel(null)).toBe('本场总结记录（还没有）');
    expect(summaryLogLabel(0)).toBe('本场总结记录（还没有）');
    expect(summaryLogLabel(1)).toBe('本场总结记录（1 次）');
    expect(summaryLogLabel(3)).toBe('本场总结记录（3 次）');
  });

  it('按过几次由**服务给的条目数**说话', () => {
    const view = summaryLogView([record(0), record(1), record(2)]);
    expect(view.entryLabel).toBe('本场总结记录（3 次）');
    expect(view.title).toBe('本场总结记录（3 次）');
    expect(view.count).toBe(3);
    expect(view.emptyNote).toBeNull();
  });

  it('每条：时刻按分钟级、窗口用服务给的标签、三小段按固定次序列', () => {
    const view = summaryLogView([record(1, { window_label: '整场' })]);
    const entry = view.entries[0];
    expect(entry?.time).toBe(formatWallClock(T('21:03:00')));
    expect(entry?.time).toMatch(/^\d{2}:\d{2}$/);
    expect(entry?.windowLabel).toBe('整场');
    expect(entry?.elapsedSeconds).toBe(9.4);
    expect(entry?.sections.map((section) => section.title)).toEqual([...SUMMARY_SECTION_TITLES]);
    expect(entry?.sections[0]?.items).toEqual(['第 1 次的讲什么']);
    expect(entry?.sections[1]?.items).toEqual(['第 1 次的讲到哪']);
    expect(entry?.sections[2]?.items).toEqual([]);
  });

  it('条目顺序照服务给的先后（界面不重排）', () => {
    const view = summaryLogView([record(0), record(1), record(2)]);
    expect(view.entries.map((entry) => entry.key)).toEqual([
      `0-${T('20:41:00')}`,
      `1-${T('21:03:00')}`,
      `2-${T('20:41:00')}`,
    ]);
  });

  it('某一条三份都空时，那条自己也给空态那句', () => {
    const view = summaryLogView([record(0, { what: [], where: [], lost: [] })]);
    expect(view.entries[0]?.emptyNote).toBe(SUMMARY_EMPTY_NOTE);
  });
});
