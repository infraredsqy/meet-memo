import type { ReactNode } from 'react';
import {
  summaryLogLabel,
  type SummaryFailureView,
  type SummaryLogView,
  type SummaryState,
} from '../summary';
import type { SummaryController } from '../summary-controller';

/**
 * 位置归调用方（排在每路健康之后、告警带之前），这里只有面板本体：右上角一个 `关闭`。
 * **关掉即消失、不落文件** —— 记录仍在底部的本场总结记录里。
 */
function SummaryCard({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}): ReactNode {
  return (
    <div className="summary-card" id="summary-card">
      <div className="summary-card__head">
        <span className="summary-card__title" id="summary-card-title">
          {title}
        </span>
        <button className="btn btn--small btn--ghost" type="button" onClick={onClose}>
          关闭
        </button>
      </div>
      <div id="summary-card-body">{children}</div>
    </div>
);
}

function SummarySections({
  sections,
  emptyNote,
}: {
  sections: { title: string; items: string[] }[];
  emptyNote: string | null;
}): ReactNode {
  return (
    <>
      {emptyNote !== null && <p className="summary-card__item">{emptyNote}</p>}
      {sections.map((section) => (
        <div key={section.title}>
          <p className="summary-card__sub">{section.title}</p>
          {section.items.map((item, index) => (
            <p className="summary-card__item" key={`${section.title}-${index}`}>
              {item}
            </p>
))}
        </div>
))}
    </>
);
}

function SummaryFailureCard({
  view,
  onClose,
}: {
  view: { text: string; lines: string[] };
  onClose: () => void;
}): ReactNode {
  return (
    <SummaryCard title="本次摘要生成失败" onClose={onClose}>
      <p className="summary-card__item">{view.text}</p>
      {view.lines.map((line) => (
        <p className="summary-card__item" key={line}>
          {line}
        </p>
))}
    </SummaryCard>
);
}

export function SummaryPanels({
  summary,
  summaryState,
  summaryFailureText,
  logView,
  logError,
  summaryLogOpen,
  onCloseLog,
}: {
  summary: SummaryController;
  summaryState: SummaryState;
  summaryFailureText: SummaryFailureView | null;
  logView: SummaryLogView | null;
  logError: string | null;
  summaryLogOpen: boolean;
  onCloseLog: () => void;
}): ReactNode {
  return (
    <>
      {summaryState.phase === 'done' && summaryState.result !== null && (
        <section className="band band--quiet">
          <SummaryCard title={summaryState.result.title} onClose={() => summary.close()}>
            <SummarySections
              sections={summaryState.result.sections}
              emptyNote={summaryState.result.emptyNote}
            />
          </SummaryCard>
        </section>
)}

      {summaryFailureText !== null && (
        <section className="band band--quiet">
          <SummaryFailureCard view={summaryFailureText} onClose={() => summary.close()} />
        </section>
)}

      {/* **判据是两样取或**：手上有服务给的那一份（`logView`），**或者**有取不到时那句说明
          （`logError`）。只看前一半的话，本场**第一次**展开、而那一次 `GET /api/summaries`
          没取到时（网络错、服务忙、非 200），面板整块不出现 —— 控制层已经备好的那句说明
          够不着，入口那一行还恒写着「还没有」，用户看到的是「按了没反应」。 */}
      {summaryLogOpen && (logView !== null || logError !== null) && (
        <section className="band band--quiet">
          <SummaryCard
            title={logView === null ? summaryLogLabel(null) : logView.title}
            onClose={onCloseLog}
          >
            {logError !== null && <p className="summary-card__item">{logError}</p>}
            {logView !== null && logView.emptyNote !== null && (
              <p className="summary-card__item">{logView.emptyNote}</p>
)}
            {/* 外层 `.list` + 每条 `.list__row` 管**条目之间**的次序；每一条里面再套一层
                `.list` 管**起头那一行与三小段之间**的次序（`.list__row` 自带 flex，
                直接往里塞两行内容会把它们横着排成一行）。 */}
            {logView !== null && (
              <div className="list" id="summary-log-list">
                {logView.entries.map((entry) => (
                  <div className="list__row" key={entry.key}>
                    <div className="list">
                      <p className="summary-card__sub">
                        [{entry.time}] 取的「{entry.windowLabel}」
                      </p>
                      <SummarySections sections={entry.sections} emptyNote={entry.emptyNote} />
                    </div>
                  </div>
))}
              </div>
)}
          </SummaryCard>
        </section>
)}
    </>
);
}
