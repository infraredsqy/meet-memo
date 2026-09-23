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
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
from concurrent.futures.thread import _worker as _threadpool_worker
from typing import Any, Callable

from meeting import __version__, config as config_module, logging_setup
from meeting.alerts import AlertJudge, AlertThresholds
from meeting.asr import pipeline as asr_pipeline
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


def _interrupted_message() -> str:
    """强制打断那一档的 log 前缀：**冻结常量原样 + 真实按下次数**。

    §3.5 的行为表把「第一次按下、当时已经停在 `ended·draining`」也划进「等同第二次 `Ctrl+C`」
    这一档，于是常量 `DRAIN_INTERRUPTED_MESSAGE` 末尾那句「用户第二次 Ctrl+C」在用户只按了
    一次时与事实对不上。常量**逐字冻结、不改**；这一档的两行 log 都在它后面补上真实次数
    （取 `meeting/service/app.py` 的按下次数计数器），记录因此不失真。
    """
    from meeting.service import app as service_app

    press = service_app.shutdown_press_count()
    detail = (
        f"本次是第 {press} 次按下"
        if press
        else "本次没有经过信号处理函数（按下次数为 0）"
    )
    return f"{service_app.DRAIN_INTERRUPTED_MESSAGE}（{detail}）"

#: `levels` 抽帧节奏：对外一律 **4 次/秒**（0.25 秒一帧，两路合在一帧里发）
LEVELS_INTERVAL_SECONDS = 0.25

#: 转写健康度节拍的间隔（秒）。
#:
#: **取值依据**：这一条管的是「转写挂了这件事多久能被看见」。取值只影响**滞后**，不影响开销 ——
#: 每一拍读的是 `health()` + `backlog_depth()` 两个**纯读**（几个标志位 + 一个队列长度），
#: 读数没变时一个副作用都不发生（不写快照、不提交、不落 log）。
#:
#: 取 0.5 秒的理由：界面里转写健康位（`transcript.health`）与失联横幅本来就按秒级刷新
#: （`heartbeat.interval_seconds` 默认 10 秒、可配 1–60 秒），半秒的滞后看不出差别；
#: 再往上加（1 秒以上）只会让「转写停了」这件事在 `/debug` 与界面上多等一拍，
#: 换不来任何好处。**用例走的就是这条节拍**（不另开接缝），因此整文件耗时也由它限住。
TRANSCRIBER_HEALTH_INTERVAL_SECONDS = 0.5

# ---------------------------------------------------------------------------
# 收尾预算（phase3 §3.3；名字由 phase2 冻结，取值与实现归本 Phase）
#
# 公式原样落地：`budget = 30 + backlog × (segment_timeout + 5)`，下限 30 秒、封顶 300 秒。
# 三个数**硬编码、不开配置键**（方案 §5.3 没给它们开键）；公式里的 `+5` 是
# `DRAIN_BUDGET_SEGMENT_OVERHEAD_SECONDS`。V1 那个写死的 10 秒收尾等待在代码里不再存在。
# ---------------------------------------------------------------------------

#: 预算下限（秒）
DRAIN_BUDGET_MIN_SECONDS = 30

#: 预算封顶（秒）
DRAIN_BUDGET_MAX_SECONDS = 300

#: 公式里每段的那一笔额外开销（秒）：`backlog × (segment_timeout + 5)`
DRAIN_BUDGET_SEGMENT_OVERHEAD_SECONDS = 5

#: 转写后端缺席时的 `backlog` 缺省
DRAIN_DEFAULT_BACKLOG = 0

#: 转写后端缺席时的 `segment_timeout` 缺省（秒）
DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS = 20.0

#: `log` 步的 flush 与摘除**至少**拿到的预算（秒）。
#:
#: 共享预算里的唯一例外：`log` 是四步里唯一在任何构建里都有真实实现、要做 IO 的步骤；
#: 骨架构建下 `flush` / `transcode` / `post` 三步都是 `skipped`，一旦末尾那步把 deadline
#: 耗尽，`log` 会落 `failed`，本该「正常收尾」的一档就被拖成收尾失败档（用例会以偶发形式红）。
#: 缺席步与末尾那步**不消耗 deadline**（它们的耗时按实测扣减，不把共享预算一次性吃掉）。
#:
#: **与 `DRAIN_BUDGET_SEGMENT_OVERHEAD_SECONDS` 同值但语义无关，不许合并**：上面那个 `5`
#: 是**每段额外开销**、进的是 `budget = 30 + backlog × (segment_timeout + 5)` 这个公式
#: （跟着积压量被乘出来）；本常量是**共享预算之外**给 `log` 那一步的兜底（不进公式、
#: 不随积压量变化、只在 `max(<剩余预算>, DRAIN_LOG_MIN_BUDGET_SECONDS)` 这一处出现）。
#: 后人「统一」两处时若把它们合成一个名字，公式与兜底会一起静默改掉 —— 两个名字因此
#: 各留各的，取值也各自独立。
DRAIN_LOG_MIN_BUDGET_SECONDS = 5

#: `_drain_wait()` 的轮询/收尾间隔（秒）：等收尾落定时的步进
DRAIN_SETTLE_POLL_SECONDS = 0.05

#: 收尾四步的同步调用池里的工作线程条数（审查问题 m7）。
#:
#: 四步在**编排**上严格串行，而线程占用上会叠：某一步被按剩余预算判超时之后，它的等待被
#: 放弃、已提交的那次同步调用照跑到底，后续步的调用因此需要另有一条线程可用（说明与判据见
#: `_DaemonThreadPoolExecutor` 与 `tests/test_drain.py` 的 ⑤k）。取 4 的来路：四步各有同步
#: 调用，最坏情形是「前面几步被放弃后仍在飞 + 当前步要开跑」，4 条线程把这一幕整个装下，
#: 同时仍是一个小规模、daemon、不登记 `_threads_queues` 的池。
DRAIN_EXECUTOR_MAX_WORKERS = 4

#: 会中总结那一次模型调用专用的工作线程条数（`1987cb20` phase2 的复审裁定）。
#:
#: **为什么它会中总结要另起一台池**（复审 minor 6）：那次模型调用是一条同步的
#: `urllib.request`，跑在事件循环上就把 `/api/state`、三个 SSE 通道、心跳与健康节拍
#: 一起按住（复审实测：假客户端原地 sleep 3 秒，同一刻另一条 `GET /api/state` 等了 2.70 秒；
#: 真端点预算默认 25 秒，按住的就是这么久）。设计章 4.5 对 `summary-running` 那一档写的是
#: 「总结区 `正在总结刚才的 15 分钟…`；**其余照常**」—— 被按住的界面正好是那一档要避免的样子。
#:
#: **为什么是 2 而不是 1**：`summary.deadline_seconds`（默认 25 秒）到点时，
#: `_summary_wait()` 放弃的是**等待**，已经交给工作线程的那次调用会照着它自己拿到的
#: `timeout_seconds` 跑到底（契约与 `_drain_wait()` 同一口径）。弃等之后紧接着按下的第二次
#: 总结因此需要另有一条线程可用，否则它要排在前一次调用的尾巴上、自己那 25 秒预算在队列里
#: 白等。两条线程把「被放弃的那一次仍在飞 + 新按下的这一次要开跑」这一幕整个装下，
#: 同时仍是一个小规模、daemon、不登记 `_threads_queues` 的池。
SUMMARY_EXECUTOR_MAX_WORKERS = 2


class DrainLimitExceeded(TimeoutError):
    """某一步超出这一趟收尾的共享预算。

    **只表达超时这一件事**：步骤自身抛出的异常不由本异常承载（那些异常原样上抛到该步的
    `failed` 记账，见 phase3 §3.1 第 5 条与 §3.0 的边界）。
    """


class _DaemonThreadPoolExecutor(_ThreadPoolExecutor):
    """工作线程是 **daemon** 的线程池（同步实现丢进工作线程的那些调用专用）。

    两处调用点：**收尾四步**（`_drain_wait()`）与**会中总结那一次模型调用**
    （`_summary_wait()`，`1987cb20` phase2 的复审裁定）。两处各起一台池（收尾那台 4 条线程、
    会中总结那台按 `SUMMARY_EXECUTOR_MAX_WORKERS`），工作线程都是本类的 daemon 线程。

    **为什么必须是 daemon**（phase3 §3.5 行为表第二行「**立即中止**」）：第二次 `Ctrl+C`
    的语义是「先落盘（`draining` + `interrupted: true`）、再 `cancel()` 收尾 task、随即请求
    关停」。`cancel()` 中止的是 `_drain_wait()` 里那次 `wait_for` 的**等待**，已经交给工作
    线程的那次同步调用（`launch()` / `finalize()` / 场次 log 的 flush 与摘除）**照跑到底**；
    解释器收尾时等这些工作线程，于是「立即中止」被在飞的同步步拖着走（审查实测：第二次
    `Ctrl+C` 之后进程还要挂 1.5–3.0 秒）。把收尾四步的工作线程换成 daemon，解释器收尾不再
    等它们，「立即中止」的时延因此与在飞步骤的剩余时长**无关**。
    （判据见 `tests/test_shutdown_signal.py` 第 ③ 条：在飞的那一步阻塞
    `SHUTDOWN_BLOCKING_HOOK_SECONDS` = 2.0 秒，第二次按下之后的退出时延按
    `INTERRUPT_EXIT_LATENCY_LIMIT_SECONDS` = 1.0 秒断；修好之后实测 0.17–0.44 秒。）

    **不破坏「先落盘、再中止」的顺序**：`mark_interrupted()` 那一版盘上内容由事件循环
    这一侧的 `cancel()` **之前**写完（`SessionStore.write_session_json()` 的临时文件 +
    `os.replace`）。daemon 线程只可能让「收尾 `log` 步的结束标记」少写一半 —— 那一半不是
    重启自洽的读入口（重启读的是 `session.json`），而 `session.json` 的每一版都是原子写，
    因此**不会留下半截坏文件**（同目录的 `.tmp` 残留可接受，下一次原子写会覆盖它）。

    **daemon 之外还要做的一件事（实测过才写的）**：`concurrent.futures.thread` 在模块
    import 时经 `threading._register_atexit()` 登记了 `_python_exit()`，它在解释器退出时
    把**每一台**登记过的池的工作线程都 `join()` 一遍 —— 这一遍**不看 `daemon` 标记**，
    于是「只把线程设成 daemon」并不够（实测：8 秒的在飞同步步仍然把进程拖满 8 秒，
    改成下面的写法之后同一现场 0.5 秒内收场）。
    本类因此**不把工作线程登记进 `_threads_queues`**：退出时没人给它发哨兵、它是 daemon、
    `threading._shutdown()` 只等非 daemon 线程，进程于是立刻退；而正常路径上的收场由
    `shutdown(wait=False)` 负责（它自己会往队列里放哨兵、不需要那张登记表）。

    **只给收尾四步用**：事件循环的默认执行器一律不动（`asyncio.to_thread()` 那一路照旧），
    于是「收尾之外还有谁在用线程池」这件事不受本类影响。

    **两处调用点各起一台池（`__init__` 的两个入参因此可传）**：收尾四步那台取
    `DRAIN_EXECUTOR_MAX_WORKERS` = 4、线程名前缀 `drain-step`；会中总结那台取
    `SUMMARY_EXECUTOR_MAX_WORKERS` = 2、前缀 `summary-call`。两台**互不占线程**：
    会中总结按下的那几秒里，收尾四步的调用照旧有四条线程可用，反过来也一样。

    **工作线程取 `DRAIN_EXECUTOR_MAX_WORKERS` 条（审查问题 m7 的直接修复）**：四步的调用
    在**编排**上是**严格串行**的（§3.1 的顺序写死），而「被放弃的步」与「后续步」在**线程
    占用**上会叠在一起。某一步被 `wait_for` 按剩余预算判超时之后，它的**等待**被放弃，
    已经提交给工作线程的那一次同步调用却照跑到底（§3.5 明写这一条：`cancel()` 中止的只是
    等待，那次调用仍跑到返回）。线程数只有 1 时，后续步骤的调用只能排在它后面等 —— 收尾的
    **后续步**于是被前一步的剩余时长顶住，`log` 那一步会以「拿到
    `DRAIN_LOG_MIN_BUDGET_SECONDS` 却在队列里等光」的形式落 `failed`，本该「正常收尾」的一档
    被拖成收尾失败档。池扩到能容纳「被放弃的那一步 + 后续步」并行推进的规模之后，一步超时
    不再堵住后续步的**可用性**（判据见 `tests/test_drain.py` 的 ⑤k）；「一趟收尾的等待总量
    以那条 deadline 为界」这条口径一个字不改 —— 它由 `_drain_wait()` 的等待上限保证，
    与线程数无关。
    """

    def __init__(self, *, max_workers: int = DRAIN_EXECUTOR_MAX_WORKERS,
                 thread_name_prefix: str = "drain-step") -> None:
        super().__init__(max_workers=max_workers, thread_name_prefix=thread_name_prefix)

    def _adjust_thread_count(self) -> None:
        """与标准库同一套起线程逻辑，两处不同：线程是 **daemon**、**不登记**进退出时的 join 表。

        线程名与弱引用回调逐行照抄标准库（少了弱引用回调，池被回收时工作线程不会醒）。
        """
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = "%s_%d" % (self._thread_name_prefix or self, num_threads)
            thread = threading.Thread(
                name=thread_name,
                target=_threadpool_worker,
                args=(
                    weakref.ref(self, weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                ),
                daemon=True,
            )
            thread.start()
            self._threads.add(thread)
            # 标准库在这里还会写 `_threads_queues[thread] = self._work_queue`；
            # 本类**故意不写**（见类说明：那一张表是解释器退出时无条件 join 的来源）。


class _AttemptsSeen:
    """把总结客户端包一层，**只记一件事**：`complete()` 被调了几次。

    会中总结的尝试次数由内核 `run_summary()` 逐个 attempt 累加，正常路径上那两格
    （`elapsed_seconds` / `attempts`）随 `SummaryOutcome.detail` 一起给出来。走到
    「墙钟预算到点、等待被放弃」那一档时内核还没返回，它的记账读不到 —— 契约那一档要的
    仍是 `{elapsed_seconds, attempts}`，于是**在交给内核的客户端上**记一份。

    读数为什么可信：内核每一次 attempt 都先经 `complete()` 才可能卡住（`runner.py` 的循环
    第一件事就是这次调用），因此到点时这里记下的就是**已经发出去的那几次**。
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.attempts = 0

    def complete(self, messages: Any, *, timeout_seconds: float) -> str:
        self.attempts += 1
        return self._client.complete(messages, timeout_seconds=timeout_seconds)


def compute_drain_budget(backlog: int, segment_timeout: float) -> float:
    """`budget = 30 + backlog × (segment_timeout + 5)`（下限 30、封顶 300）。

    输入来自转写后端的 `backlog_depth()` / `segment_timeout()`；转写后端缺席时调用方按
    `DRAIN_DEFAULT_BACKLOG` / `DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS` 取缺省
    （phase3 §3.3；这两个缺省值与原公式出处是方案 §5.3 行 250 / §7.2 行 338）。

    **公式本体在 `meeting/asr/pipeline.py`**（积压量是转写域的量，那个模块也用它算
    自己 `stop()` 的收尾预算），这里只做转交：同一趟收尾的两处读数因此永远同源。
    下面三个常量与那边逐字同值，是**跨 Phase 冻结的名字**（phase3 按它们读账目）。
    """
    return asr_pipeline.compute_drain_budget(backlog, segment_timeout)


# ---------------------------------------------------------------------------
# 动作层的错误（`code` 取值只用 `ACTION_CODES` 里已冻结的那九个串，不自造）
# ---------------------------------------------------------------------------

#: 动作失败的 `code` 全集（phase2 §3.6.2；`1987cb20` phase2 补上会中总结那两档）
ACTION_CODES: tuple[str, ...] = (
    "already_recording",
    "draining",
    "capture_unavailable",
    "not_recording",
    "invalid_setting",
    "persist_failed",
    "action_unavailable",
    "transcript_unavailable",
    "summary_failed",
)

#: 动作名清单（phase2 §3.13 + `dca4539b` 的 `reprobe` + `1987cb20` phase2 的 `summarize`）
ACTION_NAMES: tuple[str, ...] = (
    "start",
    "stop",
    "mute_session",
    "settings_update",
    "reprobe",
    "dev_inject",
    "summarize",
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
        self._pending_restart_session: str | None = None

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

        口径（§3.5.5 + §7.1 高危点 7）：这里读的是**运行期生效值**。归 `pending_restart` 的那些键
        （两个设备键位、`web.*` / `log.*` / `alert.*` 与三个后端选择键）由 `settings_update`
        写盘成功之后，快照里的 `settings` **不会**立刻反映写进去的新值，要重启才可见；
        热读键才在写盘后立刻换成新值（那一处在 `settings_update()` 里按 `applied` 重取一次
        本函数）—— `asr.*` 五个之外，`alarm.sound` 也走这一条，于是勾出声开关当场就改
        运行期行为（`settings.alarm_sound` 与界面响不响一起变，见 `docs/contract.md` 第 4.6 章）。

        `settings.devices` 的两个子键逐一取 `devices.mic` / `devices.loopback` 的生效值
        （`dca4539b` 定名；没配时为 `null`，与配置层 `NULLABLE_KEYS` 的空值形状一致）。
        """
        return SettingsView(
            alarm_sound=bool(self._value("alarm.sound")),
            heartbeat_interval_seconds=int(self._value("heartbeat.interval_seconds")),
            # `settings.asr` 的五个子键与配置键 `asr.*` **逐字同名**（只去掉前缀），取生效值；
            # 嵌套写法只出现在状态 JSON 里，不进请求 / 应答 / config.json
            asr={name: self._value(f"asr.{name}") for name in config_module.ASR_SETTING_KEYS},
            # `settings.devices` 的两个子键同理（子键名与配置键的点号后半段逐字同名）
            devices=self._devices_view(),
        )

    def _devices_view(self) -> dict[str, Any]:
        """`settings.devices` 的两个子键：取两个设备键位的**生效值**（没配即 `None`）。"""
        return {
            name: self._value(f"devices.{name}") for name in config_module.DEVICE_SETTING_KEYS
        }

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
        组装初始快照（**含 `transcript` 三格的初值**，见 ⑤b）→ 扫未收尾场次 → 打开浏览器。
        **绝不自动开始录音。**
        """
        if self._started:
            return
        self._started = True
        self.started_at = now_iso()

        # ① 取事件循环（`asyncio.get_running_loop()` 只有在这里才成立）
        self._loop = asyncio.get_running_loop()

        # ② 写关停目标：语义 = 「先跑收尾、再请求关停」（常驻 app.py 的安装点，形态不变）
        from meeting.service import app as service_app

        service_app.set_shutdown_target(
            self._loop, service_app.make_trigger_shutdown(self._loop, self)
        )

        # ③ 信号接管自检（服务器的信号捕获已被 ServiceServer.capture_signals() 屏蔽）
        self._signal_ok = service_app.check_signal_handlers()

        # ④ 解析三个后端（唯一入口是 registry；失败回退替身 + warning）
        self._resolve_backends()

        # ⑤ 组装初始快照（`top_state: idle`、`session: null`、`sources` 取 probe() 的三态）
        self._probe_devices()

        # ⑤b `transcript` 三格的初值**取自后端**，不留给 dataclass 默认值
        #
        # **为什么要有这一步**（复审 m3）：`_refresh_transcript_snapshot()`
        # 只挂在节拍与「一行到来」上时，快照初值就只能是 `TranscriptState` 的 dataclass 默认
        # `health = "normal"`；而真实现的空闲态报的是 `stopped`（没起过场次 —— 见
        # `meeting/backends/transcribe.py` 的 `health()`），于是健康度节拍**第一拍**
        # （0.5 秒后）才把它刷成 `stopped` 并 `commit()`（`revision 1 -> 2`）。后果有两条：
        # ① `contract/fixtures/snapshot/idle*.json` 描述的只是「服务起来前半秒」那一瞬的取值，
        # ② 空闲场次的用例被迫抢在那一拍之前读快照 —— 一条**纯粹的时序依赖**
        # （机器被压住 >0.5 秒就红）。
        #
        # 初值在 `startup()` 里就地取一次之后：空闲档稳定成 `health = "stopped"`、
        # `revision` 停在 `REVISION_START`（初值不 `commit()`：`startup()` 里没有任何状态迁移），
        # 上面那两条一起消失。位置放在 ⑤ 之后是必须的 —— `_probe_devices()` 才把转写后端
        # `bind()` 上（`_config` 也在那里给），早于它调这一步会读到一份没接上配置的后端。
        # 返回值（= `health` 有没有迁移）在这里**有意丢掉**：`startup()` 不做提交。
        self._refresh_transcript_snapshot()
        # 空闲态不发提示级（`docs/ui-design.md` 章 2.5：空闲态告警区不出现）：快照这一格与
        # 判定器那份记账一起从零开始。`_refresh_transcript_snapshot()` 的收口判据是
        # `top_state ∈ {recording, alert}`，开机这一刻是 `idle`，所以它自己不会写进任何提示；
        # 这里再显式清一次，是为了让「快照 = 记账」这条不变量从第一帧就成立。
        self._clear_hints()

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
        """关停动作：停 ticker、停采集、**停转写**、清关停目标（本 Phase 不做收尾，收尾归 phase3）。

        `transcriber_backend.stop()` 在关停路径上一直缺一个调用点：协议里这个方法从 phase1
        就冻结着，而在此之前没有任何一处调它 —— 于是转写的读线程与 worker 会一直挂到进程退出
        （`9e5db89c` phase2 §7 的那条接线）。它与采集一样**失败只记账、不抛**：关停路径上
        任何一步出事都不许把进程卡住。
        """
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        if self._transcriber_ticker is not None:
            self._transcriber_ticker.cancel()
            self._transcriber_ticker = None
        try:
            if self.capture_backend is not None:
                self.capture_backend.stop()
        except Exception:
            _log.exception("关停时停采集失败（如实记账，不静默）")
        try:
            if self.transcriber_backend is not None:
                self.transcriber_backend.stop()
        except Exception:
            _log.exception("关停时停转写失败（如实记账，不静默）")
        # 收尾四步的执行器**不等**（工作线程是 daemon：第二次 `Ctrl+C` 之后进程不许被在飞的
        # 同步步拖住）——`wait=False` 顺带把已排队的收尾步丢掉，这正是「立即中止」要的形态。
        self._shutdown_drain_executor()
        # 会中总结那台执行器照同一口径收场：到点被放弃的那次调用仍在飞，关停不等它
        # （它同样是一条只读的模型调用，写不出半截现场）。
        self._shutdown_summary_executor()
        # 会中总结那两样一并清掉（缓冲与记录表走同一个入口）：关停之后这两笔内存不该留着
        self._clear_session_summary(reason="服务关停")
        from meeting.service import app as service_app

        service_app.clear_shutdown_target()

    def _shutdown_drain_executor(self) -> None:
        """收尾执行器收场（幂等）：**不等待**在飞的工作线程（daemon）。"""
        executor = self._drain_executor
        if executor is None:
            return
        self._drain_executor = None
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # pragma: no cover - 收场路径不掩盖别的结论
            _log.exception("收尾执行器收场失败（工作线程是 daemon，不影响进程退出）")

    def _shutdown_summary_executor(self) -> None:
        """会中总结执行器收场（幂等）：与收尾那台同一口径，**不等待**在飞的工作线程。"""
        executor = self._summary_executor
        if executor is None:
            return
        self._summary_executor = None
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # pragma: no cover - 收场路径不掩盖别的结论
            _log.exception("会中总结执行器收场失败（工作线程是 daemon，不影响进程退出）")

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
        """转写后端的 `line_sink` 同样只经 `call_soon_threadsafe` 进循环。

        **顺路把这一行收进会中总结那条内存缓冲**（`1987cb20` phase2）：缓冲只在这一个 sink 里
        喂，`bind()` 被调两次也**只装一层**（两次都返回本方法包出来的 sink，收行的那一步在
        sink 内部、与装了几层无关，于是同一行不会进缓冲两遍）。收行**先于**
        `_call_soon_threadsafe`：缓冲自己是线程安全的，事件循环没起来 / 已经关掉时那一支
        （`loop is None` 时同步处理、其余丢弃）也照旧把这行收下 —— 「谁收行」与「谁推状态」
        是两件事。
        """
        del backend  # 同 `bind_capture()`：出口属于 runtime

        def sink(line: Any) -> None:
            try:
                self.summary_buffer.append(line)
            except Exception:  # noqa: BLE001 - 缓冲出意外不许把一行转写挡在门外
                _log.warning("转写行进会中总结缓冲失败（这一行照旧往下走）", exc_info=True)
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

        self._probe_and_apply(
            reason="设备探测结果写入初始快照",
            threshold="探针三态（unprobed / ready / probe_failed），本档无阈值",
        )

    def rebind_and_probe(self) -> list[Any]:
        """重新 `bind()` 一次再 `probe()`（`reprobe` 动作走的就是这条路，幂等）。

        `probe()` 每次都用**当下**的配置与**当下**的系统端点表，于是「录到一半插上设备之后
        打一个动作重新探测」这条要求落在这里 —— 两路快照随即更新。
        """
        backend = self.capture_backend
        try:
            sink = self.bind_capture(backend)
            backend.bind(sink, config=self._config_view())
        except Exception:
            _log.exception("采集后端 bind() 失败（如实记账，不静默）")
        return self._probe_and_apply(
            reason="重新探测设备（reprobe）",
            threshold="探针三态（unprobed / ready / probe_failed），本档无阈值",
        )

    def reprobe(self) -> None:
        """`reprobe`：重新枚举并试开设备，更新快照两路（`dca4539b` phase1 §2）。

        **只在没有场次在跑的时候可用**：`recording` / `alert` 态下这两路设备是开场前定下来的，
        会中换设备属于明文不做的那一档（`docs/contract.md` 第 7 章），因此这两态一律拒绝，
        `code` 取既有的 `already_recording`（取值域冻结在 `ACTION_CODES` 里，不自造新码）。
        `ended`（含收尾停留档）态下采集已经停了，重新探测不干扰任何在飞的东西。
        """
        if self.snapshot.top_state in ("recording", "alert"):
            raise self._already_recording_error()
        _log.info("重新探测设备：按当下配置与当下系统端点表再探一遍")
        self.rebind_and_probe()
        self.commit()

    def _probe_and_apply(self, *, reason: str, threshold: str) -> list[Any]:
        """`probe()` 走一遍，把结果落进快照两路（启动与 `reprobe` 共用这一段）。"""
        backend = self.capture_backend
        try:
            probes = backend.probe()
        except Exception:
            _log.exception("采集后端 probe() 失败（如实记账，不静默）")
            probes = []
        self.probes = list(probes or [])
        self._apply_probe_results(reason=reason, threshold=threshold)
        return self.probes

    def _apply_probe_results(self, *, reason: str, threshold: str) -> None:
        """把 `self.probes` 按 `source` 落进快照两路（`dca4539b` phase1 §3 的落点）。

        越界取值（后端返回第四个探针状态这类）记 warning 并把该路落 `probe_failed`：
        服务照常起、`top_state` 不动。
        """
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
            if item.health == "probe_failed":
                _log.warning(
                    "设备探测：%s 这一路没有可用的设备（status=%s，试过哪几条见上面的 detail）",
                    item.id, status,
                )
            item.device_name = getattr(probe, "device_name", None)
            _log.info(
                "设备探测：%s -> status=%s device=%s（%s）",
                item.id, status, item.device_name, getattr(probe, "detail", ""),
            )
        # 探测结果落定即落一次「每一路健康迁移」（`unprobed` / `ready` / `probe_failed` 三档：
        # `unprobed` / `ready` 无阈值，`probe_failed` 的阈值口径见 `PROBE_STATUS` 的三档）
        self._log_health_migrations(reason=reason, threshold=threshold)

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

    def _ensure_transcriber_health_ticker(self) -> None:
        """起转写健康度节拍（幂等；没有事件循环时什么都不做）。

        **由 `start_session()` 的收尾那一步起**（`startup()` 里不起）。理由是这个节拍要盯的
        只有一件事 —— worker 停摆之后不再有行到来、快照得有人刷 —— 而那件事只在场次起来之后
        才谈得上。反向摆（`startup()` 里就起）会多出一条时序依赖：它的**第一拍**落在什么时刻
        取决于「用户多快按下开始」，空闲档 `stopped -> normal` 那一次迁移因此会落在这一场第一个
        提交的前后两种位置，`revision` 跟着差 1（复审 m3 的修法上曾实测到这一点）。
        场次起来之后这一跳已经由 `start_session()` 自己带走（那一刷就在 `commit()` 前面），
        节拍随后只管「期间有没有迁移」。
        """
        if self._transcriber_ticker is not None or self._loop is None:
            return
        self._transcriber_ticker = self._loop.create_task(self._transcriber_health_loop())

    async def _transcriber_health_loop(self) -> None:
        """转写健康度节拍：**每 `TRANSCRIBER_HEALTH_INTERVAL_SECONDS` 秒**把 `transcript`
        的三格刷一遍（`_refresh_transcript_snapshot()`），健康度迁移就 `commit()`。

        **为什么要有这一条**（`phase2/task.md` §7：「一行到来时**与 worker 停摆时**把它们写对」）：
        刷新原先只挂在「一行到来」上，而 **worker 停摆恰恰不会再有行到来** —— 没有行的原因
        就是它停了。于是真出现「转写挂了」时，快照里的 `health` 会一直停在 `normal`
        （界面 / `/debug` 都看这一格），直到有人重启或下一场开局；`buffered_lines` 同理
        （停摆时积压只增不减，正是最该看见的时候）。
        （**起拍时刻**：这条节拍由 `start_session()` 起，第一拍落在那一场开始之后半秒 ——
        空闲档的初值与 `stopped -> normal` 那一跳都不归它管，分别由 `startup()` 的 ⑤b 与
        `start_session()` 自己带走，见那两处。复审 m3。）

        **与水位那一档（`_levels_loop`）分开**：那一条只在 `recording` / `alert` 态跑，
        而「转写起没起来、还在不在」这件事与会话状态无关（idle 时也要如实报）。

        **拍子的副作用只有快照**：`_refresh_transcript_snapshot()` 只在 `health` 真迁移、
        或 `alerts.hints` 的键集合变化时返回 `True`，于是 `commit()`（`revision +1` + 一次
        `state` 广播）只可能发生在状态真的变了那一刻；积压量变化、以及持续提示条的
        `duration_seconds` 刷新照旧只写快照，`revision` 原地不动（契约第 3.3 节）。
        这一条由三条用例钉住：`test_snapshot_reads_do_not_refresh_and_do_not_push_the_revision`
        （读快照是纯读）、`test_worker_stall_reaches_the_snapshot_without_any_new_line`
        （本拍子真的把停摆刷上去了）与 `test_backlog_changes_reach_the_snapshot_without_touching_the_revision`
        （积压量不投票）。
        """
        try:
            while True:
                await asyncio.sleep(TRANSCRIBER_HEALTH_INTERVAL_SECONDS)
                try:
                    if self._refresh_transcript_snapshot():
                        self.commit()
                except Exception:  # noqa: BLE001 - 一拍刷不动不许把节拍打死
                    _log.warning("转写健康度节拍这一拍刷不动（继续下一拍）", exc_info=True)
        except asyncio.CancelledError:  # pragma: no cover - 关停时取消
            raise

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
            self._log_health_migrations(
                source=frame.source,
                reason="连续有信号达恢复阈值",
                threshold=(
                    "恢复阈值 alert.recover_seconds"
                    f"={self.judge.thresholds.recover_seconds}s"
                ),
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
        self._log_health_migrations(
            source=source,
            reason=f"这一路起致命级告警：{alert.message}",
            threshold=(
                "静默阈值 alert.silence_seconds"
                f"={self.judge.thresholds.silence_seconds}s / silence_dbfs"
                f"={self.judge.thresholds.silence_dbfs} dBFS"
            ),
        )
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
        """一行转写：进 `transcript` 字段并推 `transcript` 通道（**不参与状态迁移**）。

        **本单元补上的三格**（`9e5db89c` phase2 §7）：`transcript.health` /
        `buffered_lines` / `failed_segments` 在此之前生产代码里一处都没写过 ——
        一行到来时就地刷新一遍（判定权全部收在 `_refresh_transcript_snapshot()` 一处）。
        **worker 停摆那一档没有行可等**，所以另有一条不依赖行的刷新路径：健康度节拍
        `_transcriber_health_loop()`（`startup()` 的 ⑤b 再取一次初值）。
        **读路径不刷**（`_snapshot_json()` 是纯读，一个字都不问转写后端），理由见
        `_refresh_transcript_snapshot()` 的 docstring。
        三个字段的**提交资格只有 `health` 一格**：契约第 3.3 节写明 `transcript` 通道
        不参与状态迁移（`docs/contract.md:208`），所以「一行到来」「积压量变了」
        「失败计数多了一格」都不许给 `revision` 添抖动 ——
        `commit()` 在这里的唯一触发条件是 `health` 的**迁移**（`normal ↔ behind ↔ stopped`），
        那才是状态真的变了；顺风路径上的每一行都只广播 `transcript` 通道、
        `revision` 原地不动，而积压量与失败计数照旧写进快照（经 `/api/state` 读得到）。
        """
        try:
            text = getattr(line, "text", "")
            self.snapshot.transcript.last_line_at = now_iso()
            if str(text).startswith("[[识别失败"):
                # 占位符是失败档的唯一形状（`docs/legacy-assets.md` 的 B12）：
                # 计数只在这里 +1，与后端报的失败原因同源（同一份文本）
                self.snapshot.transcript.failed_segments += 1
            changed = self._refresh_transcript_snapshot()
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
            if changed:
                self.commit()
        except Exception:
            _log.exception("处理一行转写时抛错（不阻塞录音、不影响状态机）")

    def _refresh_transcript_snapshot(self) -> bool:
        """把 `transcript` 的三个字段刷成**当下**的取值；返回「状态有没有真的迁移」。

        读的是转写后端自己的两个计数（`health()` / `backlog_depth()`）。
        返回值描述**两件会进快照 `revision` 的事**：
          - `health` 的**迁移**（`normal ↔ behind ↔ stopped`）—— 调用方据此 `commit()`
            （契约第 3.3 节：`transcript` 通道不参与状态迁移，一行到来 / 积压量变化都不该给
            `revision` 添抖动）；
          - `alerts.hints` 的**键集合**变化（提示级新增 / 移除 / 被上限挤掉，以及离开
            `recording` / `alert` 时整格清空）—— 界面要看得见提示条的出现与消失，
            因此也借既有 `state` 通道提交一次。
        `buffered_lines` 与 `failed_segments` 照样写进快照（`/api/state` 读得到），
        但它们**不给 `revision` 投票** —— 「积压 5 段变 4 段」不是一次状态迁移；同理，同一个
        键集合里 `duration_seconds` 的刷新也不投票（那正是 0.5 秒节拍每拍都会发生的事）。

        **四个调用点**（`phase2/task.md` §7：「一行到来时**与 worker 停摆时**」）：

          - `startup()` 的 ⑤b：**初值取自后端**一次 —— 不这样做的话快照初值就是
            dataclass 默认的 `normal`，空闲态要等健康度节拍第一拍（0.5 秒后）才翻成
            `stopped` 并多提交一次（复审 m3），空闲档于是带上一条时序依赖；
          - `start_session()`：转写后端刚起（`stopped -> normal`）时就地刷一遍，
            让这次迁移跟着那一场自己的 `commit()` 走，而不是等下一拍（否则整场里会有一段
            「转写在跑、快照还写着 `stopped`」的窗口）；
          - `_apply_transcript_line()`：一行到来时就地刷一遍（顺风路径上 `health` 的迁移
            立刻 `commit()`，不必等下一拍）；
          - `_transcriber_health_loop()`：**低频节拍**（`TRANSCRIBER_HEALTH_INTERVAL_SECONDS`）
            也刷一遍 —— worker 停摆之后不再有行到来，只靠上一个调用点的话这一档永远刷不到
            快照上，界面 / `/debug` 会一直看到 `health = normal`（而「转写挂了」正是最该看见的）。

        **为什么不挂在读路径上**：`to_dict()` / `_snapshot_json()` 是快照的产出者，
        而刷新会经 `commit()` 提交（`revision +1` + 一次 `state` 广播）—— 让 `GET /api/state`
        这种只读请求去改状态，一来把「读一次就涨 `revision`」变成契约假命题，
        二来 `GET /api/heartbeat` 与随后的 `GET /api/state` 会读到两个不同的 `revision`。
        节拍与读请求完全解耦，读路径因此保持纯读。

        两个调用点都只写快照、只由上面那两件事触发 `commit()`，因此拍多少下都不会给
        `revision` 添抖动（本函数在读数没变、提示级键集合没变时是纯读）。`startup()` 那一次
        丢掉返回值，于是它连「迁移」都不提交（那一刻还没有任何状态迁移可谈）；
        `start_session()` 那一次同样丢掉返回值，因为那一场自己的 `commit()` 就在后面几步。

        **提示级在这里接线**：算出 `health` 与 `backlog` 之后交给 `_sync_transcript_hints()`
        （它调判定器、再把 `alerts.hints` 摆进快照）。本函数是全仓唯一的转写刷新落点 ——
        启动、开场、每行、0.5 秒节拍四处都经过它 —— 所以接线只在这里做一次就够，
        四条路径都会带上提示级。**每次都调判定器**（不是只在 health 迁移那一刻）：
        提示条要在窗口内一直刷新 `duration_seconds`。
        **只有在 `recording` / `alert` 态才发提示级**：`top_state ∈ {idle, ended}` 时
        `_sync_transcript_hints()` 不把读数交给判定器、并把那一格清空（理由与依据见它的
        docstring：空闲态那个 `health == "stopped"` 报的是「真转写后端还没起过场次」，
        与录音中「转写停了」是两件事）。

        两个后端方法都可能缺席（`null` 替身之外的第三方实现）或抛错：`health` 读不出来按
        `normal`、`backlog_depth` 读不出来按 `0`，并各留一条 warning —— 快照里宁可少一格
        数字，也不许因为转写侧的读数把状态机带崩。

        **健康度走的是转写域自己的校验器**（`meeting.asr.pipeline.validate_transcript_health`，
        三取值 `normal` / `behind` / `stopped`）：采集域那个 `validate_health()` 认的是探针
        七取值，越界落 `probe_failed` —— 拿它校转写域会让快照里长出前端 schema
        （`TRANSCRIPT_HEALTHS`）不认的第四个值。两个域各自冻结，校验器就各归各的。
        """
        backend = self.transcriber_backend
        health = "normal"
        backlog = 0
        if backend is not None:
            try:
                health = asr_pipeline.validate_transcript_health(backend.health())
            except Exception:
                _log.warning("转写后端 health() 读不出来（按 normal 落快照）", exc_info=True)
                health = "normal"
            try:
                backlog = max(0, int(backend.backlog_depth()))
            except Exception:
                _log.warning("转写后端 backlog_depth() 读不出来（按 0 落快照）", exc_info=True)
                backlog = 0

        transcript = self.snapshot.transcript
        changed = False
        if transcript.health != health:
            _log.info(
                "转写健康迁移：%s -> %s（积压 %s 段，门槛 %s 段，单段超时 %ss）",
                transcript.health, health, backlog,
                getattr(backend, "behind_threshold", None), self._transcriber_timeout(backend),
            )
            transcript.health = health
            changed = True
        # **积压量只写进快照，不参与状态迁移、不涨 `revision`**（契约第 3.3 节：
        # `transcript` 通道不参与状态迁移；`phase2/task.md` §7：这两个计数只在 `/debug`
        # 与 log 里看着用）。积压量几乎每一行都在变，让它触发 `commit()` 等于给 `state`
        # 通道加一份高频抖动，并把契约给界面的那句「`id` 变了就是状态变了」变成假命题。
        # 写进快照之后照样经 `/api/state` 的读路径可见。
        if transcript.buffered_lines != backlog:
            transcript.buffered_lines = backlog
        if self._sync_transcript_hints(health=health, backlog=backlog):
            changed = True
        return changed

    def _clear_hints(self) -> None:
        """把提示级清成空 —— **快照那一格与判定器那份记账一起清**（唯一入口）。

        三样东西必须同进同退：判定器的条目与记时表（`judge.clear_hints()`）、快照的
        `alerts.hints`、本类那份键集合记账（`_hint_keys`）。只清其中一两样的话，
        「快照与记账是同一份真相」这条不变量会有一刻不成立，旧记账还会让下一场的键集合判据
        误判（复审 m3）。**不改**致命级与两条电平计数。
        """
        self.judge.clear_hints()
        self.snapshot.alerts.hints = []
        self._hint_keys = ()

    def _sync_transcript_hints(self, *, health: str, backlog: int) -> bool:
        """把**已经算好的** `health` 与积压段数交给判定器，再把它算出的提示级摆进快照。

        返回「提示级的**键集合**有没有变」（新增 / 移除 / 被上限挤掉都算变），调用方据此
        决定要不要 `commit()`；同一个键集合里 `duration_seconds` 的刷新不算变化。

        **离开 `recording` / `alert` 之后提示级恒为空**（`docs/ui-design.md` 章 2.5 那张表：
        空闲态与结束档的「告警区」不出现）。判定权随状态收口：`top_state ∈ {idle, ended}`
        时**不把读数交给判定器**，并把已经挂着的那几条连记账一起清掉（`_clear_hints()`，
        键集合因此算作变了一次、由调用点借既有 `state` 通道提交一次，界面看得见它消失）。

        **为什么要收这一道口**：空闲态那个 `health == "stopped"` 报的是「真转写后端还没起过
        场次」（`docs/state.md` 第 4 章那两格 fixtures 的既有口径），与录音中「转写停了」属于
        两种情形；相位一接线时把算好的 health 无条件交给判定器，等于把前者升成了一条用户可见的
        告警条 —— 设计稿里空闲态压根没有告警区。这条提示因此只在
        `top_state ∈ {recording, alert}` 上成立，离开这两个态就归零、下一场 `start` 从零开始。

        **每次都调判定器，不挑时机**：提示条要在窗口内一直刷新 `duration_seconds`
        （「已经落后 12 秒」这句话得跟着走），只在 health 迁移那一刻调一次的话，条目会停在
        `duration_seconds = 0`。判定器那边 `_merge_hint()` 本来就幂等（同一个三元组沿用原来
        那一条、只刷时长），所以每拍都调一遍是它的正常用法。

        `health` 与 `backlog` 由调用方传进来（`_refresh_transcript_snapshot()` 刚读过的同一份
        读数），**不在这里再问一次后端** —— 读路径是纯读、快照刷新也不该把后端的读数翻倍。

        提示级的来源与阈值都在判定器里（`docs/state.md` 第 6 章）：健康度 `normal / behind /
        stopped`，积压 `≥ BACKLOG_HINT_THRESHOLD`，两类都定在 `source = transcript` 上、靠
        `message` 分辨，各自有一条对称的清除路径。
        """
        if self.snapshot.top_state not in ("recording", "alert"):
            if not self.snapshot.alerts.hints and not self._hint_keys:
                return False
            _log.info(
                "离开录音 / 告警态（top_state=%s）：提示级清空（原 %s 条）",
                self.snapshot.top_state,
                len(self.snapshot.alerts.hints),
            )
            self._clear_hints()
            return True
        self.judge.on_transcript_health(health)
        self.judge.on_backlog_depth(backlog)
        hints = self.judge.hints()
        keys = tuple((item.level, str(item.source), str(item.message)) for item in hints)
        if keys == self._hint_keys:
            return False
        _log.info(
            "提示级键集合变化：%s -> %s（快照 %s 条）",
            len(self._hint_keys),
            len(keys),
            len(hints),
        )
        self._hint_keys = keys
        self.snapshot.alerts.hints = hints
        return True

    def _transcriber_timeout(self, backend: Any) -> float:
        """转写后端的 `segment_timeout()`（读不出来按缺省；只给日志用）。"""
        try:
            return float(backend.segment_timeout())
        except Exception:  # noqa: BLE001 - 读数失败不影响别的结论
            return float(DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS)

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

    def _write_session(
        self,
        *,
        ended_at: str | None = None,
        interrupted: bool | None = None,
        state: str | None = None,
    ) -> str:
        """把当前快照写成盘上那一版 `session.json`（**原子的那一处**）。

        `interrupted=None`（普通调用点）时沿用盘上已经写着的那一版 —— 于是第三个 Phase 的
        强制打断路径先落盘的 `interrupted: true` **不会被取消后的记账抹回否**
        （phase2 §3.5.6 的「三条不覆盖约束」之一）。

        `state=None`（普通调用点、也是绝大多数调用点）时盘上那一档由
        `_session_state_on_disk()` 经 `state_for()` 的四档映射算出来。**只有取消路径**
        （`_settle_step(..., cancelled=True)`）显式传 `state="draining"`：它把这一档**钉住**，
        不让映射接管。理由正是 §3.5 第 3 条的第二条不覆盖约束（「不许把 `state` 从 `draining`
        改成别的值」）—— 现场里前面已经有 `failed` 步、在飞那一步又被 `cancel()` 打断时，
        `state_for()` 的 `has_failed_step → failed` 那一档会算出 `"failed"`，把
        `mark_interrupted()` 刚写下的 `draining` 覆盖掉；而 phase2 冻结的
        `state_for()` 四档映射本身是跨 Phase 契约、**不许在这里改**。
        取消路径因此只钉这一档的**写值**，映射与四档取值域一个字不动。
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
            state=self._session_state_on_disk() if state is None else str(state),
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
        # 新一场从零开始：判定器那份提示级记账已经被上面清掉，快照那一格与本类那份键集合
        # 记账同步归零（三样一起清，见 `_clear_hints()`）—— 上一场的提示级不许跨场留下，
        # 也不许让本场第一次刷新把它们当成「键集合没变」（复审 m3）。
        self._clear_hints()
        # 会中总结那两样同样从零开始（缓冲与记录表**走同一个入口**清，见
        # `_clear_session_summary()`）：上一场的行与那几次总结不许跟着进这一场。
        self._clear_session_summary(reason="新的一场开始")
        start_recording(self.snapshot, session=session, mode=mode, devices=devices)
        _log.info("场次开始：%s（目录 %s，mode=%s）", session_id, session_dir, mode)
        # `idle → recording` 的副作用会把两路 `health` 推到 `capturing`（phase2 §3.5.2），
        # 逐路落一条（阈值取 `mode`：本场采哪几路是这一档的口径）
        self._log_health_migrations(
            reason=f"场次开始（mode={mode}）",
            threshold=f"本场 mode={mode}（采到的路进 capturing）",
        )

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
            # 场次起来这一刻就地刷一遍 `transcript`（判定权仍在 `_refresh_transcript_snapshot()`）：
            # 转写后端刚从 `stopped` 变成在跑，而 `commit()` 就在下面 —— 顺手带上这一格。
            # 这一刷是**必须**的：不刷的话那一次迁移要等健康度节拍，而节拍第一拍落在什么时刻
            # 取决于「用户多快按下开始」，于是空闲档 `stopped -> normal` 这一跳会落在这一场的
            # 第一个提交之前、之后两种位置（`revision` 因此差 1）—— 一条把机器快慢烤进
            # golden 的时序依赖。刷在提交之前，这一跳就固定由这一场的提交带走。
            # `buffered_lines` 照旧只写快照、不参与这次提交。
            self._refresh_transcript_snapshot()

        self._pending_restart_session = None
        self.last_frame_ts = None
        self.commit()
        # 健康度节拍**从这一场开始起**（见 `_ensure_transcriber_health_ticker()` 的说明）
        self._ensure_transcriber_health_ticker()
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
        # 撤回空闲态：提示级不再成立（提示条只在 recording / alert 上出现），连记账一起清，
        # 于是下面那次提交广播出去的就是「空闲态、告警区不出现」那一份。
        self._clear_hints()
        for item in self.snapshot.sources:
            item.health = "unprobed"
            item.envelope = []
        _log.info("场次撤回空闲态（%s）", session.id)
        self._log_health_migrations(
            reason=f"采集起不来，场次撤回空闲态（{session.id}）",
            threshold="无阈值（撤回这一步不看阈值）",
        )
        self.commit()

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
        # 进 `ended` 这一刻提示级一并归零（提示条只在 recording / alert 上成立）：清在下面那次
        # 提交之前，于是广播出去的「结束档」那一份里 `alerts.hints` 就是空数组 —— 不必等
        # 0.5 秒那一拍来收尾（收尾四步期间没有任何转写刷新，等节拍会留下一段「已结束、
        # 还挂着提示条」的窗口）。
        self._clear_hints()
        _log.info(
            "场次结束：%s（目录 %s，reason=%s，本场 %ss）",
            session.id if session else "?", session.dir if session else "?", reason,
            round(session.elapsed_seconds, 3) if session else "?",
        )
        # `→ ended` 的副作用把两路 `health` 推到 `stopped`（phase2 §3.5.2），逐路落一条
        self._log_health_migrations(
            reason=f"场次结束、进收尾（reason={reason}）",
            threshold="无阈值（收尾这一步不看阈值）",
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
        运行期仍取启动时的生效值，快照 `settings` 里读到的还是旧值（可观察判据是主 log 那一行
        「……仍取启动值」，见 §7.1 高危点 7）；本次改动里凡有热读键（`asr.*` 五个与
        `alarm.sound`），就在下面重取一次 `_settings_view()` 并提交 —— 出声开关因此勾选即生效。
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
        # 热读键立刻生效：生效值在内存里换掉（下一段读到的就是新值）
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
        # 这一行只对**真正要重启才生效**的那些键说：热读键（含 `alarm.sound`）走上面的
        # `applied` 分支，写盘那一刻快照与运行期行为就已经换过去了，报「仍取启动值」
        # 会是一句与事实相反的话（`alarm.sound` 原先归 `pending_restart`，这一行是那时的形状）。
        if pending:
            _log.info(
                "settings_update：%s 归 pending_restart，仍取启动时的生效值（改了要重启才生效）",
                "、".join(sorted(pending)),
            )
        if "alarm.sound" in changes:
            _log.info(
                "settings_update：alarm.sound 收在热读档，本刻起出声开关 = %r（下一次提交的快照一并可见）",
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

    # -- 会中总结（`1987cb20` phase2：动作、记录表、缓冲的清理） ------------

    def _clear_session_summary(self, *, reason: str) -> None:
        """把会中总结那两样**一起**清成空：内存缓冲（`summary_buffer`）与本场记录表
        （`summary_records`）。

        **唯一入口**（照 `_clear_hints()` 那种同进同退的先例）：两样必须一起清 —— 只清缓冲
        会留下「记录里那几条还挂着、它们指的窗口已经取不回」的现场，只清记录又会把上一场的行
        留给这一场。两个调用点：
        `start_session()`（新的一场从零开始）与 `shutdown()`（关停之后这两笔内存不留）。
        **不动** `summary_client`（那是注入进来的协作者，与本场的数据无关）。
        """
        had_lines = len(self.summary_buffer)
        had_records = len(self.summary_records)
        self.summary_buffer.clear()
        self.summary_records.clear()
        if had_lines or had_records:
            _log.info(
                "会中总结的两笔内存已清空（%s）：转写缓冲 %s 行、本场记录 %s 条",
                reason, had_lines, had_records,
            )

    def _session_id_for_summary(self) -> str | None:
        """本场总结记录归属的场次目录名（没有场次时 `null`）。"""
        session = getattr(self.snapshot, "session", None)
        return None if session is None else str(session.id)

    def summaries_dict(self) -> dict[str, Any]:
        """`GET /api/summaries` 的应答体：本场按过的、**成功产出的**那几次（按时间先后）。

        没有场次或一次都没按过时 `session_id` 为 `null`、`entries` 是空数组。每一格的形状由
        `phase2/task.md` 的接口口径写死（`window` / `window_label` / `at` / `elapsed_seconds` /
        `what` / `where` / `lost` —— **没有 `lines` 与 `bullets`**）。
        """
        return {
            "session_id": self._session_id_for_summary(),
            "entries": [dict(entry) for entry in self.summary_records],
        }

    def _summary_executor_or_create(self) -> _DaemonThreadPoolExecutor:
        """会中总结那台执行器（**惰性创建**：不按下总结的进程一个线程都不起）。"""
        if self._summary_executor is None:
            self._summary_executor = _DaemonThreadPoolExecutor(
                max_workers=SUMMARY_EXECUTOR_MAX_WORKERS,
                thread_name_prefix="summary-call",
            )
        return self._summary_executor

    async def _summary_wait(
        self,
        *,
        window: str,
        settings: SummarySettings,
        client: Any,
        started_at: str | None,
    ) -> SummaryOutcome:
        """把一次**同步**的会中总结调用丢进工作线程，连同 `asyncio.wait_for` 一起等。

        形状照收尾那套先例（`_drain_wait()` + `_DaemonThreadPoolExecutor`）：

          - `run_summary(...)` 经 `loop.run_in_executor(self._summary_executor_or_create(), …)`
            提交，返回的线程 Future 由 `asyncio.wrap_future()` 包成可等待对象 —— 事件循环在
            这一段只负责等，`/api/state`、三个 SSE 通道、心跳与健康节拍照旧转；
          - 等待上限 = `settings.deadline_seconds`（配置里那条 `summary.deadline_seconds`，
            默认 25 秒）。它与内核 `run_summary()` 现算剩余量用的是同一条线，**一秒都不多给**；
          - 到点时放弃的是**等待**：已经交给工作线程的那次调用照跑到底（与 `_drain_wait()` 的
            `cancel()` 口径同一句），因此紧接着按下的第二次总结由池里另一条线程接住
            （`SUMMARY_EXECUTOR_MAX_WORKERS` 取 2 的来路）。

        `client` 外面包一层 `_AttemptsSeen` 记账：走到超时这一档时内核的
        `SummaryOutcome.detail` 已经拿不到了，`attempts` 只能从这里读（见那个类的说明）。

        到点的报法与内核判「预算用尽」那一档同形：`ActionError("summary_failed", …, 502,
        detail={"elapsed_seconds", "attempts"})`。`summarize()` 那个 try 里有一条
        `except ActionError: raise` 专门放它过去（否则会被「动手之后出的意外」那一条重新包成
        `{error: ...}`，`detail` 的形状就偏了）。
        """
        loop = asyncio.get_running_loop()
        seen = _AttemptsSeen(client)
        lines = self.summary_buffer.lines()
        pending = asyncio.wrap_future(
            loop.run_in_executor(
                self._summary_executor_or_create(),
                lambda: run_summary(
                    lines,
                    window=window,
                    settings=settings,
                    client=seen,
                    started_at=started_at,
                ),
            )
        )
        started = time.monotonic()
        try:
            return await asyncio.wait_for(
                pending, timeout=max(0.0, float(settings.deadline_seconds))
            )
        except asyncio.TimeoutError as exc:
            elapsed = time.monotonic() - started
            attempts = max(1, seen.attempts)
            _log.warning(
                "会中总结等满预算（%ss）还没结果：按 summary_failed 如实上报"
                "（实测等了 %ss、试了 %s 次）",
                settings.deadline_seconds, round(elapsed, 3), attempts,
            )
            raise ActionError(
                "summary_failed",
                f"等了 {elapsed:.1f} 秒（预算 {settings.deadline_seconds:g} 秒）还没有结果，"
                f"试了 {attempts} 次",
                status=502,
                detail={"elapsed_seconds": round(elapsed, 3), "attempts": attempts},
            ) from exc

    async def summarize(self, args: dict[str, Any]) -> dict[str, Any]:
        """`summarize`：按一下，把最近这段时间的实时转写读一遍，给回三小段。

        **判定的先后是固定的**（照 `start` 的先例，`phase2/task.md` 的接口口径）：

          1. 判态 —— 没有在录的场次（`idle` / `ended`）→ `not_recording`(409)；
          2. 校验 `window` —— 取值不在四档里 → `invalid_setting`(400)（**不静默落缺省档**：
             动作层的非法取值是一句话就说清的事，悄悄替人挑一档会让人以为按对了）；
          3. 看转写健康 —— `stopped`（转写停了 / 没起过）→ `transcript_unavailable`(503)；
          4. 看模型端点配置 —— 端点三件套缺一个 → `summary_failed`(502)，`detail.missing`
             点名缺哪几个键；
          5. **才动手**。

        **前四档没过，一个请求都不发出去、一条记录都不写**（判据是用例里那个记账的假客户端
        读数为 0）。第 3 / 4 档的读数在读到的当口就判掉：判定与动手之间不再把后端或配置问第二遍。

        **这是一条协程，而且「动手」那一段经 `_summary_wait()` 丢进工作线程**
        （复审 minor 6 的直接修复）：`run_summary()` 是一次**同步**调用（真客户端是
        `urllib.request`），放在事件循环上跑就把 `/api/state`、三个 SSE 通道、心跳与健康节拍
        一起按住 —— 真端点预算默认 25 秒就是按住 25 秒，而设计章 4.5 对 `summary-running`
        那一档写的正是「总结区 `正在总结刚才的 15 分钟…`；**其余照常**」。
        挪出循环之后，总结期间别处的请求照常应答（判据见
        `tests/test_summary_action.py` 里那条量往返耗时的用例）。

        **预算语义一个字不改**：那一次调用的等待上限就是配置里那条
        `summary.deadline_seconds`（默认 25 秒），与内核 `run_summary()` 现算剩余量时用的
        是同一条线；到点时报的还是 `{elapsed_seconds, attempts}` 那一档（契约 4.7 与
        `docs/contract.md` 章 4 的 `summary_failed` 表）。

        **总结是只读的**：成功与失败两条路都不调 `commit()`、不涨 `revision`、不广播 ——
        快照里一个字都不变（结果走这个应答体，记录走 `GET /api/summaries`）。

        返回值就是应答体里那个 `summary` 子对象（`routes.py` 把它挂在通用信封上）。
        """
        if self.snapshot.top_state not in ("recording", "alert"):
            raise ActionError("not_recording", "现在没有在录的场次，总结动作无效", status=409)

        window = args.get("window")
        choice = str(window).strip() if window is not None else None
        if choice is not None and choice not in WINDOW_CHOICES:
            raise ActionError(
                "invalid_setting",
                f"window 取值非法：{window!r}（只能是 {' / '.join(WINDOW_CHOICES)}）",
                status=400,
                detail={"key": "window", "value": window, "legal": list(WINDOW_CHOICES)},
            )

        settings = SummarySettings.from_config(self._config_view())

        # 转写健康度：**这一档的读数只读一次**（判定与动手之间不许再问一遍后端）
        health = None
        if self.transcriber_backend is not None:
            try:
                health = asr_pipeline.validate_transcript_health(self.transcriber_backend.health())
            except Exception:
                _log.warning("会中总结：转写后端 health() 读不出来", exc_info=True)
                health = None
        if health in (None, "stopped"):
            raise ActionError(
                "transcript_unavailable",
                "转写没在跑（没起过，或者已经停了）：会中总结吃的是实时转写，现在没有可用的那一路",
                status=503,
                detail={"health": "stopped"},
            )

        missing = tuple(settings.missing)
        if missing:
            raise ActionError(
                "summary_failed",
                f"模型端点没配齐，缺这些配置键：{'、'.join(missing)}"
                "（配好 summary.base_url / summary.api_key / summary.model 之后再来一次）",
                status=502,
                detail={"missing": list(missing)},
            )

        session = getattr(self.snapshot, "session", None)
        started_at = None if session is None else str(session.started_at)
        try:
            client = self.summary_client
            if client is None:
                client = ModelClient.from_settings(settings)
            outcome = await self._summary_wait(
                window=choice if choice is not None else settings.default_window,
                settings=settings,
                client=client,
                started_at=started_at,
            )
        except ActionError:
            # 已经按契约分好档的那一条（`_summary_wait()` 判到墙钟预算到点）：**原样上抛**，
            # `detail` 就是契约里那两格，不许在这里被重新包成 {error: ...}
            raise
        except Exception as exc:
            # 「才动手」之后出的意外同样按这一档报（配置与态都已经过了，这不是参数错）
            _log.exception("会中总结在「动手」之后出的意外（如实回 summary_failed）")
            raise ActionError(
                "summary_failed",
                f"会中总结失败：{type(exc).__name__}: {exc}",
                status=502,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc

        if not outcome.ok:
            raise ActionError(
                "summary_failed",
                outcome.error or "会中总结没拿到结果",
                status=502,
                detail=dict(outcome.detail),
            )

        entry = self._summary_result(outcome)
        _log.info(
            "会中总结完成：window=%s（%s）lines=%s attempts=%s elapsed=%ss",
            entry["window"], entry["window_label"], outcome.lines,
            int(outcome.attempts), entry["elapsed_seconds"],
        )
        # **只记成功产出的那几次**（失败没有「那几条」可记）；记录活在内存里，不落文件。
        # 记录这一条按 `GET /api/summaries` 写死的形状来（`phase2/task.md` 那一份没有 `lines`），
        # 与上面交给应答体的那一份是**两个形状**：应答体多一格窗口行数。
        self.summary_records.append(self._summary_entry(outcome))
        return entry

    @staticmethod
    def _summary_result(outcome: SummaryOutcome) -> dict[str, Any]:
        """`SummaryOutcome` → 应答体里那个 `summary` 子对象（逐键照接口口径）。

        形状写死（`phase2/task.md` 的成功体）：`window` / `window_label` / `at` /
        `elapsed_seconds` / `lines` / `what` / `where` / `lost`。**`ok` / `bullets` / `attempts` /
        `detail` 不进应答体** —— 失败那几档走失败信封的 `code` + `detail`。`at` 由这里现取
        （`now_iso()`，与快照里那些时刻同一个格式），此后不再变。
        """
        return {
            "window": str(outcome.window),
            "window_label": str(outcome.window_label),
            "at": now_iso(),
            "elapsed_seconds": float(outcome.elapsed_seconds),
            "lines": int(outcome.lines),
            **sections_for(outcome),
        }

    @classmethod
    def _summary_entry(cls, outcome: SummaryOutcome) -> dict[str, Any]:
        """`SummaryOutcome` → 记录表里那一条（`GET /api/summaries` 写死的形状）。

        与 `_summary_result()` 同一份来源、少一格 `lines`（那一份是 `GET` 的口径里没有的）。
        """
        result = cls._summary_result(outcome)
        result.pop("lines")
        return result

    # -- 收尾编排（三步记账 + 终态判定；预算与失败出路的完整语义归 phase3） --

    async def drain(self) -> str:
        """收尾协程（**普通协程、持有 task 句柄、可被 `cancel()`**）。

        编排：`flush` → `transcode` → `log` → `post`；每步进入与落定各写一次盘、各提交一次；
        **四步全部落定之后把前提交给 `finish_drain()`** 判终态。被取消时按收尾步骤表落盘
        （已落定的保持原样、未开跑的维持 `waiting`、**不伪造终态**），随后同样走
        `finish_drain()` 这一个出口。

        **取消路径也真的经 `cancelled=True` 那条记账机制**（phase3 §3.5 第 3 条 / §2.4 的
        「取消时按收尾步骤表落盘」）：取消时的记账与终态提交都带 `cancelled=True` ——
        `_settle_step(..., cancelled=True)` 把在飞那一步按它**此刻真实的状态**记一笔
        （`running` 就照实写 `running`，不伪造终态、不改已落定的步骤），`_finish_drain(cancelled=True)`
        随后走终态出口。两条都落在 `frozen_ended` / `frozen_interrupted` 上，于是
        **先落盘那一版 `draining` + `interrupted: true` 不被抹回 `false`、`state` 不被改成别的值、
        `ended_at` 不被补写**（§3.5 的三条不覆盖约束）。

        **自适应预算（phase3 §3.3）**：一开头按积压量与段长算出这一趟收尾的总上限
        `compute_drain_budget(backlog, segment_timeout)`，**四步共享同一条 deadline**，
        每一步的调用都经 `_drain_wait()` 包在「剩余预算」里 —— 于是等待总量以那条 deadline
        为界、不随步数翻倍，且**绝不阻塞事件循环**（收尾期间 `/api/state`、SSE 与
        `POST /api/actions` 照常应答）。**四步调用无一例外都经 `_drain_wait()`**；
        唯一例外是 `log` 步的 flush 与摘除，它至少拿到 `DRAIN_LOG_MIN_BUDGET_SECONDS`。
        """
        try:
            await self._run_drain_steps()
        except asyncio.CancelledError:
            _log.warning("收尾被取消：按收尾步骤表落盘，随后走 finish_drain() 判终态")
            self._settle_cancelled_step()
            self._finish_drain(cancelled=True)
            raise
        finally:
            self._drain_task = None
        return self.snapshot.ended.phase if self.snapshot.ended else "draining"

    def _settle_cancelled_step(self) -> bool:
        """取消时的记账（§3.5 第 3 条）：把**在飞那一步**经 `_settle_step(..., cancelled=True)`
        记一笔，于是「取消时不许覆盖 `interrupted` / `state` / `ended_at`」这三条真的被执行到。

        **不伪造终态**是这一笔的关键：在飞那一步此刻的真实状态就是 `running`（它被取消打断、
        既没跑完也没抛错），所以照实记 `running` —— 已落定的步骤一个都不改，`waiting` 也维持
        `waiting`（「未开跑的维持 `waiting`」）。**取消路径因此真的经这条机制**：在修本条之前
        `_settle_step(..., cancelled=True)` 一处调用点都没有，`frozen_*` 收到的永远是默认的
        `False`，那三条不覆盖约束没有一条被实际走到。

        没有 `ended`（重启现场那一档，此时也没有收尾在跑）时什么也不做；四步恰好已全部落定时
        也不补这一笔（那一档盘上最终值由 `_finish_drain(cancelled=True)` 那一趟收口，§3.5 第 3 条）。
        """
        ended = self.snapshot.ended
        if ended is None:
            return False
        in_flight = [step for step in ended.steps if step.status == "running"]
        if len(in_flight) != 1:
            _log.info(
                "取消时没有「正在跑」的收尾步骤要记（running 的步骤数=%s）：已落定的保持原样",
                len(in_flight),
            )
            return False
        step = in_flight[0]
        _log.warning(
            "取消时的记账：把在飞那一步 %s 按它此刻真实的状态记一笔（running 照实记 running，"
            "不伪造终态；已落定的步骤与盘上那一版 interrupted/state/ended_at 都不覆盖）",
            step.id,
        )
        self._settle_step(
            step.id,
            "running",
            "收尾被取消：这一步还没跑完（照实记 running，不伪造终态）",
            None,
            None,
            cancelled=True,
        )
        return True

    def _drain_inputs(self) -> tuple[int, float]:
        """这一趟收尾的预算输入：`(backlog, segment_timeout)`（缺省按 §3.3 取）。"""
        backlog = DRAIN_DEFAULT_BACKLOG
        segment_timeout = DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS
        backend = self.transcriber_backend
        if backend is None:
            return backlog, segment_timeout
        try:
            backlog = int(backend.backlog_depth())
        except Exception:
            _log.exception(
                "转写后端读不出 backlog_depth()（按缺省 %s 算预算）", DRAIN_DEFAULT_BACKLOG
            )
            backlog = DRAIN_DEFAULT_BACKLOG
        try:
            segment_timeout = float(backend.segment_timeout())
        except Exception:
            _log.exception(
                "转写后端读不出 segment_timeout()（按缺省 %ss 算预算）",
                DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS,
            )
            segment_timeout = DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS
        return backlog, segment_timeout

    def _remaining_budget(self, deadline: float | None) -> float:
        """这一趟还剩下多少预算（秒）：`deadline` 减此刻的单调钟，**下限夹在 0**。

        契约口径（§3.3）：**四步共享同一条 deadline**，每一步的调用都拿它的剩余时间当上限，
        所以四步调用点一律把 `_run_drain_steps()` 算出来的那一条传进来（判据见
        `tests/test_drain.py` 的 ⑤h：四步读到的截止时刻逐值相等、开跑时的剩余预算逐级变小）。

        **`deadline is None` 在这里显式报错，不给出任何上限**（审查问题 m2 的直接修复）：
        这条上限只能来自 `compute_drain_budget()`（§8 的坑表），而 `None` 换不出一个**说得清**
        的数 —— 退成 `float("inf")` 就是「没有上限」（§3.3 点名要避免的静默退化），退成
        `DRAIN_BUDGET_MIN_SECONDS` 是另一条凭空的保守上界、把「谁忘了传」这件事盖住（四步
        调用点现下无一例外都传得进 deadline，那一支此前是不可达的死代码，改成 `inf` 也全绿）。
        于是这里选择**当场说话**：拿到 `None` 就抛 `ValueError`，报出「上限只能来自
        `compute_drain_budget()`」，调用点不会静默退化成一个猜出来的秒数。
        """
        if deadline is None:
            raise ValueError(
                "收尾等待上限必须是一条具体的 deadline（四步共享的那条）："
                "上限只能来自 compute_drain_budget()，收到 None 时这里不会给出任何兜底秒数"
            )
        return max(0.0, deadline - time.monotonic())

    def _drain_wait_timeout(self) -> float:
        """「等这一趟收尾落定」用多大的上限（第一次 `Ctrl+C` 的等待，`_wait_drain_settled()` 取它）。

        **与本次收尾同源**：`drain()` 一开头就把这一趟算出来的那笔预算留给运行时
        （`_run_drain_steps()` 写 `_drain_budget` / `_drain_deadline`），这里直接沿用，
        **不再把 `_drain_inputs()` 读第二遍**。

        契约依据（§3.5 第一行「等收尾落定后关停服务」、§3.3「一趟收尾的总上限以那条 deadline
        为界」）：真转写后端的 `backlog_depth()` 会随收尾推进而下降，收尾跑到一半再读一次会
        拿到一个**更小**的积压量、算出一条**更短**的上限 —— 等待会因此在收尾自己那条
        deadline 之前就放弃、留下 `draining` 现场，而退出码看着还是干净档。同源之后这条
        短路不存在了。

        取值 = **本次收尾 deadline 的剩余时间** 加 `log` 步那条最小预算（§3.3 写死的那笔
        共享预算之外的唯一开销：`log` 步的 flush 与摘除至少拿到
        `DRAIN_LOG_MIN_BUDGET_SECONDS`）。收尾此刻还在跑 → 用剩余时间；收尾已经跑完
        （或还没开跑）→ `_drain_deadline` 已清空，按本次那笔预算全额等，与
        「四步共享一条 deadline」这条口径同源。
        """
        deadline = self._drain_deadline
        budget = float(
            DRAIN_BUDGET_MIN_SECONDS if self._drain_budget is None else self._drain_budget
        )
        remaining = budget if deadline is None else self._remaining_budget(deadline)
        return remaining + float(DRAIN_LOG_MIN_BUDGET_SECONDS)

    def _drain_executor_or_create(self) -> _DaemonThreadPoolExecutor:
        """取（必要时建）收尾四步的同步执行器：工作线程是 daemon。

        用 `loop.run_in_executor(<这个执行器>, ...)` 而**不是** `asyncio.to_thread()`：
        后者固定走事件循环的**默认**执行器（非 daemon 工作线程），第二次 `Ctrl+C` 之后
        解释器会 join 它，进程于是被在飞的同步步拖住（§3.5 行为表第二行的「立即中止」）。
        显式传执行器只影响收尾四步，默认执行器那一路一个字不动。
        """
        if self._drain_executor is None:
            self._drain_executor = _DaemonThreadPoolExecutor()
        return self._drain_executor

    async def _drain_wait(
        self,
        step_id: str,
        call: Callable[[], Any],
        *,
        deadline: float,
        min_budget: float = 0.0,
        invoke: bool = False,
    ) -> tuple[Any, float]:
        """把一步的调用包在**剩余预算**里：

            `asyncio.wait_for(<该步调用>, max(<剩余预算>, min_budget))`

        **四步的调用无一例外都经这里**，因此一趟收尾的等待总量以那条 deadline 为界。
        `deadline` 是**必传**的（`_remaining_budget()` 在收到 `None` 时当场抛 `ValueError`，
        不给兜底秒数：上限只能来自 `compute_drain_budget()`）。
        `min_budget` 只给 `log` 步用（`DRAIN_LOG_MIN_BUDGET_SECONDS`）：它的 flush 与摘除
        不受剩余预算约束。

        **同步与协程两条路受同一个剩余预算约束**（本函数写死的形态）：
        `asyncio.wait_for(<这一趟要 await 的东西>, budget)` 是**唯一**的等待形式，
        `budget` 一律由 `_remaining_budget(deadline)` 与 `min_budget` 现算 —— 于是
        **没有任何一条路能绕过等待上限**，`log` 步的实际耗时也以本函数的返回值为准。

        执行落点还是分两种（哪种都不许把事件循环钉住）：

          - **同步实现**（`finalize()` / `launch()` / 场次 log 的 flush 与摘除这类 IO）：
            调用点传 `invoke=True`，调用经**收尾专用的 daemon 执行器**提交到工作线程
            （`loop.run_in_executor(...)` + `wrap_future`），再连同等待一起包进 `wait_for`；
            若让它直接跑在事件循环上，收尾期间 `/api/state` 就不应答了 —— 与「收尾必须
            可观测」相抵（判据见 `tests/test_drain.py` 的「收尾期间可观测」那两条）。
            **工作线程是 daemon**：第二次 `Ctrl+C` 的「立即中止」因此不被在飞的同步步拖住
            （见 `_DaemonThreadPoolExecutor`）。
          - **返回协程的调用**：不传 `invoke`，`call()` 的返回值（协程）经
            `asyncio.ensure_future(...)` 包成任务再 `wait_for`，协程在事件循环这一侧推进。

        **同步 callable 忘了传 `invoke=True`：在开跑之前就刺耳**（审查问题 m1 的直接修复）。
        这一步本来会在事件循环上**整段跑完**那个同步实现（审查实测：一个 3 秒的 `time.sleep`
        把循环钉满 3 秒，`min_budget` 拦不住它，收尾期间 `/api/state`、SSE 与
        `POST /api/actions` 一起停摆），之后才因为 `wait_for` 认不出普通值而抛 `TypeError`。
        现在这条错法在**调用之前**就被认出来：`invoke` 为假、而 `call` 是一个同步可调用对象
        （`inspect.iscoroutinefunction()` 为假）时，本函数当场抛 `TypeError`（文案里点名
        `invoke=True`），**同步实现一行都不跑**，那一步立刻经由既有的 `except` 落 `failed`，
        收尾的其余步骤照常推进、事件循环一直在转
        （判据见 `tests/test_drain.py` 的 ⑤b 与新增的「忘传 `invoke=True`」那一条）。
        协程形态照旧支持：`async def` 的调用点、以及返回可等待对象的普通可调用对象
        （`call()` 的返回值经 `asyncio.iscoroutine()` 认出来，照样进等待层）。

        **本包装层只负责超时**：超出上限抛 `DrainLimitExceeded`（调用方落该步 `failed`）；
        步骤自身抛出的异常**不加捕获、原样上抛**（于是它照样冒到该步的 `failed` 记账，
        不会被吞成 `skipped`）。返回 `(这一步调用的返回值, 实测耗时秒数)`。
        """
        if not invoke and not inspect.iscoroutinefunction(call):
            raise TypeError(
                f"收尾步骤 {step_id} 的调用是一个同步可调用对象，调用点必须传 invoke=True"
                "（同步实现要提交到收尾专用的 daemon 工作线程）：直接在这里调用它会把事件循环"
                "整段钉住，收尾期间 /api/state、SSE 与 POST /api/actions 会一起停摆"
            )
        budget = max(float(min_budget), self._remaining_budget(deadline))
        started = time.monotonic()
        try:
            # 两步分开写：先拿到「这一趟要 await 的东西」，再把它**无一例外**包进
            # `asyncio.wait_for(...)` —— 同步路（daemon 执行器的线程 Future）与协程路
            # （协程 / 任务）因此受同一个 `budget` 约束，没有哪条路能绕过去。
            if invoke:
                loop = asyncio.get_running_loop()
                pending: Any = asyncio.wrap_future(
                    loop.run_in_executor(self._drain_executor_or_create(), call)
                )
            else:
                pending = call()
                if asyncio.iscoroutine(pending):
                    pending = asyncio.ensure_future(pending)
            awaited = await asyncio.wait_for(pending, timeout=budget)
        except asyncio.TimeoutError as exc:
            elapsed = time.monotonic() - started
            raise DrainLimitExceeded(
                f"超出收尾预算（budget={round(budget, 3)}s，实测耗时 {round(elapsed, 3)}s）"
            ) from exc
        return awaited, max(0.0, time.monotonic() - started)

    async def _run_drain_steps(self) -> None:
        backlog, segment_timeout = self._drain_inputs()
        budget = compute_drain_budget(backlog, segment_timeout)
        deadline = time.monotonic() + budget
        # 这一趟的预算与 deadline 都留给运行时：「等收尾落定」的上限与本次收尾同源
        # （`_wait_drain_settled()` 经 `_drain_wait_timeout()` 沿用这笔预算，不再读第二次输入）
        self._drain_budget = budget
        self._drain_deadline = deadline
        _log.info(
            "收尾开始：backlog=%s segment_timeout=%ss -> budget=%ss（下限 %ss、封顶 %ss）；"
            "四步共享这一条 deadline（log 步的 flush 与摘除另保底 %ss）",
            backlog, segment_timeout, budget,
            DRAIN_BUDGET_MIN_SECONDS, DRAIN_BUDGET_MAX_SECONDS, DRAIN_LOG_MIN_BUDGET_SECONDS,
        )
        try:
            await self._drain_flush(deadline)
            await self._drain_transcode(deadline)
            await self._drain_log(deadline)
            await self._drain_post(deadline)
        finally:
            self._drain_deadline = None
        self._finish_drain()
        # 收尾走完之后的最后一趟落盘：盘上那一版与终态一致
        # （含 `state`、`drain.steps` 与结束时间）
        self._write_session(ended_at=now_iso())
        _log.info(
            "收尾终态已落盘：ended.phase=%s（这一趟实测耗时 %ss）",
            self.snapshot.ended.phase if self.snapshot.ended else "?",
            round(max(0.0, time.monotonic() - (deadline - budget)), 3),
        )

    async def _drain_flush(self, deadline: float) -> None:
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
            _value, elapsed = await self._drain_wait(
                step_id, self.capture_backend.finalize, deadline=deadline, invoke=True
            )
        except Exception as exc:
            self._fail_step(step_id, exc, resolution, "采集归档落盘失败")
            return
        self._done_step(step_id, "归档已落盘（finalize() 返回）", resolution, elapsed=elapsed)

    async def _drain_transcode(self, deadline: float) -> None:
        """第 2 步 `transcode`：收尾转换（后端 `transcode()`）；采集替身记 `skipped`、抛错记 `failed`。

        闸门只看**采集侧**：transcode 是 CaptureBackend 的能力（`registry.capabilities_of()`
        的第三项），转写后端在不在场与这一步无关 —— 解析结论、`run_ok` 也都记在采集侧。
        """
        step_id = "transcode"
        self._begin_step(step_id, "收尾转换（后端 transcode()）")
        resolution = self.capture_resolution
        if resolution is None or resolution.skipped() or not self.capture_capabilities.get("transcode"):
            self._skip_step(
                step_id,
                "这一场没有可转码的产物（未接真采集后端 / 采集替身没有 transcode 能力；转码归 dca4539b）",
                resolution,
            )
            return
        try:
            _value, elapsed = await self._drain_wait(
                step_id, self.capture_backend.transcode, deadline=deadline, invoke=True
            )
        except Exception as exc:
            self._fail_step(step_id, exc, resolution, "收尾转换失败")
            return
        self._done_step(step_id, "收尾转换完成（transcode() 返回）", resolution, elapsed=elapsed)

    async def _drain_log(self, deadline: float) -> None:
        """第 3 步 `log`：写 `session.json` 的结束标记，并 flush / 写结束标记 / 摘掉场次 log。

        这一格**在任何构建里都有真实实现**，所以它**永远不该出现 `skipped`**；写失败
        （例如磁盘满）是「有实现却没做成」，按 §3.0 的裁决记 **`failed`** 并如实上报
        （**不中断录音**：此刻本来就已经在收尾了，进程照常活着、页面照常看得见）。

        **预算上的例外（§3.1 第 3 条）**：`log` 步的 flush 与摘除至少拿到
        `DRAIN_LOG_MIN_BUDGET_SECONDS`，不受剩余预算约束 —— 否则前面几步把 deadline 用光时，
        本该「正常收尾」的一档会被拖成收尾失败档。
        """
        step_id = "log"
        self._begin_step(step_id, "写这一场的结束标记（会话文件 + 场次日志）")
        started = time.monotonic()
        try:
            # **`ended_at` 不在这里写**（§2.4 的「只读契约清单」那一行）：它只在收尾走到
            # 终态（`done` / `failed`）时才落值，而终态由 `finish_drain()` 一处判定 ——
            # 那一趟之后的 `_run_drain_steps()` 末尾那次落盘才是 `ended_at` 的唯一写入点。
            # 这一步原先跟着写 `ended_at`，于是盘上会出现「`state=draining` + `ended_at`
            # 有值」的瞬态（`log` 步落下时收尾还没走完、`ended` 里那一刻也没有失败步），
            # 与「强制打断路径不写 `ended_at`」那条口径也读不顺。这里只把这一半的
            # **结束标记**（`state` 与四步概要先算出来交给场次 log）落稳。
            self._write_session()
            _log.info("场次结束标记已写入 session.json（会话文件这一半）")
        except Exception as exc:
            self._fail_step(step_id, exc, None, "写这一场的结束标记失败")
            return

        state = self._session_state_on_disk()
        steps = [step.status for step in (self.snapshot.ended.steps if self.snapshot.ended else [])]
        try:
            # **`log` 步也经 `_drain_wait()`**：同步 IO 一律提交到工作线程再连同等待一起
            # 包进那一个等待上限里，`log` 步的实际耗时因此以它返回的实测值为准。
            problem, log_elapsed = await self._drain_wait(
                step_id,
                lambda: logging_setup.write_session_end_marker(state=state, steps=steps),
                deadline=deadline,
                min_budget=float(DRAIN_LOG_MIN_BUDGET_SECONDS),
                invoke=True,
            )
        except asyncio.CancelledError:
            # 取消是强制打断路径（§3.5）：记账交回取消路径，这里不吞
            raise
        except Exception as exc:
            self._fail_step(step_id, exc, None, "场次日志的 flush / 结束标记 / 摘除失败")
            return
        if problem:
            self._fail_step(
                step_id,
                OSError(problem),
                None,
                f"场次日志写失败（如实上报，不静默）[{problem}]",
            )
            return
        self._settle_step(
            step_id,
            "done",
            "场次日志已 flush、结束标记已写、handler 已摘掉（结束标记是这一场的最后一行）",
            None,
            None,
            elapsed=max(float(log_elapsed), max(0.0, time.monotonic() - started)),
        )

    async def _drain_post(self, deadline: float) -> None:
        """第 4 步 `post`：让会中模型退出显存 → 拉起会后流程（**启动即算完成，不等它跑完**）；钩子缺席记 `skipped`。"""
        step_id = "post"
        self._begin_step(step_id, "拉起会后流程（启动即算完成，不等它跑完）")
        resolution = self.post_resolution
        if resolution is None or resolution.skipped():
            self._skip_step(step_id, "会后流程还没接入（PostProcessHook 缺席，归 65dd3e4c）", resolution)
            return
        session_dir = self.snapshot.session.dir if self.snapshot.session else None
        # **先让会中模型退出显存，再拉起会后流程**（顺序是这一步的全部要点）：
        # `post.launch()` 一返回，那条 daemon 线程就已经在跑 `run_session()`，会后离线链
        # （MOSSTD）的加载紧接着就开始 —— 释放放在 `launch()` 之后等于没做。16 GB 卡上两份
        # 模型同住才是真风险，而「会中用哪个模型、会后用哪个模型」正是本节要的那一处交接。
        #
        # **防御式取法**：`release_gpu()` 只长在 `TranscriberBackend` 这一个具体后端上
        # （不在 `backends.base` 的协议里），收尾用例挂的那些替身后端没有它 —— 取不到就
        # 什么都不做。挂假件的用例因此不受这一处接线影响。
        #
        # **不新增 step_id**：这一次释放的耗时算在 `"post"` 这一步的记账之内（`state.py` 的
        # 取值域只有 flush / transcode / log / post）。释放里抛异常照收尾相邻几步的既有姿态办：
        # 包住、收成一条 log、照旧往下走 —— 释放失败不该把整场收尾拖成 `failed`。
        release = getattr(self.transcriber_backend, "release_gpu", None)
        if callable(release):
            try:
                # 与其余几步同一形态：同步实现提交到收尾专用的 daemon 工作线程，
                # 连同等待一起包进这一步的剩余预算里。
                await self._drain_wait(step_id, release, deadline=deadline, invoke=True)
            except asyncio.CancelledError:
                # 取消是强制打断路径（§3.5）：记账交回取消路径，这里不吞
                raise
            except Exception:
                _log.exception(
                    "会中模型退出显存失败（如实记账，不静默；会后流程照常拉起）"
                )
        try:
            _value, elapsed = await self._drain_wait(
                step_id, lambda: self.post_hook.launch(session_dir), deadline=deadline, invoke=True
            )
        except Exception as exc:
            self._fail_step(step_id, exc, resolution, "会后流程启动失败")
            return
        self._done_step(step_id, "会后流程已启动（不等它跑完）", resolution, elapsed=elapsed)

    # -- 收尾记账（写盘 + 提交都收在这几个方法里） -------------------------

    def _begin_step(self, step_id: str, detail: str) -> None:
        _log.info("收尾步骤开始：%s（%s）", step_id, detail)
        self._drain_step_started = time.monotonic()
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
        elapsed: float | None = None,
    ) -> None:
        if elapsed is None and self._drain_step_started is not None:
            # 缺席步（`skipped`）也报耗时：它们各自也有 enter -> settle 的实测时长
            elapsed = max(0.0, time.monotonic() - self._drain_step_started)
        # 每步的「结束 + 耗时」逐条进 log（方案 §5.3 行 250、§6.3 行 304）
        spent = "" if elapsed is None else f"，耗时 {round(float(elapsed), 3)}s"
        _log.info("收尾步骤结束：%s -> %s（%s）%s", step_id, status, detail, spent)
        if elapsed is not None:
            # 单列一行「耗时」：`log` 步的结束那一行落在摘掉场次 handler 之后，
            # 这一行则一定写进场次 log（方案 §6.3 行 304 的「每一步的耗时」两处都查得到）。
            _log.info("收尾步骤耗时：%s 用时 %ss", step_id, round(float(elapsed), 3))
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
            # **取消路径的落盘把 `state` 钉在 `draining` 上**（§3.5 第 3 条的第二条不覆盖
            # 约束「不许把 `state` 从 `draining` 改成别的值」）：前面已经有 `failed` 步
            # （本例就是 `post`）时，`state_for()` 的 `has_failed_step → failed` 那一档
            # 会算出 `"failed"`，把 `mark_interrupted()` 刚写下的 `draining` 覆盖掉 ——
            # 强制打断档盘上必须是 `draining` + `interrupted: true` + 无 `ended_at`。
            # 只钉这一档的写值；`state_for()` 的四档映射与取值域一个字不动。
            self._write_session(
                interrupted=True if cancelled else None,
                state="draining" if cancelled else None,
            )
        except Exception:
            _log.exception("收尾写盘失败（步骤 %s 落定）：如实记账，不静默", step_id)
        self.commit()
        if resolution is not None:
            resolution.mark_run(status != "failed")

    def _done_step(
        self,
        step_id: str,
        detail: str,
        resolution: BackendResolution | None,
        *,
        elapsed: float | None = None,
    ) -> None:
        self._settle_step(step_id, "done", detail, None, resolution, elapsed=elapsed)

    def _skip_step(
        self,
        step_id: str,
        detail: str,
        resolution: BackendResolution | None,
        *,
        elapsed: float | None = None,
    ) -> None:
        # 缺席（构建期没有真实实现）→ skipped，不阻塞 done，留一行 warning
        _log.warning("收尾步骤跳过：%s —— %s", step_id, detail)
        self._settle_step(step_id, "skipped", detail, None, resolution, elapsed=elapsed)

    def _fail_step(
        self,
        step_id: str,
        exc: Exception,
        resolution: BackendResolution | None,
        what: str,
        *,
        elapsed: float | None = None,
    ) -> None:
        # 有实现但抛错 / 超预算 → failed（error = 原因 + 可复制的命令 + log 路径）
        session_dir = self.snapshot.session.dir if self.snapshot.session else "<场次目录>"
        error = (
            f"{what}：{type(exc).__name__}: {exc}"
            f"；可复制命令：meeting post {session_dir}"
            f"；log 路径：{self.log_path}"
        )
        _log.exception("收尾步骤失败：%s（%s）", step_id, what)
        self._settle_step(step_id, "failed", what, error, resolution, elapsed=elapsed)

    def _finish_drain(self, *, cancelled: bool = False) -> str:
        """调**收尾的唯一出口** `finish_drain()` 判终态；失败档把两条出路写进 log。"""
        session_dir = self.snapshot.session.dir if self.snapshot.session else None
        final_state = finish_drain(self.snapshot)
        ended = self.snapshot.ended
        if final_state == "done":
            _log.info("收尾完成：四步全部落定且没有失败步（ended.phase=done，top_state 停在 ended）")
            # 成功档的收尾副作用把两路 `health` 从 `stopped` 带回 `unprobed`（phase2 §3.5.2），
            # 逐路落一条（失败档保留 `stopped` 现场，health 不动、因此这里不落）
            self._log_health_migrations(
                reason="收尾走到终态（ended.phase=done）",
                threshold="无阈值（收尾终态这一步不看阈值）",
            )
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
                    "出路一：`meeting post %s`（音频与 session.json 都在盘上，重跑不欠这次收尾什么）；"
                    "出路二：看清现场后重启服务（重启走 restart_unfinished 那一档）",
                    failed, session_dir,
                )
        return final_state

    # -- 强制打断（§3.5）与关停接线 ---------------------------------------

    def mark_interrupted(self) -> dict[str, Any] | None:
        """强制打断的**先落盘**：写 `state: "draining"` + `interrupted: true`。

        - **保留原 `steps`**（`steps_from_ended()`）、**不伪造终态**、**不写 `done`**、
          **不写 `ended_at`** —— 于是重启与 `meeting post` 都还接得上；
        - 走 `SessionStore.write_session_json()` 的**薄封装** `SessionStore.mark_interrupted()`
          （同一入口、同一原子写），**本 Phase 不开第二个写盘入口、不新开文件句柄**；
          `state` / `interrupted` / `ended_at` 三个参数的组装**在那层薄封装里**，
          这里只负责「取出现场记录 → 调它 → 落 log」（phase3 §2.4 / §4 的薄封装口径）；
        - 只在**四步尚未全部落定**这一档负责盘上最终那一版；四步恰好已全部落定时按终态规则
          由 `finish_drain()` 那一趟落（见 `trigger_shutdown()` 的判据）。

        取消时的记账不覆盖这一版（`_settle_step(cancelled=True)` 与 `_write_session()` 的
        `interrupted=None` 两处都沿用盘上已写着的那一版）。
        """
        session = self.snapshot.session
        if session is None:  # pragma: no cover - 走到这里一定有场次
            return None
        steps = steps_from_ended(self.snapshot.ended) if self.snapshot.ended is not None else None
        # 这一版的 `state` / `interrupted` / `ended_at` 三个参数由那层薄封装组装
        # （`SessionStore.mark_interrupted()`），这里交出去的是**现场记录本身**
        record = self._session_record(
            session,
            state="",
            ended_at=None,
            interrupted=False,
            steps=steps,
        )
        path = self.store.mark_interrupted(session.dir, record)
        written = dict(record)
        written["state"] = "draining"
        written["interrupted"] = True
        written["ended_at"] = None
        _log.warning(
            "%s：已落盘 state=draining + interrupted=true"
            "、原 steps 原样保留、不写 ended_at；盘上那一版 -> %s",
            _interrupted_message(),
            path,
        )
        return written

    def drain_has_settled(self) -> bool:
        """四步是否全部落定（盘上术语：`waiting` / `running` 都不算落定）。"""
        ended = self.snapshot.ended
        if ended is None:
            return False
        return all(step.status in ("done", "skipped", "failed") for step in ended.steps)

    def drain_finished(self) -> bool:
        """这一趟收尾是否**走完且终态已定**（`ended.phase == "done"`，收尾 task 句柄已置空）。

        第一次 `Ctrl+C` 的等待条件：收尾要**真跑完**（含 `log` 步的挂摘）才关停服务。
        """
        return self.snapshot.ended is not None and self.snapshot.ended.phase == "done"

    def drain_in_progress(self) -> bool:
        """收尾 task 是否还在跑 / 已经排上但还没开始跑。"""
        task = self._drain_task
        return task is not None and not task.done()

    async def _cancel_drain_task(self) -> None:
        """对 `_drain_task` 调 `cancel()` 并等它收场。

        **句柄已被读成「已结束」时跳过 `cancel()`、只做落盘**（§3.5 第 2 步）。
        """
        task = self._drain_task
        if task is None or task.done():
            _log.info("收尾 task 已结束（或没有收尾在跑）：跳过 cancel()，只做落盘")
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            _log.info("收尾 task 已按取消路径落盘（未开跑的步骤维持 waiting，不伪造终态）")

    async def trigger_shutdown(self, *, force: bool) -> str:
        """**信号路线唯一的线程安全触发入口**（由 `app.handle_shutdown_signal` 排进事件循环）。

        语义是「先跑收尾、再请求关停」，四类触发点在这里分支（§3.5 的表）：

          - `force=False` 且**当场没有收尾要管** → 直接退出（空闲退出档）：`idle`（进程刚起、
            没开过任何一场）、`idle` 挂着重启提示（`notice.restart_unfinished`）、以及
            `ended` 且四步已全部落定**无 `failed`**（`ended.phase == "done"`，收尾干净走完）
            三格都落在这一支 —— 第三种是审查问题 C1 补上的那一格，它**不写盘**；
          - `force=False` 且当时 `recording` / `alert` → 走**同一条**
            `stop_session(reason="ctrl_c")` 路径起收尾，等它落定再请求关停；
          - `force=True`，或当时已经停在 `ended·draining`（`stop` 之后、收尾失败或还没跑完）
            → **等同「第二次 Ctrl+C」**：先落盘（`draining` + `interrupted: true`、不写
            `ended_at`）→ 再 `cancel()` 收尾 task → 请求关停。**绝不写 `done`**。这一支落的
            log 行是冻结常量 `DRAIN_INTERRUPTED_MESSAGE` 加上**真实按下次数**（见下面那行
            注释：这一支里用户可能只按过一次）。这一支开头仍挡着
            `_first_press_can_just_exit()` 那一格（收尾干净走完），避免误改已经落稳的 `done`。

        全部动作（收尾状态、`interrupted`、`session.json` 的写值、`cancel()`）都发生在事件循环
        这一侧 —— 信号处理函数本身只做「把这个回调排进循环」这一件事。
        """
        from meeting.service import app as service_app

        if not force and self._first_press_can_just_exit():
            # 这一支有三格（§3.5 行为表的二 / 四 / 收尾干净那一格），都不是「有收尾要做」：
            # 空闲态、重启提示态、以及「四步已全部落定且无 failed」的收尾干净档。
            # 三格都不写盘、不 `cancel()`、不写 `interrupted`（C1 的那一格就在第三种）。
            _log.info("这一次 Ctrl+C 落在「没有收尾要管」这一格：不写盘、直接退出（空闲退出档）")
            self._shutdown_interrupted = False
            self._request_server_shutdown()
            return "exit"

        if not force and self.snapshot.top_state in ("recording", "alert"):
            try:
                self.stop_session(reason="ctrl_c")
                _log.info("第一次 Ctrl+C：收尾已起（与 stop 同一条路径），等它落定再关停服务")
            except ActionError as exc:
                _log.warning("第一次 Ctrl+C 想收尾但动作被拒（%s）：不关停，等第二次 Ctrl+C", exc)
                return "idle"
            await self._wait_drain_settled()
            self._shutdown_interrupted = False
            self._request_server_shutdown()
            return "drained"

        # 第二次按下 / 已经停在 ended·draining 再按：立即中止，顺序写死（先落盘、再 cancel()）。
        # **先挡一格**：四步已全部落定且**无 `failed`** 时，这一场已经收尾干净（`ended.phase` 已是
        # `done`、盘上是 `state="done"` + `ended_at`），当场**没有收尾要管** —— 不落盘、不 `cancel()`、
        # 不写 `interrupted`，直接走空闲退出档。判据与 `_first_press_can_just_exit()` 同一句，
        # 两处都不许把「收尾干净走完」读成「已停在 `ended·draining` 再按」（审查问题 C1）。
        if self._first_press_can_just_exit():
            _log.info(
                "这一次关停请求落在「收尾干净走完」这一格：没有收尾要管，"
                "盘上那一版（state=done + ended_at）一个字都不改，直接退出"
            )
            self._shutdown_interrupted = False
            self._request_server_shutdown()
            return "exit"

        self._shutdown_interrupted = True
        # 冻结常量 `DRAIN_INTERRUPTED_MESSAGE` 原样保留（§3.5 的行为表把它划给「等同第二次
        # Ctrl+C」这一档），同一行里补上**真实按下次数**：`stop` 之后收尾中再按的那一档，
        # 用户只按了一次，常量末尾那句「用户第二次 Ctrl+C」与事实对不上，这一项把记录对齐。
        _log.warning("%s", _interrupted_message())
        if self.snapshot.ended is not None:
            self._settle_drain_host()
            self.mark_interrupted()
        else:  # pragma: no cover - 重启现场那一档：盘上已是 draining + interrupted
            _log.warning("强制打断：手上没有收尾中的场次（重启现场的盘上那一版已由启动扫描落稳）")
        await self._cancel_drain_task()
        self._request_server_shutdown()
        return "interrupted"

    def _settle_drain_host(self) -> None:
        """强制打断只写 `draining` + `interrupted: true`：让 `_session_state_on_disk()` 的映射
        落在 `draining` 这一档上（`ended.phase` 保持 `draining`，不写 `done`）。"""
        if self.snapshot.ended is not None and not self.drain_has_settled():
            self.snapshot.ended.phase = "draining"

    def _drain_running(self) -> bool:
        """这一趟收尾**是不是还在跑 / 还没开跑**（句柄在手上且未落定）。

        §3.5 行为表把「收尾进行中」「收尾失败现场」两档与「无收尾可做」分得一清二楚，
        判据因此落在**句柄 `_drain_task` 的读法**上（它由 phase2 的 `drain()` 持有、走完含
        被取消那趟后置空），**不看盘上有没有未收尾场次**：`_pending_restart_session` 是
        `start` 的拦截条件（§3.6 的启动扫描留下的现场），此刻**没有任何收尾在跑**。
        """
        return self.drain_in_progress()

    def _drain_failed_face(self) -> bool:
        """是不是「收尾已经跑完、停在 `ended·draining` 的**失败现场**」（§3.5 行为表第四行）。

        两个条件缺一不可：四步都落定、且四步里**有 `failed`**。
        **只判「落定」会把「收尾干净走完」那一格也吞进来** —— `stop` 已收尾干净
        （`ended.phase == "done"`、盘上 `state="done"` + `ended_at`）时这样误判，第一次
        `Ctrl+C` 就会被当成「已停在 `ended·draining` 再按」，把盘上那一版无条件改写成
        `draining` + `interrupted: true` + `ended_at=None` 并以强制打断档退出（审查问题 C1；
        负向实测：把这一句退回 `return True`，`tests/test_shutdown_signal.py` 的
        `test_clean_drain_then_sigbreak_exits_zero_without_rewriting_disk` 实测退出码 130）。
        """
        ended = self.snapshot.ended
        if ended is None:
            return False
        if not self.drain_has_settled() or self._drain_running():
            return False
        return any(step.status == "failed" for step in ended.steps)

    def _first_press_can_just_exit(self) -> bool:
        """第一次 `Ctrl+C` 能不能「直接退出」（§3.5 行为表的第一 / 第二 / 第三 / 第四行）。

        判据落在**当场要干的活**上，逐格写清（判据按「此时此刻有没有收尾要管」读）：

          - `recording` / `alert` → 起收尾，等它落定再关停（行为表第一行，不算「直接退出」）；
          - `ended` + 收尾还在跑 → 等同第二次按下（行为表第三行）；
          - `ended` + 收尾已落定的**失败现场**（四步落定且有 `failed`）→ 等同第二次按下
            （行为表第四行）；
          - `ended` + 四步已全部落定且**无 `failed`**（`ended.phase == "done"`）→ **没有收尾要管**：
            收尾终态已经由 `finish_drain()` 那一趟落稳（盘上 `state="done"` + `ended_at` 都在），
            这一刻直接退出即可（空闲退出档），**盘上一个字节都不改**（C1 的那一格）；
          - **其余情形直接退出、走空闲退出档** —— `idle` 挂着重启提示
            （`notice.restart_unfinished`）时正落在这一格：这一刻 `_drain_task is None`，
            手上没有任何要收的尾，重启现场那一版盘上值已由启动扫描落稳。
            `_pending_restart_session` 只拦新场次的 `start`，拿它当关停档位的判据会让
            「重启提示态下第一次 `Ctrl+C`」落进强制打断档（退出码 130）。
        """
        top = self.snapshot.top_state
        if top in ("recording", "alert"):
            return False
        if self._drain_running():
            return False
        if self._drain_failed_face():
            return False
        # 剩下的两格都落在这一句上（四步已全部落定且**无 `failed`** 的「收尾干净走完」，
        # 与「其余情形」）：两者当场都没有收尾要管，盘上那一版由上一趟自己落稳，
        # 这里**不产生任何写盘动作**。
        return True

    async def _wait_drain_settled(self, *, timeout: float | None = None) -> None:
        """等这一趟收尾落定（第一次 `Ctrl+C` 的「收尾真的跑完」）。

        退出条件只有两条：收尾走到终态（`ended.phase == "done"`），或四步全部落定且收尾 task
        已经不在跑（失败档 / 取消档）。上限**与本次收尾同源** —— 不传 `timeout` 时取
        `_drain_wait_timeout()`（本次那笔预算 / 本次 deadline 的剩余时间加 `log` 步那条最小
        预算），**这里不把 `_drain_inputs()` 读第二遍**：真转写后端的积压量会随收尾下降，
        再读一次只会得到一条更短的上限，等待就会在收尾还没跑完时放弃。等待只做睡觉与读快照，
        **不打断收尾**。
        """
        if timeout is None:
            timeout = self._drain_wait_timeout()
        deadline = time.monotonic() + float(timeout)
        while True:
            if self.drain_finished():
                return
            if not self.drain_in_progress() and self.drain_has_settled():
                return
            if time.monotonic() >= deadline:
                _log.warning("等收尾落定超时（%ss）：按当前落盘的那一版关停，不静默", timeout)
                return
            await asyncio.sleep(DRAIN_SETTLE_POLL_SECONDS)

    def _request_server_shutdown(self) -> None:
        from meeting.service import app as service_app

        service_app.request_server_shutdown()

    @property
    def shutdown_interrupted(self) -> bool:
        """本次关停是不是强制打断那一档（`cli.py` 据此挑退出码，§3.5）。"""
        return bool(self._shutdown_interrupted)



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
    "SESSION_MODES",
    "SUMMARY_EXECUTOR_MAX_WORKERS",
    "TRANSCRIBER_HEALTH_INTERVAL_SECONDS",
    "ActionError",
    "DrainLimitExceeded",
    "ServiceRuntime",
    "compute_drain_budget",
]
