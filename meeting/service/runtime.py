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
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from typing import Any, Callable

from meeting import __version__, config as config_module, logging_setup
from meeting.alerts import AlertJudge, AlertThresholds
from meeting.backends.base import (
    SOURCE_IDS,
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
    build_session_record,
    restart_unfinished_text,
    state_for,
    steps_from_ended,
)
from meeting.state import (
    DRAIN_STEP_IDS,
    PROBE_STATUS,
    Alert,
    InvalidTransitionError,
    Notice,
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

_log = logging.getLogger("meeting.service.runtime")

#: `levels` 抽帧节奏：对外一律 **4 次/秒**（0.25 秒一帧，两路合在一帧里发）
LEVELS_INTERVAL_SECONDS = 0.25

#: 收尾预算的名字（取值与实现归 phase3，本 Phase 只冻结名字与签名）
DRAIN_BUDGET_MIN_SECONDS = 30
DRAIN_BUDGET_MAX_SECONDS = 300


def compute_drain_budget(backlog: int, segment_timeout: float) -> float:
    """`budget = 30 + backlog × (segment_timeout + 5)`（下限 30、封顶 300）。

    **本 Phase 只冻结名字与签名，取值实现归 phase3**（phase2 §3.5.6 / §3.13）。
    """
    budget = DRAIN_BUDGET_MIN_SECONDS + int(backlog) * (float(segment_timeout) + 5.0)
    return float(min(DRAIN_BUDGET_MAX_SECONDS, max(DRAIN_BUDGET_MIN_SECONDS, budget)))


# ---------------------------------------------------------------------------
# 动作层的错误（`code` 取值只用已冻结的那七个串，不自造）
# ---------------------------------------------------------------------------

#: 动作失败的 `code` 全集（phase2 §3.6.2）
ACTION_CODES: tuple[str, ...] = (
    "already_recording",
    "draining",
    "capture_unavailable",
    "not_recording",
    "invalid_setting",
    "persist_failed",
    "action_unavailable",
)

#: 动作名清单（phase2 §3.13）
ACTION_NAMES: tuple[str, ...] = (
    "start",
    "stop",
    "mute_session",
    "settings_update",
    "dev_inject",
)

#: `start` 的 `mode` 取值域与缺省
SESSION_MODES: tuple[str, ...] = ("both", "mic", "loopback")
DEFAULT_SESSION_MODE = "both"


class ActionError(Exception):
    """一次动作的失败（带机器可读的 `code`、给人看的 `message` 与 `detail`）。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status = int(status)
        self.detail = dict(detail or {})
        super().__init__(message)


class ServiceRuntime:
    """唯一状态权威：持有 `Snapshot` 与唯一一份状态字段，只在事件循环里改，不加锁。"""

    def __init__(self, out_root: str, config: Any) -> None:
        self.output_root = str(out_root)
        self.config = config
        self.store = SessionStore(str(out_root))

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

        # 收尾 task 句柄（属性名冻结；启动时赋值、走完含被取消那趟后置空）
        self._drain_task: asyncio.Task | None = None

        # SSE
        from meeting.service.hub import EventHub

        self.hub = EventHub(snapshot_provider=self._snapshot_json)

        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._signal_ok = False
        self._ticker: asyncio.Task | None = None
        self._pending_restart_session: str | None = None

    # -- 只读小接口 -------------------------------------------------------

    @property
    def revision(self) -> int:
        return int(self.snapshot.revision)

    @property
    def top_state(self) -> str:
        return self.snapshot.top_state

    @property
    def url(self) -> str:
        """由**监听 socket 的 `getsockname()` 读回值**拼出的 URL（绑定前取不到）。"""
        if self.bound_port is None:
            raise ConfigValueError(
                "service.url",
                None,
                "先绑监听 socket 并调 set_binding()，再取 URL"
                "（绑定前这个值取不到，不许拿配置值或字面量顶替）",
                "service.port / service.url 只能由监听 socket 的 getsockname() 产生",
            )
        return f"http://{self.bound_host}:{self.bound_port}"

    @property
    def log_path(self) -> str:
        return logging_setup.service_log_path()

    @property
    def out_root(self) -> str:
        return self.output_root

    def _value(self, key: str) -> Any:
        if self.config is None:
            return config_module.default_for(key)
        return self.config.get(key)

    def _settings_view(self) -> SettingsView:
        """快照 `settings` 的**生效值**视图。

        口径（§3.5.5 + §7.1 高危点 7）：这里读的是**运行期生效值**，也就是启动时定下来的那一份。
        `settings_update` 把非热读键写盘成功之后，快照里的 `settings` **不会**立刻反映写进去的
        新值 —— 那 16 个键（含 `alarm.sound`）改名归 `pending_restart`，要重启才在快照里可见；
        热读五键（`asr.*`）才在写盘后立刻换成新值（那一处在 `settings_update()` 里按 `applied`
        重取一次本函数）。
        """
        return SettingsView(
            alarm_sound=bool(self._value("alarm.sound")),
            heartbeat_interval_seconds=int(self._value("heartbeat.interval_seconds")),
            # `settings.asr` 的五个子键与配置键 `asr.*` **逐字同名**（只去掉前缀），取生效值；
            # 嵌套写法只出现在状态 JSON 里，不进请求 / 应答 / config.json
            asr={name: self._value(f"asr.{name}") for name in config_module.ASR_SETTING_KEYS},
            devices={},
        )

    # -- 绑定地址 ---------------------------------------------------------

    def set_binding(self, host: str, port: int) -> None:
        """从监听 socket 的 `getsockname()` 读回值写入（`port` 不许另外猜）。

        **已经绑到一个真实端口之后不许被改掉**（`port == 0` 是「还没绑」的同一档，
        仍可被真实端口覆盖）。这样「快照里的 `service.port` 就是本次真正监听的那个端口」
        这条判据在 `cli.py` 之外也成立。
        """
        if self.bound_port not in (None, 0):
            return
        self.bound_host = str(host)
        self.bound_port = int(port)

    def bind_from_scope(self, scope: dict[str, Any]) -> None:
        """ASGI 挂载路径上用请求的 `server` 信息补齐绑定（只补一次）。"""
        if self.bound_port not in (None, 0):
            return
        server = scope.get("server") or ("127.0.0.1", 0)
        host, _, port = str(server[0]), server[1], None
        try:
            port = int(server[1])
        except (TypeError, ValueError, IndexError):  # pragma: no cover - 非标准 server 元组
            port = 0
        self.set_binding(host or "127.0.0.1", port or 0)

    # -- 生命周期（由框架 lifespan 的 startup 钩子触发） -------------------

    async def startup(self) -> None:
        """启动序列：取事件循环 → 写关停目标 → 信号自检 → 解析后端 → 探测设备 →
        组装初始快照 → 扫未收尾场次 → 打开浏览器。**绝不自动开始录音。**
        """
        if self._started:
            return
        self._started = True
        self.started_at = now_iso()

        # ① 取事件循环（`asyncio.get_running_loop()` 只有在这里才成立）
        self._loop = asyncio.get_running_loop()

        # ② 写关停目标：语义 = 「立即请求关停」（常驻 app.py 的安装点，形态不变）
        from meeting.service import app as service_app

        service_app.set_shutdown_target(self._loop, service_app.make_shutdown_trigger(self._loop))

        # ③ 信号接管自检（服务器的信号捕获已被 ServiceServer.capture_signals() 屏蔽）
        self._signal_ok = service_app.check_signal_handlers()

        # ④ 解析三个后端（唯一入口是 registry；失败回退替身 + warning）
        self._resolve_backends()

        # ⑤ 组装初始快照（`top_state: idle`、`session: null`、`sources` 取 probe() 的三态）
        self._probe_devices()

        # ⑥ 扫未收尾场次（只取最新的一场；命中即挂 notice 并改写现场）
        self._scan_unfinished()

        # ⑦ 打开浏览器（尾部；`MEETING_NO_BROWSER=1` 时跳过并落一行日志）
        self._open_browser()

    def _open_browser(self) -> None:
        """打开浏览器这一步：故障一律收成警告，**不阻塞启动**。"""
        from meeting.service import app as service_app

        try:
            service_app.launch_browser(self.url)
        except BaseException as exc:  # 兜底：这一步的任何意外都不许带崩启动钩子
            _log.warning(
                "打开浏览器这一步出意外（只警告、不阻塞）：%s: %s", type(exc).__name__, exc
            )

    async def shutdown(self) -> None:
        """关停动作：停 ticker、停采集、清关停目标（本 Phase 不做收尾，收尾归 phase3）。"""
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        try:
            if self.capture_backend is not None:
                self.capture_backend.stop()
        except Exception:
            _log.exception("关停时停采集失败（如实记账，不静默）")
        from meeting.service import app as service_app

        service_app.clear_shutdown_target()

    # -- 后端与探测 -------------------------------------------------------

    def _resolve_backends(self) -> None:
        """三个后端的解析入口（唯一入口是 `registry`；**判缺席只看解析结论**）。"""
        self.capture_resolution = resolve_capture_backend()
        self.capture_backend = self.capture_resolution.implementation
        self.capture_capabilities = capabilities_of(self.capture_backend)

        self.transcriber_resolution = resolve_transcriber_backend()
        self.transcriber_backend = self.transcriber_resolution.implementation

        self.post_resolution = resolve_post_hook()
        self.post_hook = self.post_resolution.implementation

        _log.info(
            "后端解析：capture=%s（在场=%s）/ transcriber=%s（在场=%s）/ post=%s（在场=%s）",
            self.capture_resolution.target or self.capture_resolution.selector,
            self.capture_resolution.has_implementation,
            self.transcriber_resolution.target or self.transcriber_resolution.selector,
            self.transcriber_resolution.has_implementation,
            self.post_resolution.target or self.post_resolution.selector,
            self.post_resolution.has_implementation,
        )

    def _config_view(self) -> ReadOnlyConfig:
        values = self.config.as_dict() if self.config is not None else config_module.as_dict()
        return ReadOnlyConfig(values)

    def bind_capture(self, backend: Any) -> Callable[[Any], None]:
        """给采集后端包一层线程安全的事实出口（`loop.call_soon_threadsafe`）。

        后端**不必知道事件循环在哪**，也不许自己碰状态 —— 它拿到的永远是包好的回调
        （`bind()` 里交给后端的正是这个 `sink`；本方法因此不读 `backend`，形参只为调用点可读）。
        """
        del backend  # 只用于调用点的可读性：出口属于 runtime，不属于某个后端实例

        def sink(fact: Any) -> None:
            """可从**任何线程**调的事实出口。"""
            loop = self._loop
            if loop is None or not loop.is_running():
                # 事件循环还没起来 / 已经关掉：同步处理（后端线程不要因为这里抛错而死）
                if loop is None:
                    self.apply_fact(fact)
                return
            try:
                loop.call_soon_threadsafe(self.apply_fact, fact)
            except RuntimeError:  # pragma: no cover - 循环正在关闭
                _log.debug("事件循环已在关闭，丢弃一条事实：%r", fact)

        return sink

    def bind_transcriber(self, backend: Any) -> Callable[[Any], None]:
        """转写后端的 `line_sink` 同样只经 `call_soon_threadsafe` 进循环。"""
        del backend  # 同 `bind_capture()`：出口属于 runtime

        def sink(line: Any) -> None:
            loop = self._loop
            if loop is None or not loop.is_running():
                if loop is None:
                    self._apply_transcript_line(line)
                return
            try:
                loop.call_soon_threadsafe(self._apply_transcript_line, line)
            except RuntimeError:  # pragma: no cover - 循环正在关闭
                _log.debug("事件循环已在关闭，丢弃一行转写：%r", line)

        return sink

    def _probe_devices(self) -> None:
        """调 capture 后端的 `probe()`，**在它之前先 `bind()`**，结果直取进初始快照。"""
        backend = self.capture_backend
        view = self._config_view()
        try:
            sink = self.bind_capture(backend)
            backend.bind(sink, config=view)
        except Exception:
            _log.exception("采集后端 bind() 失败（如实记账，不静默）")

        # 帧间隔的声明位 = 实例属性 `frame_interval`（读不到或非正数 → 回退并记 warning）
        raw_interval = getattr(backend, "frame_interval", None)
        if isinstance(raw_interval, bool) or not isinstance(raw_interval, (int, float)):
            self._warn_frame_interval(raw_interval)
        elif not math.isfinite(float(raw_interval)) or float(raw_interval) <= 0:
            self._warn_frame_interval(raw_interval)
        else:
            self.frame_interval = float(raw_interval)

        try:
            if self.transcriber_backend is not None:
                self.transcriber_backend.bind(self.bind_transcriber(self.transcriber_backend),
                                              config=view)
        except Exception:
            _log.exception("转写后端 bind() 失败（如实记账、不阻塞录音）")

        try:
            probes = backend.probe()
        except Exception:
            _log.exception("采集后端 probe() 失败（如实记账，不静默）")
            probes = []
        self.probes = list(probes or [])

        by_source: dict[str, Any] = {}
        for probe in self.probes:
            source = getattr(probe, "source", None)
            if source in SOURCE_IDS:
                by_source[source] = probe
            else:
                _log.warning("设备探测：不认识的一路 %r，忽略", source)

        for item in self.snapshot.sources:
            probe = by_source.get(item.id)
            if probe is None:
                item.health = "unprobed"
                item.device_name = None
                _log.info("设备探测：%s 没有探测结果，按 unprobed 处理", item.id)
                continue
            status = getattr(probe, "status", None)
            if status not in PROBE_STATUS:
                # 后端违约（返回第四个探针状态这类）：记 warning、该路落 probe_failed、
                # top_state 不动、服务照常起（采集侧的异常不结束会话）
                _log.warning(
                    "设备探测：%s 返回了取值域外的状态 %r，按 probe_failed 处理", item.id, status
                )
            item.health = validate_health(status)
            if item.health == "probe_failed" and status in PROBE_STATUS:
                _log.warning(
                    "设备探测：%s 能打开但没信号（status=%s）", item.id, status
                )
            item.device_name = getattr(probe, "device_name", None)
            _log.info(
                "设备探测：%s -> status=%s device=%s（%s）",
                item.id, status, item.device_name, getattr(probe, "detail", ""),
            )

    def _warn_frame_interval(self, raw: Any) -> None:
        from meeting.state import DEFAULT_FRAME_INTERVAL_SECONDS

        self.frame_interval = DEFAULT_FRAME_INTERVAL_SECONDS
        _log.warning(
            "采集后端没有声明可用的 frame_interval（读到 %r），按 %ss 算帧时钟窗口"
            "（只影响窗口取值，不阻塞 start、不改 top_state）",
            raw, DEFAULT_FRAME_INTERVAL_SECONDS,
        )

    def _scan_unfinished(self) -> None:
        """扫未收尾场次：命中即在空闲态挂 `notice.restart_unfinished` 并改写现场。

        **绝不自动续录、绝不把状态恢复成「录音中」**；改写只动那一份 `session.json`。
        """
        session_dir, warning = self.store.scan_unfinished()
        if warning:
            _log.warning(warning)
        if session_dir is None:
            return
        record, _error = self.store.load(session_dir)
        if record is None:  # pragma: no cover - scan_unfinished 已保证读得出来
            return
        log_path = self.log_path
        self.store.mark_unfinished(session_dir, record)
        self._pending_restart_session = session_dir
        self.snapshot.notice = Notice(
            kind="restart_unfinished",
            text=restart_unfinished_text(session_dir, log_path),
            session_dir=session_dir,
            log_path=log_path,
        )
        _log.warning(
            "启动扫描改写：已把 %s 的 %s 改写成 state=draining + interrupted=true"
            "（原 steps 原样保留、不伪造终态），这一场拦住新场次",
            session_dir, SESSION_FILE_NAME,
        )

    # -- 状态 JSON 产出（唯一产出者） -------------------------------------

    def _live_elapsed(self) -> None:
        """把本场已录秒数刷成当下值：录音 / 告警态走秒，进 `ended` 之后冻结（不再动）。"""
        session = self.snapshot.session
        if session is None or session.frozen:
            return
        session.elapsed_seconds = max(0.0, time.monotonic() - session.started_monotonic)

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
            out_root=self.out_root,
        )

    def commit(self) -> int:
        """**唯一的状态提交 + 广播点**：`revision +1` 与一次全量 `state` 广播都在这里。

        别处（迁移函数、动作处理器、收尾三函数、hub）一律不自己改 `revision`、
        不自己发 `state`。

        `alerts.*.muted` 在这里按**当下**重算一遍（判定权收在 `AlertJudge.refresh_muted()`
        一处）：本次提交要广播出去的那份快照，说的就是此刻会不会出声 —— 会话级静音复位、
        抑制窗口到点、下一场 `start` 复位之后，同一条告警算出来就回到 `False`。
        """
        self.snapshot.alerts.refresh_muted(judge=self.judge)
        self.snapshot.revision += 1
        self.hub.broadcast_state(self._snapshot_json())
        return int(self.snapshot.revision)

    # -- 心跳 -------------------------------------------------------------

    def recording_alive(self) -> bool | None:
        """`recording_alive`：`recording` / `alert` 态按帧时钟算；其余态为 `null`。"""
        if self.snapshot.top_state not in ("recording", "alert"):
            return None
        if self.capture_backend is None:
            return None
        if self.last_frame_ts is None:
            return False
        from meeting.state import frame_clock_window

        window = frame_clock_window(self.frame_interval)
        return (time.monotonic() - self.last_frame_ts) <= window

    def seconds_since_last_tick(self) -> float | None:
        """距最近一帧事实的秒数；**一帧都没收到过就是 `null`**。"""
        if self.last_frame_ts is None:
            return None
        return round(max(0.0, time.monotonic() - self.last_frame_ts), 3)

    def heartbeat_dict(self) -> dict[str, Any]:
        """`GET /api/heartbeat` 的应答（端口探测 + 界面判迟到 / 失联）。"""
        from meeting.service.app import SERVICE_ID

        return {
            "service": SERVICE_ID,
            "server_time": now_iso(),
            "revision": int(self.snapshot.revision),
            "top_state": self.snapshot.top_state,
            "recording_alive": self.recording_alive(),
            "seconds_since_last_tick": self.seconds_since_last_tick(),
        }

    # -- 事实分派 ---------------------------------------------------------

    def apply_fact(self, fact: Any) -> None:
        """**事实入循环后的唯一处理点**（后端线程经 `call_soon_threadsafe` 送到这里）。"""
        try:
            if isinstance(fact, LevelFrame):
                self._apply_level_frame(fact)
            elif isinstance(fact, SourceLost):
                self._apply_source_lost(fact)
            elif isinstance(fact, SourceFacts):
                self._apply_source_facts(fact)
            elif isinstance(fact, TranscriptLine):
                self._apply_transcript_line(fact)
            else:
                _log.warning("收到不认识的事实 %r（忽略，不影响状态机）", type(fact).__name__)
        except InvalidTransitionError as exc:
            # 状态机拒绝的事实：不涨 revision、不写盘、不广播（记一条 warning）
            _log.warning("事实被状态机拒绝（不影响 revision）：%s", exc)

    def _ensure_ticker(self) -> None:
        if self._ticker is not None or self._loop is None:
            return
        self._ticker = self._loop.create_task(self._levels_loop())

    async def _levels_loop(self) -> None:
        """`levels` 抽帧：**4 次/秒**（降采样落在 runtime 侧，hub 只搬运与背压）。"""
        try:
            while True:
                await asyncio.sleep(LEVELS_INTERVAL_SECONDS)
                if self.snapshot.top_state in ("recording", "alert"):
                    self.hub.broadcast_levels(
                        {
                            "revision": int(self.snapshot.revision),
                            "frames": sample_levels(self.snapshot),
                        }
                    )
        except asyncio.CancelledError:  # pragma: no cover - 关停时取消
            raise

    def _apply_level_frame(self, frame: LevelFrame) -> None:
        """电平帧：更新瞬时值 / 峰值保持 / 包络，刷新帧时钟，交给 `alerts.py` 判定。"""
        if self.snapshot.top_state not in ("recording", "alert"):
            return
        self._ensure_ticker()
        item = self.snapshot.source(frame.source)
        item.push_level(float(frame.ts), float(frame.dbfs), float(frame.peak))
        self.last_frame_ts = time.monotonic()
        self.last_fact_at = self.last_frame_ts

        changed = False
        judgement = self.judge.on_level_frame(frame.source, float(frame.ts), float(frame.dbfs))
        if judgement.alert is not None:
            self._enter_alert(frame.source, judgement.alert, float(frame.ts))
            changed = True
        if judgement.recovered and self.snapshot.source(frame.source).health == "lost":
            # 该路连续有信号达 `alert.recover_seconds`
            if recover_source(self.snapshot, source=frame.source):
                _log.info("状态迁移：alert -> recording（%s 连续有信号达恢复阈值）", frame.source)
            else:
                _log.info(
                    "一路恢复：%s 回到 restored（还有别的路在告警，top_state 保持 alert）",
                    frame.source,
                )
            self.judge.mark_recovered(frame.source)
            changed = True
        if changed:
            self.commit()

    def _enter_alert(self, source: str, alert: Alert, ts: float) -> bool:
        """把一条致命级判定落到快照上（迁移 / 更新都由 `begin_alert()` 一处做）。

        返回 `True` 表示**顶层真的发生了迁移**（调用方随后调一次 `commit()`）。
        """
        item = self.snapshot.source(source)
        if item.health == "lost" and self.snapshot.top_state == "alert":
            # 同一路还在告警里：只刷新那一条（同时只留一条致命级）
            self.snapshot.alerts.critical = alert
            return False
        migrated = begin_alert(self.snapshot, source=source, alert=alert, lost_since=now_iso())
        _log.warning(
            "告警起：%s（%s），阈值 silence_seconds=%ss / silence_dbfs=%s dBFS，"
            "muted=%s（出声开关 alarm.sound=%s）",
            alert.message, source, self.judge.thresholds.silence_seconds,
            self.judge.thresholds.silence_dbfs, alert.muted, self._value("alarm.sound"),
        )
        if migrated:
            _log.info("状态迁移：recording -> alert（%s）", source)
        return True

    def _apply_source_lost(self, fact: SourceLost) -> None:
        """后端报「这一路的流中断了」：立刻起致命级（**服务绝不因此结束会话**）。"""
        if self.snapshot.top_state not in ("recording", "alert"):
            return
        self._ensure_ticker()
        now = time.monotonic()
        alert = self.judge.on_source_lost(fact.source, now, fact.reason)
        self._enter_alert(fact.source, alert, now)
        self.commit()

    def _apply_source_facts(self, fact: SourceFacts) -> None:
        """流存活事实：**只是事实输入，不刷新帧时钟**（`recording_alive` 由帧时钟算）。"""
        if self.snapshot.top_state not in ("recording", "alert"):
            return
        self._ensure_ticker()
        _log.debug("流存活事实：%s stream_alive=%s", fact.source, fact.stream_alive)

    def _apply_transcript_line(self, line: Any) -> None:
        """一行转写：进 `transcript` 字段并推 `transcript` 通道（**不参与状态迁移**）。"""
        try:
            text = getattr(line, "text", "")
            self.snapshot.transcript.last_line_at = now_iso()
            self.hub.broadcast_transcript(
                {
                    "revision": int(self.snapshot.revision),
                    "line": {
                        "ts": float(getattr(line, "ts", 0.0)),
                        "source": str(getattr(line, "source", "")),
                        "text": text,
                        "kind": str(getattr(line, "kind", "line")),
                    },
                }
            )
        except Exception:
            _log.exception("处理一行转写时抛错（不阻塞录音、不影响状态机）")

    # -- 场次生命周期 -----------------------------------------------------

    def _session_state_on_disk(self) -> str:
        """盘上这一版该写哪个 `state`（映射只在 `session.state_for()` 一处）。"""
        failed = (
            self.snapshot.ended is not None
            and any(step.status == "failed" for step in self.snapshot.ended.steps)
        )
        return state_for(
            top_state=self.snapshot.top_state,
            ended_phase=None if self.snapshot.ended is None else self.snapshot.ended.phase,
            has_failed_step=failed,
        )

    def _session_record(
        self,
        session: SessionInfo,
        *,
        state: str,
        ended_at: str | None,
        interrupted: bool,
        steps: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """按当前快照组装一份 `session.json` 的内容（盘上那一版的唯一组装点）。"""
        devices = {item.id: item.device_name for item in self.snapshot.sources}
        return build_session_record(
            session_id=session.id,
            session_dir=session.dir,
            mode=session.mode,
            state=state,
            created_at=session.started_at,
            started_at=session.started_at,
            devices=devices,
            steps=steps,
            ended_at=ended_at,
            interrupted=interrupted,
        )

    def _write_session(self, *, ended_at: str | None = None, interrupted: bool | None = None) -> str:
        """把当前快照写成盘上那一版 `session.json`（**原子的那一处**）。

        `interrupted=None`（普通调用点）时沿用盘上已经写着的那一版 —— 于是第三个 Phase 的
        强制打断路径先落盘的 `interrupted: true` **不会被取消后的记账抹回否**
        （phase2 §3.5.6 的「三条不覆盖约束」之一）。
        """
        session = self.snapshot.session
        if session is None:  # pragma: no cover - 调用点都有会话
            raise InvalidTransitionError("没有会话可写盘", top_state=self.snapshot.top_state)
        if interrupted is None:
            existing = self.store.read_session_json(session.dir) or {}
            interrupted = bool(existing.get("interrupted", False))
        steps = steps_from_ended(self.snapshot.ended) if self.snapshot.ended is not None else None
        record = self._session_record(
            session,
            state=self._session_state_on_disk(),
            ended_at=ended_at,
            interrupted=interrupted,
            steps=steps,
        )
        return self.store.write_session_json(session.dir, record)

    def start_session(self, mode: str | None = None) -> SessionInfo:
        """`start`：判态 → 校验 `mode` → 判后端可用性 → 建目录写文件 → 进 `recording`。"""
        mode = DEFAULT_SESSION_MODE if mode is None else mode

        # ① 判态三档（含「重启现场拦住新场次」那一档）
        if self.snapshot.top_state in ("recording", "alert"):
            raise self._already_recording_error()
        if self.snapshot.top_state == "ended" and self._is_draining():
            raise self._draining_error(self.snapshot.session)
        if self.snapshot.top_state == "idle" and self._pending_restart_session is not None:
            raise self._draining_error_for_dir(self._pending_restart_session)

        # ② 校验 args（**在建目录之前**）
        if mode not in SESSION_MODES:
            raise ActionError(
                "invalid_setting",
                f"mode 取值非法：{mode!r}（只能是 {' / '.join(SESSION_MODES)}）",
                status=400,
                detail={"key": "mode", "value": mode, "legal": list(SESSION_MODES)},
            )

        # ③ 判后端可用性
        backend = self.capture_backend
        if backend is None:  # pragma: no cover - registry 保证有替身
            raise ActionError(
                "capture_unavailable", "采集后端未接入：当前没有可用的采集实现", status=503
            )

        # ④ 建目录 → **先把第一版 session.json 落稳** → 挂场次 log → 起采集 → 进 recording
        session_id, session_dir = self.store.create_session_dir()
        session = SessionInfo(
            id=session_id,
            dir=session_dir,
            started_at=now_iso(),
            mode=mode,
            started_monotonic=time.monotonic(),
            elapsed_seconds=0.0,
            frozen=False,
        )
        devices = {item.id: item.device_name for item in self.snapshot.sources}
        try:
            # 第一版盘上内容先写稳（此时快照还没动：写不进去就什么都没发生过）
            self.store.write_session_json(
                session.dir,
                self._session_record(
                    session,
                    state="recording",
                    ended_at=None,
                    interrupted=False,
                    steps=[
                        {"id": step_id, "status": "waiting", "detail": "", "error": None}
                        for step_id in DRAIN_STEP_IDS
                    ],
                ),
            )
        except Exception as exc:
            # **不进入 recording**：清掉刚建的场次目录并留痕（R16 与重启自洽都依赖这份文件）
            self.store.remove_session_dir(session_dir)
            _log.error(
                "场次开始失败：%s 的 %s 写不进去（%s: %s）；场次目录已清理：%s",
                session_id, SESSION_FILE_NAME, type(exc).__name__, exc, session_dir,
            )
            raise ActionError(
                "capture_unavailable",
                f"场次目录建好但 {SESSION_FILE_NAME} 写不进去，已清理并放弃这一场",
                status=503,
                detail={"session_id": session_id, "dir": session_dir,
                        "error": f"{type(exc).__name__}: {exc}"},
            ) from exc

        # 盘上第一版已稳，这才把状态推进到 recording
        self.judge.reset_for_new_session()
        start_recording(self.snapshot, session=session, mode=mode, devices=devices)
        _log.info("场次开始：%s（目录 %s，mode=%s）", session_id, session_dir, mode)

        try:
            # 采集起不来 → 如实回 `capture_unavailable`(503)，**刚建的场次目录清掉**
            # （态不对 / 后端不可用时盘上不留任何目录，phase2 §3.6.2 的硬要求）
            backend.start(mode, session_dir)
        except Exception as exc:
            message = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
            _log.error("采集起不来：%s（场次 %s）", message, session_id)
            self.store.remove_session_dir(session_dir)
            _log.warning("场次目录已清理：%s（采集起不来，盘上不留这一场）", session_dir)
            self._abort_session(session)
            raise ActionError(
                "capture_unavailable",
                str(message),
                status=503,
                detail={"session_id": session_id, "dir": session_dir, "reason": str(message)},
            ) from exc

        try:
            logging_setup.attach_session_log(session_dir)
        except OSError as exc:
            _log.error(
                "挂场次 log 失败（%s: %s）；录音照常继续，这一场没有场次 log", type(exc).__name__, exc
            )

        if self.transcriber_backend is not None:
            try:
                self.transcriber_backend.start(mode, session_dir)
            except Exception:
                # 转写起步失败**不阻塞录音**（转写坏了可以重跑）
                _log.exception("转写后端起步失败（不阻塞录音）")

        self._pending_restart_session = None
        self.last_frame_ts = None
        self.commit()
        return session

    def _abort_session(self, session: SessionInfo) -> None:
        """采集起不来：把这一场撤回空闲态（场次目录已由调用方清掉）。"""
        try:
            logging_setup.detach_session_log()
        except OSError:  # pragma: no cover - 摘不掉也不阻塞
            pass
        self.snapshot.session = None
        self.snapshot.ended = None
        self.snapshot.top_state = "idle"
        for item in self.snapshot.sources:
            item.health = "unprobed"
            item.envelope = []
        self.commit()
        _log.info("场次撤回空闲态（%s）", session.id)

    def stop_session(self, reason: str = "user") -> asyncio.Task | None:
        """`stop`：冻结时长 → 进 `ended·draining` → 起收尾协程（句柄存 `_drain_task`）。"""
        if reason not in ("user", "ctrl_c"):
            raise ActionError(
                "not_recording",
                f"stop_session 的 reason 取值非法：{reason!r}（只能是 user / ctrl_c）",
                status=409,
            )
        if self.snapshot.top_state not in ("recording", "alert"):
            raise ActionError("not_recording", "现在没有在录的场次，结束动作无效", status=409)

        session = self.snapshot.session
        if session is not None:
            session.elapsed_seconds = max(0.0, time.monotonic() - session.started_monotonic)
        begin_drain(self.snapshot, reason=reason)
        _log.info(
            "场次结束：%s（目录 %s，reason=%s，本场 %ss）",
            session.id if session else "?", session.dir if session else "?", reason,
            round(session.elapsed_seconds, 3) if session else "?",
        )
        self.commit()
        self._drain_task = asyncio.get_running_loop().create_task(self.drain())
        return self._drain_task

    def mute_session(self) -> None:
        """会话级静音：**只影响出声**，不改 `alarm.sound` 持久化值。

        `alerts.*.muted` 不在这里手写 `True`：它由 `commit()` 按当下重算（判定权收在
        `AlertJudge.refresh_muted()` 一处），因此本动作只翻开关，快照随之变 `True`。
        """
        if self.snapshot.top_state not in ("recording", "alert"):
            raise ActionError("not_recording", "现在没有在录的场次，静音动作无效", status=409)
        self.judge.mute_session = True
        _log.info("本场已静音（会话级；下一场 start 自动复位，持久化开关 alarm.sound 未改）")
        self.commit()

    # -- 动作错误（拒绝路径的 detail 三件套） ------------------------------

    def _is_draining(self) -> bool:
        return self.snapshot.ended is None or self.snapshot.ended.phase == "draining"

    def _failed_steps_detail(self) -> list[dict[str, Any]]:
        if self.snapshot.ended is None:
            return []
        return [
            {"id": step.id, "error": step.error}
            for step in self.snapshot.ended.steps
            if step.status == "failed"
        ]

    def _already_recording_error(self) -> ActionError:
        session = self.snapshot.session
        elapsed = 0.0
        if session is not None:
            elapsed = (
                session.elapsed_seconds
                if session.frozen
                else max(0.0, time.monotonic() - session.started_monotonic)
            )
        return ActionError(
            "already_recording",
            "已经有一场在录",
            status=409,
            detail={
                "session_id": None if session is None else session.id,
                "dir": None if session is None else session.dir,
                "elapsed_seconds": round(float(elapsed), 3),
            },
        )

    def _draining_error(self, session: SessionInfo | None) -> ActionError:
        return ActionError(
            "draining",
            "上一场还在收尾（或收尾失败后停留），这一场不能开",
            status=409,
            detail={
                "session_id": None if session is None else session.id,
                "dir": None if session is None else session.dir,
                "failed_steps": self._failed_steps_detail(),
                "log_path": self.log_path,
            },
        )

    def _draining_error_for_dir(self, session_dir: str) -> ActionError:
        """重启现场的拒绝路径：场次目录来自扫描结果（快照里 `session` 仍是 `null`）。"""
        record = self.store.read_session_json(session_dir) or {}
        failed = [
            {"id": step.get("id"), "error": step.get("error")}
            for step in (record.get("drain", {}) or {}).get("steps", [])
            if step.get("status") == "failed"
        ]
        return ActionError(
            "draining",
            "上一场没走完（服务重启过），这一场不能开",
            status=409,
            detail={
                "session_id": record.get("session_id") or session_dir.rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
                "dir": session_dir,
                "failed_steps": failed,
                "log_path": self.log_path,
            },
        )

    # -- 设置更新 ---------------------------------------------------------

    def settings_update(self, args: dict[str, Any]) -> dict[str, Any]:
        """写 `config.json`（原子写）；**可热读的键立刻生效，其余键写盘成功但要重启**。

        「写盘成功」与「快照立刻反映」是两件事：归 `pending_restart` 的那些键本次只落盘，
        运行期仍取启动时的生效值，快照 `settings` 里读到的还是旧值（`alarm.sound` 的可观察
        判据是主 log 那一行「本场出声开关仍取启动值」，见 §7.1 高危点 7）；只有当本次改动
        里有热读五键（`asr.*`）时，才在下面重取一次 `_settings_view()` 并提交。
        """
        try:
            changes = config_module.validate_setting_changes(args, current=self.config)
        except config_module.ConfigValueError as exc:
            raise ActionError(
                "invalid_setting",
                str(exc),
                status=400,
                detail={"key": exc.key, "value": exc.value, "legal": exc.legal},
            ) from exc

        try:
            config_module.write_config_values(changes)
        except OSError as exc:
            raise ActionError(
                "persist_failed",
                f"配置写盘失败（{type(exc).__name__}: {exc}）；这次改动一处都没生效",
                status=500,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        except config_module.ConfigCorruptError as exc:
            raise ActionError(
                "persist_failed",
                f"现有 config.json 读不出来，没法安全叠加：{exc}",
                status=500,
                detail={"error": str(exc)},
            ) from exc

        applied, pending = config_module.split_setting_changes(changes)
        # 热读五键立刻生效：生效值在内存里换掉（下一段读到的就是新值）
        if applied and self.config is not None:
            values = dict(self.config.applied)
            values.update({key: changes[key] for key in applied})
            self.config = config_module.Config(
                applied=values, sources=dict(self.config.sources), path=self.config.path
            )
            config_module.set_current(self.config)
            self.snapshot.settings = self._settings_view()
            self.commit()

        for key in changes:
            env_name = config_module.env_name_for(key)
            if env_name and env_name in os.environ:
                _log.warning(
                    "settings_update：%s 被环境变量 %s 压着（写盘值 %r），运行期仍由环境变量说话",
                    key, env_name, changes[key],
                )
        if "alarm.sound" in changes:
            _log.warning(
                "alarm.sound 归 pending_restart：本场出声开关仍取启动值 %r（改了要重启才生效）",
                self._value("alarm.sound"),
            )
        return {"applied": applied, "pending_restart": pending}

    def dev_inject(self, args: dict[str, Any]) -> None:
        """`dev_inject`：只在 dev 采集替身在场时可用，且要有正在进行的场次。"""
        if not self.capture_capabilities.get("dev_inject"):
            raise ActionError(
                "action_unavailable",
                "dev_inject 只在 dev 采集替身在场时可用（当前后端不支持）",
                status=404,
            )
        if self.snapshot.top_state not in ("recording", "alert"):
            raise ActionError("not_recording", "没有正在进行的场次，注入动作无对象可挂", status=409)

        source = args.get("source")
        fault = args.get("fault")
        try:
            self.capture_backend.inject_fault(
                source=source,
                fault=fault,
                after_seconds=args.get("after_seconds", 0.0),
                duration_seconds=args.get("duration_seconds"),
            )
        except (TypeError, ValueError) as exc:
            raise ActionError(
                "invalid_setting", str(exc), status=400,
                detail={"source": source, "fault": fault, "error": str(exc)},
            ) from exc

    # -- 收尾编排（三步记账 + 终态判定；预算与失败出路的完整语义归 phase3） --

    async def drain(self) -> str:
        """收尾协程（**普通协程、持有 task 句柄、可被 `cancel()`**）。

        phase2 的编排：`flush` → `transcode` → `log` → `post`；每步进入与落定各写一次盘、
        各提交一次；**四步全部落定之后把前提交给 `finish_drain()`** 判终态。
        被取消时按收尾步骤表落盘（已落定的保持原样、未开跑的维持 `waiting`、
        **不伪造终态**），随后同样走 `finish_drain()` 这一个出口。自适应预算、
        `_drain_wait()` 的超时包装与失败后的两条出路归 phase3。
        """
        try:
            await self._run_drain_steps()
        except asyncio.CancelledError:
            _log.warning("收尾被取消：按收尾步骤表落盘，随后走 finish_drain() 判终态")
            self._finish_drain(cancelled=True)
            raise
        finally:
            self._drain_task = None
        return self.snapshot.ended.phase if self.snapshot.ended else "draining"

    async def _run_drain_steps(self) -> None:
        await self._drain_flush()
        await self._drain_transcode()
        await self._drain_log()
        await self._drain_post()
        self._finish_drain()
        # 收尾走完之后的最后一趟落盘：盘上那一版与终态一致
        # （含 `state`、`drain.steps` 与结束时间）
        self._write_session(ended_at=now_iso())
        _log.info("收尾终态已落盘：ended.phase=%s", self.snapshot.ended.phase if self.snapshot.ended else "?")

    async def _drain_flush(self) -> None:
        """第 1 步 `flush`：停采集、落盘（后端 `finalize()`）；采集替身记 `skipped`。"""
        step_id = "flush"
        self._begin_step(step_id, "停采集并把这一场的归档落盘（后端 finalize()）")
        resolution = self.capture_resolution
        if resolution is None or resolution.skipped() or not self.capture_capabilities.get("finalize"):
            self._skip_step(
                step_id,
                "这一场没有真实归档要落（采集替身不写真实音频；finalize 归 dca4539b）",
                resolution,
            )
            return
        try:
            await self._call_step(step_id, self.capture_backend.finalize)
        except Exception as exc:
            self._fail_step(step_id, exc, resolution, "采集归档落盘失败")
            return
        self._done_step(step_id, "归档已落盘（finalize() 返回）", resolution)

    async def _drain_transcode(self) -> None:
        """第 2 步 `transcode`：收尾转换；缺席记 `skipped`、抛错记 `failed`。"""
        step_id = "transcode"
        self._begin_step(step_id, "收尾转换（后端 transcode()）")
        resolution = self.transcriber_resolution
        if resolution is None or resolution.skipped() or not self.capture_capabilities.get("transcode"):
            self._skip_step(
                step_id,
                "这一场没有可转码的产物（转写后端缺席 / 采集替身不写真实音频；转码归 dca4539b）",
                resolution,
            )
            return
        try:
            await self._call_step(step_id, self.capture_backend.transcode)
        except Exception as exc:
            self._fail_step(step_id, exc, resolution, "收尾转换失败")
            return
        self._done_step(step_id, "收尾转换完成（transcode() 返回）", resolution)

    async def _drain_log(self) -> None:
        """第 3 步 `log`：写 `session.json` 的结束标记与**服务级主日志**的结束标记。

        **只记账**：不写 log 内容明细、不记耗时、不写 `budget` / `backlog`；
        **场次日志文件的 flush、结束标记与摘除整体归 phase3**（本 Phase 只在 `start` 时挂上）。
        """
        step_id = "log"
        self._begin_step(step_id, "写这一场的结束标记（会话文件 + 服务级主日志）")
        try:
            self._write_session(ended_at=now_iso())
            _log.info(
                "场次结束标记已写入 session.json（会话文件这一半）；场次日志文件的 flush / "
                "结束标记 / 摘除归 phase3"
            )
        except Exception as exc:
            self._fail_step(step_id, exc, None, "写这一场的结束标记失败")
            return
        self._settle_step(step_id, "done", "结束标记已写入（会话文件 + 服务级主日志）", None, None)

    async def _drain_post(self) -> None:
        """第 4 步 `post`：拉起会后流程（**启动即算完成，不等它跑完**）；钩子缺席记 `skipped`。"""
        step_id = "post"
        self._begin_step(step_id, "拉起会后流程（启动即算完成，不等它跑完）")
        resolution = self.post_resolution
        if resolution is None or resolution.skipped():
            self._skip_step(step_id, "会后流程还没接入（PostProcessHook 缺席，归 65dd3e4c）", resolution)
            return
        session_dir = self.snapshot.session.dir if self.snapshot.session else None
        try:
            await self._call_step(step_id, lambda: self.post_hook.launch(session_dir))
        except Exception as exc:
            self._fail_step(step_id, exc, resolution, "会后流程启动失败")
            return
        self._done_step(step_id, "会后流程已启动（不等它跑完）", resolution)

    async def _call_step(self, step_id: str, call: Callable[[], Any]) -> None:
        """调一步的实现。

        phase3 把这里换成 `_drain_wait()`（`asyncio.wait_for(<该步调用>, <剩余预算>)`，
        四步共享一条 deadline）。本 Phase 不加超时包装，**也不吞这一步自己抛出的异常**。
        """
        result = call()
        if asyncio.iscoroutine(result):
            await result

    # -- 收尾记账（写盘 + 提交都收在这几个方法里） -------------------------

    def _begin_step(self, step_id: str, detail: str) -> None:
        _log.info("收尾步骤开始：%s（%s）", step_id, detail)
        begin_drain_step(step_id, self.snapshot)
        try:
            self._write_session()
        except Exception:
            _log.exception("收尾写盘失败（步骤 %s 进入）：如实记账，不静默", step_id)
        self.commit()

    def _settle_step(
        self,
        step_id: str,
        status: str,
        detail: str,
        error: str | None,
        resolution: BackendResolution | None,
        *,
        cancelled: bool = False,
    ) -> None:
        _log.info("收尾步骤结束：%s -> %s（%s）", step_id, status, detail)
        finish_drain_step(
            step_id,
            status,
            detail,
            error,
            snapshot=self.snapshot,
            frozen_ended=cancelled,
            frozen_interrupted=cancelled,
        )
        try:
            self._write_session(interrupted=True if cancelled else None)
        except Exception:
            _log.exception("收尾写盘失败（步骤 %s 落定）：如实记账，不静默", step_id)
        self.commit()
        if resolution is not None:
            resolution.mark_run(status != "failed")

    def _done_step(self, step_id: str, detail: str, resolution: BackendResolution | None) -> None:
        self._settle_step(step_id, "done", detail, None, resolution)

    def _skip_step(self, step_id: str, detail: str, resolution: BackendResolution | None) -> None:
        # 缺席（构建期没有真实实现）→ skipped，不阻塞 done，留一行 warning
        _log.warning("收尾步骤跳过：%s —— %s", step_id, detail)
        self._settle_step(step_id, "skipped", detail, None, resolution)

    def _fail_step(
        self,
        step_id: str,
        exc: Exception,
        resolution: BackendResolution | None,
        what: str,
    ) -> None:
        # 有实现但抛错 → failed（error = 原因 + 可复制的命令 + log 路径）
        session_dir = self.snapshot.session.dir if self.snapshot.session else "<场次目录>"
        error = (
            f"{what}：{type(exc).__name__}: {exc}"
            f"；可复制命令：meeting post {session_dir}"
            f"；log 路径：{self.log_path}"
        )
        _log.exception("收尾步骤失败：%s（%s）", step_id, what)
        self._settle_step(step_id, "failed", what, error, resolution)

    def _finish_drain(self, *, cancelled: bool = False) -> str:
        """调**收尾的唯一出口** `finish_drain()` 判终态；成功档补 `sources` 回 `unprobed`。"""
        session_dir = self.snapshot.session.dir if self.snapshot.session else None
        final_state = finish_drain(self.snapshot)
        ended = self.snapshot.ended
        if final_state == "done":
            _log.info("收尾完成：四步全部落定且没有失败步（ended.phase=done，top_state 停在 ended）")
        else:
            if cancelled:
                _log.warning(
                    "收尾被取消且四步未全部落定：ended.phase 保持 draining"
                    "、top_state 停在 ended、session 保留（不判 done）"
                )
            else:
                failed = [step.id for step in (ended.steps if ended else []) if step.status == "failed"]
                _log.error(
                    "收尾有失败步 %s：会话停在 draining，服务不自杀；"
                    "出路是 `meeting post %s` 或看清现场后重启服务",
                    failed, session_dir,
                )
        return final_state


__all__ = [
    "ACTION_CODES",
    "ACTION_NAMES",
    "DEFAULT_SESSION_MODE",
    "DRAIN_BUDGET_MAX_SECONDS",
    "DRAIN_BUDGET_MIN_SECONDS",
    "LEVELS_INTERVAL_SECONDS",
    "SESSION_MODES",
    "ActionError",
    "ServiceRuntime",
    "compute_drain_budget",
]
