import { useState, type Dispatch, type RefObject, type SetStateAction } from 'react';
import { postAction } from '../../api';
import type { SessionStatusPoller } from '../../api/sessions';
import { failureFrom, type ActionFailureView } from '../../failure';
import type { PostJobCell, SessionsResponse } from '../../contract';
import { runAction } from '../dispatch';
import { loadSessionList } from '../sessions-list';
import { startWhy } from '../model';
import { POST_MINUTES_ACTION, POST_OFFLINE_ACTION, POST_STOP_ACTION } from '../post';
import { SPEAKER_RENAME_ACTION } from '../names';

/** 两个按钮的动作名 → 界面上的名字（与 `docs/requirements.md` R26 的叫法逐字对齐）。 */
const POST_LABELS: Readonly<Record<string, string>> = {
  [POST_OFFLINE_ACTION]: '生成会后精细稿',
  [POST_MINUTES_ACTION]: '生成纪要',
};

/**
 * 纪要回执 `inputs` 四档 → 人话（`docs/contract.md` 4.3：受理那一刻读到的输入档位）。
 * 服务只给枚举值，标签由界面翻（契约章 6：按有哪格画哪格）。
 */
const POST_INPUT_LABELS: Readonly<Record<string, string>> = {
  both: '会中稿 + 会后稿',
  live_only: '只有会中稿',
  offline_only: '只有会后稿',
  none: '会中稿与会后稿都没有',
};

/**
 * 待确认的一只弹窗：「启动新会议会打断会后总结」。打断那只挂在**开始录音**上、
 * 只在 **offline 档在飞**时摆出来（唯一硬闸：会后精细稿与会中录音不并排；
 * minutes / 转码在飞没有东西会被打断，不弹）；`dir` 是确认之后要开始的那一场。
 * （收编 / 移除那两只确认住在左栏 `../WorkspaceRail` 里。）
 */
export type ConfirmState = { what: 'start-interrupt'; dir: string };

/** 起录重试的窗口：还没停到块边界就被 409 打回时，隔一拍再试（超了如实画失败块）。 */
const START_RETRY_LIMIT = 60;
const START_RETRY_MS = 500;

export interface WorkspaceActionsInput {
  /** 注册表清单（`startSelected` 从它找选中那一行）。 */
  sessions: SessionsResponse | null;
  selection: string | null;
  /** 「此刻选中的目录」的 ref 转手（settle 回调闭包里拿最新的一份）。 */
  selectionRef: RefObject<string | null>;
  /** 顶层 `post_jobs`（offline 在飞读数与打断确认的 flying 查找都从它来）。 */
  postJobs: PostJobCell[];
  /** 单场轮询器（settle 里重问读数用）。 */
  pollerRef: RefObject<SessionStatusPoller | null>;
  setSessions: Dispatch<SetStateAction<SessionsResponse | null>>;
  setSelection: Dispatch<SetStateAction<string | null>>;
  setFailure: Dispatch<SetStateAction<ActionFailureView | null>>;
  setConfirm: Dispatch<SetStateAction<ConfirmState | null>>;
}

/**
 * 工作台的六类动作派发收在这一个 hook 里：停会后精细稿（`post_stop`）、转码暂停 /
 * 继续（`transcode_pause` / `transcode_resume`）、起录（`start`）、打断后起录（`start`
 * 带 409 重试窗）、会后两钮（`post_offline` / `post_minutes`）、说话人改名
 * （`speaker_rename`）。忙闩（`busy`）、「起录在飞」一档（`starting`）与动作回执句
 * （`postNote`）是这六类共用的三样状态，也归这里持有。
 *
 * 派发骨架在 `../dispatch`；清单取数（`loadList`）与落定后的重取
 * （`reloadAfterAction`）是几类动作共用的两脚，一并住在这里。
 */
export function useWorkspaceActions({
  sessions,
  selection,
  selectionRef,
  postJobs,
  pollerRef,
  setSessions,
  setSelection,
  setFailure,
  setConfirm,
}: WorkspaceActionsInput) {
  const [busy, setBusy] = useState(false);
  /**
   * 「起录在飞」一档（spec per-session-model-reload-starves-capture 修法二）：`busy`
   * 在起录那两趟（`startNow` / `startAfterInterrupt`）上的细分。置位在派发前
   * （`startNow` 走 `before`，打断确认那趟在入口置位、跨整个重试窗），收回只在
   * **落定**（成功或没被接走的失败；被打回重试的那一拍不走尾段，忙与这一档都保持）。
   * 在飞期间开始钮画成「正在启动…」并保持禁用；成功落定之后由 SSE 快照翻
   * `recording`、既有闸（`HomeGate`）切会中屏 —— 那是「正在录制」的确认时刻，这里
   * 不新增任何状态（顶层 `TOP_STATE` 四态与服务端推送都纹丝不动）。
   */
  const [starting, setStarting] = useState(false);
  /**
   * 会后那两个按钮的动作回执（一句话：这一趟已起；`post_minutes` 的回执带 `inputs` /
   * `note` 时把输入档位与说破句也画进来；被拒的原因在失败块里）。
   */
  const [postNote, setPostNote] = useState<string | null>(null);

  /** 会后精细稿（offline 档）有没有在飞：唯一硬闸的读数（弹窗与服务侧 409 同源）。 */
  const offlineFlying = postJobs.find((job) => job.kind === 'offline') ?? null;

  // -- 取数 ---------------------------------------------------------------

  /** 刷一遍注册表列表；首次加载时顺带按服务给的 `recent` 定选中行
   *  （接线与选区落座和会中屏共用同一份：`../sessions-list`）。 */
  const loadList = (): Promise<SessionsResponse | null> =>
    loadSessionList({
      onList: setSessions,
      onSelection: setSelection,
      onFail: (error) => setFailure(failureFrom(error)),
    });

  const reloadAfterAction = (): void => {
    setBusy(true);
    void loadList().then(
      () => setBusy(false),
      () => setBusy(false),
    );
  };

  // -- 动作 ----------------------------------------------------------------

  /**
   * 「新会议」的登记入口在左栏（`../WorkspaceRail`）：登记不打断任何在飞的活，
   * 收编与移除的两只确认也住在那条栏里。这里只剩**开始录音**这一件事 ——
   * 唯一要确认的打断挂在它身上（见 `startSelected`）。
   */

  /**
   * 停掉正在跑的「生成会后精细稿」（R28 / b1c6a58e）：打一只 `post_stop`，受理即回。
   * 界面随即靠轮询看到状态翻成「被打断」、按钮回到可按。（派发骨架在 `../dispatch`。）
   */
  const stopPost = (): void => {
    if (offlineFlying === null) return;
    void runAction({
      name: POST_STOP_ACTION,
      args: { dir: offlineFlying.dir },
      before: () => setPostNote(null),
      onFail: setFailure,
      setBusy: setBusy,
      onOk: () => setPostNote('停止已受理：当前块收尾之后停下。'),
      settle: reloadAfterAction,
    });
  };

  /**
   * 转码那一段的暂停 / 继续（本节点，工序链上的小钮）：各打一只 `transcode_pause` /
   * `transcode_resume`，受理即回 —— 暂停在段边界落定、继续摘旗接着前进，链上那一段
   * 照轮询翻面。失败画在首页的失败块（`draining`：没有在跑的工序可暂停那一档）。
   */
  const transcodeAct = (action: 'transcode_pause' | 'transcode_resume'): void => {
    if (selection === null) return;
    void runAction({
      name: action,
      args: { dir: selection },
      onFail: setFailure,
      setBusy: setBusy,
      settle: () => {
        const dir = selectionRef.current;
        if (dir !== null) void pollerRef.current?.tick(dir);
      },
    });
  };

  const startSelected = (): void => {
    if (selection === null) return;
    const row = sessions?.sessions.find((item) => item.dir === selection) ?? null;
    if (row === null || startWhy(row) !== null) return;
    // 唯一硬闸的前台半边：会后精细稿在飞时先弹确认（设计意图第 13 条那句原话）。
    // 确认 = 先打 post_stop（受理即回）→ 等它在块边界停下 → 再起录（模型释放顺序照
    // 设计意图第 21 条：先放会后的模型，再拉会中的）。
    if (offlineFlying !== null) {
      setConfirm({ what: 'start-interrupt', dir: row.dir });
      return;
    }
    startNow(row.dir);
  };

  /** 起录本体（`start` 动作 + 失败块 + 刷新；骨架在 `../dispatch`）。起录那一趟把
   * 「起录在飞」一档（`starting`）跟着忙闩一起收放：派发置位、落定收回（重试那一拍
   * 不落定、保持），在飞期间开始钮画「正在启动…」。 */
  const startNow = (dir: string): void => {
    void runAction({
      name: 'start',
      args: { dir, mode: 'both' },
      onFail: setFailure,
      setBusy: setBusy,
      before: () => setStarting(true),
      settle: () => {
        setStarting(false);
        reloadAfterAction();
      },
    });
  };

  /**
   * 确认打断之后的起录：先停（`post_stop` 受理即回，当前块收尾后才真停），再等
   * 在飞清空、起录。那一等靠 `start` 自己的 409 兜底（服务侧硬闸同一句读数）：
   * 还没停到块边界就按 409 退回，这里隔一拍再试（上限 60 拍，超了如实画失败块）。
   * 重试那一拍自己善后（`onReject` 回 true）：忙保持、失败块不画。
   */
  const startAfterInterrupt = (dir: string): void => {
    // 「起录在飞」一档与 `startNow` 同一收放：置位在派发，收回在落定——被打回重试
    // （`draining`）的那一拍不走尾段，这一档跨整个重试窗保持，开始钮一直画「正在启动…」。
    setStarting(true);
    const attempt = (left: number): void => {
      void runAction({
        name: 'start',
        args: { dir, mode: 'both' },
        onFail: setFailure,
        setBusy: setBusy,
        onReject: (failure) => {
          if (failure.kind === 'envelope' && failure.code === 'draining' && left > 0) {
            window.setTimeout(() => attempt(left - 1), START_RETRY_MS);
            return true;
          }
          return false;
        },
        settle: () => {
          setStarting(false);
          reloadAfterAction();
        },
      });
    };
    attempt(START_RETRY_LIMIT);
  };

  /**
   * 打断确认弹窗那只「打断并开始录音」的落点。确认 = 先停、再起录（设计意图
   * 第 13 / 21 条）：post_stop 受理即回（当前块收尾后才真停），stop 的应答回来再发起
   * 录——那一刻在飞多半还没清，`start` 的 409（服务侧硬闸）兜住，
   * `startAfterInterrupt` 隔一拍再试。这只守卫接的是「弹窗开着的那一会儿，在飞那趟
   * 自己收了工」——此刻不发 stop、直接起录。
   */
  const confirmStartInterrupt = (dir: string): void => {
    setConfirm(null);
    setFailure(null);
    setBusy(true);
    const flying = postJobs.find((job) => job.kind === 'offline') ?? null;
    const stopFirst =
      flying !== null
        ? postAction(POST_STOP_ACTION, { dir: flying.dir }).then(
            () => undefined,
            (error) => setFailure(failureFrom(error)),
          )
        : Promise.resolve();
    void stopFirst.then(() => {
      setBusy(false);
      startAfterInterrupt(dir);
    });
  };

  /**
   * 会后那两个按钮：各自打一个动作，回执只说「这一趟跑的是哪一场」。
   *
   * 起成功之后**不当场宣称跑完了**（那一趟可能跑一小时），只把动作回执如实写在下面，
   * 并重取一遍列表（`state` / `has_offline` 这些现场读数都从服务来）。
   *
   * **纪要免闸的说破半边**（spec「纪要免闸」条）：`post_minutes` 的回执带 `inputs`
   * （当时读到的输入档位）与 `note`（同场会后精细稿还在跑时的说破句），照实画进
   * 下面这一行 —— `docs/ui-design.md` 3.1：那一句照实画在这一格，不许静默降级。
   */
  const postSelected = (action: string): void => {
    if (selection === null) return;
    void runAction({
      name: action,
      args: { dir: selection },
      onFail: (failure) => {
        setFailure(failure);
        setPostNote(null);
      },
      setBusy: setBusy,
      onOk: (result) => {
        const parts = [`${POST_LABELS[action]}：这一趟已起。`];
        if (result.kind === 'post' && result.post.inputs !== undefined) {
          parts.push(`这次读到的输入档位：${POST_INPUT_LABELS[result.post.inputs] ?? result.post.inputs}。`);
        }
        if (result.kind === 'post' && result.post.note !== undefined) {
          parts.push(`${result.post.note}。`);
        }
        setPostNote(parts.join(''));
      },
      settle: reloadAfterAction,
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
    // 失败照原样抛回弹窗那一侧（rethrow）：它要据此清提示、收回忙。
    return runAction({
      name: SPEAKER_RENAME_ACTION,
      args: { dir: selection, key, name },
      onFail: setFailure,
      rethrow: true,
      settle: () => {
        const dir = selectionRef.current;
        if (dir !== null) void pollerRef.current?.tick(dir);
      },
    });
  };

  return {
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
  };
}
