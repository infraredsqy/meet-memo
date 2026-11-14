import type {
  OfflineSourceProgress,
  PostJobCell,
  SessionRow,
  SessionStatus,
} from '../contract';
import { statusLabel } from './model';
import { sourceDisplayName } from '../live/format';

/** 会后进度界面的**纯逻辑**。 */

export type { PostJobCell };

/**
 * 行状态的档位：列表行只说录音的**四档**，另有一档如实保留「目录不存在」（`gone`）。
 * 收尾走到哪一步、会后那几趟在不在飞都**不进这个口径**。
 */
export type RowTone = 'gone' | 'blank' | 'unstarted' | 'recording' | 'ended';

export interface RowStatusView {
  tone: RowTone;
  label: string;
  /** 图形标记（形状是「不靠颜色」的那一半）。 */
  mark: string;
  dim: boolean;
}

export function rowStatusView(row: SessionRow, _postJobs: readonly PostJobCell[]): RowStatusView {
  if (!row.dir_exists) {
    return { tone: 'gone', label: statusLabel(row), mark: 'glyph--dashed', dim: true };
  }
  switch (row.state) {
    case 'recording':
      return { tone: 'recording', label: '录音中', mark: 'glyph--rec', dim: false };
    case 'ended':
      return { tone: 'ended', label: '录音结束', mark: 'glyph--done', dim: false };
    default:
      break;
  }
  return {
    tone: row.kind === 'empty' ? 'blank' : 'unstarted',
    label: statusLabel(row),
    mark: 'glyph--idle',
    dim: false,
  };
}

export interface OfflineRoadView {
  source: 'mic' | 'loopback';
  label: string;
  done: number;
  total: number | null;
  remaining: number | null;
  currentChunk: number | null;
  /** 填充百分比；分母未知（`total: null`）时是 `null` —— 进度条不猜。 */
  percent: number | null;
  counts: OfflineSourceProgress['counts'];
}

export type OfflinePhase = 'in-flight' | 'stale-running' | 'interrupted' | 'finished' | 'never-run';

export interface ProgressView {
  phase: OfflinePhase;
  phaseLabel: string;
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

export function roadPercent(done: number, total: number | null): number | null {
  if (total === null || total <= 0) return null;
  return Math.min(100, Math.max(0, Math.round((done / total) * 1000) / 10));
}

const ZERO_COUNTS = { transcribed: 0, silent: 0, failed: 0, unjudged: 0 };

/**
 * 阶段那一格的图形标记（形状是「不靠颜色」的那一半）：真在跑画 `spinner`（`null`），
 * 停着一版 running 画红叉，**被打断同画红叉**（这一场没跑完、需要人决定要不要重按）。
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
 * 笼统一句会把它们说反。
 */
const FINISHED_PHASE: Readonly<Record<string, string>> = {
  done: '已完成',
  partial: '部分完成',
  failed: '失败',
  ledger: '有逐块记录，尚未出结论',
};

/** 判据逐条写在 `phaseLabel` 里，组件不解读。 */
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
 * —— 界面只从这一刻起见证往后的块，此前的不编）。
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
 * 归档靠**四档计数的差值**，次序 `transcribed` / `silent` / `failed` / `unjudged`；差值对不上
 * `done` 的差值时，多出来的块如实记 `observed`（界面不猜它是哪一档）。`before` 为 `null`
 * 时交空数组 —— 初始那批归 `initialChunkRows()`。**重跑那一档不进这里**。
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
 * 两次读数之间，逐块行该不该整段**重播**（记录已重置 —— 新一趟开始了）。
 *
 * 重跑时盘上 `done` 从 0 重数，`advanceChunkRows()` 的差值把回落全钳成 0，旧行一行都掉不下
 * 去。判据两条、命中即重播：**`started_at` 换新**，或**某路 `done` 回落 ∨ 四档合计回落**。
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

export interface GpuBusyView {
  busy: boolean;
  label: string;
  note: string;
  mark: string;
}

const POST_KIND_LABELS: Readonly<Record<string, string>> = {
  offline: '会后精细稿',
  minutes: '纪要',
  transcode: '转码',
};

/**
 * 忙/闲的判据：忙 = 有模型开着 —— 本机正在录（会中模型）∨ 会后那趟在本进程跑。
 * **转码那一趟不吃显卡**，它不点亮这一格、也不进小注。
 */
export function gpuBusyView(
  postJobs: readonly PostJobCell[],
  recording: boolean,
): GpuBusyView {
  const models = postJobs.filter((job) => job.kind !== 'transcode');
  const busy = recording || models.length > 0;
  if (!busy) {
    return { busy: false, label: '空闲', note: '没有模型开着', mark: 'glyph--idle' };
  }
  const what: string[] = [];
  if (recording) what.push('会中模型开着');
  for (const job of models) {
    const kind = POST_KIND_LABELS[job.kind] ?? job.kind;
    what.push(`会后那趟在跑（${kind}）`);
  }
  return {
    busy: true,
    label: '忙碌',
    note: what.join('；'),
    mark: 'glyph--rec',
  };
}

/**
 * 进度区现在该不该问服务要读数（轮询器只在这一档跑，不空转）。
 *
 * 联结命中时问，**但只问到读数落定为止**：`in_process` ∨ 盘上 `running` 才继续问，终态那一跳
 * 让 watch 翻 false。**转码与纪要在飞也问** —— 链上那一段要跟着翻面。
 */
export function shouldWatch(
  row: SessionRow | null,
  postJobs: readonly PostJobCell[],
  status: SessionStatus | null,
): boolean {
  if (row === null) return false;
  const fresh = status !== null && status.dir === row.dir;
  const flying = (current: SessionStatus): boolean =>
    current.offline.in_process ||
    current.offline.state === 'running' ||
    current.transcode.in_process ||
    current.post.some((job) => job.kind === 'minutes' && job.running);
  if (postJobs.some((job) => job.dir === row.dir)) {
    if (!fresh) return true;
    return flying(status as SessionStatus);
  }
  if (!fresh) return false;
  return flying(status as SessionStatus);
}
