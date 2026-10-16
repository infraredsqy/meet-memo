import type {
  OfflineSourceProgress,
  PostJobCell,
  SessionRow,
  SessionStatus,
} from '../contract';
import { statusLabel } from './model';
import { sourceDisplayName } from '../live/format';

/**
 * 会后进度界面的**纯逻辑**（`docs/requirements.md` R27 / R31，设计意图第 12 条）。
 *
 * 这一层只做「服务给的字段 → 界面要摆的读数」，不碰组件、不碰取数；组件（
 * `home/WorkspaceHome.tsx`）只把它算好的东西摆到位置上 —— 与会中电平块同一套分层
 * （`live/levels.ts` 算读数、`App.tsx` 只写 CSS 变量）。
 *
 * 四件事，判据全部落在服务给的原文上：
 *
 * 1. **列表行四态**（b3）：空闲 / 正在录 / 会后跑着 / 等你动手 —— 纯显示层重映射
 *    加 `post_job.dir === row.dir` 联结；行 schema 十一键一字不动。
 * 2. **进度读数**：`GET /api/sessions/{id}` 的 `offline` 那份 → 每路一条的读数与
 *    进度条百分比。**分母按路各算各的**（契约 4.9：合成一个整场分母就是在造一个
 *    不存在的量，设计意图第 9 条）。
 * 3. **「真在跑 vs 停着一版 running vs 被打断」**：`offline.in_process` 与 `offline.state`
 *    各答各的事（契约 4.9）；进程崩在半路那一场盘上停着 `running`，读数如实分两层；
 *    「被打断」是盘上自己的终态（b1c6a58e，R28）—— 自成一相，**不许掉进「跑完了」**，
 *    阶段行明说「被打断、可以重跑」。
 * 4. **全局「显卡忙不忙」**（b1/b2）：忙 = 有模型开着 —— 本机正在录（快照状态）
 *    ∨ 会后那趟在本进程跑（`GET /api/sessions` 顶层的 `post_job`）。`meeting post`
 *    命令行那趟活在另一进程、服务看不见，这一格因此不点亮 —— 如实显示。
 */

/** `GET /api/sessions` 顶层第三格的类型（`PostJobCell`）归契约层；这里引用不重定义。 */
export type { PostJobCell };

// ---------------------------------------------------------------------------
// 1. 列表行四态（纯显示层重映射 + post_job 联结）
// ---------------------------------------------------------------------------

/**
 * 行状态的档位。spec 四态是 `idle` / `recording` / `post-running` / `waiting`；
 * 另有一档如实保留：「路径已不在」（`gone`）。行 `state` 是三档口径（契约 4.8）：
 * `recording` / `ended` / `null` —— 收尾走到哪一步不进这个口径，落盘与转码的进度
 * 由详情里的进度区（`GET /api/sessions/{id}` 的 `offline` 那份）另答。
 */
export type RowTone = 'gone' | 'idle' | 'recording' | 'post-running' | 'waiting';

export interface RowStatusView {
  tone: RowTone;
  /** 状态格上的字。 */
  label: string;
  /**
   * 图形标记（形状是「不靠颜色」的那一半，`ui-design` §7.10 的 `glyph--*`）；
   * `null` = 会后跑着，画 `spinner`（转的那个，与会中总结进行中同一只）。
   */
  mark: string | null;
  /** 灰档：只有「路径已不在」用它（沿 `wslist__state--dim` 的既有口径）。 */
  dim: boolean;
}

/**
 * 一行（或选中那一场的详情「状态」格）该写什么。
 *
 * 判据按次序：
 *
 * - 目录不在原地 → 「目录不存在」（最优先 —— 目录都没了，联结无从谈起）；
 * - `post_job.dir === row.dir` → 「会后任务运行中」（联结命中；跑的是哪一趟写在全局那格）；
 * - 三档口径 `state`：`recording` → 录音中；`ended` → **待处理语义**
 *   （05a48b6a 立下这条口径：录音已结束、会后归两个按钮，都不按就什么都不跑）；
 * - 其余（还没开过会的目录）→ 空闲族，文案以 `model.ts` `statusLabel()` 为唯一出处。
 */
export function rowStatusView(row: SessionRow, postJob: PostJobCell | null): RowStatusView {
  if (!row.dir_exists) {
    return { tone: 'gone', label: statusLabel(row), mark: 'glyph--dashed', dim: true };
  }
  if (postJob !== null && postJob.dir === row.dir) {
    return { tone: 'post-running', label: '会后任务运行中', mark: null, dim: false };
  }
  switch (row.state) {
    case 'recording':
      return { tone: 'recording', label: '录音中', mark: 'glyph--rec', dim: false };
    case 'ended':
      return { tone: 'waiting', label: '待处理', mark: 'glyph--dashed', dim: false };
    default:
      break;
  }
  return { tone: 'idle', label: statusLabel(row), mark: 'glyph--idle', dim: false };
}

// ---------------------------------------------------------------------------
// 2. 进度读数（offline 那份 → 每路一条 + 进度条百分比）
// ---------------------------------------------------------------------------

export interface OfflineRoadView {
  source: 'mic' | 'loopback';
  label: string;
  done: number;
  total: number | null;
  remaining: number | null;
  currentChunk: number | null;
  /** 填充百分比（一位小数，0–100）；分母未知（`total: null`）时是 `null` —— 进度条不猜。 */
  percent: number | null;
  counts: OfflineSourceProgress['counts'];
}

export type OfflinePhase = 'in-flight' | 'stale-running' | 'interrupted' | 'finished' | 'never-run';

export interface ProgressView {
  phase: OfflinePhase;
  /** 阶段那一行的整句（「正在跑：本地 第 2/5 块」/「停着一版"在跑"，其实谁都没在跑」）。 */
  phaseLabel: string;
  /** `state` 与 `in_process` 两格的原文（界面在阶段行旁边如实摆出这两格）。 */
  onDiskState: SessionStatus['offline']['state'];
  inProcess: boolean;
  roads: OfflineRoadView[];
  /** 四档合计（两路相加；只作一句话的附注，不作分母）。 */
  counts: OfflineSourceProgress['counts'];
  elapsedSeconds: number | null;
  startedAt: string | null;
  finishedAt: string | null;
  currentSource: 'mic' | 'loopback' | null;
}

/** 一路的百分比：`done / total`，一位小数、钳在 0–100；分母未知就不算。 */
export function roadPercent(done: number, total: number | null): number | null {
  if (total === null || total <= 0) return null;
  return Math.min(100, Math.max(0, Math.round((done / total) * 1000) / 10));
}

const ZERO_COUNTS = { transcribed: 0, silent: 0, failed: 0, unjudged: 0 };

/**
 * 阶段那一格的图形标记（形状是「不靠颜色」的那一半）：真在跑画 `spinner`（`null`），
 * 停着一版 running 画红叉（「出事了」那一档的形状），**被打断同画红叉**（这一场
 * 没跑完、需要人决定要不要重按），终态画实心块打勾，没跑过画空心框。
 */
export function phaseMark(phase: OfflinePhase): string | null {
  switch (phase) {
    case 'in-flight':
      return null;
    case 'stale-running':
      return 'glyph--cross';
    case 'interrupted':
      return 'glyph--cross';
    case 'finished':
      return 'glyph--done';
    default:
      return 'glyph--idle';
  }
}

/**
 * 终态那一档的人话。
 *
 * 这四档不能合成一句「已完成」：盘上 `failed` / `partial` 是**没跑成**的两种场面，
 * 笼统一句会把它们说反（先前那一版只能把盘上的词挂在括号里打补丁）。
 */
const FINISHED_PHASE: Readonly<Record<string, string>> = {
  done: '已完成',
  partial: '部分完成',
  failed: '失败',
  ledger: '有逐块记录，尚未出结论',
};

/** `offline` 那份 → 进度区的读数。判据逐条写在 `phaseLabel` 里，组件不解读。 */
export function progressView(status: SessionStatus): ProgressView {
  const offline = status.offline;
  const inFlight = offline.in_process;
  const onDiskRunning = offline.state === 'running';
  const phase: OfflinePhase = inFlight
    ? 'in-flight'
    : onDiskRunning
      ? 'stale-running'
      : offline.state === 'interrupted'
        ? 'interrupted'
        : offline.state === 'idle'
          ? 'never-run'
          : 'finished';

  const roads: OfflineRoadView[] = offline.sources.map((road) => ({
    source: road.source,
    label: sourceDisplayName(road.source),
    done: road.done,
    total: road.total,
    remaining: road.remaining,
    currentChunk: road.current_chunk,
    percent: roadPercent(road.done, road.total),
    counts: { ...road.counts },
  }));

  const counts = roads.reduce(
    (sum, road) => ({
      transcribed: sum.transcribed + road.counts.transcribed,
      silent: sum.silent + road.counts.silent,
      failed: sum.failed + road.counts.failed,
      unjudged: sum.unjudged + road.counts.unjudged,
    }),
    { ...ZERO_COUNTS },
  );

  const onTheRoad = roads.find((road) => road.source === offline.current_source) ?? null;
  const phaseLabel =
    phase === 'in-flight'
      ? onTheRoad !== null && onTheRoad.currentChunk !== null
        ? `正在运行：${onTheRoad.label} 第 ${onTheRoad.currentChunk}/${onTheRoad.total ?? '?'} 块`
        : '正在运行'
      : phase === 'stale-running'
        ? '磁盘记录为运行中，当前没有进程在运行'
        : phase === 'interrupted'
          ? '已中断'
          : phase === 'never-run'
            ? '尚未运行'
            : FINISHED_PHASE[offline.state] ?? `磁盘记录：${offline.state}`;

  return {
    phase,
    phaseLabel,
    onDiskState: offline.state,
    inProcess: offline.in_process,
    roads,
    counts,
    elapsedSeconds: offline.elapsed_seconds,
    startedAt: offline.started_at,
    finishedAt: offline.finished_at,
    currentSource: offline.current_source,
  };
}

// ---------------------------------------------------------------------------
// 3. 逐块行（每跑完一块落一行）
// ---------------------------------------------------------------------------

/** 逐块行的四档归属 + 「读得着但归不了档」的兜底。 */
export type ChunkVerdict = 'transcribed' | 'silent' | 'failed' | 'unjudged' | 'observed';

export interface ChunkRow {
  key: string;
  source: 'mic' | 'loopback';
  label: string;
  chunk: number;
  total: number | null;
  verdict: ChunkVerdict;
}

const VERDICT_LABELS: Readonly<Record<ChunkVerdict, string>> = {
  transcribed: '成功',
  silent: '静音',
  failed: '失败',
  unjudged: '未判定',
  observed: '已处理',
};

/** 逐块行上那句人话（一处定义，组件不拼句子）。 */
export function chunkRowLabel(row: ChunkRow): string {
  const where = row.total === null ? `${row.label} 第 ${row.chunk} 块` : `${row.label} 第 ${row.chunk}/${row.total} 块`;
  return `${where} · ${VERDICT_LABELS[row.verdict]}`;
}

/**
 * 第一次读到这一场时，把**已经跑过的**那些块补成行（档位读不着、如实记 `observed`
 * —— 界面只从这一刻起见证往后的块，此前的不编）。`done` 可能比块数多不了、也可能
 * 分母未知，行数照 `done` 走。
 */
export function initialChunkRows(status: SessionStatus): ChunkRow[] {
  const rows: ChunkRow[] = [];
  for (const road of status.offline.sources) {
    for (let index = 1; index <= road.done; index += 1) {
      rows.push({
        key: `${road.source}-${index}`,
        source: road.source,
        label: sourceDisplayName(road.source),
        chunk: index,
        total: road.total,
        verdict: 'observed',
      });
    }
  }
  return rows;
}

/**
 * 两次读数之间新跑完的块 → 追加的行。
 *
 * 归档靠**四档计数的差值**（每档差几就有几块落在那一档），次序 `transcribed` /
 * `silent` / `failed` / `unjudged`；差值对不上 `done` 的差值时，多出来的块如实记
 * `observed`（界面不猜它是哪一档）。`before` 为 `null`（这一场是刚选中才第一次读）
 * 时交空数组 —— 初始那批归 `initialChunkRows()`。**重跑那一档不进这里**：记录已重置
 * （`chunkLedgerReset()` 命中）时差值全是钳出来的 0，组件按重播整段换行，不走差值。
 */
export function advanceChunkRows(
  _prev: ChunkRow[],
  before: SessionStatus | null,
  next: SessionStatus,
): ChunkRow[] {
  if (before === null || before.dir !== next.dir) return [];
  const appended: ChunkRow[] = [];
  const beforeRoads = new Map(before.offline.sources.map((road) => [road.source, road]));
  for (const road of next.offline.sources) {
    const was = beforeRoads.get(road.source);
    if (was === undefined) continue;
    const deltaDone = Math.max(0, road.done - was.done);
    if (deltaDone === 0) continue;
    const deltas: Array<[ChunkVerdict, number]> = [
      ['transcribed', Math.max(0, road.counts.transcribed - was.counts.transcribed)],
      ['silent', Math.max(0, road.counts.silent - was.counts.silent)],
      ['failed', Math.max(0, road.counts.failed - was.counts.failed)],
      ['unjudged', Math.max(0, road.counts.unjudged - was.counts.unjudged)],
    ];
    for (let index = 1; index <= deltaDone; index += 1) {
      const chunk = was.done + index;
      let verdict: ChunkVerdict = 'observed';
      const match = deltas.find(([, delta]) => delta > 0);
      if (match !== undefined) {
        verdict = match[0];
        match[1] -= 1;
      }
      appended.push({
        key: `${road.source}-${chunk}`,
        source: road.source,
        label: sourceDisplayName(road.source),
        chunk,
        total: road.total,
        verdict,
      });
    }
  }
  return appended;
}

/**
 * 两次读数之间，逐块行该不该整段**重播**（review-2 m2：记录已重置 —— 新一趟开始了）。
 *
 * 后端每趟重跑都新建记录、`start()` 落**只有表头**的记录（`meeting/offline/progress.py`
 * 的 `start()`；`runtime.py` 每趟新造 ledger），重跑时盘上 `done` 从 0 重数 ——
 * `advanceChunkRows()` 的差值 `Math.max(0, next - was)` 把回落全钳成 0，旧行一行
 * 都掉不下去，追加行号还会接旧记录（出「第 11/10 块」这种行）。判据两条、命中即重播：
 *
 * - **`started_at` 换新**：新一趟的墙钟起点，`start()` 写一次后整趟不变（`finish()`
 *   只动 `finished_at`）—— 两边一比就知道手上是哪一趟的记录；
 * - **某路 `done` 回落 ∨ 四档合计回落**：记录被整段换过的另一面（`started_at` 相同
 *   时兜底）。
 *
 * 换场（`dir` 不同）不算 —— 那一档由组件的换选中清零负责。
 */
export function chunkLedgerReset(before: SessionStatus, next: SessionStatus): boolean {
  if (before.dir !== next.dir) return false;
  if (before.offline.started_at !== next.offline.started_at) return true;
  const beforeRoads = new Map(before.offline.sources.map((road) => [road.source, road]));
  const sumOf = (counts: OfflineSourceProgress['counts']): number =>
    counts.transcribed + counts.silent + counts.failed + counts.unjudged;
  return next.offline.sources.some((road) => {
    const was = beforeRoads.get(road.source);
    if (was === undefined) return false;
    return road.done < was.done || sumOf(road.counts) < sumOf(was.counts);
  });
}

// ---------------------------------------------------------------------------
// 4. 全局「显卡忙不忙」（R31；两处 topbar__right 共用这一份读法）
// ---------------------------------------------------------------------------

export interface GpuBusyView {
  busy: boolean;
  /** 那一格的字：「忙碌」/「空闲」。 */
  label: string;
  /** 旁边一句为什么（哪只模型开着）。 */
  note: string;
  /** 图形标记：忙 = `glyph--rec`（实心），闲 = `glyph--idle`（空心框）。 */
  mark: string;
}

const POST_KIND_LABELS: Readonly<Record<string, string>> = {
  offline: '会后精细稿',
  minutes: '纪要',
};

/**
 * 忙/闲的判据（b2）：忙 = 有模型开着 —— 本机正在录（会中模型）∨ 会后那趟在本进程跑。
 * 两处调用各自带自己的输入：工作台首页（会中模型必关着）传 `recording: false` +
 * 列表应答的 `post_job`；会中那一屏传 `recording: top_state ∈ {recording, alert}`
 * —— 会后那趟在那一屏没有读数入口，不编。
 */
export function gpuBusyView(postJob: PostJobCell | null, recording: boolean): GpuBusyView {
  const busy = recording || postJob !== null;
  if (!busy) {
    return { busy: false, label: '空闲', note: '没有模型开着', mark: 'glyph--idle' };
  }
  const what: string[] = [];
  if (recording) what.push('会中模型开着');
  if (postJob !== null) {
    const kind = POST_KIND_LABELS[postJob.kind] ?? postJob.kind;
    what.push(`会后那趟在跑（${kind}）`);
  }
  return {
    busy: true,
    label: '忙碌',
    note: what.join('；'),
    mark: 'glyph--rec',
  };
}

// ---------------------------------------------------------------------------
// 5. 要不要轮询（进度区的「往下刷」只在这一档开着）
// ---------------------------------------------------------------------------

/**
 * 进度区现在该不该问服务要读数（`api/sessions.ts` 的轮询器只在这一档跑，不空转）：
 *
 * - 选中的这一场被 `post_job` 联结命中 → 问，**但只问到读数落定为止**：手上还没有
 *   这一场的读数（或还是上一场的旧记录）时先问一跳；读数到手后按读数本身判 ——
 *   `in_process` ∨ 盘上 `running` 才继续问，**终态那一跳让 watch 翻 false**
 *   （review-1 C1：联结命中恒真会让「跑完」永远收不了尾，行四态、全局格、轮询器
 *   一起楔死）；读数翻新由组件那头的「不用刷顺手刷一遍列表」接住；
 * - 手上已有一份读数、且它说「真在跑」（`in_process`）或「盘上停着 `running`」→ 问
 *   （后者也要问：万一那一趟其实还活着，读数会翻回 `in_process: true`）；
 * - 其余（终态、**被打断**（b1c6a58e：它也是终态，重按按钮才再跑）、还没跑过、
 *   什么都没选）→ 不问。
 */
export function shouldWatch(
  row: SessionRow | null,
  postJob: PostJobCell | null,
  status: SessionStatus | null,
): boolean {
  if (row === null) return false;
  const fresh = status !== null && status.dir === row.dir;
  if (postJob !== null && postJob.dir === row.dir) {
    if (!fresh) return true;
    return status.offline.in_process || status.offline.state === 'running';
  }
  if (!fresh) return false;
  return status.offline.in_process || status.offline.state === 'running';
}
