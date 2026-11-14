import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';

/**
 * 工作台首页（空闲与已结束的那一屏）：左边"我这些会"，右边选中的那一场。
 *
 * 界面手上**只有服务给的原文**：注册表来自 `GET /api/sessions`（界面只读、不落任何本地存储）；
 * 会后在进度区读 `GET /api/sessions/{id}`。会中那一路（`start` 之后）由外面那层闸（`HomeGate`）
 * 切到 `LiveView`，本组件不碰会中屏。
 *
 * 会后进度区**不开 SSE 通道**，按心跳的样板自排轮询（有活动才开）。
 */

import type { ActionFailureView } from '../../failure';
import { FailureBlock } from '../../failure-ui';
import { ConfirmDialog } from '../../dialogs';
import { MaterialsPanel } from '../../materials/MaterialsPanel';
import { WorkspaceRail } from '../WorkspaceRail';
import { chainView } from '../chain';
import { drainStripView } from '../drain';
import {
  DEFAULT_TRANSCRIPT_KIND,
  kindViewOf,
  type TranscriptKind,
} from '../transcripts';
import {
  createSessionStatusPoller,
  fetchSessionTranscripts,
  SessionStatusHttpError,
  type SessionStatusTick,
} from '../../api/sessions';
import { clampHeartbeatInterval } from '../../api/heartbeat';
import { HEARTBEAT_INTERVAL_DEFAULT_SECONDS } from '../../contract';
import { emptyStateLines, segmentsView, startButtonLabel, startWhy } from '../model';
import {
  advanceChunkRows,
  chunkLedgerReset,
  gpuBusyView,
  initialChunkRows,
  progressView,
  rowStatusView,
  shouldWatch,
  type ChunkRow,
} from '../progress';
import {
  POST_MINUTES_ACTION,
  POST_OFFLINE_ACTION,
  postButtonsView,
  postStopView,
} from '../post';
import { namesRows } from '../names';
import type {
  Ended,
  SessionRow,
  SessionStatus,
  SessionTranscripts,
  SessionsResponse,
} from '../../contract';
import { useWorkspaceActions, type ConfirmState } from './useWorkspaceActions';
import { ProgressArea } from './ProgressArea';
import { ScriptsArea } from './ScriptsArea';
import { SpeakerNamesDialog } from './SpeakerNamesDialog';

/** 开始钮的悬停小注（按不动时换成那句原因）。 */
const RECORD_HINT = '在这一场工作区里开始一段新录音；已有段落会保留，续接在它们之后';

/** 开始钮在「起录在飞」那一档的文案。 */
const STARTING_LABEL = '正在启动…';

/** 快照 `ended` 的转手包：落盘那一小块的读数来源。 */
export interface HomeDrain {
  ended: Ended;
  dir: string | null;
  logPath: string | null;
}

export function WorkspaceHome(props: {
  /** 心跳周期（会后进度区的轮询跟着同一个节奏）。 */
  heartbeatSeconds?: number;
  /** 快照 `ended` 的转手包；`null` = 快照上没有收尾这件事。 */
  drain?: HomeDrain | null;
}): ReactNode {
  const { heartbeatSeconds, drain = null } = props;
  const [sessions, setSessions] = useState<SessionsResponse | null>(null);
  const [selection, setSelection] = useState<string | null>(null);
  const [failure, setFailure] = useState<ActionFailureView | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [namesOpen, setNamesOpen] = useState(false);

  /** 最近一份单场读数（换选中就清零，权威照旧在服务侧）。 */
  const [status, setStatus] = useState<SessionStatus | null>(null);
  /** 轮询/取数失败那一行（进度区里如实画，不顶掉首页的失败块）。 */
  const [statusError, setStatusError] = useState<string | null>(null);
  const [chunkRows, setChunkRows] = useState<ChunkRow[]>([]);
  /** 这一场的稿子正文（换选中就清零）。 */
  const [transcripts, setTranscripts] = useState<SessionTranscripts | null>(null);
  /** 取稿失败那一行（如实画在稿子那一块里，不顶掉首页的失败块）。 */
  const [transcriptsError, setTranscriptsError] = useState<string | null>(null);
  /** 切在哪一份稿（换选中回默认，不落本地存储）。 */
  const [srcKind, setSrcKind] = useState<TranscriptKind>(DEFAULT_TRANSCRIPT_KIND);
  /** 上一份读数（逐块行按两次读数的差值追加）。 */
  const prevStatusRef = useRef<SessionStatus | null>(null);
  const selectionRef = useRef<string | null>(null);
  /** 读数回调走 ref 转手：轮询器只造一次，回调永远是最新的那份逻辑。 */
  const handleTickRef = useRef<(tick: SessionStatusTick) => void>(() => undefined);
  const pollerRef = useRef<ReturnType<typeof createSessionStatusPoller> | null>(null);
  if (pollerRef.current === null) {
    pollerRef.current = createSessionStatusPoller((tick) => handleTickRef.current(tick));
  }

  /** 稿子取数：走 ref 转手，换场之后的迟到应答不顶掉新场那一块（`selectionRef` 判场）。 */
  const loadTranscriptsRef = useRef<(dir: string) => void>(() => undefined);
  loadTranscriptsRef.current = (dir: string): void => {
    void fetchSessionTranscripts(dir).then(
      (data) => {
        if (selectionRef.current !== dir) return;
        setTranscripts(data);
        setTranscriptsError(null);
      },
      (error) => {
        if (selectionRef.current !== dir) return;
        setTranscripts(null);
        setTranscriptsError(error instanceof Error ? error.message : String(error));
      },
);
  };
  /** 上一跳的会后在飞记号（翻假的那一跳重取稿子）。 */
  const offlineFlyingRef = useRef(false);

  const selected: SessionRow | null = (sessions?.sessions ?? []).find(
    (row) => row.dir === selection,
) ?? null;
  const postJobs = sessions?.post_jobs ?? [];

  const {
    busy,
    starting,
    postNote,
    loadList,
    reloadAfterAction,
    stopPost,
    transcodeAct,
    startSelected,
    postSelected,
    renameSpeaker,
    confirmStartInterrupt,
  } = useWorkspaceActions({
    sessions,
    selection,
    selectionRef,
    postJobs,
    pollerRef,
    setSessions,
    setSelection,
    setFailure,
    setConfirm,
  });

  // 挂载即取一次列表：首次打开 / 整页刷新都能见到列表或空态。
  // [] 只在挂载跑一次；loadList 只经由 setter 依赖最新 state，无闭包陈旧问题。
  useEffect(() => {
    void loadList();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** 一跳读数进来：逐块行按差值追加（记录已重置时整段重播），读数整块替换。 */
  const handleTick = (tick: SessionStatusTick): void => {
    if (tick.kind === 'error') {
      // 404 档（目录没了 / 不在清单里）：重试也是同一句，停表并如实画出 —— 停表语义只有 404 一档。
      // 其余档（网络层瞬态等）轮询器自己接着跳，这里只画错误行 —— 一跳失败就停表会把进度区冻死。
      if (tick.error instanceof SessionStatusHttpError) {
        pollerRef.current?.stop();
        statusFatalRef.current = true;
      }
      setStatusError(tick.error.message);
      return;
    }
    if (tick.dir !== selectionRef.current) return;
    setStatusError(null);
    statusFatalRef.current = false;
    const before = prevStatusRef.current;
    if (
      before === null ||
      before.dir !== tick.status.dir ||
      chunkLedgerReset(before, tick.status)
) {
      setChunkRows(initialChunkRows(tick.status));
    } else {
      setChunkRows((rows) => [...rows, ...advanceChunkRows(rows, before, tick.status)]);
    }
    prevStatusRef.current = tick.status;
    setStatus(tick.status);
    // 会后转写从「在飞」翻「收工」的那一跳重取稿子：会后稿刚落盘就读得到。
    const flying = tick.status.offline.in_process;
    if (offlineFlyingRef.current && !flying) {
      loadTranscriptsRef.current(tick.status.dir);
    }
    offlineFlyingRef.current = flying;
  };
  handleTickRef.current = handleTick;

  // 换选中（含首次定选中）：进度区清零，立刻问一次 —— 不等第一个周期。
  useEffect(() => {
    selectionRef.current = selected?.dir ?? null;
    prevStatusRef.current = null;
    statusFatalRef.current = false;
    offlineFlyingRef.current = false;
    setStatus(null);
    setStatusError(null);
    setChunkRows([]);
    // 稿子那一块跟着换场清零、回到默认（会中稿），并按新场重取一跳。
    setTranscripts(null);
    setTranscriptsError(null);
    setSrcKind(DEFAULT_TRANSCRIPT_KIND);
    if (selected !== null) {
      loadTranscriptsRef.current(selected.dir);
      void pollerRef.current?.tick(selected.dir);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selection]);

  // 有活动才持续轮询：联结命中（会后那趟在跑）或读数说「真在跑 / 停着 running」。
  // 从「在刷」翻到「不用刷」的那一下顺手刷一遍列表 —— post_job 那格与行四态要跟上。
  const watch = selected !== null && shouldWatch(selected, postJobs, status);
  const watchRef = useRef(false);
  /** 心跳补问用的「此刻 post_job 命中的目录」：ref 转手，interval 闭包不握挂表那一刻的旧对象。 */
  const postDirRef = useRef<string | null>(null);
  /** 单场读数已进 404 档的记档：重试也是同一句，心跳不再补问；成功读数、换选中、新一趟起跑三处清掉。 */
  const statusFatalRef = useRef(false);
  useEffect(() => {
    const poller = pollerRef.current;
    if (poller === null) return;
    const was = watchRef.current;
    watchRef.current = watch;
    if (watch && selected !== null) {
      poller.setIntervalFromSnapshot(heartbeatSeconds ?? 0);
      poller.start(selected.dir);
    } else {
      poller.stop();
      if (was) void loadList();
    }
    return () => poller.stop();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watch, selection, heartbeatSeconds]);

  // post_job 出现在选中行上、而轮询还没起时的补跳：手上那份读数多半是上一趟留下的旧终态，
  // 主动问一跳 —— 读数到手后 `shouldWatch()` 照**新读数**判。不命中选中行不问（那一半由
  // postFlying 的列表心跳接住）。
  const postDir = postJobs[0]?.dir ?? null;
  postDirRef.current = postDir;
  useEffect(() => {
    if (postDir === null || watch) return;
    if (selectionRef.current !== postDir) return;
    statusFatalRef.current = false;
    void pollerRef.current?.tick(postDir);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [postDir]);

  // 会后在飞期间给列表一条心跳节奏的重取：post 命中的不是选中行时，没有单场读数可等 ——
  // 跑完那一刻服务把 post_job 翻 null，只有重取列表才知道，gpucell 与行四态才不会楔死在
  // 「跑着」。deps 挂布尔不挂对象：每次重取 post_job 都是新对象，挂对象会把周期永远重排。
  const postFlying = postJobs.length > 0;
  useEffect(() => {
    if (!postFlying) return;
    const seconds = clampHeartbeatInterval(heartbeatSeconds ?? HEARTBEAT_INTERVAL_DEFAULT_SECONDS);
    const timer = setInterval(() => {
      void loadList();
      const dir = postDirRef.current;
      if (
        dir !== null &&
        selectionRef.current === dir &&
        !watchRef.current &&
        !statusFatalRef.current
) {
        void pollerRef.current?.tick(dir);
      }
    }, seconds * 1000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [postFlying, heartbeatSeconds]);

  const whyForSelected = selected === null ? null : startWhy(selected);
  const post = selected === null ? null : postButtonsView(selected);
  const lines = emptyStateLines();
  /** 全局「显卡忙不忙」：会中模型在这一屏必关着（闸只在 idle / 收尾落定后进本屏）。 */
  const gpu = gpuBusyView(postJobs, false);
  /** 进度区的读数（读不出来就画失败那一行）。 */
  const prog = status === null ? null : progressView(status);
  /** 停止入口：这一场的 offline 在飞时出现在进度区头行。 */
  const stop = postStopView(postJobs, selected?.dir ?? null);

  /** 事后工序链：录音结束过的那一场才摆；读数要等到单场读数到手、且它说的正是选中这一场。 */
  const chain =
    selected !== null &&
    selected.state === 'ended' &&
    status !== null &&
    status.dir === selected.dir
      ? chainView({ drain, status, selectedDir: selected.dir })
      : null;
  /** 稿子那一块的读数（切在哪一份就给哪一份的）。 */
  const srcView = transcripts === null ? null : kindViewOf(transcripts, srcKind);

  const isEmpty = sessions !== null && sessions.sessions.length === 0;

  /** 落盘那一小块：快照 `ended` 说的是快照里那一场收尾，只在选中它时显示。 */
  const strip =
    drain !== null && selected !== null && selected.dir === drain.dir
      ? drainStripView(drain.ended)
      : null;

  return (
    <main className="shell page--home" data-top-state="idle">
      <section className="home">
        {/* 左栏：这一台机器上有哪些工作区。它与会中屏共用同一个组件 */}
        <WorkspaceRail
          sessions={sessions}
          selection={selection}
          postJobs={postJobs}
          gpu={gpu}
          busy={busy}
          onRowActivate={setSelection}
          onReload={reloadAfterAction}
          onFailure={setFailure}
        />

        <div className="home__aside">
          {isEmpty ? (
            <div className="ws-empty">
              <div className="ws-empty__logo" aria-hidden="true">
                ◇
              </div>
              <div className="ws-empty__title">{lines.title}</div>
              <div className="ws-empty__body">{lines.body}</div>
            </div>
) : selected === null ? (
            <p className="hint">尚未选择工作区。</p>
) : (
            <div className="wspane">
              {/* 面板头：这一场是谁 + 拿它录一段。动作钮作用的正是左边选中那一行 */}
              <div className="wspane__head">
                <div className="wspane__title">{selected.session_id ?? selected.dir}</div>
                <div className="wspane__headacts">
                  <button
                    className="btn btn--primary btn--start"
                    type="button"
                    disabled={busy || starting || whyForSelected !== null}
                    title={whyForSelected ?? RECORD_HINT}
                    onClick={startSelected}
                  >
                    {starting ? STARTING_LABEL : startButtonLabel(selected)}
                  </button>
                  {whyForSelected !== null && (
                    <p className="wspane__why">{whyForSelected}</p>
)}
                </div>
              </div>
              {/* 工作区分两栏：这一栏说「这一场是谁」，下一栏说「拿它做什么」 */}
              <div className="wspane__read">
              <p className="kv">
                <span className="kv__k">目录</span>
                <span className="kv__v num">{selected.dir}</span>
              </p>
              {(() => {
                const cell = rowStatusView(selected, postJobs);
                return (
                  <p className="kv">
                    <span className="kv__k">状态</span>
                    <span className="kv__v">
                      <span className={`glyph ${cell.mark}`} aria-hidden="true" />
                      {cell.label}
                    </span>
                  </p>
);
              })()}
              {/* 落盘那一小块：「录音已结束」之下的一小段进度，不阻塞任何东西；失败也只是把原因、命令与 log 写全 */}
              {strip !== null && (
                <div className="wsdrain" aria-label="落盘进度" data-settled={strip.settled ? 'true' : 'false'}>
                  <p className="wsdrain__line">
                    <span
                      className={`glyph ${
                        strip.failed ? 'glyph--dashed' : strip.settled ? 'glyph--done' : 'glyph--draining'
                      }`}
                      aria-hidden="true"
                    />
                    {strip.headline}
                  </p>
                  {strip.steps.length > 0 && (
                    <p className="wsdrain__steps num">
                      {strip.steps.map((step) => `${step.label}：${step.status}`).join(' · ')}
                    </p>
)}
                  {(strip.error !== null || strip.failures.length > 0) && (
                    <div className="wsdrain__bad">
                      {strip.error !== null && <p className="wsdrain__fail">{strip.error}</p>}
                      {strip.failures.map((line) => (
                        <p className="wsdrain__fail" key={line}>
                          {line}
                        </p>
))}
                      {/* 兜底：可复制的命令与 log 路径（绝不说「已落盘」） */}
                      <code className="codeblock">meeting post {selected.dir}</code>
                      {drain?.logPath != null && (
                        <>
                          <code className="codeblock">{drain.logPath}</code>
                          <p className="wsdrain__fail">log：{drain.logPath}</p>
                        </>
)}
                    </div>
)}
                </div>
)}
              {selected.date !== null && (
                <p className="kv">
                  <span className="kv__k">日期</span>
                  <span className="kv__v num">{selected.date}</span>
                </p>
)}
              {/* 各段的时长与时间（续接过的会在这里看得出接过） */}
              {(() => {
                const segs = segmentsView(selected);
                if (segs === null) return null;
                return (
                  <>
                    <p className="kv">
                      <span className="kv__k">录音</span>
                      <span className="kv__v num">{segs.total}</span>
                    </p>
                    {segs.lines.map((line) => (
                      <p className="kv" key={line.label}>
                        <span className="kv__k">{line.label}</span>
                        <span className="kv__v num">
                          {line.time} · {line.seconds}
                        </span>
                      </p>
))}
                  </>
);
              })()}

              {/* 会后那两个按钮跟读数住同一栏：左栏是「这一场是谁 + 拿它做什么」，右栏是「会后跑得怎么样」 */}
              {post !== null && (
                <div className="wspane__post" aria-label="会后">
                  <div className="wspane__posttitle">会后</div>
                  <div className="wspane__postacts">
                    <button
                      className="btn"
                      type="button"
                      disabled={busy || !post.canPostOffline}
                      title="用离线模型重新转写整段录音，产出带说话人编号的会后精细稿；耗时较长，占用显卡"
                      onClick={() => postSelected(POST_OFFLINE_ACTION)}
                    >
                      生成会后精细稿
                    </button>
                    <button
                      className="btn"
                      type="button"
                      disabled={busy || !post.canPostMinutes}
                      title="把该工作区现有的稿子交给 DSH 生成纪要；缺少会后精细稿时按现有档位处理"
                      onClick={() => postSelected(POST_MINUTES_ACTION)}
                    >
                      生成纪要
                    </button>
                  </div>
                  {post.offlineNotice !== null && (
                    <p className="wspane__postline">{post.offlineNotice}</p>
)}
                  {post.why !== null && <p className="wspane__why">{post.why}</p>}
                  {postNote !== null && <p className="wspane__postline">{postNote}</p>}
                  {/* 说话人名字：会后区的一行入口，点开小弹窗当场改名 */}
                  <p className="wspane__postline">
                    <button
                      className="btn btn--small btn--ghost"
                      type="button"
                      disabled={busy || post.why !== null}
                      title={
                        post.why ??
                        '为该工作区的说话人命名；名字随该工作区目录保存，生成纪要时自动带上'
                      }
                      onClick={() => setNamesOpen(true)}
                    >
                      说话人名字…
                    </button>
                  </p>
                </div>
)}
              </div>

              <div className="wspane__work">
              {/* 事后工序链：落盘 → 转码 → 会后转写 → 纪要，每段一格现状 + 读数；家务进度不用告警带或致命色 */}
              {chain !== null && (
                <div className="wschain" aria-label="事后工序">
                  <p className="wschain__walk">
                    <span className={`glyph ${chain.walk.mark}`} aria-hidden="true" />
                    {chain.walk.line}
                  </p>
                  <ol className="wschain__list">
                    {chain.segments.map((segment) => (
                      <li
                        className="wschain__seg"
                        key={segment.id}
                        data-seg={segment.id}
                        data-running={segment.running ? 'true' : undefined}
                        data-failed={segment.failed ? 'true' : undefined}
                      >
                        <span className="wschain__name">{segment.name}</span>
                        <span className="wschain__line">
                          {segment.mark === null ? (
                            <span className="spinner" aria-hidden="true" />
) : (
                            <span className={`glyph ${segment.mark}`} aria-hidden="true" />
)}
                          {segment.line}
                        </span>
                        {segment.pause !== null &&
                         (segment.pause.canPause || segment.pause.canResume) && (
                          <button
                            className="btn btn--small btn--ghost wschain__act"
                            type="button"
                            disabled={busy}
                            title={
                              segment.pause.canPause
                                ? '在当前段结束后暂停；已转好的段凭凭据保留'
                                : '从凭据记下的那一段接着转'
                            }
                            onClick={() =>
                              transcodeAct(
                                segment.pause?.canPause ? 'transcode_pause' : 'transcode_resume',
)
                            }
                          >
                            {segment.pause?.canPause ? '暂停' : '继续'}
                          </button>
)}
                      </li>
))}
                  </ol>
                </div>
)}

              {/* 稿子正文：两份稿平级，见 `./ScriptsArea`；这一场录完（`ended`）才摆 */}
              {selected.state === 'ended' && (
                <ScriptsArea
                  srcView={srcView}
                  error={transcriptsError}
                  srcKind={srcKind}
                  onSrcKind={setSrcKind}
                />
)}

              {/* 会后进度区：可展开收缩；读数没到手或取数出事时也摆 */}
              {(prog !== null || statusError !== null) && (
                <ProgressArea
                  prog={prog}
                  statusError={statusError}
                  stop={stop}
                  busy={busy}
                  chunkRows={chunkRows}
                  onStop={stopPost}
                />
)}

              {/* 会议资料：这一场的 meeting_materials/。粘贴图片、拖入文件、提交文字、选盘上的文件 */}
              <MaterialsPanel dir={selected.dir} />
              </div>
            </div>
)}
          {failure !== null && (
            <FailureBlock
              failure={failure}
              logPath={null}
              extraLines={[]}
              /** forbidden 档只有 why + next 两行，不带说明行。 */
              forbiddenNote={false}
            />
)}
        </div>
      </section>

      {confirm !== null && confirm.what === 'start-interrupt' && (
        <ConfirmDialog
          title="启动新会议会打断会后总结，是否继续"
          text="「生成会后精细稿」正在运行。继续将先停止该任务（已完成的块保留在逐块记录中，可重新运行），等它停下后开始新一场录音；取消则不执行任何操作。"
          confirmLabel="打断并开始录音"
          busy={busy}
          onConfirm={() => confirmStartInterrupt(confirm.dir)}
          onCancel={() => setConfirm(null)}
        />
)}

      {namesOpen && (
        <SpeakerNamesDialog
          rows={namesRows(status)}
          busy={busy}
          onRename={renameSpeaker}
          onClose={() => setNamesOpen(false)}
        />
)}
    </main>
);
}
