"""纯状态机与状态字段（phase2 §3.1 / §3.5.6）。

四态与迁移函数、`Snapshot` 与嵌套 dataclass、`to_dict()`（全应用唯一的状态 JSON 形态）、
15 柱 × 2 秒包络的维护算法、收尾三函数都在这里；本模块无 IO（不开文件、不写 log、不广播）。
三条边界（phase2 冻结）：顶层只有 `TOP_STATE` 四态，不新增第五个；迁移只能经本模块的
迁移函数，出发态不匹配抛 `InvalidTransitionError`（被拒的迁移不改 `revision`、不写盘、
不广播）；收尾三函数只改内存快照——落盘归 `ServiceRuntime`，`revision +1` 与广播归
`ServiceRuntime.commit()`，终态判定只在 `finish_drain()` 一处。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from meeting.backends.base import SOURCE_IDS
from meeting.backends.base import SOURCE_STATUS as PROBE_STATUS_BASE
from meeting.backends.base import DEFAULT_FRAME_INTERVAL_SECONDS  # noqa: F401 - 转出口，见 __all__

# 取值域常量（连取值一起冻结：消费方只做成员关系判断，不重抄字面量）

#: 顶层四态（不新增第五个）
TOP_STATE: tuple[str, ...] = ("idle", "recording", "alert", "ended")

#: `session.json` 的 `state` 四取值（`failed` 不是 `ended.phase` 的取值）
SESSION_STATE: tuple[str, ...] = ("recording", "draining", "done", "failed")

#: 告警分级
ALERT_LEVEL: tuple[str, ...] = ("critical", "hint")

#: `ended.phase` 两取值
ENDED_PHASE: tuple[str, ...] = ("draining", "done")

#: 收尾三步的顺序（转码已摘出收尾序列：flush 落定即自动在后台起，不占收尾预算、
#: 不挡 start 与会后动作——见 `meeting/capture/transcode.py`）
DRAIN_STEP_IDS: tuple[str, ...] = ("flush", "log", "post")

#: 收尾里"留给用户按按钮"的格子（`post`）：它停在 `waiting` 也算收尾走完——
#: 按「结束录音」之后会后一个动作都不自动跑（R26），这一格本来就该空着等人。
POST_DRAIN_STEP_ID = "post"

#: `DrainStep.status` 的五档取值域（与 `DRAIN_STEP_IDS` 同形）
DRAIN_STEP_STATUS: tuple[str, ...] = ("waiting", "running", "done", "failed", "skipped")

#: `probe()` 的全部合法返回（与 `backends/base.py` 的 `SOURCE_STATUS` 同源）
PROBE_STATUS: tuple[str, ...] = tuple(PROBE_STATUS_BASE)

#: 快照 `sources[*].health` 的全部合法取值（七档）
SOURCE_HEALTH: tuple[str, ...] = (
    "unprobed",
    "ready",
    "probe_failed",
    "capturing",
    "lost",
    "restored",
    "stopped",
)

# （`notice` 槽位已随「默认输出根」退场，见 `docs/design-intent.md` 第 4 条；
#   "最近一场"由工作台收编节点（e2f6166b）落在工作区注册表里，不再扫场次目录。）

#: 包络：一根柱的窗口（秒）与柱数（最近 30 秒 = 15 × 2 秒）
ENVELOPE_BAR_SECONDS = 2.0
ENVELOPE_BAR_COUNT = 15

#: `revision` 的固定起点：只为本节点选的起点，不跨进程延续、不落盘；测试只断言
#: 「存在、是 int、单调 +1、同一次事实处理合并递增」，不许把取值钉成常数。
REVISION_START = 1

#: 帧时钟窗口的两个配套常量（`recording_alive` 的判据，phase2 §3.5.4）
HEARTBEAT_MIN_WINDOW_SECONDS = 2.0
HEARTBEAT_WINDOW_FACTOR = 3.0

#: 两路的人话标签
SOURCE_LABELS: dict[str, str] = {"mic": "本地", "loopback": "远端"}


def frame_clock_window(frame_interval: float) -> float:
    """`recording_alive` 的判据窗口（秒）：`max(2.0s, 3 × frame_interval)`。

    协议默认 `0.5s` 与 dev 替身的 `0.1s` 都算成 2.0 秒；界面的 10 秒心跳周期
    绝不参与这个计算（phase2 §3.5.4 / §6）。
    """
    return max(HEARTBEAT_MIN_WINDOW_SECONDS, HEARTBEAT_WINDOW_FACTOR * float(frame_interval))


# 错误


class InvalidTransitionError(RuntimeError):
    """非法迁移：出发态不匹配 / 未知状态取值（一律按错误处理，不许兜底成 `idle`）。"""

    def __init__(self, message: str, *, top_state: str | None = None) -> None:
        self.top_state = top_state
        super().__init__(message)


class DrainStateError(RuntimeError):
    """收尾三函数的非法调用（未知步骤 / 取值域外的 `status` / 不在收尾中）。"""


# 嵌套 dataclass（字段名就是双端契约的输入）


@dataclass
class ServiceInfo:
    """快照 `service` 五子键（启动时定一次，之后不变）。没有 `out_root`：落点由工作区给。"""

    started_at: str
    version: str
    port: int
    url: str
    log_path: str


@dataclass
class SessionInfo:
    """快照 `session`（`id` 是场次目录名，`dir` 是场次目录路径）。

    `paused` 是这一场此刻是否暂停中（第 8 条的读法）；`paused_seconds` 与
    `paused_at_monotonic` 是算时长用的内部计数（暂停的几段不进 `elapsed_seconds`，
    第 7 条：权威长度是实际录到的音频时长），它们不上快照。
    """

    id: str
    dir: str
    started_at: str
    mode: str
    started_monotonic: float = 0.0
    elapsed_seconds: float = 0.0
    frozen: bool = False
    #: 本场是否正处在暂停中（动作 `pause` / `resume` 翻它；顶层四态一个字不动）
    paused: bool = False
    #: 已经关掉的那几段暂停加起来多少秒（`resume` 时结清；只进内存，不落 `session.json`）
    paused_seconds: float = 0.0
    #: 当前这一段暂停的起点（`time.monotonic()`）；不在暂停中时是 `None`
    paused_at_monotonic: float | None = None


@dataclass
class EnvelopeWindow:
    """包络的一根柱正在累计的那 2 秒窗口。"""

    start: float = 0.0
    peak: float | None = None


@dataclass
class SourceState:
    """快照 `sources[*]`（`health` 七取值，见 `SOURCE_HEALTH`）。"""

    id: str
    label: str
    device_name: str | None = None
    health: str = "unprobed"
    lost_since: str | None = None
    restored: bool = False
    level_dbfs: float | None = None
    peak_dbfs: float | None = None
    envelope: list[float] = field(default_factory=list)
    window: EnvelopeWindow = field(default_factory=EnvelopeWindow)
    bars: list[float | None] = field(default_factory=list)
    #: 最左边几根柱还没有值时沿用的电平（会话开场取第一帧的峰值）
    bar_default: float = -100.0

    def encode(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "device_name": self.device_name,
            "health": self.health,
            "lost_since": self.lost_since,
            "restored": self.restored,
            "level_dbfs": self.level_dbfs,
            "peak_dbfs": self.peak_dbfs,
            "envelope": list(self.envelope),
        }

    def push_level(self, ts: float, dbfs: float, peak: float) -> None:
        """收一帧 `LevelFrame`：更新瞬时值 / 峰值保持，并维护 15 柱 × 2 秒的包络。

        一根柱的值 = 它那 2 秒窗口内所有 `LevelFrame.peak` 的最大值（`bar` 与
        `envelope[-1]` 用同一个数）。15 根柱是最近 30 秒的滚动窗口，时钟以第一帧的
        时间戳为基准向后切；窗口一跨就把最旧那根挤出去、右边补空，没有帧的柱编码时
        沿用左边那根的值——数组长度任何时刻都恰好 15。
        """
        self.level_dbfs = float(dbfs)
        current = self.peak_dbfs
        self.peak_dbfs = float(peak) if current is None else max(float(current), float(peak))

        if not self.bars:
            # 会话开场：窗口基准取本帧时间戳，15 根柱此刻全部为空、只有当下这一根有值
            self.window = EnvelopeWindow(start=float(ts), peak=float(peak))
            self.bars = [None] * (ENVELOPE_BAR_COUNT - 1) + [float(peak)]
            self.bar_default = float(peak)
            self.envelope = _encode_envelope(self.bars, self.bar_default)
            return

        offset = float(ts) - self.window.start
        if offset < 0:
            # 时间戳回退（后端换时钟）：算进当下这一根，已经落定的柱一根都不动
            index = ENVELOPE_BAR_COUNT - 1
        else:
            index = min(int(offset // ENVELOPE_BAR_SECONDS), ENVELOPE_BAR_COUNT - 1)
        if index == 0:
            # 同一根柱的窗口里又来了帧：最右那根永远是当下这个 2 秒窗口
            index = ENVELOPE_BAR_COUNT - 1
        else:
            # 时钟走到第 `index` 个窗口（相对基准）：左边让位、右边补空
            self.bars = self.bars[index:] + [None] * index
            self.window.start += index * ENVELOPE_BAR_SECONDS
            index = ENVELOPE_BAR_COUNT - 1
        value = self.bars[index]
        self.bars[index] = float(peak) if value is None else max(float(value), float(peak))
        self.envelope = _encode_envelope(self.bars, self.bar_default)


def _encode_envelope(bars: list[float | None], default: float = -100.0) -> list[float]:
    """把 15 根柱编码成对外数组：没有值就沿用左边那根；最左边都没有值时给 `default`。"""
    out: list[float] = []
    previous = float(default)
    for value in bars:
        if value is not None:
            previous = float(value)
        out.append(previous)
    return out


@dataclass
class Alert:
    """一条告警（字段集六项，不因合并而新增或改名）。"""

    level: str
    source: str
    since: str
    duration_seconds: float
    message: str
    muted: bool = False

    def encode(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "source": self.source,
            "since": self.since,
            "duration_seconds": self.duration_seconds,
            "message": self.message,
            "muted": self.muted,
        }


@dataclass
class AlertState:
    """快照 `alerts`：致命级同时只留一条；提示级同类合并、上限 3。"""

    critical: Alert | None = None
    hints: list[Alert] = field(default_factory=list)

    def refresh_muted(self, *, judge: Any) -> None:
        """按当下重算这批告警的 `muted`（判定权在 `AlertJudge` 一处，这里只转交）。

        调用点只有 `ServiceRuntime.commit()`：每次提交快照前重算，快照里 `muted`
        说的永远是「此刻会不会出声」。
        """
        if judge is None:
            return
        if self.critical is not None:
            judge.refresh_muted([self.critical])
        if self.hints:
            judge.refresh_muted(self.hints)

    def encode(self) -> dict[str, Any]:
        return {
            "critical": None if self.critical is None else self.critical.encode(),
            "hints": [item.encode() for item in self.hints],
        }


@dataclass
class TranscriptState:
    """快照 `transcript` 的四格（`9e5db89c` phase2 §7 起由转写后端供数）。

    - `health`：`normal` / `behind` / `stopped`，读 `TranscriberBackend.health()`；
      真实现没起过场次时报 `stopped`，初值 `normal` 会被 `startup()` 就地覆盖（缺省替身恒报 `normal`）。
    - `last_line_at`：服务侧收到最近一行转写的墙钟（ISO）；`buffered_lines`：后端报的积压段数；
      `failed_segments`：累计识别失败段数（正文以 `[[识别失败` 开头的那一行加一）。

    四格里只有 `health` 的迁移参与状态变更（`revision` 由它投票）；两个计数写进快照、
    不涨 `revision`。刷新与提交的判定权收在 `meeting/service/runtime.py` 的
    `_refresh_transcript_snapshot()` 一处。
    """

    health: str = "normal"
    last_line_at: str | None = None
    buffered_lines: int = 0
    failed_segments: int = 0

    def encode(self) -> dict[str, Any]:
        return {
            "health": self.health,
            "last_line_at": self.last_line_at,
            "buffered_lines": int(self.buffered_lines),
            "failed_segments": int(self.failed_segments),
        }


@dataclass
class DrainStep:
    """收尾三步里的一步（四键齐，含 `error`）。"""

    id: str
    status: str = "waiting"
    detail: str = ""
    error: str | None = None

    def encode(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "detail": self.detail,
            "error": self.error,
        }


@dataclass
class EndedState:
    """快照 `ended`（`phase` 只有 `draining` / `done` 两取值）。"""

    phase: str = "draining"
    steps: list[DrainStep] = field(default_factory=list)
    error: str | None = None

    def encode(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "steps": [step.encode() for step in self.steps],
            "error": self.error,
        }


@dataclass
class SettingsView:
    """快照 `settings`（顶层七键；`asr` 五子键；`devices` 两子键 `mic` / `loopback`）。

    `summary_default_window`（`311f84f5`）是快速总结的缺省档（四档取值）——
    服务是唯一来源，前端不自造第二份。`summary_preset_names` / `summary_active_preset`
    只给名字清单与当前指针：预设里的 `base_url` / `api_key` / `model` 是配置，
    密钥不进快照、不在界面回显；指针指向的名字不在清单里时报空串（那一档实际生效的
    是老四键平铺，快照说的就是真会用哪套）。
    """

    alarm_sound: bool = True
    heartbeat_interval_seconds: int = 10
    summary_default_window: str = "15m"
    summary_preset_names: list[str] = field(default_factory=list)
    summary_active_preset: str = ""
    asr: dict[str, Any] = field(default_factory=dict)
    devices: dict[str, Any] = field(default_factory=dict)

    def encode(self) -> dict[str, Any]:
        return {
            "alarm_sound": bool(self.alarm_sound),
            "heartbeat_interval_seconds": int(self.heartbeat_interval_seconds),
            "summary_default_window": str(self.summary_default_window),
            "summary_preset_names": list(self.summary_preset_names),
            "summary_active_preset": str(self.summary_active_preset),
            "asr": dict(self.asr),
            "devices": dict(self.devices),
        }


@dataclass
class Snapshot:
    """内存里的那一份状态（唯一状态权威由 `ServiceRuntime` 持有它）。"""

    revision: int = REVISION_START
    top_state: str = "idle"
    session: SessionInfo | None = None
    sources: list[SourceState] = field(default_factory=list)
    alerts: AlertState = field(default_factory=AlertState)
    transcript: TranscriptState = field(default_factory=TranscriptState)
    ended: EndedState | None = None
    settings: SettingsView = field(default_factory=SettingsView)

    def source(self, source_id: str) -> SourceState:
        for item in self.sources:
            if item.id == source_id:
                return item
        raise InvalidTransitionError(f"未知的采集路：{source_id!r}", top_state=self.top_state)

    def drain_step(self, step_id: str) -> DrainStep:
        if step_id not in DRAIN_STEP_IDS:
            raise DrainStateError(f"未知的收尾步骤：{step_id!r}（取值域 {DRAIN_STEP_IDS}）")
        if self.ended is None:
            raise DrainStateError("当前没有收尾中的会话（ended 为 None）")
        for step in self.ended.steps:
            if step.id == step_id:
                return step
        raise DrainStateError(f"收尾步骤表里没有 {step_id!r}")

    def _sources_json(self) -> list[dict[str, Any]]:
        by_id = {item.id: item for item in self.sources}
        out: list[dict[str, Any]] = []
        for source_id in SOURCE_IDS:
            item = by_id.get(source_id)
            if item is None:
                out.append(
                    SourceState(id=source_id, label=SOURCE_LABELS[source_id]).encode()
                )
            else:
                out.append(item.encode())
        return out

    def to_dict(self, service: ServiceInfo) -> dict[str, Any]:
        """产出状态 JSON 的唯一形态（`GET /api/state` 与 SSE 的 `snapshot` 帧共用）。"""
        return {
            "revision": int(self.revision),
            "service": {
                "started_at": service.started_at,
                "version": service.version,
                "port": int(service.port),
                "url": service.url,
                "log_path": service.log_path,
            },
            "top_state": self.top_state,
            "session": None if self.session is None else {
                "id": self.session.id,
                "dir": self.session.dir,
                "started_at": self.session.started_at,
                "mode": self.session.mode,
                "elapsed_seconds": round(float(self.session.elapsed_seconds), 3),
                "frozen": bool(self.session.frozen),
                "paused": bool(self.session.paused),
            },
            "sources": self._sources_json(),
            "alerts": self.alerts.encode(),
            "transcript": self.transcript.encode(),
            "ended": None if self.ended is None else self.ended.encode(),
            "settings": self.settings.encode(),
        }


def new_snapshot(*, settings: SettingsView | None = None) -> Snapshot:
    """空形态快照：四态 `idle`、`session` 为 `None`、两路 `unprobed`、包络为空数组。"""
    return Snapshot(
        revision=REVISION_START,
        top_state="idle",
        session=None,
        sources=[
            SourceState(id=source_id, label=SOURCE_LABELS[source_id]) for source_id in SOURCE_IDS
        ],
        alerts=AlertState(),
        transcript=TranscriptState(),
        ended=None,
        settings=settings if settings is not None else SettingsView(),
    )


# 迁移函数（非法迁移抛错，不许静默 no-op、不许兜底）


def start_recording(
    snapshot: Snapshot,
    *,
    session: SessionInfo,
    mode: str,
    devices: dict[str, str | None] | None = None,
) -> SessionInfo:
    """`idle → recording` 与 `ended → recording`（下一场）两条迁移。

    副作用（一次性做完，调用方随后调一次 `commit()`）：`ended` 置 `None`、
    `session` 换成新一场、两路 `health → capturing`、告警与峰值保持清空。
    """
    if snapshot.top_state not in ("idle", "ended"):
        raise InvalidTransitionError(
            f"start 只能从 idle / ended 出发，当前是 {snapshot.top_state!r}",
            top_state=snapshot.top_state,
        )
    if mode not in ("both", "mic", "loopback"):
        raise InvalidTransitionError(f"未知的 mode：{mode!r}", top_state=snapshot.top_state)

    devices = devices or {}
    snapshot.session = session
    snapshot.ended = None
    snapshot.alerts = AlertState()
    snapshot.top_state = "recording"
    for item in snapshot.sources:
        item.health = "capturing"
        item.device_name = devices.get(item.id)
        item.lost_since = None
        item.restored = False
        item.level_dbfs = None
        item.peak_dbfs = None
        item.envelope = []
        item.bars = []
        item.window = EnvelopeWindow()
        item.bar_default = -100.0
    return session


def begin_alert(
    snapshot: Snapshot,
    *,
    source: str,
    alert: Alert,
    lost_since: str,
) -> bool:
    """`recording → alert`：该路 `health → lost`、记 `lost_since`、`alerts.critical` 置一条。

    已经是 `alert` 时只在同态内更新（返回 `False` 表示没有顶层迁移，调用方据此措辞；
    `snapshot.ended` 与其他路一概不受影响）。
    """
    if snapshot.top_state not in ("recording", "alert"):
        raise InvalidTransitionError(
            f"alert 只能从 recording / alert 出发，当前是 {snapshot.top_state!r}",
            top_state=snapshot.top_state,
        )
    migrated = snapshot.top_state == "recording"
    snapshot.source(source).health = "lost"
    snapshot.source(source).lost_since = lost_since
    snapshot.alerts.critical = alert
    snapshot.top_state = "alert"
    return migrated


def recover_source(snapshot: Snapshot, *, source: str) -> bool:
    """一路从 `lost` 恢复：该路 `health → restored`、清 `lost_since`。

    返回 `True` 表示顶层从 `alert` 回到 `recording`（两路都恢复了）；还有路是 `lost`
    时返回 `False`（`top_state` 保持 `alert`，`alerts.critical` 由调用方按判定结果维护）。
    """
    if snapshot.top_state not in ("recording", "alert"):
        raise InvalidTransitionError(
            f"恢复只能发生在 recording / alert，当前是 {snapshot.top_state!r}",
            top_state=snapshot.top_state,
        )
    item = snapshot.source(source)
    item.health = "restored"
    item.lost_since = None
    item.restored = True
    remaining = [one for one in snapshot.sources if one.health == "lost"]
    if not remaining:
        snapshot.top_state = "recording"
        snapshot.alerts.critical = None
        return True
    return False


def begin_drain(snapshot: Snapshot, *, reason: str, error: str | None = None) -> EndedState:
    """`recording` / `alert` → `ended`（子状态 `draining`）：时长冻结、三步全 `waiting`。

    三步的顺序与取值域取自 `DRAIN_STEP_IDS` / `DRAIN_STEP_STATUS`（不重抄字面量）。
    """
    if snapshot.top_state not in ("recording", "alert"):
        raise InvalidTransitionError(
            f"stop 只能从 recording / alert 出发，当前是 {snapshot.top_state!r}",
            top_state=snapshot.top_state,
        )
    if snapshot.session is None:
        raise InvalidTransitionError("stop 时 session 不该是 None", top_state=snapshot.top_state)

    snapshot.session.frozen = True
    # 进收尾 = 这一场不再往归档写内容，「暂停中」这一刻起不成立（时长也已冻结）
    snapshot.session.paused = False
    snapshot.session.paused_at_monotonic = None
    snapshot.ended = EndedState(
        phase="draining",
        steps=[DrainStep(id=step_id) for step_id in DRAIN_STEP_IDS],
        error=error,
    )
    snapshot.top_state = "ended"
    for item in snapshot.sources:
        item.health = "stopped"
    return snapshot.ended


def end_session(snapshot: Snapshot) -> None:
    """`ended` 终态里的一处收尾：`sources[*].health` 从 `stopped` 回到 `unprobed`。

    只在成功档（`ended.phase == "done"`）调；失败停留档保留 `stopped` 现场。
    """
    if snapshot.top_state != "ended":
        raise InvalidTransitionError(
            f"只有 ended 才有这一步，当前是 {snapshot.top_state!r}", top_state=snapshot.top_state
        )
    for item in snapshot.sources:
        item.health = "unprobed"
        item.lost_since = None


# 收尾三函数（签名由 phase2 冻结；只改内存快照、自己不碰 IO）。
# 作用在哪一份内存快照上由调用方指定（`ServiceRuntime` 在收尾编排里经 `snapshot=`
# 关键字交进来），没有隐式回源：调用点显式给快照，是谁的现场就在谁身上改。
# 落盘与提交不在这里：`session.json` 由 `ServiceRuntime` 一侧写，`revision +1`
# 与广播由 `commit()` 做。

def begin_drain_step(step_id: str, snapshot: Snapshot) -> DrainStep:
    """把该步的 `status` 置 `running`、`detail` 刷成这一趟的说明（只改内存）。"""
    step = snapshot.drain_step(step_id)
    step.status = "running"
    step.detail = f"收尾步骤 {step_id} 进行中"
    step.error = None
    return step


def finish_drain_step(
    step_id: str,
    status: str,
    detail: str | None = None,
    error: str | None = None,
    *,
    snapshot: Snapshot,
    frozen_ended: bool = False,
    frozen_interrupted: bool = False,
) -> DrainStep:
    """落该步的登记（`status` 取 `done` / `failed` / `skipped`，外加 `post` 那一格的 `waiting`）。

    `frozen_*` 是取消路径的登记约束（phase2 §3.5.6「三条不覆盖约束」，phase3 的强制打断
    依赖它们），普通调用点一律不传：`frozen_ended=True` 不许改 `ended` 的 `phase` / `error`
    （先落盘写下的「收尾中 + 已打断」由此保住）；`frozen_interrupted=True` 已落定的步骤保持原样。

    `waiting` 是 `post` 那一格的专有终态（R26 / `docs/design-intent.md` 第 10 条）：
    按「结束录音」之后会后一个动作都不自动跑，那一步做完该做的就如实空着等人按按钮。
    别的步骤不许落回 `waiting`——那会把「收尾协程被取消、三步都还没轮到」与
    「这一步不归我管」混成同一副面孔。
    """
    if status not in DRAIN_STEP_STATUS:
        raise DrainStateError(f"未知的收尾状态：{status!r}（取值域 {DRAIN_STEP_STATUS}）")
    if status == "waiting" and step_id != POST_DRAIN_STEP_ID:
        raise DrainStateError(
            f"`waiting` 只能作为 {POST_DRAIN_STEP_ID!r} 那一格的落定结果（它空着等人按按钮），"
            f"别的步骤不许落回 `waiting`：{step_id!r}"
        )

    target = snapshot
    step = target.drain_step(step_id)
    step.status = status
    step.detail = "" if detail is None else str(detail)
    step.error = None if error is None else str(error)

    if status == "failed":
        if target.ended is not None and not frozen_ended:
            target.ended.error = step.error or f"收尾步骤 {step_id} 失败"
    return step


def finish_drain(snapshot: Snapshot, *, cancelled: bool = False) -> str:
    """收尾的唯一出口：按当前三步结果判终态，返回最终 `ended.phase`（写盘与提交不在这里）。

    三步都落定（`{done, skipped, waiting}`，或只差 `post` 停在 `running`）且无 `failed`
    → `done`；任一步 `failed` 或取消那一趟（`cancelled=True`，三步没跑完）→ 保持 `draining`。
    两种结果下 `top_state` 都停在 `ended`、`session` 保留。

    `cancelled` 由取消路径传入：被取消的收尾没有跑完，终态该是 `draining`。取消那趟能把
    在飞那一步照实记成 `running`，所以「只差 post 停在 running」那条必须让开这一趟，
    否则取消会被误判成「收尾走完了」。
    """
    target = snapshot
    if target.ended is None:
        raise DrainStateError("当前没有收尾中的会话（ended 为 None）")
    settled = all(
        step.status in ("done", "skipped", "waiting")
        or (step.id == POST_DRAIN_STEP_ID and step.status == "running")
        for step in target.ended.steps
    )
    if settled and not cancelled:
        target.ended.phase = "done"
        end_session(target)
        return "done"
    target.ended.phase = "draining"
    return "draining"


# 小工具


def now_iso() -> str:
    """本地时区的 ISO 时间戳（秒精度）。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def elapsed_seconds(session: SessionInfo, *, monotonic_now: float) -> float:
    """本场已录秒数：`frozen` 时取冻结值，否则按单调时钟走秒。"""
    if session.frozen:
        return float(session.elapsed_seconds)
    return max(0.0, float(monotonic_now) - float(session.started_monotonic))


def sample_levels(snapshot: Snapshot) -> dict[str, Any]:
    """`levels` 帧的 `frames` 部分（4Hz 抽帧的载荷，两路合在一帧里）。

    场次在录而某一路还没收到过电平帧时，该路按「左边没有柱时沿用 `bar_default`」的
    同一条编码口径补成 15 根静音柱——形状契约（包络长度恰满、`bar == envelope[-1]`）
    任何时刻都成立，补齐只发生在产出这一侧；空闲态的空包络照原样推出。
    """
    active = snapshot.top_state in ("recording", "alert")
    frames: dict[str, Any] = {}
    for item in snapshot.sources:
        envelope = item.envelope
        if active and not envelope:
            envelope = _encode_envelope([None] * ENVELOPE_BAR_COUNT, item.bar_default)
        frames[item.id] = {
            "ts": None,
            "dbfs": item.level_dbfs,
            "peak": item.peak_dbfs,
            "bar": envelope[-1] if envelope else None,
            "envelope": list(envelope),
        }
    return frames


def validate_health(value: Any) -> str:
    """把后端报上来的探针三态收进快照的七取值域；越界取值落 `probe_failed`。"""
    return str(value) if value in SOURCE_HEALTH else "probe_failed"


__all__ = [
    "ALERT_LEVEL",
    "DRAIN_STEP_IDS",
    "DRAIN_STEP_STATUS",
    "ENDED_PHASE",
    "ENVELOPE_BAR_COUNT",
    "ENVELOPE_BAR_SECONDS",
    "HEARTBEAT_MIN_WINDOW_SECONDS",
    "HEARTBEAT_WINDOW_FACTOR",
    "PROBE_STATUS",
    "REVISION_START",
    "SESSION_STATE",
    "SOURCE_HEALTH",
    "SOURCE_LABELS",
    "TOP_STATE",
    "Alert",
    "AlertState",
    "DrainStateError",
    "DrainStep",
    "EndedState",
    "EnvelopeWindow",
    "InvalidTransitionError",
    "ServiceInfo",
    "SessionInfo",
    "SettingsView",
    "Snapshot",
    "SourceState",
    "TranscriptState",
    "begin_alert",
    "begin_drain",
    "begin_drain_step",
    "elapsed_seconds",
    "end_session",
    "finish_drain",
    "finish_drain_step",
    "frame_clock_window",
    "new_snapshot",
    "now_iso",
    "recover_source",
    "sample_levels",
    "start_recording",
    "validate_health",
]
