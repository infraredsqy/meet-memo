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
 * `/api/events` 的 EventSource 封装（`docs/contract.md` 第 3 章）。
 *
 * 几条口径照着契约写死：
 *
 * - **连接即快照**：建连后第一帧必然是完整 `snapshot`，新开的页面直接落在当前状态、
 *   不经过 `idle`。因此这里不维护「我该从哪一版接着补」的游标 —— 没有那个东西。
 * - **不依赖 Last-Event-Id**：服务侧没有重放机制、没有事件缓冲，断线重连后**仍然先发
 *   `snapshot`**。界面拿到它整块替换手上的那份，这就是全部的重同步手段。
 * - **`levels` / `transcript` 是增量流**：高频通道，不占 `revision`，
 *   到达不代表状态变更。
 * - **zod parse 失败要抛带通道名的错误**：形状漂了要能一眼看出是哪一路漂的，
 *   而不是抛一句「JSON.parse 失败」。
 */

/** 连接状态。`connecting` 与 `open` 由浏览器那头报，`closed` 是收尾后的静止档。 */
export type ConnectionState = 'connecting' | 'open' | 'closed';

/** 各通道解析出来的载荷类型。订阅者从这里取，`FramePayloads` 因此有了真实用处。 */
export type FramePayloads = {
  snapshot: Snapshot;
  state: Snapshot;
  levels: LevelsData;
  transcript: TranscriptData;
};

/** 各通道的解析器。分开写是为了让错误信息能点名是哪一路。 */
const PARSE_CHANNEL: Record<SseEventName, string> = {
  snapshot: 'snapshot（快照帧）',
  state: 'state（状态帧）',
  levels: 'levels（电平帧）',
  transcript: 'transcript（转写帧）',
};

/** 形状对不上的那一帧：带上通道名与原始 data，方便排障。 */
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
  /** 连接状态变化（含重连时的 `connecting`）。 */
  onConnectionState?: (state: ConnectionState) => void;
  /**
   * 拿到完整快照：建连首帧、每次状态变更、以及慢消费者重同步。
   *
   * `resync` 为真时说明「我漏了一段」—— 契约要求把手上这份整块换成它，
   * 并把丢了多少条如实告诉用户，**不把 `dropped` 当错误码**。
   */
  onSnapshot?: (snapshot: Snapshot, info: { resync: boolean; dropped: number }) => void;
  /** 电平帧。只在 `recording` / `alert` 态、4 次/秒。 */
  onLevels?: (levels: LevelsData) => void;
  /** 转写帧。每出一行来一帧。 */
  onTranscript?: (transcript: TranscriptData) => void;
  /** 帧形状不符合契约。 */
  onError?: (error: Error) => void;
}

export interface EventsClient {
  /** 连上（重复调用是幂等的）。 */
  connect(): void;
  /** 断开。服务侧只把订阅者摘掉，不动会话、不停采集。 */
  close(): void;
  /** 当前连接状态。 */
  state(): ConnectionState;
  /** 本连接生命周期里见到 `resync: true` 的次数。 */
  resyncCount(): number;
  /**
   * **同步解析一帧**（`raw` 是一行 `data:` 的正文），返回这一帧的载荷。
   *
   * 事件流的常规消费方式是构造时给的回调；这一条是**回调之外的另一条读法**：
   * 排障手测时想把手上这段原文直接过一遍 schema，或者将来需要在回调之外补一条
   * 同步读法时用它。形状不对时抛 `ContractParseError`（带通道名与原文）。
   *
   * 它调到的就是回调那条路径上的同一个 `parseFrame()`：**载荷照旧推给订阅者**，
   * 因此这一条不会把回调「吃掉」。
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
   * 返回类型照真实行为收准：它**不再写 `| null`** —— 五个分支过去恒 `return null`，
   * 那个 `null` 是「返回值没人用」留下的空转，`FramePayloads` 也因此成了死类型
   * （复审抓到的那一格）。现在返回值就是这一帧的载荷：调用方拿得到，类型也说得清。
   *
   * 类型靠**逐通道的重载**说准（`K` 是联合类型时 `FramePayloads[K]` 收不出单通道那一种，
   * 而函数体里按 `event` 分支之后每一支的形状都是确定的）—— 四个重载与四个实现分支
   * 一一对应，`SseEventName` 每加一个取值，少一个重载就编译不过。
   *
   * 载荷**照旧同时推给订阅者**：事件流的消费方式是回调（`onSnapshot` / `onLevels` /
   * `onTranscript`），返回值是给「想在这一刻直接拿这一帧」的调用方用的（例如排障手测、
   * 或将来在回调之外补一条同步读法），两件事互不冲突。
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
          // 重同步帧比十键多 `resync` / `dropped` 两键，交给订阅者的那一份是**整个**扩展快照
          // （第 3.5 节要求把手上这份整块换成它）；返回值给的是它，字段比十键那一档多两个。
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

  /** 把一帧交给解析器（`name` 在这里是联合类型，返回值这一层不关心 —— 载荷由回调接走）。 */
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
      // 断线由浏览器自己按服务给的 `retry: 2000` 重连，这里只如实反映状态；
      // 重连后服务仍然先发 snapshot，界面照单整块替换，不需要任何补齐逻辑。
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

    /** 实现签名：与四个重载并列时要显式标出来（`EventsClient.parse` 的重载就是它）。 */
    parse(event: SseEventName, raw: string): unknown {
      return (parseFrame as (event: SseEventName, raw: string) => unknown)(event, raw);
    },
  } as EventsClient;
}
