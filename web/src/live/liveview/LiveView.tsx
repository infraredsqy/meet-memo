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
 *
 * 区块各归各的子组件（同目录）：顶栏 `./LiveTopBar`、采集带与总结入口 `./CaptureBand`、
 * 总结三块面板 `./SummaryPanels`、告警区 `./AlertsArea`、转写区 `./TranscriptArea`、
 * 左栏小详情 `./WsBarDetail`；本组件持全部状态与接线，只做装配。
 */

/**
 * 会中主视图（由 `home/HomeGate` 在非空闲形势下渲染出去）。
 *
 * **SSE 那条唯一连接在 `App.tsx` 顶层**：全页恰一个 `EventSource`（web-homegate-dual-sse）。
 * LiveView 只经 `events` 转手源注册五路回调 —— 挂件晚于首帧时，源侧把积压帧补递上来；
 * 卸载即清空。心跳那条轮询仍由本组件自持（照旧）。
 */
export function LiveView({
  snapshot: snapshotProp,
  events,
}: {
  snapshot: Snapshot;
  events: EventsSource;
}): ReactNode {
  /** 服务推过来的那一份。界面手上这份是**副本**，分歧以服务为准。 */
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
  /**
   * 正在切去的那个组合（`c9f5d04d` 的受理反馈）：按下这一刻就把高亮交给它，
   * 快照没回来的半途界面照样自洽（高亮在目标档上，别的档不亮也不至于「没反应」）。
   * 收摊的时机有两个 —— 推来的快照真落到这一档（落地），或这一趟动作失败
   * （没落成，高亮退回快照说的那一档，原因交给失败块）。
   */
  const [pendingCombo, setPendingCombo] = useState<CaptureCombo | null>(null);
  const [logShown, setLogShown] = useState(false);
  /** 本场总结记录那一块开着没有（结果面板与它是两块内容，各自关各自的）。 */
  const [summaryLogOpen, setSummaryLogOpen] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  /**
   * 工作区注册表的那一份（`GET /api/sessions` 的应答原文）：顶层 `post_jobs` 喂全局
   * 「显卡忙不忙」那一格；清单整份喂左栏那条**常驻工作区栏**（d61829f1：会中屏与
   * 工作台共用同一条栏 —— 录音时能看清单、能切到别的工作区看详情、能添加工作区）。
   * 之前这一屏只留 `post_jobs`，且硬编码 `null` 显假「闲」（review-1 m2）——
   * 同一服务的同一份读数，按下面的心跳节奏伸手取即可。
   */
  const [wsSessions, setWsSessions] = useState<SessionsResponse | null>(null);
  /** 工作区栏的选区（左栏清单里点中的那一场；默认落在最近一次被打开的那一场）。 */
  const [wsSelection, setWsSelection] = useState<string | null>(null);
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

  // SSE 那条唯一连接的接线：挂载即往源侧注册四路回调 —— 晚于首帧的积压帧在
  // 注册这一刻补递上来（快照以源侧的最后一份为准），卸载即清空（转手结构见
  // `home/events-source.ts`）。连接本体的 connect/close 归 `App` 顶层；
  // 连接掉线的「出事了」由心跳失联横幅说，界面不另摆一格连接读数。
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

  // 心跳那条自持的轮询（照旧）：周期来自快照的 settings（见下面那条 effect）。
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
        // 恢复：转写区在中断那一格留一条断口（设计章 4.5）。
        // 形状照 `insertGap()` / `gapRow()` 给的那一档：`[HH:MM–HH:MM] 数据已停` ——
        // 起于冻结那一刻（frozenAt），止于恢复这一刻
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

  // 工作区栏读数（review-1 m2 起，d61829f1 扩成整份清单）：挂上这一屏就按心跳节奏取
  // 一遍 `GET /api/sessions`，把顶层 `post_jobs` 喂给全局那格「显卡忙不忙」、清单整份
  // 喂给左栏那条常驻工作区栏。取不到的那一跳（瞬态失联）手上这份照旧，下一跳再试；
  // 周期与心跳同一个可配节奏。首份到手时给选区落座：最近一次被打开的那一场
  // （取数与选区落座的接线与工作台共用同一份：`../../home/sessions-list`）。
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

  // 快速总结的缺省档同样来自快照的 settings（`311f84f5`）：预选跟着服务走，
  // 用户显式挑过的档不被覆盖（判据在 summary-controller 的 applyDefaultWindow 里）。
  useEffect(() => {
    if (snapshot === null) return;
    summary.applyDefaultWindow(normalizeDefaultWindow(snapshot.settings.summary_default_window));
  }, [snapshot, summary]);

  // 换新场次时清掉上一场的增量流与转写窗口：免得旧电平、旧正文顶着新状态。
  // 总结区那两样（这一次的应答 / 本场记录那一份）跟着一起收：新的一场从零开始，
  // 服务侧 `start_session()` 也把缓冲与记录一起清了（phase2 的「缓冲与生命周期」）。
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
   * 切工作区（d61829f1）：发**既有**的 `workspace_open` 登记动作（不造新端点），
   * 成功后才把详情切过去 —— 登记失败（目录没了这类）详情留在原处，失败块说原因。
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
   * 四组合直切（`c9f5d04d`）：映射表差几步发几步，按次序发、中途失败即停。
   *
   * 「现状」在半途要读对：上一趟还没落地（`pendingCombo` 有值）时快照还是旧的，
   * 现状改读按下那一档所代表的那份 —— 连切几次才不会串态（spec 完成判定第一条）。
   * 重按当前档映射表给空表，一个请求都不发；受理反馈靠 `pendingCombo` 即时点亮
   * （快照没回来，界面也不读成「没反应」）。
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
   * 「数据已停」＝失联（界面看不见服务）或采集已停（服务在应答但没有帧）。
   *
   * 这两档为真时两路电平、15 根包络、读数**全部冻在被冻那一刻**（设计章 4.5：
   * 不把估出来的数据继续画成在动）：下面的效果把手上的帧按住，视图只吃按住的
   * 那一份；恢复（失联解除 / 采集恢复）那一刻松手，下一帧起立刻回到实时。
   */
  const dataStopped = lostPredicate(heartbeat) ||
    (snapshot !== null &&
      captureStalled(reply, snapshot.top_state, heartbeat.stalledBeats) !== null);

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

  const lost = lostPredicate(heartbeat);
  const stalled = view?.stalled ?? null;
  const logPath = snapshot?.service.log_path ?? null;
  // 全局「显卡忙不忙」（R31）：忙 = 有模型开着 —— 快照说录着（含告警）是会中那一半，
  // `GET /api/sessions` 顶层的 `post_jobs` 是会后那一半（上面的 effect 按心跳节奏取；
  // 转码那趟不吃显卡，gpuBusyView 不把它数进来）。收尾窗口那一档（ended + 收尾未落定）
  // recording 已假，这一格靠 post_jobs 答忙闲，不再显假「闲」（review-1 m2）。
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
          <div className="heartbeat__do">
            先不要关机，也不要关闭本页。到服务那边确认一下：
            <span className="num">Ctrl+C</span> 停掉再敲一次 <span className="num">meeting web</span>
            {logPath === null ? '' : <>；日志在 <span className="num">{logPath}</span></>}。
          </div>
        </div>
      )}

      {/* 常驻工作区栏（d61829f1）：会中屏与工作台共用同一条左栏 —— 录音时能看清单、
          能切到别的工作区看详情、能添加工作区；只有「开始录音」受单场限制。
          会中自己的内容仍读作居中的 1440 单列（右边那格 `.wsbar-main` 里的 `.shell`）。 */}
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

      {/* 动作失败与拒绝：按 code 分派，detail 有哪个键显示哪个键（契约 6.7）。
          它排在告警区外面：失联时那一整块要收掉（两句话不同屏），
          而动作失败讲的是「刚才这一下没成」这件事 */}
      {failure !== null && (
        <section className="band band--alert" aria-label="失败">
          <FailureBlock failure={failure} logPath={logPath} extraLines={drainFailedSteps} />
        </section>
      )}

      {/* 会议资料（160d1a78）：会中把图片 / 文件放进这一场的 meeting_materials/。
          目标目录 = 当前场次（资料跟着场次走，哪怕录制中照收）；还没有场次时这一块
          不摆 —— 没有目标就没有入口。页面级粘贴由面板自己挂在 document 上。 */}
      {sessionDir !== null && <MaterialsPanel dir={sessionDir} />}

      <TranscriptArea rows={rows} started={started} health={snapshot.transcript.health} />
      </main>
      </div>
      </div>
    </>
  );
}
