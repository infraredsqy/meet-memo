"""转写后端 —— 把 `meeting/asr/pipeline.py` 那条流水线包成协议实现（`9e5db89c` phase2 §7）。

约定导入路径就是这个模块的 `TranscriberBackend`（`meeting/backends/registry.py` 的
`DEFAULT_PATHS["transcriber.backend"]`）：`transcriber.backend` 取默认值 `auto` 时解析到的就是它。
构造函数**不许有必填参数**（registry 无参实例化），运行期依赖一律走 `bind()`。

**这个模块在没装模型依赖的机器上必须 import 成功**（`phase2/task.md` §7 的最后一条）：
模块顶层只有标准库、包内模块与 `meeting.*` 的**轻**模块；`numpy` / `webrtcvad` /
`torch` / `transformers` 全在函数体内惰性 import（`meeting/asr/pipeline.py` 与本模块
各自的函数体），因此 `meeting web` 的启动链一行重依赖都不碰。

协议成员的落点：

  - `bind(line_sink, *, config)`：记下出口与只读配置视图（runtime 在 `start()` 之前调）；
  - `start(mode, session_dir)`：起流水线（**起步失败不阻塞录音** —— 转写坏了可以重跑，R1 的立场）；
  - `stop()`：停转写（关读线程 + 在收尾预算内把队列里剩下的段尽量做完）；
  - `release_gpu()`：把会中模型占的显存让出去（收尾第 4 步与暂停动作调；**不在协议里**，
    只是这个具体后端多出来的一个方法，调用点用 `getattr` 防御式取它）；
  - `acquire_gpu()`：把会中模型**重新拉起来**（暂停之后继续时调；同样**不在协议里**，
    同样走防御式取法）。它与 `release_gpu()` 是同一件事的两半 —— 「要用的时候才拉起来，
    用完立刻关掉」（`docs/design-intent.md` 第 21 条：暂停、打断、终止、跑完都当场结束）；
  - `health()`：`normal` / `behind` / `stopped` 三态（判定依据逐条写在下面对应的分支上）；
  - `backlog_depth()`：队列里还没处理的段数；
  - `segment_timeout()`：配置里的 `asr.timeout_seconds`（默认 20.0）。

**模型加载那一环**走 phase1 的私有测试接缝 `meeting.asr.glm._load_models`（`glm.py:130-151`）：
本节通过**模块对象**取用它（`glm_module.GlmAsrTranscriber()` / `_is_noise_segment`），
别在别处写 `from meeting.asr.glm import _load_models` 那种绑到另一个模块对象的引用 ——
那正是 V1 那个「测试一边通过一边什么都没验」的坑（`docs/v1-lineage.md` 的 C9 / E9）。
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any, Callable

from meeting import config as config_module
from meeting.asr import glm as glm_module
from meeting.asr import pipeline as pipeline_module
from meeting.backends.base import SOURCE_IDS

_log = logging.getLogger("meeting.backends.transcribe")

#: `asr.timeout_seconds` 读不出来时的缺省（与 `meeting/config.py` 的默认值、与 phase1 的口径一致）
DEFAULT_SEGMENT_TIMEOUT_SECONDS = 20.0

#: `stop()` 那条收尾线程 join 的额外余量（秒），加在收尾预算之上。
#:
#: 余量要盖住**内层最坏耗时**，不是只留 1 秒（复审 m5 修的就是这一格）。内层
#: `TranscriptionPipeline.stop()` 的最坏耗时是：
#:
#:   ``读线程收尾上界 × 路数（_join_readers 逐路等 READER_JOIN_TIMEOUT_SECONDS）
#:     + VAD 尾段送出 + worker.join(budget + DRAIN_JOIN_SLACK_SECONDS)``
#:
#: 后半截与外层同源（同一个 `budget`），前半截**完全是外层的额外开销**：`_join_readers()`
#: 每路最多 2.0 秒，两路就是 4 秒。外层若只留 1 秒，最坏档（worker 在预算内没做完）会
#: 先超时 —— 打出一条**假的**「收尾线程没跑完」warning，并且在流水线还在收尾时就返回
#: （与 `stop()` docstring 的「返回即收队完成」不符）。
#: 1 秒那一份照旧留着：它是「预算耗完那一刻 worker 手上还没做完的那一段」（与流水线里
#: `worker.join(timeout=budget + DRAIN_JOIN_SLACK_SECONDS)` 同一条口径），
#: 让「预算内该做完的都做完了」这件事落到 log 上，而不是让本方法先一步返回、
#: 把 `pipeline.stop()` 那句「还有 N 段没做完」的如实登记打断在进程退出里。
DRAIN_STOP_SLACK_SECONDS = 1.0

#: `release_gpu()` 等在飞推理收干净的上界（秒）。
#:
#: **为什么是这个量级**：会中模型由进程级单例长期持有，一场录完就该让出显存 —— 收尾第 4 步
#: （`meeting/service/runtime.py` 的 `_drain_post()`）做的正是这件事（R26 之后这一步不再
#: 拉起任何会后动作，释放是它仅剩的活），依据是 `docs/design-intent.md` 第 21 条
#: 「用完即关」：暂停、打断、终止、跑完都当场结束，显存不占到场次之外。
#:
#: 丢掉引用之前要等在飞的推理收干净（否则 worker 那一帧还握着旧模型，而下一段会经
#: `_transcriber_or_create()` 再造一份 —— 两份 GLM 同时在卡上，每份约 4.3 GiB 权重）。
#: **等的上界只能是个小值**：第 4 步那一次调用受收尾那一条 deadline 约束，吃掉它的预算会让
#: `post` 从 `done` 翻成 `failed`、`has_failed_step` 再把整场落 `failed`（`meeting/state.py`）。
#: `20` 秒（单段超时缺省）与自适应收尾预算（`drain_join_budget()`：下限 35 秒、最坏 305 秒）
#: 都会顶穿那条 deadline，因此都不用。取 `5.0` 秒：一份**已经加载好**的模型的单段推理在正常
#: 盘面上是 1–3 秒量级，5 秒盖得住；真卡住时到点就丢引用 + 记一条 log（不抛、不静默）。
RELEASE_GPU_WAIT_SECONDS = 5.0

#: 完成判定点名的那条实现路径（只在文档与用例里引用，不在运行时使用）
TRANSCRIBER_TARGET = "meeting.backends.transcribe:TranscriberBackend"


class _SinkProxy:
    """行出口的**间接层**：把「后端 → 出口」这一步推迟到每一行真正到来的时候。

    为什么需要它：runtime 在 `probe()` **之前**就 `bind()` 了一次（那时还没有会话），
    会话起来之后还会 `bind()` 第二次；流水线的 worker 线程则可能在后端被重新 `bind()` 之后
    才算出第一行。worker 每次经这里现取一次后端当前的 `line_sink`，于是「绑定时刻」与
    「产出时刻」谁先谁后都不影响那一行被送出去（`phase2/task.md` §7 的接线要求）。
    """

    def __init__(self, backend: "TranscriberBackend") -> None:
        self._backend = backend

    def __call__(self, line: Any) -> None:
        self._backend._deliver(line)


class TranscriberBackend:
    """真转写后端：游标尾读归档 PCM → VAD 切段 → 单 worker 顺序识别 → 落盘 + 行出口。

    两路各一条读线程（`meeting/asr/pipeline.py` 的 `tail_archive_segments()`），一个 worker 顺序消费
    两路的段 —— 采集与模型因此解耦到「模型停摆不影响落盘」（`docs/v1-lineage.md` 的 R2 / R3）。
    """

    def __init__(
        self,
        *,
        transcriber_factory: Callable[[], Any] | None = None,
        vad_factory: Callable[[], Any] | None = None,
        budget_fn: Callable[[int, float], float] | None = None,
        read_chunk_bytes: int | None = None,
    ) -> None:
        """四个测试接缝（缺省都指向生产那一份；registry 的无参实例化因此照常成立）。

        - `transcriber_factory()` 造一个 `SegmentTranscriber`（缺省 `GlmAsrTranscriber()`，
          模型惰性加载）；用例注入假模型，**任何用例都不加载真模型**；
        - `vad_factory()` 造一路的 VAD（缺省真 `webrtcvad`）；
        - `budget_fn(backlog, segment_timeout)` 算收尾预算（缺省就是
          `meeting.service.runtime.compute_drain_budget`，公式只此一份）；
        - `read_chunk_bytes` 覆写一次读多少字节（用例用小块把时间戳的落点掐细）。
        """
        self._line_sink: Callable[[Any], None] | None = None
        self._config: Any = None
        self._transcriber_factory = transcriber_factory
        self._vad_factory = vad_factory
        self._budget_fn = budget_fn
        self._read_chunk_bytes = read_chunk_bytes
        self._pipeline: pipeline_module.TranscriptionPipeline | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._mode: str | None = None
        self._session_dir: str | None = None
        self._segment_timeout: float | None = None
        self._model_error: str = ""
        self._transcriber: Any = None
        #: 模型引用这一格的锁：`release_gpu()` 的「丢引用 + `empty_cache()`」与
        #: `_transcriber_or_create()` 的新建互斥（释放半路被 worker 抢进去 = 两份模型同时在卡上）
        self._model_lock = threading.RLock()
        #: 在飞推理的计数**与它的等待条件**（`release_gpu()` 等的就是它回到 0）。
        #: 用 `Condition` 而不是轮询：等待是事件驱动的（`_transcribe_segment()` 每段都会通知）
        self._inflight = 0
        self._inflight_cv = threading.Condition()
        #: 模型引用已经被释放掉了吗（幂等那一格；`_transcriber_or_create()` 造新模型时复位）
        self._gpu_released = False
        self._started = False
        self._stopped = False

    # -- 协议 -------------------------------------------------------------

    def bind(self, line_sink: Callable[[Any], None], *, config: Any = None) -> None:
        """记下事实出口与只读配置视图（runtime 在 `start()` 之前调一次）。"""
        self._line_sink = line_sink
        self._config = config
        if self._pipeline is not None:
            self._pipeline.set_line_sink(line_sink)
        _log.info(
            "转写后端已接上出口（backend=%s；session=%s）",
            TRANSCRIBER_TARGET, self._session_dir,
        )

    def start(self, mode: str, session_dir: str) -> None:
        """起转写：建流水线（**空壳那一档只建对象、不起线程**）并起读线程 + worker。

        **上一场先收干净再起这一场**（`_retire_previous()`）：否则上一场的读线程会继续
        轮询上一场的归档路径，同一进程里连开两场时那是实打实的资源泄漏。

        **这一步是同步的、会占住调用方线程**（复审 m3）：有上一场没被收走时，这里要等它
        那笔收尾预算跑完（下限 30 秒、封顶 300 秒 + 读线程收尾上界），而调用方是事件循环上的
        `ServiceRuntime.start_session()` —— 于是「再开一场」这个动作最长要等到那时候才应答。
        触发时机与上界逐条写在 `_retire_previous()` 的 docstring 与 `docs/service.md` 第 2 章。

        **起步失败不阻塞录音**：任何异常都收成一条 error 日志 + `health()` 落到 `stopped`，
        绝不往上抛（录音这一场照跑）。
        """
        self._retire_previous()
        self._mode = str(mode)
        self._session_dir = str(session_dir)
        self._stop.clear()
        self._stopped = False
        self._started = True
        try:
            self._pipeline = self._build_pipeline()
            self._pipeline.start(mode, session_dir)
        except Exception as exc:  # noqa: BLE001 - 转写坏了可以重跑，录音照跑
            self._model_error = f"{type(exc).__name__}: {exc}"
            _log.exception("转写后端起步失败（不阻塞录音）")
        _log.info(
            "转写后端已起：mode=%s session=%s（流水线=%s）",
            mode, session_dir, "在跑" if self._pipeline is not None else "没起来",
        )

    def _retire_previous(self) -> None:
        """把上一场的流水线**同步**收干净（幂等；没有上一场时什么都不做）。

        收尾走的是 `pipeline.stop()` 本身（关读线程 + 在收尾预算内做完队列里剩下的段），
        它是同步的：返回即收队完成，不会留下跨着下一场继续轮询的读线程。

        **这是同步收尾的第二个调用点，触发时机与上界都与 `stop()` 不同**（复审 m3）：

          - **触发时机**：`start()` 的第一句。服务里「开始录音」那一场起来时，上一场若还没被
            收走（`stop` 动作只收采集、不碰转写；生产路径上 `stop()` 只有 `shutdown()` 一个
            调用点），这里就会**在调用方线程上**把上一场收掉 —— 而调用方正是事件循环上的
            `ServiceRuntime.start_session()`（`meeting/service/runtime.py`）。于是「再开一场」
            这个动作在上一场收尾跑完之前不应答（`/api/heartbeat`、SSE、动作端点同样被占住）；
          - **上界**：这里**没有外层上界**，等的就是 `pipeline.stop()` 自己那笔预算
            （`compute_drain_budget(积压 + 每路最多一段尾段, 单段超时)`，下限 30 秒、封顶
            300 秒）+ 读线程收尾上界 × 路数（每路 `READER_JOIN_TIMEOUT_SECONDS`，最坏
            300 + 4 = 304 秒）。因此这一档与 `stop()` 同一量级、同一取舍，差的只是 `stop()`
            额外那两项余量（`DRAIN_STOP_SLACK_SECONDS` 与按积压量预留的收尾余量）。

        为什么不异步化：换场必须**先**把上一场收干净（否则上一场的读线程会继续轮询上一场的
        归档路径，同一进程里连开两场时那是实打实的资源泄漏），与 `stop()` 同源的取舍。
        完整口径写在 `docs/service.md` 第 2 章。
        """
        previous, self._pipeline = self._pipeline, None
        self._thread = None
        if previous is None:
            return
        try:
            note = previous.stop()
        except Exception:  # noqa: BLE001 - 上一场收场失败不挡这一场
            _log.exception("上一场转写收尾失败（如实登记，不静默）")
            return
        if note["segments_seen"] or note["unfinished"]:
            _log.info(
                "上一场转写已收队：入队 %s 段、处理完 %s 段、剩 %s 段（%s）",
                note["segments_seen"], note["segments_done"], note["unfinished"], note["note"],
            )

    def stop(self, *, drain_join_seconds: float | None = None) -> None:
        """停转写：关读线程 + 在收尾预算内把队列里剩下的段尽量做完（**幂等**）。

        **`stop()` 是同步等待**（复审 m4 定的口径）：流水线的收尾是同步 IO + `join()`，
        本方法把它放进一条 daemon 线程（`asr-stop`）之后就**在调用方线程里 `join` 它**。
        调用方正是事件循环上的 `ServiceRuntime.shutdown()`，因此**关停期间事件循环确实
        被占住**：这一段里 `/api/heartbeat`、SSE、动作端点都不应答，进程也退不出去。
        这是有意的取舍 —— 此刻服务正在离场、录音已经收尾，等的就是「把队列里剩下的段
        尽量做完」（短一点会丢临时稿的尾巴，异步化则要改整个关停编排）。
        `Ctrl+C` 之后可能要等一会儿才退出的完整口径（含那个上界的算法）写在
        `docs/service.md` 第 2 章。

        **本方法等的上界** = 流水线的收尾预算
        （默认 `compute_drain_budget(backlog_depth() + 每路最多一段尾段, segment_timeout())`，
        下限 30 秒、封顶 300 秒）+ `DRAIN_STOP_SLACK_SECONDS` + 读线程收尾上界 × 路数，
        由 `drain_join_budget()` 算。空闲态的下限因此是 `30 + 1 + 4 = 35` 秒
        （积压 0 时那笔预算就是下限 30 秒；**尾段那一项已经含在预算里**：它按「积压 + 每路
        一段」算，复审 m2）、最坏档是 301 + 4 = **305 秒**（两路都在读；`mode=mic` 时读线程
        那一项是一路）。这一条是必须的：本方法是 `transcriber_backend.stop()` 在**生产路径
        上的唯一调用点**（`runtime.shutdown()` 那一条），写死一个小常量（曾经是 5 秒）就等于
        把流水线里「在 30–300 秒的预算内尽量做完」与那句「还有 N 段没做完」的如实登记
        **在真关停里打成空头支票** —— 预算还没花完，进程就退了。

        `drain_join_seconds` 是**显式的更小上界**（给「必须马上退」的调用方用，例如测试里
        要钉「上界确实来自这儿、不是写死的常量」的那一幕）：给了就以它为准，
        不给就走上面那条自适应预算 —— **默认值必须让积压能排空**。
        """
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        pipeline = self._pipeline
        if pipeline is None:
            return
        join_budget = self.drain_join_budget(explicit=drain_join_seconds)

        def _drain() -> None:
            try:
                note = pipeline.stop()
            except Exception:  # noqa: BLE001 - 收场失败不掩盖别的结论
                _log.exception("停转写流水线时抛错（如实登记，不静默）")
                return
            _log.info(
                "转写收尾：%s（本场共入队 %s 段、处理完 %s 段；失败 %s）",
                note["note"], note["segments_seen"], note["segments_done"],
                note["failures"] or "无",
            )
            if note["reader_errors"]:
                _log.warning("转写收尾：有读线程报过错 %s", note["reader_errors"])
            # `pipeline.stop()` 那一次收队是**在预算内尽量做完**：预算耗完时 worker 手上可能
            # 还扣着一段，它接着写出来的那一行会让 `_write_line()` 重开产物句柄 ——
            # 那个句柄此后没人关（复审 m4）。等到写行的那条线程真的退出再关一次：
            # 它退出了就再没有人能重开。等不到时它按自己的登记留着句柄、只记一条 warning。
            pipeline.close_transcripts_when_drained()

        thread = threading.Thread(target=_drain, name="asr-stop", daemon=True)
        thread.start()
        thread.join(timeout=join_budget)
        if thread.is_alive():  # pragma: no cover - 上界已与流水线预算同源，这里只兜进程退出
            _log.warning(
                "转写收尾线程在 %.1fs（收尾预算 + 余量）内没跑完（daemon，不阻塞进程退出）",
                join_budget,
            )
        worker = self._thread
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=2.0)
        self._thread = None

    def release_gpu(self) -> bool:
        """**让出会中模型占的显存**：等干净在飞的推理，丢掉模型引用，然后清一次缓存。

        生产调用点有两处：**收尾第 4 步**（`meeting/service/runtime.py` 的 `_drain_post()`）——
        R26 之后这一步不再拉起任何会后动作，把会中模型让出显存是它仅剩的活
        （`docs/design-intent.md` 第 21 条「用完即关」：录完了模型就当场退出显卡，
        与之后跑不跑会后无关），这一步受收尾那一条 deadline 约束（见下面「等的上界」）；
        以及**暂停那个动作**（同一文件的 `pause_session()`，`10770b41` 起：暂停让模型当场退出
        显存，`resume` 由配对的 `acquire_gpu()` 把它拉回来）。暂停那一处经
        `_submit_model_lifecycle()` 提交到后台的单线程执行器，不等它做完才回应答。

        逐条语义：

          - **没加载过就 no-op**：`_transcriber is None`（或它内部那两格本来就空）时当场返回
            `False`，不报错、不 import `torch`、不写日志 —— `meeting post` 那个独立入口
            （`meeting/cli.py`，归 `65dd3e4c`）的进程里根本没有会中模型，释放必须安静地什么都不做；
          - **幂等**：连调几次都安全，第二次起是上面那一格；`shutdown()` 之后再来一次也一样
            （本方法不碰 `_stopped`，也不改 `health()`：那两个是「转写这一档现在产不产出」的域，
            与「模型占不占显存」无关，混在一起会把收尾期间的健康度节拍推成 `stopped`）；
          - **有界等待**：见下面「等的上界」那一节；
          - **释放顺序**：**先**丢掉后端这一格与模型内部那两格（`GlmAsrTranscriber.processor` /
            `.transcriber`），**之后**才尝试 `torch.cuda.empty_cache()`。`torch` 在函数体内
            惰性 import（模块顶层只许标准库与包内模块，`tests/test_transcriber_backend.py` 的
            AST 判据盯着这一条）；`empty_cache()` 用 `try/except` 包住 —— 没装 `torch`、没卡、
            `empty_cache()` 自己抛错都只是「这一趟没能清缓存」，不影响上面的引用已经丢掉。
            **`_transcriber` 是唯一那条持有链**（`.processor` / `.transcriber` 是它内部的两格；
            没有 `lru_cache`、没有模块级模型缓存、worker 每段现取不缓存），丢掉引用就够了。

        **等的上界** = `RELEASE_GPU_WAIT_SECONDS`（那个常量上逐条写了为什么取 5 秒、为什么
        不用单段超时与 `drain_join_budget()`）—— 本方法**不许**等自适应收尾预算：调用它的那
        一步受收尾 deadline 约束，吃掉预算会把 `post` 从 `done` 翻成 `failed`。到点就**照旧
        丢引用**并记一条 warning，不抛、不静默。

        **为什么必须等**：worker 那一帧还在跑推理时旧模型事实上还活着，而下一段会经
        `_transcriber_or_create()` 再**造一份**（每份约 4.3 GiB 权重）—— 等着是为了不让两份
        GLM 同时在卡上，叠加 MOSSTD 的峰值才是真的越界。

        返回 `True` = 这一次真的丢掉了模型引用；`False` = 本来就没有可丢的（no-op 那一格）。
        """
        with self._model_lock:
            if not self._has_model_reference_locked():
                # 没加载过：安静地什么都不做（**连 torch 都不 import**）
                return False
            if self._gpu_released:
                # 已经释放过：幂等那一格，不重复登记、不重复清缓存
                return False
        started = time.monotonic()
        drained = self._wait_for_inflight_inference()
        waited = max(0.0, time.monotonic() - started)
        with self._model_lock:
            if self._gpu_released:  # pragma: no cover - 并发调两次时的第二格，与上面同义
                return False
            self._drop_model_references_locked()
            self._gpu_released = True
        _log.info(
            "会中模型让出显存：丢掉模型引用（等了 %.2fs；在飞推理=%s）",
            waited, "已收干净" if drained else "没等到",
        )
        cache_cleared = self._empty_cuda_cache()
        _log.info(
            "会中模型让出显存完成：模型引用与 processor 已丢，torch.cuda.empty_cache()=%s",
            "已调用" if cache_cleared else "跳过",
        )
        return True

    def acquire_gpu(self) -> bool:
        """**把会中模型重新拉起来**：与 `release_gpu()` 成对的另一半（第 21 条）。

        调用点只有一处：`meeting/service/runtime.py` 的 `resume_session()` —— 暂停期间模型已经
        退出显存（`pause_session()` 那一半调的 `release_gpu()`），继续就是「又要用它了」，
        当场把它拉起来。与释放同一条落地口径：**不在协议里**，只是这个具体后端多出来的一个
        方法，调用点用 `getattr` 防御式取它（替身后端没有这一格照样跑得通）。

        逐条语义：

          - **已经在手就 no-op**：后端那一格还握着识别器时当场返回 `False`，不重复造一份、
            不写日志 —— 反复按继续不会造出第二份识别器（每份权重约 4.3 GiB）；
          - **经既有的那条拉起路径造**：`_transcriber_or_create()` —— 与「释放之后下一段
            音频要转写时」走的是同一个 factory、同一把 `_model_lock`（释放半路进来的这一段
            与它互斥），于是「继续那一刻造出来的」与「下一段真要转写时造出来的」同源同形；
          - **造不出来不抛**：factory 抛错（依赖没装 / 权重不在本地缓存 / 显存不够）时如实记
            一条 warning 并返回 `False` —— **继续这个动作不许因为模型拉不起来而失败**
            （R1 的立场：转写坏了可以重跑，录音照跑）。下一次真出段时照旧经同一条路再试，
            那一档的失败由 `_transcribe_segment()` 记进 `_model_error`（`health()` 落 `stopped`）；
          - **不碰 `_stopped`，也不动 `health()` 的读数**：与释放同一条边界 —— 那两个是
            「转写这一档现在产不产出」的域，与「模型在不在手上」无关。

        **权重的真实加载仍在第一次转写时**（`GlmAsrTranscriber` 是惰性加载：它的私有入口
        `meeting/asr/glm.py` 的 `_load_transcriber()` 只在 `transcribe_segment()` 里被调）：
        本方法把识别器交回手上，权重的加载沿既有那条惰性路径发生。本节点做到的是
        「暂停期间不占显存」与「继续之后模型回到手上、下一次转写照旧经它」，没有在这里
        越过模块边界去调那个私有加载入口。

        返回 `True` = 这一次真的重造了一份识别器；`False` = 本来就在手（no-op 那一格）。
        """
        with self._model_lock:
            if self._transcriber is not None:
                # 已经在手：安静地什么都不做（与释放那一格的 no-op 同姿态）
                return False
        try:
            self._transcriber_or_create()
        except Exception:  # noqa: BLE001 - 拉不起来不许把「继续」这动作拖失败
            _log.warning(
                "会中模型没能重新拉起（如实登记，不静默；下一次出段时照旧经同一条路再试一遍）",
                exc_info=True,
            )
            return False
        _log.info("会中模型已重新拉起：识别器经同一个 factory 重造了一份（继续之后接着用）")
        return True

    def _has_model_reference_locked(self) -> bool:
        """现在手上真的握着模型引用吗（`release_gpu()` 判 no-op 的那一格）。"""
        if self._transcriber is None:
            return False
        return any(
            getattr(self._transcriber, name, None) is not None
            for name in ("processor", "transcriber")
        )

    def _wait_for_inflight_inference(self) -> bool:
        """等在飞的推理收干净（事件驱动，上界 `RELEASE_GPU_WAIT_SECONDS`）。

        判据取**在飞推理的计数回到 0**：「现在没有任何一帧还握着旧模型」这件事，正面证据只有
        它。worker 是「先 `get()` 再处理」的长驻线程（`meeting/asr/pipeline.py` 的
        `_worker_loop()`），它活着的时候大多正等在 `get()` 上、手里空空；而
        `TranscriberBackend.stop()` 在生产路径上是**早退空转**（`self._thread` 从没被赋过线程
        对象），拿它当释放手段既等错了对象、又会走那笔自适应收尾预算。

        等待经 `Condition.wait(timeout=…)` 唤醒 —— `_transcribe_segment()` 每段结束都会通知，
        因此没有轮询、没有固定 `sleep`。等不到（上界到点）时返回 `False`，由调用方如实记一条
        warning 并照旧丢引用。
        """
        remaining = max(0.0, float(RELEASE_GPU_WAIT_SECONDS))
        with self._inflight_cv:
            while self._inflight > 0 and remaining > 0:
                began = time.monotonic()
                self._inflight_cv.wait(timeout=remaining)
                remaining -= max(0.0, time.monotonic() - began)
            if self._inflight <= 0:
                return True
        _log.warning(
            "会中模型在 %.1fs 内没等到在飞推理收干净（还有 %s 段在跑）：照旧丢掉引用并"
            "请求清缓存 —— 那一段所在的那一帧可能还握着旧模型，如实登记，不静默也不抛",
            float(RELEASE_GPU_WAIT_SECONDS), self._inflight,
        )
        return False

    def _drop_model_references_locked(self) -> bool:
        """丢掉模型引用的三格（后端这一格 + `GlmAsrTranscriber` 内部那两格）。"""
        model = self._transcriber
        if model is None:
            return False
        dropped = False
        for name in ("processor", "transcriber"):
            if getattr(model, name, None) is not None:
                try:
                    setattr(model, name, None)
                except Exception:  # noqa: BLE001 - 只读替身上丢不掉这一格，不影响别的
                    _log.warning("丢掉模型内部那一格 %s 失败（如实登记，不静默）", name, exc_info=True)
                else:
                    dropped = True
        self._transcriber = None
        return dropped

    def _empty_cuda_cache(self) -> bool:
        """尽力清一次显存缓存（**必须在丢掉引用之后**）：无 `torch` / 无卡 / 自身抛错都只是跳过。

        `torch` 在函数体内惰性 import（顶层不许有重依赖），并且**先看 `sys.modules`**：
        释放这件事不该为了清一次缓存去把一个几百兆的重依赖 import 进来。
        """
        torch = sys.modules.get("torch")
        if torch is None:
            return False
        try:
            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001 - 无卡机器上它必须是安全的 no-op
            _log.warning("torch.cuda.empty_cache() 没做成（如实登记，不静默）", exc_info=True)
            return False
        return True

    def drain_join_budget(self, *, explicit: float | None = None) -> float:
        """`stop()` 等流水线收尾的上界（秒）：**与那笔收尾预算同源，且盖得住内层最坏档**。

        默认 = `compute_drain_budget(backlog_depth() + flush_tail_segments(), segment_timeout())`
        （公式只此一份，住在 `meeting/asr/pipeline.py`，`meeting/service/runtime.py` 的那一个
        同名函数转交给它）+ `DRAIN_STOP_SLACK_SECONDS` + `drain_inner_slack()`。
        积压越多、单段超时越长，这个上界就越大（封顶 300 + 余量）—— 「上界随积压增长」这条判据
        由用例 `test_stop_join_budget_follows_the_pipeline_drain_budget` 钉住
        （受控破坏：把它换回写死的 5.0 → 那条用例红）。

        **为什么积压量要自己加上 `flush_tail_segments()`**（复审 m2）：外层在原处读
        `backlog_depth()`，而内层 `pipeline.stop()` 是**先 `vad.flush()` 把每路手上那一段
        入队、之后**才读 `qsize()` 算自己的预算 —— 两次读的是不同时刻的积压量，尾段只进内层。
        最坏档（模型卡住 + 关停时两路都还压着尾巴）于是内层预算比外层大
        `每多一段 × (单段超时 + 5)`（默认 25 秒/段），外层先超时 → 打出一条**假的**
        「收尾线程没跑完」warning，并且在流水线还在收尾时就返回（与 `stop()` docstring 的
        「返回即收队完成」相反，末尾那句「还有 N 段没做完」也没机会落 log）。
        外层按「积压 + 每路最多一段尾段」预留之后，这一档的上界必然盖得住内层。
        这一条由用例 `test_stop_join_budget_covers_the_vad_flush_tail` 钉住
        （受控破坏：把这一项去掉 → 那条用例按「读数差」当场红）。

        读 **`backlog_depth() + flush_tail_segments()`** 而不是只读积压量：两者在同一个方法里
        一起算出来，中间不留「读完之后内层又长了」的缝。

        `pipeline` 还没起来（或已经被上一场收走）时按下限那一档算：那一刻没有积压要做。

        **为什么还要加 `drain_inner_slack()`**（复审 m5）：预算那 1 秒余量是给 worker 的，
        而内层 `pipeline.stop()` 在 worker join **之前**还要逐路 join 读线程（每路最多
        `READER_JOIN_TIMEOUT_SECONDS`）再送出 VAD 尾段。不把这一段算进来的话，最坏档外层
        会先超时：打出一条假的「收尾线程没跑完」，并且在流水线还在收尾时就返回。
        「内层用满预算时外层不许误报」这条关系由用例
        `test_stop_join_budget_covers_a_reader_tail_that_uses_its_whole_budget` 钉住
        （受控破坏：把 `drain_inner_slack()` 改回 0.0 → 那条用例红）。
        """
        if explicit is not None:
            return max(0.0, float(explicit))
        pipeline = self._pipeline
        if pipeline is None:
            return float(DRAIN_STOP_SLACK_SECONDS)
        backlog = int(self.backlog_depth()) + self.flush_tail_segments()
        return float(
            pipeline_module.compute_drain_budget(backlog, float(self.segment_timeout()))
        ) + float(DRAIN_STOP_SLACK_SECONDS) + self.drain_inner_slack()

    def flush_tail_segments(self) -> int:
        """`pipeline.stop()` 里那次 VAD 尾段 flush 最多能**多入队**几段（复审 m2）。

        内层顺序是：关读线程 → 逐路 `vad.flush()`（手上还压着的那一段强行送出、入队）
        → **之后**才读 `qsize()` 算收尾预算。于是「算预算用的积压量」最多比外层读到的多
        **每路一段**（`TranscriptionPipeline.stop()` 里那个 `for source in self.sources` 循环，
        每路只 flush 一次）。这里按**本场真的在读的那几路**算，与外层余量里读线程那一项的
        路数口径一致；拿不到就按两路的上界算 —— 余量宁可多留，不许少留。
        """
        sources = getattr(self._pipeline, "sources", None)
        return len(sources) if sources else len(SOURCE_IDS)

    def drain_inner_slack(self) -> float:
        """`pipeline.stop()` 在 worker join **之前**可能花掉的上界（秒）。

        = 读线程收尾上界 × 本场路数：`TranscriptionPipeline.stop()` 先 `_join_readers()`
        （逐路最多 `READER_JOIN_TIMEOUT_SECONDS`）、再送 VAD 尾段、然后才
        `worker.join(budget + DRAIN_JOIN_SLACK_SECONDS)`。后半截与外层同源，只有前半截是
        外层的额外开销，因此它单独成一项加进 `drain_join_budget()`。

        路数取**这一场真的在读的那几路**（`pipeline.sources`，`mode=mic` 时是一路）；
        拿不到就按两路的上界算 —— 余量宁可多留，不许少留。
        """
        sources = getattr(self._pipeline, "sources", None)
        routes = len(sources) if sources else len(SOURCE_IDS)
        return float(pipeline_module.READER_JOIN_TIMEOUT_SECONDS) * int(routes)

    def health(self) -> str:
        """`normal` / `behind` / `stopped` 三态（直接进快照的 `transcript.health`）。

        判定顺序与依据：

          1. **`stopped`**：已经 `stop()` 过、或 worker 没起来 / 已经停了、或模型加载 /
             推理失败过（`_model_error` 非空）—— 「转写这一档现在不产出」如实报出来；
          2. **`behind`**：积压段数压过 `BACKLOG_BEHIND_THRESHOLD`（取值依据写在
             `meeting/asr/pipeline.py` 那个常量上）；
          3. **`normal`**：起过、worker 与读线程都活着、模型没报过错。
        """
        if self._stopped or not self._started:
            return "stopped"
        pipeline = self._pipeline
        if pipeline is None or self._model_error:
            return "stopped"
        if not pipeline.worker_alive():
            return "stopped"
        if pipeline.backlog_depth() > pipeline.behind_threshold:
            return "behind"
        if not pipeline.readers_alive():
            # 读线程出事：这一路不会再产出新段（worker 本身还在跑，但流水线已经残了）
            return "behind"
        return "normal"

    def backlog_depth(self) -> int:
        """队列里还没处理的段数。"""
        pipeline = self._pipeline
        if pipeline is None:
            return 0
        return int(pipeline.backlog_depth())

    def segment_timeout(self) -> float:
        """单段超时（秒）：配置里的 `asr.timeout_seconds`（默认 20.0）。"""
        if self._segment_timeout is not None:
            return float(self._segment_timeout)
        self._segment_timeout = self._read_timeout()
        return float(self._segment_timeout)

    # -- 内部 -------------------------------------------------------------

    def _read_timeout(self) -> float:
        """读一次 `asr.timeout_seconds`（读不出来 / 非正数就落回缺省，不抛错）。"""
        value: Any = None
        config = self._config
        getter = getattr(config, "get", None)
        try:
            if callable(getter):
                value = getter("asr.timeout_seconds")
            elif config is None:
                value = config_module.get("asr.timeout_seconds")
        except Exception:  # noqa: BLE001 - 配置视图出事不阻塞转写
            _log.warning("读 asr.timeout_seconds 失败，按缺省 %ss 算", DEFAULT_SEGMENT_TIMEOUT_SECONDS)
            value = None
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            return DEFAULT_SEGMENT_TIMEOUT_SECONDS
        if timeout <= 0:  # pragma: no cover - 配置层已经保证正数
            return DEFAULT_SEGMENT_TIMEOUT_SECONDS
        return timeout

    def _build_pipeline(self) -> pipeline_module.TranscriptionPipeline:
        """建一条流水线（**这一档会 import 重依赖**：`numpy` / `webrtcvad`）。"""
        kwargs: dict[str, Any] = {
            "transcriber": _LazyGlmTranscriber(self),
            "line_sink": _SinkProxy(self),
            "segment_timeout": self.segment_timeout(),
        }
        if self._vad_factory is not None:
            kwargs["vad_factory"] = self._vad_factory
        if self._budget_fn is not None:
            kwargs["budget_fn"] = self._budget_fn
        if self._read_chunk_bytes is not None:
            kwargs["read_chunk_bytes"] = self._read_chunk_bytes
        return pipeline_module.TranscriptionPipeline(**kwargs)

    def _deliver(self, line: Any) -> None:
        """把一行交给 runtime 包好的出口（出口为 `None` 时只记一条 warning）。"""
        sink = self._line_sink
        if sink is None:
            _log.warning("转写还没有接上出口，这一行只能留在本地：%r", line)
            return
        sink(line)

    def _transcribe_segment(self, audio: Any) -> Any:
        """调 phase1 的单段识别（**模型加载失败在这里如实留痕**）。

        `GlmAsrTranscriber` 的惰性加载在第一次 `transcribe_segment` 时发生：模型依赖没装、
        权重不在本地缓存、显存不足都会在这里抛。这一支把原因记进 `_model_error`
        （`health()` 因此落 `stopped`），再把异常原样上抛给流水线 —— 那一段落占位符，
        **绝不静默丢段、也绝不落半截文本**。

        整段包在**在飞计数**里（`release_gpu()` 等的就是它回到 0）：这一个是 worker 唯一
        握着模型的那一帧，计数在飞期间释放会造出第二份模型（每份约 4.3 GiB 权重）。
        """
        with self._inflight_cv:
            self._inflight += 1
        try:
            try:
                transcriber = self._transcriber_or_create()
                result = transcriber.transcribe_segment(audio)
            except Exception as exc:  # noqa: BLE001 - 原因要留在 log 与 health 里，异常照旧上抛
                if not self._model_error:
                    _log.warning("模型这一侧出事（health 落 stopped）：%s", type(exc).__name__, exc_info=True)
                self._model_error = f"{type(exc).__name__}: {exc}"
                raise
        finally:
            # 收工就通知等在这一格上的 `release_gpu()`（事件驱动，它不轮询）
            with self._inflight_cv:
                self._inflight -= 1
                self._inflight_cv.notify_all()
        self._model_error = ""
        return result

    def _transcriber_or_create(self) -> Any:
        """现取识别器（第一次真的要用时才造一个，见 `_LazyGlmTranscriber`）。

        造新的这一格与 `release_gpu()` 的释放互斥（同一把 `_model_lock`）：释放半路进来的
        一段会在这里再取一次，两件事不交叠就不会同时出现两份模型。新造出来的这一份把
        「已经释放过」那一格复位 —— 释放过一轮之后再加载的模型，下一轮释放照样管得着。
        """
        with self._model_lock:
            if self._transcriber is None:
                factory = self._transcriber_factory
                self._transcriber = factory() if factory is not None else glm_module.GlmAsrTranscriber()
                self._gpu_released = False
            return self._transcriber


class _LazyGlmTranscriber:
    """`SegmentTranscriber` 的一层薄壳：**第一次真要识别时才造识别器**。

    分开这一层的理由有两条：`start()` 里不 import `torch` / `transformers`（于是
    「没装模型依赖的机器上启动链照常」成立），以及用例可以只注入 `transcriber_factory`
    就把真模型换掉，而不必碰流水线。
    """

    def __init__(self, backend: TranscriberBackend) -> None:
        self._backend = backend

    def transcribe_segment(self, audio_int16: Any) -> Any:
        return self._backend._transcribe_segment(audio_int16)


__all__ = [
    "DEFAULT_SEGMENT_TIMEOUT_SECONDS",
    "DRAIN_STOP_SLACK_SECONDS",
    "RELEASE_GPU_WAIT_SECONDS",
    "TRANSCRIBER_TARGET",
    "TranscriberBackend",
]
