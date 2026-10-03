import type { ConnectionState, EventsClientHandlers } from '../api/events';
import type { LevelsData, Snapshot, TranscriptData } from '../contract';

/**
 * 唯一那条 SSE 连接的**源侧转手**（web-homegate-dual-sse）：
 *
 * 连接本体属于 `App.tsx` 顶层（全页恰一个 `EventSource`），但消费它的两个子屏
 * （工作台 / 会中）是**挂载 → 卸载 → 再挂载**地切换的。回调节不能跟着子组件的
 * 生命周期走 —— 这里替它们先接住：任何时刻来的帧先落到源侧的「最后一份」槽位；
 * 谁挂上来谁 `use()` 注册，注册时把手上还没消费过的积压**补递**过去（快照以
 * 「最后一份」为准），卸载即清空。未挂载时各帧只落在槽位里，不炸也不丢。
 *
 * `snapshot` 同时喂两处：判屏（`HomeGate` 的 gate state，经 `setSnapshot`）与
 * 当前挂着的子屏（经注册的 `onSnapshot`）—— 同一份服务推的原文，谁也不持有
 * 第二份权威。
 */
export interface EventsSource {
  /**
   * 子屏挂载时注册五个回调（与 `EventsClientHandlers` 同形）；晚于首帧的积压帧
   * 在注册这一刻补递，返回的卸载函数把注册清空（卸载后回占位，不发对新屏）。
   */
  use(handlers: EventsClientHandlers): () => void;
}

export function createEventsSource(
  /** 判屏用：`HomeGate` 的 gate state 从这里走（同一份快照。 */
  setSnapshot: (snapshot: Snapshot) => void,
): { source: EventsSource; clientHandlers: EventsClientHandlers } {
  type Registered = EventsClientHandlers | null;
  let registered: Registered = null;
  /** 源侧持有的「最后一份」各通道积压（子屏没挂上时先住在这里）。 */
  let heldConnection: ConnectionState = 'closed';
  let heldSnapshot: Snapshot | null = null;
  let heldLevels: LevelsData | null = null;
  let heldTranscript: TranscriptData | null = null;

  const clientHandlers: EventsClientHandlers = {
    onConnectionState: (state) => {
      heldConnection = state;
      registered?.onConnectionState?.(state);
    },
    onSnapshot: (snapshot) => {
      heldSnapshot = snapshot;
      setSnapshot(snapshot);
      registered?.onSnapshot?.(snapshot, { resync: false, dropped: 0 });
    },
    onLevels: (levels) => {
      heldLevels = levels;
      registered?.onLevels?.(levels);
    },
    onTranscript: (transcript) => {
      heldTranscript = transcript;
      registered?.onTranscript?.(transcript);
    },
    onError: (error) => {
      // 未挂载时占位（照 HomeGate 原先的 () => undefined）：错误只讲给挂着的屏。
      registered?.onError?.(error);
    },
  };

  const source: EventsSource = {
    use(handlers): () => void {
      registered = handlers;
      // 补递积压：连接状态、最后一份快照（以快照为准）、最后一帧电平/转写。
      if (heldConnection !== 'closed') handlers.onConnectionState?.(heldConnection);
      if (heldSnapshot !== null) handlers.onSnapshot?.(heldSnapshot, { resync: false, dropped: 0 });
      if (heldLevels !== null) handlers.onLevels?.(heldLevels);
      if (heldTranscript !== null) handlers.onTranscript?.(heldTranscript);
      return () => {
        registered = null;
      };
    },
  };

  return { source, clientHandlers };
}
