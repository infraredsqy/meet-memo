import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Component } from 'react';
import type { CSSProperties, ErrorInfo, ReactNode } from 'react';
import {
  createEventsClient,
  createHeartbeatClient,
  fetchSummaries,
  postAction,
  type ActionResult,
  type ConnectionState,
  type HeartbeatTick,
} from './api';
import { failureFrom, type ActionFailureView } from './failure';
import type { Heartbeat, LevelsData, Snapshot, SummaryWindowChoice } from './contract';
import { decideAlarmSound, initialAlarmSound } from './live/alarm';
import {
  applyHeartbeatReading,
  captureStalled,
  clockLabelWhileLost,
  frozenClockValue,
  initialHeartbeatState,
  lostBanner,
  recoveredNoteText,
  recoveredNoteVisible,
  HEARTBEAT_GUIDANCE,
  type HeartbeatState,
} from './live/heartbeat';
import { emptyLossRecord, observeLosses, type LossRecord } from './live/loss';
import { holdFrameWhileDataStopped, releaseHeldFrame, type StoredLevels } from './live/levels';
import { healthView } from './live/status';
import { createAlarmSound, browserAudioContextFactory } from './live/sound';
import {
  SUMMARY_CLIENT_TIMEOUT_SECONDS,
  SUMMARY_WINDOWS,
  summaryAvailable,
  summaryFailureView,
  summaryLogLabel,
  summaryRunningNote,
  summaryWhy,
} from './live/summary';
import {
  createSummaryController,
  type SummaryAnswer,
  type SummaryController,
  type SummaryRecord,
} from './live/summary-controller';
import {
  appendRow,
  insertGap,
  rowTime,
  showJumpToLatest,
  transcriptRow,
  type TranscriptRow,
} from './live/transcript';
import { liveView } from './live/view';
import type { ChannelView } from './live/view';

/**
 * 会中主视图。
 *
 * 用户开着这一页开会，不用盯着屏幕也知道一切正常；一旦出事，第一眼看出是哪一路死了。
 *
 * **这里摆的全是服务推来的副本**：`snapshot` 走 SSE，电平优先走高频的 `levels` 帧、
 * 没有帧时退回快照；包络 15 根、峰值保持、`muted`、四态全部由服务算好。
 * 界面自己只记三样观察：心跳四级、两路丢过的窗口、转写窗口 —— 三样都只活在内存里，
 * 不进 localStorage / sessionStorage / Cookie（契约章 6 第 2 条）。
 *
 * 手动总结那四件套（按钮 / 四档窗口 / 进行中 / 结果面板）与底部的本场记录回看接在这里：
 * 请求状态机与三小段的读法在 `live/summary.ts` 与 `live/summary-controller.ts`，
 * 组件只把算好的东西摆到位置上 —— **只有这一次动作的应答与本场记录那一次 GET 进视图**，
 * 两者都是服务给的原文，界面不派生（契约章 6 第 1 条）。
 */

/** 手工核对用的网址：界面不自取快照，权威来自 SSE 推送，这一条只在排障时打。 */
export const STATE_PROBE_URL = '/api/state';


/** 错误边界：主视图崩了就画一页说明，别留白屏。 */
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
          <p className="hint">
            刷新页面即可重连 —— 界面不缓存任何会中状态，重连后服务会先推一份快照。
          </p>
        </div>
      );
    }
    return this.props.children;
  }
}

/** 一路电平表：刻度、填充、峰值保持指针、15 根历史包络、当前读数。 */
function Channel({ channel }: { channel: ChannelView }): ReactNode {
  const id = useId();
  const { meter, health } = channel;
  const dead = health.dead;
  const frozen = meter.frozen;
  const cls = [
    'm-ch',
    dead ? 'm-ch--dead' : '',
    health.restored ? 'm-ch--restored' : '',
    frozen ? 'm-ch--frozen' : '',
    health.idle && !frozen ? 'm-ch--idle' : '',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <section className={cls} aria-label={channel.name} data-ch={channel.sourceId}>
      <div className="m-ch__head">
        <span className={`m-ch__mark m-ch__mark--${channel.mark}`} aria-hidden="true" />
        <span className="m-ch__name">{channel.name}</span>
        <span className="m-ch__device" title={channel.device ?? undefined}>
          {channel.device ?? '设备名未探测'}
        </span>
        <span className={`m-health m-health--${health.tone}`}>
          <span className={`glyph ${health.glyph}`} aria-hidden="true" />
          {health.text}
        </span>
      </div>
      <div className="m-grid">
        {/* 左格：竖电平 + 读数。两样裹在同一格里，读数才落在井的正下方
            （`.m-grid` 的 meter 那一栏高度就是这个格子的高度） */}
        <div className="m-cell">
          <div
            className="m-meter m-meter--v"
            style={{ '--level': `${meter.levelPercent.toFixed(1)}%`, '--hold': `${meter.holdPercent.toFixed(1)}%` } as CSSProperties}
          >
            <div className="m-meter__scale">
              <span>0</span>
              <span>{'\u2212'}20</span>
              <span>{'\u2212'}40</span>
              <span>{'\u2212'}60</span>
            </div>
            <div className="m-meter__track">
              <span className="m-meter__ticks" />
              <span className="m-meter__fill" />
              <span className="m-meter__peak" />
              <span className="m-meter__mark">{'\u2715'}</span>
            </div>
          </div>
          <span className="m-ch__readout num">{channel.readout}</span>
        </div>
        <div className="m-scale" aria-hidden="true">
          <span style={{ left: '0%' }}>0</span>
          <span style={{ left: '33.333%' }}>{'\u2212'}20</span>
          <span style={{ left: '66.667%' }}>{'\u2212'}40</span>
          <span style={{ left: '100%' }}>{'\u2212'}60 dBFS</span>
        </div>
        <div className="m-env" aria-hidden="true">
          <span className="m-env__zero" />
          {meter.envelope.map((height, index) => (
            <span
              key={`${id}-bar-${index}`}
              className="m-env__bar"
              style={{ '--h': `${height.toFixed(1)}%` } as CSSProperties}
            />
          ))}
        </div>
        <div className="m-env-note">
          <span>历史包络 30 秒 · 2 秒一格</span>          <span className="m-env__stamp">{channel.stamp}</span>
        </div>
      </div>
    </section>
  );
}

/** 失败块的公共画法：原因、下一步、log 路径（可复制）。 */
function FailureBlock({
  failure,
  logPath,
  drainFailedSteps,
}: {
  failure: ActionFailureView;
  logPath: string | null;
  drainFailedSteps: string[];
}): ReactNode {
  if (failure.kind === 'forbidden') {
    return (
      <div className="fail">
        <div className="fail__why">
          请求被拦截 · HTTP {failure.status}（未到达动作端点）
        </div>
        <div className="fail__next">{failure.text}</div>
        <div className="fail__log">
          这一档没有失败信封、也没有 code —— 服务在中间件那一道就把它挡回去了。
        </div>
      </div>
    );
  }
  return (
    <div className="fail">
      <div className="fail__why">
        <code>{failure.code}</code> · {failure.message}
      </div>
      {failure.detail.length > 0 && (
        <ul className="fail__next">
          {failure.detail.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
      {drainFailedSteps.length > 0 && (
        <ul className="fail__next">
          {drainFailedSteps.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
      {logPath !== null && (
        <>
          <code className="codeblock">{logPath}</code>
          <div className="fail__log">log：{logPath}</div>
        </>
      )}
    </div>
  );
}

/**
 * 总结结果面板与失败面板共用的那一块（设计章 2.3）。
 *
 * 位置归调用方（排在每路健康之后、告警带之前），这里只有面板本体：右上角一个 `关闭`，
 * 头一行是**这一次**的窗口（`刚才的 15 分钟`），底下按三小段摆那几条。
 * **关掉即消失、不落文件** —— 记录仍在底部的本场总结记录里。
 *
 * 面板本体（`.summary-card`）与三小段的类名都来自设计章 7.10 的类名契约，
 * 一个字都不新造。
 */
function SummaryCard({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}): ReactNode {
  return (
    <div className="summary-card" id="summary-card">
      <div className="summary-card__head">
        <span className="summary-card__title" id="summary-card-title">
          {title}
        </span>
        <button className="btn btn--small btn--ghost" type="button" onClick={onClose}>
          关闭
        </button>
      </div>
      <div id="summary-card-body">{children}</div>
    </div>
  );
}

/** 三小段：段标题 + 那几条（结果面板与记录回看两处共用同一种摆法）。 */
function SummarySections({
  sections,
  emptyNote,
}: {
  sections: { title: string; items: string[] }[];
  emptyNote: string | null;
}): ReactNode {
  return (
    <>
      {emptyNote !== null && <p className="summary-card__item">{emptyNote}</p>}
      {sections.map((section) => (
        <div key={section.title}>
          <p className="summary-card__sub">{section.title}</p>
          {section.items.map((item, index) => (
            <p className="summary-card__item" key={`${section.title}-${index}`}>
              {item}
            </p>
          ))}
        </div>
      ))}
    </>
  );
}

/** 失败面板：一句原因 +（有就有的）补充行，右上角同样是 `关闭`。 */
function SummaryFailureCard({
  view,
  onClose,
}: {
  view: { text: string; lines: string[] };
  onClose: () => void;
}): ReactNode {
  return (
    <SummaryCard title="本次摘要生成失败" onClose={onClose}>
      <p className="summary-card__item">{view.text}</p>
      {view.lines.map((line) => (
        <p className="summary-card__item" key={line}>
          {line}
        </p>
      ))}
    </SummaryCard>
  );
}

function LiveView(): ReactNode {
  /** 服务推过来的那一份。界面手上这份是**副本**，分歧以服务为准。 */
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [observedAt, setObservedAt] = useState<string>(() => new Date().toISOString());
  const [connection, setConnection] = useState<ConnectionState>('closed');
  const [levels, setLevels] = useState<{ revision: number; data: LevelsData } | null>(null);
  /** 「数据已停」期间按住的那一份帧：电平与包络冻在被冻那一刻，后面来的帧不进画面。 */
  const [heldLevels, setHeldLevels] = useState<StoredLevels | null>(null);
  const [rows, setRows] = useState<TranscriptRow[]>([]);
  const [heartbeat, setHeartbeat] = useState<HeartbeatState>(() => initialHeartbeatState());
  const [reply, setReply] = useState<Heartbeat | null>(null);
  const [losses, setLosses] = useState<LossRecord>(() => emptyLossRecord());
  const [failure, setFailure] = useState<ActionFailureView | null>(null);
  const [busy, setBusy] = useState(false);
  const [logShown, setLogShown] = useState(false);
  const [transcriptOpen, setTranscriptOpen] = useState(false);
  const [jumpVisible, setJumpVisible] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  /** 本场总结记录那一块开着没有（结果面板与它是两块内容，各自关各自的）。 */
  const [summaryLogOpen, setSummaryLogOpen] = useState(false);
  /** 总结区那台状态机（这一次请求的状态、当前档位、记录那一份）。 */
  const summaryRef = useRef<SummaryController | null>(null);
  if (summaryRef.current === null) {
    summaryRef.current = createSummaryController({
      // 这一次动作的应答原文：三小段、窗口标签、时刻、耗时都是服务给的
      request: async (window: SummaryWindowChoice): Promise<SummaryAnswer> => {
        const result: ActionResult = await postAction('summarize', { window });
        if (result.kind !== 'summarize') {
          throw new Error(`summarize 的成功体该带 summary 键，收到的是 ${result.kind}`);
        }
        return result.summary;
      },
      // 本场记录那一次 GET 的原文
      loadLog: async (): Promise<SummaryRecord[]> => (await fetchSummaries()).entries,
      timeoutSeconds: SUMMARY_CLIENT_TIMEOUT_SECONDS,
    });
  }
  const summary = summaryRef.current;
  const [, bumpSummary] = useState(0);

  const eventsRef = useRef<ReturnType<typeof createEventsClient> | null>(null);
  const heartRef = useRef<ReturnType<typeof createHeartbeatClient> | null>(null);
  /** 心跳四级的最新一份：轮询回调要拿它判「这一跳是不是刚从失联回来」。 */
  const heartbeatRef = useRef<HeartbeatState>(heartbeat);
  const alarmRef = useRef(initialAlarmSound());
  const soundRef = useRef(createAlarmSound(browserAudioContextFactory()));
  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const lastRowIdRef = useRef<number>(0);

  const heartbeatSeconds = snapshot?.settings.heartbeat_interval_seconds ?? null;

  const applySnapshot = useCallback((next: Snapshot) => {
    const at = new Date().toISOString();
    setSnapshot(next);
    setObservedAt(at);
    setLosses((previous) => observeLosses(previous, next, at));
  }, []);

  useEffect(() => {
    const client = createEventsClient({
      onConnectionState: setConnection,
      onSnapshot: applySnapshot,
      onLevels: (data) => setLevels({ revision: data.revision, data }),
      onTranscript: (data) => {
        lastRowIdRef.current += 1;
        const row = transcriptRow(data, new Date().toISOString());
        setRows((current) => appendRow(current, row));
      },
      onError: (error) => {
        console.error(error);
        setFailure({
          kind: 'envelope',
          code: 'invalid_setting',
          message: error.message,
          detail: [],
          fields: {},
        });
      },
    });
    eventsRef.current = client;
    client.connect();

    const heart = createHeartbeatClient((tick: HeartbeatTick) => {
      if (tick.kind === 'reply') setReply(tick.heartbeat);
      const at = Date.now();
      const before = heartbeatRef.current;
      const next = applyHeartbeatReading(before, tick, at);
      heartbeatRef.current = next;
      setHeartbeat(next);
      setNowMs(at);
      if (before.level === '失联' && next.level !== '失联' && before.frozenAt !== null) {
        // 恢复：转写区在中断那一格留一条断口（设计章 4.5）。
        // 形状照 transcriptGap() 给的那一档：`[HH:MM–HH:MM] 数据已停` ——
        // 起于冻结那一刻（frozenAt），止于恢复这一刻
        setRows((rowsNow) => insertGap(rowsNow, before.frozenAt!, '', new Date(at).toISOString()));
      }
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

  // 心跳周期来自快照的 settings（服务是唯一来源），周期改了不必刷新页面。
  useEffect(() => {
    if (heartbeatSeconds === null) return;
    heartRef.current?.setIntervalFromSnapshot(heartbeatSeconds);
  }, [heartbeatSeconds]);

  // 换新场次时清掉上一场的增量流与转写窗口：免得旧电平、旧正文顶着新状态。
  // 总结区那两样（这一次的应答 / 本场记录那一份）跟着一起收：新的一场从零开始，
  // 服务侧 `start_session()` 也把缓冲与记录一起清了（phase2 的「缓冲与生命周期」）。
  useEffect(() => {
    if (snapshot === null) return;
    if (snapshot.top_state === 'idle' && snapshot.session === null) {
      setLevels(null);
      setRows([]);
      lastRowIdRef.current = 0;
      summary.close();
    }
  }, [snapshot, summary]);

  // 总结区那台状态机一变就重画（它只在事件里改状态，不走 React 的 setState）。
  useEffect(() => summary.subscribe(() => bumpSummary((n) => n + 1)), [summary]);

  // 记录那一块的面板开着的时候，成功的这一次顺手把列表刷新一遍（新的一条就进来了）。
  useEffect(() => {
    summary.setLogOpen(summaryLogOpen);
    if (summaryLogOpen && summary.log() === null) void summary.loadRecords();
  }, [summaryLogOpen, summary]);

  /**
   * 发声：每来一份新快照判一次「这一帧该不该响」。
   *
   * 判据全在服务推的字段上（`settings.alarm_sound` 与这一条告警的 `muted`），
   * 界面不算任何抑制窗口（`live/alarm.ts`）。
   */
  useEffect(() => {
    if (snapshot === null) return;
    const decision = decideAlarmSound(alarmRef.current, snapshot.alerts, snapshot.settings);
    alarmRef.current = decision.state;
    if (decision.play) soundRef.current.play();
  }, [snapshot]);

  // 恢复记录那条 30 秒的短记录要自己到时消失：每秒走一下本地时钟。
  useEffect(() => {
    if (heartbeat.noticeSinceMs === null) return;
    const timer = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [heartbeat.noticeSinceMs]);

  /**
   * 时长那一格的秒针。
   *
   * 录音中 / 告警时每秒走一下 `nowMs`（其余档位不必：空闲固定 `00:00:00`、
   * 已结束那几档由服务冻结，它们都不会因为这一下重画而变样，所以直接不挂这个钟）。
   */
  useEffect(() => {
    if (snapshot === null) return;
    if (snapshot.top_state !== 'recording' && snapshot.top_state !== 'alert') return;
    setNowMs(Date.now());
    const timer = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [snapshot?.top_state, snapshot?.revision]);

  // 自动播放策略：一次用户交互之后补一次音频授权。
  useEffect(() => {
    const unlock = (): void => soundRef.current.unlock();
    window.addEventListener('pointerdown', unlock, { once: true });
    window.addEventListener('keydown', unlock, { once: true });
    return () => {
      window.removeEventListener('pointerdown', unlock);
      window.removeEventListener('keydown', unlock);
    };
  }, []);

  /**
   * 打一个动作。**返回值交给调用方** —— 会中总结那一条要吃应答体里那份 `summary`
   * （三小段是它画的），所以这里是 `async` 并且把 `ActionResult` 交回去。
   *
   * **只有这一处让动作的应答进视图，而且它只进这一个视图**：应答的原文原样交给
   * 「这一次总结」那块面板，权威状态照旧等服务的下一次推送 —— 这里一个状态都不改
   * （契约章 6 第 1 条）。
   */
  const runAction = useCallback(
    async (name: string, args: Record<string, unknown> = {}): Promise<ActionResult> => {
      setBusy(true);
      setFailure(null);
      try {
        // 返回的是服务给的应答原文（**权威状态照旧等服务的下一次推送**：这里一个状态都不改）
        return await postAction(name, args);
      } catch (error) {
        setFailure(failureFrom(error));
        throw error;
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  /**
   * 打一个动作；失败时**只画失败块**，不往外抛 —— 调用方是 `onClick`，抛出去就是一条
   * 未处理的 promise 拒绝（失败块已经说清楚了，控制台上那条噪声没有用处）。
   */
  const runActionQuietly = useCallback(
    (name: string, args: Record<string, unknown> = {}): void => {
      void runAction(name, args).catch(() => undefined);
    },
    [runAction],
  );

  /**
   * 「数据已停」＝失联（界面看不见服务）或采集已停（服务在应答但没有帧）。
   *
   * 这两档为真时两路电平、15 根包络、读数**全部冻在被冻那一刻**（设计章 4.5：
   * 不把估出来的数据继续画成在动）：下面的效果把手上的帧按住，视图只吃按住的
   * 那一份；恢复（失联解除 / 采集恢复）那一刻松手，下一帧起立刻回到实时。
   */
  const dataStopped =
    (heartbeat.level === '失联' && heartbeat.lastReplyAt !== null) ||
    (snapshot !== null && captureStalled(reply, snapshot.top_state) !== null);

  // 冻结开始：按住当下这份帧（已在按住就不换 —— 冻结期间来多少帧都不动）；
  // 恢复：松手回实时（levels.ts 的 holdFrameWhileDataStopped / releaseHeldFrame）
  useEffect(() => {
    if (dataStopped) {
      const current = levels === null ? null : { revision: levels.revision, frames: levels.data.frames };
      setHeldLevels((held) => holdFrameWhileDataStopped(held, current));
    } else {
      setHeldLevels((held) => (held === null ? held : releaseHeldFrame()));
    }
  }, [dataStopped, levels]);

  const view = useMemo(() => {
    if (snapshot === null) return null;
    // 秒在走那一格：服务给的读数 + 界面从收到这份快照起走过的时间（快照一换就归零）
    const live = snapshot.top_state === 'ended' ? 0 : Math.max(0, (nowMs - Date.parse(observedAt)) / 1000);
    // 数据已停：只吃按住的那份帧（把它的 revision 钉到当前快照上 —— 冻结期间
    // 「旧」正是要的结果，陈旧帧判据在这里不适用）；其余时候随到随吃
    const frames =
      dataStopped && heldLevels !== null
        ? { revision: snapshot.revision, frames: heldLevels.frames }
        : (levels?.data ?? null);
    return liveView(snapshot, heartbeat, frames, reply, observedAt, live, losses);
  }, [snapshot, heartbeat, levels, heldLevels, dataStopped, reply, observedAt, nowMs, losses]);

  const lost = heartbeat.level === '失联' && heartbeat.lastReplyAt !== null;
  const stalled = view?.stalled ?? null;
  const logPath = snapshot?.service.log_path ?? null;
  const alarmSoundOn = snapshot?.settings.alarm_sound ?? false;
  const chips = useMemo(() => {
    if (snapshot === null) return [];
    return snapshot.sources.map((source) => {
      const key: 'mic' | 'loopback' = source.id === 'mic' ? 'mic' : 'loopback';
      return healthView(source, losses.windows[key]).chip;
    });
  }, [snapshot, losses]);

  // 转写：贴底就跟着滚动，用户翻上去了就不动他（设计章 2.3）。
  useLayoutEffect(() => {
    const box = transcriptRef.current;
    if (box === null) return;
    const sticking = !showJumpToLatest(box.scrollTop, box.scrollHeight, box.clientHeight);
    if (sticking) box.scrollTop = box.scrollHeight;
    setJumpVisible(!sticking);
  }, [rows]);

  const onTranscriptScroll = useCallback(() => {
    const box = transcriptRef.current;
    if (box === null) return;
    setJumpVisible(showJumpToLatest(box.scrollTop, box.scrollHeight, box.clientHeight));
  }, []);

  const onJump = useCallback(() => {
    const box = transcriptRef.current;
    if (box === null) return;
    box.scrollTop = box.scrollHeight;
    setJumpVisible(false);
  }, []);

  const drainFailedSteps = useMemo(() => {
    const steps = snapshot?.ended?.steps ?? [];
    return steps
      .filter((step) => step.status === 'failed')
      .map((step) => `收尾第 ${step.id} 步失败：${step.error ?? step.detail}`);
  }, [snapshot]);

  /**
   * 收尾失败那一档要画的那块明细。
   *
   * 两个来路：动作失败（用户按了 stop 而收尾报错）与服务侧自己的收尾失败
   * （`ended.error` 有值、或某一步 `status = failed`）。后者这一趟没有任何动作，
   * `failure` 是空的 —— 不从这里兜一份出来的话，页面上就只剩一条
   * 「收尾出事了，先别关机」的标题，原因与 log 路径一个字都没有。
   */
  const drainFailure = useMemo<ActionFailureView | null>(() => {
    if (failure !== null) return failure;
    const ended = snapshot?.ended;
    if (ended === undefined || ended === null) return null;
    const detail = [
      ...(ended.error === null ? [] : [`收尾失败：${ended.error}`]),
      ...drainFailedSteps,
    ];
    if (detail.length === 0) return null;
    return { kind: 'envelope', code: 'persist_failed', message: '收尾没走完', detail, fields: {} };
  }, [failure, snapshot, drainFailedSteps]);

  const bodyClass = [
    'page--live',
    lost ? 'is-heartbeat-lost' : '',
  ]
    .filter(Boolean)
    .join(' ');
  useEffect(() => {
    document.body.className = bodyClass;
  }, [bodyClass]);

  if (snapshot === null || view === null) {
    return (
      <main className="shell page--live">
        <p className="hint">正在等待服务的首份快照…（连接：{connection}）</p>
      </main>
    );
  }

  const started = snapshot.top_state !== 'idle';
  const showStart = snapshot.top_state === 'idle';
  const showEnd = snapshot.top_state === 'recording' || snapshot.top_state === 'alert';
  const capsuleTone =
    view.status.tone === 'alert'
      ? 'statusline__caps--alert'
      : view.status.tone === 'on'
        ? 'statusline__caps--on'
        : '';
  const recoveredVisible = recoveredNoteVisible(heartbeat, nowMs);
  const captureNote = stalled === null ? null : `采集已停止 · ${stalled.seconds.toFixed(1)} 秒未收到音频帧`;
  /** 失联横幅的正文两处：周期与「最后一次应答 HH:MM:SS」。 */
  const lostView = lostBanner(heartbeat, heartbeatSeconds ?? 10);

  /**
   * 总结区那四样。**能不能按**由服务推来的那份快照算（四态 + 心跳失联 + 采集已停 +
   * 转写健康位），**这一次请求的状态**由界面那台状态机给 —— 两样分得清，谁也不冒充谁。
   */
  const summaryState = summary.state();
  const summaryAvailability = {
    topState: snapshot.top_state,
    lost,
    stalled: stalled !== null,
    transcriptHealth: snapshot.transcript.health,
    running: summaryState.phase === 'running',
  };
  const summaryReady = summaryAvailable(summaryAvailability);
  // 跑起来时旁边写进行中那一句（不写禁用原因），其余各档照 `summaryWhy()` 的现成文案
  const summaryWhyText = summaryWhy(summaryAvailability);
  const logView = summary.log();
  /** 记录那一块取不到时给的那句话（取到了就是 `null`）。 */
  const logError = summary.logError();
  const summaryFailureText =
    summaryState.phase === 'failure' && summaryState.failure !== null
      ? summaryFailureView(summaryState.failure, summaryState.elapsedSeconds)
      : null;

  return (
    <>
      {/* 心跳失联：全屏横幅盖在任何状态之上（设计章 4.5）。它是这一屏上唯一说
          「出事了」的地方 —— 下面的整块告警区这时候是收着的 */}
      {lost && (
        <div className="heartbeat" role="alert">
          <div className="heartbeat__title">录音已中断</div>
          <div className="heartbeat__body">{lostView.body}</div>
          <div className="heartbeat__why">{HEARTBEAT_GUIDANCE}</div>
          <div className="heartbeat__do">
            先不要关机，也不要关闭本页。到服务那边确认一下：
            <span className="num">Ctrl+C</span> 停掉再敲一次 <span className="num">meeting web</span>
            {logPath === null ? '' : <>；日志在 <span className="num">{logPath}</span></>}。
          </div>
        </div>
      )}

      <main
        className={`shell ${bodyClass}`}
        data-hb-level={heartbeat.level}
        data-hb-last-reply={heartbeat.lastReplyAt ?? ''}
        data-top-state={snapshot.top_state}
        /* 致命告警的身份四样值（哪一路 / since / muted）：真机验证拿它判「这一帧到了
           没有」，界面上不显示、也不参与任何样式 */
        data-critical={
          view.critical === null
            ? ''
            : `${view.critical.source}|${view.critical.since}|${String(view.critical.muted)}`
        }
      >
      {/* 服务重启过（章 4.5）：只在空闲态、且日志里有未收尾记录时出现 */}
      {view.restartNote !== null && <p className="restart-note hint">{view.restartNote}</p>}

      <header className={`topbar ${lost ? 'topbar--hb-lost' : ''}`}>
        <div className="topbar__left">
          <div className="statusline">
            <span className={`statusline__caps ${capsuleTone}`}>
              <span className={`glyph ${view.status.glyph}`} aria-hidden="true" />
              <span className="statusline__name">{view.status.name}</span>
            </span>
            <span className="statusline__note">{view.status.note}</span>
          </div>
          {recoveredVisible && heartbeat.gapSeconds !== null && (
            <span className="recovered-note">{recoveredNoteText(heartbeat.gapSeconds)}</span>
          )}
        </div>

        <div className="topbar__main">
          <span className={`clock ${view.clock.faint ? 'clock--faint' : ''}`}>
            {/* 失联：标签改「最后读数」，数字也一起冻在最后一份快照的已录时长上
                （clockLabelWhileLost / frozenClockValue）—— 数字不许再走 */}
            <span className="clock__label">
              {lost ? clockLabelWhileLost() : view.clock.label}
            </span>
            <span className="clock__value num">
              {lost
                ? frozenClockValue(snapshot.session?.elapsed_seconds)
                : view.clock.value}
            </span>
          </span>
          {showStart && (
            <button
              className="btn btn--primary btn--start"
              type="button"
              disabled={busy}
              onClick={() => runActionQuietly('start', { mode: 'both' })}
            >
              开始录音
            </button>
          )}
          {showEnd && (
            <button
              className="btn btn--primary btn--end"
              type="button"
              disabled={busy}
              onClick={() => runActionQuietly('stop')}
            >
              停止录音
            </button>
          )}
        </div>

        <div className="topbar__right">
          <span className="kv">
            <input
              type="checkbox"
              id="alarm-switch-main"
              checked={alarmSoundOn}
              disabled={busy}
              onChange={(event) =>
                runActionQuietly('settings_update', { 'alarm.sound': event.target.checked })
              }
            />
            <label className="kv__k" htmlFor="alarm-switch-main">
              告警提示音
            </label>
          </span>
        </div>
      </header>

      {/* 采集带：两路左右并列，右侧三分之一给总结入口（方向 A） */}
      <section className="band">
        <div className="band__head">
          <h2 className="band__title">音频采集</h2>
          <div className="meter-legend">
            <span className="meter-legend__item">
              <span className="m-ch__mark m-ch__mark--local" aria-hidden="true" />
              本地
            </span>
            <span className="meter-legend__item">
              <span className="m-ch__mark m-ch__mark--remote" aria-hidden="true" />
              远端
            </span>
            <span className="meter-legend__item">历史包络 2 秒一格</span>
          </div>
        </div>
        <div className="band__body">
          <div className="m-band m-band--split m-band--tools">
            <div className="m-chans">
              {view.channels.map((channel) => (
                <Channel key={channel.sourceId} channel={channel} />
              ))}
            </div>
            <div className="live-tools">
              {/* 四个窗口档位（设计章 2.3）。缺省选中的是「15 分钟」；按下之后请求里带的
                  取值来自 `SUMMARY_WINDOWS` —— 界面上写的是人话，请求里带的是契约取值 */}
              <div className="seg" role="group" aria-label="摘要时间范围">
                {SUMMARY_WINDOWS.map((item) => (
                  <button
                    key={item.choice}
                    className={`seg__item ${summary.state().window === item.choice ? 'seg__item--on' : ''}`}
                    type="button"
                    aria-pressed={summary.state().window === item.choice}
                    disabled={!summaryReady}
                    onClick={() => summary.select(item.choice)}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
              {/* 主按钮：录音中 / 告警态可用；跑起来时按钮位换成进行中那一句（不显示倒计时、
                  不提前宣布失败）。其余各档按钮按不动，原因写在下面那一行 */}
              {summary.state().phase === 'running' ? (
                <p className="sumbar__note" id="summary-run">
                  <span className="spinner" aria-hidden="true" />{' '}
                  {summaryRunningNote(summary.state().window)}
                </p>
              ) : (
                <button
                  className="btn btn--primary btn--block"
                  type="button"
                  id="summary-btn"
                  disabled={!summaryReady}
                  onClick={() => void summary.run()}
                >
                  快速总结
                </button>
              )}
              {summaryWhyText !== null && (
                <p className="sumbar__why" id="summary-why">
                  {summaryWhyText}
                </p>
              )}
              {/* 本场摘要记录：入口写着按过的次数，点开列每一次（时刻 + 窗口 + 那几条）。
                  那一行在**还没取过服务那份**时先写「还没有」—— 这一刻手上确实一条都没有；
                  取回来之后整行换成服务给的条目数。它是**本场**的，与结果面板两块内容各关各的 */}
              <button
                className="btn btn--ghost btn--block"
                type="button"
                id="summary-log-btn"
                aria-expanded={summaryLogOpen}
                onClick={() => setSummaryLogOpen((open) => !open)}
              >
                {summaryLogLabel(summary.logCount())}
              </button>
            </div>
          </div>
        </div>
      </section>

      {/* 每路健康：两条文字保证，任何宽度下都不折叠 */}
      <section className="band band--quiet">
        <div className="band__head">
          <h2 className="band__title">各通道状态</h2>
        </div>
        <div className="band__body">
          <div className="m-chips">
            {chips.map((text) => (
              <span className="m-chip" key={text}>
                {text}
              </span>
            ))}
          </div>
        </div>
      </section>

      {/* 总结结果面板（设计章 2.3 的主位第 5 项）：**位置在每路健康之后、告警带之前**。
          只有几条，关掉即消失、不落文件；记录仍在底部的本场总结记录里。
          结果与失败共用一块面板：一次总结只会落在其中一档 */}
      {summaryState.phase === 'done' && summaryState.result !== null && (
        <section className="band band--quiet">
          <SummaryCard title={summaryState.result.title} onClose={() => summary.close()}>
            <SummarySections
              sections={summaryState.result.sections}
              emptyNote={summaryState.result.emptyNote}
            />
          </SummaryCard>
        </section>
      )}

      {summaryFailureText !== null && (
        <section className="band band--quiet">
          <SummaryFailureCard view={summaryFailureText} onClose={() => summary.close()} />
        </section>
      )}

      {/* 本场总结记录：入口在采集带右侧，展开的内容跟到这里（与结果面板同一处落位）。

          **判据是两样取或**：手上有服务给的那一份（`logView`），**或者**有取不到时那句说明
          （`logError`）。只看前一半的话，本场**第一次**展开、而那一次 `GET /api/summaries`
          没取到时（网络错、服务忙、非 200），面板整块不出现 —— 控制层已经备好的那句说明
          够不着，入口那一行还恒写着「还没有」，用户看到的是「按了没反应」。 */}
      {summaryLogOpen && (logView !== null || logError !== null) && (
        <section className="band band--quiet">
          <SummaryCard
            title={logView === null ? summaryLogLabel(null) : logView.title}
            onClose={() => setSummaryLogOpen(false)}
          >
            {logError !== null && <p className="summary-card__item">{logError}</p>}
            {logView !== null && logView.emptyNote !== null && (
              <p className="summary-card__item">{logView.emptyNote}</p>
            )}
            {/* 每一次一条：`[<HH:MM>] 取的「<窗口档位>」` 起一小段，下面按
                讲什么 / 讲到哪 / 丢掉什么 的次序列那几条（`1987cb20/phase3/task.md`
                的「记录回看」那一节的排版；与设计稿把每条压成一句话的写法不一致，
                这一处偏差记在交付报告里）。

                外层 `.list` + 每条 `.list__row` 管**条目之间**的次序；每一条里面再套一层
                `.list` 管**起头那一行与三小段之间**的次序（`.list__row` 自带 flex，
                直接往里塞两行内容会把它们横着排成一行）。两层的类名都来自设计章 7.10，
                一行内联样式、一个新造的类名都没有。 */}
            {logView !== null && (
              <div className="list" id="summary-log-list">
                {logView.entries.map((entry) => (
                  <div className="list__row" key={entry.key}>
                    <div className="list">
                      <p className="summary-card__sub">
                        [{entry.time}] 取的「{entry.windowLabel}」
                      </p>
                      <SummarySections sections={entry.sections} emptyNote={entry.emptyNote} />
                    </div>
                  </div>
                ))}
              </div>
            )}
          </SummaryCard>
        </section>
      )}

      {/* 告警区：失联时整块收掉（两句话不得同屏）；采集已停那一条也不与致命横幅同屏 */}
      {!view.alertsHidden && (
        <section className="band band--alert" aria-label="告警">
          {view.fatal !== null && view.critical !== null && (
            <div className="alert alert--fatal">
              <div className="alert__head">
                <span className="alert__glyph" aria-hidden="true">
                  {'\u25B2'}
                </span>
                <span className="alert__where">{view.fatal.where}</span>
                <span className="alert__since">{view.fatal.since}</span>
                <span className="alert__elapsed">{view.fatal.elapsed}</span>
              </div>
              <div className="alert__acts">
                <button
                  className="btn btn--small"
                  type="button"
                  aria-pressed={view.critical.muted}
                  disabled={busy}
                  onClick={() => runActionQuietly('mute_session')}
                >
                  {view.muteLabel}
                </button>
                <button
                  className="btn btn--small btn--ghost"
                  type="button"
                  onClick={() => setLogShown((shown) => !shown)}
                >
                  查看日志
                </button>
                <button
                  className="btn btn--small btn--danger"
                  type="button"
                  disabled={busy}
                  onClick={() => runActionQuietly('stop')}
                >
                  停止录音
                </button>
              </div>
              {logShown && logPath !== null && <code className="codeblock">{logPath}</code>}
            </div>
          )}

          {stalled !== null && view.fatal === null && (
            <div className="alert alert--fatal">
              <div className="alert__title">采集已停止</div>
              <div className="alert__body">
                服务仍在应答，但已连续 {stalled.seconds.toFixed(1)} 秒未收到任何一路的音频帧，
                最后一次收到音频帧是 {stalled.lastFrameAt}。这段时间里两路的内容都记不下来。
              </div>
              <div className="alert__body">
                先不要关机，也不要关闭本页。到服务那边确认一下：看日志里最后一次收到音频帧
                那一段在报什么{logPath === null ? '' : `；日志在 ${logPath}`}。
              </div>
            </div>
          )}

          {view.hints.map((hint) => (
            <div className="alert alert--hint" key={`${hint.since}-${hint.text}`}>
              <span className="alert__glyph" aria-hidden="true">
                {'\u25B2'}
              </span>
              <span className="alert__since">{hint.since}</span>
              <span>{hint.text}</span>
            </div>
          ))}

          {/* 心跳迟到：一行弱提示，不出声、无横幅 */}
          {heartbeat.level === '迟到' && heartbeat.lateText !== null && (
            <div className="heartbeat heartbeat--inline">{heartbeat.lateText}</div>
          )}

          {/* 失败与拒绝：按 code 分派，detail 有哪个键显示哪个键（契约 6.7） */}
          {failure !== null && (
            <FailureBlock
              failure={failure}
              logPath={logPath}
              drainFailedSteps={drainFailedSteps}
            />
          )}
        </section>
      )}

      {/* 收尾：能不能关机，只由这一条回答 */}
      {snapshot.ended !== null && (
        <section className="band band--alert" aria-label="收尾">
          <div className="alert alert--fatal">
            <div className="alert__title">
              {snapshot.ended.phase === 'done'
                ? '已完成，可以关机'
                : drainFailedSteps.length > 0
                  ? '收尾出事了，先别关机'
                  : '正在落盘，先别关机'}
            </div>
            <div className="alert__body">
              {snapshot.ended.phase === 'done'
                ? '音频与日志均已落盘，会后处理已启动，可以关机。'
                : '音频仍在转码、落盘，会后处理也刚启动。这些步骤走完之前，数据还没有落好。'}
            </div>
            <div className="alert__acts">
              <span className="kv__v">
                {snapshot.ended.steps
                  .map((step) => `${step.id}：${step.status}`)
                  .join(' · ')}
              </span>
            </div>
          </div>
          {/* 收尾失败：原因 + 可复制的命令 + log 路径（设计章 4.2 二）。
              这一档的失败明细不只来自动作失败 —— 服务自己在收尾里出事时
              （`ended.error` 有值、或某一步 `status = failed`）也要画出来，
              所以判据是「收尾没走到 done + 有错可报」 */}
          {snapshot.ended.phase !== 'done' && drainFailure !== null && (
            <FailureBlock
              failure={drainFailure}
              logPath={logPath}
              drainFailedSteps={drainFailedSteps}
            />
          )}
        </section>
      )}

      {/* 动作失败与拒绝：按 code 分派，detail 有哪个键显示哪个键（契约 6.7）。
          它排在告警区外面：失联时那一整块要收掉（两句话不同屏），
          而动作失败讲的是「刚才这一下没成」这件事 */}
      {failure !== null && (
        <section className="band band--alert" aria-label="失败">
          <FailureBlock failure={failure} logPath={logPath} drainFailedSteps={drainFailedSteps} />
        </section>
      )}

      {/* 心跳失联那条判据的说明（章 4.5）：只在失联时出现 */}
      {lost && <div className="hint hb-guidance">{HEARTBEAT_GUIDANCE}</div>}

      {/* 实时转写：临时稿。区里不出现保存 / 导出 / 正式记录这类字样 */}
      <section className="band transcript-block">
        <div className="band__head">
          <h2 className="band__title">实时转写</h2>
          <span className="band__note">
            {started ? '最近 200 行' : '尚未开始 · 开录后逐行显示'}
          </span>
        </div>
        <div className="band__body">
          {/* 类名契约（ui-design.md 章 7.10 / docs/ui/live.html L214–216）：这一行标签
              用 `.sumbar` 内放两个 `.tag` —— 与设计稿同一条用法，不另造类名 */}
          <div className="sumbar">
            <span className="tag">草稿 · 会后重建</span>
            <span className="tag">
              {snapshot.transcript.health === 'normal'
                ? '正常'
                : snapshot.transcript.health === 'behind'
                  ? '延迟'
                  : '已停止'}
            </span>
          </div>
          {!started && (
            <p className="empty">
              尚未开始录音。按下「开始录音」后，两路的转写会逐行显示在这里，
              每行带分钟级时间戳，并标明来源（本地或远端）。
            </p>
          )}
          {started && (
            <>
              <div
                className={`transcript ${transcriptOpen ? 'is-open' : ''}`}
                id="transcript-lines"
                ref={transcriptRef}
                onScroll={onTranscriptScroll}
              >
                {/* 断口行就是普通 transcript__line + transcript__gap（docs/ui/live.html
                    L256–260 的用法）：不再挂自造的修饰类，视觉与语义都归契约类名 */}
                {rows.map((row, index) => (
                  <div
                    className="transcript__line"
                    key={`${row.at}-${index}`}
                  >
                    <span className="transcript__time">
                      {/* 断口行带 [起–止] 区间（transcript.ts 的 gapRow 摆好），
                          普通行是 [HH:MM] —— 两样都由 rowTime() 给 */}
                      {rowTime(row)}
                    </span>
                    <span className="transcript__route">{row.route}</span>
                    {row.gap ? (
                      <span className="transcript__gap">{row.text}</span>
                    ) : (
                      <span className="transcript__text">{row.text}</span>
                    )}
                  </div>
                ))}
              </div>
              {/* 「回到最新」在滚动区外面：滚动区自己滚起来时它不该跟着跑掉 */}
              {jumpVisible && (
                <div className="transcript__jump">
                  <button className="btn btn--small btn--ghost" type="button" onClick={onJump}>
                    回到最新
                  </button>
                </div>
              )}
              {/* 窄屏（720 以下）把转写折成一行「最近一句」，靠这个钮展开；桌面宽度下
                  这一格由 CSS 收着（转写本来就在页面上） */}
              <div className="band__foot">
                <button
                  className="btn btn--small transcript__toggle"
                  type="button"
                  aria-expanded={transcriptOpen}
                  aria-controls="transcript-lines"
                  onClick={() => setTranscriptOpen((open) => !open)}
                >
                  {transcriptOpen ? '收起' : '展开'}{' '}
                  <span className="transcript__toggle-hint">最近一行</span>
                </button>
              </div>
            </>
          )}
        </div>
      </section>

      <p className="hint">
        页面上的状态全部来自服务推送；不在浏览器本地存储中保存任何会中状态。
        {captureNote === null ? '' : ` ${captureNote}。`}
      </p>
      </main>
    </>
  );
}

export function App(): ReactNode {
  return (
    <ShellErrorBoundary>
      <LiveView />
    </ShellErrorBoundary>
  );
}
