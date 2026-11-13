"""告警判定与阈值（设计口径）——服务侧是告警的唯一判定者。

纯判定：输入是帧 / 事实与阈值配置，输出判定结果；不持有快照、不写状态、不广播，
可脱离服务单独单测。口径（逐条照设计口径）：

- 门限只有这一处：静默秒数按 `alert.silence_dbfs` 逐帧累计，一律用帧自己的时间戳
  （`LevelFrame.ts`），不拿墙钟做差（慢机器上不虚报）；
- 静默累计与恢复累计是两条独立的连续计数；恢复期间出现一帧低于门限 → 恢复计数清零，
  但不改 `top_state`；
- 暂停区间不计入这两条计数（`docs/design-intent.md` 第 17 条：告警不许一惊一乍），
  恢复后的第一帧从零起算；
- 提示级进 `alerts.hints`：合并键是 `(level, source, message)` 三元组、上限 3，
  合并沿用原 `since` 只刷新 `duration_seconds`，超限的键在合并那一刻就从登记里回收；
  有两条对称的清除路径（`normal` 撤健康度两条、积压回落撤积压一条）；
- 致命级进 `alerts.critical`，同时只留一条；`muted` = 会话级静音 ∨ 抑制窗口
  （按「哪一路 + 故障类型」跟踪，帧时间戳口径推进），首发那一帧给 `false`（界面据此响一声）；
- `muted` 的唯一入口是 `AlertJudge.refresh_muted()`，每次提交快照时按当下重算；
  窗口到点本身不发广播，「窗口已过、下一次提交还没来」那段快照上仍是旧值
  （口径见 `docs/state.md` 第 5 章）；
- 服务侧只许用更大的阈值，不许用更小的值兜底（阈值只从配置读）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from meeting.state import ALERT_LEVEL, Alert, now_iso

_log = logging.getLogger("meeting.alerts")

#: 「待转写 ≥ 20 段」当落后前兆的量级（写成常量、不加配置键）
BACKLOG_HINT_THRESHOLD = 20

#: 提示级两类的来源标识（`(level, source, message)` 三元组里的 `source` 一栏）
TRANSCRIPT_SOURCE = "transcript"
BACKLOG_SOURCE = "transcript"

#: 提示级上限（同类合并之后 `hints` 的长度不超过它）
HINT_LIMIT = 3


# 阈值


@dataclass(frozen=True)
class AlertThresholds:
    """四条阈值的生效值（从配置读；代码里不出现「取更小值兜底」那一类逻辑）。"""

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


# 判定结果


@dataclass
class SourceJudgement:
    """一帧之后对这一路的判定结果（runtime 把它落到快照上）。"""

    source: str
    alert: Alert | None = None
    recovered: bool = False
    below_threshold: bool = False


def muted_at(
    *, last_raise_ts: float | None, moment: float, suppress_seconds: float
) -> bool:
    """抑制窗口这一半的纯判定：`moment` 是否在 `[last_raise_ts, last_raise_ts + suppress_seconds)` 内。

    窗口起点为空（这一路还没 raise 过或已恢复）→ `False`。锚点取上一次 raise 的时刻，
    而锚点要等这次算完才写进去——首发那一次算出来是 `False`，界面据此响一声。
    """
    if last_raise_ts is None:
        return False
    return float(moment) < float(last_raise_ts) + float(suppress_seconds)


@dataclass
class _SourceTrack:
    """一路的两条独立连续计数（静默 / 恢复）与上一次 raise 的时刻。"""

    silent_seconds: float = 0.0
    recover_seconds: float = 0.0
    last_ts: float | None = None
    silence_started_ts: float | None = None
    below: bool = False
    in_alert: bool = False
    event_key: str | None = None
    #: 上一次 raise 的时刻（帧时间戳口径）。`_raise_critical()` / `on_source_lost()` 都是
    #: 先算 `muted`、后写这个字段，于是首发算出来是 `False`（该响一声），此后重算才 `True`。
    last_raise_ts: float | None = None
    #: 刚起、还没被广播出去的那一条致命级（首发帧）。
    #:
    #: `commit()` 写下锚点后会重算一遍 `muted`，那次重算就发生在锚点刚开的同一刻——
    #: 没有这个记号的话首发帧会被自己刚开的窗口压成 `muted: true`（界面一声不响）。
    #: `refresh_muted()` 认出「正是这一条还没出去」就原样放行并清空记号。
    #:
    #: 隐含前提：每次 raise 之后必有一次提交。同一路连 raise 两次中间没提交时，第二次
    #: 会覆盖第一次的引用，那条广播出去时已被压成 `true`（静默退回旧行为，不报错）。
    #: 当前到不了这条路径（raise 与紧随的 `commit()` 之间没有 `await`），留这段是把前提
    #: 写下来：将来谁在中间插进一个 `await`，这一格就会从「必然走完」变成「看调度」。
    raise_in_flight: Alert | None = None


@dataclass
class AlertJudge:
    """逐帧累计 + 分级判定 + `muted` 计算（不持有快照）。"""

    thresholds: AlertThresholds = field(default_factory=AlertThresholds)
    mute_session: bool = False
    #: 本场暂停中：暂停区间不计入静默 / 恢复两条累计（`docs/design-intent.md` 第 17 条）。
    #: 置位期间 `on_level_frame()` 只回答「什么都没变」；`resume_session()` 清掉每条计数
    #: 的基准，恢复后第一帧从零起算，暂停那一段不会以「一帧跨几分钟」的形式补记进去。
    paused: bool = False
    _tracks: dict[str, _SourceTrack] = field(default_factory=dict)
    _hints: dict[tuple[str, str, str], Alert] = field(default_factory=dict)
    #: 提示级的「现在」从哪来（提示级没有帧时间戳可依；单测可注入可控的钟）
    _clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)
    #: 每条提示级首次出现的时刻（合并键 → 时刻），`duration_seconds` 由它算到当下
    _hint_started_at: dict[tuple[str, str, str], float] = field(default_factory=dict)

    def _now(self) -> float:
        """服务侧看表的那一刻（给没有帧时间戳可依的判定用）。"""
        return float(self._clock())

    def refresh_muted(self, alerts: Iterable[Alert]) -> None:
        """`muted` 的唯一入口：按当下重算一批告警的 `muted`（每次提交快照时调一次）。

        判据 = `mute_session` 或这一条事件还在抑制窗口里。致命级按 `Alert.source` 取那一路
        的跟踪状态；提示级没有抑制窗口，只读 `mute_session`。首发那一条（`raise_in_flight`）
        原样放行——跳过的只是窗口这一半的重算，会话级静音期间照样算 `true`。
        """
        for alert_source, alerts_of_source in self._group_by_source(alerts):
            track = self._tracks.get(alert_source)
            for alert in alerts_of_source:
                if alert.level != "critical" or track is None:
                    alert.muted = bool(self.mute_session)
                    continue
                if track.raise_in_flight is alert:
                    track.raise_in_flight = None  # 这一条正要出去，记号到此为止
                    # 本场静音期间该条照样是 `true`：静音那一半不因这次放行而失效
                    if self.mute_session:
                        alert.muted = True
                    continue
                alert.muted = self._muted(track, self._frame_moment(track))

    @staticmethod
    def _group_by_source(alerts: Iterable[Alert]) -> Iterable[tuple[str, list[Alert]]]:
        """把一批告警按 `source` 分组（致命级与提示级各归自己那一路的跟踪状态）。"""
        grouped: dict[str, list[Alert]] = {}
        for alert in alerts:
            grouped.setdefault(str(alert.source), []).append(alert)
        return list(grouped.items())

    # -- 会话级静音（下一场 `start` 时由 runtime 复位）

    def reset_session_mute(self) -> None:
        """下一场 `start` 时自动复位为「未静音」（不改 `alarm.sound` 持久化值）。"""
        self.mute_session = False

    def reset_for_new_session(self) -> None:
        """新一场开场：清空两条计数与全部提示级（会话级状态不复用上一场）。

        提示级的两份登记（`_hints` 与 `_hint_started_at`）一并清干净——只清一份的话，
        下一场开场之后旧登记还会影响新一场的合并判据（复审 m3）。暂停那格同样复位。
        """
        self.reset_session_mute()
        self.paused = False
        self._tracks.clear()
        self.clear_hints()

    # -- 暂停（暂停区间不计入两条累计）

    def pause_session(self) -> None:
        """本场进入暂停：`on_level_frame()` 从这一刻起只回答「什么都没变」。

        两条累计与抑制窗口的锚点都不动（不写零、不清零）——暂停既不该让静默上涨，
        也不该被当成「有信号」把静默清掉。
        """
        self.paused = True

    def resume_session(self) -> None:
        """本场继续：清掉每条计数的基准，恢复之后的第一帧从零起算。

        不清 `track.last_ts` 不够：恢复后的第一帧会拿它做差，把整个暂停区间当成一帧的
        间隔补进 `silent_seconds`（几分钟的静默当场成立）。
        """
        self.paused = False
        for track in self._tracks.values():
            track.last_ts = None

    # -- 电平帧

    def on_level_frame(self, source: str, ts: float, dbfs: float) -> SourceJudgement:
        """收一帧电平事实，回答「这一路现在该起致命级 / 该恢复 / 都没变」。

        暂停区间什么都不判（`docs/design-intent.md` 第 17 条）：暂停期间电平帧照发
        （`recording_alive` 与界面电平都靠它），但两条累计一格不动。
        """
        if self.paused:
            self._tracks.setdefault(source, _SourceTrack())
            return SourceJudgement(source=source)
        track = self._tracks.setdefault(source, _SourceTrack())
        ts = float(ts)
        previous = track.last_ts
        judgement = SourceJudgement(source=source)

        if float(dbfs) < self.thresholds.silence_dbfs:
            # 低于门限：静默累计增长、恢复累计清零（两条独立的计数）
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
        """起一条致命级（同时只留一条由 runtime 落盘时保证），并推进抑制窗口的锚点。

        次序是要害：先按当下的锚点算出这一条会不会出声，再把锚点写成这一刻——首发那次
        锚点还空着，`muted` 是 `False`；同时挂到 `raise_in_flight` 让紧随的 `commit()` 重算放行。
        """
        moment = float(track.last_ts if track.last_ts is not None else ts)
        muted = self._muted(track, moment)
        track.in_alert = True
        track.event_key = message
        # 这一条事件的抑制窗口从当下起算；到点之后 `_muted()` 当场算出假，不复用历史痕迹。
        track.last_raise_ts = moment
        alert = Alert(
            level="critical",
            source=source,
            since=now_iso(),
            duration_seconds=float(track.silent_seconds),
            message=message,
            muted=muted,
        )
        track.raise_in_flight = alert
        return alert

    def is_muted(self, source: str) -> bool:
        """某一路当前这一条事件是不是「不出声」。"""
        track = self._tracks.get(source)
        if track is None:
            return bool(self.mute_session)
        return self._muted(track, self._frame_moment(track))

    def _frame_moment(self, track: _SourceTrack) -> float:
        """帧时间戳口径的「此刻」：这一路最近一帧的时刻；一帧都没来过时退回服务侧的钟。

        抑制窗口按帧时间戳推进（§3.3.1 的累计口径），判断窗口到点用同一口径。
        """
        if track.last_ts is not None:
            return float(track.last_ts)
        return self._now()

    def _muted(self, track: _SourceTrack, moment: float) -> bool:
        """这一条事件在 `moment` 这一刻会不会出声（会话级静音 ∨ 抑制窗口）。

        只看窗口锚点，不看「有没有刚起的那一条」——首发帧的放行落在
        `refresh_muted()` 的记号上，这里照窗口如实算。
        """
        if self.mute_session:
            return True
        return muted_at(
            last_raise_ts=track.last_raise_ts,
            moment=moment,
            suppress_seconds=float(self.thresholds.alarm_suppress_seconds),
        )

    # -- 流中断事实

    def on_source_lost(self, source: str, ts: float, reason: str = "") -> Alert:
        """后端报「这一路的流中断了」：立刻起致命级（不等静默阈值）。

        与 `_raise_critical()` 同一个次序：先算 `muted`（首发那次锚点还空着 → `False`），
        再把锚点写成这一刻。
        """
        track = self._tracks.setdefault(source, _SourceTrack())
        track.below = False
        track.in_alert = True
        track.last_ts = float(ts)
        message = f"{source} 这一路的采集流中断了"
        if reason:
            message = f"{message}（{reason}）"
        track.event_key = message
        muted = self._muted(track, float(ts))
        track.last_raise_ts = float(ts)
        alert = Alert(
            level="critical",
            source=source,
            since=now_iso(),
            duration_seconds=0.0,
            message=message,
            muted=muted,
        )
        track.raise_in_flight = alert
        return alert

    def mark_recovered(self, source: str) -> None:
        """一路恢复回 `recording`：从「在告警里」摘出，并关掉这一条事件的抑制窗口。

        关窗口是契约点名的「恢复后再发生算新事件」：下一次告警窗口从那一刻重开，
        新的那条该出声。
        """
        track = self._tracks.get(source)
        if track is None:
            return
        track.in_alert = False
        track.silent_seconds = 0.0
        track.recover_seconds = 0.0
        track.silence_started_ts = None
        track.last_raise_ts = None
        track.raise_in_flight = None

    # -- 会中开关某一路（`set_source` 的两处收场点）

    def reset_source(self, source: str) -> None:
        """把这一路的判定记录整个收走（下一条帧当全新一路起算）。

        用在会中开关某一路的两处（`set_source` 关断 / 重开成功）：重开必须当干净开场。
        为什么重开也要收：留着关掉前的旧 `last_ts`，重开第一帧会拿它做差，把整段关掉的
        时间补进 `silent_seconds`——重开第一秒就被旧计数拉回致命级。只收这一路，
        其余路（含提示级登记）不动（与 `reset_for_new_session()` 的整场清空分开）。
        """
        self._tracks.pop(source, None)

    def event_message(self, source: str) -> str | None:
        """这一路当前在告警那条事件的原文（没在告警里就是 `None`）。

        给「致命级换挂」用（`set_source` 关掉横幅那一路、而同场还有别的路真在 `lost` 时）：
        换挂时原话照搬，不另造文案。
        """
        track = self._tracks.get(source)
        if track is None or not track.in_alert:
            return None
        return track.event_key

    # -- 提示级

    def on_transcript_health(self, health: str) -> Alert | None:
        """`transcript.health` 掉档（`behind` / `stopped`）→ 出一条提示级；`normal` → 撤回。

        撤回按这一类清：`normal` 自己的文案（`转写正常`）没有对应的提示条，拿它 `pop()`
        只会扑空，掉档那两条会永远挂在快照上。积压那一条另立一类（`on_backlog_depth()`），
        健康度回 `normal` 不动它——转写跟得上不代表队列已清空。
        """
        if health == "normal":
            dropped = self._drop_hints(HEALTH_HINT_MESSAGES)
            if dropped:
                _log.info("转写健康度回到 normal：撤回 %s 条健康度提示", dropped)
            return None
        return self._merge_hint(
            level="hint",
            source=TRANSCRIPT_SOURCE,
            message=_health_message(health),
        )

    def on_backlog_depth(self, depth: int) -> Alert | None:
        """`backlog_depth() ≥ BACKLOG_HINT_THRESHOLD` → 出一条提示级；回落 → 撤回。

        文案里不写当前段数：`message` 是故障类型标识、也是合并键的一部分，写进变化的段数
        就等于每次都是新键、合并永不发生（段数只进 log 与 `/debug`）。回落要对称地清掉，
        否则提示条会在队列清空后还挂在快照上（`docs/state.md` 第 6 章两条清除路径之一）。
        """
        if int(depth) < BACKLOG_HINT_THRESHOLD:
            dropped = self._drop_hints(BACKLOG_HINT_MESSAGES)
            if dropped:
                _log.info(
                    "待转写段数回落到门槛 %s 以下（%s 段）：撤回积压提示",
                    BACKLOG_HINT_THRESHOLD,
                    int(depth),
                )
            return None
        _log.debug("待转写段数 %s（阈值 %s）", int(depth), BACKLOG_HINT_THRESHOLD)
        return self._merge_hint(
            level="hint",
            source=BACKLOG_SOURCE,
            message=_backlog_message(),
        )

    def _drop_hints(self, messages: Iterable[str]) -> int:
        """撤掉 `TRANSCRIPT_SOURCE` 上这几条文案的提示级，返回真撤掉几条。

        两类提示级共用 `source = transcript`，靠 `message`（三元组第三格）区分；
        按类逐条撤，清健康度那条时不会顺手带走积压那条。
        """
        dropped = 0
        for message in messages:
            key = ("hint", TRANSCRIPT_SOURCE, message)
            if self._hints.pop(key, None) is not None:
                self._hint_started_at.pop(key, None)
                dropped += 1
        return dropped

    def _merge_hint(self, *, level: str, source: str, message: str) -> Alert:
        """按 `(level, source, message)` 三元组合并：沿用原有那条的 `since`，刷新 `duration_seconds`。

        超出上限的键在合并这一步就回收（`_evict_over_the_limit()`，复审 m4）：
        只让 `hints()` 截断返回值的话，被挤掉的那条会永远留在登记里。
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
        # 新起的那条走 `build_hint()`（提示级组装的唯一一处，复审 #4）
        alert = build_hint(
            source=source,
            message=message,
            duration_seconds=0.0,
            muted=self.mute_session,
        )
        self._hints[key] = alert
        self._evict_over_the_limit()
        return alert

    def _evict_over_the_limit(self) -> None:
        """把超出上限 `HINT_LIMIT` 的提示级从登记里删掉（`hints()` 与登记恒同长）。

        丢哪条与 `hints()` 的截断口径逐条一致：按 `since` 排序留最早的三条（`since`
        分辨率是秒，同一秒按进入登记的先后）；记时表同步删，不留无主刻度。
        """
        if len(self._hints) <= HINT_LIMIT:
            return
        ordered = sorted(self._hints.items(), key=lambda entry: entry[1].since)
        for key, item in ordered[HINT_LIMIT:]:
            self._hints.pop(key, None)
            self._hint_started_at.pop(key, None)
            _log.warning("提示级超过上限 %s，丢弃：%s", HINT_LIMIT, item.message)

    def clear_hints(self) -> None:
        """清空提示级与它的两份登记（条目与起点同进同退）；不改致命级与两条计数。"""
        self._hints.clear()
        self._hint_started_at.clear()

    def hints(self) -> list[Alert]:
        """当前提示级列表：`since` 最早的三条（超限的键已在合并时回收，这里截断通常空操作）。"""
        ordered = sorted(self._hints.values(), key=lambda item: item.since)
        kept = ordered[:HINT_LIMIT]
        for item in ordered[HINT_LIMIT:]:  # pragma: no cover - 超限的键在合并时已回收
            _log.warning("提示级超过上限 %s，丢弃：%s", HINT_LIMIT, item.message)
        return kept


def _health_message(health: str) -> str:
    if health == "behind":
        return "转写落后了"
    if health == "stopped":
        return "转写停了"
    return "转写正常"


#: 健康度这一类提示级的全部文案（`normal` 那条只在这张表里：它没有对应的提示条，
#: 撤回时撤的是掉档那两条）。清哪一类由它说了算。
HEALTH_HINT_MESSAGES: tuple[str, ...] = (
    _health_message("behind"),
    _health_message("stopped"),
)


def _backlog_message() -> str:
    """积压那一条提示的文案（故障类型标识，段数不写进来）。"""
    return f"待转写段数已达 {BACKLOG_HINT_THRESHOLD} 段"


#: 积压这一类提示级的全部文案（单列出来是为了与健康度那一类对称地清）。
BACKLOG_HINT_MESSAGES: tuple[str, ...] = (_backlog_message(),)


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
    "BACKLOG_HINT_MESSAGES",
    "BACKLOG_HINT_THRESHOLD",
    "BACKLOG_SOURCE",
    "HEALTH_HINT_MESSAGES",
    "HINT_LIMIT",
    "TRANSCRIPT_SOURCE",
    "AlertJudge",
    "AlertThresholds",
    "SourceJudgement",
    "build_hint",
    "muted_at",
]
