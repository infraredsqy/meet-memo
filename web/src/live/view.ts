/**
 * 会中主视图的一份视图模型：把服务推来的那份快照摊成组件要摆的样子。
 *
 * 这一层**只读快照**，不写、不推断、不缓存（除了一份界面自己的观察记录：
 * 心跳四级与两路丢过的窗口，两样都由调用方持有、当参数传进来）。
 *
 * 一条贯穿的规矩：**凡是在界面上出现的数据都能指到服务推来的某个字段**。
 * 包络、峰值保持、`muted`、某一路丢没丢、时长、四态，全部如此；
 * 界面对它们只做两件事 —— 读出来、按设计的规定格式化。
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
import { captureStalled, type CaptureStalledView, type HeartbeatState } from './heartbeat';
import { meterView, type MeterView, type StoredLevels } from './levels';
import {
  pauseBanner,
  pauseButtonLabel,
  sourceToggle,
  type PauseBannerView,
  type SourceToggleView,
} from './pause';
import { healthView, statusView, type HealthView, type StatusView } from './status';
import { emptyLossRecord, type LossRecord } from './loss';

export interface ChannelView {
  sourceId: 'mic' | 'loopback';
  /** 路名 + 形状标记：`本地 · 麦克风` / `远端 · loopback`。 */
  name: string;
  mark: 'local' | 'remote';
  /** 当前实际打开的设备名（探测落到的那个端点）；没有就是 `null`。 */
  device: string | null;
  health: HealthView;
  meter: MeterView;
  /** 读数那一格的字（`−35.2 dBFS` / `—`）。 */
  readout: string;
  /** 包络床右侧的时间戳（分钟级）；这一档没有历史时是 `—`。 */
  stamp: string;
  /** 这一路被用户关掉了没有（服务侧落在现成的 `stopped` 档上）。 */
  closed: boolean;
  /** 这一档摆不摆那颗「关掉 / 打开」的按钮；要摆时是它的样子。 */
  toggle: SourceToggleView | null;
}

export interface LiveView {
  status: StatusView;
  clock: ClockView;
  /** 两路，次序恒为 `mic`、`loopback`。 */
  channels: ChannelView[];
  /** 致命级横幅；`null` 表示现在没有。 */
  fatal: FatalBannerView | null;
  /** 致命级告警的原文 —— 发声判定与静音按钮上的字要用它。 */
  critical: Alert | null;
  hints: HintView[];
  /** 本场静音按钮上的字。 */
  muteLabel: string;
  /** 采集已停那一档；`null` 表示不在这一档。 */
  stalled: CaptureStalledView | null;
  /** 心跳失联：整块告警区收掉（两句话不得同屏）。 */
  alertsHidden: boolean;
  /** 这一场此刻是不是暂停中（读快照的 `session.paused`，界面不自己记）。 */
  paused: boolean;
  /** 暂停 / 继续那颗按钮上的字。 */
  pauseLabel: string;
  /** 「本场已暂停」那条横幅；不在这一档时是 `null`。 */
  pause: PauseBannerView | null;
}

/**
 * 包络床右侧那个时间戳（分钟级）。
 *
 * 取服务推来的两个时刻里能用的那一个：`transcript.last_line_at`（服务收到最近一行时的
 * 墙钟）优先；它还没值（这一场还没出过转写行）时退回**这一份快照到达界面的时刻** ——
 * 「我手上这份数据是什么时候的」。两样都指不出来就给 `—`：床面上没有历史可标。
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

/**
 * 一份快照 → 一份视图模型。
 *
 * 传进来的几样各有来路：
 *
 * - `snapshot`：服务推来的那一份（权威）；
 * - `heartbeat`：界面自己记的心跳四级（失联时读数冻结、告警区收掉都看它）；
 * - `frames`：手上那份 `levels` 帧 —— 它比快照新时才用（见 `levels.ts`）；
 * - `reply`：最近一次成功的心跳应答 —— 「采集已停」那一档要它的
 *   `recording_alive` 与 `seconds_since_last_tick`；
 * - `observedAtIso`：这一份快照到达界面的时刻（只用来给包络床标时间）；
 * - `liveElapsedSeconds`：从收到这一份快照起到现在走了多久 —— 服务在录音途中并不每秒
 *   提交快照，界面在这一个读数上加上自己走过的时间，时长那一格才「秒在走」
 *   （见 `format.ts` 的 `clockView`）；
 * - `losses`：界面记下的「本场哪一路丢过、什么时候丢的、什么时候接回来的」。
 *   默认值是空记录（换场次前、或新开的窗口还没看到过任何一路丢）。
 */
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
   * 「界面看不见服务」这件事要**观察过一次应答之后**才算数。
   *
   * `applyHeartbeatReading` 的初始档就是「正常」，所以刚打开页面、还没收到第一次应答的
   * 那十秒里它不会报失联；但这一层还要再收一道口：**一次应答都没成功过**时，
   * 手上没有任何服务侧时间戳可比，那时候把读数压成 `—`、把告警区收掉，是拿「我还没问」
   * 冒充「我问不到」—— 刚开机那十秒里音频明明在流，屏幕却写成数据已停，那正是设计章 4.5
   * 明令不许犯的那类错。所以这一档要等 `lastReplyAt` 有值。
   */
  const lost = heartbeat.level === '失联' && heartbeat.lastReplyAt !== null;

  /**
   * 「采集已停」那一档：服务**在应答**（HTTP 通、`server_time` 也新），
   * 但应答里的 `recording_alive` 为假 —— 采集线程已经停了（契约第 5 章）。
   *
   * 它与失联**互斥**（HTTP 不通才是失联），两句话不得同屏的约束因此照旧：上面那条
   * `lost` 管「界面看不见服务」，这条管「服务看得见、采集停了」。
   *
   * 处置与失联同一套（任务书「边界」第 6 条：照失联横幅的结构写；数据冻结同理）：
   * 读数退成 `—`、健康位写「数据已停」、电平冻结压灰、包络时间戳停住。
   * 「停住的那一刻」从应答里倒推：`server_time - seconds_since_last_tick`
   * 就是服务最后一次收到帧的墙钟（`captureStalled()` 已经算过同一个数）。
   */
  const stalled = captureStalled(reply, snapshot.top_state);
  const dataStopped = lost || stalled !== null;
  const frozenStamp = lost
    ? formatWallClock(heartbeat.frozenAt)
    : stalled === null
      ? null
      : stalled.lastFrameAt;

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
      // 数据停了：读数退成 `—`，那个数字不再顶着（设计章 4.5）。
      readout: dataStopped || health.frozen ? '—' : formatDbfs(meter.readout),
      stamp: frozenStamp ?? envelopeStamp(snapshot, source, observedAtIso),
      // 「用户关掉这一路」与「这一路出事」是两件事：关掉的那一路服务侧落 `stopped`
      // 这一档（不报故障、不进告警），按钮也是照这一格翻的。
      closed: source.health === 'stopped',
      toggle: sourceToggle(source, snapshot.top_state),
    };
  });

  /**
   * 「通道失效」那条致命横幅只在「录音还在跑、某一路没了」的两态渲染
   * （设计章 4.2 二；会中状态表）。服务在 `ended` 保留 `alerts.critical`
   * 是契约事实（phase1 的行为，界面不改服务端），但结束态的「出事了」
   * 只由收尾横幅与四步明细回答 —— 再摆一条「远端这一路采不到了」就是两处
   * 说同一件事的横幅。收尾中若之前有过提示级条，`hints` 照旧保留结束时的
   * 状态（设计章 2.5），收的只有致命级这一条。
   */
  const fatalVisible = snapshot.top_state === 'recording' || snapshot.top_state === 'alert';

  /**
   * 「暂停中」只从快照读（`session.paused`）：界面不自己记、不自己推断，
   * 刷新页面之后看到的是同一份（契约章 6 第 1、2 条）。
   */
  const paused = snapshot.session?.paused ?? false;

  /**
   * 暂停态里那一格「已录」**不走秒**：把界面自走的那一项收掉。
   *
   * 这一格读的是服务给的 `session.elapsed_seconds`（契约第 2 章：录到的秒数，扣掉暂停
   * 那几段），而 `clockView` 是 `base + live`、`live` 由界面在这份快照之上再加自己走过
   * 的时间。暂停期间服务给的数已经冻住了，界面这一项若不收，两个数一加就把服务刚扣掉的
   * 暂停时间又走回来 —— 屏幕上那格照涨，正是 spec 坑与注意第一条要挡的
   * 「看起来像还在正常录」。所以暂停态下 `live` 压成 0，读数停在快照给的那个数上
   * （**不是把显示值归零**：`base` 照旧是服务给的那一份）。
   * `clockView` 的式子一个字不动（它的单测钉着 `base + live`）。
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
    alertsHidden: lost,
    paused,
    pauseLabel: pauseButtonLabel(paused),
    pause: pauseBanner(paused, snapshot.top_state),
  };
}
