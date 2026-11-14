/**
 * 会中主视图的一份视图模型：把服务推来的那份快照摊成组件要摆的样子。
 *
 * 一条贯穿的规矩：**凡是在界面上出现的数据都能指到服务推来的某个字段** —— 界面对它们只做
 * 两件事：读出来、按设计的规定格式化。
 */

import type { Alert, Heartbeat, Snapshot, Source } from '../contract';
import {
  fatalBanner,
  hintLines,
  muteButtonLabel,
  type FatalBannerView,
  type HintView,
} from './alarm';
import { clockView, formatDbfs, formatWallClock, type ClockView } from './format';
import {
  captureGapNote,
  captureStalled,
  lostPredicate,
  type CaptureStalledView,
  type HeartbeatState,
} from './heartbeat';
import { comboOf, snapshotComboState, type CaptureCombo } from './combo';
import { meterView, type MeterView, type StoredLevels } from './levels';
import { pauseBanner, type PauseBannerView } from './pause';
import { healthView, statusView, type HealthView, type StatusView } from './status';
import { emptyLossRecord, type LossRecord } from './loss';

export interface ChannelView {
  sourceId: 'mic' | 'loopback';
  name: string;
  mark: 'local' | 'remote';
  device: string | null;
  health: HealthView;
  meter: MeterView;
  readout: string;
  stamp: string;
  closed: boolean;
}

export interface LiveView {
  status: StatusView;
  clock: ClockView;
  channels: ChannelView[];
  fatal: FatalBannerView | null;
  critical: Alert | null;
  hints: HintView[];
  muteLabel: string;
  stalled: CaptureStalledView | null;
  gapNote: string | null;
  alertsHidden: boolean;
  paused: boolean;
  /** 收音状态只有这一处出口。 */
  combo: CaptureCombo;
  pause: PauseBannerView | null;
}

/**
 * 包络床右侧那个时间戳（分钟级）：`transcript.last_line_at` 优先，它还没值（这一场还没出过
 * 转写行）时退回**这一份快照到达界面的时刻**。两样都指不出来就给 `—`：床面上没有历史可标。
 */
function envelopeStamp(snapshot: Snapshot, source: Source, observedAtIso: string): string {
  if (source.envelope.length === 0) return '—';
  const lastLine = snapshot.transcript.last_line_at;
  if (lastLine !== null) {
    const formatted = formatWallClock(lastLine);
    if (formatted !== '—') return formatted;
  }
  return formatWallClock(observedAtIso);
}

export function liveView(
  snapshot: Snapshot,
  heartbeat: HeartbeatState,
  frames: StoredLevels | null,
  reply: Heartbeat | null,
  observedAtIso: string,
  liveElapsedSeconds = 0,
  losses: LossRecord = emptyLossRecord(),
): LiveView {
  const status = statusView(snapshot.top_state, snapshot.ended?.phase ?? null);
  /**
   * **一次应答都没成功过**时不报失联：手上没有任何服务侧时间戳可比，把读数压成 `—`、把告警区
   * 收掉，是拿「我还没问」冒充「我问不到」。
   */
  const lost = lostPredicate(heartbeat);

  /**
   * 「采集已停」与失联**互斥**（HTTP 不通才是失联）：`lost` 管「界面看不见服务」，这条管
   * 「服务看得见、采集停了」。判「停」要连续两次读数，计数住在 `heartbeat.stalledBeats` 一处。
   */
  const stalled = captureStalled(reply, snapshot.top_state, heartbeat.stalledBeats);
  const gapNote = captureGapNote(reply);
  const dataStopped = lost || stalled !== null;
  const frozenStamp = lost
    ? formatWallClock(heartbeat.frozenAt)
    : stalled === null
      ? null
      : stalled.lastFrameAt;

  // 「暂停中」只从快照读（`session.paused`）：界面不自己记、不自己推断。
  const paused = snapshot.session?.paused ?? false;

  const combo = comboOf(snapshotComboState(snapshot));

  const channels = snapshot.sources.map<ChannelView>((source) => {
    const key: 'mic' | 'loopback' = source.id === 'mic' ? 'mic' : 'loopback';
    const health = healthView(source, losses.windows[key]);
    // 数据停了：健康位一律改写，估出来的读数不再顶着。
    // 两档各写各的 —— 失联是「界面看不见服务」，采集停是「服务在、帧不来」，
    // 处置同一套，但原因不同，字面上要分得开。
    const stoppedHealth: HealthView = dataStopped
      ? {
          ...health,
          text: lost ? '数据已停止' : '采集已停止',
          tone: 'stopped',
          chip: `${source.label} · ${lost ? '数据已停止' : '采集已停止'}`,
          frozen: true,
          dead: false,
        }
      : health;
    const meter = meterView(source, frames, snapshot.revision);
    const frozenMeter: MeterView = dataStopped || health.frozen ? { ...meter, frozen: true } : meter;
    return {
      sourceId: source.id === 'mic' ? 'mic' : 'loopback',
      name: `${source.label} · ${source.id === 'mic' ? '麦克风' : 'loopback'}`,
      mark: source.id === 'mic' ? 'local' : 'remote',
      device: source.device_name,
      health: stoppedHealth,
      meter: frozenMeter,
      readout: dataStopped || health.frozen ? '—' : formatDbfs(meter.readout),
      stamp: frozenStamp ?? envelopeStamp(snapshot, source, observedAtIso),
      // 「用户关掉这一路」与「这一路出事」是两件事：关掉的那一路服务侧落 `stopped`
      // 这一档（不报故障、不进告警），四组合控件照这一格判现状。
      closed: source.health === 'stopped',
    };
  });

  /**
   * 「通道失效」那条致命横幅只在「录音还在跑、某一路没了」的两态渲染：服务在 `ended` 保留
   * `alerts.critical` 是契约事实，但结束态的「出事了」只由收尾横幅与三步明细回答 ——
   * 再摆一条「远端这一路采不到了」就是两处说同一件事的横幅。
   */
  const fatalVisible = snapshot.top_state === 'recording' || snapshot.top_state === 'alert';

  /**
   * 暂停态里那一格「已录」**不走秒**：把界面自走的那一项收掉。
   *
   * `clockView` 是 `base + live`，暂停期间服务给的数已经冻住了，`live` 若不收，两个数一加就把
   * 服务刚扣掉的暂停时间又走回来 —— 屏幕上那格照涨，正是「看起来像还在正常录」那一类错。
   * （**不是把显示值归零**：`base` 照旧是服务给的那一份。）
   */
  const liveClockSeconds = paused ? 0 : liveElapsedSeconds;

  return {
    status,
    clock: clockView(snapshot.top_state, snapshot.session?.elapsed_seconds ?? null, liveClockSeconds),
    channels,
    fatal: fatalVisible ? fatalBanner(snapshot.alerts.critical) : null,
    critical: snapshot.alerts.critical,
    hints: hintLines(snapshot.alerts),
    muteLabel: muteButtonLabel(snapshot.alerts.critical),
    stalled,
    gapNote,
    alertsHidden: lost,
    paused,
    combo,
    pause: pauseBanner(paused, snapshot.top_state),
  };
}
