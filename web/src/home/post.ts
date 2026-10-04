import type { PostJobCell, SessionRow } from '../contract';

/**
 * 工作台上那一场会后的两个按钮的纯逻辑（`docs/requirements.md` R26 / R29）。
 *
 * 两个按钮住在**选中那一场的详情**里（会话后视图那一块地）：都不按，这一场就什么都不跑。
 * 这一层只做「这一行现在能不能按、按钮上那一格写什么」这两件事，判据全部来自服务给的
 * `GET /api/sessions` 那一行（不落本地存储、不自己猜 —— 契约章 6 第 1、2、6 条）。
 */

/** 按钮的名字：各自说清跑的是什么（R26）。 */
export const POST_OFFLINE_ACTION = 'post_offline';
export const POST_MINUTES_ACTION = 'post_minutes';

/** 停掉那一趟的动作名（R28 / b1c6a58e）：post_* 族，只停「生成会后精细稿」这一趟。 */
export const POST_STOP_ACTION = 'post_stop';

export interface PostButtonsView {
  /** 「生成会后精细稿」能不能按。 */
  canPostOffline: boolean;
  /** 「直接出纪要」能不能按。 */
  canPostMinutes: boolean;
  /** 按不动时给一句为什么（`null` = 两个都能按）。 */
  why: string | null;
  /** 「出纪要」按钮上那一格：这一场有没有会后精细稿（R29）；没得说时是 `null`。 */
  offlineNotice: string | null;
}

/**
 * 这一行的两个按钮长什么样。
 *
 * 判据（逐条来自服务给的原文）：
 *
 * - 目录不在原地 → 两个都按不动（同 `startWhy()` 的口气）；
 * - 盘上 `state` 是 `recording` / `draining` → 收尾还没走完，两个都按不动
 *   （服务侧那两个动作按 `dir` 那一场的盘上 `state` 判读、以 `invalid_setting` 400 拒掉
 *   —— `docs/contract.md` 4.3；运行时自己还在录 / 收尾没落定的那一档才是 `draining` 409，
 *   那一档不会与工作台同屏 —— 这里只是先把它写在脸上），那一格
 *   按 `unsettledOfflineNotice()` 把「还在录 / 还在收尾」与「盘上有没有会后稿」并列说；
 * - 其余（`done` / `failed`，或还没开过会的那一行）按得动 —— 会后那两趟活该不该跑，
 *   与收尾那一步的成败无关。
 */
export function postButtonsView(row: SessionRow): PostButtonsView {
  if (!row.dir_exists) {
    return {
      canPostOffline: false,
      canPostMinutes: false,
      why: '目录不存在，无法运行会后任务',
      // 「目录不存在」是**路径那一格**的说法（`statusLabel()`、`wslist__state`）；摆到这一格上
      // 会被读成在回答会后稿的事。上面那句 `why` 已经把「跑不了」说完了，这一格不再复述。
      offlineNotice: null,
    };
  }
  if (row.state === 'recording' || row.state === 'draining') {
    return {
      canPostOffline: false,
      canPostMinutes: false,
      why: '该工作区正在录音或收尾中',
      offlineNotice: unsettledOfflineNotice(row),
    };
  }
  return {
    canPostOffline: true,
    canPostMinutes: true,
    why: null,
    offlineNotice: offlineNoticeOf(row),
  };
}

/** 「出纪要」按钮上那一格：这一场有没有会后精细稿（R29 / `has_offline`）。 */
export function offlineNoticeOf(row: SessionRow): string {
  return row.has_offline ? '会后精细稿：有' : '会后精细稿：无';
}

/**
 * 「该工作区正在录音或收尾中」那一档的那一格：**两件事并列说，各说各的**（review-2 C1）。
 *
 * 「这一场还没落定」与「盘上此刻有没有那份会后稿」是两件各自为真的事：盘上那一份只能按
 * 名目读（`has_offline` 只看文件名 —— `meeting/workspace.py`），同一个目录被重开一场新会时
 * `session.json` 回到 `recording`、上一场留下的那份稿还在原地。因此这一格既不因为两个按钮
 * 按不动就把「盘上有没有」收起来，也不拿「盘上有」盖住「这一场还没落定」。
 */
export function unsettledOfflineNotice(row: SessionRow): string {
  const onDisk = row.has_offline ? '有' : '无';
  return `该工作区正在录音或收尾中；磁盘上的会后精细稿：${onDisk}`;
}

// ---------------------------------------------------------------------------
// 停掉那一趟（R28 / b1c6a58e）：入口在进度区头行，只在「这一场的 offline 在飞」时出现
// ---------------------------------------------------------------------------

export interface PostStopView {
  /** 停止入口显不显示。 */
  visible: boolean;
}

/**
 * 停止入口的判据：**这一场**的 offline 那一趟正在本进程里飞。
 *
 * 三条同时成立才出现：`post_job` 在飞（顶层第三格非 null）、跑的是精细稿那一趟
 * （`kind: 'offline'` —— 纪要那一趟是另一套机制，服务对它的 stop 请求如实拒，入口
 * 因此不摆）、跑的正是选中这一场（入口长在进度区头行上，只管自己这一场）。
 */
export function postStopView(postJob: PostJobCell | null, selectedDir: string | null): PostStopView {
  const visible =
    postJob !== null &&
    postJob.kind === 'offline' &&
    selectedDir !== null &&
    postJob.dir === selectedDir;
  return { visible };
}
