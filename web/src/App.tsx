import { useEffect, useMemo, useRef, useState } from 'react';
import { Component } from 'react';
import type { ErrorInfo, ReactNode } from 'react';
import { createEventsClient } from './api';
import type { Snapshot } from './contract';
import { HomeGate } from './home/HomeGate';
import { createEventsSource } from './home/events-source';

class ShellErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  constructor(props: { children: ReactNode }) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error: Error): { error: Error } {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('会中视图渲染失败', error, info);
  }

  render(): ReactNode {
    if (this.state.error !== null) {
      return (
        <div className="shell">
          <h1 className="title-md">会中界面崩了</h1>
          <pre className="fail__why">{this.state.error.message}</pre>
          <p className="hint">刷新页面重连。</p>
        </div>
);
    }
    return this.props.children;
  }
}

export function App(): ReactNode {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  /**
   * 全页恰一个 `EventSource` —— 连接本体在这里 connect/close；快照同时喂判屏（gate 的 state）
   * 与源侧转手槽位。
   */
  const { source, clientHandlers } = useMemo(() => createEventsSource(setSnapshot), [setSnapshot]);
  const clientRef = useRef<ReturnType<typeof createEventsClient> | null>(null);
  useEffect(() => {
    const client = createEventsClient(clientHandlers);
    clientRef.current = client;
    client.connect();
    return () => {
      client.close();
      clientRef.current = null;
    };
  }, [clientHandlers]);
  return (
    <ShellErrorBoundary>
      <HomeGate snapshot={snapshot} events={source} />
    </ShellErrorBoundary>
);
}
