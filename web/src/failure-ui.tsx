import type { ReactNode } from 'react';
import type { ActionFailureView } from './failure';

/** 失败块的公共画法：原因、下一步、log 路径（可复制）。位置归调用方。 */
export function FailureBlock({
  failure,
  logPath = null,
  extraLines = [],
  /**
   * forbidden 那一档要不要带「没有失败信封…」说明行。会中主视图（App.tsx）默认带上；
   * 工作区首页（WorkspaceHome.tsx）沿 HomeFailure 的旧画法传 false——那条旧画法只有
   * why + next 两行（不复刻旧行为时首页会因为合并组件多出一行）。
   */
  forbiddenNote = true,
}: {
  failure: ActionFailureView;
  logPath: string | null;
  /** 额外的明细行（如收尾失败那几步）；有哪个就补哪几行。 */
  extraLines: string[];
  /** forbidden 档的说明行开关，默认 true（见上）。 */
  forbiddenNote?: boolean;
}): ReactNode {
  /**
   * extraLines 先对 detail 去重（web-drain-failure-double-lines）：会中收尾那一档
   * `drainFailure`（live/drain.ts）的 detail 已按逐字等价线摊进了收尾失败那几行，
   * App 又把 `drainFailedSteps` 传成 extraLines —— 同一行就被画两遍。这里只做
   * 字符串行**精确相等**的去重：已在 detail 里的 extra 行不再渲染，行序保持；
   * drain.ts 的 detail 构成与调用方传参都不动。
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
    // 兜底档：没有失败信封、也没有 code —— 界面不替它编一个。形状照 forbidden 那条。
    return (
      <div className="fail">
        <div className="fail__why">{failure.text}</div>
        <div className="fail__log">这一档没有失败信封、也没有 code。</div>
      </div>
    );
  }
  if (failure.kind === 'drain') {
    // 收尾失败那一档：形状照信封档，但首行没有 code 芯片（这个 code 服务没有回过）。
    return (
      <div className="fail">
        <div className="fail__why">{failure.message}</div>
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
