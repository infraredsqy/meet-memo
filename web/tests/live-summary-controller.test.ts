import { describe, expect, it } from 'vitest';

import { SUMMARY_CLIENT_TIMEOUT } from '../src/live/summary';
import {
  createSummaryController,
  type SummaryAnswer,
  type SummaryRecord,
} from '../src/live/summary-controller';
import { T } from './live-fixtures';

/**
 * 总结区那台请求状态机（`live/summary-controller.ts`）。
 *
 * 三条在这一层才说得清的东西：**请求状态与档位**、**客户端那条兜底线**、
 * **迟到的应答不许覆盖已经画出来的东西**。记录那一路（取不到时保留手上这份）也在这里。
 *
 * 注入件全是函数：动作端点、记录端点、时钟、兜底秒数 —— 一条网络请求都不发。
 */

function answer(over: Partial<SummaryAnswer> = {}): SummaryAnswer {
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

function record(over: Partial<SummaryRecord> = {}): SummaryRecord {
  return {
    window: '15m',
    window_label: '15 分钟',
    at: T('21:03:00'),
    elapsed_seconds: 9.4,
    what: ['在过下周的排期'],
    where: ['说到第三项'],
    lost: ['有一处识别失败的缺口'],
    ...over,
  };
}

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

/** 让 `void` 发出去的那些取数把微任务走完（不读系统时间，只让出一轮事件循环）。 */
async function settle(): Promise<void> {
  for (let round = 0; round < 5; round += 1) {
    await Promise.resolve();
  }
}

/**
 * 一个「挂住不回」的应答口子：用例握住 `release`，想什么时候放行就什么时候放行。
 *
 * 兜底那一档量的就是「到点了服务那边一句话都没回」，用事件放行比定时器干净，
 * 也不随机器快慢飘。
 */
function held(): {
  request: () => Promise<SummaryAnswer>;
  release: () => void;
} {
  let resolveHeld!: (value: SummaryAnswer) => void;
  const promise = new Promise<SummaryAnswer>((resolve) => {
    resolveHeld = resolve;
  });
  return {
    request: () => promise,
    release: () => resolveHeld(answer()),
  };
}

/** 记录那一路的同款口子：取的时候挂住，放行之后交回一条记录。 */
function heldLog(): { after: () => Promise<SummaryRecord[]>; release: () => void } {
  let resolveHeld!: (value: SummaryRecord[]) => void;
  const promise = new Promise<SummaryRecord[]>((resolve) => {
    resolveHeld = resolve;
  });
  return {
    after: () => promise,
    release: () => resolveHeld([record()]),
  };
}

describe('一次总结的状态迁移', () => {
  it('初值：闲、缺省 15 分钟、没有结果也没有失败', () => {
    const controller = createSummaryController({
      request: async () => answer(),
      loadLog: async () => [],
    });
    const state = controller.state();
    expect(state.phase).toBe('idle');
    expect(state.window).toBe('15m');
    expect(state.result).toBeNull();
    expect(state.failure).toBeNull();
  });

  it('成功：进「有结果」那一档，三小段与标题都摆好', async () => {
    const controller = createSummaryController({
      request: async () => answer(),
      loadLog: async () => [],
      now: () => 0,
    });
    await controller.run();
    const state = controller.state();
    expect(state.phase).toBe('done');
    expect(state.result?.title).toBe('刚才的 15 分钟');
    expect(state.result?.sections.map((section) => section.title)).toEqual([
      '刚才在讲什么',
      '讲到哪儿了',
      '丢掉了什么',
    ]);
    expect(state.failure).toBeNull();
  });

  it('请求里带的是当前选中的档位', async () => {
    const seen: string[] = [];
    const controller = createSummaryController({
      request: async (window) => {
        seen.push(window);
        return answer({ window, window_label: '整场' });
      },
      loadLog: async () => [],
    });
    controller.select('full');
    await controller.run();
    expect(seen).toEqual(['full']);
    expect(controller.state().result?.title).toBe('刚才的 整场');
  });

  it('失败：进「失败」那一档，code 与 detail 原文照收', async () => {
    const controller = createSummaryController({
      request: async () => {
        throw { code: 'transcript_unavailable', message: '转写没在跑', detail: { health: 'stopped' } };
      },
      loadLog: async () => [],
    });
    await controller.run();
    const state = controller.state();
    expect(state.phase).toBe('failure');
    expect(state.failure?.code).toBe('transcript_unavailable');
    expect(state.result).toBeNull();
  });

  it('再按一次：上一次的结果与失败一起清掉（面板画的是这一次）', async () => {
    let round = 0;
    const controller = createSummaryController({
      request: async () => {
        round += 1;
        if (round === 1) return answer();
        throw { code: 'not_recording', message: '现在没有在录的场次', detail: {} };
      },
      loadLog: async () => [],
    });
    await controller.run();
    expect(controller.state().phase).toBe('done');
    await controller.run();
    expect(controller.state().phase).toBe('failure');
    expect(controller.state().result).toBeNull();
  });

  it('关掉面板：回到闲，结果与失败都不留', async () => {
    const controller = createSummaryController({
      request: async () => answer(),
      loadLog: async () => [],
    });
    await controller.run();
    controller.close();
    const state = controller.state();
    expect(state.phase).toBe('idle');
    expect(state.result).toBeNull();
    expect(state.failure).toBeNull();
  });
});

describe('客户端那条兜底线', () => {
  it('拖过线还没回 → 按超时那一档收，代号是界面自己的那一个', async () => {
    const gate = held();
    const controller = createSummaryController({
      request: gate.request,
      loadLog: async () => [],
      timeoutSeconds: 0.03,
    });
    await controller.run();
    const state = controller.state();
    expect(state.phase).toBe('failure');
    expect(state.failure?.code).toBe(SUMMARY_CLIENT_TIMEOUT);
    expect(state.elapsedSeconds).not.toBeNull();
  });

  it('还没到线就一直画进行中：这时不许有失败', async () => {
    const gate = held();
    const controller = createSummaryController({
      request: gate.request,
      loadLog: async () => [],
      timeoutSeconds: 5,
    });
    const running = controller.run();
    expect(controller.state().phase).toBe('running');
    expect(controller.state().failure).toBeNull();
    gate.release();
    await running;
    expect(controller.state().phase).toBe('done');
  });

  it('迟到的应答不许覆盖兜底那一档：画面停在超时上', async () => {
    const gate = held();
    const controller = createSummaryController({
      request: gate.request,
      loadLog: async () => [],
      timeoutSeconds: 0.03,
    });
    await controller.run();
    expect(controller.state().phase).toBe('failure');
    gate.release();
    // 让那一趟迟到的应答把微任务走完
    await Promise.resolve();
    await Promise.resolve();
    expect(controller.state().phase).toBe('failure');
    expect(controller.state().failure?.code).toBe(SUMMARY_CLIENT_TIMEOUT);
  });
});

describe('本场总结记录那一路', () => {
  it('取到之后入口写着次数、条目按服务给的先后排', async () => {
    const controller = createSummaryController({
      request: async () => answer(),
      loadLog: async () => [record({ at: T('20:41:00') }), record({ at: T('21:03:00') })],
    });
    expect(controller.log()).toBeNull();
    expect(controller.logCount()).toBeNull();
    await controller.loadRecords();
    expect(controller.log()?.entryLabel).toBe('本场总结记录（2 次）');
    expect(controller.logCount()).toBe(2);
    expect(controller.log()?.entries.map((entry) => entry.time)).toEqual([
      '20:41',
      '21:03',
    ]);
    expect(controller.logError()).toBeNull();
  });

  it('一条都没有：入口写 `（还没有）`，展开给空态那句', async () => {
    const controller = createSummaryController({
      request: async () => answer(),
      loadLog: async () => [],
    });
    await controller.loadRecords();
    expect(controller.log()?.entryLabel).toBe('本场总结记录（还没有）');
    expect(controller.logCount()).toBe(0);
    expect(controller.log()?.emptyNote).toBe('这一场还没总结过。');
  });

  it('取不到：手上那份旧的留着，另给一行说明（不冒充「还没总结过」）', async () => {
    let round = 0;
    const controller = createSummaryController({
      request: async () => answer(),
      loadLog: async () => {
        round += 1;
        if (round === 1) return [record()];
        throw new Error('连接断了');
      },
    });
    await controller.loadRecords();
    expect(controller.log()?.count).toBe(1);
    await controller.loadRecords();
    // 旧的那一份还在（一行都没被清掉）
    expect(controller.log()?.count).toBe(1);
    expect(controller.logError()).toContain('连接断了');
  });

  /**
   * 本场**第一次**展开就取不到那一档（修复 m2 钉住的那条口径）。
   *
   * 判据不是「面板该不该出」—— 那是组件里那一行 JSX 的事（`web/tests` 是 node 环境，
   * 按边界第 2 条不写 DOM 用例）。这一条钉的是**判据那一半的来源**：面板要照常出现，
   * 靠的是「手上那份是空的（`log() === null`）**而说明在**（`logError() !== null`）」
   * 这一个组合确实成立。旧行为下 `log()` 恒为 `null` 且组件只看它，于是面板整块不出现、
   * 那句说明够不着；带 DOM 的那一条走无头浏览器探针（`--only happy` 的「取数失败那次展开」）。
   */
  it('本场第一次展开就取不到：手上那份是空的，说明必须在（面板靠它照常出现）', async () => {
    const controller = createSummaryController({
      request: async () => answer(),
      loadLog: async () => {
        throw new Error('连接断了');
      },
    });
    // 「点开」那一下是组件干的：面板开着时手上没有服务那份，于是发这一次 GET
    controller.setLogOpen(true);
    expect(controller.log()).toBeNull();
    await controller.loadRecords();
    // 一路失败之后**仍然**没有服务给的那一份 —— 光看它是看不出「面板该出」的……
    expect(controller.log()).toBeNull();
    expect(controller.logCount()).toBeNull();
    // ……但说明在，组件那两样取或的判据因此成立
    expect(controller.logError()).toContain('连接断了');
  });

  it('正在取的时候不重复发（连按两下只打一次）', async () => {
    let calls = 0;
    const gate = heldLog();
    const controller = createSummaryController({
      request: async () => answer(),
      loadLog: async () => {
        calls += 1;
        return gate.after();
      },
    });
    const first = controller.loadRecords();
    const second = controller.loadRecords();
    gate.release();
    await Promise.all([first, second]);
    expect(calls).toBe(1);
    expect(controller.log()?.count).toBe(1);
  });

  it('面板开着的时候，成功这一次顺手把列表刷新一遍', async () => {
    let round = 0;
    const controller = createSummaryController({
      request: async () => answer(),
      loadLog: async () => {
        round += 1;
        return round === 1 ? [] : [record()];
      },
    });
    controller.setLogOpen(true);
    await controller.loadRecords();
    expect(controller.log()?.count).toBe(0);
    await controller.run();
    // 刷新是 `void` 发出去的，等它把微任务走完
    await Promise.resolve();
    await Promise.resolve();
    expect(controller.log()?.count).toBe(1);
  });

  it('成功之后入口那一行的次数跟着服务走（面板关着也取一次）', async () => {
    let round = 0;
    const controller = createSummaryController({
      request: async () => answer(),
      loadLog: async () => {
        round += 1;
        // 服务侧：第一次取时还没有记录，之后每一次总结成功都多一条
        return round === 1 ? [] : [record()];
      },
    });
    controller.setLogOpen(false);
    expect(controller.logCount()).toBeNull();
    // 先取一次（服务那边此刻 0 条）—— 入口那一行写「还没有」
    await controller.loadRecords();
    expect(controller.logCount()).toBe(0);
    await controller.run();
    await settle();
    // 那一次成功的总结之后，服务那边有了一条 —— 界面把这一条取回来
    expect(controller.logCount()).toBe(1);
    expect(controller.log()?.entryLabel).toBe('本场总结记录（1 次）');
  });
});

describe('订阅', () => {
  it('状态一变就叫一次；退订之后不再叫', async () => {
    const controller = createSummaryController({
      request: async () => answer(),
      loadLog: async () => [],
    });
    let calls = 0;
    const off = controller.subscribe(() => {
      calls += 1;
    });
    await controller.run();
    expect(calls).toBeGreaterThanOrEqual(2);
    const before = calls;
    off();
    controller.select('full');
    expect(calls).toBe(before);
  });

  it('选中同一档不算变化，不惊动订阅者', () => {
    const controller = createSummaryController({
      request: async () => answer(),
      loadLog: async () => [],
    });
    let calls = 0;
    controller.subscribe(() => {
      calls += 1;
    });
    controller.select('15m');
    expect(calls).toBe(0);
    controller.select('5m');
    expect(calls).toBe(1);
  });
});
