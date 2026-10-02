import { useEffect, useState, type ReactNode } from 'react';
import { LiveView } from '../App';
import { WorkspaceHome } from './WorkspaceHome';
import { createEventsClient } from '../api/events';
import { showsWorkspaceHome } from './gate';
import type { Snapshot } from '../contract';

/**
 * 空闲态 ↔ 会中（含收尾/会后）那一屏的闸。
 *
 * 判据全部来自服务推的快照（SSE 一条自己的连接，死守"不持本地权威状态"），
 * 具体条件在 `home/gate.ts`（`showsWorkspaceHome()`）里逐条写着，本组件只照它选一屏：
 *
 * - 回工作台：`idle` 且没有场次；以及 `ended` 且收尾四步都落定（按「结束录音」之后
 *   会后一个动作都不自动跑，工作区回到「当前没有进行中的会议」——「我这些会」那一行还在，
 *   右边是这一场的详情，会后那两个按钮住在那里）；
 * - 留在会中那一屏：`recording` / `alert` / 收尾还在跑。
 *
 * 首份快照没到之前一帧都不画（给一行占位，见下）。
 *
 * 服务重启后的自洽归 `LiveView` 与首页各自实现：首页打开时先看注册表的 `recent`
 * 选中最近一场（最近一场只从注册表读，不扫任何场次目录）。
 */
export function HomeGate(): ReactNode {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  useEffect(() => {
    const client = createEventsClient({
      onConnectionState: () => undefined,
      onSnapshot: setSnapshot,
      onLevels: () => undefined,
      onTranscript: () => undefined,
      onError: () => undefined,
    });
    client.connect();
    return () => client.close();
  }, []);
  if (snapshot === null) {
    // 首份快照还没到（SSE 连上的空档）：不画首页。有场次没收尾时这一帧若画首页，
    // 会先闪一下首页再切走；SSE 连不上也停在占位，不冒充「没有会」。
    return <p className="hint">正在接服务…</p>;
  }
  if (showsWorkspaceHome(snapshot)) {
    // 心跳周期顺手交给工作台：会后进度区的轮询跟着同一个可配节奏走（节点 52b24392）。
    return <WorkspaceHome heartbeatSeconds={snapshot.settings.heartbeat_interval_seconds} />;
  }
  return <LiveView />;
}
