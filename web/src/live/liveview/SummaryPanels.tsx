import type { ReactNode } from 'react';
import {
  summaryLogLabel,
  type SummaryFailureView,
  type SummaryLogView,
  type SummaryState,
} from '../summary';
import type { SummaryController } from '../summary-controller';

/**
 * 总结结果面板与失败面板共用的那一块（设计章 2.3）。
 *
 * 位置归调用方（排在每路健康之后、告警带之前），这里只有面板本体：右上角一个 `关闭`，
 * 头一行是**这一次**的窗口（`刚才的 15 分钟`），底下按三小段摆那几条。
 * **关掉即消失、不落文件** —— 记录仍在底部的本场总结记录里。
 *
 * 面板本体（`.summary-card`）与三小段的类名都来自设计章 7.10 的类名契约，
 * 一个字都不新造。
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

/** 三小段：段标题 + 那几条（结果面板与记录回看两处共用同一种摆法）。 */
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

/** 失败面板：一句原因 +（有就有的）补充行，右上角同样是 `关闭`。 */
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

/**
 * 总结区带后的三块面板：这一次的结果、这一次的失败、本场总结记录 —— 三块互斥或
 * 各自显隐，谁有货谁出现；都没有时一块也不画。
 *
 * 「能不能按」由调用方算（四态 + 失联 + 采集停 + 转写健康位），这里的开关只看
 * 那台状态机（`summaryState`）与记录那一份的取数结果。
 */
export function SummaryPanels({
  summary,
  summaryState,
  summaryFailureText,
  logView,
  logError,
  summaryLogOpen,
  onCloseLog,
}: {
  /** 总结区那台状态机（结果面板与失败面板的 `关闭` 都走它的 `close()`）。 */
  summary: SummaryController;
  /** 状态机当前那份状态（`state()` 的读数，调用方每轮重画时取好）。 */
  summaryState: SummaryState;
  /** 这一次的失败视图（`summaryFailureView()` 的读数）；没失败是 `null`。 */
  summaryFailureText: SummaryFailureView | null;
  /** 本场记录那一份（服务给的原文）；没取到是 `null`。 */
  logView: SummaryLogView | null;
  /** 记录那一块取不到时给的那句话（取到了就是 `null`）。 */
  logError: string | null;
  /** 记录那一块开着没有。 */
  summaryLogOpen: boolean;
  onCloseLog: () => void;
}): ReactNode {
  return (
    <>
      {/* 总结结果面板（设计章 2.3 的主位第 5 项）：**位置在通道卡之后、告警带之前**。
          只有几条，关掉即消失、不落文件；记录仍在底部的本场总结记录里。
          结果与失败共用一块面板：一次总结只会落在其中一档 */}
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

      {/* 本场总结记录：入口在采集带右侧，展开的内容跟到这里（与结果面板同一处落位）。

          **判据是两样取或**：手上有服务给的那一份（`logView`），**或者**有取不到时那句说明
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
            {/* 每一次一条：`[<HH:MM>] 取的「<窗口档位>」` 起一小段，下面按
                讲什么 / 讲到哪 / 丢掉什么的次序列那几条（`/设计/task.md`
                的「记录回看」那一节的排版；与设计稿把每条压成一句话的写法不一致，
                这一处偏差记在交付报告里）。

                外层 `.list` + 每条 `.list__row` 管**条目之间**的次序；每一条里面再套一层
                `.list` 管**起头那一行与三小段之间**的次序（`.list__row` 自带 flex，
                直接往里塞两行内容会把它们横着排成一行）。两层的类名都来自设计章 7.10，
                一行内联样式、一个新造的类名都没有。 */}
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
