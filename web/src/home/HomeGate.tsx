import type { ReactNode } from 'react';
import { LiveView } from '../live/liveview/LiveView';
import { WorkspaceHome } from './WorkspaceHome';
import { showsWorkspaceHome } from './gate';
import type { Snapshot } from '../contract';
import type { EventsSource } from './events-source';

/**
 * 空闲态 ↔ 会中（录音 / 告警）那一屏的闸。
 *
 * 判据全部来自服务推的快照（SSE **那条唯一连接**在 `App.tsx` 顶层 —— 全页恰一个
 * `EventSource`，本组件不再自持连接），具体条件在 `home/gate.ts`
 * （`showsWorkspaceHome()`）里逐条写着，本组件只照它选一屏。
 *
 * 首份快照没到之前一帧都不画（给一行占位，见下）。
 *
 * 服务重启后的自洽归 `LiveView` 与首页各自实现：首页打开时先看注册表的 `recent`
 * 选中最近一场（最近一场只从注册表读，不扫任何场次目录）。
 */
export function HomeGate({
  snapshot,
  events,
}: {
  snapshot: Snapshot | null;
  events: EventsSource;
}): ReactNode {
  if (snapshot === null) {
    // 首份快照还没到（SSE 连上的空档）：不画首页。有场次没收尾时这一帧若画首页，
    // 会先闪一下首页再切走；SSE 连不上也停在占位，不冒充「没有会」。
    return <p className="hint">正在接服务…</p>;
  }
  if (showsWorkspaceHome(snapshot)) {
    return (
      <WorkspaceHome
        heartbeatSeconds={snapshot.settings.heartbeat_interval_seconds}
        drain={
          snapshot.top_state === 'ended' && snapshot.ended !== null
            ? {
                ended: snapshot.ended,
                dir: snapshot.session?.dir ?? null,
                logPath: snapshot.service.log_path ?? null,
              }
            : null
        }
      />
);
  }
  return <LiveView snapshot={snapshot} events={events} />;
}
