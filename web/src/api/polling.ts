/**
 * 自排轮询的公共骨架（web-poller-scaffolding-duplicated）：`setTimeout` 自我重排
 * —— 周期改了即时生效，不必等当前这一轮走完。
 *
 * 心跳（`api/heartbeat.ts` 的 `createHeartbeatClient`）与单场状态轮询
 * （`api/sessions.ts` 的 `createSessionStatusPoller`）共用这一份；问什么、读数交给谁，
 * 由各自的 `beat` 回调定 —— 骨架只管「什么时候问、什么时候停」。
 */

/** 轮询骨架露给端点的那几样（start / stop / running / 周期）。 */
export interface PollerLoop {
  /** 设周期（调用方先 clamp）；变了就重排下一次。 */
  setIntervalSeconds(seconds: number): void;
  /** 当前周期（秒）。 */
  intervalSeconds(): number;
  /** 开始轮询；已在轮询时不动（单场轮询先换目标再调这一下）。 */
  start(): void;
  /** 停止轮询。 */
  stop(): void;
  /** 是否在轮询。 */
  running(): boolean;
}

export function createPollerLoop(io: {
  /** 周期初值（秒）：拿不到快照时的默认。 */
  defaultSeconds: number;
  /** 周期的 clamp（心跳与单场状态同一把尺：1–60 秒，非正数落默认）。 */
  clamp: (seconds: number) => number;
  /** 一跳：问一次、读数交回调。排下一次是骨架的活。 */
  beat: () => Promise<void>;
  /** `stopped` 之外的那道闸（单场轮询：有目标才排）；缺省恒可排。 */
  ready?: () => boolean;
}): PollerLoop {
  let interval = io.defaultSeconds;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let stopped = true;

  const schedule = (): void => {
    if (stopped) return;
    if (io.ready?.() === false) return;
    timer = setTimeout(() => {
      // 这一句不能少：beat() 是一次性的，排下一次是这里的活儿。
      // 少了它整套轮询只发一次请求 —— 心跳那边的迟到与失联两档就永远不会出现。
      void io.beat().then(() => schedule());
    }, interval * 1000);
  };

  return {
    setIntervalSeconds(seconds: number): void {
      const next = io.clamp(seconds);
      if (next === interval) return;
      interval = next;
      // 周期变了就重排下一次 —— 否则旧周期要等走完才换。
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
        schedule();
      }
    },

    intervalSeconds(): number {
      return interval;
    },

    start(): void {
      if (!stopped) return;
      stopped = false;
      schedule();
    },

    stop(): void {
      stopped = true;
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    },

    running(): boolean {
      return !stopped;
    },
  };
}
