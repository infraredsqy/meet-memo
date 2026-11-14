import {
  HEARTBEAT_INTERVAL_DEFAULT_SECONDS,
  HEARTBEAT_INTERVAL_MAX_SECONDS,
  HEARTBEAT_INTERVAL_MIN_SECONDS,
  heartbeatSchema,
  type Heartbeat,
} from '../contract';
import { createPollerLoop } from './polling';

/**
 * 心跳轮询与三态判定。
 *
 * **周期从哪来**：初值取快照的 `settings.heartbeat_interval_seconds`，clamp 到 1–60 秒，
 * 拿不到快照时用默认值。界面**不自造第二份配置源**，也不把周期存进浏览器本地存储。
 *
 * **三态口径**：「时间差」指的是**应答里的 `server_time` 与本地时钟的时间差** —— 拿本地两次
 * 请求的间隔去算会漏掉「服务还活着、录音线程已卡死」这一类。
 *
 * `已恢复` 不在本模块的三态里：它是失联之后由界面留下的那条短记录。
 */

export const HEARTBEAT_URL = '/api/heartbeat';

export type HeartbeatStatus = '正常' | '迟到' | '失联';

/** 把任意来源的周期夹进 1–60 秒；不是有限正数就用默认值。 */
export function clampHeartbeatInterval(seconds: unknown): number {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds <= 0) {
    return HEARTBEAT_INTERVAL_DEFAULT_SECONDS;
  }
  return Math.min(
    HEARTBEAT_INTERVAL_MAX_SECONDS,
    Math.max(HEARTBEAT_INTERVAL_MIN_SECONDS, seconds),
);
}

/**
 * 按 `server_time` 与本地时钟的差判三态。
 *
 * `intervalSeconds` 是**界面发心跳的周期**，与 `recording_alive` 的判据窗口没有关系，两者不串线。
 */
export function judgeHeartbeat(
  serverTime: string,
  intervalSeconds: number,
  nowMs: number = Date.now(),
): HeartbeatStatus {
  const periodMs = clampHeartbeatInterval(intervalSeconds) * 1000;
  const serverMs = Date.parse(serverTime);
  if (Number.isNaN(serverMs)) {
    // 时间戳读不出来 —— 没法算差，按最严重的那一档处理，别把它当正常。
    return '失联';
  }
  const lagMs = nowMs - serverMs;

  if (lagMs < periodMs) return '正常';
  if (lagMs < periodMs * 2) return '迟到';
  return '失联';
}

/** `心跳迟到 · 8 秒没收到应答` —— 迟到档那一行字的正文。 */
export function lateHeartbeatText(lagSeconds: number): string {
  return `心跳迟到 · ${Math.round(lagSeconds)} 秒没收到应答`;
}

export interface HeartbeatClientOptions {
  fetchImpl?: typeof fetch;
  url?: string;
  now?: () => number;
}

export type HeartbeatTick =
  | { kind: 'reply'; heartbeat: Heartbeat; status: HeartbeatStatus; lagSeconds: number }
  | { kind: 'error'; error: Error; status: HeartbeatStatus };

/**
 * 可配周期的心跳轮询：排程骨架（`setTimeout` 自我重排、周期即改即生效）与单场状态轮询共用
 * 同一份（`api/polling.ts`）—— 这边只管「问什么、读数交给谁」。
 */
export interface HeartbeatClient {
  setIntervalFromSnapshot(seconds: number): void;
  intervalSeconds(): number;
  tick(): Promise<HeartbeatTick>;
  start(): void;
  stop(): void;
  running(): boolean;
}

export function createHeartbeatClient(
  onTick: (tick: HeartbeatTick) => void,
  options: HeartbeatClientOptions = {},
): HeartbeatClient {
  const doFetch = options.fetchImpl ?? fetch;
  const now = options.now ?? Date.now;
  const url = options.url ?? HEARTBEAT_URL;

  const tick = async (): Promise<HeartbeatTick> => {
    try {
      const response = await doFetch(url, { headers: { Accept: 'application/json' } });
      if (!response.ok) {
        return {
          kind: 'error',
          error: new Error(`心跳应答 HTTP ${response.status}`),
          status: '失联',
        };
      }
      const parsed = heartbeatSchema.safeParse(await response.json());
      if (!parsed.success) {
        return {
          kind: 'error',
          error: new Error(`心跳应答形状不符合契约：${parsed.error.message}`),
          status: '失联',
        };
      }
      const status = judgeHeartbeat(parsed.data.server_time, loop.intervalSeconds(), now());
      const lagSeconds = (now() - Date.parse(parsed.data.server_time)) / 1000;
      return { kind: 'reply', heartbeat: parsed.data, status, lagSeconds };
    } catch (cause) {
      return {
        kind: 'error',
        error: cause instanceof Error ? cause : new Error(String(cause)),
        status: '失联',
      };
    }
  };

  const loop = createPollerLoop({
    defaultSeconds: HEARTBEAT_INTERVAL_DEFAULT_SECONDS,
    clamp: clampHeartbeatInterval,
    beat: async () => {
      const result = await tick();
      onTick(result);
    },
  });

  return {
    setIntervalFromSnapshot(seconds: number): void {
      loop.setIntervalSeconds(seconds);
    },

    intervalSeconds(): number {
      return loop.intervalSeconds();
    },

    async tick(): Promise<HeartbeatTick> {
      const result = await tick();
      onTick(result);
      return result;
    },

    start(): void {
      loop.start();
    },

    stop(): void {
      loop.stop();
    },

    running(): boolean {
      return loop.running();
    },
  };
}
