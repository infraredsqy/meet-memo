import type { ConnectionState, EventsClientHandlers } from '../api/events';
import type { LevelsData, Snapshot, TranscriptData } from '../contract';

/**
 * 唯一那条 SSE 连接的**源侧转手**。唯一的 `EventSource` 在 `App.tsx` 顶层，消费它的两个
 * 子屏（工作台 / 会中）是**挂载 → 卸载 → 再挂载**地切换的。回调节不能跟着子组件的生命
 * 周期走 —— 这里替它们先接住：任何时刻来的帧先落到源侧的「最后一份」槽位；谁挂上来谁
 * `use()` 注册，注册时把手上还没消费过的积压**补递**过去（快照以「最后一份」为准），
 * 卸载即清空。未挂载时各帧只落在槽位里，不炸也不丢。
 */
export interface EventsSource {
  use(handlers: EventsClientHandlers): () => void;
}

export function createEventsSource(
  setSnapshot: (snapshot: Snapshot) => void,
): { source: EventsSource; clientHandlers: EventsClientHandlers } {
  type Registered = EventsClientHandlers | null;
  let registered: Registered = null;
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
      registered?.onError?.(error);
    },
  };

  const source: EventsSource = {
    use(handlers): () => void {
      registered = handlers;
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
