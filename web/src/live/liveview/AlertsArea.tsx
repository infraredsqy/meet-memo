import type { ReactNode } from 'react';
import type { HeartbeatState } from '../heartbeat';
import type { LiveView as LiveViewModel } from '../view';

/**
 * 失联时调用方把整块收掉（两句话不得同屏）；「查看日志」的开合状态归调用方 ——
 * 整块收掉再回来时不重置。
 */
export function AlertsArea({
  view,
  heartbeat,
  logPath,
  busy,
  logShown,
  onToggleLog,
  onResume,
  onStop,
  onMute,
}: {
  view: LiveViewModel;
  heartbeat: HeartbeatState;
  logPath: string | null;
  busy: boolean;
  logShown: boolean;
  onToggleLog: () => void;
  onResume: () => void;
  onStop: () => void;
  onMute: () => void;
}): ReactNode {
  const stalled = view.stalled;

  return (
    <section className="band band--alert" aria-label="告警">
      {/* 「暂停中」与「哪一路死了」同一个级别：整行横带、左沿一道实心条。它排在最前面 ——
          这一屏最要紧的一句话就是「现在没在录」 */}
      {view.pause !== null && (
        <div className="alert alert--paused" role="status">
          <div className="alert__head">
            <span className="alert__glyph" aria-hidden="true">
              {'\u2016'}
            </span>
            <span className="alert__title">{view.pause.title}</span>
          </div>
          <div className="alert__body">{view.pause.body}</div>
          <div className="alert__acts">
            <button
              className="btn btn--small"
              type="button"
              disabled={busy}
              onClick={onResume}
            >
              继续
            </button>
            <button
              className="btn btn--small btn--danger"
              type="button"
              disabled={busy}
              onClick={onStop}
            >
              停止录音
            </button>
          </div>
        </div>
)}
      {view.fatal !== null && view.critical !== null && (
        <div className="alert alert--fatal">
          <div className="alert__head">
            <span className="alert__glyph" aria-hidden="true">
              {'\u25B2'}
            </span>
            <span className="alert__where">{view.fatal.where}</span>
            <span className="alert__since">{view.fatal.since}</span>
            <span className="alert__elapsed">{view.fatal.elapsed}</span>
          </div>
          <div className="alert__acts">
            <button
              className="btn btn--small"
              type="button"
              aria-pressed={view.critical.muted}
              disabled={busy}
              onClick={onMute}
            >
              {view.muteLabel}
            </button>
            <button
              className="btn btn--small btn--ghost"
              type="button"
              onClick={onToggleLog}
            >
              查看日志
            </button>
            <button
              className="btn btn--small btn--danger"
              type="button"
              disabled={busy}
              onClick={onStop}
            >
              停止录音
            </button>
          </div>
          {logShown && logPath !== null && <code className="codeblock">{logPath}</code>}
        </div>
)}

      {stalled !== null && view.fatal === null && (
        <div className="alert alert--fatal">
          <div className="alert__title">采集已停止</div>
          <div className="alert__body">
            服务仍在应答，但已连续 {stalled.seconds.toFixed(1)} 秒未收到任何一路的音频帧，
            最后一次收到音频帧是 {stalled.lastFrameAt}。这段时间里两路的内容都记不下来。
          </div>
          <div className="alert__body">
            先不要关机，也不要关闭本页。到服务那边确认一下：看日志里最后一次收到音频帧
            那一段在报什么{logPath === null ? '' : `；日志在 ${logPath}`}。
          </div>
        </div>
)}

      {view.hints.map((hint) => (
        <div className="alert alert--hint" key={`${hint.since}-${hint.text}`}>
          <span className="alert__glyph" aria-hidden="true">
            {'\u25B2'}
          </span>
          <span className="alert__since">{hint.since}</span>
          <span>{hint.text}</span>
        </div>
))}

      {/* 「本场曾中断」提示行：帧空洞记录是历史事实（录中冻结解冻后
          发帧线程报上来的），提示级 —— 只上一行字，不出声、不弹红横幅、不进两拍计数。
          它与「采集已停」那一条分工：那条答「现在停没停」（恢复即收），
          这条说的是 recording_alive 说不出的那句「那一段内容没记上」 */}
      {view.gapNote !== null && (
        <div className="alert alert--hint" role="status">
          <span className="alert__glyph" aria-hidden="true">
            {'\u25B2'}
          </span>
          <span>{view.gapNote}</span>
        </div>
)}

      {/* 心跳迟到：一行弱提示，不出声、无横幅 */}
      {heartbeat.level === '迟到' && heartbeat.lateText !== null && (
        <div className="heartbeat heartbeat--inline">{heartbeat.lateText}</div>
)}
    </section>
);
}
