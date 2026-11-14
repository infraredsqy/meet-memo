import type { DrainStep, Ended } from '../contract';

export interface DrainStepView {
  /** 步骤名（`落盘` / `日志` / `启动会后`；契约外的 id 原样带过，不编）。 */
  label: string;
  status: string;
  error: string | null;
}

export interface DrainStripView {
  headline: string;
  settled: boolean;
  failed: boolean;
  steps: DrainStepView[];
  failures: string[];
  error: string | null;
}

/**
 * `waiting` 只许落在 `post` 那一格上（它是这一格的终态 —— 会后不自动跑，
 * 停在那里等人按工作台上那两个按钮）。
 */
export function stepsSettled(steps: readonly DrainStep[]): boolean {
  if (steps.some((step) => step.status === 'running')) return false;
  return steps.every((step) => step.status !== 'waiting' || step.id === 'post');
}

const STEP_LABELS: Readonly<Record<string, string>> = {
  flush: '落盘',
  log: '日志',
  post: '启动会后',
  transcode: '转码',
};

const STEP_STATUS: Readonly<Record<string, string>> = {
  waiting: '等待',
  running: '进行中',
  done: '完成',
  failed: '失败',
  skipped: '跳过',
};

export function drainStripView(ended: Ended): DrainStripView {
  const steps = ended.steps.map<DrainStepView>((step) => ({
    label: STEP_LABELS[step.id] ?? step.id,
    status: STEP_STATUS[step.status] ?? step.status,
    error: step.error,
  }));
  const failed = ended.steps.some((step) => step.status === 'failed');
  const settled = stepsSettled(ended.steps);
  return {
    headline:
      failed || ended.error !== null
        ? '收尾出事了，先别关机'
        : settled
          ? '录音已落盘'
          : '还差最后一块没进盘',
    settled,
    failed,
    steps,
    failures: steps
      .filter((step) => step.error !== null)
      .map((step) => `${step.label}：${step.error}`),
    error: ended.error,
  };
}
