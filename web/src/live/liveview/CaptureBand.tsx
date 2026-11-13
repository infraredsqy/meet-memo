import { useId } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { SUMMARY_WINDOWS, summaryLogLabel, summaryRunningNote } from '../summary';
import type { SummaryController } from '../summary-controller';
import type { ChannelView } from '../view';

/** 一路电平表：刻度、填充、峰值保持指针、15 根历史包络、当前读数。 */
function Channel({ channel }: { channel: ChannelView }): ReactNode {
  const id = useId();
  const { meter, health } = channel;
  const dead = health.dead;
  const frozen = meter.frozen;
  const cls = [
    'm-ch',
    dead ? 'm-ch--dead' : '',
    health.restored ? 'm-ch--restored' : '',
    frozen ? 'm-ch--frozen' : '',
    health.idle && !frozen ? 'm-ch--idle' : '',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <section className={cls} aria-label={channel.name} data-ch={channel.sourceId}>
      <div className="m-ch__head">
        <span className={`m-ch__mark m-ch__mark--${channel.mark}`} aria-hidden="true" />
        <span className="m-ch__name">{channel.name}</span>
        <span className="m-ch__device" title={channel.device ?? undefined}>
          {channel.device ?? '设备名未探测'}
        </span>
        <span className={`m-health m-health--${health.tone}`}>
          <span className={`glyph ${health.glyph}`} aria-hidden="true" />
          {health.text}
        </span>
        {/* 这一卡上没有开关：会中收音收在顶栏的四组合控件一处，
            旧的那颗「关掉 / 打开」小按钮不再以任何形态留在这里 */}
      </div>
      <div className="m-grid">
        {/* 左格：竖电平 + 读数。两样裹在同一格里，读数才落在井的正下方
            （`.m-grid` 的 meter 那一栏高度就是这个格子的高度） */}
        <div className="m-cell">
          <div
            className="m-meter m-meter--v"
            style={{ '--level': `${meter.levelPercent.toFixed(1)}%`, '--hold': `${meter.holdPercent.toFixed(1)}%` } as CSSProperties}
          >
            <div className="m-meter__scale">
              <span>0</span>
              <span>{'\u2212'}20</span>
              <span>{'\u2212'}40</span>
              <span>{'\u2212'}60</span>
            </div>
            <div className="m-meter__track">
              <span className="m-meter__ticks" />
              <span className="m-meter__fill" />
              <span className="m-meter__peak" />
              <span className="m-meter__mark">{'\u2715'}</span>
            </div>
          </div>
          <span className="m-ch__readout num">{channel.readout}</span>
        </div>
        <div className="m-scale" aria-hidden="true">
          <span style={{ left: '0%' }}>0</span>
          <span style={{ left: '33.333%' }}>{'\u2212'}20</span>
          <span style={{ left: '66.667%' }}>{'\u2212'}40</span>
          <span style={{ left: '100%' }}>{'\u2212'}60 dBFS</span>
        </div>
        <div className="m-env" aria-hidden="true">
          <span className="m-env__zero" />
          {meter.envelope.map((height, index) => (
            <span
              key={`${id}-bar-${index}`}
              className="m-env__bar"
              style={{ '--h': `${height.toFixed(1)}%` } as CSSProperties}
            />
))}
        </div>
        <div className="m-env-note">
          <span className="m-env__stamp">{channel.stamp}</span>
        </div>
      </div>
    </section>
);
}

/**
 * 采集带（ui-design 2.6）：两路左右并列，右侧三分之一给总结入口（方向 A）。
 * 本地 / 远端的名字与图形长在通道卡头行里，这里不再摆第二条图例。
 * 总结区那套控制（四档窗口 / 主按钮 / 本场记录入口）就住在这条带的右列 ——
 * 它们是「这次总结」的入口；结果与失败面板跟在带后（`./SummaryPanels`）。
 */
export function CaptureBand({
  channels,
  summary,
  summaryReady,
  summaryWhyText,
  summaryLogOpen,
  onToggleSummaryLog,
}: {
  channels: ChannelView[];
  /** 总结区那台状态机（窗口档位、进行中、按过的次数都读它）。 */
  summary: SummaryController;
  /** 能不能按：四态 + 心跳失联 + 采集已停 + 转写健康位，调用方算好。 */
  summaryReady: boolean;
  /** 按不动时旁边那一句原因；跑起来时是 `null`（按钮位换成进行中那句）。 */
  summaryWhyText: string | null;
  /** 本场总结记录那一块开着没有。 */
  summaryLogOpen: boolean;
  onToggleSummaryLog: () => void;
}): ReactNode {
  return (
    <section className="band">
      <div className="band__head">
        <h2 className="band__title">音频采集</h2>
      </div>
      <div className="band__body">
        <div className="m-band m-band--split m-band--tools">
          <div className="m-chans">
            {channels.map((channel) => (
              <Channel key={channel.sourceId} channel={channel} />
))}
          </div>
          <div className="live-tools">
            {/* 四个窗口档位（设计章 2.3）。缺省选中的是「15 分钟」；按下之后请求里带的
                取值来自 `SUMMARY_WINDOWS` —— 界面上写的是人话，请求里带的是契约取值 */}
            <div className="seg" role="group" aria-label="摘要时间范围">
              {SUMMARY_WINDOWS.map((item) => (
                <button
                  key={item.choice}
                  className={`seg__item ${summary.state().window === item.choice ? 'seg__item--on' : ''}`}
                  type="button"
                  aria-pressed={summary.state().window === item.choice}
                  disabled={!summaryReady}
                  onClick={() => summary.select(item.choice)}
                >
                  {item.label}
                </button>
))}
            </div>
            {/* 主按钮：录音中 / 告警态可用；跑起来时按钮位换成进行中那一句（不显示倒计时、
                不提前宣布失败）。其余各档按钮按不动，原因写在下面那一行 */}
            {summary.state().phase === 'running' ? (
              <p className="sumbar__note" id="summary-run">
                <span className="spinner" aria-hidden="true" />{' '}
                {summaryRunningNote(summary.state().window)}
              </p>
) : (
              <button
                className="btn btn--primary btn--block"
                type="button"
                id="summary-btn"
                disabled={!summaryReady}
                onClick={() => void summary.run()}
              >
                快速总结
              </button>
)}
            {summaryWhyText !== null && (
              <p className="sumbar__why" id="summary-why">
                {summaryWhyText}
              </p>
)}
            {/* 本场摘要记录：入口写着按过的次数，点开列每一次（时刻 + 窗口 + 那几条）。
                那一行在**还没取过服务那份**时先写「还没有」—— 这一刻手上确实一条都没有；
                取回来之后整行换成服务给的条目数。它是**本场**的，与结果面板两块内容各关各的 */}
            <button
              className="btn btn--ghost btn--block"
              type="button"
              id="summary-log-btn"
              aria-expanded={summaryLogOpen}
              onClick={onToggleSummaryLog}
            >
              {summaryLogLabel(summary.logCount())}
            </button>
          </div>
        </div>
      </div>
    </section>
);
}
