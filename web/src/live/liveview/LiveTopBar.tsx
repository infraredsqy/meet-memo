import { useState } from 'react';
import type { ReactNode } from 'react';
import type { Snapshot } from '../../contract';
import type { GpuBusyView } from '../../home/progress';
import { GpuCell } from '../../home/WorkspaceRail';
import { SettingsDrawer } from '../../settings/SettingsDrawer';
import { COMBO_LABELS, COMBO_ORDER, type CaptureCombo } from '../combo';
import { clockLabelWhileLost, frozenClockValue, recoveredNoteText } from '../heartbeat';
import type { LiveView as LiveViewModel } from '../view';

/**
 * 会中屏的顶栏：左边状态胶囊与「已恢复」一行，中间时长与主按钮 ——
 * 空闲是「开始录音」，录音 / 告警是四组合直切 + 「结束录音」，右边忙闲一格与设置钮。
 * 设置抽屉跟这颗按钮是一对，开关状态也住在这里；四组合的受理反馈与
 * 「已恢复」一行由调用方算好递进来。
 */
export function LiveTopBar({
  snapshot,
  view,
  lost,
  recoveredGapSeconds,
  busy,
  shownCombo,
  gpu,
  onCombo,
  onStart,
  onStop,
  onSetting,
  onReprobe,
}: {
  snapshot: Snapshot;
  view: LiveViewModel;
  lost: boolean;
  recoveredGapSeconds: number | null;
  busy: boolean;
  /** 四组合控件此刻亮哪一档：半途亮按下去的那档（乐观），落地后亮快照说的那档。 */
  shownCombo: CaptureCombo;
  gpu: GpuBusyView;
  onCombo: (combo: CaptureCombo) => void;
  onStart: () => void;
  onStop: () => void;
  onSetting: (key: string, value: unknown) => void;
  onReprobe: () => void;
}): ReactNode {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const showStart = snapshot.top_state === 'idle';
  const showEnd = snapshot.top_state === 'recording' || snapshot.top_state === 'alert';
  const capsuleTone =
    view.status.tone === 'alert'
      ? 'statusline__caps--alert'
      : view.status.tone === 'on'
        ? 'statusline__caps--on'
        : '';

  return (
    <>
      <header className={`topbar ${lost ? 'topbar--hb-lost' : ''}`}>
        <div className="topbar__left">
          <div className="statusline">
            <span className={`statusline__caps ${capsuleTone}`}>
              <span className={`glyph ${view.status.glyph}`} aria-hidden="true" />
              <span className="statusline__name">{view.status.name}</span>
            </span>
          </div>
          {recoveredGapSeconds !== null && (
            <span className="recovered-note">{recoveredNoteText(recoveredGapSeconds)}</span>
)}
        </div>

        <div className="topbar__main">
          <span className={`clock ${view.clock.faint ? 'clock--faint' : ''}`}>
            {/* 失联：标签改「最后读数」，数字也一起冻在最后一份快照的已录时长上
                —— 数字不许再走 */}
            <span className="clock__label">
              {lost ? clockLabelWhileLost() : view.clock.label}
            </span>
            <span className="clock__value num">
              {lost
                ? frozenClockValue(snapshot.session?.elapsed_seconds)
                : view.clock.value}
            </span>
          </span>
          {showStart && (
            <button
              className="btn btn--primary btn--start"
              type="button"
              disabled={busy}
              onClick={onStart}
            >
              开始录音
            </button>
)}
          {showEnd && (
            /* 会中收音的四组合直切：暂停与两路开关收成这一处集中控制，
                一次只处在四档之一，切换即到位。半途（`pendingCombo` 有值）高亮跟着
                按下去的那一档走，受理即有反馈；「全停」选中那一格是硫红实心
                （live.css 按 `data-combo` 换色），与「在录」的青色分得开 */
            <div className="seg" role="group" aria-label="收音组合" data-combo={shownCombo}>
              {COMBO_ORDER.map((combo) => (
                <button
                  key={combo}
                  className={`seg__item ${shownCombo === combo ? 'seg__item--on' : ''}`}
                  type="button"
                  aria-pressed={shownCombo === combo}
                  disabled={busy}
                  onClick={() => onCombo(combo)}
                >
                  {COMBO_LABELS[combo]}
                </button>
))}
            </div>
)}
          {showEnd && (
            <button
              className="btn btn--primary btn--end"
              type="button"
              disabled={busy}
              onClick={onStop}
            >
              停止录音
            </button>
)}
        </div>

        <div className="topbar__right">
          {/* 全局只留这一格「显卡忙不忙」：画法与工作区栏底共用同一份
              （`WorkspaceRail.tsx` 的 `GpuCell`）。全站没有全局进度条 ——
              进度跟着各自的工作区走。 */}
          <GpuCell gpu={gpu} />
          <button
            className="btn btn--small btn--ghost"
            type="button"
            aria-haspopup="dialog"
            aria-expanded={settingsOpen}
            onClick={() => setSettingsOpen(true)}
          >
            设置
          </button>
        </div>
      </header>

      {/* 设置抽屉：ASR 那五个参数不进这扇窗 */}
      {settingsOpen && (
        <SettingsDrawer
          snapshot={snapshot}
          onClose={() => setSettingsOpen(false)}
          onSetting={onSetting}
          onReprobe={onReprobe}
        />
)}
    </>
);
}
