import type { PostJobCell, SessionRow } from '../contract';

/** 工作台上那一场会后的两个按钮的纯逻辑。判据全部来自服务给的 `GET /api/sessions` 那一行（不落本地存储、不自己猜）。 */

/** 两个按钮的动作名。 */
export const POST_OFFLINE_ACTION = 'post_offline';
export const POST_MINUTES_ACTION = 'post_minutes';

/** 停掉那一趟的动作名：只停「生成会后精细稿」这一趟。 */
export const POST_STOP_ACTION = 'post_stop';

export interface PostButtonsView {
  canPostOffline: boolean;
  canPostMinutes: boolean;
  /** 按不动时给一句为什么。 */
  why: string | null;
  /** 「出纪要」按钮上那一格：这一场有没有会后精细稿。 */
  offlineNotice: string | null;
}

/**
 * 这一行的两个按钮长什么样。
 *
 * - 目录不在原地 → 两个都按不动；
 * - 三档口径 `state` 是 `recording` → 两个都按不动，那一格按 `recordingOfflineNotice()`
 *   把「还在录」与「盘上有没有会后稿」并列说；
 * - 其余按得动 —— **「落盘没落定就拒」这条判据作废**：收尾走到哪一步不拦会后动作。
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
  if (row.state === 'recording') {
    return {
      canPostOffline: false,
      canPostMinutes: false,
      why: '该工作区正在录音中',
      offlineNotice: recordingOfflineNotice(row),
    };
  }
  return {
    canPostOffline: true,
    canPostMinutes: true,
    why: null,
    offlineNotice: offlineNoticeOf(row),
  };
}

/** 「出纪要」按钮上那一格：这一场有没有会后精细稿（`has_offline`）。 */
export function offlineNoticeOf(row: SessionRow): string {
  return row.has_offline ? '会后精细稿：有' : '会后精细稿：无';
}

/**
 * 「该工作区正在录音中」那一档的那一格：**两件事并列说，各说各的**。盘上那一份只能按名目读
 * —— 同一个目录被重开一场新会时 `session.json` 回到 `recording`、上一场留下的那份稿还在原地。
 */
export function recordingOfflineNotice(row: SessionRow): string {
  const onDisk = row.has_offline ? '有' : '无';
  return `该工作区正在录音中；磁盘上的会后精细稿：${onDisk}`;
}

export interface PostStopView {
  visible: boolean;
}

/** 停止入口的判据：**这一场**的 offline 那一趟正在本进程里飞 —— 转码那一趟不在此列；纪要那一趟是另一套机制，服务对它的 stop 请求如实拒，入口因此不摆。 */
export function postStopView(
  postJobs: readonly PostJobCell[],
  selectedDir: string | null,
): PostStopView {
  const visible =
    selectedDir !== null &&
    postJobs.some((job) => job.kind === 'offline' && job.dir === selectedDir);
  return { visible };
}
