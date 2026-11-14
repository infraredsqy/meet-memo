/**
 * 两路电平表的取数：那一条通道这一刻画成什么样。
 *
 * **一根柱、一格填充、一根峰值指针，全部来自服务**：包络是快照里 `sources[*].envelope`
 * 的 15 个数，当前值与峰值优先取 `levels` 帧，没有帧时退回快照。界面只做一件事：把 dBFS
 * 线性映射成 0–100%。**没有任何动画、没有任何插值** —— 「看起来在动」只由服务推来的新读数造成。
 *
 * **陈旧的帧要挡住**：帧随快照的 `revision` 一起存，取数时要求
 * `frame.revision >= snapshot.revision`。
 */

import type { LevelsData, Source } from '../contract';
import { dbfsToPercent } from './format';

/**
 * 线上必然是数字的读数收成数字：契约层把电平写成 `number | '<dbfs>'`，这一层让 TS 把占位符
 * 那一半挡在门外 —— 它不是数字就按「没有读数」处理。
 */
function numeric(value: number | string | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

export interface LevelFrames {
  mic: LevelsData['frames']['mic'];
  loopback: LevelsData['frames']['loopback'];
}

/** 帧与它到达时快照的 `revision`（陈旧帧判据靠它）。 */
export interface StoredLevels {
  revision: number;
  frames: LevelFrames;
}

export interface MeterView {
  levelPercent: number;
  holdPercent: number;
  /** 15 根历史包络柱的高度（`--h`）；空数组表示这一档没有历史。 */
  envelope: number[];
  /** 没有读数时由组件退成 `—`。 */
  readout: number | null;
  /** 这一路的历史与填充要冻结压灰（死了 / 数据已停）。 */
  frozen: boolean;
  /** 这一次电平是从 `levels` 帧来的（高频通道），还是从快照退回来的。 */
  fromFrame: boolean;
}

/**
 * 一条通道这一刻的电平。
 *
 * **包络优先取帧里的那一份**：`levels` 帧带 `envelope`，它 4 次/秒 地来；快照里的
 * `sources[*].envelope` 只在有提交时刷新，录音途中往往还是开场那个空数组。两处同源（帧的
 * `envelope[-1]` 就是快照那根最新柱），拿帧里那一份画才是当下这 30 秒的形状。
 */
export function meterView(
  source: Source,
  frames: StoredLevels | null,
  snapshotRevision: number,
): MeterView {
  const fresh = frames !== null && frames.revision >= snapshotRevision;
  const frame = fresh ? frames.frames[source.id === 'mic' ? 'mic' : 'loopback'] : null;

  const level = frame !== null ? numeric(frame.dbfs) : numeric(source.level_dbfs);
  const peak = frame !== null ? numeric(frame.peak) : numeric(source.peak_dbfs);
  const bars = frame !== null && frame.envelope.length > 0 ? frame.envelope : source.envelope;

  return {
    levelPercent: dbfsToPercent(level),
    holdPercent: dbfsToPercent(peak),
    envelope: bars.map((value) => dbfsToPercent(numeric(value))),
    readout: level,
    frozen: false,
    fromFrame: frame !== null,
  };
}

/** 「数据已停」那一档手上有多少帧就吃多少帧：**停止消费新帧**，手上按住的那份不动，不把估出来的数据继续画成在动。刚停住时就按住 `incoming` 这一份。 */
export function holdFrameWhileDataStopped(
  held: StoredLevels | null,
  incoming: StoredLevels | null,
): StoredLevels | null {
  return held ?? incoming;
}

/** 数据回来那一刻松手：下一帧起回到实时。 */
export function releaseHeldFrame(): null {
  return null;
}
