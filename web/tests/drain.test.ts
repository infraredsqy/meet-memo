import { describe, expect, it } from 'vitest';
import { drainStripView, stepsSettled } from '../src/home/drain';
import type { Ended } from '../src/contract';

/**
 * 工作台详情里那一小块「落盘进度」的纯逻辑用例（`home/drain.ts`）。
 *
 * 只锁不变量：两句话的判据（「还差最后一块没进盘」/「录音已落盘」）、失败档绝不
 * 说「已落盘」、三步明细各自的人话。判据本体是 `stepsSettled()`，闸（`gate.ts`）
 * 与这一小块共用同一把尺 —— 那条也在这一组里钉住。
 */

/** 造一份 `ended`（`Ended` 的形状，逐键照 `docs/contract.md` 第 2 章）。 */
function ended(over: {
  phase?: 'draining' | 'done';
  error?: string | null;
  steps?: Array<{ id: string; status: string; error?: string | null }>;
} = {}): Ended {
  const steps = over.steps ?? [
    { id: 'flush', status: 'done' },
    { id: 'log', status: 'done' },
    { id: 'post', status: 'waiting' },
  ];
  return {
    phase: over.phase ?? 'done',
    error: over.error ?? null,
    steps: steps.map((step) => ({
      id: step.id,
      status: step.status,
      detail: '',
      error: step.error ?? null,
    })) as Ended['steps'],
  };
}

describe('落盘那一小块的两句话（ui-design 4.2 二）', () => {
  it('收尾还在跑：「还差最后一块没进盘」——flush 未落定也照实说', () => {
    const view = drainStripView(ended({
      phase: 'draining',
      steps: [
        { id: 'flush', status: 'running' },
        { id: 'log', status: 'waiting' },
        { id: 'post', status: 'waiting' },
      ],
    }));
    expect(view.headline).toBe('还差最后一块没进盘');
    expect(view.settled).toBe(false);
    expect(view.failed).toBe(false);
  });

  it('收尾落定（post 停在 waiting 也算）：「录音已落盘」', () => {
    const view = drainStripView(ended({ phase: 'done' }));
    expect(view.headline).toBe('录音已落盘');
    expect(view.settled).toBe(true);
    expect(view.steps.map((step) => `${step.label}：${step.status}`)).toEqual([
      '落盘：完成',
      '日志：完成',
      '启动会后：等待',
    ]);
  });

  it('失败档：绝不出现「已落盘」，原因、命令与 log 的料一样不缺', () => {
    const view = drainStripView(ended({
      phase: 'draining',
      error: null,
      steps: [
        { id: 'flush', status: 'failed', error: '远端这条音频没写进盘：磁盘只剩 120 MB' },
        { id: 'log', status: 'done' },
        { id: 'post', status: 'waiting' },
      ],
    }));
    expect(view.headline).not.toContain('已落盘');
    expect(view.headline).toBe('收尾出事了，先别关机');
    expect(view.failed).toBe(true);
    expect(view.failures).toEqual(['落盘：远端这条音频没写进盘：磁盘只剩 120 MB']);
  });

  it('收尾自己的错误一行（ended.error）也算「出事」，不冒「已落盘」', () => {
    const view = drainStripView(ended({ phase: 'draining', error: 'log 写不下' }));
    expect(view.headline).toBe('收尾出事了，先别关机');
    expect(view.error).toBe('log 写不下');
  });

  it('闸与这一小块共用一把尺：stepsSettled 的四档就是 drainSettled 的四档', () => {
    expect(stepsSettled(ended({ phase: 'done' }).steps)).toBe(true);
    expect(
      stepsSettled(ended({ steps: [
        { id: 'flush', status: 'skipped' },
        { id: 'log', status: 'skipped' },
        { id: 'post', status: 'running' },
      ] }).steps),
).toBe(false);
    expect(stepsSettled([])).toBe(true);
  });
});
