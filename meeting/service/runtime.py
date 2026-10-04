"""`ServiceRuntime` —— 唯一状态权威（phase2 §3.5；从 `meeting/service/app.py` 整体搬来）。

搬迁边界（phase2 §3.5.1，写死）：

  - 本模块承载 `ServiceRuntime`（含 `to_dict()`、`startup()` / `shutdown()`、`bind_capture()` /
    `bind_transcriber()`、状态迁移与收尾编排）——**类名、构造语义、方法名与语义一字不改**；
  - 工厂入口 `build_runtime()` **仍在 `meeting/service/app.py`**（`cli.py` 的调用点一个字不动）；
  - `handle_shutdown_signal` / `install_shutdown_signal_handlers()` / 模块级 `_shutdown_target` /
    关掉框架信号捕获的服务器子类 `ServiceServer` **全部常驻 `app.py`**，本模块不复制一份；
  - 写进 `_shutdown_target` 的对象仍由 `startup()` 在这一侧写入（安装点与调用形态不变）。

并发模型（phase2 §3.5.3）：**全部状态收在本对象一处、只在 asyncio 事件循环里改、不加锁**；
下游后端跑在自己的线程里，把事实送进来的**唯一入口**是 `loop.call_soon_threadsafe(...)`
（在 `bind_*()` 里包好交给后端，后端拿不到裸的状态对象）；
**`to_dict()` 与 `commit()` 配合**：状态变更后经 `commit()` 一次完成「`revision +1` +
广播一次全量 `state`」，别处一律不自己改 `revision`、不自己发 `state`。

**唯一的例外是「等待一条同步调用」**：收尾四步（`_drain_wait()`）与会中总结那一次模型调用
（`summarize()` → `_summary_wait()`，`1987cb20` phase2 的复审裁定）都**不**在事件循环上跑
同步实现 —— 它们提交到各自的 daemon 工作线程，事件循环只等结果，`/api/state`、三个 SSE 通道、
心跳与健康节拍照旧转。两条等待的上限都是自己那条 deadline（收尾是 `compute_drain_budget()`、
总结是 `summary.deadline_seconds`）。

拆分说明（聚合壳）：`ServiceRuntime` 的方法体按职责拆进了本目录的 mixin 模块 ——
`runtime_shared.py`（模块级冻结名字 + daemon 线程池的叶子）、`runtime_backends.py`
（接线 / 解析 / 探测 / 起停）、`runtime_facts.py`（心跳 / 电平 / 转写 / 告警）、
`runtime_sessions.py`（场次生命周期）、`runtime_catalog.py`（工作台 / 目录 / 状态视图）、
`runtime_post.py`（会后作业）、`runtime_summary.py`（会中总结）、`runtime_drain.py`
（收尾 / 关停）。类名、构造语义、方法名与方法语义依旧一字不改 —— 本文件只保留
构造（`__init__`）、状态权威核心（`to_dict()` / `commit()` 等）与 re-export，
`__all__` 冻结名字的对外 import 路径不变；测试要 patch 的模块级名字，一律打在
**读它的那个模块**（`runtime_drain` 等）上。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import os
import threading
import time
import weakref
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
from concurrent.futures.thread import _worker as _threadpool_worker
from typing import Any, Callable

from meeting import __version__, config as config_module, logging_setup
from meeting.alerts import AlertJudge, AlertThresholds
from meeting.asr import pipeline as asr_pipeline
from meeting.backends.base import (
    SOURCE_IDS,
    FrameGap,
    LevelFrame,
    ReadOnlyConfig,
    SourceFacts,
    SourceLost,
    TranscriptLine,
)
from meeting.backends.registry import (
    BackendResolution,
    capabilities_of,
    resolve_capture_backend,
    resolve_post_hook,
    resolve_transcriber_backend,
)
from meeting.config import ConfigValueError
from meeting.session import (
    SESSION_FILE_NAME,
    SessionStore,
    archive_name,
    build_session_record,
    next_segment_number,
    resolve_workspace_dir,
    state_for,
    steps_from_ended,
)
from meeting.summary import (
    WINDOW_CHOICES,
    ModelClient,
    SummaryOutcome,
    SummarySettings,
    TranscriptBuffer,
    run_summary,
    sections_for,
)
from meeting.state import (
    DRAIN_STEP_IDS,
    PROBE_STATUS,
    Alert,
    InvalidTransitionError,
    ServiceInfo,
    SessionInfo,
    SettingsView,
    Snapshot,
    begin_alert,
    begin_drain,
    begin_drain_step,
    bind_snapshot,
    finish_drain,
    finish_drain_step,
    new_snapshot,
    now_iso,
    recover_source,
    sample_levels,
    start_recording,
    validate_health,
)
from meeting.speaker_names import (
    SpeakerNamesError,
    identity_rows as speaker_identity_rows,
    set_speaker_name,
)
from meeting.workspace import (
    WorkspaceRegistry,
    canonical_dir,
    classify_workspace_dir,
    has_offline_transcript,
)
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
from meeting.service.runtime_post import RuntimePostMixin
from meeting.service.runtime_sessions import (
    MODEL_EXECUTOR_MAX_WORKERS,
    RuntimeSessionsMixin,
)
from meeting.service.runtime_shared import (
    ACTION_CODES,
    ACTION_NAMES,
    DEFAULT_SESSION_MODE,
    SESSION_MODES,
    DRAIN_EXECUTOR_MAX_WORKERS,
    ActionError,
    PostJob,
    _DaemonThreadPoolExecutor,
    _job_matches,
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
    RuntimeSummaryMixin,
    RuntimeDrainMixin,
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
        bind_snapshot(self.snapshot)

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

        # 帧空洞账（gil-stall-hole）：发帧线程报来的 `FrameGap` 落在这里，只进心跳应答
        #（`heartbeat_dict()` 的 `capture_gaps`），**不进快照、不涨 revision、不广播** ——
        # 空洞是历史事实，`recording_alive` 只回答「现在采不采得到」，两句话各管一头。
        # `_capture_gaps` 的条目带区间（`_start` / `_end`，单调钟）供跨路合并，出账时剥掉；
        # 上限只留给「最近列表」，累计数（`_capture_gap_totals`）不设上限。本场起场清零。
        self._capture_gaps: list[dict[str, Any]] = []
        self._capture_gap_totals: dict[str, float] = {"count": 0, "seconds": 0.0}

        #: 每一路**上一次落过 log 的** `health`（`_log_health_migrations()` 的比对基线）。
        #: 契约（phase1 §3.13 / phase2 的日志事件清单）把「每一路健康迁移（含原因与阈值）」
        #: 列为**必须落 log** 的事件；基线按路存一份，于是「从什么值到什么值」永远是实测的。
        self._health_logged: dict[str, str] = {item.id: item.health for item in self.snapshot.sources}

        #: **快照 `alerts.hints` 里眼下摆着哪几条**（按 `(level, source, message)` 记账，有序）。
        #: 它是「提示级的键集合有没有变」的判据：`_sync_transcript_hints()` 拿它跟判定器刚算出来的
        #: 那几条比，只有键集合真的动了才返回「有变化」、才让调用点走一次 `commit()`。
        #: 同一个键集合里 `duration_seconds` 的刷新因此不给 `revision` 投票（契约第 3.3 节：
        #: 转写通道不参与状态迁移，0.5 秒的节拍不许把它抖起来）。
        #: **清提示级时它与判定器那份记账一起清**（唯一入口 `_clear_hints()`）：只清一边的话，
        #: 两者会有一刻对不上、旧记账还会影响下一场的键集合判据（复审 m3）。
        self._hint_keys: tuple[tuple[str, str, str], ...] = ()

        #: 本场开场读到的**旧场次记录**（`None` = 新场；有值 = 续接，`start_session()` 写、
        #: `_session_record()` 读）：旧段的段账、各路旧段文件名表与场次身份三格从它合并，
        #: 让续接那一场落盘的每一版 `session.json` 都带着前几段的完整账（第 9 条）。
        self._resumed_record: dict[str, Any] | None = None

        # 收尾 task 句柄（属性名冻结；启动时赋值、走完含被取消那趟后置空）
        self._drain_task: asyncio.Task | None = None
        #: 收尾四步的**同步调用专用**执行器（工作线程是 daemon，见
        #: `_DaemonThreadPoolExecutor` 的说明：第二次 `Ctrl+C` 的「立即中止」不许被在飞的
        #: 同步步拖住）。**惰性创建**：不跑收尾的进程一个线程都不起。
        self._drain_executor: _DaemonThreadPoolExecutor | None = None
        #: 这一趟收尾的共享 deadline（`drain()` 开头算出来、四步共用；不在收尾中时为 `None`）
        self._drain_deadline: float | None = None
        #: 最近一趟收尾**自己算出来的**那笔预算（`compute_drain_budget()` 的输出，见
        #: `_run_drain_steps()` 开头）。`_drain_deadline` 在收尾跑完时清空，这一笔留着：
        #: 「等收尾落定」的上限与本次收尾**同源**（`_wait_drain_settled()` 直接沿用，
        #: 于是 `_drain_inputs()` 一趟收尾只读一次）。
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

        # -- 会中总结（`1987cb20` phase2 §3.6）--------------------------------
        #
        # 两样东西都是**内存里的、会话级的、不落文件**（设计章 2.3 / 5.1：会中总结做完就该消失）：
        #
        #   - `summary_buffer`：**内存里那条追加中的实时转写**。盘上的会中临时稿只有分钟级
        #     `[HH:MM]`（秒被丢掉、同一分钟撞成一个戳），切不了窗口；内存这一份带完整的墙钟浮点
        #     `ts` 与入队序。喂它的入口只有一个：`bind_transcriber()` 包出来的那个 sink；
        #   - `summary_records`：**本场按过的、成功产出的那几次**（时刻 + 窗口 + 那几条）。
        #     只记成功的那几次 —— 失败没有「那几条」可记。`GET /api/summaries` 读的就是它。
        #
        # `summary_client` 是**可注入的总结客户端**（`ModelClient` 或同形的假件）：不给就由
        # `run_summary()` 按 `SummarySettings` 现造一个真客户端。用例从这里塞记账用的假件。
        #
        # `summary_client` 那一次 `complete()` 是一条**同步**调用：`summarize()` 经
        # `_summary_wait()` 把它提交到下面这台池的工作线程，事件循环因此在总结期间照旧转
        # （复审 minor 6：跑在事件循环上时，`/api/state` 与三个 SSE 通道会被一起按住）。
        # **惰性创建**：没人按下总结的进程一个线程都不起。
        self.summary_buffer = TranscriptBuffer()
        self.summary_records: list[dict[str, Any]] = []
        self.summary_client: Any = None
        #: 会中总结那一次模型调用的**同步调用专用**执行器（工作线程是 daemon，
        #: 与收尾四步那台分属两台池，见 `_DaemonThreadPoolExecutor` 的说明）
        self._summary_executor: _DaemonThreadPoolExecutor | None = None

        #: 会中模型「收 / 放」两次调用（`release_gpu()` / `acquire_gpu()`）的**同步调用专用**
        #: 执行器。**单线程**：这两次调用是同一格模型引用上的一进一出，必须按用户按下的顺序
        #: 排队（暂停 → 释放，继续 → 拉起），单线程池给的正是这条 FIFO 顺序；**惰性创建**：
        #: 没人按过暂停/继续的进程一个线程都不起。
        self._model_executor: _DaemonThreadPoolExecutor | None = None

        # -- 会后的两趟活（人按才起，`d63b0f4a` 的两个按钮）--------------------
        #
        # `self.post_job` 记**这一趟会后活跑到哪一档**（`None` = 没有在跑的）：
        #   七键形状冻结在 `PostJob`（runtime_shared；`{"kind", "dir", "thread",
        #   "started_at", "finished_at", "progress", "cancel"}`）
        # 它**不写快照、不落盘**：这一格是服务进程自己的现场（界面问「有没有会后精细稿」
        # 走 `GET /api/sessions` 的 `has_offline`，那一条读盘，与本字段无关）。
        # 「一趟没跑完就按第二个按钮」当场拒（409 `draining`）：一张显卡上不该同时开两份模型
        # （`docs/design-intent.md` 第 21 条）。
        self.post_job: PostJob | None = None

    # -- 状态 JSON 产出（唯一产出者） -------------------------------------

    def _session_elapsed_seconds(
        self, session: SessionInfo, *, moment: float | None = None
    ) -> float:
        """这一场**录到的**秒数：墙钟跨度扣掉暂停的那几段（`docs/design-intent.md` 第 7 条）。

        权威长度是实际录到的音频时长（写侧的内容字节账，落 `session.json`）；快照这一格是
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

    # -- 每一路健康迁移的日志事件（契约点名的必落 log 事件） ----------------

    def _log_health_migrations(
        self,
        *,
        source: str | None = None,
        reason: str = "",
        threshold: str | None = None,
    ) -> None:
        """把这一刻与**上一次落过 log 的那一版**之间的每一路 `health` 变化落成一条 log。

        契约依据：phase1 §3.13 与 phase2 的「必须落 log 事件」清单把**每一路健康迁移
        （含原因与阈值）**列为必须落 log 的事件；`phase3.md` §3.8 第 ⑫ 条与 `index.md`
        §7 的 G6 据此要求场次 log 里、结束标记**之前**这一类逐类可查。

        **本方法只加日志，不改任何状态语义**（`phase3.md` §3.2 明写「不改四态迁移与
        `Snapshot` 形状」）：`health` 的值仍由 `meeting/state.py` 的迁移函数写，
        `phase2.md` §3.5.2 的映射表（`alert → recording` 之后该路 `health = restored`）
        一个字都不动。这里做的只是「变化之后如实记一行」。

        行内容按契约的四项写：**是哪一路、从什么值到什么值、原因、阈值**。
        **「阈值：」这个字段有三种形态，读 log 的人照下面这三档认，别按同一种去取值**
        （审查观察项 M4 点名的误读点：这三种形态下的取值方式是各自不同的）：

          1. **实测阈值**（`alert.*` 判定真的用到阈值的那两条迁移）——
             `连续有信号达恢复阈值` 写 `恢复阈值 alert.recover_seconds=<生效值>s`、
             `这一路起致命级告警` 写 `静默阈值 alert.silence_seconds=<生效值>s /
             silence_dbfs=<生效值> dBFS`。这两个值是 `AlertJudge` 持有的、本次判定**真的用到**
             的阈值，取值从 `self.judge.thresholds` 现读（不是配置默认值、不是字面量）；
          2. **无阈值**——这一趟迁移的判据里本来就没有阈值数：`threshold` 参数显式传了那句
             说明（例如 `探针三态（unprobed / ready / probe_failed），本档无阈值`、
             `本场 mode=both（采到的路进 capturing）`），传空时这里补 `无阈值`。
             这一档里**冒号后面没有可取的数**，任何按 `<键>=<数>` 去解析的读法都会落空；
          3. **不看阈值（收尾相关的三处）**——场次进收尾、场次撤回空闲态、收尾终态这三条
             迁移各自的说明里都写死「不看阈值」（例：`无阈值（收尾终态这一步不看阈值）`）。
             这一档与第 2 档在 log 上长相接近，差别是**原因**：这里的迁移由收尾流程自己驱动，
             阈值判定根本没参与，因此它既没有实测值、也不该被当成「判定过了但没设阈值」。

        无论哪一档，**这里都不编造一个数**（例如 `alert → recording` 这一路只报「连续有信号达
        恢复阈值」，运行时的阈值口径由 `AlertJudge` 掌握、这里按第 1 档从它现读，不去猜）。

        `source` 给定时只比那一对（调用点最清楚的场合）；不给定时比全部路。
        幂等：每次调用都把基线刷成当下值，同一格不会重复落 log。
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
        """**唯一的状态提交 + 广播点**：`revision +1` 与一次全量 `state` 广播都在这里。

        别处（迁移函数、动作处理器、收尾三函数、hub）一律不自己改 `revision`、
        不自己发 `state`。

        `alerts.*.muted` 在这里按**当下**重算一遍（判定权收在 `AlertJudge.refresh_muted()`
        一处）：本次提交要广播出去的那份快照，说的就是此刻会不会出声 —— 会话级静音复位、
        抑制窗口到点、下一场 `start` 复位之后，同一条告警算出来就回到 `False`。

        **兜底**：提交之前先比一遍两路 `health`。凡是会改 `health` 的迁移点都已在自己的
        落点上落过一条带原因 / 阈值的记录（`_log_health_migrations()` 幂等），这里因此通常
        一个都不命中；真有哪条路绕过了那些落点，这一遍也会如实留下痕迹（原因写「未注明」），
        于是契约里「每一路健康迁移必须落 log」这件事不会因为少写一处调用点而整类消失。
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
    "MODEL_EXECUTOR_MAX_WORKERS",
    "SESSION_MODES",
    "SUMMARY_EXECUTOR_MAX_WORKERS",
    "TRANSCRIBER_HEALTH_INTERVAL_SECONDS",
    "ActionError",
    "DrainLimitExceeded",
    "ServiceRuntime",
    "compute_drain_budget",
]
