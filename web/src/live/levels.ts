/**
 * 两路电平表的取数：那一条通道这一刻画成什么样。
 *
 * **一根柱、一格填充、一根峰值指针，全部来自服务**：包络是快照里
 * `sources[*].envelope` 的 15 个数（一根 2 秒），当前值与峰值优先取 `levels` 帧
 * （4 次/秒的高频通道），没有帧时退回快照上的 `level_dbfs` / `peak_dbfs`。
 * 界面在这里只做一件事：把 dBFS 线性映射成 0–100%（设计章 7.7 的 `--level` /
 * `--hold` / `--h`）。**没有任何动画、没有任何插值** —— 「看起来在动」这件事
 * 只由服务推来的新读数造成。
 *
 * **陈旧的帧要挡住**：`levels` 帧只在 `recording` / `alert` 两态来；一场结束后
 * 那些帧就不再来，最后那一帧会一直躺在手上。所以帧随快照的 `revision` 一起存，
 * 取数时要求 `frame.revision >= snapshot.revision` —— 快照往前走了（服务侧提交了
 * 新的事实）之后，手上那帧比它还旧就不能再顶着用。
 */

import type { LevelsData, Source } from '../contract';
import { dbfsToPercent } from './format';

/**
 * 线上必然是数字的读数收成数字。
 *
 * 契约层把电平写成 `number | '<dbfs>'`：`'<dbfs>'` 那一半是给 `contract/fixtures/`
 * 的 golden 样本用的掩码占位符（`docs/contract.md` 第 8 章）。真服务给的一律是数字，
 * 这一层的用途是让 TS 把那一半挡在门外 —— 它不是数字就按「没有读数」处理。
 */
function numeric(value: number | string | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** 两路各自的帧。 */
export interface LevelFrames {
  mic: LevelsData['frames']['mic'];
  loopback: LevelsData['frames']['loopback'];
}

/** 帧与它到达时快照的 `revision`。 */
export interface StoredLevels {
  revision: number;
  frames: LevelFrames;
}

export interface MeterView {
  /** 电平填充：`--level`。 */
  levelPercent: number;
  /** 峰值保持指针：`--hold`。 */
  holdPercent: number;
  /** 15 根历史包络柱的高度（`--h`），一根 2 秒；空数组表示这一档没有历史。 */
  envelope: number[];
  /** 读数 `−35.2 dBFS`；没有读数时由组件退成 `—`。 */
  readout: number | null;
  /** 这一路的历史与填充要冻结压灰（死了 / 数据已停）。 */
  frozen: boolean;
  /** 这一次电平是从 `levels` 帧来的（高频通道），还是从快照退回来的。 */
  fromFrame: boolean;
}

/**
 * 一条通道这一刻的电平。
 *
 * `source` 是快照上的那一路，`frames` 是手上那份 `levels` 帧。
 *
 * **包络优先取帧里的那一份**：`levels` 帧带 `envelope`（15 根，一根 2 秒），它 4 次/秒
 * 地来；快照里的 `sources[*].envelope` 只在有提交时刷新，录音途中往往还是开场那个空数组。
 * 两处同源（帧的 `envelope[-1]` 就是快照那根最新柱，契约 3.2 的结构不变量），
 * 拿帧里那一份画，「谁在说话、谁塌了」才是当下这 30 秒的形状。
 * 手上没有新鲜帧时才退回快照那一份（空闲 / 已结束两档本来就是空的）。
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

/**
 * 「数据已停」那一档手上有多少帧就吃多少帧。
 *
 * `dataStopped`（心跳失联或采集已停）为真时，视图层**停止消费新帧**：手上按住
 * `held` 那份不动，`incoming` 再新也不换 —— 电平、15 根包络全部停在被冻那一刻的值，
 * 不把估出来的数据继续画成在动（设计章 4.5：这是本项目最不能犯的错）。
 * 刚停住（`held` 还是空）就按住 `incoming` 这一份，把它冻在那一刻。
 */
export function holdFrameWhileDataStopped(
  held: StoredLevels | null,
  incoming: StoredLevels | null,
): StoredLevels | null {
  return held ?? incoming;
}

/** 数据回来（失联解除 / 采集恢复）那一刻松手：下一帧起回到实时。 */
export function releaseHeldFrame(): null {
  return null;
}
