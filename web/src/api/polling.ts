/**
 * `setTimeout` 自我重排 —— 周期改了即时生效，不必等当前这一轮走完。
 */

export interface PollerLoop {
  setIntervalSeconds(seconds: number): void;
  intervalSeconds(): number;
  start(): void;
  stop(): void;
  running(): boolean;
}

export function createPollerLoop(io: {
  defaultSeconds: number;
  /** 心跳与单场状态同一把尺：1–60 秒，非正数落默认。 */
  clamp: (seconds: number) => number;
  beat: () => Promise<void>;
  ready?: () => boolean;
}): PollerLoop {
  let interval = io.defaultSeconds;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let stopped = true;

  const schedule = (): void => {
    if (stopped) return;
    if (io.ready?.() === false) return;
    timer = setTimeout(() => {
      // 这一句不能少：少了它整套轮询只发一次请求 —— 心跳那边的迟到与失联两档就永远不会出现。
      void io.beat().then(() => schedule());
    }, interval * 1000);
  };

  return {
    setIntervalSeconds(seconds: number): void {
      const next = io.clamp(seconds);
      if (next === interval) return;
      interval = next;
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
