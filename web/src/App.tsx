import { Component, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ErrorInfo, ReactNode } from 'react';
import {
  createEventsClient,
  createHeartbeatClient,
  postAction,
  type ConnectionState,
  type HeartbeatStatus,
  type HeartbeatTick,
} from './api';
import { failureFrom, type ActionFailureView } from './failure';
import type { LevelsData, Snapshot, TranscriptData } from './contract';

/**
 * 壳页 —— 管线样板，只求把契约链路走通给人看。
 *
 * 它管这几样：连接状态与 resync 计数、`top_state`、`revision`、心跳三态、
 * 两路 sources 的 `health` 与电平数字、开始/停止按钮、错误按 `code` 显示。
 * 电平表盘、告警横幅、设置抽屉留给 `070b31e0` 的监控界面 —— 那些不在本单元。
 *
 * **界面手上的东西全是副本**：`snapshot` 来自服务推送，心跳周期来自快照的
 * `settings.heartbeat_interval_seconds`，包络与 `bar` 由服务算好。
 * 这里没有 localStorage / sessionStorage —— 会中状态一律从服务取（第 6 章第 2 条）。
 */

/** 手工核对用的网址：壳页不自取快照，权威来自 SSE 推送，这一条只在排障时打。 */
export const STATE_PROBE_URL = '/api/state';

/** 心跳失联时服务侧采集是否还活着的读数 —— 只是显示，界面不做结论。 */
function formatSecondsSinceTick(value: number | null): string {
  if (value === null) return '—';
  return `${value.toFixed(1)} s`;
}

function formatAlive(value: boolean | null): string {
  if (value === null) return '—（没在录）';
  return value ? '采集在跑' : '采集已停';
}

/** 电平读数：没有读数时退成 `—`，不把估出来的数字顶着。 */
function formatLevel(value: number | string | null): string {
  if (value === null) return '—';
  if (typeof value === 'number') return `${value.toFixed(1)} dBFS`;
  return value;
}

/** 错误边界：壳页崩了就画一页说明，别留白屏。 */
class ShellErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  constructor(props: { children: ReactNode }) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error: Error): { error: Error } {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('壳页渲染失败', error, info);
  }

  render(): ReactNode {
    if (this.state.error !== null) {
      return (
        <div style={{ padding: 24, fontFamily: 'system-ui, sans-serif' }}>
          <h1 style={{ fontSize: 18 }}>壳页崩了</h1>
          <pre style={{ whiteSpace: 'pre-wrap', color: '#b00' }}>{this.state.error.message}</pre>
          <p style={{ color: '#666' }}>刷新页面即可重连 —— 界面不缓存任何会中状态，重连后服务会先推一份快照。</p>
        </div>
      );
    }
    return this.props.children;
  }
}

function Shell(): ReactNode {
  /** 服务推过来的那一份。界面手上这份是**副本**，分歧以服务为准。 */
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [connection, setConnection] = useState<ConnectionState>('closed');
  const [resyncCount, setResyncCount] = useState(0);
  const [levels, setLevels] = useState<LevelsData | null>(null);
  const [transcript, setTranscript] = useState<TranscriptData | null>(null);
  const [lastDropped, setLastDropped] = useState(0);
  const [failure, setFailure] = useState<ActionFailureView | null>(null);
  const [busy, setBusy] = useState(false);

  const [heartbeat, setHeartbeat] = useState<HeartbeatTick | null>(null);
  const [heartbeatStatus, setHeartbeatStatus] = useState<HeartbeatStatus | null>(null);

  const eventsRef = useRef<ReturnType<typeof createEventsClient> | null>(null);
  const heartRef = useRef<ReturnType<typeof createHeartbeatClient> | null>(null);

  /** 心跳周期来自快照；拿不到快照时由客户端兜默认 10 秒。 */
  const heartbeatSeconds = snapshot?.settings.heartbeat_interval_seconds ?? null;

  const applySnapshot = useCallback(
    (next: Snapshot, info: { resync: boolean; dropped: number }) => {
      setSnapshot(next);
      if (info.resync) {
        // 见到 resync 就整块换成这一份，并把「漏了 N 条」如实说出来。
        setResyncCount((n) => n + 1);
        setLastDropped(info.dropped);
      }
    },
    [],
  );

  useEffect(() => {
    const client = createEventsClient({
      onConnectionState: setConnection,
      onSnapshot: applySnapshot,
      onLevels: setLevels,
      onTranscript: setTranscript,
      onError: (error) => {
        console.error(error);
        setFailure({
          kind: 'envelope',
          code: 'invalid_setting',
          message: error.message,
          detail: [],
        });
      },
    });
    eventsRef.current = client;
    client.connect();

    const heart = createHeartbeatClient((tick) => {
      setHeartbeat(tick);
      setHeartbeatStatus(tick.status);
    });
    heartRef.current = heart;
    heart.start();

    return () => {
      client.close();
      heart.stop();
      eventsRef.current = null;
      heartRef.current = null;
    };
  }, [applySnapshot]);

  // 快照更新时把周期跟着改 —— 配置改了不必刷新页面。
  useEffect(() => {
    if (heartbeatSeconds === null) return;
    heartRef.current?.setIntervalFromSnapshot(heartbeatSeconds);
  }, [heartbeatSeconds]);

  /** 换新场次时清掉上一场的增量流，免得旧电平顶着新状态。 */
  useEffect(() => {
    if (snapshot === null) return;
    if (snapshot.top_state === 'idle') {
      setLevels(null);
      setTranscript(null);
    }
  }, [snapshot]);

  const runAction = useCallback(async (name: string, args: Record<string, unknown> = {}) => {
    setBusy(true);
    setFailure(null);
    try {
      await postAction(name, args);
      // 权威状态由服务随后推过来，这里不自己改任何状态。
    } catch (error) {
      setFailure(failureFrom(error));
    } finally {
      setBusy(false);
    }
  }, []);

  const onStart = useCallback(() => void runAction('start', { mode: 'both' }), [runAction]);
  const onStop = useCallback(() => void runAction('stop'), [runAction]);
  const aliveNow = heartbeat?.kind === 'reply' ? heartbeat.heartbeat.recording_alive : null;
  const secondsSinceTick =
    heartbeat?.kind === 'reply' ? heartbeat.heartbeat.seconds_since_last_tick : null;

  const connectionLabel = useMemo(() => {
    switch (connection) {
      case 'connecting':
        return '连接中…';
      case 'open':
        return '已连接';
      case 'closed':
        return '未连接';
    }
  }, [connection]);

  const lost = heartbeatStatus === '失联';

  return (
    <div
      style={{
        fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif',
        margin: '0 auto',
        maxWidth: 860,
        padding: '24px 20px 48px',
        color: '#1b1b1f',
        background: '#fafaf8',
        minHeight: '100vh',
      }}
    >
      <header style={{ marginBottom: 20 }}>
        <h1 style={{ fontSize: 20, margin: '0 0 4px' }}>meeting · 管线壳页</h1>
        <p style={{ margin: 0, color: '#6b6b74', fontSize: 13 }}>
          这页只把契约链路走通：连接、快照、动作、心跳。产品级监控界面不在这里。
        </p>
      </header>

      {lost && (
        <div
          role="alert"
          style={{
            background: '#b3261e',
            color: '#fff',
            padding: '12px 16px',
            borderRadius: 8,
            marginBottom: 16,
            fontWeight: 600,
          }}
        >
          心跳失联 —— 界面看不见服务，整场录音可能已经中断。
          {heartbeat?.kind === 'reply' && ` 最后一次应答：revision ${heartbeat.heartbeat.revision}。`}
        </div>
      )}

      {failure !== null && failure.kind === 'forbidden' && (
        <div
          role="alert"
          style={{
            border: '1px solid #d8b46a',
            background: '#fdf8ec',
            padding: '10px 14px',
            borderRadius: 8,
            marginBottom: 16,
            fontSize: 13,
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: 4 }}>
            请求被拦下 · HTTP {failure.status}（未走到动作端点）
          </div>
          <div>{failure.text}</div>
          <div style={{ color: '#6b6b74', marginTop: 4 }}>
            这一档没有失败信封，也没有 <code>code</code> —— 服务在中间件那一道就把它挡回去了。
          </div>
        </div>
      )}

      {failure !== null && failure.kind === 'envelope' && (
        <div
          style={{
            border: '1px solid #e0b4b0',
            background: '#fdf2f1',
            padding: '10px 14px',
            borderRadius: 8,
            marginBottom: 16,
            fontSize: 13,
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: 4 }}>
            动作失败 · <code>{failure.code}</code>
          </div>
          <div>{failure.message}</div>
          {failure.detail.length > 0 && (
            <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
              {failure.detail.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      <section style={cardStyle}>
        <Row label="连接状态">
          {connectionLabel}
          {resyncCount > 0 && (
            <span style={{ color: '#8a6d00' }}>
              {' '}
              · 重同步 {resyncCount} 次（最近丢了 {lastDropped} 条）
            </span>
          )}
        </Row>
        <Row label="top_state">{snapshot === null ? '—（还没拿到快照）' : snapshot.top_state}</Row>
        <Row label="revision">{snapshot === null ? '—' : snapshot.revision}</Row>
        <Row label="心跳">
          <strong style={{ color: heartbeatStatus === '失联' ? '#b3261e' : '#1b1b1f' }}>
            {heartbeatStatus ?? '—'}
          </strong>
          <span style={{ color: '#6b6b74' }}>
            {' '}
            · 周期 {heartbeatSeconds ?? 10} 秒 · 采集：{formatAlive(aliveNow)} · 距最近一帧{' '}
            {formatSecondsSinceTick(secondsSinceTick)}
          </span>
          {heartbeat !== null && heartbeat.kind === 'reply' && (
            <span style={{ color: '#6b6b74' }}>
              {' '}
              · 服务时间 {heartbeat.heartbeat.server_time}
            </span>
          )}
        </Row>
        <Row label="转写流">
          {transcript === null
            ? '—（还没收到行）'
            : `${transcript.line.kind} · ${transcript.line.source} · ${transcript.line.text}`}
        </Row>
        <Row label="操作">
          <button type="button" onClick={onStart} disabled={busy} style={buttonStyle}>
            开始
          </button>
          <button type="button" onClick={onStop} disabled={busy} style={buttonStyle}>
            停止
          </button>
          <span style={{ color: '#6b6b74', marginLeft: 8 }}>
            经 <code>POST /api/actions</code>
          </span>
        </Row>
      </section>

      <section style={cardStyle}>
        <h2 style={h2Style}>两路源</h2>
        {snapshot === null ? (
          <p style={{ color: '#6b6b74', margin: 0 }}>还没拿到快照。</p>
        ) : (
          snapshot.sources.map((source) => {
            const frame = levels?.frames[source.id === 'mic' ? 'mic' : 'loopback'] ?? null;
            return (
              <div key={source.id} style={{ padding: '8px 0', borderTop: '1px solid #ececE8' }}>
                <div style={{ display: 'flex', gap: 12, alignItems: 'baseline' }}>
                  <strong style={{ minWidth: 88 }}>
                    {source.label}（{source.id}）
                  </strong>
                  <span style={{ color: '#6b6b74' }}>health: {source.health}</span>
                  <span style={{ color: '#6b6b74' }}>
                    电平: {formatLevel(source.level_dbfs)} · 峰值: {formatLevel(source.peak_dbfs)}
                  </span>
                  <span style={{ color: '#6b6b74' }}>包络: {source.envelope.length} 根</span>
                </div>
                <div style={{ fontSize: 12, color: '#8a8a93', marginTop: 2 }}>
                  levels 帧：dbfs {formatLevel(frame?.dbfs ?? null)} · peak{' '}
                  {formatLevel(frame?.peak ?? null)} · bar {formatLevel(frame?.bar ?? null)}
                  {frame !== null && ` · ts ${String(frame.ts)}`}
                </div>
              </div>
            );
          })
        )}
      </section>

      {snapshot?.notice != null && (
        <section style={cardStyle}>
          <h2 style={h2Style}>notice</h2>
          <p style={{ margin: 0 }}>{snapshot.notice.text}</p>
        </section>
      )}

      <p style={{ color: '#8a8a93', fontSize: 12, marginTop: 24 }}>
        界面状态全部是服务推送的副本；不以本地存储保存任何会中状态。
      </p>
    </div>
  );
}

const cardStyle = {
  background: '#fff',
  border: '1px solid #e6e6e1',
  borderRadius: 10,
  padding: '14px 16px',
  marginBottom: 16,
} as const;

const h2Style = { fontSize: 14, margin: '0 0 8px', color: '#4a4a52' } as const;

const buttonStyle = {
  marginRight: 8,
  padding: '6px 14px',
  borderRadius: 6,
  border: '1px solid #c9c9c4',
  background: '#f4f4f1',
  cursor: 'pointer',
} as const;

function Row({ label, children }: { label: string; children: ReactNode }): ReactNode {
  return (
    <div style={{ display: 'flex', gap: 12, padding: '4px 0', fontSize: 14 }}>
      <span style={{ minWidth: 88, color: '#8a8a93' }}>{label}</span>
      <span>{children}</span>
    </div>
  );
}

export function App(): ReactNode {
  return (
    <ShellErrorBoundary>
      <Shell />
    </ShellErrorBoundary>
  );
}
