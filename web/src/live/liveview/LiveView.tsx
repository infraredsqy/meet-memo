import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import {
  createHeartbeatClient,
  fetchSummaries,
  postAction,
  type ActionResult,
  type HeartbeatTick,
} from '../../api';
import { failureFrom, type ActionFailureView } from '../../failure';
import { FailureBlock } from '../../failure-ui';
import type {
  Heartbeat,
  LevelsData,
  SessionRow,
  SessionsResponse,
  Snapshot,
  SummaryWindowChoice,
} from '../../contract';
import { HEARTBEAT_INTERVAL_DEFAULT_SECONDS } from '../../contract';
import { gpuBusyView } from '../../home/progress';
import { loadSessionList } from '../../home/sessions-list';
import type { EventsSource } from '../../home/events-source';
import { WorkspaceRail } from '../../home/WorkspaceRail';
import { MaterialsPanel } from '../../materials/MaterialsPanel';
import { clampHeartbeatInterval } from '../../api/heartbeat';
import { decideAlarmSound, initialAlarmSound } from '../alarm';
import {
  applyHeartbeatReading,
  captureStalled,
  lostBanner,
  lostPredicate,
  initialHeartbeatState,
  recoveredNoteVisible,
  type HeartbeatState,
} from '../heartbeat';
import { emptyLossRecord, observeLosses, type LossRecord } from '../loss';
import { drainFailedSteps as drainFailedStepsOf } from '../drain';
import { holdFrameWhileDataStopped, releaseHeldFrame, type StoredLevels } from '../levels';
import { createAlarmSound, browserAudioContextFactory } from '../sound';
import {
  comboActions,
  comboOf,
  comboStateOf,
  snapshotComboState,
  type CaptureCombo,
  type ComboState,
} from '../combo';
import {
  SUMMARY_CLIENT_TIMEOUT_SECONDS,
  summaryAvailable,
  summaryFailureView,
  summaryWhy,
} from '../summary';
import {
  createSummaryController,
  type SummaryAnswer,
  type SummaryController,
  type SummaryRecord,
} from '../summary-controller';
import {
  appendRow,
  insertGap,
  transcriptRow,
  type TranscriptRow,
} from '../transcript';
import { liveView } from '../view';
import { normalizeDefaultWindow } from '../settings';
import { AlertsArea } from './AlertsArea';
import { CaptureBand } from './CaptureBand';
import { LiveTopBar } from './LiveTopBar';
import { SummaryPanels } from './SummaryPanels';
import { TranscriptArea } from './TranscriptArea';
import { WsBarDetail } from './WsBarDetail';

/**
 * 会中主视图。
 *
 * **这里摆的全是服务推来的副本**：`snapshot` 走 SSE，电平优先走高频的 `levels` 帧、没有帧时退回快照。
 * 界面自己只记三样观察（心跳四级、两路丢过的窗口、转写窗口）—— 只活在内存里，不进
 * localStorage / sessionStorage / Cookie。
 */

/** **SSE 那条唯一连接在 `App.tsx` 顶层**：全页恰一个 `EventSource`；LiveView 只经 `events` 转手注册五路回调。 */
export function LiveView({
  snapshot: snapshotProp,
  events,
}: {
  snapshot: Snapshot;
  events: EventsSource;
}): ReactNode {
  /** 界面手上这份是**副本**，分歧以服务为准。 */
  const [snapshot, setSnapshot] = useState<Snapshot>(snapshotProp);
  const [observedAt, setObservedAt] = useState<string>(() => new Date().toISOString());
  const [levels, setLevels] = useState<{ revision: number; data: LevelsData } | null>(null);
  /** 「数据已停」期间按住的那一份帧：电平与包络冻在被冻那一刻，后面来的帧不进画面。 */
  const [heldLevels, setHeldLevels] = useState<StoredLevels | null>(null);
  const [rows, setRows] = useState<TranscriptRow[]>([]);
  const [heartbeat, setHeartbeat] = useState<HeartbeatState>(() => initialHeartbeatState());
  const [reply, setReply] = useState<Heartbeat | null>(null);
  const [losses, setLosses] = useState<LossRecord>(() => emptyLossRecord());
  const [failure, setFailure] = useState<ActionFailureView | null>(null);
  const [busy, setBusy] = useState(false);
  /** 正在切去的那个组合：按下这一刻就把高亮交给它，快照真落到这一档或动作失败才收。 */
  const [pendingCombo, setPendingCombo] = useState<CaptureCombo | null>(null);
  const [logShown, setLogShown] = useState(false);
  const [summaryLogOpen, setSummaryLogOpen] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  /** 工作区注册表的那一份（`GET /api/sessions` 的应答原文）：`post_jobs` 喂全局「显卡忙不忙」那一格，清单整份喂左栏那条常驻工作区栏。 */
  const [wsSessions, setWsSessions] = useState<SessionsResponse | null>(null);
  const [wsSelection, setWsSelection] = useState<string | null>(null);
  const summaryRef = useRef<SummaryController | null>(null);
  if (summaryRef.current === null) {
    summaryRef.current = createSummaryController({
      request: async (window: SummaryWindowChoice): Promise<SummaryAnswer> => {
        const result: ActionResult = await postAction('summarize', { window });
        if (result.kind !== 'summarize') {
          throw new Error(`summarize 的成功体该带 summary 键，收到的是 ${result.kind}`);
        }
        return result.summary;
      },
      loadLog: async (): Promise<SummaryRecord[]> => (await fetchSummaries()).entries,
      timeoutSeconds: SUMMARY_CLIENT_TIMEOUT_SECONDS,
    });
  }
  const summary = summaryRef.current;
  const [, bumpSummary] = useState(0);

  const heartRef = useRef<ReturnType<typeof createHeartbeatClient> | null>(null);
  /** 心跳四级的最新一份：轮询回调要拿它判「这一跳是不是刚从失联回来」。 */
  const heartbeatRef = useRef<HeartbeatState>(heartbeat);
  const alarmRef = useRef(initialAlarmSound());
  const soundRef = useRef(createAlarmSound(browserAudioContextFactory()));

  const heartbeatSeconds = snapshot?.settings.heartbeat_interval_seconds ?? null;

  const applySnapshot = useCallback((next: Snapshot) => {
    const at = new Date().toISOString();
    setSnapshot(next);
    setObservedAt(at);
    setLosses((previous) => observeLosses(previous, next, at));
    // 组合切换的「落地」判据：推来的这一份真落到按下的那一档，乐观高亮才收
    // （中途路过的新快照 —— 别的动作 commit 的 —— 不算落地）。
    setPendingCombo((current) =>
      current !== null && comboOf(snapshotComboState(next)) === current ? null : current,
);
  }, []);

  // SSE 那条唯一连接的接线：挂载即往源侧注册四路回调，卸载即清空。连接掉线的「出事了」由
  // 心跳失联横幅说，界面不另摆一格连接读数。
  useEffect(() => {
    return events.use({
      onSnapshot: applySnapshot,
      onLevels: (data) => setLevels({ revision: data.revision, data }),
      onTranscript: (data) => {
        const row = transcriptRow(data, new Date().toISOString());
        setRows((current) => appendRow(current, row));
      },
      onError: (error) => {
        console.error(error);
        // SSE 的错误没有失败信封（不是动作应答），走 unclassified，不编码。
        setFailure({ kind: 'unclassified', text: error.message });
      },
    });
  }, [events, applySnapshot]);

  // 心跳那条自持的轮询：周期来自快照的 settings。
  useEffect(() => {
    const heart = createHeartbeatClient((tick: HeartbeatTick) => {
      if (tick.kind === 'reply') setReply(tick.heartbeat);
      const at = Date.now();
      const before = heartbeatRef.current;
      const next = applyHeartbeatReading(before, tick, at);
      heartbeatRef.current = next;
      setHeartbeat(next);
      setNowMs(at);
      if (before.level === '失联' && next.level !== '失联' && before.frozenAt !== null) {
        // 恢复：转写区在中断那一格留一条断口 —— 起于冻结那一刻（frozenAt），止于恢复这一刻。
        setRows((rowsNow) => insertGap(rowsNow, before.frozenAt!, '', new Date(at).toISOString()));
      }
    });
    heartRef.current = heart;
    heart.start();

    return () => {
      heart.stop();
      heartRef.current = null;
    };
  }, []);

  // 心跳周期来自快照的 settings（服务是唯一来源），周期改了不必刷新页面。
  useEffect(() => {
    if (heartbeatSeconds === null) return;
    heartRef.current?.setIntervalFromSnapshot(heartbeatSeconds);
  }, [heartbeatSeconds]);

  // 工作区栏读数：挂上这一屏就按心跳节奏取一遍 `GET /api/sessions`。取不到的那一跳（瞬态
  // 失联）手上这份照旧，下一跳再试。
  const postJobs = wsSessions?.post_jobs ?? [];
  useEffect(() => {
    let cancelled = false;
    const seconds = clampHeartbeatInterval(heartbeatSeconds ?? HEARTBEAT_INTERVAL_DEFAULT_SECONDS);
    const load = (): void => {
      void loadSessionList({
        onList: (data) => {
          if (cancelled) return;
          setWsSessions(data);
        },
        onSelection: (seat) => {
          if (cancelled) return;
          setWsSelection(seat);
        },
      });
    };
    load();
    const timer = setInterval(load, seconds * 1000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [heartbeatSeconds]);

  /** 清单重取（左栏的登记动作落地后走这一条）。 */
  const reloadWs = useCallback((): void => {
    void loadSessionList({
      onList: setWsSessions,
      onSelection: setWsSelection,
    });
  }, []);

  // 快速总结的缺省档同样来自快照的 settings：用户显式挑过的档不被覆盖。
  useEffect(() => {
    if (snapshot === null) return;
    summary.applyDefaultWindow(normalizeDefaultWindow(snapshot.settings.summary_default_window));
  }, [snapshot, summary]);

  // 换新场次时清掉上一场的增量流、转写窗口与总结区那两样：免得旧电平、旧正文顶着新状态。
  useEffect(() => {
    if (snapshot === null) return;
    if (snapshot.top_state === 'idle' && snapshot.session === null) {
      setLevels(null);
      setRows([]);
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
   * 发声：每来一份新快照判一次「这一帧该不该响」—— 判据全在服务推的字段上，界面不算任何抑制窗口。
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
   * 时长那一格的秒针：录音中 / 告警时每秒走一下 `nowMs`（其余档位由服务冻结，不必挂这个钟）。
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
   * 打一个动作。**返回值交给调用方** —— 会中总结那一条要吃应答体里那份 `summary`。
   *
   * **动作应答只进这一个视图**：原文原样交给「这一次总结」那块面板，权威状态照旧等服务的下一次推送。
   */
  const runAction = useCallback(
    async (name: string, args: Record<string, unknown> = {}): Promise<ActionResult> => {
      setBusy(true);
      setFailure(null);
      try {
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

  /** 打一个动作；失败时**只画失败块**，不往外抛（调用方是 `onClick`，抛出去就是一条未处理的拒绝）。 */
  const runActionQuietly = useCallback(
    (name: string, args: Record<string, unknown> = {}): void => {
      void runAction(name, args).catch(() => undefined);
    },
    [runAction],
);

  /**
   * 切工作区：发**既有**的 `workspace_open` 登记动作，成功后才把详情切过去 —— 登记失败时详情
   * 留在原处，失败块说原因。
   */
  const switchWorkspace = useCallback(
    (dir: string): void => {
      if (dir === wsSelection) return;
      void runAction('workspace_open', { dir })
        .then(() => setWsSelection(dir))
        .catch(() => undefined); // 失败块已由 runAction 画出
    },
    [runAction, wsSelection],
);

  /**
   * 四组合直切：映射表差几步发几步，按次序发、中途失败即停。
   *
   * 「现状」在半途要读对：上一趟还没落地（`pendingCombo` 有值）时快照还是旧的，现状改读按下
   * 那一档所代表的那份 —— 连切几次才不会串态。重按当前档映射表给空表，一个请求都不发。
   */
  const onCombo = (target: CaptureCombo): void => {
    const snapshotState = snapshotComboState(snapshot);
    const current: ComboState =
      pendingCombo === null
        ? snapshotState
        : pendingCombo === 'paused'
          ? // 「全停」不改两路的开 / 关：两路留在原处，暂停这一格才是它定下的
            { ...snapshotState, paused: true }
          : comboStateOf(pendingCombo);
    const actions = comboActions(target, current);
    if (actions.length === 0) return;
    setPendingCombo(target);
    void (async () => {
      try {
        for (const action of actions) {
          await runAction(
            action.name,
            action.name === 'set_source'
              ? { source: action.source, active: action.active }
              : {},
);
        }
      } catch {
        // 失败块已由 runAction 画出；这里只把乐观高亮收掉，让它退回快照说的那一档
        setPendingCombo(null);
      }
    })();
  };

  /**
   * 「数据已停」＝失联（界面看不见服务）或采集已停（服务在应答但没有帧）：两档为真时两路电平、
   * 15 根包络、读数**全部冻在被冻那一刻**，不把估出来的数据继续画成在动。
   */
  const dataStopped = lostPredicate(heartbeat) ||
    (snapshot !== null &&
      captureStalled(reply, snapshot.top_state, heartbeat.stalledBeats) !== null);

  // 冻结开始：按住当下这份帧（已在按住就不换）；恢复：松手回实时。
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

  const lost = lostPredicate(heartbeat);
  const stalled = view?.stalled ?? null;
  const logPath = snapshot?.service.log_path ?? null;
  // 全局「显卡忙不忙」：忙 = 有模型开着 —— 快照说录着（含告警）是会中那一半，`post_jobs` 是
  // 会后那一半（转码那趟不吃显卡）。
  const gpu = gpuBusyView(
    postJobs,
    snapshot !== null && (snapshot.top_state === 'recording' || snapshot.top_state === 'alert'),
);
  /** 左栏小详情说的是选中的那一场（清单点哪行说哪场）；选区没落座时这一块不给。 */
  const wsRow: SessionRow | null =
    (wsSessions?.sessions ?? []).find((row) => row.dir === wsSelection) ?? null;

  /** 收尾失败那几步的行（读法在 `live/drain.ts`，本组件只转手）。 */
  const drainFailedSteps = useMemo(() => drainFailedStepsOf(snapshot), [snapshot]);

  const bodyClass = [
    'page--live',
    lost ? 'is-heartbeat-lost' : '',
  ]
    .filter(Boolean)
    .join(' ');
  useEffect(() => {
    document.body.className = bodyClass;
  }, [bodyClass]);

  if (view === null) {
    return (
      <main className="shell page--live">
        <p className="hint">正在接服务…</p>
      </main>
);
  }

  const started = snapshot.top_state !== 'idle';
  /** 会议资料区的目标目录：当前场次（资料跟场次走）。空闲 / 已收场没有在录的目录，
   *  这一块不摆——会后那一半在工作台详情里（`WorkspaceHome` 的 `MaterialsPanel`）。 */
  const sessionDir = snapshot.session?.dir ?? null;
  /** 四组合控件此刻亮哪一档：半途亮按下去的那档（乐观），落地后亮快照说的那档。 */
  const shownCombo: CaptureCombo = pendingCombo ?? view.combo;
  /** 「已恢复」那一行的间隔秒数；这一档没在显示时是 `null`（判据在 recoveredNoteVisible）。 */
  const recoveredGapSeconds = recoveredNoteVisible(heartbeat, nowMs) ? heartbeat.gapSeconds : null;
  const lostView = lostBanner(heartbeat, heartbeatSeconds ?? 10);

  /**
   * 总结区那四样：**能不能按**由服务推来的那份快照算，**这一次请求的状态**由界面那台状态机给
   * —— 两样分得清，谁也不冒充谁。
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
  const logError = summary.logError();
  const summaryFailureText =
    summaryState.phase === 'failure' && summaryState.failure !== null
      ? summaryFailureView(summaryState.failure, summaryState.elapsedSeconds)
      : null;

  return (
    <>
      {/* 心跳失联：全屏横幅盖在任何状态之上；下面的整块告警区这时候是收着的 */}
      {lost && (
        <div className="heartbeat" role="alert">
          <div className="heartbeat__title">录音已中断</div>
          <div className="heartbeat__body">{lostView.body}</div>
          <div className="heartbeat__do">
            先不要关机，也不要关闭本页。到服务那边确认一下：
            <span className="num">Ctrl+C</span> 停掉再敲一次 <span className="num">meeting web</span>
            {logPath === null ? '' : <>；日志在 <span className="num">{logPath}</span></>}。
          </div>
        </div>
)}

      {/* 常驻工作区栏：会中屏与工作台共用同一条左栏；只有「开始录音」受单场限制 */}
      <div className="wsbar-page">
      <WorkspaceRail
        sessions={wsSessions}
        selection={wsSelection}
        postJobs={postJobs}
        gpu={gpu}
        busy={busy}
        onRowActivate={switchWorkspace}
        onReload={reloadWs}
        onFailure={setFailure}
        detail={
          wsRow === null ? null : (
            <WsBarDetail
              key={wsRow.dir}
              row={wsRow}
              recording={snapshot.session}
              busy={busy}
              onStart={() => runActionQuietly('start', { dir: wsRow.dir, mode: 'both' })}
              onOpenSession={() => switchWorkspace(snapshot.session?.dir ?? wsRow.dir)}
            />
)
        }
      />
      <div className="wsbar-main">
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
      <LiveTopBar
        snapshot={snapshot}
        view={view}
        lost={lost}
        recoveredGapSeconds={recoveredGapSeconds}
        busy={busy}
        shownCombo={shownCombo}
        gpu={gpu}
        onCombo={onCombo}
        onStart={() => runActionQuietly('start', { mode: 'both' })}
        onStop={() => runActionQuietly('stop')}
        onSetting={(key, value) =>
          runActionQuietly('settings_update', { [key]: value })
        }
        onReprobe={() => runActionQuietly('reprobe')}
      />

      <CaptureBand
        channels={view.channels}
        summary={summary}
        summaryReady={summaryReady}
        summaryWhyText={summaryWhyText}
        summaryLogOpen={summaryLogOpen}
        onToggleSummaryLog={() => setSummaryLogOpen((open) => !open)}
      />

      <SummaryPanels
        summary={summary}
        summaryState={summaryState}
        summaryFailureText={summaryFailureText}
        logView={logView}
        logError={logError}
        summaryLogOpen={summaryLogOpen}
        onCloseLog={() => setSummaryLogOpen(false)}
      />

      {/* 告警区：失联时整块收掉（两句话不得同屏）；采集已停那一条也不与致命横幅同屏 */}
      {!view.alertsHidden && (
        <AlertsArea
          view={view}
          heartbeat={heartbeat}
          logPath={logPath}
          busy={busy}
          logShown={logShown}
          onToggleLog={() => setLogShown((shown) => !shown)}
          onResume={() => runActionQuietly('resume')}
          onStop={() => runActionQuietly('stop')}
          onMute={() => runActionQuietly('mute_session')}
        />
)}

      {/* 动作失败与拒绝：按 code 分派。它排在告警区外面：失联时那一整块要收掉（两句话不同屏） */}
      {failure !== null && (
        <section className="band band--alert" aria-label="失败">
          <FailureBlock failure={failure} logPath={logPath} extraLines={drainFailedSteps} />
        </section>
)}

      {/* 会议资料：会中把图片 / 文件放进这一场的 meeting_materials/；还没有场次时这一块不摆 */}
      {sessionDir !== null && <MaterialsPanel dir={sessionDir} />}

      <TranscriptArea rows={rows} started={started} health={snapshot.transcript.health} />
      </main>
      </div>
      </div>
    </>
);
}
