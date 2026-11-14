"""实时转写流水线 —— 游标尾读归档 PCM → VAD 切段 → 入队盖章 → 独立 worker → 落盘 + 行出口。

**音频从哪来**：采集只管往 `session_dir/meeting_audio_<source>_<段号>.pcm` 写
（`buffering=0`，写盘立刻可见；段拼起来 = 连续时间轴，暂停区间是数字零）；转写侧每路一条
读线程按游标**逐段**读新字节，不碰采集层任何对象；**时间戳取「读到这一块那一刻」的墙钟**
并随段入队——处理慢不会把后面段的时间戳挤到一起。

**VAD 切段**：帧长 30ms、静默 2.0 秒出段、单段硬上限按配置 `asr.max_segment_seconds`
（默认 10 秒，超了立即出段再续不丢段），每 0.5 秒热读一次上限。**队列无界**（禁止
busy-drop）；**一个** worker 顺序消费两路（显存只有一份）。**落盘**：每路
`meeting_transcript_<source>.md`，追加模式、逐行 flush，行格式 `[HH:MM] 文本`。

**续接**：临时稿已在场时开场各路追加一条 `<!-- … -->` 记号行、各推一条 `kind="gap"` 行；
读头从盘上最大段号接起，旧段一个字节不重读。

本模块顶层只有标准库与包内 import（`numpy` / `webrtcvad` 惰性 import），没装模型依赖的
机器上 `meeting.backends.transcribe` 仍能 import 成功。
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from datetime import datetime
from typing import Any, Callable, Protocol, runtime_checkable

from meeting import config as config_module
from meeting.asr import glm as glm_module
from meeting.backends.base import SOURCE_IDS, TranscriptLine
# 归档 PCM 的形状只认**一个口径来源**：`meeting/capture/archive.py`——这里不再抄第二份，
# 防两处各自漂移。
from meeting.capture.archive import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH
from meeting.session import archive_name, next_segment_number

_log = logging.getLogger("meeting.asr.pipeline")

#: `webrtcvad` 只认 10 / 20 / 30
FRAME_MILLISECONDS = 30

FRAME_BYTES = SAMPLE_RATE * FRAME_MILLISECONDS // 1000 * SAMPLE_WIDTH

SILENCE_TIMEOUT_SECONDS = 2.0

#: 段长上限的重读间隔（秒）：`asr.max_segment_seconds` 是热读键，改配置无需重建 VAD 实例
VAD_TUNABLE_TTL_SECONDS = 0.5

DEFAULT_READ_CHUNK_BYTES = SAMPLE_RATE * SAMPLE_WIDTH // 2

DEFAULT_ARCHIVE_POLL_SECONDS = 0.05

DEFAULT_TAIL_IDLE_SECONDS = 0.02

WORKER_IDLE_SECONDS = 0.1

DRAIN_JOIN_SLACK_SECONDS = 1.0

#: 收尾时**每一路读线程** join 的上界（秒）：`_join_readers()` 逐路等这么久，超时就落一条
#: warning 并往下走（daemon 线程，不阻塞进程退出）。必须是**有名字的常量**：外层
#: `meeting/backends/transcribe.py` 的 `stop()` 要同步等这条收尾线程，上界得把这一项
#: 按路数算进去。
READER_JOIN_TIMEOUT_SECONDS = 2.0

TRANSCRIPT_FILE_TEMPLATE = "meeting_transcript_{source}.md"

RESUME_GAP_TEXT = "续接：上面是上一段的稿子，两段之间有一段没录"

#: 转写健康度 `behind` 的**积压门槛**（段）：低于它只是正常滞后，报 `behind` 会天天亮灯。
BACKLOG_BEHIND_THRESHOLD = 20

_CLOSE_SENTINEL = object()

# **公式住在这里**（转写是积压量的来源），`meeting/service/runtime.py` 的
# `compute_drain_budget()` 直接转交——两处各写一份就成了两条会漂移的口径。

DRAIN_BUDGET_MIN_SECONDS = 30

DRAIN_BUDGET_MAX_SECONDS = 300

DRAIN_BUDGET_SEGMENT_OVERHEAD_SECONDS = 5


def compute_drain_budget(backlog: int, segment_timeout: float) -> float:
    budget = DRAIN_BUDGET_MIN_SECONDS + int(backlog) * (
        float(segment_timeout) + float(DRAIN_BUDGET_SEGMENT_OVERHEAD_SECONDS)
    )
    return float(min(DRAIN_BUDGET_MAX_SECONDS, max(DRAIN_BUDGET_MIN_SECONDS, budget)))

#: VAD 已丢不足半秒的段，这里兜的是「硬上限切出的尾段只有一两帧」那一档
MIN_SEGMENT_SECONDS = 0.5

FAILURE_LABELS: dict[str, str] = {
    "timeout": "识别失败: 超时",
    "loop": "识别失败: 重复循环",
    "empty": "识别失败: 无输出",
    "error": "识别失败: 错误",
}

DEFAULT_FAILURE_LABEL = "识别失败"


#: 转写健康度的取值域（`web/src/contract/primitives.ts` 的 `TRANSCRIPT_HEALTHS` 逐字同源，
#: 快照 `transcript.health` 只落这三档）
TRANSCRIPT_HEALTH: tuple[str, ...] = ("normal", "behind", "stopped")

#: 转写健康度的兜底档：`stopped` 在「后端行为不可信」时说的是实话（`behind` 会说成「落后」）
TRANSCRIPT_HEALTH_FALLBACK = "stopped"


def validate_transcript_health(value: Any) -> str:
    """把后端报上来的健康度收进**转写域**的三取值；越界落 `TRANSCRIPT_HEALTH_FALLBACK`。

    **必须与采集域的 `meeting.state.validate_health()` 分开**（那是七取值的探针域，越界落
    `probe_failed`）。
    """
    text = str(value)
    return text if text in TRANSCRIPT_HEALTH else TRANSCRIPT_HEALTH_FALLBACK


@runtime_checkable
class SegmentTranscriber(Protocol):
    """worker 认的转写接口：生产实现是 `meeting.asr.glm.GlmAsrTranscriber`
    （`transcribe_segment(int16 数组) -> TranscribeResult`）；测试注入自己的替身（假模型），
    **任何用例都不许加载真模型**。
    """

    def transcribe_segment(self, audio_int16: Any) -> Any: ...


def _format_failure(reason: str, duration: float = 0.0, timed_out: bool = False) -> str:
    """把一条失败结果格式化成落盘占位符：双层方括号、裸文本、**绝不含 `**`**
    （否则下游把 `**X**:` 当成说话人标签）。
    """
    label = FAILURE_LABELS.get(reason, DEFAULT_FAILURE_LABEL)
    if timed_out and duration:
        return f"[[{label} {duration:.0f}s]]"
    return f"[[{label}]]"


class VoiceActivitySegmenter:
    """一路的 VAD 切段器：吃字节、吐完整段（`feed()` 的 `emit` 出口每次给一段）。

    **VAD 后端是构造参数**（缺省 `webrtcvad.Vad(1)`，库在函数体内惰性 import）：
    测试注入一个确定性的替身，用例因此不依赖 `webrtcvad` 的判定细节、也不跑真模型。
    """

    def __init__(
        self,
        *,
        silence_timeout: float = SILENCE_TIMEOUT_SECONDS,
        vad_backend: Any = None,
        max_segment_seconds: float | None = None,
        ttl: float = VAD_TUNABLE_TTL_SECONDS,
    ) -> None:
        self.silence_timeout = float(silence_timeout)
        self._vad = _default_vad() if vad_backend is None else vad_backend
        self._ttl = float(ttl)
        self._segment_buffer = bytearray()
        self._buffer = bytearray()
        self._last_speech = 0.0
        self._pinned_cap = max_segment_seconds is not None
        self._cap_seconds = (
            float(max_segment_seconds)
            if max_segment_seconds is not None
            else float(config_module.get("asr.max_segment_seconds"))
        )
        self._cap_checked = 0.0

    @property
    def cap_bytes(self) -> int:
        return max(1, int(self._cap_seconds)) * SAMPLE_RATE * SAMPLE_WIDTH

    def _refresh_cap(self, now: float) -> None:
        if self._pinned_cap or now - self._cap_checked <= self._ttl:
            return
        self._cap_checked = now
        try:
            self._cap_seconds = float(config_module.get("asr.max_segment_seconds"))
        except Exception:  # noqa: BLE001 - 配置读不出来就沿用上一次的取值，不打断切段
            _log.warning("读 asr.max_segment_seconds 失败，沿用上一次的段长上限", exc_info=True)

    def feed(self, data: bytes, emit: Callable[[bytes], None], now: float) -> None:
        if not data:
            return
        self._buffer.extend(data)
        self._refresh_cap(now)

        while len(self._buffer) >= FRAME_BYTES:
            frame = bytes(self._buffer[:FRAME_BYTES])
            del self._buffer[:FRAME_BYTES]

            if self._vad.is_speech(frame, SAMPLE_RATE):
                self._last_speech = now
                self._segment_buffer.extend(frame)
            elif self._segment_buffer:
                self._segment_buffer.extend(frame)
                if now - self._last_speech > self.silence_timeout:
                    self._emit(emit)
            if len(self._segment_buffer) >= self.cap_bytes:
                self._emit(emit)

    def flush(self, emit: Callable[[bytes], None]) -> None:
        self._emit(emit)

    @property
    def has_pending(self) -> bool:
        return bool(self._segment_buffer)

    def _emit(self, emit: Callable[[bytes], None]) -> None:
        if not self._segment_buffer:
            return
        segment = bytes(self._segment_buffer)
        self._segment_buffer = bytearray()
        self._last_speech = 0.0
        emit(segment)


def _default_vad() -> Any:
    import webrtcvad

    return webrtcvad.Vad(1)


def tail_archive_segments(
    segment_path: Callable[[int], str | None],
    *,
    stop: threading.Event,
    on_chunk: Callable[[bytes, float], None],
    chunk_bytes: int = DEFAULT_READ_CHUNK_BYTES,
    idle_seconds: float = DEFAULT_TAIL_IDLE_SECONDS,
    archive_poll_seconds: float = DEFAULT_ARCHIVE_POLL_SECONDS,
    first_seen: Callable[[str, int], None] | None = None,
    start_index: int = 1,
) -> None:
    """按**段序号**尾读一路归档：从 `start_index` 起逐段接续，拼起来就是连续时间轴。

    - `start_index` 是读头的**起步段号**（缺省 1）：续接（同一工作区再录一段）的
      一场从这里直接接到**本段**——旧段的字节一个都不重读，临时稿里旧正文不会被
      抄第二遍；
    - `segment_path(index)` 交回第 `index` 段的路径；交回 `None` 表示**不会再有下一段**；
    - 每一块的盖章口径在 `_tail_one_segment()`：`on_chunk(data, time.time())`，读到那一刻
      的墙钟。跨段不换章：续接留下的时间洞在这里只是「EOF 等了很久」；
    - 其余口径（`"r+b"` 不隐式建文件、文件被删原样上抛、`first_seen` 每段一次）与
      生产读路径一致。
    """
    index = max(1, int(start_index))
    while not stop.is_set():
        path = segment_path(index)
        if path is None:
            return
        if not os.path.exists(path):
            if stop.wait(archive_poll_seconds):
                return
            continue
        reached_eof = _tail_one_segment(
            path,
            stop=stop,
            on_chunk=on_chunk,
            chunk_bytes=chunk_bytes,
            idle_seconds=idle_seconds,
            first_seen=first_seen,
            next_path=lambda: segment_path(index + 1),
        )
        if stop.is_set():
            return
        if reached_eof:
            index += 1


def _tail_one_segment(
    path: str,
    *,
    stop: threading.Event,
    on_chunk: Callable[[bytes, float], None],
    chunk_bytes: int,
    idle_seconds: float,
    first_seen: Callable[[str, int], None] | None,
    next_path: Callable[[], str | None],
) -> bool:
    handle = open(path, "r+b")
    try:
        if first_seen is not None:
            first_seen(path, int(handle.tell()))
        while not stop.is_set():
            data = handle.read(chunk_bytes)
            if data:
                on_chunk(data, time.time())
                continue
            following = next_path()
            if following is not None and os.path.exists(following):
                return True
            if stop.wait(idle_seconds):
                return False
        return False
    finally:
        try:
            handle.close()
        except OSError:  # pragma: no cover - 关不掉不影响收队
            pass


class TranscriptionPipeline:
    """一条会中一直在跑的实时转写：两路读线程 + 一个段队列 + 一个 worker + 每路一个产物。

    `stop()` 在收尾预算内把队列里剩下的段尽量做完；预算耗完还有剩段就**如实登记**
    （几条没做完、音频归档完整、临时稿缺的是尾巴），不许静默丢。
    """

    def __init__(
        self,
        *,
        transcriber: SegmentTranscriber,
        line_sink: Callable[[Any], None] | None = None,
        vad_factory: Callable[[], VoiceActivitySegmenter] | None = None,
        segment_timeout: float | None = None,
        read_chunk_bytes: int = DEFAULT_READ_CHUNK_BYTES,
        idle_seconds: float = DEFAULT_TAIL_IDLE_SECONDS,
        archive_poll_seconds: float = DEFAULT_ARCHIVE_POLL_SECONDS,
        budget_fn: Callable[[int, float], float] | None = None,
        behind_threshold: int = BACKLOG_BEHIND_THRESHOLD,
    ) -> None:
        self.transcriber = transcriber
        self._sink = line_sink
        self._vad_factory = vad_factory or (lambda: VoiceActivitySegmenter())
        self._segment_timeout = (
            float(config_module.get("asr.timeout_seconds"))
            if segment_timeout is None
            else float(segment_timeout)
        )
        self._read_chunk_bytes = int(read_chunk_bytes)
        self._idle_seconds = float(idle_seconds)
        self._archive_poll_seconds = float(archive_poll_seconds)
        self._budget_fn = budget_fn or compute_drain_budget
        self.behind_threshold = int(behind_threshold)

        #: 段队列：**无界**（`maxsize=0`）。禁止 `put_nowait` / `if full: continue` 这类
        #: busy-drop 路径
        self.segments: queue.Queue[Any] = queue.Queue()
        self.session_dir: str | None = None
        self.mode: str | None = None
        self.sources: tuple[str, ...] = ()
        self._start_segment: int = 1

        self._stop = threading.Event()
        self._draining = threading.Event()
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        #: worker **线程对象本身**（`stop()` 收队时会把 `_worker` 置空，而那一刻线程可能还在
        #: 写最后一行；`writer_finished()` 只有这个引用答得了）
        self._worker_thread: threading.Thread | None = None
        self._reader_threads: dict[str, threading.Thread] = {}
        self._vads: dict[str, VoiceActivitySegmenter] = {}
        self._handles: dict[str, Any] = {}
        self._sealed = False
        self._last_drain: dict[str, Any] | None = None
        self._started = False
        self._drain_deadline: float | None = None
        self.worker_started = False
        self.reader_errors: dict[str, str] = {}
        self.segments_seen = 0
        self.segments_done = 0
        self.failures: dict[str, int] = {}
        self.last_unfinished_note: str = ""

    def start(self, mode: str, session_dir: str) -> None:
        """起两路读线程与 worker（**幂等**：重复调用什么都不做）。"""
        with self._lock:
            if self._started:
                return
            self._started = True
            self._join_readers(reason="换一场之前")
            self._stop.clear()
            self.mode = str(mode)
            self.session_dir = str(session_dir)
            self.sources = tuple(_sources_for_mode(mode))
            os.makedirs(self.session_dir, exist_ok=True)
            self._start_segment = max(1, next_segment_number(session_dir, SOURCE_IDS) - 1)
            resumed = any(
                os.path.exists(self._transcript_path(source))
                for source in self.sources
            )
            for source in self.sources:
                self._vads[source] = self._vad_factory()
                self._handles[source] = None
        if resumed:
            # 续接的两件事（锁外做：出口那一头会一路调回 runtime，别攥着本类自己的锁）
            self._open_resume_gap()
        with self._lock:
            self._worker = threading.Thread(
                target=self._worker_loop, name="asr-worker", daemon=True
            )
            self._worker_thread = self._worker
            self.worker_started = True
            self._worker.start()
            for source in self.sources:
                thread = threading.Thread(
                    target=self._reader_loop,
                    args=(source,),
                    name=f"asr-reader-{source}",
                    daemon=True,
                )
                self._reader_threads[source] = thread
                thread.start()
            _log.info(
                "转写流水线已起：mode=%s session=%s 读 %s（归档尾读，读头从第 %s 段接起%s）",
                self.mode, self.session_dir, "、".join(self.sources), self._start_segment,
                "；续接开场，旧段不重读" if resumed else "",
            )

    def set_line_sink(self, line_sink: Callable[[Any], None]) -> None:
        with self._lock:
            self._sink = line_sink

    def _emit_line(self, line: TranscriptLine) -> None:
        with self._lock:
            sink = self._sink
        if sink is None:
            _log.warning("转写还没有接上出口（line_sink 为 None），这一行只能留在本地：%r", line)
            return
        sink(line)

    def _segment_path(self, source: str, index: int) -> str:
        return os.path.join(str(self.session_dir), archive_name(source, index))

    def _reader_loop(self, source: str) -> None:
        """一路读线程：按段序号游标尾读归档，把新字节喂给这一路的 VAD。

        段列表不问 `session.json`（开场第一版还没有段记录，而且元信息不当判据）：按确定的
        命名规则逐序号找盘上的段文件，序号升序接续 = 连续时间轴。
        """

        def on_chunk(data: bytes, captured_at: float) -> None:
            # **盖章点就是这里**：captured_at 是「读到这一块那一刻」的墙钟。
            self._vads[source].feed(data, lambda pcm: self._enqueue(source, captured_at, pcm),
                                    captured_at)

        def first_seen(seen_path: str, offset: int) -> None:
            _log.info("转写开始尾读归档：%s（从偏移 %s 起）", seen_path, offset)

        def segment_path(index: int) -> str | None:
            # 每个序号都值得等：续接之后同一场会还会落下更多段
            return self._segment_path(source, index)

        try:
            tail_archive_segments(
                segment_path,
                stop=self._stop,
                on_chunk=on_chunk,
                chunk_bytes=self._read_chunk_bytes,
                idle_seconds=self._idle_seconds,
                archive_poll_seconds=self._archive_poll_seconds,
                first_seen=first_seen,
                start_index=self._start_segment,
            )
        except Exception as exc:  # noqa: BLE001 - 一路读线程出事不许带崩另一路
            with self._lock:
                self.reader_errors[source] = f"{type(exc).__name__}: {exc}"
            _log.warning("尾读 %s 这一路归档失败：%s: %s", source, type(exc).__name__, exc)

    def _enqueue(self, source: str, captured_at: float, pcm: bytes) -> None:
        if not pcm:
            return
        if len(pcm) < int(SAMPLE_RATE * MIN_SEGMENT_SECONDS) * SAMPLE_WIDTH:
            return
        with self._lock:
            self.segments_seen += 1
        self.segments.put((source, float(captured_at), bytes(pcm)))

    def _worker_loop(self) -> None:
        """**唯一**的消费线程：顺序消费两路的段（显存只有一份）。

        队列一空就退出等于把 worker 提前收摊（后面来的段全成了孤儿）；收队的条件因此是
        「收队信号已下 **且** 队列真的空了」——只有 `stop()` 会下那个信号。
        """
        while True:
            try:
                item = self.segments.get(timeout=WORKER_IDLE_SECONDS)
            except queue.Empty:
                if self._draining.is_set() and self.segments.empty():
                    return
                continue
            try:
                if item is _CLOSE_SENTINEL:  # pragma: no cover - 哨兵走的是 `_draining`
                    return
                source, captured_at, pcm = item
                self._handle_segment(source, captured_at, pcm)
            except Exception:  # noqa: BLE001 - 单段出事不许把 worker 带走
                _log.exception("处理一段转写时抛错（worker 继续消费后面的段）")
            finally:
                self.segments.task_done()
                with self._lock:
                    self.segments_done += 1

    def _handle_segment(self, source: str, captured_at: float, pcm: bytes) -> None:
        import numpy as np

        audio = np.frombuffer(pcm, dtype=np.int16)
        audio_seconds = len(audio) / SAMPLE_RATE
        # **同一支钟**：captured_at 是墙钟，这里取 queued_at 也必须用墙钟——混进
        # monotonic（与本机墙钟差十几亿秒）的话 max(0.0,…) 会恒夹成 0.0，而 wait= 正是
        # 本单元唯一一条「流水线落后」的可观测前兆。
        queued_at = time.time()
        waited = max(0.0, queued_at - captured_at)

        # 噪声预筛命中 → **静音，不写行**（连占位符也不写）：语义是「这一段落是静音」，
        # 按一次成功的「什么都没识别出来」登记，不进失败计数。
        if glm_module._is_noise_segment(audio):
            _log.info(
                "seg %s audio=%.1fs asr=0.0s wait=%.1fs tokens=0 reason=noise（静音，不写行）",
                _stamp(captured_at), audio_seconds, waited,
            )
            return

        started = time.monotonic()
        result = None
        try:
            result = self.transcriber.transcribe_segment(audio)
        except Exception as exc:  # noqa: BLE001 - 模型抛错一律写占位符，不许静默丢段
            _log.warning("识别抛错（写占位符，不落半截文本）：%s: %s", type(exc).__name__, exc)
            result = None
        duration = max(0.0, time.monotonic() - started)

        reason = str(getattr(result, "reason", "") or "")
        tokens = int(getattr(result, "tokens", 0) or 0)

        if result is None:
            self._finish_failed(
                source, captured_at, waited, audio_seconds,
                _format_failure("error"), "error", duration, tokens,
            )
            return

        if not getattr(result, "ok", True):
            reason = reason or "error"
            text = _format_failure(
                reason,
                duration=float(getattr(result, "duration", duration) or duration),
                timed_out=bool(getattr(result, "timed_out", False)),
            )
            self._finish_failed(source, captured_at, waited, audio_seconds, text, reason, duration, tokens)
            return

        # 文本级兜底剥离（设计内部已剥过一次，这里再兜一道）。
        text = glm_module._strip_repeat_loops(
            str(getattr(result, "text", "") or ""),
            max_unit=config_module.get("asr.repeat_max_unit"),
            min_coverage=config_module.get("asr.repeat_min_coverage"),
        ).strip()
        if not text:
            # 空输出：**不许静默丢**（时间线要不断、缺口要可见）
            self._finish_failed(
                source, captured_at, waited, audio_seconds,
                _format_failure("empty"), "empty", duration, tokens,
            )
            return

        line = TranscriptLine(
            ts=float(captured_at), source=str(source), text=text, kind="line"
        )
        self._write_line(source, captured_at, text)
        self._log_segment(source, captured_at, audio_seconds, duration, waited, tokens, "ok")
        self._emit_line(line)

    def _finish_failed(
        self,
        source: str,
        captured_at: float,
        waited: float,
        audio_seconds: float,
        placeholder: str,
        reason: str,
        duration: float,
        tokens: int,
    ) -> None:
        """失败一律写占位符：**超时与判环一律不落部分文本**。"""
        with self._lock:
            self.failures[reason] = self.failures.get(reason, 0) + 1
        self._write_line(source, captured_at, placeholder)
        self._log_segment(source, captured_at, audio_seconds, duration, waited, tokens, reason)
        self._emit_line(
            TranscriptLine(
                ts=float(captured_at), source=str(source), text=placeholder, kind="line"
            )
        )

    def _log_segment(
        self,
        source: str,
        captured_at: float,
        audio_seconds: float,
        duration: float,
        waited: float,
        tokens: int,
        reason: str,
    ) -> None:
        """每段一条日志。

        行里的 `source=` 是本单元加的一列：两路共用一个 worker，出了事要能看出是哪一路。
        **`wait` 持续增长就是流水线落后的前兆**：它是「这一段在队列里等了多久」，
        正常时应贴着 0；一直往上爬说明 worker 追不上读线程。
        """
        _log.info(
            "seg %s audio=%.1fs asr=%.1fs wait=%.1fs tokens=%d reason=%s source=%s",
            _stamp(captured_at), audio_seconds, duration, waited, tokens, reason, source,
        )

    def _transcript_path(self, source: str) -> str:
        return os.path.join(str(self.session_dir), TRANSCRIPT_FILE_TEMPLATE.format(source=source))

    def _open_resume_gap(self) -> None:
        """续接开场的两件事：各路临时稿追加一条记号行 + 各推一条 `kind="gap"` 的行。

        记号行取 `<!-- … -->` 注释形：`LINE_RE` 认不出它，读侧（纪要切块、行数统计）
        跳过它、不把它当发言；它照旧计入 splitlines 的行数分母（现行口径，不开口子）。
        """
        at = time.time()
        for source in self.sources:
            self._append_marker(source, at, RESUME_GAP_TEXT)
            self._emit_line(
                TranscriptLine(ts=float(at), source=str(source), text=RESUME_GAP_TEXT, kind="gap")
            )
        _log.info(
            "续接开场：各路临时稿落了一条记号行、各推一条断口行（读头从第 %s 段接起）",
            self._start_segment,
        )

    def _append_marker(self, source: str, at: float, text: str) -> None:
        """往这一路的临时稿追加一条注释形记号行（**独立小开的句柄**，写完即关）。

        只在 `start()` 里、读线程与 worker 都还没起来时调用——此刻没有别人在写这一路。
        """
        path = self._transcript_path(source)
        line = f"<!-- [{_stamp(at)}] {text} -->\n"
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()

    def _write_line(self, source: str, captured_at: float, text: str) -> None:
        """追加一行到这一路的产物。

        「写一行」与「收队关句柄」共用同一把锁：join 有上界（预算耗完 worker 可能还在写），
        写在锁外会撞上「句柄已关 / 已置空」的竞态。
        """
        path = self._transcript_path(source)
        line = f"[{_stamp(captured_at)}] {text}\n"
        with self._lock:
            handle = self._handles.get(source)
            if handle is None:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                handle = open(path, "a", encoding="utf-8")
                self._handles[source] = handle
            handle.write(line)
            handle.flush()

    def _close_handles(self) -> None:
        with self._lock:
            handles, self._handles = self._handles, {}
        for handle in handles.values():
            if handle is None:  # pragma: no cover - 句柄总是打开成功后才进字典
                continue
            try:
                handle.close()
            except OSError:  # pragma: no cover - 关不掉不影响收尾结论
                _log.warning("关转写产物失败（如实登记，不静默）", exc_info=True)

    def stop(self) -> dict[str, Any]:
        """关读线程 → flush 尾段 → **在收尾预算内**把队列里剩下的段尽量做完。

        返回登记字典（`budget` / `unfinished` / `note` / `segments_seen` / `segments_done`），
        供 `meeting/backends/transcribe.py` 复述进 log。返回即收队完成：读线程与 worker
        都 join 过了。
        """
        with self._lock:
            if self._sealed:
                # 幂等档：原样交回第一趟的登记（不重算、不覆写 note）——再算一遍
                # budget=0/unfinished=0 就是与 backlog_depth()>0 自相矛盾的假读数。
                if self._last_drain is not None:
                    return self._last_drain
                return self._drain_note(budget=0.0, unfinished=0)
            self._sealed = True

        self._stop.set()
        self._join_readers(reason="收尾")

        for source in self.sources:
            vad = self._vads.get(source)
            if vad is None or not vad.has_pending:
                continue
            cap = time.time()
            vad.flush(lambda pcm, src=source, ts=cap: self._enqueue(src, ts, pcm))

        self._draining.set()
        backlog = self.segments.qsize()
        budget = float(self._budget_fn(backlog, self._segment_timeout))
        self._drain_deadline = time.monotonic() + budget
        worker = self._worker
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=budget + DRAIN_JOIN_SLACK_SECONDS)
        # 收队走完就把句柄放开：`worker_alive()` 因此不再靠「还在收尾预算里」那一档兜着
        self._worker = None
        # **「没做完」= 队列里剩下的 + 已被 worker 取走、还在处理的那一段**：
        # 只看 qsize() 的话 `unfinished` 在生产路径上恒为 0（空头支票）。
        with self._lock:
            outstanding = int(self.segments_seen) - int(self.segments_done)
        unfinished = max(self.segments.qsize(), outstanding)
        note = self._drain_note(budget=budget, unfinished=unfinished)
        self._last_drain = note
        if unfinished:
            _log.warning(
                "转写收尾预算用尽：还有 %s 段没做完（预算 %.1fs = 30 + %s × (%.1fs + 5)，"
                "封顶 300s）；音频归档完整、缺的只是临时稿的尾巴 —— 如实登记，不静默丢",
                unfinished, budget, backlog, self._segment_timeout,
            )
        self._close_handles()
        return note

    def _join_readers(self, *, reason: str) -> None:
        """把读线程 join 干净（**幂等**，可重复调；收尾与「换一场」都会走它）。"""
        self._stop.set()
        for thread in list(self._reader_threads.values()):
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=READER_JOIN_TIMEOUT_SECONDS)
                if thread.is_alive():  # pragma: no cover - daemon 线程，不阻塞进程退出
                    _log.warning(
                        "%s：读线程 %s 在 %ss 内没有退出",
                        reason, thread.name, READER_JOIN_TIMEOUT_SECONDS,
                    )

    def _drain_note(self, *, budget: float, unfinished: int) -> dict[str, Any]:
        note = (
            f"收尾预算 {budget:.1f}s 用尽，还有 {unfinished} 段没做完"
            "（音频归档完整，临时稿缺的是尾巴）"
            if unfinished
            else f"收尾预算 {budget:.1f}s 内队列已清空（没有剩段）"
        )
        self.last_unfinished_note = note
        return {
            "budget": budget,
            "unfinished": int(unfinished),
            "note": note,
            "segments_seen": int(self.segments_seen),
            "segments_done": int(self.segments_done),
            "failures": dict(self.failures),
            "reader_errors": dict(self.reader_errors),
        }

    def backlog_depth(self) -> int:
        return int(self.segments.qsize())

    def worker_alive(self) -> bool:
        worker = self._worker
        if worker is None:
            return False
        if worker.is_alive():
            return True
        deadline = self._drain_deadline
        return self._draining.is_set() and deadline is not None and time.monotonic() <= deadline

    def readers_alive(self) -> bool:
        if not self._reader_threads:
            return False
        return all(thread.is_alive() for thread in self._reader_threads.values())

    def writer_finished(self) -> bool:
        """**写行的那个线程真的退出了吗**。

        与 `worker_alive()` 的差别：`stop()` 收队时把 `_worker` 置空（`worker_alive()` 当场
        变 False，而线程可能还在 `_write_line()` 里），且「还在收尾预算内」那一档在这里不算
        活着。写产物行的只有 worker 这一条线程，它退出了就再没人能重开产物句柄。
        """
        thread = self._worker_thread
        return thread is None or not thread.is_alive()

    def close_transcripts_when_drained(self, *, timeout: float | None = None) -> bool:
        """等 worker 把手上那一段写完，再把产物句柄收干净。

        **为什么要再关一次**：`stop()` 末尾那次 `_close_handles()` 关不掉**之后**才写的行——
        worker 预算耗完那一刻手上还扣着一段时，`stop()` 返回后它把那一行写出，
        `_write_line()` 发现句柄已空就重开一个并记回 `self._handles`，此后没人关（第二次
        `stop()` 走幂等格直接返回）。同一进程连开多场就是一场漏一个 fd。

        **为什么不能就地再关**：关在 worker 写最后一行前面，它会把句柄重开。因此先等
        **写行的那条线程**退出（`writer_finished()`）才关——它退出后 `_write_line()`
        不可能再被调，这一次关闭才是干净的。等不到就**不关**（硬关会把最后一行写进已关闭
        的句柄），留日志由 daemon 线程自己写完。返回是否收干净。
        """
        if timeout is None:
            try:
                timeout = float(self._segment_timeout) + float(DRAIN_JOIN_SLACK_SECONDS)
            except (TypeError, ValueError):  # pragma: no cover - 构造参数本来就是数值
                timeout = float(DRAIN_JOIN_SLACK_SECONDS)
        thread = self._worker_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        if not self.writer_finished():
            _log.warning(
                "转写收尾之后 worker 还在写最后一行（等满 %.1fs）：产物句柄先留着不关，"
                "由它自己写完（daemon 线程，进程退出时回收）—— 不静默丢那一行",
                timeout,
            )
            return False
        self._close_handles()
        return True


def _sources_for_mode(mode: str) -> tuple[str, ...]:
    """本场读哪几路归档：与采集侧同一套取值域（`meeting.capture.engine`）。

    采集侧的 `sources_for_mode()` 是唯一口径来源，这里 import 它（不抄一份取值域），
    未知 mode 也由它当场抛 `ValueError`——同一个冻结取值域不允许采集侧 fail-fast、
    转写侧静默回退成两路全读。
    """
    from meeting.capture.engine import sources_for_mode

    return tuple(sources_for_mode(mode))


def _stamp(captured_at: float) -> str:
    """采集时刻 → `HH:MM`（分钟级时间戳；墙钟可能回溯，统一用采集时刻格式化）。"""
    return datetime.fromtimestamp(captured_at).strftime("%H:%M")


__all__ = [
    "BACKLOG_BEHIND_THRESHOLD",
    "DRAIN_BUDGET_MAX_SECONDS",
    "DRAIN_BUDGET_MIN_SECONDS",
    "DRAIN_BUDGET_SEGMENT_OVERHEAD_SECONDS",
    "DEFAULT_ARCHIVE_POLL_SECONDS",
    "DEFAULT_FAILURE_LABEL",
    "DEFAULT_READ_CHUNK_BYTES",
    "DEFAULT_TAIL_IDLE_SECONDS",
    "DRAIN_JOIN_SLACK_SECONDS",
    "FAILURE_LABELS",
    "FRAME_BYTES",
    "FRAME_MILLISECONDS",
    "MIN_SEGMENT_SECONDS",
    "READER_JOIN_TIMEOUT_SECONDS",
    "RESUME_GAP_TEXT",
    "SAMPLE_RATE",
    "SAMPLE_WIDTH",
    "SILENCE_TIMEOUT_SECONDS",
    "TRANSCRIPT_FILE_TEMPLATE",
    "TRANSCRIPT_HEALTH",
    "TRANSCRIPT_HEALTH_FALLBACK",
    "TranscriptionPipeline",
    "VAD_TUNABLE_TTL_SECONDS",
    "VoiceActivitySegmenter",
    "WORKER_IDLE_SECONDS",
    "close_transcripts_when_drained",
    "compute_drain_budget",
    "tail_archive_segments",
    "validate_transcript_health",
]
