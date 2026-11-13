import {
  HEARTBEAT_INTERVAL_DEFAULT_SECONDS,
  HEARTBEAT_INTERVAL_MAX_SECONDS,
  HEARTBEAT_INTERVAL_MIN_SECONDS,
  heartbeatSchema,
  type Heartbeat,
} from '../contract';
import { createPollerLoop } from './polling';

/**
 * 心跳轮询与三态判定（`docs/contract.md` 第 5 章）。
 *
 * **周期从哪来**：初值取快照的 `settings.heartbeat_interval_seconds`，
 * clamp 到 1–60 秒，拿不到快照时用默认 10 秒；快照更新时跟随。
 * 界面**不自造第二份配置源**，也不把周期存进浏览器本地存储（第 6 章第 2 条）。
 *
 * **三态口径**（第 5 章「客户端轮询口径」原文）：
 *
 * | 状态 | 判据 |
 * |---|---|
 * | `正常` | 时间差 < 1 个周期 |
 * | `迟到` | 时间差 ≥ 1 个周期且 < 2 个周期 |
 * | `失联` | 时间差 ≥ 2 个周期 |
 *
 * 「时间差」指的是**应答里的服务侧时间戳（`server_time`）与本地时钟的时间差**，
 * 因此判定以 `server_time` 为基准 —— 拿本地两次请求的间隔去算会漏掉
 * 「服务还活着、录音线程已卡死」这一类。
 *
 * `已恢复` 不在本模块的三态里：它是失联之后回到正常/迟到时由界面留下的那条短记录
 * （30 秒），属于展示层的事。
 */

export const HEARTBEAT_URL = '/api/heartbeat';

export type HeartbeatStatus = '正常' | '迟到' | '失联';

/** 把任意来源的周期夹进 1–60 秒；不是有限正数就用默认 10 秒。 */
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
 * 按 `server_time` 与本地时钟的差判三态（第 5 章口径表）。
 *
 * `intervalSeconds` 是**界面发心跳的周期**，与 `recording_alive` 的判据窗口
 * （服务侧 `max(2.0s, 3 × frame_interval)` = 2 秒）没有关系，两者不串线。
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
  /** 覆盖 fetch（测试注入用）。 */
  fetchImpl?: typeof fetch;
  /** 覆盖端点（测试注入用）。 */
  url?: string;
  /** 覆盖时钟（测试注入用）。 */
  now?: () => number;
}

export type HeartbeatTick =
  | { kind: 'reply'; heartbeat: Heartbeat; status: HeartbeatStatus; lagSeconds: number }
  | { kind: 'error'; error: Error; status: HeartbeatStatus };

/**
 * 可配周期的心跳轮询。排程骨架（setTimeout 自我重排、周期即改即生效）与单场状态
 * 轮询共用同一份（`api/polling.ts`）—— 这边只管「问什么、读数交给谁」。
 *
 * 周期随快照走：`setIntervalFromSnapshot(snapshot.settings.heartbeat_interval_seconds)`。
 */
export interface HeartbeatClient {
  /** 用快照里的 `settings.heartbeat_interval_seconds` 设周期（clamp 1–60）。 */
  setIntervalFromSnapshot(seconds: number): void;
  /** 当前周期（秒）。 */
  intervalSeconds(): number;
  /** 马上要一次应答。 */
  tick(): Promise<HeartbeatTick>;
  /** 开始轮询。 */
  start(): void;
  /** 停止轮询。 */
  stop(): void;
  /** 是否在轮询。 */
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
