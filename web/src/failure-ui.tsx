import type { ReactNode } from 'react';
import type { ActionFailureView } from './failure';

export function FailureBlock({
  failure,
  logPath = null,
  extraLines = [],
  forbiddenNote = true,
}: {
  failure: ActionFailureView;
  logPath: string | null;
  extraLines: string[];
  forbiddenNote?: boolean;
}): ReactNode {
  /**
   * 同一行明细不画两遍 —— 只做字符串行**精确相等**的去重：已在 detail 里的 extra 行
   * 不再渲染，行序保持。
   */
  const detailSet = new Set('detail' in failure ? failure.detail : []);
  const extraUnique = extraLines.filter((line) => !detailSet.has(line));
  if (failure.kind === 'forbidden') {
    return (
      <div className="fail">
        <div className="fail__why">
          请求被拦截 · HTTP {failure.status}（未到达动作端点）
        </div>
        <div className="fail__next">{failure.text}</div>
        {forbiddenNote && (
          <div className="fail__log">
            这一档没有失败信封、也没有 code —— 服务在中间件那一道就把它挡回去了。
          </div>
)}
      </div>
);
  }
  if (failure.kind === 'unclassified') {
    return (
      <div className="fail">
        <div className="fail__why">{failure.text}</div>
        <div className="fail__log">这一档没有失败信封、也没有 code。</div>
      </div>
);
  }
  return (
    <div className="fail">
      <div className="fail__why">
        <code>{failure.code}</code> · {failure.message}
      </div>
      {failure.detail.length > 0 && (
        <ul className="fail__next">
          {failure.detail.map((line) => (
            <li key={line}>{line}</li>
))}
        </ul>
)}
      {extraUnique.length > 0 && (
        <ul className="fail__next">
          {extraUnique.map((line) => (
            <li key={line}>{line}</li>
))}
        </ul>
)}
      {logPath !== null && (
        <>
          <code className="codeblock">{logPath}</code>
          <div className="fail__log">log：{logPath}</div>
        </>
)}
    </div>
);
}
