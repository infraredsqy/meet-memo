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
  - **暂停区间不计入这两条计数**（`pause_session()` / `resume_session()`）：暂停期间电平帧
    照发（那是 `recording_alive` 与界面电平的输入），但判定器一格不动 —— 安静房间里暂停
    三分钟不该弹一条「这一路静默已达 180 秒」；恢复后的第一帧从零起算（`docs/design-intent.md`
    第 17 条：告警要能一眼看见，但不许一惊一乍）；
  - 提示级（`transcript.health` 掉档、`backlog_depth() ≥ BACKLOG_HINT_THRESHOLD`）
    进 `alerts.hints`，**同类合并的键是 `(level, source, message)` 三元组**、**上限 3**；
    合并时沿用原有那一条的 `since`、只刷新 `duration_seconds`；超限的键在合并那一刻就从
    判定器的记账里回收（`hints()` 与记账本恒同长 —— 只截断返回值的写法会让被挤掉的键永远留下）；
  - 致命级进 `alerts.critical`，**同时只留一条**；
  - `muted` 由服务侧算出：`mute_session` 生效期间 **或** 这一路的上一次 raise 还在
    `alert.alarm_suppress_seconds` 的抑制窗口里 → `true`。抑制窗口按**「哪一路 + 故障类型」**
    跟踪（帧时间戳口径推进），界面侧不做任何窗口计算；
  - **首发那一帧给 `false`**（界面据此响一声）：锚点取的是**上一次 raise 的时刻**，而首发
    那次算 `muted` 的时候这一路还没 raise 过，锚点要等算完才写进去 —— 于是这条告警不会被
    自己压住。此后同一条告警重算是 `true`（不重复出声）；`mark_recovered()` 关掉窗口，
    恢复后再发生按新事件处理，首发又是 `false`；
  - `muted` 的**唯一入口是 `AlertJudge.refresh_muted()`**，服务侧每次提交快照时调它按当下重算
    一遍：会话级静音复位、抑制窗口到点、下一场 `start` 复位之后，同一条告警算出来就回到
    `false`（这是一条「此刻会不会响」的当下判定，随窗口来去）。**重算只发生在提交那一刻**：
    窗口到点本身不发广播，所以「窗口已过、下一次提交还没来」那段里快照上的 `muted` 仍是
    上一次算出来的 `true`（方向是不重复出声，口径见 `docs/state.md` 第 5 章）；
  - 提示级合并也只有一份实现（`AlertJudge._merge_hint()`，由 `on_transcript_health()` /
    `on_backlog_depth()` 进入）：同一个三元组再次出现时沿用原有那条的 `since`，并把
    `duration_seconds` 刷新到当下；
  - 提示级有两条**对称的清除路径**（只进不出的写法会让提示条永远挂在快照上）：
    `transcript.health` 回到 `normal` 时撤掉健康度那两条文案（`behind` / `stopped`），
    积压回落到 `BACKLOG_HINT_THRESHOLD` 以下时撤掉积压那一条；两类共用
    `source = transcript`、靠 `message` 分辨，各清各的、互不牵连；
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
    *, last_raise_ts: float | None, moment: float, suppress_seconds: float
) -> bool:
    """抑制窗口这一半的纯判定：`moment` 那一刻**还在不在**上一次 raise 开的窗口里。

    窗口 = `[last_raise_ts, last_raise_ts + suppress_seconds)`；窗口起点为空
    （这一路还没 raise 过，或这一条已经恢复）→ `False`。会话级静音那一半由
    `AlertJudge._muted()` 在前面并进来。抽成纯函数是为了让「窗口到点就回到会出声」
    这件事可以直接断言。

    **锚点取上一次 raise 的时刻**：首发那一刻这一路还没有 raise 过，锚点是空的，于是算出来
    是 `False`（界面据此响一声）；锚点要等这一次算完才写进去，起事件的那一帧因此不会被
    自己压住（见 `_raise_critical()` / `on_source_lost()`）。
    """
    if last_raise_ts is None:
        return False
    return float(moment) < float(last_raise_ts) + float(suppress_seconds)


@dataclass
class _SourceTrack:
    """一路的两条独立连续计数（静默 / 恢复）与**上一次 raise 的时刻**。"""

    silent_seconds: float = 0.0
    recover_seconds: float = 0.0
    last_ts: float | None = None
    silence_started_ts: float | None = None
    below: bool = False
    in_alert: bool = False
    event_key: str | None = None
    #: **上一次 raise 的时刻**（帧时间戳口径）：抑制窗口 = `[它, 它 + alarm_suppress_seconds)`。
    #: `_raise_critical()` / `on_source_lost()` 都是**先算 `muted`、后写这个字段**，于是首发
    #: 那一次算的时候锚点还空着、算出来是 `False`（该响一声），此后同一条告警重算才落在窗口里
    #: （`True`）。只记这一个当下值、不累计历史；窗口到点后 `_muted()` 当场算出为假。
    #: （首发帧扛住紧随其后的那次重算，另靠下面的 `raise_in_flight`，两件事各管一段。）
    last_raise_ts: float | None = None
    #: **刚起、还没被广播出去的那一条致命级**（首发帧）。
    #:
    #: `commit()` 在写下锚点之后会重算一遍 `muted`，而那次重算就发生在锚点刚开的这一刻 ——
    #: 于是同一条告警会被自己刚开的窗口压住，广播出去的第一帧成了 `muted: true`（界面一声
    #: 都不响）。窗口锚点这一个量表达不了「这一帧就是首发」，所以另记一个对象引用：
    #: `refresh_muted()` 认出「正是这一条还没出去」，原样放行；它一提交完就清空，
    #: 此后同一条告警的重算落进窗口里（`true`，不重复出声）。
    #:
    #: **隐含前提：每次 raise 之后必有一次提交**。这个记号是在**提交那一步**
    #: （`refresh_muted()` 认领并清空）才算走完的，`_raise_critical()` 与
    #: `on_source_lost()` 都只是把它挂上去。同一路连着 raise 两次、中间没有提交时，
    #: 第二次会把第一次那个引用**覆盖掉** —— 第一次那条永远等不到认领，它广播出去时
    #: 已经被自己刚开的窗口压成 `true`，于是回到「首发也不响」的旧行为（静默退回，不报错、
    #: 不抛异常）。恢复路径 `mark_recovered()` 也会清掉它，那一支是有意的：这一路已经接回来，
    #: 在飞的那条不该再管。
    #:
    #: **当前到不了这条路径**：raise 与紧随其后的 `commit()` 之间没有 `await`、也没有别的
    #: 让出点（`alert` 态那两条迁移各自在同一个同步段里走完），所以两次 raise 中间一定夹着
    #: 一次提交。留这一段是把前提写下来：将来谁在中间插进一个 `await`（或者把提交挪到队列
    #: 后面），这一格就会从「必然走完」变成「看调度」。
    raise_in_flight: Alert | None = None


@dataclass
class AlertJudge:
    """逐帧累计 + 分级判定 + `muted` 计算（**不持有快照**）。"""

    thresholds: AlertThresholds = field(default_factory=AlertThresholds)
    mute_session: bool = False
    #: **本场暂停中**：暂停区间不计入静默 / 恢复这两条累计（`docs/design-intent.md` 第 17 条
    #: 「告警不许一惊一乍」——安静房间里暂停三分钟不该弹一条「这一路静默已达 180 秒」）。
    #: 置位期间 `on_level_frame()` 只回答「什么都没变」，两条计数一格不动；`resume_session()`
    #: 把每条计数的基准（`track.last_ts`）清掉，于是恢复之后的第一帧从零起算，
    #: 暂停那一整段不会以「一帧跨越了几分钟」的形式补记进去。
    paused: bool = False
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

        **首发那一条原样放行**：刚 raise 出来、还没广播过的这一条（`track.raise_in_flight`）
        跳过重算，把它算好的 `muted: false` 留到这一帧广播出去，随后立刻清掉这个记号 ——
        下一次提交再算就是「窗口里的同一条告警」，`true`，不重复出声。少了这一步，
        首发帧会被自己刚开的窗口压住（`commit()` 的重算就发生在锚点写下的同一刻）。

        **放行不等于绕过会话级静音**：本场静音生效期间一律 `true`（`mute_session` 优先），
        因此那条「刚起、还没广播」的条目在静音期间照样算成 `true` —— 跳过的是窗口这一半的
        重算，会话级静音那一半照算（它只看开关，不看窗口锚点）。
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

    # -- 会话级静音（下一场 `start` 时由 runtime 复位） --------------------

    def reset_session_mute(self) -> None:
        """下一场 `start` 时自动复位为「未静音」（**不改 `alarm.sound` 持久化值**）。"""
        self.mute_session = False

    def reset_for_new_session(self) -> None:
        """新一场开场：清空两条计数与全部提示级（会话级状态不复用上一场）。

        **提示级的记账一并清干净**（`_hints` 与 `_hint_started_at` 两份都在
        `clear_hints()` 里清）：只清其中一份的话，`hints()` 与记账本会有一刻对不上，
        下一场开场之后旧记账还会影响新一场的合并判据（复审 m3）。

        暂停那一格同样复位（上一场停在暂停里结束，不该把「暂停中」带进新一场）。
        """
        self.reset_session_mute()
        self.paused = False
        self._tracks.clear()
        self.clear_hints()

    # -- 暂停（暂停区间不计入两条累计） ------------------------------------

    def pause_session(self) -> None:
        """本场进入暂停：`on_level_frame()` 从这一刻起只回答「什么都没变」。

        **两条累计与抑制窗口的锚点都不动**（不写零、不清零）：暂停既不该让静默往上涨，
        也不该被当成「有信号」把静默清掉；它只是「这段时间不算数」。
        """
        self.paused = True

    def resume_session(self) -> None:
        """本场继续：把每条计数的基准清掉，恢复之后的第一帧从零起算。

        **不清 `track.last_ts` 是不够的**：那一格是「上一帧的时刻」，恢复后的第一帧会拿它
        做差，把整个暂停区间当成一帧的间隔补进 `silent_seconds`（几分钟的静默当场成立）。
        清掉之后第一帧的 `delta` 是 0（帧时间戳口径的「从现在起算」）。
        """
        self.paused = False
        for track in self._tracks.values():
            track.last_ts = None

    # -- 电平帧 -----------------------------------------------------------

    def on_level_frame(self, source: str, ts: float, dbfs: float) -> SourceJudgement:
        """收一帧电平事实，回答「这一路现在该起致命级 / 该恢复 / 都没变」。

        **暂停区间什么都不判**（`docs/design-intent.md` 第 17 条）：暂停期间电平帧照发
        （`recording_alive` 与界面上的电平都靠它，停了会把「暂停」误报成「采集已停」），
        但这一路的两条累计一格不动 —— 于是安静房间里暂停多久都不会冒出一条致命级。
        """
        if self.paused:
            self._tracks.setdefault(source, _SourceTrack())
            return SourceJudgement(source=source)
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
        """起一条致命级（**同时只留一条**由 runtime 落盘时保证），并推进抑制窗口的锚点。

        **次序是这一条的要害**：先按当下的锚点算出这一条会不会出声，再把锚点写成这一刻。
        首发那次算的时候锚点还空着，于是 `muted` 是 `False`；这一条同时挂到
        `raise_in_flight` 上，让紧随其后的 `commit()` 那次重算（它就发生在锚点刚开的同一刻）
        原样放行 —— 广播出去的第一帧因此真的是 `muted: false`，界面据此响一声。
        """
        moment = float(track.last_ts if track.last_ts is not None else ts)
        muted = self._muted(track, moment)
        track.in_alert = True
        track.event_key = message
        # 这一条事件的抑制窗口从**当下**起算；下一次再 raise 时 `_muted()` 会按它算出
        # 「还在窗口里」。窗口是「有期状态」：到点之后 `_muted()` 当场算出假，不复用历史痕迹。
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
        """帧时间戳口径的「此刻」：这一路最近一帧的时刻。

        抑制窗口按帧时间戳推进（§3.3.1 的累计口径），判断窗口到点时因此也用同一个口径；
        这一路一帧都还没来过时退回服务侧看表的那一刻。
        """
        if track.last_ts is not None:
            return float(track.last_ts)
        return self._now()

    def _muted(self, track: _SourceTrack, moment: float) -> bool:
        """这一条事件**在 `moment` 这一刻**会不会出声（**会话级静音 ∨ 抑制窗口**）。

        两个输入都取当下的值：会话级静音开关 + 这一路上一次 raise 的时刻。于是它每次算出来
        的都是「此刻会不会响」这一个答案。窗口这一半只看锚点，不看「有没有刚起的那一条」——
        首发帧的放行落在 `refresh_muted()` 那一个记号上，这里照窗口如实算。
        """
        if self.mute_session:
            return True
        return muted_at(
            last_raise_ts=track.last_raise_ts,
            moment=moment,
            suppress_seconds=float(self.thresholds.alarm_suppress_seconds),
        )

    # -- 流中断事实 -------------------------------------------------------

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
        track.last_raise_ts = None
        track.raise_in_flight = None

    # -- 提示级 -----------------------------------------------------------

    def on_transcript_health(self, health: str) -> Alert | None:
        """`transcript.health` 掉档（`behind` / `stopped`）→ 出一条提示级；`normal` → 撤回。

        **撤回按这一类清**：存进去的键是「转写落后了」/「转写停了」这两条文案，而 `normal`
        本身也有一条自己的文案（`转写正常`）—— 拿它去 `pop()` 只会扑空，那两条提示于是
        永远挂在快照上。这里改成把这一类（`HEALTH_HINT_MESSAGES` 里那两条）逐个撤掉。

        积压那一条另立一类（`on_backlog_depth()` 负责），健康度回到 `normal` 不动它：
        两件事各归各的门槛，转写跟得上并不代表队列已经清空。
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

        **文案里不写当前段数**：`message` 是这条提示的故障类型标识，也是同类合并键的一部分
        （§3.3.2 的三元组），把每次变化的段数写进去就等于每次都是新键、合并永远不会发生。
        段数本身只进 log 与 `/debug`。

        低于门槛这一档要**对称地清掉**那条提示：只进不出的写法会让「待转写段数已达 20 段」
        在队列清空之后还留在快照上（`docs/state.md` 第 6 章的两条清除路径之一）。
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

        两类提示级共用 `source = transcript`，靠 `message` 区分（三元组的第三格就是它）。
        按「这一类的那几条文案」逐个撤，于是清健康度那一条时不会顺手把积压那一条也带走。
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

        **本 Phase 里提示级合并的唯一实现**：同一个键再次出现时不新增条目，`since` 保持首次
        发生时刻，`duration_seconds` 刷新到当下（= 时刻差，从这条提示第一次出现算起）。

        **超出上限的键在合并这一步就回收**（`_evict_over_the_limit()`）：上限是 3，记账里
        因此最多同时留 3 条。只让 `hints()` 截断返回值的话，被挤掉的那条会永远留在
        `_hints` 里（复审 m4：`hints()` 返回 3 条而记账 4 条，转写回 `normal` 之后仍是 4 条）。
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
        # 新起的那条走 `build_hint()`（提示级组装的唯一一处；六字段与上面逐项一致：
        # level="hint" / source / since=now_iso() / duration_seconds=0.0 / message /
        # muted —— 复审 #4 的裁定：不再绕开它另写一遍 `Alert(...)` 字面量）
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
        """把超出上限 `HINT_LIMIT` 的那些提示级**从记账里删掉**（`hints()` 与记账本同长）。

        丢哪一条与 `hints()` 的截断口径**逐条一致**：按 `since` 排序（`since` 的分辨率是秒，
        同一秒里按进入记账本的先后）留下最早的三条，其余的在记账里删掉 —— 于是
        `hints()` 恒等于 `_hints.values()` 本身，两边不会再出现「返回 3 条、记账 4 条」。
        记时表（`_hint_started_at`）同步删，合并那条判据因此不会留下无主的刻度。
        """
        if len(self._hints) <= HINT_LIMIT:
            return
        ordered = sorted(self._hints.items(), key=lambda entry: entry[1].since)
        for key, item in ordered[HINT_LIMIT:]:
            self._hints.pop(key, None)
            self._hint_started_at.pop(key, None)
            _log.warning("提示级超过上限 %s，丢弃：%s", HINT_LIMIT, item.message)

    def clear_hints(self) -> None:
        """清空提示级**与它的两份记账**（新一场开场 / 离开录音与告警态时用）。

        `_hints`（条目）与 `_hint_started_at`（`duration_seconds` 的起点）必须一起清：
        快照与记账、条目与起点，两对都得同进同退。**不改**致命级与两条计数。
        """
        self._hints.clear()
        self._hint_started_at.clear()

    def hints(self) -> list[Alert]:
        """当前提示级列表：`since` 最早的三条（超出上限的键已在合并时回收）。

        `_merge_hint()` 那边一超限就把多出来的键从记账里删掉，所以这里的截断通常是空操作；
        留着它是为了让「上限 3」这条口径在返回处也成立（判定可算，无空档）。
        """
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


#: 健康度这一类提示级的**全部**文案（`normal` 那条是「转写正常」，它只出现在这一张表里：
#: 它没有对应的提示条，撤回时要撤的是上面那两条掉档文案）。清哪一类由它说了算。
HEALTH_HINT_MESSAGES: tuple[str, ...] = (
    _health_message("behind"),
    _health_message("stopped"),
)


def _backlog_message() -> str:
    """积压那一条提示的文案（故障类型标识，段数不写进来）。"""
    return f"待转写段数已达 {BACKLOG_HINT_THRESHOLD} 段"


#: 积压这一类提示级的全部文案（眼下只有一条，单列出来是为了与健康度那一类对称地清）。
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
