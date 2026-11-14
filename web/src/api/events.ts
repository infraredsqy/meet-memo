import {
  levelsDataSchema,
  resyncSnapshotSchema,
  snapshotSchema,
  sseEventNameSchema,
  transcriptDataSchema,
  type LevelsData,
  type Snapshot,
  type SseEventName,
  type TranscriptData,
} from '../contract';

/**
 * `/api/events` 的 EventSource 封装。
 *
 * - **连接即快照**：建连后第一帧必然是完整 `snapshot`，新开的页面直接落在当前状态、
 *   不经过 `idle` —— 因此这里不维护「我该从哪一版接着补」的游标。
 * - **不依赖 Last-Event-Id**：断线重连后**仍然先发 `snapshot`**，整块替换手上的那份。
 * - **`levels` / `transcript` 是增量流**：不占 `revision`，到达不代表状态变更。
 * - **zod parse 失败要抛带通道名的错误**。
 */

/** `connecting` 与 `open` 由浏览器那头报，`closed` 是收尾后的静止档。 */
export type ConnectionState = 'connecting' | 'open' | 'closed';

export type FramePayloads = {
  snapshot: Snapshot;
  state: Snapshot;
  levels: LevelsData;
  transcript: TranscriptData;
};

const PARSE_CHANNEL: Record<SseEventName, string> = {
  snapshot: 'snapshot（快照帧）',
  state: 'state（状态帧）',
  levels: 'levels（电平帧）',
  transcript: 'transcript（转写帧）',
};

export class ContractParseError extends Error {
  readonly channel: string;
  readonly raw: string;

  constructor(channel: string, raw: string, cause: unknown) {
    const detail = cause instanceof Error ? cause.message : String(cause);
    super(`通道「${channel}」的帧形状不符合契约：${detail}`);
    this.name = 'ContractParseError';
    this.channel = channel;
    this.raw = raw;
  }
}

export interface EventsClientHandlers {
  onConnectionState?: (state: ConnectionState) => void;
  /** 拿到完整快照。`resync` 为真时说明「我漏了一段」—— 把手上这份整块换成它，**不把 `dropped` 当错误码**。 */
  onSnapshot?: (snapshot: Snapshot, info: { resync: boolean; dropped: number }) => void;
  /** 电平帧。只在 `recording` / `alert` 态、4 次/秒。 */
  onLevels?: (levels: LevelsData) => void;
  onTranscript?: (transcript: TranscriptData) => void;
  onError?: (error: Error) => void;
}

export interface EventsClient {
  /** 连上（幂等）。 */
  connect(): void;
  /** 断开。服务侧只把订阅者摘掉，不动会话、不停采集。 */
  close(): void;
  state(): ConnectionState;
  resyncCount(): number;
  /**
   * **同步解析一帧**（`raw` 是一行 `data:` 的正文），返回这一帧的载荷。形状不对时抛
   * `ContractParseError`（带通道名与原文）。
   *
   * 它调到的就是回调那条路径上的同一个 `parseFrame()`：**载荷照旧推给订阅者**，因此这一条
   * 不会把回调「吃掉」。
   */
  parse(event: 'snapshot', raw: string): Snapshot;
  parse(event: 'state', raw: string): Snapshot;
  parse(event: 'levels', raw: string): LevelsData;
  parse(event: 'transcript', raw: string): TranscriptData;
}

type EventSourceFactory = (url: string) => EventSource;

const defaultFactory: EventSourceFactory = (url) => new EventSource(url);

export function createEventsClient(
  handlers: EventsClientHandlers = {},
  options: { url?: string; eventSourceFactory?: EventSourceFactory } = {},
): EventsClient {
  const url = options.url ?? '/api/events';
  const factory = options.eventSourceFactory ?? defaultFactory;

  let source: EventSource | null = null;
  let state: ConnectionState = 'closed';
  let resyncs = 0;

  const setState = (next: ConnectionState): void => {
    if (state === next) return;
    state = next;
    handlers.onConnectionState?.(next);
  };

  const report = (error: Error): void => {
    handlers.onError?.(error);
  };

  /**
   * 解析一帧并**当场交给订阅者**；返回解析出来的载荷（形状不对时抛带通道名的错误）。
   *
   * 类型靠**逐通道的重载**说准 —— 四个重载与四个实现分支一一对应，`SseEventName` 每加一个
   * 取值，少一个重载就编译不过。
   */
  function parseFrame(event: 'snapshot', raw: string): Snapshot;
  function parseFrame(event: 'state', raw: string): Snapshot;
  function parseFrame(event: 'levels', raw: string): LevelsData;
  function parseFrame(event: 'transcript', raw: string): TranscriptData;
  function parseFrame(event: SseEventName, raw: string): unknown {
    let json: unknown;
    try {
      json = JSON.parse(raw);
    } catch (cause) {
      throw new ContractParseError(PARSE_CHANNEL[event], raw, cause);
    }

    const channel = PARSE_CHANNEL[event];
    switch (event) {
      case 'snapshot': {
        // 首帧给十键快照，慢消费者重同步给十二键快照。先试扩展形状再退十键形状：
        // 反过来会让带 `resync` 的帧因为多出两个键被 strict() 拒掉。
        const extended = resyncSnapshotSchema.safeParse(json);
        if (extended.success) {
          resyncs += 1;
          handlers.onSnapshot?.(extended.data, {
            resync: true,
            dropped: extended.data.dropped,
          });
          // 重同步帧比十键多 `resync` / `dropped` 两键，交给订阅者的那一份是**整个**扩展快照。
          return extended.data;
        }
        const plain = snapshotSchema.safeParse(json);
        if (!plain.success) {
          throw new ContractParseError(channel, raw, plain.error);
        }
        handlers.onSnapshot?.(plain.data, { resync: false, dropped: 0 });
        return plain.data;
      }
      case 'state': {
        const parsed = snapshotSchema.safeParse(json);
        if (!parsed.success) {
          throw new ContractParseError(channel, raw, parsed.error);
        }
        handlers.onSnapshot?.(parsed.data, { resync: false, dropped: 0 });
        return parsed.data;
      }
      case 'levels': {
        const parsed = levelsDataSchema.safeParse(json);
        if (!parsed.success) {
          throw new ContractParseError(channel, raw, parsed.error);
        }
        handlers.onLevels?.(parsed.data);
        return parsed.data;
      }
      case 'transcript': {
        const parsed = transcriptDataSchema.safeParse(json);
        if (!parsed.success) {
          throw new ContractParseError(channel, raw, parsed.error);
        }
        handlers.onTranscript?.(parsed.data);
        return parsed.data;
      }
      default: {
        const exhaustive: never = event;
        throw new ContractParseError(String(exhaustive), raw, '未知事件名');
      }
    }
  }

  const dispatchFrame = (name: SseEventName, raw: string): void => {
    switch (name) {
      case 'snapshot':
        parseFrame('snapshot', raw);
        return;
      case 'state':
        parseFrame('state', raw);
        return;
      case 'levels':
        parseFrame('levels', raw);
        return;
      case 'transcript':
        parseFrame('transcript', raw);
        return;
      default: {
        const exhaustive: never = name;
        throw new ContractParseError(String(exhaustive), raw, '未知事件名');
      }
    }
  };

  const bind = (name: SseEventName): void => {
    if (source === null) return;
    source.addEventListener(name, (event: Event) => {
      // EventSource 给的每帧都是 MessageEvent；`data` 就是那一行 `data:` 的正文。
      const message = event as MessageEvent;
      const raw = typeof message.data === 'string' ? message.data : String(message.data);
      try {
        dispatchFrame(name, raw);
      } catch (error) {
        report(error instanceof Error ? error : new Error(String(error)));
      }
    });
  };

  return {
    connect(): void {
      if (source !== null) return;
      setState('connecting');
      const es = factory(url);
      source = es;

      es.onopen = () => {
        setState('open');
      };
      // 断线由浏览器自己按服务给的 `retry: 2000` 重连，这里只如实反映状态。
      es.onerror = () => {
        setState('connecting');
      };
      for (const name of sseEventNameSchema.options) {
        bind(name);
      }
    },

    close(): void {
      if (source === null) return;
      source.close();
      source = null;
      setState('closed');
    },

    state(): ConnectionState {
      return state;
    },

    resyncCount(): number {
      return resyncs;
    },

    /** 实现签名：与四个重载并列时要显式标出来。 */
    parse(event: SseEventName, raw: string): unknown {
      return (parseFrame as (event: SseEventName, raw: string) => unknown)(event, raw);
    },
  } as EventsClient;
}
