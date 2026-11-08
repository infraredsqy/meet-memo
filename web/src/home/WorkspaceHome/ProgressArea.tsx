import { useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import type { ChunkRow, ProgressView } from '../progress';
import { chunkRowLabel, phaseMark } from '../progress';
import type { PostStopView } from '../post';

/**
 * 会后进度区（R27，节点 52b24392）：可展开收缩；展开是逐块行往下刷 + 每路一条的进度条。
 * 读数是 `GET /api/sessions/{id}` 那只纯读端点的原文，轮询只在有活动时开（轮询与取数
 * 归主组件）；「停着一版 running」按 `in_process` 分层如实画，不替盘上的话下结论。
 * 展开与收起是纯界面偏好，状态住在这里（不落本地存储）。
 */
export function ProgressArea({
  prog,
  statusError,
  stop,
  busy,
  chunkRows,
  onStop,
}: {
  /** 进度区的读数（有读数才算；读不出来就画失败那一行，`statusError`）。 */
  prog: ProgressView | null;
  /** 轮询/取数失败那一行（如实画在进度区里，不顶掉首页的失败块）。 */
  statusError: string | null;
  /** 停止入口（R28 / b1c6a58e）：这一场的 offline 在飞时出现在进度区头行。 */
  stop: PostStopView;
  busy: boolean;
  /** 逐块行（每跑完一块落一行；第一次读到时把已跑过的补成 `observed` 行）。 */
  chunkRows: ChunkRow[];
  onStop: () => void;
}): ReactNode {
  /** 进度区展开没有（纯界面偏好，只活在这一页的内存里，不落本地存储）。 */
  const [progOpen, setProgOpen] = useState(true);
  /** 阶段行的图形标记（`phaseMark()` 照 phase 给；没有标记就摆 spinner）。 */
  const progMark = prog === null ? null : phaseMark(prog.phase);

  return (
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
            受理即回——当前块收尾之后停，盘上状态记为「已中断」。 */}
        {stop.visible && (
          <button
            className="btn btn--small btn--ghost wsprog__stop"
            type="button"
            disabled={busy}
            title="在当前分块结束后停止；已完成的块保留在逐块记录中，可重新运行"
            onClick={onStop}
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
                    暂无逐块记录。
                  </p>
                )}
                {chunkRows.map((item) => (
                  <p className="wsprog__row" role="listitem" key={item.key}>
                    {chunkRowLabel(item)}
                  </p>
                ))}
              </div>
              {/* 四档合计与已运行秒数：给人看的读数（设计意图第 6、7 条那句话
                  是给维护者的口径，不摆到界面上） */}
              <p className="hint">
                {prog.elapsedSeconds === null
                  ? ''
                  : `已运行 ${Math.round(prog.elapsedSeconds)} 秒 · `}
                {prog.counts.transcribed + prog.counts.silent + prog.counts.failed + prog.counts.unjudged} 块 ·
                成功 {prog.counts.transcribed} · 静音 {prog.counts.silent} · 失败 {prog.counts.failed} ·
                未判定 {prog.counts.unjudged}
              </p>
            </>
          )}
        </div>
      )}
    </div>
  );
}
