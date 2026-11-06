"""`ServiceRuntime`——唯一状态权威（phase2 §3.5；从 `app.py` 整体搬来）。

搬迁边界（phase2 §3.5.1）：本模块承载 `ServiceRuntime`（类名、构造语义、方法名与语义
一字不改）；工厂入口 `build_runtime()` 仍在 `app.py`；信号处理那四样常驻 `app.py`，
本模块不复制；写进 `_shutdown_target` 的对象仍由 `startup()` 在这一侧写入。

并发模型（phase2 §3.5.3）：全部状态收在本对象一处、只在 asyncio 事件循环里改、不加锁；
后端把事实送进来的唯一入口是 `loop.call_soon_threadsafe(...)`；`to_dict()` 与 `commit()`
配合：状态变更后经 `commit()` 一次完成「`revision +1` + 广播一次全量 `state`」，别处一律
不自己改 `revision`、不自己发 `state`。唯一例外是「等待一条同步调用」：收尾三步与会中
总结的模型调用都提交到各自的 daemon 工作线程，事件循环只等结果，HTTP 面照旧转；上限
分别是 `compute_drain_budget()` 与 `summary.deadline_seconds`。

拆分说明：方法体按职责拆进本目录的 mixin 模块（`runtime_shared` / `runtime_backends` /
`runtime_facts` / `runtime_sessions` / `runtime_catalog` / `runtime_post` /
`runtime_summary` / `runtime_materials` / `runtime_drain`）；本文件只保留构造、状态权威
核心与 re-export，`__all__` 冻结名字的对外 import 路径不变；测试要 patch 的模块级名字
一律打在读它的那个模块上。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from meeting import __version__, logging_setup  # logging_setup：本模块不再直调，但它是
# tests 以 `runtime_module.logging_setup` 为 patch 路径的那一格（5 处），名字留在这里。
from meeting.alerts import AlertJudge, AlertThresholds
from meeting.backends.registry import BackendResolution
from meeting.config import ConfigValueError
from meeting.session import SessionStore
from meeting.state import (
    ServiceInfo,
    SessionInfo,
    Snapshot,
    new_snapshot,
    now_iso,
)
from meeting.summary import TranscriptBuffer
from meeting.workspace import WorkspaceRegistry
from meeting.service.runtime_backends import RuntimeBackendsMixin
from meeting.service.runtime_catalog import RuntimeCatalogMixin
from meeting.service.runtime_drain import (
    DRAIN_BUDGET_MAX_SECONDS,
    DRAIN_BUDGET_MIN_SECONDS,
    DRAIN_BUDGET_SEGMENT_OVERHEAD_SECONDS,
    DRAIN_DEFAULT_BACKLOG,
    DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS,
    DRAIN_LOG_MIN_BUDGET_SECONDS,
    DRAIN_SETTLE_POLL_SECONDS,
    DrainLimitExceeded,
    RuntimeDrainMixin,
    compute_drain_budget,
)
from meeting.service.runtime_facts import (
    LEVELS_INTERVAL_SECONDS,
    TRANSCRIBER_HEALTH_INTERVAL_SECONDS,
    RuntimeFactsMixin,
)
from meeting.service.runtime_materials import (
    MATERIALS_DIR_NAME,
    RuntimeMaterialsMixin,
)
from meeting.service.runtime_post import RuntimePostMixin
from meeting.service.runtime_sessions import (
    MODEL_EXECUTOR_MAX_WORKERS,
    RuntimeSessionsMixin,
)
from meeting.service.runtime_transcode import RuntimeTranscodeMixin
from meeting.service.runtime_shared import (
    ACTION_CODES,
    ACTION_NAMES,
    DEFAULT_SESSION_MODE,
    SESSION_MODES,
    ActionError,
    PostJob,
    PostJobSlotReadsMixin,
    SessionDirAddressingMixin,
    _DaemonThreadPoolExecutor,
)
from meeting.service.runtime_summary import (
    SUMMARY_EXECUTOR_MAX_WORKERS,
    RuntimeSummaryMixin,
)

_log = logging.getLogger("meeting.service.runtime")


class ServiceRuntime(
    RuntimeBackendsMixin,
    RuntimeFactsMixin,
    RuntimeSessionsMixin,
    RuntimeCatalogMixin,
    RuntimePostMixin,
    RuntimeTranscodeMixin,
    RuntimeSummaryMixin,
    RuntimeMaterialsMixin,
    RuntimeDrainMixin,
    PostJobSlotReadsMixin,
    SessionDirAddressingMixin,
):
    """唯一状态权威：持有 `Snapshot` 与唯一一份状态字段，只在事件循环里改，不加锁。"""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.store = SessionStore()
        # 工作区注册表（住在配置目录 workspaces.json；列表是软件自己记的，不扫任何根目录）
        self.registry = WorkspaceRegistry()

        # `service` 六个字段（重建生效日志、取到实际监听地址之后才齐）
        self.started_at: str = now_iso()
        self.bound_host: str | None = None
        self.bound_port: int | None = None

        # 唯一一份状态
        self.snapshot: Snapshot = new_snapshot(settings=self._settings_view())

        # 后端与判定
        self.capture_resolution: BackendResolution | None = None
        self.transcriber_resolution: BackendResolution | None = None
        self.post_resolution: BackendResolution | None = None
        self.capture_backend: Any = None
        self.transcriber_backend: Any = None
        self.post_hook: Any = None
        self.capture_capabilities: dict[str, bool] = {
            "finalize": False,
            "transcode": False,
            "dev_inject": False,
        }
        self.judge = AlertJudge(thresholds=AlertThresholds.from_config(config))
        self.probes: list[Any] = []

        # 帧时钟（`recording_alive` 的唯一来源）
        self.frame_interval: float = 0.5
        self.last_frame_ts: float | None = None
        self.last_fact_at: float | None = None
        #: 本场 start 的单调钟基准（`recording_alive` 的开场窗口自它起算，de899128）：
        #: 「还没有第一帧」在窗口内报 `None`（未知）、超窗报 `False`（=停）。
        #: 与 `last_frame_ts` 同点写、同点清（`start_session()` 起场 / `__init__` 起步）。
        self.session_start_mono: float | None = None

        # 帧空洞记录（gil-stall-hole）：发帧线程报来的 `FrameGap` 落在这里，只进心跳应答
        #（`heartbeat_dict()` 的 `capture_gaps`），不进快照、不涨 revision、不广播——
        # 空洞是历史事实，`recording_alive` 只回答「现在采不采得到」，两句话各管一头。
        # 条目带区间（`_start` / `_end`，单调钟）供跨路合并，出读数时剥掉；上限只留给
        # 「最近列表」，累计数不设上限。本场起场清零。
        self._capture_gaps: list[dict[str, Any]] = []
        self._capture_gap_totals: dict[str, float] = {"count": 0, "seconds": 0.0}

        #: 每一路上一次落过 log 的 `health`（`_log_health_migrations()` 的比对基线）：
        #: 契约把「每一路健康迁移（含原因与阈值）」列为必须落 log 的事件，基线按路存一份。
        self._health_logged: dict[str, str] = {item.id: item.health for item in self.snapshot.sources}

        #: 快照 `alerts.hints` 里眼下摆着哪几条（按 `(level, source, message)` 登记，有序）。
        #: 它是「提示级的键集合有没有变」的判据：只有键集合真的动了才让调用点走一次
        #: `commit()`，`duration_seconds` 的刷新因此不给 `revision` 投票（转写通道不参与
        #: 状态迁移，0.5 秒的节拍不许把它抖起来）。清提示级时与判定器那份登记一起清
        #: （唯一入口 `_clear_hints()`，复审 m3）。
        self._hint_keys: tuple[tuple[str, str, str], ...] = ()

        #: 本场开场读到的旧场次记录（`None` = 新场；有值 = 续接）：旧段的段记录、各路旧段
        #: 文件名表与场次身份三格从它合并（第 9 条）。
        self._resumed_record: dict[str, Any] | None = None

        # 收尾 task 句柄（属性名冻结；启动时赋值、走完含被取消那趟后置空）
        self._drain_task: asyncio.Task | None = None
        #: 收尾三步的同步调用专用执行器（工作线程是 daemon：第二次 `Ctrl+C` 的「立即中止」
        #: 不许被在飞的同步步拖住）。惰性创建：不跑收尾的进程一个线程都不起。
        self._drain_executor: _DaemonThreadPoolExecutor | None = None
        #: 这一趟收尾的共享 deadline（`drain()` 开头算出来、三步共用；不在收尾中时为 `None`）
        self._drain_deadline: float | None = None
        #: 最近一趟收尾自己算出来的那笔预算。`_drain_deadline` 收尾跑完时清空，这一笔留着：
        #: 「等收尾落定」的上限与本次收尾同源（`_wait_drain_settled()` 直接沿用）。
        self._drain_budget: float | None = None
        #: 当前这一步进入的时刻（每一步的实测耗时从它算起）
        self._drain_step_started: float | None = None
        #: 本次关停是不是强制打断那一档（`trigger_shutdown()` 写、`cli.py` 读来挑退出码）
        self._shutdown_interrupted = False

        # SSE
        from meeting.service.hub import EventHub

        self.hub = EventHub(snapshot_provider=self._snapshot_json)

        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._signal_ok = False
        self._ticker: asyncio.Task | None = None
        #: 转写健康度节拍（低频、只写快照；见 `_transcriber_health_loop()`）
        self._transcriber_ticker: asyncio.Task | None = None

        # -- 会中总结（`1987cb20` phase2 §3.6）：两样都是内存里的、会话级的、不落文件
        # （设计章 2.3 / 5.1：会中总结做完就该消失）。
        # `summary_buffer`：内存里那条追加中的实时转写——盘上的会中临时稿只有分钟级
        # `[HH:MM]`（秒被丢掉、同一分钟撞成一个戳），切不了窗口；这份带完整墙钟浮点 `ts`
        # 与入队序，喂它的入口只有 `bind_transcriber()` 包出来的那个 sink。
        # `summary_records`：本场按过的、成功产出的那几次（时刻 + 窗口 + 那几条），只记
        # 成功的——失败没有「那几条」可记。`GET /api/summaries` 读的就是它。
        # `summary_client` 可注入（用例塞假件）；那一次 `complete()` 是同步调用，经
        # `_summary_wait()` 提交到下面这台池（跑在事件循环上会把 `/api/state` 与三个 SSE
        # 通道按住）。惰性创建：没人按总结的进程一个线程都不起。
        self.summary_buffer = TranscriptBuffer()
        self.summary_records: list[dict[str, Any]] = []
        self.summary_client: Any = None
        #: 会中总结那一次模型调用的同步调用专用执行器（与收尾三步那台分属两台池）
        self._summary_executor: _DaemonThreadPoolExecutor | None = None

        #: 会中模型「收 / 放」两次调用的同步调用专用执行器。单线程：两次调用是同一格模型
        #: 引用上的一进一出，必须按用户按下的顺序 FIFO 排队；惰性创建。
        self._model_executor: _DaemonThreadPoolExecutor | None = None

        # -- 会后的多趟活与转码工序（人按才起；转码由 flush 落定自动起）
        # `self.post_jobs` 按 `(kind, dir)` 记每一趟在飞的活：offline / minutes 是会后两个
        # 按钮，transcode 是自动起的那道独立工序（三个 transcode_* 动作也落在同一张登记上）。
        # 不同场次、不同种类的活可以并排飞，唯一槽判据是 `(kind, dir)`——同一种重活在
        # 同一场上只能有一趟。唯一硬闸 = 本地大模型互斥：offline 与会中录音不并排（`start`
        # 按 409 拒、录音中按「生成会后精细稿」按 409 拒），纪要与转码都免闸。
        # 不写快照、不落盘：界面问「有没有会后精细稿」走 `GET /api/sessions` 的 `has_offline`
        # （读盘），与本字段无关。
        self.post_jobs: dict[tuple[str, str], PostJob] = {}

    # 状态 JSON 产出（唯一产出者）

    def _session_elapsed_seconds(
        self, session: SessionInfo, *, moment: float | None = None
    ) -> float:
        """这一场**录到的**秒数：墙钟跨度扣掉暂停的那几段（`docs/design-intent.md` 第 7 条）。

        权威长度是实际录到的音频时长（写侧的内容字节计数，落 `session.json`）；快照这一格是
        它在界面上的读数，暂停期间因此不往前走 —— 否则「已录 12:30」会把休息的十分钟也算进
        这一场，按第 7 条那是附注、按第 8 条那段时间根本没录。
        """
        now = time.monotonic() if moment is None else float(moment)
        paused = float(session.paused_seconds)
        if session.paused_at_monotonic is not None:
            paused += max(0.0, now - float(session.paused_at_monotonic))
        return max(0.0, (now - session.started_monotonic) - paused)

    def _live_elapsed(self) -> None:
        """把本场已录秒数刷成当下值：录音 / 告警态走秒，进 `ended` 之后冻结（不再动）。"""
        session = self.snapshot.session
        if session is None or session.frozen:
            return
        session.elapsed_seconds = self._session_elapsed_seconds(session)

    def _snapshot_json(self) -> dict[str, Any]:
        self._live_elapsed()
        return self.snapshot.to_dict(self._service_info())

    def to_dict(self) -> dict[str, Any]:
        """产出 `GET /api/state` 与 SSE 首帧共用的那份快照（**唯一产出者**）。"""
        if self.bound_port is None:
            raise ConfigValueError(
                "service.port",
                None,
                "先绑监听 socket 并调 set_binding()，再取快照"
                "（框架 startup 钩子跑起来之前取快照一律视为实现不合格）",
                "service.port / service.url 不落配置值、不落 0、不落字面量拼接",
            )
        return self._snapshot_json()

    def _service_info(self) -> ServiceInfo:
        return ServiceInfo(
            started_at=self.started_at,
            version=__version__ if isinstance(__version__, str) and __version__ else "0.0.0",
            port=int(self.bound_port or 0),
            url=self.url if self.bound_port not in (None, 0) else "",
            log_path=self.log_path,
        )

    # 每一路健康迁移的日志事件（契约点名的必落 log 事件）

    def _log_health_migrations(
        self,
        *,
        source: str | None = None,
        reason: str = "",
        threshold: str | None = None,
    ) -> None:
        """把这一刻与上一次落过 log 的那一版之间的每一路 `health` 变化落成一条 log。

        契约把「每一路健康迁移（含原因与阈值）」列为必须落 log 的事件（phase1 §3.13、
        `index.md` §7 的 G6 要求场次 log 结束标记之前逐类可查）。本方法只加日志、不改任何
        状态语义：`health` 的值仍由 `meeting/state.py` 的迁移函数写。

        行内容按契约四项写：哪一路、从什么值到什么值、原因、阈值。「阈值：」有三种形态，
        读 log 的人按三档认（审查观察项 M4 点名的误读点）：

          1. 实测阈值（`alert.*` 判定真用到阈值的两条迁移）：从 `self.judge.thresholds`
             现读（不是配置默认值、不是字面量）；
          2. 无阈值：判据里本来就没有阈值数，`threshold` 显式传说明或传空补「无阈值」——
             这档冒号后面没有可取的数，按 `<键>=<数>` 解析会落空；
          3. 不看阈值（收尾相关三处）：迁移由收尾流程驱动，阈值判定没参与——
             与第 2 档长相接近，差别是原因。

        无论哪一档都不编造一个数。`source` 给定时只比那一对；幂等（基线刷成当下值）。
        """
        for item in self.snapshot.sources:
            if source is not None and item.id != source:
                continue
            previous = self._health_logged.get(item.id)
            if previous == item.health:
                continue
            self._health_logged[item.id] = item.health
            _log.info(
                "每一路健康迁移：%s %s -> %s（原因：%s；阈值：%s）",
                item.id,
                "未记录" if previous is None else previous,
                item.health,
                reason or "未注明",
                threshold or "无阈值",
            )

    def commit(self) -> int:
        """唯一的状态提交 + 广播点：`revision +1` 与一次全量 `state` 广播都在这里。

        别处一律不自己改 `revision`、不自己发 `state`。`alerts.*.muted` 在这里按当下重算
        （判定权收在 `AlertJudge.refresh_muted()` 一处）。提交前先比一遍两路 `health` 作兜底：
        正常时一个都不命中（各迁移点已落过记录），真有哪条路绕过了落点，这里也如实留痕
        （原因写「未注明」）——「健康迁移必须落 log」不因少写一处调用点而整类消失。
        """
        self._log_health_migrations()
        self.snapshot.alerts.refresh_muted(judge=self.judge)
        self.snapshot.revision += 1
        self.hub.broadcast_state(self._snapshot_json())
        return int(self.snapshot.revision)


__all__ = [
    "ACTION_CODES",
    "ACTION_NAMES",
    "DEFAULT_SESSION_MODE",
    "DRAIN_BUDGET_MAX_SECONDS",
    "DRAIN_BUDGET_MIN_SECONDS",
    "DRAIN_BUDGET_SEGMENT_OVERHEAD_SECONDS",
    "DRAIN_DEFAULT_BACKLOG",
    "DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS",
    "DRAIN_LOG_MIN_BUDGET_SECONDS",
    "DRAIN_SETTLE_POLL_SECONDS",
    "LEVELS_INTERVAL_SECONDS",
    "MATERIALS_DIR_NAME",
    "MODEL_EXECUTOR_MAX_WORKERS",
    "SESSION_MODES",
    "SUMMARY_EXECUTOR_MAX_WORKERS",
    "TRANSCRIBER_HEALTH_INTERVAL_SECONDS",
    "ActionError",
    "DrainLimitExceeded",
    "ServiceRuntime",
    "compute_drain_budget",
]
