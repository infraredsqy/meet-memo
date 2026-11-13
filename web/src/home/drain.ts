import type { DrainStep, Ended } from '../contract';

/**
 * 工作台详情里那一小块「落盘进度」的纯逻辑（ui-design 4.2 二）。
 *
 * 停止录音那一刻就回工作台（闸在 `gate.ts`：ended 任意相都回），收尾于是只剩
 * 「录音已结束」之下的一小段进度：它在详情里只占一小块、不阻塞任何东西，
 * 「能不能关机」的两句话只活在这一小块里 —— 收尾还在跑写「还差最后一块没进盘」，
 * 落定写「录音已落盘」。**落盘、转码这类家务不许用告警带或致命色**（同一节口径），
 * 这里的读数只管把字算出来，形状归组件（quiet 的小块，不是 `.alert`）。
 *
 * 判据全部落在服务推来的快照 `ended` 上：`stepsSettled()`（收尾三步都落定，
 * `post` 停在 `waiting` 也算落定 —— 会后一个动作都不自动跑）是唯一的落定线，
 * 闸（`gate.ts` 的 `drainSettled`）与这一小块共用同一把尺；这一层不另立第二把。
 */

/** 收尾一步的界面读法：id 的人话 + 状态的人话。 */
export interface DrainStepView {
  /** 步骤名（`落盘` / `日志` / `启动会后`；契约外的 id 原样带过，不编）。 */
  label: string;
  /** `等待 / 进行中 / 完成 / 失败 / 跳过`。 */
  status: string;
  /** 服务给的失败原话（没失败是 `null`）。 */
  error: string | null;
}

export interface DrainStripView {
  /** 头行那一句：收尾还在跑 / 已落定 / 有失败步，各是各的话。 */
  headline: string;
  /** 落定没有（`stepsSettled()` 的原文；头行与图形都按它分档）。 */
  settled: boolean;
  /** 有失败步没有（失败档如实给原因、命令与 log，绝不说「已落盘」）。 */
  failed: boolean;
  /** 三步（落盘 / 日志 / 启动会后）各自的那一格，一行小字摆完。 */
  steps: DrainStepView[];
  /** 失败明细行（原因原话；没有失败步时是空数组）。 */
  failures: string[];
  /** 收尾自己的错误一行（`ended.error`；没有是 `null`）。 */
  error: string | null;
}

/**
 * 收尾三步是否都落定：没有任何一步在跑，且 `waiting` 只许落在 `post` 那一格上
 * （它是这一格的终态 —— 会后不自动跑，停在那里等人按工作台上那两个按钮）。
 */
export function stepsSettled(steps: readonly DrainStep[]): boolean {
  if (steps.some((step) => step.status === 'running')) return false;
  return steps.every((step) => step.status !== 'waiting' || step.id === 'post');
}

/** 步骤 id → 人话（契约里的三步；看见别的 id 原样带过 —— 界面不编名字）。 */
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

/** 快照 `ended` → 那一小块的读数。组件不解读，这里逐条写判据。 */
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
