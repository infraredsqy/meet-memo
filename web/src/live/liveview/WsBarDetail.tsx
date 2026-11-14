import { useState } from 'react';
import type { ReactNode } from 'react';
import type { SessionRow, Snapshot } from '../../contract';
import { startButtonLabel } from '../../home/model';

/**
 * 会中屏左栏里那块**选中工作区的小详情**：清单里点哪一行，这里说哪一场 ——
 * 「切到别的工作区看详情」就落在这小块上。
 *
 * 「开始录音」是这一屏唯一受单场硬限制的动作：**在录时按下不发起、当场如实拒**
 * （指出在录的是哪一场、录了多久，并给「打开那一场」的去处 —— 把清单选区切回在录
 * 那一行；选区**已**是在录那一行时这只钮不摆——按下只会撞 `switchWorkspace` 的早退，
 * 是只空转的钮），不停在「没反应」。拒的话只从快照 `session` 上读，不编数；真发出去的
 * `start` 若被服务 409（快照陈旧那一拍）也走同一条失败块兜底。
 */
export function WsBarDetail({
  row,
  recording,
  busy,
  onStart,
  onOpenSession,
}: {
  row: SessionRow;
  recording: Snapshot['session'];
  busy: boolean;
  onStart: () => void;
  onOpenSession: () => void;
}): ReactNode {
  const [rejected, setRejected] = useState(false);
  const elapsed = recording?.elapsed_seconds ?? null;
  const elapsedLabel =
    elapsed === null
      ? ''
      : elapsed < 90
        ? `已录 ${Math.max(1, Math.round(elapsed))} 秒`
        : `已录 ${Math.round(elapsed / 60)} 分钟`;
  return (
    <div className="wsbar__detail" aria-label="选中工作区详情">
      <div className="wsbar__detailname">{row.session_id ?? row.dir}</div>
      <p className="wsbar__detailrow num">{row.dir}</p>
      <div className="wsbar__detailacts">
        <button
          className="btn btn--small btn--primary"
          type="button"
          disabled={busy}
          title={rejected ? undefined : '在这一场开始一段新录音；同一时刻只录一场'}
          onClick={() => {
            if (recording !== null) {
              // 在录时按下即如实拒，不发起 —— 话只从快照上读。
              setRejected(true);
              return;
            }
            onStart();
          }}
        >
          {startButtonLabel(row)}
        </button>
      </div>
      {rejected && recording !== null && (
        <div className="wsbar__reject">
          <p className="wsbar__why">
            已经有一场在录（{recording.id}
            {elapsedLabel === '' ? '' : `，${elapsedLabel}`}）。同一时刻只录一场。
          </p>
          {/* 选区已是在录行时这只钮没有真实去处（按下只会早退），不摆 */}
          {recording.dir !== row.dir && (
            <button className="btn btn--small btn--ghost" type="button" onClick={onOpenSession}>
              打开那一场 →
            </button>
)}
        </div>
)}
    </div>
);
}
