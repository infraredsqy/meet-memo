import { useCallback, useLayoutEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { Snapshot } from '../../contract';
import { rowTime, showJumpToLatest, type TranscriptRow } from '../transcript';

/**
 * 转写区（ui-design 2.5「转写区」一行）：实时转写的临时稿。区里不出现保存 / 导出 /
 * 正式记录这类字样。
 *
 * 展开 / 收起、贴底跟滚与「回到最新」都是这一区自己的事：三样状态住在这里，
 * 换场次清行归调用方（`rows` 清空后贴底效果自然回到最新一行）。
 */
export function TranscriptArea({
  rows,
  started,
  health,
}: {
  rows: TranscriptRow[];
  /** 这一场起过没有（空闲给空态文案；起过才摆滚动区）。 */
  started: boolean;
  /** 转写健康位：只在不正常时说话（延迟 / 停了才值得一行字）。 */
  health: Snapshot['transcript']['health'];
}): ReactNode {
  const [transcriptOpen, setTranscriptOpen] = useState(false);
  const [jumpVisible, setJumpVisible] = useState(false);
  const transcriptRef = useRef<HTMLDivElement | null>(null);

  // 转写：贴底就跟着滚动，用户翻上去了就不动他（设计章 2.3）。
  useLayoutEffect(() => {
    const box = transcriptRef.current;
    if (box === null) return;
    const sticking = !showJumpToLatest(box.scrollTop, box.scrollHeight, box.clientHeight);
    if (sticking) box.scrollTop = box.scrollHeight;
    setJumpVisible(!sticking);
  }, [rows]);

  const onTranscriptScroll = useCallback(() => {
    const box = transcriptRef.current;
    if (box === null) return;
    setJumpVisible(showJumpToLatest(box.scrollTop, box.scrollHeight, box.clientHeight));
  }, []);

  const onJump = useCallback(() => {
    const box = transcriptRef.current;
    if (box === null) return;
    box.scrollTop = box.scrollHeight;
    setJumpVisible(false);
  }, []);

  return (
    <section className="band transcript-block">
      <div className="band__head">
        <h2 className="band__title">实时转写</h2>
      </div>
      <div className="band__body">
        {/* 草稿标注照章 2.3 固定在区头；健康只在不正常时说话（延迟 / 停了才值得一行字） */}
        <div className="sumbar">
          <span className="tag">草稿 · 会后重建</span>
          {(health === 'behind' || health === 'stopped') && (
            <span className="tag">{health === 'behind' ? '延迟' : '已停止'}</span>
          )}
        </div>
        {!started && <p className="empty">开录后，两路的转写逐行显示在这里。</p>}
        {started && (
          <>
            <div
              className={`transcript ${transcriptOpen ? 'is-open' : ''}`}
              id="transcript-lines"
              ref={transcriptRef}
              onScroll={onTranscriptScroll}
            >
              {/* 断口行就是普通 transcript__line + transcript__gap（设计稿
                  `docs/ui/live.html` 里同一条用法）：不挂自造的修饰类，视觉与语义都归契约类名 */}
              {rows.map((row, index) => (
                <div
                  className="transcript__line"
                  key={`${row.at}-${index}`}
                >
                  <span className="transcript__time">
                    {/* 断口行带 [起–止] 区间（transcript.ts 的 gapRow 摆好），
                        普通行是 [HH:MM] —— 两样都由 rowTime() 给 */}
                    {rowTime(row)}
                  </span>
                  <span className="transcript__route">{row.route}</span>
                  {row.gap ? (
                    <span className="transcript__gap">{row.text}</span>
                  ) : (
                    <span className="transcript__text">{row.text}</span>
                  )}
                </div>
              ))}
            </div>
            {/* 「回到最新」在滚动区外面：滚动区自己滚起来时它不该跟着跑掉 */}
            {jumpVisible && (
              <div className="transcript__jump">
                <button className="btn btn--small btn--ghost" type="button" onClick={onJump}>
                  回到最新
                </button>
              </div>
            )}
            {/* 窄屏（720 以下）把转写折成一行「最近一句」，靠这个钮展开；桌面宽度下
                这一格由 CSS 收着（转写本来就在页面上） */}
            <div className="band__foot">
              <button
                className="btn btn--small transcript__toggle"
                type="button"
                aria-expanded={transcriptOpen}
                aria-controls="transcript-lines"
                onClick={() => setTranscriptOpen((open) => !open)}
              >
                {transcriptOpen ? '收起' : '展开'}{' '}
                <span className="transcript__toggle-hint">最近一行</span>
              </button>
            </div>
          </>
        )}
      </div>
    </section>
  );
}
