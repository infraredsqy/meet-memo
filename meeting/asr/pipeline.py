"""实时转写流水线 —— 游标尾读归档 PCM → VAD 切段 → 入队盖章 → 独立 worker → 落盘 + 行出口。

节点 `9e5db89c` 的 phase2（`phase2/task.md` 的落地口径 1–6）。吃 phase1 的
`meeting/asr/guards.py`（四层防护的判定内核）与 `meeting/asr/glm.py`（单段识别）。

**音频从哪来 —— 这一段的骨头**（`phase2/task.md` §1 / `docs/v1-lineage.md` 的 R2 / R3）：

  - 采集只管往 `session_dir/meeting_audio_<source>.pcm` 里写（`meeting/capture/archive.py`
    的 `open_pcm()` 是 `buffering=0` 的追加写，写在盘上**立刻可见**）；
  - 转写侧**每路一条读线程**，按游标从那两个文件里读**新字节**（不碰采集层的任何对象、
    不要求采集给它开旁路出口 —— 这正是 spec 要的那条边界）；
  - **时间戳取「读到这一块那一刻」的墙钟**并随段入队 —— 这就是「入队即盖章」在 V2 的形状，
    与 V1 `_TaggedSink`（`mnotes_core/pipeline.py:47-62`）的语义一致：
    **处理慢不会把后面段的时间戳挤到一起**（V1 的事故根因就是「等 ASR 返回后再取时间戳」，
    一个退化段卡住 6 分钟，6 分钟的内容被压进 1 分钟里）。

**VAD 切段**照 V1 `mnotes_core/vad.py:12-77` 的判据：采样 16k / 单声道 / int16；帧长 30ms
（480 样本 / 960 字节）；`webrtcvad.Vad(1)`；静默 **2.0 秒**出段；**单段硬上限按配置
`asr.max_segment_seconds` 秒**（默认 10 秒，超了立即出段再续，一段都不丢）。每一路自己一个
VAD 实例与自己的缓冲，每 0.5 秒重读一次段长上限（V1 的 `_TUNABLE_TTL`，改配置无需重建实例）。

**队列与 worker**：段队列**无界**（`queue.Queue()`，禁止 busy-drop —— V1 `docs/specs.md:157`
把 busy-drop 明确列为反模式），三元组 `(source, capture_ts, pcm_bytes)`，与 V1
`coordinator.py:108` 的形状一致；**一个** worker 线程顺序消费两路的段（显存只有一份，
顺序消费就是「不丢内容」要的形状）。

**落盘**：每路一个文件 `session_dir/meeting_transcript_<source>.md`（`source ∈ mic / loopback`），
**追加模式、逐行 flush**，行的形状与 V1 的行格式接口一致（`docs/v1-lineage.md` 的 B12 / B13）：

    [HH:MM] 文本
    [HH:MM] [[识别失败: 超时 20s]]

**文件名常量为什么落在这里**（`phase2/task.md` §4 要的那句说明）：产物落在**场次目录**里，
而场次目录的唯一来源是 `start(mode, session_dir)` 收到的那个参数 —— 常量跟着**唯一使用者**
走，读这一份模块就知道盘上会有哪两个文件；`meeting/cli.py` 不参与运行期产物命名
（它只负责起服务与退出码），因此不在那里再定一份「同名的另一个常量」。

**本模块顶层的 import 只有标准库与包内模块**（`phase2/task.md` 的复审自检项 5）：
`numpy` 与 `webrtcvad` 都在函数体内惰性 import，于是「没装模型依赖的机器上
`meeting.backends.transcribe` 仍能 import 成功」这条要求成立。

搬过来的三处出处（照 `docs/v1-lineage.md` 的 1.0 留出处注释）：

  - VAD 切段判据：`mnotes_core/vad.py:12-77`（`VADProcessor`）
  - 入队盖章的语义：`mnotes_core/pipeline.py:47-62`（`_TaggedSink`）、队列形状 `coordinator.py:108`
  - 每段一条日志的字段：`docs/specs.md:143-149`（`audio= / asr= / wait= / tokens= / reason=`）
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

_log = logging.getLogger("meeting.asr.pipeline")

# ---------------------------------------------------------------------------
# 形状与常量
# ---------------------------------------------------------------------------

#: 归档 PCM 的形状（16k / 单声道 / int16 小端裸 PCM，与 `meeting/capture/archive.py` 一致）
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
CHANNELS = 1

#: 一帧多少毫秒（`webrtcvad` 只认 10 / 20 / 30）
FRAME_MILLISECONDS = 30

#: 一帧多少字节（`16000 × 30 / 1000 × 2` = 960 字节 = 480 个 int16 样本）
FRAME_BYTES = SAMPLE_RATE * FRAME_MILLISECONDS // 1000 * SAMPLE_WIDTH

#: 静默多久算一段说完（秒）——照 V1 `vad.py:26` 的 `SILENCE_TIMEOUT`
SILENCE_TIMEOUT_SECONDS = 2.0

#: 段长上限的重读间隔（秒）——照 V1 `vad.py:21` 的 `_TUNABLE_TTL`：
#: `asr.max_segment_seconds` 是热读键，改配置无需重建 VAD 实例
VAD_TUNABLE_TTL_SECONDS = 0.5

#: 读线程一次读多少字节（0.5 秒的音频；够大到读得动，够小到时间戳盖得准）
DEFAULT_READ_CHUNK_BYTES = SAMPLE_RATE * SAMPLE_WIDTH // 2

#: 归档还没出现时的轮询间隔（秒）：采集侧先开文件、转写随后起步，有几十毫秒的空档
DEFAULT_ARCHIVE_POLL_SECONDS = 0.05

#: 读线程从归档尾读时的轮询间隔（秒）：一次没读到新字节就按这个节奏再来
DEFAULT_TAIL_IDLE_SECONDS = 0.02

#: worker 取段的等待上限（秒）——照 V1 `pipeline.py:113` 的 `get(timeout=0.1)`
WORKER_IDLE_SECONDS = 0.1

#: `stop()` 里 worker 线程 join 的收尾余量（秒）：预算耗完也最多再等这一下
DRAIN_JOIN_SLACK_SECONDS = 1.0

#: 收尾时**每一路读线程** join 的上界（秒）：`_join_readers()` 逐路等这么久，超时就落一条
#: warning 并往下走（daemon 线程，不阻塞进程退出）。
#:
#: 它是「内层最坏耗时」的一项，因此**必须是一个有名字的常量**：外层
#: `meeting/backends/transcribe.py` 的 `stop()` 要在事件循环线程上同步等这条收尾线程，
#: 它的上界得把这一项按路数算进去（复审 m5：外层余量曾只留 1 秒，比内层最坏档小一截，
#: 最坏档会打出一条假的「收尾线程没跑完」并在流水线还在收尾时就返回）。
READER_JOIN_TIMEOUT_SECONDS = 2.0

#: 每路的产物文件名（`source ∈ mic / loopback`）
TRANSCRIPT_FILE_TEMPLATE = "meeting_transcript_{source}.md"

#: 转写健康度 `behind` 的**积压门槛**（段）。
#:
#: **取值依据**：V1 `docs/specs.md:147` 的 TUI 计数器就是「待转写 ≥20 段转红」——20 段按
#: 默认段长 10 秒算是 **200 秒（3 分半）的语音还没落成文字**，那时人眼已经明显觉得
#: 「字幕追不上说话」；低于这个数只是正常的处理滞后，报 `behind` 会天天亮灯。
#: 只做「积压门槛」这一件事，不改任何状态迁移（`phase2/task.md` §5）。
BACKLOG_BEHIND_THRESHOLD = 20

#: worker 的收队哨兵（**不入队**，拿它当「不再有段了」）
_CLOSE_SENTINEL = object()

# ---------------------------------------------------------------------------
# 收尾预算（公式的唯一落点）
#
# `budget = 30 + 积压段数 × (单段超时 + 5)`，下限 30 秒、封顶 300 秒。
# **公式本身住在这里**（转写是积压量的来源），`meeting/service/runtime.py` 的
# `compute_drain_budget()` 直接转交给它 —— 两处各写一份的话，收尾那一趟与服务侧读到的
# 就成了两条会各自漂移的口径。三个数硬编码、不开配置键（方案 §5.3 没给它们开键）。
# ---------------------------------------------------------------------------

#: 预算下限（秒）
DRAIN_BUDGET_MIN_SECONDS = 30

#: 预算封顶（秒）
DRAIN_BUDGET_MAX_SECONDS = 300

#: 公式里每段那笔额外开销（秒）：`积压段数 × (单段超时 + 5)`
DRAIN_BUDGET_SEGMENT_OVERHEAD_SECONDS = 5


def compute_drain_budget(backlog: int, segment_timeout: float) -> float:
    """收尾预算：`30 + backlog × (segment_timeout + 5)`（下限 30、封顶 300）。"""
    budget = DRAIN_BUDGET_MIN_SECONDS + int(backlog) * (
        float(segment_timeout) + float(DRAIN_BUDGET_SEGMENT_OVERHEAD_SECONDS)
    )
    return float(min(DRAIN_BUDGET_MAX_SECONDS, max(DRAIN_BUDGET_MIN_SECONDS, budget)))

#: 一段音频至少要有半秒才值得识别（**数值口径按 `phase2/task.md` §2 与 V1 那行注释的本意取 0.5 秒**）。
#:
#: 判据的出处是 V1 `mnotes_core/pipeline.py:127`：
#: `if not seg_bytes or len(seg_bytes) < SAMPLE_RATE * 0.5:  # < 0.5s 跳过` —— **表达式与它自己那句
#: 注释对不上**：`seg_bytes` 是 int16 的**字节数**，`SAMPLE_RATE * 0.5 = 8000` 比的是**字节**，
#: 折算成音频只有 `8000 / (16000 × 2) = 0.25` 秒。那是 V1 的一处**量纲笔误**（按它注释的意思，
#: 该写 `SAMPLE_RATE * 0.5 * SAMPLE_WIDTH`）。
#:
#: 本单元**不跟着这个笔误走**：V2 按 `int(SAMPLE_RATE * MIN_SEGMENT_SECONDS) * SAMPLE_WIDTH`
#: （`_enqueue` 的入口那一格）比字节，`0.5` 秒就是真的半秒 —— 这是 task.md 的意图，也是 V1 那行
#: 注释写的意图。VAD 已经丢掉不足 500ms 的段（V1 `replay._segment` 同一判据），这里再兜一道：
#: 兜的是「硬上限切出来的尾段只有一两帧」那一档（不足半秒的段识别出来基本都是噪声）。
MIN_SEGMENT_SECONDS = 0.5

#: 失败占位符的标签（`[[识别失败: 超时 20s]]` 这一形状；**绝不含 `**`**）
FAILURE_LABELS: dict[str, str] = {
    "timeout": "识别失败: 超时",
    "loop": "识别失败: 重复循环",
    "empty": "识别失败: 无输出",
    "error": "识别失败: 错误",
}

#: 认不出的失败原因落这一档（形状仍是双层方括号 + 裸文本）
DEFAULT_FAILURE_LABEL = "识别失败"


#: 转写健康度的取值域（`web/src/contract/primitives.ts` 的 `TRANSCRIPT_HEALTHS` 逐字同源，
#: 快照 `transcript.health` 只落这三档）
TRANSCRIPT_HEALTH: tuple[str, ...] = ("normal", "behind", "stopped")

#: 转写健康度的兜底档（后端报了取值域之外的东西时落它：`behind` 会说成「落后」，
#: `stopped` 会说成「没在跑」，两档都比事实更具体，只有 `stopped` ——
#: 「这一档现在不产出」—— 在「后端行为不可信」时说的是实话）
TRANSCRIPT_HEALTH_FALLBACK = "stopped"


def validate_transcript_health(value: Any) -> str:
    """把后端报上来的健康度收进**转写域**的三取值；越界取值落 `TRANSCRIPT_HEALTH_FALLBACK`。

    **这一条必须与采集域的 `meeting.state.validate_health()` 分开**（那是**七取值**的探针域，
    越界落 `probe_failed`）。两个域共用一个校验器的后果是实测过的：把 `normal` 交给采集域的
    校验器，快照里的 `transcript.health` 会出成 `"probe_failed"` —— 一个在转写域里不存在、
    前端 schema（`TRANSCRIPT_HEALTHS`）也不认的值。既然两个域各自冻结，校验器就各归各的
    （转写域的这一个跟着 `TRANSCRIPT_HEALTH` 走，采集域那一个留在 `meeting/state.py`）。
    """
    text = str(value)
    return text if text in TRANSCRIPT_HEALTH else TRANSCRIPT_HEALTH_FALLBACK


@runtime_checkable
class SegmentTranscriber(Protocol):
    """worker 认的转写接口（**唯一**一个方法）。

    生产实现是 phase1 的 `meeting.asr.glm.GlmAsrTranscriber`（`transcribe_segment(int16 数组)
    -> TranscribeResult`）；测试注入自己的替身（假模型），**任何用例都不许加载真模型**。
    """

    def transcribe_segment(self, audio_int16: Any) -> Any: ...


# ---------------------------------------------------------------------------
# 占位符
# ---------------------------------------------------------------------------


def _format_failure(reason: str, duration: float = 0.0, timed_out: bool = False) -> str:
    """把一条失败结果格式化成落盘占位符（照 V1 `pipeline.py:39-44`）。

    形状：`[[识别失败: 超时 20s]]` —— **双层方括号、裸文本**：不带时间戳（时间戳由调用方
    拼上）、**绝不含 `**`**，否则下游会把 `**X**:` 当成说话人标签
    （`docs/v1-lineage.md` 的 B12 / E12）。
    """
    label = FAILURE_LABELS.get(reason, DEFAULT_FAILURE_LABEL)
    if timed_out and duration:
        return f"[[{label} {duration:.0f}s]]"
    return f"[[{label}]]"


# ---------------------------------------------------------------------------
# VAD 切段（照 V1 mnotes_core/vad.py:12-77 的判据）
# ---------------------------------------------------------------------------


class VoiceActivitySegmenter:
    """一路的 VAD 切段器：吃字节、吐完整段（`feed()` 的 `emit` 出口每次给一段）。

    判据逐条照 V1 `mnotes_core/vad.py:12-77`：帧长 30ms、`webrtcvad.Vad(1)`、
    静默 2.0 秒出段、单段硬上限按配置 `asr.max_segment_seconds`（默认 10 秒，超了立即出段
    再续，一段都不丢）；段长上限**每 0.5 秒重读一次配置**（V1 的 `_TUNABLE_TTL`），
    因此改 `asr.max_segment_seconds` 不需要重建实例。

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
        #: 段长上限**显式给定时以它为准**（`max_segment_seconds=...` 是调用方的决定，
        #: 热读只负责「没显式给」的那一档：配置改了就换、不改就沿用配置值）
        self._pinned_cap = max_segment_seconds is not None
        self._cap_seconds = (
            float(max_segment_seconds)
            if max_segment_seconds is not None
            else float(config_module.get("asr.max_segment_seconds"))
        )
        self._cap_checked = 0.0

    # -- 段长上限（热读） --------------------------------------------------

    @property
    def cap_bytes(self) -> int:
        """当前生效的单段硬上限（字节）：`秒数 × 采样率 × 2`。"""
        return max(1, int(self._cap_seconds)) * SAMPLE_RATE * SAMPLE_WIDTH

    def _refresh_cap(self, now: float) -> None:
        """偶尔重读一次 `asr.max_segment_seconds`（照 V1 `vad.py:36-43`）。"""
        if self._pinned_cap or now - self._cap_checked <= self._ttl:
            return
        self._cap_checked = now
        try:
            self._cap_seconds = float(config_module.get("asr.max_segment_seconds"))
        except Exception:  # noqa: BLE001 - 配置读不出来就沿用上一次的取值，不打断切段
            _log.warning("读 asr.max_segment_seconds 失败，沿用上一次的段长上限", exc_info=True)

    # -- 主路径 ------------------------------------------------------------

    def feed(self, data: bytes, emit: Callable[[bytes], None], now: float) -> None:
        """喂一块字节；每切出一段就调一次 `emit(pcm_bytes)`（可能一次切出多段）。"""
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
            # 硬上限：单段超过上限秒数强制切分（不丢，续录新段）
            if len(self._segment_buffer) >= self.cap_bytes:
                self._emit(emit)

    def flush(self, emit: Callable[[bytes], None]) -> None:
        """把手上这一段剩下的音频强行送出（收队时用，**不丢尾巴**）。"""
        self._emit(emit)

    @property
    def has_pending(self) -> bool:
        """手上还压着一段没送出的音频吗。"""
        return bool(self._segment_buffer)

    def _emit(self, emit: Callable[[bytes], None]) -> None:
        if not self._segment_buffer:
            return
        segment = bytes(self._segment_buffer)
        self._segment_buffer = bytearray()
        self._last_speech = 0.0
        emit(segment)


def _default_vad() -> Any:
    """缺省的 VAD 后端：`webrtcvad.Vad(1)`（库在函数体内惰性 import）。"""
    import webrtcvad

    return webrtcvad.Vad(1)


# ---------------------------------------------------------------------------
# 归档尾读（游标读新字节）
# ---------------------------------------------------------------------------


def tail_archive(
    path: str,
    *,
    stop: threading.Event,
    on_chunk: Callable[[bytes, float], None],
    chunk_bytes: int = DEFAULT_READ_CHUNK_BYTES,
    idle_seconds: float = DEFAULT_TAIL_IDLE_SECONDS,
    archive_poll_seconds: float = DEFAULT_ARCHIVE_POLL_SECONDS,
    first_seen: Callable[[str, int], None] | None = None,
) -> None:
    """按游标尾读一个归档 PCM：读到的每一块都连**读到的时刻**一起交给 `on_chunk`。

    - 归档文件还没出现时按 `archive_poll_seconds` 等它（采集侧先开文件、转写随后起步）；
    - `first_seen(path, offset)` 只在文件第一次被打开时调一次（记账：这一路从哪个偏移起读）；
    - `flag` 用 `"r+b"` 打开（文件不存在就报错，于是"归档还没出现"这件事不会被隐式建文件
      掩盖掉；`"a+b"` 会**建出**一个空文件，那等于转写侧替采集侧把文件建了）；
    - 文件被删 / 打不开时原样抛给调用方（读线程据此记一条 warning 并把自己标成不健康）。
    """
    handle = None
    try:
        while not stop.is_set():
            if handle is None:
                if not os.path.exists(path):
                    if stop.wait(archive_poll_seconds):
                        return
                    continue
                handle = open(path, "r+b")
                if first_seen is not None:
                    first_seen(path, int(handle.tell()))
            data = handle.read(chunk_bytes)
            if data:
                # **盖章点**：这一刻的墙钟随这一块里切出来的段一起入队
                on_chunk(data, time.time())
                continue
            if stop.wait(idle_seconds):
                return
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:  # pragma: no cover - 关不掉不影响收队
                pass


# ---------------------------------------------------------------------------
# 流水线
# ---------------------------------------------------------------------------


class TranscriptionPipeline:
    """一条会中一直在跑的实时转写：两路读线程 + 一个段队列 + 一个 worker + 每路一个产物。

    起停：

      - `start(mode, session_dir)`：按 `mode` 决定读哪几路（`meeting.capture.engine` 的同一套
        取值域：`both` / `mic` / `loopback`），起读线程与 worker；
      - `stop()`：关读线程 → flush VAD 的尾段 → **在收尾预算内**把队列里剩下的段尽量做完
        （预算 = `compute_drain_budget(backlog, segment_timeout)`，即
        `30 + 积压段数 × (单段超时 + 5)`，下限 30、封顶 300）；预算耗完还有剩段就
        **如实记账**（几条没做完、音频归档完整、临时稿缺的是尾巴），不许静默丢。

    出口：每成一行调一次 `line_sink(TranscriptLine(...))`（由 `TranscriberBackend` 交给
    runtime 包好的线程安全出口）。
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
        #: 收尾预算的算法**照 `compute_drain_budget` 的形状**（同一个函数、同一条公式），
        #: 这里让它可以被注入：用例要验「预算耗完还有剩段就如实记账」时不必真等 30 秒
        self._budget_fn = budget_fn or compute_drain_budget
        self.behind_threshold = int(behind_threshold)

        #: 段队列：**无界**（`maxsize=0`）。禁止 `put_nowait` / `if full: continue` 这类
        #: busy-drop 路径（V1 `docs/specs.md:157` 把它明确列为反模式）
        self.segments: queue.Queue[Any] = queue.Queue()
        self.session_dir: str | None = None
        self.mode: str | None = None
        self.sources: tuple[str, ...] = ()

        self._stop = threading.Event()
        self._draining = threading.Event()
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        #: worker **线程对象本身**（`stop()` 收队时会把 `_worker` 置空，而那一刻线程可能还在
        #: 写最后一行；`writer_finished()` 问的是「线程真的退出了没有」，只有这个引用答得了）
        self._worker_thread: threading.Thread | None = None
        self._reader_threads: dict[str, threading.Thread] = {}
        self._vads: dict[str, VoiceActivitySegmenter] = {}
        self._handles: dict[str, Any] = {}
        self._sealed = False
        self._started = False
        self._model_failed = False
        #: 收队 deadline（`stop()` 里设；`worker_alive()` 用它判「还在收尾预算内」）
        self._drain_deadline: float | None = None
        #: 记账（给 `meeting/backends/transcribe.py` 的 `health()` 与 log 用）
        self.worker_started = False
        self.reader_errors: dict[str, str] = {}
        self.segments_seen = 0
        self.segments_done = 0
        self.failures: dict[str, int] = {}
        self.model_error: str = ""
        self.last_unfinished_note: str = ""

    # -- 起 ---------------------------------------------------------------

    def start(self, mode: str, session_dir: str) -> None:
        """起两路读线程与 worker（**幂等**：重复调用什么都不做）。"""
        with self._lock:
            if self._started:
                return
            self._started = True
            # 上一场可能还留着读线程（换场时 `start()` 会被再调一次）：先收干净再起新的
            self._join_readers(reason="换一场之前")
            self._stop.clear()
            self.mode = str(mode)
            self.session_dir = str(session_dir)
            self.sources = tuple(_sources_for_mode(mode))
            os.makedirs(self.session_dir, exist_ok=True)
            for source in self.sources:
                self._vads[source] = self._vad_factory()
                self._handles[source] = None
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
                "转写流水线已起：mode=%s session=%s 读 %s（归档尾读，采集与模型解耦）",
                self.mode, self.session_dir, "、".join(self.sources),
            )

    # -- 出口 -------------------------------------------------------------

    def set_line_sink(self, line_sink: Callable[[Any], None]) -> None:
        """换一份行出口（`bind()` 可能比 `start()` 先来；这是线程安全的一次赋值）。"""
        with self._lock:
            self._sink = line_sink

    def _emit_line(self, line: TranscriptLine) -> None:
        with self._lock:
            sink = self._sink
        if sink is None:
            _log.warning("转写还没有接上出口（line_sink 为 None），这一行只能留在本地：%r", line)
            return
        sink(line)

    # -- 读线程 -----------------------------------------------------------

    def _archive_path(self, source: str) -> str:
        return os.path.join(str(self.session_dir), f"meeting_audio_{source}.pcm")

    def _reader_loop(self, source: str) -> None:
        """一路读线程：游标尾读归档，把新字节喂给这一路的 VAD。"""
        path = self._archive_path(source)

        def on_chunk(data: bytes, captured_at: float) -> None:
            # **盖章点就是这里**：captured_at 是「读到这一块那一刻」的墙钟，
            # 它随这一块里切出来的段一起入队（处理慢不会把后面段的时间戳挤到一起）。
            self._vads[source].feed(data, lambda pcm: self._enqueue(source, captured_at, pcm),
                                    captured_at)

        def first_seen(seen_path: str, offset: int) -> None:
            _log.info("转写开始尾读归档：%s（从偏移 %s 起）", seen_path, offset)

        try:
            tail_archive(
                path,
                stop=self._stop,
                on_chunk=on_chunk,
                chunk_bytes=self._read_chunk_bytes,
                idle_seconds=self._idle_seconds,
                archive_poll_seconds=self._archive_poll_seconds,
                first_seen=first_seen,
            )
        except Exception as exc:  # noqa: BLE001 - 一路读线程出事不许带崩另一路
            with self._lock:
                self.reader_errors[source] = f"{type(exc).__name__}: {exc}"
            _log.warning("尾读 %s 这一路归档失败：%s: %s", source, type(exc).__name__, exc)

    def _enqueue(self, source: str, captured_at: float, pcm: bytes) -> None:
        """**入队即盖章**：三元组 `(source, capture_ts, pcm_bytes)` 进无界队列。"""
        if not pcm:
            return
        if len(pcm) < int(SAMPLE_RATE * MIN_SEGMENT_SECONDS) * SAMPLE_WIDTH:
            # 不足半秒（V1 `pipeline.py:127` 那行注释的意思；那个表达式本身有量纲笔误，
            # 见 `MIN_SEGMENT_SECONDS` 上的说明）：不识别，也不写行
            return
        with self._lock:
            self.segments_seen += 1
        self.segments.put((source, float(captured_at), bytes(pcm)))

    # -- worker -----------------------------------------------------------

    def _worker_loop(self) -> None:
        """**唯一**的消费线程：顺序消费两路的段（显存只有一份）。

        **队列空的那一小会也要继续等**：段是按「2 秒静默出段」的节奏来的，中间必然有空档；
        队列一空就退出等于把 worker 提前收摊（后面来的段全成了孤儿）。收队的条件因此是
        「收队信号已下 **且** 队列真的空了」—— 只有 `stop()` 会下那个信号，而它在设完信号
        之后还会再等一小会，队列里剩下的段因此不会被空档抢跑丢掉。
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
        """一段的完整处理：噪声预筛 → 识别 → 文本级兜底 → 一行 / 一个占位符 + 一条日志。"""
        import numpy as np

        audio = np.frombuffer(pcm, dtype=np.int16)
        audio_seconds = len(audio) / SAMPLE_RATE
        # **同一支钟**：`captured_at` 是 `tail_archive()` 的 `on_chunk(data, time.time())`
        # （墙钟，:355），这里取「worker 把它取出来那一刻」也必须用墙钟 —— 混进
        # `time.monotonic()`（与本机墙钟差着十几亿秒）的话 `max(0.0, …)` 会把读数
        # 恒定夹成 `0.0`，而 `wait=` 正是本单元唯一一条「流水线落后」的可观测前兆
        # （V1 `mnotes_core/pipeline.py:140` 的 `waited = time.time() - captured_at` 同此）。
        queued_at = time.time()
        waited = max(0.0, queued_at - captured_at)

        # 噪声预筛命中 → **静音，不写行**（连占位符也不写）：phase1 `glm.py` 的
        # `_is_noise_segment` 把这一档记成 `reason == "noise"`，语义是「这一段落是静音」——
        # 它按一次成功的「什么都没识别出来」记账，因此既不进失败计数、也不落占位符
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
            with self._lock:
                self._model_failed = True
                self.model_error = f"{type(exc).__name__}: {exc}"
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
            with self._lock:
                self._model_failed = False
            self._finish_failed(source, captured_at, waited, audio_seconds, text, reason, duration, tokens)
            return

        with self._lock:
            self._model_failed = False
        # 文本级兜底剥离（第四层防护的兜底；phase1 的 `transcribe_segment` 内部已经剥过一次，
        # 这里按 `phase2/task.md` §3 再兜一道：成功档拿到的就是剥离后的文本）
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
        """每段一条日志（字段照 V1 `docs/specs.md:143-149`）。

        行里的 `source=` 是本单元加的一列：两路共用一个 worker，出了事要能看出是哪一路。
        **`wait` 持续增长就是流水线落后的前兆**：它是「这一段在队列里等了多久」，
        正常时应贴着 0；一直往上爬说明 worker 追不上读线程。
        """
        _log.info(
            "seg %s audio=%.1fs asr=%.1fs wait=%.1fs tokens=%d reason=%s source=%s",
            _stamp(captured_at), audio_seconds, duration, waited, tokens, reason, source,
        )

    # -- 落盘 -------------------------------------------------------------

    def _write_line(self, source: str, captured_at: float, text: str) -> None:
        """追加一行到这一路的产物（**追加模式、逐行 flush**）。

        行的形状：`[HH:MM] 文本` —— 分钟级时间戳取**采集时刻**（`capture_ts`），
        两路各写各的文件（`meeting_transcript_<source>.md`）。

        「写一行」与「收队关句柄」共用同一把锁：`stop()` 在 worker join **之后**才
        `_close_handles()`，因此正常路径上两者不会撞；但 join 有上界（预算 + 余量都耗完时
        worker 可能还在写），那时若把 write 放在锁外，就会撞上「句柄已经关掉 / 已经置空」
        这两条竞态。写在锁里之后，两种情况都收得很干净：写完了才关，或者发现句柄已空、
        重开一个继续写。
        """
        path = os.path.join(str(self.session_dir), TRANSCRIPT_FILE_TEMPLATE.format(source=source))
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
                _log.warning("关转写产物失败（如实记账，不静默）", exc_info=True)

    # -- 停 ---------------------------------------------------------------

    def stop(self) -> dict[str, Any]:
        """关读线程 → flush 尾段 → **在收尾预算内**把队列里剩下的段尽量做完。

        返回记账字典（`budget` / `unfinished` / `note` / `segments_seen` / `segments_done`），
        供 `meeting/backends/transcribe.py` 复述进 log。

        **`unfinished` 算的是「还没做完」而不是「队列里还剩几段」**：worker 是「先 `get()`
        再处理」，预算耗完那一刻它手上那一段已经不在队列里了 —— 漏掉它就等于
        「在飞的那一段被静默丢掉」，而那正是 spec 不许的那件事。

        **返回即收队完成**：读线程与 worker 都 join 过了（join 的上界 = 收尾预算 + 余量），
        因此调用方不需要再猜「线程收干净了没有」。
        """
        with self._lock:
            if self._sealed:
                return self._drain_note(budget=0.0, unfinished=0)
            self._sealed = True

        self._stop.set()
        self._join_readers(reason="收尾")

        # 读线程已经停了，手上还压着的尾巴在这里送出（不丢半段）
        for source in self.sources:
            vad = self._vads.get(source)
            if vad is None or not vad.has_pending:
                continue
            cap = time.time()
            vad.flush(lambda pcm, src=source, ts=cap: self._enqueue(src, ts, pcm))

        self._draining.set()
        backlog = self.segments.qsize()
        budget = float(self._budget_fn(backlog, self._segment_timeout))
        # 收队 deadline：「在预算内尽量做完」这段窗口的右端（`worker_alive()` 读它）
        self._drain_deadline = time.monotonic() + budget
        worker = self._worker
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=budget + DRAIN_JOIN_SLACK_SECONDS)
        # 收队走完就把句柄放开：`worker_alive()` 因此不再靠「还在收尾预算里」那一档兜着
        # （那一档只描述 join 还没结束的窗口，`stop()` 返回之后它就不该再算「活着」）
        self._worker = None
        # **「没做完」= 队列里剩下的 + 已经被 worker 取走、还在处理的那一段**。
        # worker 是「先 `get()` 再处理」（`:551-568`），所以在预算耗完那一刻，它手上那一段
        # 早就离开了队列 —— 只看 `qsize()` 的话，真实预算（下限 30 秒）下这一格恒为 0，
        # `unfinished` 与那句「如实记账」的 warning 在生产路径上永远不出现（空头支票）。
        # `segments_seen - segments_done` 正好是这两部分的和：入队计数全部 +1、
        # 每处理完一段（`task_done()` 那一格）才 +1，差集就是还没做完的那些。
        with self._lock:
            outstanding = int(self.segments_seen) - int(self.segments_done)
        unfinished = max(self.segments.qsize(), outstanding)
        note = self._drain_note(budget=budget, unfinished=unfinished)
        if unfinished:
            _log.warning(
                "转写收尾预算用尽：还有 %s 段没做完（预算 %.1fs = 30 + %s × (%.1fs + 5)，"
                "封顶 300s）；音频归档完整、缺的只是临时稿的尾巴 —— 如实记账，不静默丢",
                unfinished, budget, backlog, self._segment_timeout,
            )
        self._close_handles()
        return note

    def _join_readers(self, *, reason: str) -> None:
        """把读线程 join 干净（**幂等**，可重复调）。

        收尾与「换一场」都会走它：写线程/读线程是 daemon，不 join 的话它们会跨着用例继续
        轮询别人留下的文件句柄（同一进程里连跑多场时，那是实打实的资源泄漏）。

        每一路等的上界是 `READER_JOIN_TIMEOUT_SECONDS`（**逐路**各算一次），
        于是「`stop()` 从进来到 worker join 开始」这一段的最坏耗时 = 该上界 × 路数。
        """
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

    # -- 读数（`TranscriberBackend` 的三态与两个计数） ----------------------

    def backlog_depth(self) -> int:
        """队列里还没处理的段数（**无界队列的 `qsize()`**，不额外记账、不偷偷丢弃）。"""
        return int(self.segments.qsize())

    def worker_alive(self) -> bool:
        """worker 还活着吗。

        「还活着」包含两档：正常消费中（线程活着），以及**收队中但还没过 deadline**
        （`_draining` 已设、收尾预算还没耗完 —— 这一档正是「在预算内尽量做完」的那段窗口）。
        """
        worker = self._worker
        if worker is None:
            return False
        if worker.is_alive():
            return True
        deadline = self._drain_deadline
        return self._draining.is_set() and deadline is not None and time.monotonic() <= deadline

    def readers_alive(self) -> bool:
        """两路读线程都还活着吗（一路读崩了就是 `False`，健康度如实降档）。"""
        if not self._reader_threads:
            return False
        return all(thread.is_alive() for thread in self._reader_threads.values())

    def writer_finished(self) -> bool:
        """**写行的那个线程真的退出了吗**（复审 m4 的判据就落在这一格上）。

        与 `worker_alive()` 的差别是两条：`stop()` 收队时会把 `_worker` 置空（于是
        `worker_alive()` 当场变 `False`，而线程可能还在 `_write_line()` 里），
        以及「还在收尾预算内」那一档在 `worker_alive()` 里算活着、在这里不算。
        写产物行的只有 worker 这一条线程（`_write_line()` 的唯一调用点在 `_handle_segment()`），
        因此**它退出了就再也没有人能重开产物句柄** —— `close_transcripts_when_drained()`
        等的正是这一刻。
        """
        thread = self._worker_thread
        return thread is None or not thread.is_alive()

    def close_transcripts_when_drained(self, *, timeout: float | None = None) -> bool:
        """等 worker 把手上那一段写完，再把产物句柄收干净（复审 m4）。

        **为什么要再关一次**：`stop()` 末尾那次 `_close_handles()` 关不掉**之后**才写的行 ——
        worker 在预算耗完那一刻手上还扣着一段时，`stop()` 返回之后它把那一行写出来，
        `_write_line()` 发现句柄已空就重开一个并记回 `self._handles`，那个句柄此后没人关
        （第二次 `stop()` 走的是幂等那一格，直接返回）。同一进程里连开多场就是一场漏一个 fd。

        **为什么不能就地再关一次**：worker 可能正要写最后一行，关在它前面它就把句柄重开了。
        因此这里先等**写行的那条线程**退出（`writer_finished()`），等到了才关 —— 它退出之后
        `_write_line()` 不可能再被调用，这一次关闭就是干净的。

        `timeout` 是等的上界（缺省 = 本场单段超时 + `DRAIN_JOIN_SLACK_SECONDS`，与
        `pipeline.stop()` 等 worker 的余量同源）。等不到就**不关**：把句柄留着、如实记一条
        日志，让那条 daemon 线程自己写完（进程退出时由 OS 回收）—— 硬关的话会把它的最后一行
        写进一个已关闭的句柄。

        返回 `True` = 已经收干净（含「本来就没有在飞的写者」这一档）。
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

    def model_ok(self) -> bool:
        """模型这一侧还好吗：最近一次识别没有因为加载 / 推理失败而抛错。"""
        return not self._model_failed


def _sources_for_mode(mode: str) -> tuple[str, ...]:
    """本场读哪几路归档：与采集侧同一套取值域（`meeting.capture.engine`）。

    采集侧的 `sources_for_mode()` 是唯一口径来源，这里 import 它（不抄一份取值域）。
    """
    from meeting.capture.engine import sources_for_mode

    return tuple(sources_for_mode(mode)) if mode in ("both", "mic", "loopback") else tuple(SOURCE_IDS)


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
    "tail_archive",
    "validate_transcript_health",
]
