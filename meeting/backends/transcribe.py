"""转写后端 —— 把 `meeting/asr/pipeline.py` 那条流水线包成协议实现。

本模块即 `transcriber.backend` 默认值 `auto` 解析到的实现；构造函数**不许有必填参数**
（registry 无参实例化），运行期依赖走 `bind()`。**没装模型依赖的机器上必须 import 成功**：
顶层只有标准库与轻模块，重依赖全在函数体内惰性 import。

协议成员：`bind` / `start`（起步失败不阻塞录音）/ `stop` / `health`（三态）/
`backlog_depth` / `segment_timeout`；`release_gpu` / `acquire_gpu` 不在协议里，是这个后端
多出来的方法，调用点用 `getattr` 防御式取它——「要用才拉起来，用完立刻关掉」
模型加载走 `meeting.asr.glm._load_models` 私有接缝：经**模块对象**
取用，别写 `from meeting.asr.glm import _load_models` 那种绑到另一个模块对象的引用。
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

#: `asr.timeout_seconds` 读不出来时的缺省（与 `meeting/config.py` 的默认值、与设计的口径一致）
DEFAULT_SEGMENT_TIMEOUT_SECONDS = 20.0

#: `stop()` 那条收尾线程 join 的额外余量（秒），加在收尾预算之上。余量要盖住**内层最坏耗时**：
#: 内层 `pipeline.stop()` 在 worker join 之前还要逐路 join 读线程（每路最多
#: `READER_JOIN_TIMEOUT_SECONDS`，两路 4 秒）——外层只留 1 秒的话最坏档会先超时，打出一条
#: **假的**「收尾线程没跑完」并在流水线还在收尾时就返回。那 1 秒照旧留着：
#: 它是「预算耗完那一刻 worker 手上还没做完的那一段」。
DRAIN_STOP_SLACK_SECONDS = 1.0

#: `release_gpu()` 等在飞推理收干净的上界（秒）。会中模型由进程级单例长期持有，录完就该让出
#: 显存（收尾第 4 步，「用完即关」）。丢引用前要等在飞的推理收干净，
#: 否则下一段会再造一份模型——两份 GLM 同时在卡上（每份约 4.3 GiB 权重）。**上界只能是小值**：
#: 收尾那一步受 deadline 约束，20 秒（单段超时）与自适应收尾预算都会顶穿它、把 `post` 翻成
#: `failed`。已加载模型的单段推理正常 1–3 秒，5 秒盖得住；真卡住就到点丢引用 + 记 log。
RELEASE_GPU_WAIT_SECONDS = 5.0

#: 完成判定点名的那条实现路径（只在文档与用例里引用，不在运行时使用）
TRANSCRIBER_TARGET = "meeting.backends.transcribe:TranscriberBackend"


class _SinkProxy:
    """行出口的**间接层**：把「后端 → 出口」这一步推迟到每一行真正到来的时候。

    为什么需要它：runtime 在 `probe()` **之前**就 `bind()` 了一次（那时还没有会话），
    会话起来之后还会 `bind()` 第二次；流水线的 worker 线程则可能在后端被重新 `bind()` 之后
    才算出第一行。worker 每次经这里现取一次后端当前的 `line_sink`，于是「绑定时刻」与
    「产出时刻」谁先谁后都不影响那一行被送出去。
    """

    def __init__(self, backend: "TranscriberBackend") -> None:
        self._backend = backend

    def __call__(self, line: Any) -> None:
        self._backend._deliver(line)


class TranscriberBackend:
    """真转写后端：游标尾读归档 PCM → VAD 切段 → 单 worker 顺序识别 → 落盘 + 行出口。

    两路各一条读线程（`meeting/asr/pipeline.py` 的 `tail_archive_segments()`），一个 worker 顺序消费
    两路的段 —— 采集与模型因此解耦到「模型停摆不影响落盘」。
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

        **这一步是同步的、会占住调用方线程**：有上一场没被收走时，这里要等它
        那笔收尾预算跑完（下限 30 秒、封顶 300 秒 + 读线程收尾上界），而调用方是事件循环上的
        `ServiceRuntime.start_session()` —— 于是「再开一场」这个动作最长要等到那时候才应答。
        触发时机与上界逐条写在 `_retire_previous()` 的 docstring。

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
        """把上一场的流水线**同步**收干净（幂等；没有上一场什么都不做）。

        这是同步收尾的第二个调用点（`start()` 第一句）。上一场若没被收走
        （`stop` 动作只收采集、生产路径上 `stop()` 只有 `shutdown()` 一个调用点），
        这里就在**调用方线程**（事件循环上的 `start_session()`）上把上一场收掉——
        「再开一场」在收尾跑完之前不应答。上界 = `pipeline.stop()` 自己那笔预算
        （下限 30、封顶 300 秒）+ 读线程收尾上界 × 路数（最坏约 304 秒），与 `stop()`
        同一量级同一取舍。不异步化：换场必须**先**收干净，否则上一场读线程继续轮询
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

        **同步等待**：收尾放一条 daemon 线程（`asr-stop`），本方法
        在调用方线程里 `join` 它——关停期间事件循环确实被占住（心跳、SSE、动作端点都不
        应答）。这是有意的取舍：此刻服务正在离场，短一点会丢临时稿尾巴。完整口径见
        应答）。这是有意的取舍：此刻服务正在离场，短一点会丢临时稿尾巴。

        本方法等的上界由 `drain_join_budget()` 算：流水线收尾预算（下限 30、封顶 300，
        **尾段那一项已含在预算里**）+ `DRAIN_STOP_SLACK_SECONDS` + 读线程收尾上界
        × 路数。空闲态下限 35 秒、最坏 305 秒。这一条必须存在：本方法是生产路径上
        `transcriber_backend.stop()` 的唯一调用点，写死小常量等于把「预算内尽量做完」
        打成空头支票——预算没花完进程就退了。

        `drain_join_seconds` 是显式的更小上界（给「必须马上退」的调用方用）。
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
            # 那个句柄此后没人关。等到写行的那条线程真的退出再关一次：
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

        生产调用点：收尾第 4 步与暂停动作（起，
        `resume` 由配对的 `acquire_gpu()` 拉回；那一处经后台单线程执行器提交，不等做完）。

        - **没加载过就 no-op**（连 torch 都不 import）：`meeting post` 那个独立入口的进程里
          根本没有会中模型，释放必须安静地什么都不做；
        - **幂等**：第二次起返回 `False`；不碰 `_stopped`、不改 `health()`——那是「转写这一档
          现在产不产出」的域，与「模型占不占显存」无关，混着会把收尾期间健康度推成 `stopped`；
        - **释放顺序**：先丢三格引用、**之后**才 `empty_cache()`（`torch` 惰性 import；
          `_transcriber` 是唯一持有链，丢引用就够了）；
        - **有界等待**：上界 `RELEASE_GPU_WAIT_SECONDS`，**不许**等自适应收尾预算（调用方
          那一步受收尾 deadline 约束）；到点照旧丢引用并记 warning，不抛、不静默。

        返回 `True` = 真的丢掉了模型引用。
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
        """**把会中模型重新拉起来**：与 `release_gpu()` 成对的另一半。

        调用点只有 `resume_session()`（暂停期间模型已退出显存）。不在协议里，调用点
        `getattr` 防御式取它（替身后端没有这一格照样跑得通）。

        - **已经在手就 no-op**：反复按继续不会造出第二份识别器（每份约 4.3 GiB）；
        - **经 `_transcriber_or_create()` 造**：与释放同一把 `_model_lock` 互斥，
          「继续那一刻造的」与「下一段真要转写时造的」同源同形；
        - **造不出来不抛**：继续这个动作不许因为模型拉不起来而失败；下一出段照旧
          经同一条路再试，失败由 `_transcribe_segment()` 记进 `_model_error`；
        - 不碰 `_stopped`、不动 `health()`（与释放同一条边界）。

        权重的真实加载仍在第一次转写时（`GlmAsrTranscriber` 惰性加载）；本方法只把识别器
        交回手上，不越模块边界去调那个私有加载入口。
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
        """现在手上真的握着模型引用吗（`release_gpu()` 判 no-op 的那一格）。

        判据问模型自己（`has_model_references()`）：后端不按字段名戳它的内部。
        模型对象没声明这一格时按「握着」算——释放照常往下走。
        """
        if self._transcriber is None:
            return False
        teller = getattr(self._transcriber, "has_model_references", None)
        if callable(teller):
            return bool(teller())
        return True

    def _wait_for_inflight_inference(self) -> bool:
        """等在飞的推理收干净（事件驱动，上界 `RELEASE_GPU_WAIT_SECONDS`）。

        判据取**在飞计数回到 0**：「没有任何一帧还握着旧模型」的正面证据只有它。worker
        大多正等在 `get()` 上、手里空空；`stop()` 在生产路径上是早退空转（`_thread` 从没
        被赋过线程对象），拿它当释放手段既等错对象又走那笔收尾预算。等待经
        `Condition.wait()` 唤醒（每段结束都通知），没有轮询。等不到返回 `False`。
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
        """丢掉模型引用（后端这一格 + 模型对象经公开释放方法丢它内部那几格）。

        释放走模型自己的 `release_model_references()`：内部布局归模型类管，后端不再按
        字段名 `setattr`。模型对象没声明这个方法时只丢后端这一格并如实登记（不静默）。
        """
        model = self._transcriber
        if model is None:
            return False
        dropper = getattr(model, "release_model_references", None)
        dropped = False
        if callable(dropper):
            try:
                dropped = bool(dropper())
            except Exception:  # noqa: BLE001 - 释放失败照旧丢后端这一格，不掩盖
                _log.warning("模型对象丢内部引用时抛错（如实登记，不静默）", exc_info=True)
        else:
            _log.warning(
                "模型对象没有声明 release_model_references()：只丢了后端这一格（如实登记，不静默）"
            )
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

        默认 = `compute_drain_budget(backlog + 尾段上界, 单段超时)`（公式只此一份）
        + `DRAIN_STOP_SLACK_SECONDS` + `drain_inner_slack()`：

        - **尾段那一份**：内层 `pipeline.stop()` 是先 `vad.flush()` 把每路手上
          那一段入队**之后**才读 `qsize()` 算自己的预算——两次读的是不同时刻的积压量。
          不预留的话最坏档外层先超时，打出假的「收尾线程没跑完」；
        - **读线程收尾那一份**：内层在 worker join 之前逐路 join 读线程，
          不算进来的话外层同样先超时；
        - **上界随积压增长**。

        两条余量读 **`backlog_depth() + flush_tail_segments()`** 一起算，中间不留
        「读完之后内层又长了」的缝。`pipeline` 没起来时按下限档算。
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
        """`pipeline.stop()` 里那次 VAD 尾段 flush 最多能**多入队**几段。

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
        """单段超时（秒）：配置里的 `asr.timeout_seconds`（默认 20.0）。

        **每问每读**（热档）：`asr.timeout_seconds` 是热读键，glm 那一侧每段现读；
        预算与识别取同一档热度，改配置后两边才不会分叉（识别按新值、预算按旧值）。
        """
        return self._read_timeout()

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
        """调设计的单段识别（**模型加载失败在这里如实留痕**）。

        惰性加载的失败（依赖没装、权重不在缓存、显存不足）在这里抛：原因记进 `_model_error`
        （`health()` 落 `stopped`），异常原样上抛给流水线——那段落占位符，不静默丢段、
        不落半截文本。整段包在**在飞计数**里（`release_gpu()` 等它回到 0）：这是 worker
        唯一握着模型的那一帧，在飞期间释放会造出第二份模型。
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
