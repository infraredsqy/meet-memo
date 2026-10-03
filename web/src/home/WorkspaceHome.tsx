import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';

/**
 * 工作台首页（空闲态那一屏）：左边"我这些会"，右边选中的那一场。
 *
 * 界面手上**只有服务给的原文**：注册表来自 `GET /api/sessions`（服务侧配置目录里那份
 * `workspaces.json`，界面只读、不落任何本地存储 —— 契约章 6 第 2 条）；目录选择器来自
 * `GET /api/dirs`（起点用户主目录）；打开/收编/删除走 `POST /api/actions` 的
 * `workspace_open` / `workspace_remove`。会中那一路（`start` 之后）由外面那层闸
 * （`App.tsx`）切到 `LiveView`，本组件不碰会中屏。
 *
 * 会后进度区（R27，节点 52b24392）读 `GET /api/sessions/{id}`：**不开 SSE 通道**，
 * 按心跳的样板自排轮询（`api/sessions.ts` 的轮询器，有活动才开），逐块行往下刷、
 * 带进度条、可展开收缩；左清单每行四态与全局「显卡忙不忙」一格的读法在 `home/progress.ts`。
 */

import { postAction } from '../api';
import { confirmWorkspaceOpen } from './confirmWorkspaceOpen';
import {
  browseDirs,
  createSessionStatusPoller,
  fetchSessions,
  SessionStatusHttpError,
  type SessionStatusTick,
} from '../api/sessions';
import { clampHeartbeatInterval } from '../api/heartbeat';
import { HEARTBEAT_INTERVAL_DEFAULT_SECONDS } from '../contract';
import { decisionFor, emptyStateLines, initialSelection, segmentsView, startButtonLabel, startWhy } from './model';
import {
  advanceChunkRows,
  chunkLedgerReset,
  chunkRowLabel,
  gpuBusyView,
  initialChunkRows,
  phaseMark,
  progressView,
  rowStatusView,
  shouldWatch,
  type ChunkRow,
} from './progress';
import {
  POST_MINUTES_ACTION,
  POST_OFFLINE_ACTION,
  POST_STOP_ACTION,
  postButtonsView,
  postIdleNote,
  postStopView,
} from './post';
import { SPEAKER_RENAME_ACTION, duplicateNameHolders, namesRows, type NamesRowView } from './names';
import { failureFrom, type ActionFailureView } from '../failure';
import { FailureBlock } from '../failure-ui';
import type { DirsResponse, SessionRow, SessionStatus, SessionsResponse } from '../contract';

// ---------------------------------------------------------------------------
// 小料
// ---------------------------------------------------------------------------

/** 一只确认弹窗（非空目录当工作区 / 从列表移除共用这一只）。 */
function ConfirmDialog({
  title,
  text,
  confirmLabel,
  busy,
  onConfirm,
  onCancel,
}: {
  title: string;
  text: string;
  confirmLabel: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}): ReactNode {
  return (
    <div className="dlg__layer" role="presentation">
      <div className="dlg" role="dialog" aria-modal="true" aria-label={title}>
        <div className="dlg__title">{title}</div>
        <div className="dlg__text">{text}</div>
        <div className="dlg__acts">
          <button className="btn" type="button" disabled={busy} onClick={onCancel}>
            取消
          </button>
          <button className="btn btn--primary" type="button" disabled={busy} onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

/** 目录选择器弹窗：起点用户主目录（服务给 `home`），`up` 回上层。只挑**已有**目录。 */
function DirPicker({
  browse,
  onBrowse,
  onPick,
  onClose,
}: {
  browse: DirsResponse | null;
  onBrowse: (dir: string | null) => void;
  /** 点一项：这一项的 dir 交给调用方，按它的名目走三档（本弹窗不开动作）。 */
  onPick: (item: { dir: string; kind: 'empty' | 'meeting' | 'unknown' | 'missing' }) => void;
  onClose: () => void;
}): ReactNode {
  return (
    <div className="dlg__layer" role="presentation">
      <div className="dlg dlg--picker" role="dialog" aria-modal="true" aria-label="选择已有目录">
        <div className="dlg__title">选择已有目录（起点主目录）</div>
        <p className="hint">起点是用户主目录；选择器只用来挑、软件不建目录。</p>
        <div className="picker__list" role="listbox" aria-label="这一层的子目录">
          {browse === null && <p className="empty">正在取目录…</p>}
          {browse !== null && browse.dirs.length === 0 && (
            <p className="empty">这一层没有子目录。</p>
          )}
          {browse !== null &&
            browse.dirs.map((item) => (
              <div
                key={item.dir}
                className="picker__row"
                role="option"
                aria-selected="false"
                tabIndex={0}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') onPick(item);
                }}
              >
                <button className="picker__pick" type="button" onClick={() => onPick(item)}>
                  <span className="picker__name">{item.name}</span>
                  <span className="picker__kind">
                    {item.kind === 'empty'
                      ? '空目录'
                      : item.kind === 'meeting'
                        ? item.suspected
                          ? '疑似会议目录'
                          : '会议目录'
                        : '普通目录'}
                  </span>
                </button>
                <button
                  className="btn btn--small btn--ghost"
                  type="button"
                  title="把这一层换成这个目录，继续往深处挑（review-2 m1）"
                  onClick={() => onBrowse(item.dir)}
                >
                  进这一层
                </button>
              </div>
            ))}
        </div>
        <div className="dlg__acts">
          <button
            className="btn"
            type="button"
            disabled={browse === null || browse.up === null}
            onClick={() => browse !== null && onBrowse(browse.up)}
          >
            向上一层
          </button>
          <button className="btn btn--primary" type="button" onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * 一只改名字的弹窗（人名节点 `91b6752e`，R10 第四条 / R30）：一行一个身份
 * （路标签 + 编号 + 现名/待确认），输入框 + 确认，动作回包即重问单场状态（当场生效）。
 *
 * 身份读数来自服务（单场状态的 `speakers` 一格），名字跟着这一场的目录走——界面
 * 只递话，落盘在服务侧那一半。两个编号叫同一个名合法：撞名时提示一句
 * 「编号是分开的，可以重名」，提交照走（ui-design §5.2 的既有口径）；这句话只在
 * 动作**成功之后**才说——失败那一档弹窗不留成功话术（review-1 m3）。
 */
function SpeakerNamesDialog({
  rows,
  busy,
  onRename,
  onClose,
}: {
  rows: NamesRowView[];
  busy: boolean;
  /** 确认一颗：打改名动作 + 重问单场状态；失败画在首页的失败块，弹窗只清提示、收忙。 */
  onRename: (key: string, name: string) => Promise<void>;
  onClose: () => void;
}): ReactNode {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const confirmRow = (row: NamesRowView): void => {
    const name = (drafts[row.key] ?? '').trim();
    if (!name) {
      setNote('先给这一行写一个名字；不想起就关掉这一窗（跳过 = 不起名）。');
      return;
    }
    setPending(row.key);
    void onRename(row.key, name).then(
      () => {
        // 撞名提示挪进成功分支（review-1 m3）：动作结果落地之前不冒「已提交」。
        setNote(
          duplicateNameHolders(rows, row.key, name).length > 0
            ? '编号是分开的，可以重名；已照你写的名字提交。'
            : null,
        );
        setPending(null);
        setDrafts((current) => ({ ...current, [row.key]: '' }));
      },
      () => {
        // 失败清掉撞名提示、不留成功话术：失败归首页的失败块如实说（review-1 m3）。
        setNote(null);
        setPending(null);
      },
    );
  };

  return (
    <div className="dlg__layer" role="presentation">
      <div className="dlg" role="dialog" aria-modal="true" aria-label="说话人名字">
        <div className="dlg__title">说话人名字</div>
        <p className="hint">
          名字跟着这一场的目录走，下一次出纪要时附录里那一格照这里确认过的名字写；编号带路，两路的「1 号」各是各的。
        </p>
        {rows.length === 0 && (
          <p className="empty">这一场盘上还没有可起名的身份（录过的场次才有本地 / 远端或编号）。</p>
        )}
        {rows.map((row) => (
          <div className="picker__row" key={row.key}>
            <button className="picker__pick" type="button" onClick={() => confirmRow(row)}>
              <span className="picker__name num">{row.title}</span>
              <span className="picker__kind">{row.name ?? '待确认'}</span>
            </button>
            <input
              className="field__input"
              type="text"
              value={drafts[row.key] ?? ''}
              placeholder={row.name ?? '待确认'}
              aria-label={`${row.title} 的名字`}
              disabled={pending !== null || busy}
              onChange={(event) => {
                const value = event.target.value;
                setDrafts((current) => ({ ...current, [row.key]: value }));
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter') confirmRow(row);
              }}
            />
            <button
              className="btn btn--small btn--primary"
              type="button"
              disabled={pending !== null || busy}
              onClick={() => confirmRow(row)}
            >
              确认
            </button>
          </div>
        ))}
        {note !== null && <p className="hint">{note}</p>}
        <div className="dlg__acts">
          <button className="btn" type="button" disabled={pending !== null} onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * 名目 → 挑了之后走哪一档（spec 三情形的唯一出处是 `api/sessions.ts` 的 `probeDecision()`，
 * 这里只转手）。`missing` 名目到不了选择器——`GET /api/dirs` 只摆存在的目录；真到了不走
 * 确认弹窗，显式失败（不可当作可收编入口，见 `openFromPicker`）。
 */
type PickPath = { dir: string; kind: 'empty' | 'meeting' | 'unknown' | 'missing' };

/** 两个按钮的动作名 → 界面上的名字（与 `docs/requirements.md` R26 的叫法逐字对齐）。 */
const POST_LABELS: Readonly<Record<string, string>> = {
  [POST_OFFLINE_ACTION]: '生成会后精细稿',
  [POST_MINUTES_ACTION]: '直接出纪要',
};

/**
 * 待确认的三只弹窗：收编非空目录 / 从列表移除 / 「新会议会打断正在跑的会后」。
 * 打断那只只在 **offline 档在飞**时摆出来（minutes 在飞没有东西会被打断，不弹）；
 * `viaConfirm` 记登记走哪半边——非空目录收编那半边要带 `confirm: true`（R20），
 * 空/会议目录那半边走普通登记。
 */
type ConfirmState =
  | { what: 'unknown'; dir: string }
  | { what: 'remove'; dir: string }
  | { what: 'post-interrupt'; dir: string; viaConfirm: boolean };

// ---------------------------------------------------------------------------
// 首页本体
// ---------------------------------------------------------------------------

export function WorkspaceHome({
  heartbeatSeconds,
}: { heartbeatSeconds?: number } = {}): ReactNode {
  const [sessions, setSessions] = useState<SessionsResponse | null>(null);
  const [selection, setSelection] = useState<string | null>(null);
  const [failure, setFailure] = useState<ActionFailureView | null>(null);
  const [busy, setBusy] = useState(false);
  // 选择器的两层弹窗与待确认的一只
  const [picker, setPicker] = useState(false);
  const [browse, setBrowse] = useState<DirsResponse | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  /** 选择器取数失败的暂存（弹窗里画一行，不顶掉首页的失败块）。 */
  const [browseError, setBrowseError] = useState<string | null>(null);
  /** 会后那两个按钮的动作回执（一句话：这一趟已起 / 被拒的原因在失败块里）。 */
  const [postNote, setPostNote] = useState<string | null>(null);
  /** 说话人名字那只弹窗开没有（R10 第四条：唯一要动手的地方，入口在会后区那一行）。 */
  const [namesOpen, setNamesOpen] = useState(false);

  // -- 会后进度区（R27）：单场读数 + 逐块行 + 展开/收缩 ---------------------
  /** 最近一份单场读数（选中那一场的；换选中就清零，权威照旧在服务侧）。 */
  const [status, setStatus] = useState<SessionStatus | null>(null);
  /** 轮询/取数失败那一行（进度区里如实画，不顶掉首页的失败块）。 */
  const [statusError, setStatusError] = useState<string | null>(null);
  /** 逐块行（每跑完一块落一行；第一次读到时把已跑过的补成 `observed` 行）。 */
  const [chunkRows, setChunkRows] = useState<ChunkRow[]>([]);
  /** 进度区展开没有（纯界面偏好，只活在这一页的内存里，不落本地存储）。 */
  const [progOpen, setProgOpen] = useState(true);
  /** 上一份读数（逐块行按两次读数的差值追加）与「此刻选中的目录」。 */
  const prevStatusRef = useRef<SessionStatus | null>(null);
  const selectionRef = useRef<string | null>(null);
  /** 读数回调走 ref 转手：轮询器只造一次，回调永远是最新的那份逻辑。 */
  const handleTickRef = useRef<(tick: SessionStatusTick) => void>(() => undefined);
  const pollerRef = useRef<ReturnType<typeof createSessionStatusPoller> | null>(null);
  if (pollerRef.current === null) {
    pollerRef.current = createSessionStatusPoller((tick) => handleTickRef.current(tick));
  }

  // -- 取数 ---------------------------------------------------------------

  /** 刷一遍注册表列表；首次加载时顺带按服务给的 `recent` 定选中行。 */
  const loadList = (): Promise<SessionsResponse | null> =>
    fetchSessions().then(
      (data) => {
        setSessions(data);
        setSelection((current) =>
          current === null ? initialSelection(data.sessions, data.recent) : current,
        );
        return data;
      },
      (error) => {
        setFailure(failureFrom(error));
        return null;
      },
    );

  const reloadAfterAction = (): void => {
    setBusy(true);
    void loadList().then(
      () => setBusy(false),
      () => setBusy(false),
    );
  };

  // 挂载即取一次列表（review-2 C1）：不靠动作回调顺带刷出，首次打开/整页刷新都能
  // 见到列表或空态；与 SSE 快照竞速无碍 —— loadList 自己 setSessions。
  // [] 只在挂载跑一次；loadList 只经由 setter 依赖最新 state，无闭包陈旧问题。
  useEffect(() => {
    void loadList();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // -- 会后进度区的轮询（有活动才问，不空转）---------------------------------

  const selected: SessionRow | null = (sessions?.sessions ?? []).find(
    (row) => row.dir === selection,
  ) ?? null;
  const postJob = sessions?.post_job ?? null;

  /** 一跳读数进来：逐块行按差值追加（首跳把已跑过的补成行；账已重置——重跑那一跳
   * ——整段重播，review-2 m2），读数整块替换。 */
  const handleTick = (tick: SessionStatusTick): void => {
    if (tick.kind === 'error') {
      // 404 档（目录没了 / 不在清单里）：重试也是同一句，停表并如实画出；顺带记一档
      // （m1），postFlying 心跳的重问点照它跳过 —— 停表语义只有 404 一档。
      // 其余档（网络层瞬态等）轮询器自己接着跳（api.test.ts 钉住的契约「失败跳照交
      // 不停表」），这里只画错误行 —— 一跳失败就停表会把进度区冻死（review-1 m1）。
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
  };
  handleTickRef.current = handleTick;

  // 换选中（含首次定选中）：进度区清零，立刻问一次 —— 不等第一个周期。
  // （`tick()` 自己会把读数交给 onTick，与轮询循环同一入口。）
  useEffect(() => {
    selectionRef.current = selected?.dir ?? null;
    prevStatusRef.current = null;
    statusFatalRef.current = false;
    setStatus(null);
    setStatusError(null);
    setChunkRows([]);
    if (selected !== null) {
      void pollerRef.current?.tick(selected.dir);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selection]);

  // 有活动才持续轮询：联结命中（会后那趟在跑）或读数说「真在跑 / 停着 running」。
  // 从「在刷」翻到「不用刷」的那一下顺手刷一遍列表 —— post_job 那格与行四态要跟上。
  const watch = selected !== null && shouldWatch(selected, postJob, status);
  const watchRef = useRef(false);
  /** 心跳补问用的「此刻 post_job 命中的目录」：ref 转手，interval 闭包不握挂表那一刻
   * 的旧对象（m1，review-3）。 */
  const postDirRef = useRef<string | null>(null);
  /** 单场读数已进 404 档的记档：重试也是同一句，心跳不再补问（维持 review-1 m1 的
   * 停表语义）；成功读数、换选中、新一趟起跑三处清掉。 */
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

  // C1（review-2）补跳：post_job 出现在选中行上、而轮询还没起 —— 手上那份读数多半是
  // 上一趟留下的旧终态，`shouldWatch()` 的联结分支照它判只能翻 false；watch-effect 的
  // 依赖（watch 值没变）也不会重跑，post_job 出现这件事原先没有任何机制补一跳 tick。
  // 这里在「联结命中选中行 ∧ watch 仍假」时主动问一跳：读数到手后 shouldWatch 照
  // **新读数**判 —— 在飞 → watch 翻真、轮询起（最多一个周期内进度区开始跟随）；若这一趟
  // 已在按下与补跳之间跑完 → 读到终态、watch 仍假，无害（不空转）。依赖挂 post_job 的
  // dir（同一趟不变，翻 null 或换场才重跑），防重复补跳；不命中选中行不问（那一半由
  // postFlying 的列表心跳接住）。
  // m1（review-3）：这一跳遇瞬态失败后 postDir 不变、watch 仍假 —— 重问点由下面
  // postFlying 的列表心跳接住（同一趟内按心跳节奏自动补问，最多一个心跳周期内自愈），
  // 这里仍是起跑的那一跳；新一趟起跑时把 404 记档清掉，别让上一趟的停表挡这一趟。
  const postDir = postJob?.dir ?? null;
  postDirRef.current = postDir;
  useEffect(() => {
    if (postDir === null || watch) return;
    if (selectionRef.current !== postDir) return;
    statusFatalRef.current = false;
    void pollerRef.current?.tick(postDir);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [postDir]);

  // 会后在飞期间给列表一条心跳节奏的重取（review-1 C1 的第 2 条路）：post 命中的
  // 不是选中行时，没有单场读数可等 —— 跑完那一刻服务把 post_job 翻 null，只有重取
  // 列表才知道，gpucell 与行四态才不会楔死在「跑着」。post_job 清 null 即停。
  // deps 挂布尔不挂对象：每次重取 post_job 都是新对象，挂对象会把周期永远重排。
  // m1（review-3）：顺这条心跳把补跳的重问点接住 —— 补跳那一跳遇瞬态失败后，postDir
  // 不变（补跳 effect 不重跑）、watch 仍假（轮询不起），原先进度区整趟停死；这里守卫
  // 「联结命中的还是选中行 ∧ 轮询没起 ∧ 未进 404 记档」就再问一跳，失败后最多一个
  // 心跳周期内自愈、无需用户动作；watch 翻真（轮询接管）或 post_job 翻 null（本
  // effect 拆掉）自然停。判等全走 ref：闭包里的 postJob 是挂 interval 那一刻的对象。
  const postFlying = postJob !== null;
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

  // -- 动作 ----------------------------------------------------------------

  /**
   * 「新会议」的登记入口。**offline 档会后在跑时先弹一句确认**（R28 / 设计意图第
   * 13 条）：「启动新会议会打断会后总结，是否继续」——在飞的是 minutes 时**不弹**
   * （没有东西会被打断；服务对 minutes 的停止也如实拒，d3），登记直接走。非空目录
   * （need-confirm）先按 R20 问收编，过了之后再按同一条件问打断（两问都过才登记）。
   * 不确认就什么都不发；确认之后先打停止动作（受理即回，不等块边界），再走原来的
   * 登记。读数是顶层 `post_job`，服务端对登记动作不设闸 —— 拦与放都在这一只弹窗里。
   */
  const openDir = (
    dir: string,
    kindHint: 'empty' | 'meeting' | 'need-confirm',
  ): void => {
    if (kindHint === 'need-confirm') {
      setConfirm({ what: 'unknown', dir });
      return;
    }
    if (postJob !== null && postJob.kind === 'offline') {
      setConfirm({ what: 'post-interrupt', dir, viaConfirm: false });
      return;
    }
    openDirNow(dir);
  };

  /** 登记本体（确认弹窗放行之后走这里；`workspace_open` 动作 + 失败块 + 刷新）。 */
  const openDirNow = (dir: string): void => {
    setFailure(null);
    setBusy(true);
    void postAction('workspace_open', { dir })
      .then(
        () => undefined,
        (error) => setFailure(failureFrom(error)),
      )
      .then(() => {
        setBusy(false);
        reloadAfterAction();
      });
  };

  /**
   * 停掉正在跑的「生成会后精细稿」（R28 / b1c6a58e）：打一只 `post_stop`，受理即回。
   * 界面随即靠轮询看到状态翻成「被打断」、按钮回到可按。
   */
  const stopPost = (): void => {
    if (postJob === null || postJob.kind !== 'offline') return;
    setFailure(null);
    setPostNote(null);
    setBusy(true);
    void postAction(POST_STOP_ACTION, { dir: postJob.dir })
      .then(
        () =>
          setPostNote(
            '停止已受理：这一趟会在当前块收尾之后停下（盘上状态写成「被打断」）。',
          ),
        (error) => setFailure(failureFrom(error)),
      )
      .then(() => {
        setBusy(false);
        reloadAfterAction();
      });
  };

  const startSelected = (): void => {
    if (selection === null) return;
    const row = sessions?.sessions.find((item) => item.dir === selection) ?? null;
    if (row === null || startWhy(row) !== null) return;
    setFailure(null);
    setBusy(true);
    void postAction('start', { dir: row.dir, mode: 'both' })
      .then(
        () => undefined,
        (error) => setFailure(failureFrom(error)),
      )
      .then(() => {
        setBusy(false);
        reloadAfterAction();
      });
  };

  /**
   * 会后那两个按钮：各自打一个动作，回执只说「这一趟跑的是哪一场」。
   *
   * 起成功之后**不当场宣称跑完了**（那一趟可能跑一小时），只把动作回执如实写在下面，
   * 并重取一遍列表（`state` / `has_offline` 这些现场读数都从服务来）。
   */
  const postSelected = (action: string): void => {
    if (selection === null) return;
    setFailure(null);
    setPostNote(null);
    setBusy(true);
    void postAction(action, { dir: selection })
      .then(
        () => setPostNote(`${POST_LABELS[action]}：这一趟已起（跑成什么样由这一场盘上的状态回答）。`),
        (error) => {
          setFailure(failureFrom(error));
          setPostNote(null);
        },
      )
      .then(() => {
        setBusy(false);
        reloadAfterAction();
      });
  };

  /**
   * 说话人改名（`speaker_rename`，4.11）：打一只动作写这一场目录里的人名表，
   * 回包后**重问一次单场状态**——`speakers` 一格带上新名字，弹窗当场生效。
   * 失败画在首页的失败块，并把失败**以拒绝如实交回弹窗**（review-1 m3）——
   * 弹窗那侧清掉自己的提示、收回忙，不再把成功话术留在失败那一档。
   */
  const renameSpeaker = (key: string, name: string): Promise<void> => {
    if (selection === null) return Promise.resolve();
    setFailure(null);
    return postAction(SPEAKER_RENAME_ACTION, { dir: selection, key, name }).then(
      () => undefined,
      (error) => {
        setFailure(failureFrom(error));
        throw error;
      },
    ).then(() => {
      const dir = selectionRef.current;
      if (dir !== null) void pollerRef.current?.tick(dir);
    });
  };

  // -- 选择器换层 -----------------------------------------------------------

  const browseInto = (dir: string | null): void => {
    setBrowse(null);
    setBrowseError(null);
    void browseDirs(dir).then(
      (data) => setBrowse(data),
      (error) => {
        const view = failureFrom(error);
        setBrowseError(
          view.kind === 'envelope' || view.kind === 'drain' ? view.message : view.text
        );
      },
    );
  };

  // -- 画 -------------------------------------------------------------------

  const whyForSelected = selected === null ? null : startWhy(selected);
  const post = selected === null ? null : postButtonsView(selected);
  const lines = emptyStateLines();
  /** 全局「显卡忙不忙」：会中模型在这一屏必关着（闸只在 idle / 收尾落定后进本屏）。 */
  const gpu = gpuBusyView(postJob, false);
  /** 进度区的读数（有读数才算；读不出来就画失败那一行）。 */
  const prog = status === null ? null : progressView(status);
  const progMark = prog === null ? null : phaseMark(prog.phase);
  /** 停止入口（R28 / b1c6a58e）：这一场的 offline 在飞时出现在进度区头行。 */
  const stop = postStopView(postJob, selected?.dir ?? null);

  const openFromPicker = (item: { dir: string; kind: PickPath['kind'] }): void => {
    setPicker(false);
    if (item.kind === 'missing') {
      // 理论上不可达（选择器只摆存在的目录）；真到达就如实失败，不该弹「确认收编」。
      setFailure(failureFrom(new Error(`该目录已不存在：${item.dir}`)));
      return;
    }
    openDir(item.dir, decisionFor(item.kind));
  };

  return (
    <main className="shell page--home" data-top-state="idle">
      <header className="topbar">
        <div className="topbar__left">
          <span className="title-md">会议工作台</span>
        </div>
        <div className="topbar__main">
          <button
            className="btn btn--primary btn--start"
            type="button"
            disabled={busy}
            onClick={() => {
              setPicker(true);
              browseInto(null);
            }}
          >
            新会议
          </button>
          <button
            className="btn"
            type="button"
            disabled={busy || selected === null || whyForSelected !== null}
            onClick={startSelected}
          >
            {selected === null ? '开始录音' : startButtonLabel(selected)}
          </button>
        </div>
        <div className="topbar__right">
          {/* 全局只留这一格「显卡忙不忙」（R31）：读法与判据在 `home/progress.ts` 的
              `gpuBusyView()`。全站没有全局进度条 —— 进度跟着各自的工作区走。 */}
          <span
            className={`gpucell ${gpu.busy ? 'gpucell--busy' : ''}`}
            title={gpu.note}
            data-busy={gpu.busy ? 'true' : 'false'}
          >
            <span className={`glyph ${gpu.mark}`} aria-hidden="true" />
            {gpu.label}
          </span>
          <span className="hint">清单住在服务端 · 界面只读</span>
        </div>
      </header>

      <section className="home">
        <aside className="home__list" aria-label="工作区列表">
          {sessions === null && failure === null && <p className="hint">正在取列表…</p>}
          {sessions !== null && sessions.sessions.length === 0 && (
            <div className="ws-empty">
              <div className="ws-empty__logo" aria-hidden="true">
                ◇
              </div>
              <div className="ws-empty__title">{lines.title}</div>
              <div className="ws-empty__body">{lines.body}</div>
            </div>
          )}
          {sessions !== null && sessions.sessions.length > 0 && (
            <div className="list wslist" role="listbox" aria-label="这些会">
              {sessions.sessions.map((row) => {
                const cell = rowStatusView(row, postJob);
                return (
                <div
                  key={row.dir}
                  className={`wslist__row ${row.dir === selection ? 'wslist__row--on' : ''}`}
                  role="option"
                  aria-selected={row.dir === selection}
                  tabIndex={0}
                  onClick={() => setSelection(row.dir)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') setSelection(row.dir);
                  }}
                >
                  <span className="wslist__name">{row.session_id ?? row.dir}</span>
                  <span className="wslist__path num">{row.dir}</span>
                  {/* 行四态（R27）：字 + 图形两条通道并存，颜色只是冗余的那一半。
                      判据在 `home/progress.ts` 的 `rowStatusView()`。 */}
                  <span
                    className={`wslist__state wslist__state--${cell.tone} ${
                      cell.dim ? 'wslist__state--dim' : ''
                    }`}
                  >
                    {cell.mark === null ? (
                      <span className="spinner" aria-hidden="true" />
                    ) : (
                      <span className={`glyph ${cell.mark}`} aria-hidden="true" />
                    )}
                    {cell.label}
                  </span>
                  <button
                    className="btn btn--small btn--ghost wslist__remove"
                    type="button"
                    title="只从列表移除，文件一个字节不动；以后可用「新会议」收回"
                    disabled={busy}
                    onClick={(event) => {
                      event.stopPropagation();
                      setConfirm({ what: 'remove', dir: row.dir });
                    }}
                  >
                    删除
                  </button>
                </div>
                );
              })}
            </div>
          )}
        </aside>

        <div className="home__aside">
          {selected === null ? (
            <p className="hint">左边挑一场，或按「新会议」。</p>
          ) : (
            <div className="wspane">
              <div className="wspane__title">{selected.session_id ?? selected.dir}</div>
              <p className="kv">
                <span className="kv__k">目录</span>
                <span className="kv__v num">{selected.dir}</span>
              </p>
              {(() => {
                const cell = rowStatusView(selected, postJob);
                return (
                  <p className="kv">
                    <span className="kv__k">状态</span>
                    <span className="kv__v">
                      {cell.mark === null ? (
                        <span className="spinner" aria-hidden="true" />
                      ) : (
                        <span className={`glyph ${cell.mark}`} aria-hidden="true" />
                      )}
                      {cell.label}
                    </span>
                  </p>
                );
              })()}
              {selected.date !== null && (
                <p className="kv">
                  <span className="kv__k">日期</span>
                  <span className="kv__v num">{selected.date}</span>
                </p>
              )}
              {/* 各段的时长与时间（续接过的会在这里看得出接过）：盘上段账的原文读数，
                  合计只数写完的段——权威时长是各段在录时长之和（设计意图第 7 条）。 */}
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
              {whyForSelected !== null && <p className="wspane__why">{whyForSelected}</p>}

              {post !== null && (
                <div className="wspane__post" aria-label="会后">
                  <div className="wspane__posttitle">会后</div>
                  <div className="wspane__postacts">
                    <button
                      className="btn btn--primary"
                      type="button"
                      disabled={busy || !post.canPostOffline}
                      title="走离线模型把整段录音重跑一遍，出带说话人编号的那一份稿；慢，占显卡"
                      onClick={() => postSelected(POST_OFFLINE_ACTION)}
                    >
                      生成会后精细稿
                    </button>
                    <button
                      className="btn"
                      type="button"
                      disabled={busy || !post.canPostMinutes}
                      title="把这一场手上有的稿子交给 DSH 出纪要；缺会后稿就按现有档位来"
                      onClick={() => postSelected(POST_MINUTES_ACTION)}
                    >
                      直接出纪要
                    </button>
                  </div>
                  <p className="wspane__postline">{post.offlineNotice}</p>
                  {post.why !== null && <p className="wspane__why">{post.why}</p>}
                  {postNote !== null && <p className="wspane__postline">{postNote}</p>}
                  {/* 说话人名字（R10 第四条）：会后区的一行入口，点开小弹窗当场改名。
                      这一场还在录 / 收尾没落定 / 目录不在原地时与两个按钮同判按不动。 */}
                  <p className="wspane__postline">
                    <button
                      className="btn btn--small btn--ghost"
                      type="button"
                      disabled={busy || post.why !== null}
                      title={
                        post.why ??
                        '给这一场的说话人起名；名字跟着这一场的目录走，下一次出纪要自动带上'
                      }
                      onClick={() => setNamesOpen(true)}
                    >
                      说话人名字…
                    </button>
                  </p>
                  <p className="hint">{postIdleNote()}</p>
                </div>
              )}

              {/* 会后进度区（R27）：可展开收缩；展开是逐块行往下刷 + 每路一条的进度条。
                  读数是 `GET /api/sessions/{id}` 那只纯读端点的原文，轮询只在有活动时开。
                  「停着一版 running」按 `in_process` 分层如实画，不替盘上的话下结论。 */}
              {(prog !== null || statusError !== null) && (
                <div className="wsprog" aria-label="会后进度">
                  <div className="wsprog__head">
                    <span className="wsprog__title">会后进度</span>
                    {prog !== null && (
                      <span className="wsprog__phase" data-phase={prog.phase}>
                        {progMark === null ? (
                          <span className="spinner" aria-hidden="true" />
                        ) : (
                          <span className={`glyph ${progMark}`} aria-hidden="true" />
                        )}
                        {prog.phaseLabel}
                      </span>
                    )}
                    {/* 停止入口（R28 / b1c6a58e）：只在这一场的 offline 在飞时出现。
                        受理即回——当前块收尾之后停，盘上状态写成「被打断」。 */}
                    {stop.visible && (
                      <button
                        className="btn btn--small btn--ghost wsprog__stop"
                        type="button"
                        disabled={busy}
                        title="停在当前块收尾之后；已跑过的块留在逐块账里，重新按按钮可从头再跑"
                        onClick={stopPost}
                      >
                        停止
                      </button>
                    )}
                    <button
                      className="btn btn--small btn--ghost wsprog__toggle"
                      type="button"
                      aria-expanded={progOpen}
                      onClick={() => setProgOpen((open) => !open)}
                    >
                      {progOpen ? '收起' : '展开'}
                    </button>
                  </div>
                  {/* 错误行不进收起的那一半：收起态也要看得见「读数出事了」（review-1 m1 顺带） */}
                  {statusError !== null && <p className="wsprog__row wsprog__row--bad">{statusError}</p>}
                  {progOpen && (
                    <div className="wsprog__body">
                      {prog !== null && (
                        <>
                          <div className="wsprog__roads">
                            {prog.roads.map((road) => (
                              <div className="wsprog__road" key={road.source} data-road={road.source}>
                                <span className="wsprog__roadname">{road.label}</span>
                                <div
                                  className="wsprog__bar"
                                  role="progressbar"
                                  aria-label={`${road.label}这一路跑到第 ${road.done} 块`}
                                  aria-valuemin={0}
                                  aria-valuemax={100}
                                  aria-valuenow={road.percent ?? undefined}
                                >
                                  {/* 电平块的同一套手法：读数只写 CSS 变量，长短归样式 */}
                                  <span
                                    className="wsprog__fill"
                                    style={{ '--p': `${road.percent ?? 0}%` } as CSSProperties}
                                  />
                                </div>
                                <span className="wsprog__frac num">
                                  {road.total === null ? `${road.done}/?` : `${road.done}/${road.total}`}
                                </span>
                              </div>
                            ))}
                          </div>
                          <div className="wsprog__rows" role="list" aria-label="逐块记录">
                            {chunkRows.length === 0 && (
                              <p className="wsprog__row wsprog__row--empty">
                                还没有块跑完的记录（从这一页打开起开始记）。
                              </p>
                            )}
                            {chunkRows.map((item) => (
                              <p className="wsprog__row" role="listitem" key={item.key}>
                                {chunkRowLabel(item)}
                              </p>
                            ))}
                          </div>
                          <p className="hint">
                            已跑 {prog.counts.transcribed + prog.counts.silent + prog.counts.failed + prog.counts.unjudged} 块 ·
                            跑成 {prog.counts.transcribed} · 静音 {prog.counts.silent} · 失败 {prog.counts.failed} ·
                            未判 {prog.counts.unjudged}
                            {prog.elapsedSeconds === null
                              ? ''
                              : ` · 已跑 ${Math.round(prog.elapsedSeconds)} 秒`}
                            ；块数是给人看的读数，不作判据（设计意图第 6、7 条）。
                          </p>
                        </>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
          {failure !== null && (
            <FailureBlock
              failure={failure}
              logPath={null}
              extraLines={[]}
              /** forbidden 档照 HomeFailure 的旧画法只有 why + next 两行，不带说明行。 */
              forbiddenNote={false}
            />
          )}
        </div>
      </section>

      {picker && (
        <DirPicker
          browse={browse}
          onBrowse={browseInto}
          onPick={openFromPicker}
          onClose={() => {
            setPicker(false);
            setBrowse(null);
          }}
        />
      )}
      {picker && browseError !== null && (
        <div className="dlg__layer" role="presentation">
          <div className="dlg">
            <div className="dlg__title">目录浏览失败</div>
            <div className="dlg__text">{browseError}</div>
            <div className="dlg__acts">
              <button className="btn" type="button" onClick={() => setPicker(false)}>
                关闭
              </button>
            </div>
          </div>
        </div>
      )}

      {confirm !== null && confirm.what === 'unknown' && (
        <ConfirmDialog
          title="把非空目录当工作区？"
          text="这个目录不是空的、里面也没有会议材料。确认的话，它会被登记进左边的清单（目录里的文件原样不动）。"
          confirmLabel="确认收编"
          busy={busy}
          onConfirm={() => {
            const dir = confirm.dir;
            setConfirm(null);
            // 收编这一问过了（R20）；offline 档在飞时接着问打断（review-1 m2）——
            // 两问都过才登记，任何一问取消都零发送。
            if (postJob !== null && postJob.kind === 'offline') {
              setConfirm({ what: 'post-interrupt', dir, viaConfirm: true });
              return;
            }
            setFailure(null);
            setBusy(true);
            void confirmWorkspaceOpen(dir, {
              onFail: setFailure,
              reload: () => {
                setBusy(false);
                reloadAfterAction();
              },
            });
          }}
          onCancel={() => setConfirm(null)}
        />
      )}

      {confirm !== null && confirm.what === 'remove' && (
        <ConfirmDialog
          title="从列表移除这一场？"
          text="只把它从左边的清单里拿掉；工作区目录与里面所有文件一个字节不动。之后随时可用「新会议」把它收回来。"
          confirmLabel="从列表移除"
          busy={busy}
          onConfirm={() => {
            const dir = confirm.dir;
            setConfirm(null);
            setFailure(null);
            setBusy(true);
            void postAction('workspace_remove', { dir })
              .then(
                () => undefined,
                (error) => setFailure(failureFrom(error)),
              )
              .then(() => {
                setBusy(false);
                reloadAfterAction();
              });
          }}
          onCancel={() => setConfirm(null)}
        />
      )}

      {confirm !== null && confirm.what === 'post-interrupt' && (
        <ConfirmDialog
          title="启动新会议会打断正在跑的会后，是否继续？"
          text="上一场的「生成会后精细稿」还在跑。继续的话先停掉它——已跑完的块留在逐块账里，盘上状态写成「被打断」，以后可以重新按按钮从头再跑——然后登记你挑的这个目录。取消就什么都不发生。"
          confirmLabel="打断并继续"
          busy={busy}
          onConfirm={() => {
            const { dir, viaConfirm } = confirm;
            setConfirm(null);
            setFailure(null);
            setBusy(true);
            // 确认 = 先停、再登记（R28）：停止动作受理即回（不等块边界），登记照走。
            // 弹窗只在 offline 档在飞时出现；这只守卫接的是「弹窗开着的那一会儿，
            // 在飞那趟自己收了工」——此刻不发 stop、登记照走。minutes 在飞不弹此窗
            // （openDir 已收窄，没有东西会被打断）。登记按来路走：非空目录收编那半边
            // 带 `confirm: true`（R20 / review-1 m2），其余走普通登记。
            const flying = postJob;
            const stopFirst =
              flying !== null && flying.kind === 'offline'
                ? postAction(POST_STOP_ACTION, { dir: flying.dir }).then(
                    () => undefined,
                    (error) => setFailure(failureFrom(error)),
                  )
                : Promise.resolve();
            void stopFirst.then(() => {
              if (viaConfirm) {
                void confirmWorkspaceOpen(dir, {
                  onFail: setFailure,
                  reload: () => {
                    setBusy(false);
                    reloadAfterAction();
                  },
                });
                return;
              }
              openDirNow(dir);
            });
          }}
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
