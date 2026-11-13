import type { Ended, SessionStatus } from '../contract';
import { sourceDisplayName } from '../live/format';
import { drainStripView } from './drain';
import { progressView } from './progress';

/**
 * 会后区那条**事后工序链**的纯逻辑（本节点，设计意图第 12 条：进度看得见）。
 *
 * 链上四段 —— **落盘 → 转码 → 会后转写 → 纪要** —— 每段一格：现状一行（说人话，
 * 不摆机器对）+ 读数；当前在跑的那一段看得见。链上只摆事实，不替任何一层下结论：
 *
 * - **落盘**：快照 `ended` 只对快照里那一场说话（`drainStripView()` 同一把尺）；
 *   其余已结束的场次收尾是过去的事，这一格只说「已结束」（与列表行同一口径，不编）。
 * - **转码**：`status.transcode`（契约 4.9；判据读盘，重启后照旧）。暂停 / 继续的
 *   可按判据也在这里算：暂停要「真在跑」，继续要「停在 `paused`」。
 * - **会后转写**：`status.offline` 的既有读法（`progressView()` 的阶段行原话）。
 * - **纪要**：`status.minutes`（4.9 的盘上读数）+ `post` 清单里 minutes 那一项的
 *   `running` 合起来答——盘上停着一版 `running` 而清单里没有在飞的，是「重启过后
 *   没人跑」那一档，如实分层，不折成「跑完了」。
 *
 * 「我现在能不能走」收成链上的一行（设计意图第 12 条的「能不能去睡觉」）：
 * 四档，按次序判 —— 落盘出了岔子先别关机 / 落盘没走完先别关机 /
 * 有活在跑先别关机（关页面不影响）/ 没有在跑的活可以走。
 */

/** 链上四段的 id（次序即流程次序，不可跳步展示）。 */
export type ChainSegmentId = 'drain' | 'transcode' | 'offline' | 'minutes';

export interface ChainSegmentView {
  id: ChainSegmentId;
  /** 段名（一处定义）。 */
  name: string;
  /** 现状那一行（说人话）。 */
  line: string;
  /** 图形标记（形状通道）；在跑是 `null`（组件画 spinner）。 */
  mark: string | null;
  /** 这一段真在跑（链上「当前那一段」的读法）。 */
  running: boolean;
  /** 这一段停着（暂停 / 被打断 —— 等人决定，与失败分开）。 */
  waiting: boolean;
  /** 这一段失败了。 */
  failed: boolean;
  /** 转码那一段的暂停 / 继续（别的段没有这一格）。 */
  pause: { canPause: boolean; canResume: boolean } | null;
}

export interface ChainWalkView {
  /** 「我现在能不能走」那一行。 */
  line: string;
  /** 形状标记：在跑 `glyph--rec`、落盘在跑 `glyph--draining`、落盘出事 `glyph--cross`、
   *  没有在跑的活 `glyph--idle`。 */
  mark: string;
}

export interface ChainView {
  segments: ChainSegmentView[];
  walk: ChainWalkView;
}

export interface ChainInput {
  /** 快照 `ended` 的转手包（只对快照里那一场说话；`null` = 快照上没有收尾这件事）。 */
  drain: { dir: string | null; ended: Ended } | null;
  /** 选中这一场的单场读数（组件保证 `status.dir === selectedDir` 才调）。 */
  status: SessionStatus;
  /** 选中这一场的目录。 */
  selectedDir: string;
}

export const CHAIN_SEGMENT_NAMES: Readonly<Record<ChainSegmentId, string>> = {
  drain: '落盘',
  transcode: '转码',
  offline: '会后转写',
  minutes: '纪要',
};

/** 转码的 `state` → 现状那一行（分母按路各算各的，读数只取在转的那一路）。 */
function transcodeLine(status: SessionStatus): {
  line: string;
  mark: string | null;
  running: boolean;
  waiting: boolean;
  failed: boolean;
} {
  const cell = status.transcode;
  const current =
    cell.sources.find((road) => road.current_segment !== null) ?? null;
  const where =
    current !== null
      ? `${sourceDisplayName(current.source)} 第 ${current.current_segment}/${current.total ?? '?'} 段`
      : null;
  switch (cell.state) {
    case 'running':
      if (cell.in_process) {
        return {
          line: where === null ? '正在转' : `正在转：${where}`,
          mark: null,
          running: true,
          waiting: false,
          failed: false,
        };
      }
      return {
        line: '盘上记着在跑，此刻没东西在跑',
        mark: 'glyph--cross',
        running: false,
        waiting: false,
        failed: false,
      };
    case 'paused':
      return {
        line: where === null ? '已暂停' : `已暂停（${where}停住）`,
        mark: 'glyph--draining',
        running: false,
        waiting: true,
        failed: false,
      };
    case 'done':
      return { line: '已完成', mark: 'glyph--done', running: false, waiting: false, failed: false };
    case 'partial':
      return {
        line: '部分完成',
        mark: 'glyph--dashed',
        running: false,
        waiting: false,
        failed: false,
      };
    case 'failed':
      return { line: '失败', mark: 'glyph--cross', running: false, waiting: false, failed: true };
    default:
      return {
        line: '还没跑过',
        mark: 'glyph--idle',
        running: false,
        waiting: false,
        failed: false,
      };
  }
}

/** 纪要的现状：`post` 清单里 minutes 那一项在飞优先，盘上那份照实分层。 */
function minutesView(status: SessionStatus): {
  line: string;
  mark: string | null;
  running: boolean;
  waiting: boolean;
  failed: boolean;
} {
  const flying = status.post.some((job) => job.kind === 'minutes' && job.running);
  const cell = status.minutes;
  if (flying) {
    return { line: '正在生成', mark: null, running: true, waiting: false, failed: false };
  }
  switch (cell.state) {
    case 'running':
      return {
        line: '盘上记着在跑，此刻没东西在跑',
        mark: 'glyph--cross',
        running: false,
        waiting: false,
        failed: false,
      };
    case 'done':
      return { line: '已完成', mark: 'glyph--done', running: false, waiting: false, failed: false };
    case 'delivered':
      return {
        line: '已交给 DSH 生成',
        mark: 'glyph--done',
        running: false,
        waiting: false,
        failed: false,
      };
    case 'failed':
      return { line: '失败', mark: 'glyph--cross', running: false, waiting: false, failed: true };
    default:
      return {
        line: '还没跑过（按「生成纪要」起）',
        mark: 'glyph--idle',
        running: false,
        waiting: false,
        failed: false,
      };
  }
}

/** 链上的读数。组件不解读，这里逐段写判据。 */
export function chainView(input: ChainInput): ChainView {
  const { drain, status, selectedDir } = input;

  // -- 落盘：快照里那一场给三态读法，其余已结束的场次只说「已结束」-----------
  const liveDrain = drain !== null && drain.dir === selectedDir;
  const strip = liveDrain && drain !== null ? drainStripView(drain.ended) : null;
  const drainSegment: ChainSegmentView = {
    id: 'drain',
    name: CHAIN_SEGMENT_NAMES.drain,
    line:
      strip === null
        ? '已结束'
        : strip.failed || strip.error !== null
          ? '收尾出事了，先别关机'
          : strip.settled
            ? '录音已落盘'
            : '还差最后一块没进盘',
    mark:
      strip === null
        ? 'glyph--done'
        : strip.failed || strip.error !== null
          ? 'glyph--cross'
          : strip.settled
            ? 'glyph--done'
            : null,
    running: strip !== null && !strip.settled && !strip.failed && strip.error === null,
    waiting: false,
    failed: strip !== null && (strip.failed || strip.error !== null),
    pause: null,
  };

  // -- 转码 -----------------------------------------------------------------
  const transcode = transcodeLine(status);
  const transcodeSegment: ChainSegmentView = {
    id: 'transcode',
    name: CHAIN_SEGMENT_NAMES.transcode,
    line: transcode.line,
    mark: transcode.mark,
    running: transcode.running,
    waiting: transcode.waiting,
    failed: transcode.failed,
    pause: {
      canPause: status.transcode.state === 'running' && status.transcode.in_process,
      canResume: status.transcode.state === 'paused',
    },
  };

  // -- 会后转写：阶段行的既有读法（`progressView()` 不在这里重抄）-----------
  const prog = progressView(status);
  const offlineState = status.offline.state;
  const offlineFlying = prog.phase === 'in-flight';
  const offlineLine =
    offlineFlying
      ? prog.phaseLabel // 在跑的那一句与进度区同源（`本地第 2/5 块` 那一套）
      : offlineState === 'running'
        ? '盘上记着在跑，此刻没东西在跑'
        : offlineState === 'interrupted'
          ? '已中断，可以重按'
          : offlineState === 'done'
            ? '已完成'
            : offlineState === 'partial'
              ? '部分完成'
              : offlineState === 'failed'
                ? '失败'
                : '还没跑过（按「生成会后精细稿」起）';
  const offlineSegment: ChainSegmentView = {
    id: 'offline',
    name: CHAIN_SEGMENT_NAMES.offline,
    line: offlineLine,
    mark:
      offlineFlying
        ? null
        : offlineState === 'running' || offlineState === 'interrupted'
          ? 'glyph--cross'
          : offlineState === 'done'
            ? 'glyph--done'
            : offlineState === 'partial'
              ? 'glyph--dashed'
              : offlineState === 'failed'
                ? 'glyph--cross'
                : 'glyph--idle',
    running: offlineFlying,
    waiting: offlineState === 'interrupted',
    failed: offlineState === 'failed',
    pause: null,
  };

  // -- 纪要 -----------------------------------------------------------------
  const minutes = minutesView(status);
  const minutesSegment: ChainSegmentView = {
    id: 'minutes',
    name: CHAIN_SEGMENT_NAMES.minutes,
    line: minutes.line,
    mark: minutes.mark,
    running: minutes.running,
    waiting: false,
    failed: minutes.failed,
    pause: null,
  };

  // -- 「我现在能不能走」：四档，判据按次序 ---------------------------------
  const anyRunning =
    drainSegment.running || transcode.running || offlineSegment.running || minutes.running;
  const walk: ChainWalkView = drainSegment.failed
    ? { line: '先别关机：落盘出了岔子，原因与命令写在落盘那一小块里。', mark: 'glyph--cross' }
    : drainSegment.running
      ? { line: '先别关机：落盘还没走完。', mark: 'glyph--draining' }
      : anyRunning
        ? { line: '先别关机：还有活没跑完；关掉页面不影响，服务照跑。', mark: 'glyph--rec' }
        : { line: '可以走了：这一场眼下没有在跑的活。', mark: 'glyph--idle' };

  return {
    segments: [drainSegment, transcodeSegment, offlineSegment, minutesSegment],
    walk,
  };
}
