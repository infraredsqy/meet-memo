import type { ReactNode } from 'react';
import type { TranscriptKind, TranscriptKindView } from '../transcripts';

/**
 * 稿子区（本节点，契约 4.13）：两份稿平级（会中稿 / 会后稿），每一份里两路都显示、
 * 不做按路筛选；默认会中稿（它一定有），两份都在时可以切。缺的那一路照常占位，
 * 给一句读得懂的缺稿话（服务给 `reason`，句子在 `../transcripts` 翻）。
 * 切在哪一份归主组件（换选中回默认），这里只摆。
 */
export function ScriptsArea({
  srcView,
  error,
  srcKind,
  onSrcKind,
}: {
  /** 稿子那一块的读数（切在哪一份就给哪一份的；取数没回来时是 `null`）。 */
  srcView: TranscriptKindView | null;
  /** 取稿失败那一行（如实画在稿子那一块里，不顶掉首页的失败块）。 */
  error: string | null;
  srcKind: TranscriptKind;
  onSrcKind: (kind: TranscriptKind) => void;
}): ReactNode {
  return (
    <div className="wssrc" aria-label="稿子">
      <div className="wssrc__head">
        <span className="wssrc__title">稿子</span>
        <div className="wssrc__tabs" role="tablist" aria-label="两份稿切换">
          {(['live', 'offline'] as const).map((kind) => (
            <button
              className={`wssrc__tab${srcKind === kind ? ' wssrc__tab--on' : ''}`}
              type="button"
              role="tab"
              aria-selected={srcKind === kind}
              key={kind}
              onClick={() => onSrcKind(kind)}
            >
              {kind === 'live' ? '会中稿' : '会后稿'}
            </button>
          ))}
        </div>
      </div>
      {error !== null && <p className="wssrc__bad">{error}</p>}
      {srcView === null ? (
        error === null && (
          <p className="wssrc__note">
            <span className="spinner" aria-hidden="true" />
            正在取这一场的稿子。
          </p>
        )
      ) : (
        <>
          {srcView.headline !== null && (
            <p className="wssrc__note">{srcView.headline}</p>
          )}
          {srcView.roads.map((road) => (
            <div className="wssrc__road" key={road.source} data-road={road.source}>
              <p className="wssrc__roadname">{road.label}</p>
              {road.present ? (
                <pre className="wssrc__text">{road.text}</pre>
              ) : (
                <p className="wssrc__note">{road.note}</p>
              )}
            </div>
          ))}
        </>
      )}
    </div>
  );
}
