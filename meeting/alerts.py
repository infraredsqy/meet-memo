"""告警判定与阈值（phase2 §3.3）—— **服务侧是告警的唯一判定者**。

本模块只做**纯判定**：输入是帧 / 事实与阈值配置，输出是判定结果（该不该起致命级、该不该
恢复、这一条现在 `muted` 是真是假、该不该发提示级）。**它不持有快照、不写状态、不广播**
（把判定落到 `Snapshot` 上是 `ServiceRuntime` 的事），因此可以脱离服务单独单测。

口径（逐条照 phase2 §3.3）：

  - 门限**只有这一处**：采集后端只报原始电平（`LevelFrame`），静默秒数由本模块按
    `alert.silence_dbfs` **逐帧累计**，且**一律用帧自己的时间戳（`LevelFrame.ts`）**，
    不拿墙钟做差（慢机器上不虚报）；
  - **静默累计与恢复累计是两条独立的连续计数**，不许用同一个变量表达；恢复计数期间出现
    一帧低于门限 → 恢复计数清零，但**不改变 `top_state`**；
  - 提示级（`transcript.health` 掉档、`backlog_depth() ≥ BACKLOG_HINT_THRESHOLD`）
    进 `alerts.hints`，**同类合并的键是 `(level, source, message)` 三元组**、**上限 3**；
    合并时沿用原有那一条的 `since`、只刷新 `duration_seconds`；
  - 致命级进 `alerts.critical`，**同时只留一条**；
  - `muted` 由服务侧算出：`mute_session` 生效期间 **或** 这一条还在
    `alert.alarm_suppress_seconds` 的抑制窗口里 → `true`。抑制窗口按**「哪一路 + 故障类型」**
    跟踪（帧时间戳口径推进），界面侧不做任何窗口计算；
  - `muted` 的**唯一入口是 `AlertJudge.refresh_muted()`**，服务侧每次提交快照时调它按当下重算
    一遍：会话级静音复位、抑制窗口到点、下一场 `start` 复位之后，同一条告警算出来就回到
    `false`（这是一条「此刻会不会响」的当下判定，随窗口来去）；
  - 提示级合并也只有一份实现（`AlertJudge._merge_hint()`，由 `on_transcript_health()` /
    `on_backlog_depth()` 进入）：同一个三元组再次出现时沿用原有那条的 `since`，并把
    `duration_seconds` 刷新到当下；
  - **服务侧只许用更大的阈值，不许用更小的值兜底**（阈值只从配置读）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from meeting.state import ALERT_LEVEL, Alert, now_iso

_log = logging.getLogger("meeting.alerts")

#: 「待转写 ≥ 20 段」当落后前兆的量级（**写成常量、不加配置键**）
BACKLOG_HINT_THRESHOLD = 20

#: 提示级两类的来源标识（`(level, source, message)` 三元组里的 `source` 一栏）
TRANSCRIPT_SOURCE = "transcript"
BACKLOG_SOURCE = "transcript"

#: 提示级上限（同类合并之后 `hints` 的长度不超过它）
HINT_LIMIT = 3


# ---------------------------------------------------------------------------
# 阈值
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlertThresholds:
    """四条阈值的生效值（从配置读；**代码里不出现「取更小值兜底」那一类逻辑**）。"""

    silence_dbfs: float = -60.0
    silence_seconds: float = 180.0
    recover_seconds: float = 10.0
    alarm_suppress_seconds: float = 60.0

    @classmethod
    def from_config(cls, config: Any) -> "AlertThresholds":
        def value(key: str) -> Any:
            if config is None:
                from meeting import config as config_module

                return config_module.get(key)
            return config.get(key)

        return cls(
            silence_dbfs=float(value("alert.silence_dbfs")),
            silence_seconds=float(value("alert.silence_seconds")),
            recover_seconds=float(value("alert.recover_seconds")),
            alarm_suppress_seconds=float(value("alert.alarm_suppress_seconds")),
        )


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------


@dataclass
class SourceJudgement:
    """一帧之后对**这一路**的判定结果（runtime 把它落到快照上）。"""

    source: str
    alert: Alert | None = None
    recovered: bool = False
    below_threshold: bool = False


def muted_at(
    *, event_started_ts: float | None, moment: float, suppress_seconds: float
) -> bool:
    """抑制窗口这一半的纯判定：这一条事件在 `moment` 那一刻**还在不在**窗口里。

    窗口 = `[event_started_ts, event_started_ts + suppress_seconds)`；窗口起点为空
    （还没起过事件，或这一条已经恢复）→ `False`。会话级静音那一半由 `AlertJudge._muted()`
    在前面并进来。抽成纯函数是为了让「窗口到点就回到会出声」这件事可以直接断言。
    """
    if event_started_ts is None:
        return False
    return float(moment) < float(event_started_ts) + float(suppress_seconds)


@dataclass
class _SourceTrack:
    """一路的两条独立连续计数（静默 / 恢复）与当前事件的抑制窗口起点。"""

    silent_seconds: float = 0.0
    recover_seconds: float = 0.0
    last_ts: float | None = None
    silence_started_ts: float | None = None
    below: bool = False
    in_alert: bool = False
    event_key: str | None = None
    #: 当前这一条事件的**抑制窗口起点**（帧时间戳口径）：窗口 = `[起点, 起点 + alarm_suppress_seconds)`。
    #: 它只记「起点」这一个当下值，不累计历史 —— 窗口到点后 `muted` 由 `_muted()` 当场算出为假。
    event_started_ts: float | None = None


@dataclass
class AlertJudge:
    """逐帧累计 + 分级判定 + `muted` 计算（**不持有快照**）。"""

    thresholds: AlertThresholds = field(default_factory=AlertThresholds)
    mute_session: bool = False
    _tracks: dict[str, _SourceTrack] = field(default_factory=dict)
    _hints: dict[tuple[str, str, str], Alert] = field(default_factory=dict)
    #: 提示级的「现在」从哪来（提示级没有帧时间戳可依；单测可以注入一个可控的钟）
    _clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)
    #: 每条提示级首次出现的时刻（合并键 → 时刻），`duration_seconds` 由它算到当下
    _hint_started_at: dict[tuple[str, str, str], float] = field(default_factory=dict)

    def _now(self) -> float:
        """服务侧看表的那一刻（提示级的累计用它）。

        静默 / 恢复那两条计数与抑制窗口的推进都只认帧时间戳（`LevelFrame.ts`）；这里说的是
        服务侧自己的钟，给没有帧时间戳可依的那些判定用。
        """
        return float(self._clock())

    def refresh_muted(self, alerts: Iterable[Alert]) -> None:
        """**`muted` 的唯一入口**：按当下重算一批告警的 `muted`（每次提交快照时调一次）。

        判据 = `mute_session` 生效期间 **或** 这一条事件还在 `alarm_suppress_seconds` 的
        抑制窗口里。抑制窗口到点、`reset_session_mute()` 复位、下一场 `start` 复位之后，
        同一批条目在这里算出来的就是 `False`。致命级按 `Alert.source`（= 哪一路）取那一路的
        跟踪状态，提示级没有抑制窗口（它出声与否只由会话级静音决定），只读 `mute_session`。
        """
        for alert_source, alerts_of_source in self._group_by_source(alerts):
            track = self._tracks.get(alert_source)
            for alert in alerts_of_source:
                if alert.level != "critical" or track is None:
                    alert.muted = bool(self.mute_session)
                    continue
                alert.muted = self._muted(track, self._frame_moment(track))

    @staticmethod
    def _group_by_source(alerts: Iterable[Alert]) -> Iterable[tuple[str, list[Alert]]]:
        """把一批告警按 `source` 分组（致命级与提示级各归自己那一路的跟踪状态）。"""
        grouped: dict[str, list[Alert]] = {}
        for alert in alerts:
            grouped.setdefault(str(alert.source), []).append(alert)
        return list(grouped.items())

    # -- 会话级静音（下一场 `start` 时由 runtime 复位） --------------------

    def reset_session_mute(self) -> None:
        """下一场 `start` 时自动复位为「未静音」（**不改 `alarm.sound` 持久化值**）。"""
        self.mute_session = False

    def reset_for_new_session(self) -> None:
        """新一场开场：清空两条计数与全部提示级（会话级状态不复用上一场）。"""
        self.reset_session_mute()
        self._tracks.clear()
        self._hints.clear()
        self._hint_started_at.clear()

    # -- 电平帧 -----------------------------------------------------------

    def on_level_frame(self, source: str, ts: float, dbfs: float) -> SourceJudgement:
        """收一帧电平事实，回答「这一路现在该起致命级 / 该恢复 / 都没变」。"""
        track = self._tracks.setdefault(source, _SourceTrack())
        ts = float(ts)
        previous = track.last_ts
        judgement = SourceJudgement(source=source)

        if float(dbfs) < self.thresholds.silence_dbfs:
            # 低于门限：静默累计增长、恢复累计清零（**两条独立的计数**）
            if not track.below:
                track.below = True
                track.silence_started_ts = ts
                previous = None  # 静默的秒数从这一帧起算
            delta = 0.0 if previous is None else max(0.0, ts - float(previous))
            track.last_ts = ts
            track.silent_seconds += delta
            track.recover_seconds = 0.0
            judgement.below_threshold = True
            if track.silent_seconds >= self.thresholds.silence_seconds and not track.in_alert:
                judgement.alert = self._raise_critical(
                    track,
                    source=source,
                    ts=ts,
                    message=(
                        f"{source} 这一路静默已连续达 {self.thresholds.silence_seconds:g} 秒"
                        f"（门限 {self.thresholds.silence_dbfs:g} dBFS）"
                    ),
                )
            return judgement

        # 有信号：静默累计清掉（恢复后再发生算新事件），恢复累计往上走
        if track.below:
            previous = None  # 恢复的秒数从这一帧起算
        delta = 0.0 if previous is None else max(0.0, ts - float(previous))
        track.last_ts = ts
        track.below = False
        track.silent_seconds = 0.0
        track.silence_started_ts = None
        track.recover_seconds += delta
        if track.in_alert and track.recover_seconds >= self.thresholds.recover_seconds:
            judgement.recovered = True
        return judgement

    def _raise_critical(
        self, track: _SourceTrack, *, source: str, ts: float, message: str
    ) -> Alert:
        """起一条致命级（**同时只留一条**由 runtime 落盘时保证），并记下抑制窗口的起点。"""
        moment = float(track.last_ts if track.last_ts is not None else ts)
        muted = self._muted(track, moment)
        track.in_alert = True
        track.event_key = message
        # 这一条事件的抑制窗口从**当下**起算；下一帧起 `_muted()` 会按它算出「还在窗口里」。
        # 窗口是「有期状态」：到点之后 `_muted()` 当场算出假，不复用任何历史痕迹。
        track.event_started_ts = moment
        return Alert(
            level="critical",
            source=source,
            since=now_iso(),
            duration_seconds=float(track.silent_seconds),
            message=message,
            muted=muted,
        )

    def is_muted(self, source: str) -> bool:
        """某一路当前这一条事件是不是「不出声」。"""
        track = self._tracks.get(source)
        if track is None:
            return bool(self.mute_session)
        return self._muted(track, self._frame_moment(track))

    def _frame_moment(self, track: _SourceTrack) -> float:
        """帧时间戳口径的「此刻」：这一路最近一帧的时刻。

        抑制窗口按帧时间戳推进（§3.3.1 的累计口径），判断窗口到点时因此也用同一个口径；
        这一路一帧都还没来过时退回服务侧看表的那一刻。
        """
        if track.last_ts is not None:
            return float(track.last_ts)
        return self._now()

    def _muted(self, track: _SourceTrack, moment: float) -> bool:
        """这一条事件**在 `moment` 这一刻**会不会出声（**会话级静音 ∨ 抑制窗口**）。

        两个输入都取当下的值：会话级静音开关 + 这一条事件的窗口起点。于是它每次算出来的
        都是「此刻会不会响」这一个答案。
        """
        if self.mute_session:
            return True
        return muted_at(
            event_started_ts=track.event_started_ts,
            moment=moment,
            suppress_seconds=float(self.thresholds.alarm_suppress_seconds),
        )

    # -- 流中断事实 -------------------------------------------------------

    def on_source_lost(self, source: str, ts: float, reason: str = "") -> Alert:
        """后端报「这一路的流中断了」：立刻起致命级（不等静默阈值）。"""
        track = self._tracks.setdefault(source, _SourceTrack())
        track.below = False
        track.in_alert = True
        track.last_ts = float(ts)
        message = f"{source} 这一路的采集流中断了"
        if reason:
            message = f"{message}（{reason}）"
        track.event_key = message
        muted = self._muted(track, float(ts))
        track.event_started_ts = float(ts)
        return Alert(
            level="critical",
            source=source,
            since=now_iso(),
            duration_seconds=0.0,
            message=message,
            muted=muted,
        )

    def mark_recovered(self, source: str) -> None:
        """一路恢复回 `recording`：把它从「在告警里」摘出来，并关掉这一条事件的抑制窗口。

        关窗口是契约点名的「**恢复后再发生算新事件**」：下一次再起告警时窗口从那一刻重新开
        60 秒，于是新的那一条该出声（`alerts.*.muted` 说的就是这条事件此刻会不会响）。
        """
        track = self._tracks.get(source)
        if track is None:
            return
        track.in_alert = False
        track.silent_seconds = 0.0
        track.recover_seconds = 0.0
        track.silence_started_ts = None
        track.event_started_ts = None

    # -- 提示级 -----------------------------------------------------------

    def on_transcript_health(self, health: str) -> Alert | None:
        """`transcript.health` 掉档（`behind` / `stopped`）→ 出一条提示级；`normal` → 撤回。"""
        if health == "normal":
            self._hints.pop(("hint", TRANSCRIPT_SOURCE, _health_message("normal")), None)
            return None
        return self._merge_hint(
            level="hint",
            source=TRANSCRIPT_SOURCE,
            message=_health_message(health),
        )

    def on_backlog_depth(self, depth: int) -> Alert | None:
        """`backlog_depth() ≥ BACKLOG_HINT_THRESHOLD` → 出一条提示级。

        **文案里不写当前段数**：`message` 是这条提示的故障类型标识，也是同类合并键的一部分
        （§3.3.2 的三元组），把每次变化的段数写进去就等于每次都是新键、合并永远不会发生。
        段数本身只进 log 与 `/debug`。
        """
        if int(depth) < BACKLOG_HINT_THRESHOLD:
            return None
        _log.debug("待转写段数 %s（阈值 %s）", int(depth), BACKLOG_HINT_THRESHOLD)
        return self._merge_hint(
            level="hint",
            source=BACKLOG_SOURCE,
            message=f"待转写段数已达 {BACKLOG_HINT_THRESHOLD} 段",
        )

    def _merge_hint(self, *, level: str, source: str, message: str) -> Alert:
        """按 `(level, source, message)` 三元组合并：沿用原有那条的 `since`，刷新 `duration_seconds`。

        **本 Phase 里提示级合并的唯一实现**：同一个键再次出现时不新增条目，`since` 保持首次
        发生时刻，`duration_seconds` 刷新到当下（= 时刻差，从这条提示第一次出现算起）。
        """
        key = (level, source, message)
        now = self._now()
        existing = self._hints.get(key)
        if existing is not None:
            started = self._hint_started_at.get(key, now)
            existing.duration_seconds = max(0.0, now - float(started))
            existing.muted = self.mute_session
            return existing
        self._hint_started_at[key] = now
        alert = Alert(
            level=level,
            source=source,
            since=now_iso(),
            duration_seconds=0.0,
            message=message,
            muted=self.mute_session,
        )
        self._hints[key] = alert
        return alert

    def reset_hints(self) -> None:
        """清空提示级（新一场开场 / 服务启动时用；**不改**致命级与两条计数）。"""
        self._hints.clear()
        self._hint_started_at.clear()

    def hints(self) -> list[Alert]:
        """当前提示级列表：**保留 `since` 最早的三条、丢掉其余**（判定可算，无空档）。

        超出上限这件事同样在这里留痕（丢掉的那条只进 log，不进快照）。
        """
        ordered = sorted(self._hints.values(), key=lambda item: item.since)
        kept = ordered[:HINT_LIMIT]
        for item in ordered[HINT_LIMIT:]:
            _log.warning("提示级超过上限 %s，丢弃：%s", HINT_LIMIT, item.message)
        return kept


def _health_message(health: str) -> str:
    if health == "behind":
        return "转写落后了"
    if health == "stopped":
        return "转写停了"
    return "转写正常"


def build_hint(
    *, source: str, message: str, duration_seconds: float = 0.0, muted: bool = False
) -> Alert:
    """组装一条提示级（供 runtime 与纯单测使用；字段集与其他 `Alert` 一致）。"""
    return Alert(
        level="hint",
        source=source,
        since=now_iso(),
        duration_seconds=float(duration_seconds),
        message=message,
        muted=bool(muted),
    )


__all__ = [
    "ALERT_LEVEL",
    "BACKLOG_HINT_THRESHOLD",
    "BACKLOG_SOURCE",
    "HINT_LIMIT",
    "TRANSCRIPT_SOURCE",
    "AlertJudge",
    "AlertThresholds",
    "SourceJudgement",
    "build_hint",
    "muted_at",
]
