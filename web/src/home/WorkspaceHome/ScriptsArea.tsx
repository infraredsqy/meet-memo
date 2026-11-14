import type { ReactNode } from 'react';
import type { TranscriptKind, TranscriptKindView } from '../transcripts';

export function ScriptsArea({
  srcView,
  error,
  srcKind,
  onSrcKind,
}: {
  srcView: TranscriptKindView | null;
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
