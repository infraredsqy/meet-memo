"""`tests/test_asr_pipeline.py` —— 实时转写流水线（进程内、无真实模型、无真实声卡）。

`9e5db89c` phase2 的落地口径 1–6 在这里逐条落到用例上：

  - **时间戳是采集时刻**：每一行的时间戳落在「那段音频写进归档的墙钟窗口」之内
    （照 `tests/test_capture_accounting.py` 的口径记下写入时刻，不靠固定 sleep 凑时序）；
  - **落盘格式**：两路各一个 `meeting_transcript_<source>.md`，每行匹配 `^\\[\\d{2}:\\d{2}\\] `；
  - **队列语义**：段队列无界（`maxsize == 0`）、顺序消费、源码里没有 busy-drop 路径；
  - **三档占位符**：超时 / 判环 / 无输出各自落成规定的方括号形状、**不含 `**`**，
    用 `docs/v1-lineage.md` A7 的下游正则 match 之后 `spk` 组是 `None`；
  - **噪声段不写行**（连占位符也不写）；
  - **收尾预算**：`30 + n × (timeout + 5)`、下限 30、封顶 300；预算耗完还有剩段时
    **如实记账**（`caplog` 断言那一行）。

**没有一条用例加载真模型、打开真声卡**：模型是注入的替身，VAD 是确定性的能量阈值替身。
"""

from __future__ import annotations

import contextlib
import logging
import re
import struct
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from meeting import config as config_module
from meeting.asr.pipeline import (
    BACKLOG_BEHIND_THRESHOLD,
    FAILURE_LABELS,
    FRAME_BYTES,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    TRANSCRIPT_FILE_TEMPLATE,
    TranscriptionPipeline,
    VoiceActivitySegmenter,
    _format_failure,
)
from meeting.service.runtime import compute_drain_budget
from meeting.session import archive_name

#: 慢档标记：`pytest.mark.slow` 的归位判据与实测数据见 `docs/service.md` 第 7 章。
pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# 常量与假件
# ---------------------------------------------------------------------------

#: 假 VAD 的判定阈值：一帧里最大样本绝对值超过它就当语音（确定、与 webrtcvad 无关）
SPEECH_AMPLITUDE = 4000

#: 用例的显式等待上限（秒；单条用例合计 ≤5 秒的看门之内）
WAIT_TIMEOUT_SECONDS = 3.0

#: 用例注入的静默窗口（秒）——生产值是 `SILENCE_TIMEOUT_SECONDS = 2.0`。
#: 判据本身没变（还是「离上一块语音多久了」），只是把**等它成立**的那段时间压短，
#: 好让整文件耗时留在 10 秒线以内（单条仍守住 5 秒看门）。
TEST_SILENCE_TIMEOUT_SECONDS = 0.3

#: 每段日志里排队延迟那一格（`wait=%.1fs`，`docs/specs.md:143-149` 的字段）
WAIT_RE = re.compile(r"wait=([\d.]+)s")

#: 一行转写的形状（`docs/v1-lineage.md` A7 那条下游正则的**收紧变体**）。
#:
#: A7 的原件住在 V1 的 `.work/handoff/split_raw.py:12-13`：
#: `r"^\[(\d{2}:\d{2}(?::\d{2})?)\]\s*(?:\*\*(?P<spk>.+?)\*\*\s*:?\s*)?(?P<text>.*)$"`。
#: 本用例这一份与它有**四处**不同，都是**按 A7 的口径收紧**、不是等价替换（复审 m7）：
#:
#:   1. 时间戳只吃 `[HH:MM]`（A7 另允许秒级的 `[HH:MM:SS]`）—— **我们产出的就是分钟级**
#:      （`meeting/asr/pipeline.py` 的 `_stamp()`），收紧之后「多出一个秒段」这种形状偏差
#:      在本用例里就立不住了；
#:   2. 说话人组用 `[^*]+`（A7 是 `.+?`）：占位符里那个 `**` 因此不可能被吃进说话人组；
#:   3. 这一份要求说话人之后那个冒号**紧跟着** `**`（A7 允许中间夹着空白）；
#:   4. 正文用 `.+`（A7 是 `.*`）：空正文在我们的产物里不成立。
#:
#: 四条收紧都不影响 `task.md` 完成判定 5 要的那件事：用这一份 match 三档占位符，
#: `spk` 组仍是 `None`、`text` 仍是占位符本身（`review3.md` 的 m7 另用 A7 原件复核过同一结论）。
LINE_RE = re.compile(r"^\[(\d{2}:\d{2})\]\s*(?:\*\*(?P<spk>[^*]+)\*\*:\s*)?(?P<text>.+)$")

#: 落盘行的开头（`phase2/task.md` 完成判定 2）
STAMPED_LINE_RE = re.compile(r"^\[\d{2}:\d{2}\] ")

#: 噪声预筛与文本兜底的替身（打在 `meeting.asr.pipeline.glm_module` 那份模块对象上）
PIPELINE_MODULE = "meeting.asr.pipeline"


class FakeVad:
    """确定性的 VAD 后端：一帧里最大样本绝对值超过阈值就当语音。

    `webrtcvad.Vad` 的 `is_speech(frame_bytes, sample_rate)` 同形接口，用例因此不依赖
    真 VAD 的判定细节（也就不必为了「切出一段」去凑真实语音波形）。
    """

    def __init__(self, threshold: int = SPEECH_AMPLITUDE) -> None:
        self.threshold = int(threshold)
        self.frames = 0

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        del sample_rate
        self.frames += 1
        samples = struct.unpack(f"<{len(frame) // 2}h", frame)
        return max(abs(value) for value in samples) >= self.threshold


class FakeTranscriber:
    """假模型：按脚本吐 `TranscribeResult`（队列空时给一条成功结果）。"""

    def __init__(self, script: list | None = None, *, sleep_seconds: float = 0.0) -> None:
        self.script = list(script or [])
        self.sleep_seconds = float(sleep_seconds)
        self.calls: list[bytes] = []

    def transcribe_segment(self, audio_int16):
        from meeting.asr.glm import TranscribeResult

        self.calls.append(bytes(audio_int16.tobytes()))
        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        if self.script:
            item = self.script.pop(0)
            if callable(item):
                return item()
            return item
        return TranscribeResult(text="嗯，这是一段测试文本。", ok=True, reason="ok", tokens=9)


class BlockingTranscriber:
    """**被按住的模型**：一直等到 `release` 被设上（或 `hold_seconds` 到点）才返回。

    解耦证明里按 worker 的那一只手。`hold_seconds` 是**上界**而不是节拍：用例放行之后
    它立刻返回，所以「按住多久」这件事完全由用例说了算（不靠 sleep 凑时序）。
    """

    def __init__(self, *, hold_seconds: float = 3.0) -> None:
        self.hold_seconds = float(hold_seconds)
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def transcribe_segment(self, audio_int16):  # noqa: ARG002 - 只要把 worker 按住
        from meeting.asr.glm import TranscribeResult

        self.calls += 1
        self.started.set()
        self.release.wait(self.hold_seconds)
        return TranscribeResult(text="卡住之后才回来的一行。", ok=True, reason="ok")


class RaisingTranscriber:
    """**会抛错的模型**（解耦证明里的另一只手：模型崩了也要继续落盘）。"""

    def __init__(self) -> None:
        self.calls = 0

    def transcribe_segment(self, audio_int16):  # noqa: ARG002
        self.calls += 1
        raise RuntimeError("假模型按要求抛错")


class StopInsideTheModel:
    """**在飞的那一段里叫停**：`transcribe_segment()` 里直接调 `pipeline.stop()`。

    为什么只有这一手能在**真实预算**下验到「在飞的那一段」：worker 是「先 `get()` 再处理」，
    它手上那一段在 `stop()` 读队列的那一刻早就离开了队列，于是「预算耗完还有剩段」这件事
    只在 join **超时**（worker 还在跑）时才成立 —— 真公式的下限是 30 秒，用例等不起。

    `stop()` 自己有一条现成的分支管这一档（`worker is not threading.current_thread()`
    就不 join），走它就不必等那 30 秒；预算照旧是**真公式算出来的那个数**（队列空 → 下限 30），
    用例连一只小预算算盘都没有注入。
    """

    def __init__(self) -> None:
        self.pipeline: TranscriptionPipeline | None = None
        self.entered = threading.Event()
        self.stopped = threading.Event()
        self.note: dict | None = None

    def transcribe_segment(self, audio_int16):  # noqa: ARG002
        from meeting.asr.glm import TranscribeResult

        self.entered.set()
        assert self.pipeline is not None, "用例该在 start() 之前把流水线接上"
        self.note = self.pipeline.stop()
        self.stopped.set()
        return TranscribeResult(text="在飞的那一段照样落一行。", ok=True, reason="ok")


def silent_pcm(seconds: float) -> bytes:
    """`seconds` 秒的全零 PCM（16k / 单声道 / int16）。"""
    return b"\x00\x00" * int(SAMPLE_RATE * seconds)


def speech_pcm(seconds: float, amplitude: int = 12000) -> bytes:
    """`seconds` 秒的「语音」（常量非零样本，够假 VAD 判成语音）。"""
    samples = int(SAMPLE_RATE * seconds)
    return struct.pack(f"<{samples}h", *([amplitude] * samples))


def config_default(key: str, default=None):
    """配置层的缺省取值（用例里替换 `config_module.get` 时，其余键照常落回缺省）。"""
    del default
    return config_module.default_for(key)


class LogCapture(logging.Handler):
    """直接挂在 `meeting.asr.pipeline` 这个 logger 上的记录器。

    为什么不用 `caplog`：本项目的 logger（`meeting.*`）是**命名 logger 且 `propagate=False`**
    （`meeting/logging_setup.py` 的口径：不污染第三方），而 `caplog` 抓的是 root 那一层 ——
    于是这里一条都抓不到。挂在自己的 logger 上拿到的是同一批记录的正身。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def capture_pipeline_log():
    """临时挂一个 `LogCapture`（退出时摘掉、级别还原，不留副作用）。

    本进程里 `meeting.asr.pipeline` 这个 logger 没有自己的级别（有效级别落在 root 的
    WARNING 上），所以除了挂 handler，还要**临时把级别降到 INFO** —— 否则每段一条的
    `seg … wait=…`（INFO 级）在 handler 之前就被拦掉了，用例会以为「一条日志都没有」。
    """

    handler = LogCapture()
    logger = logging.getLogger(PIPELINE_MODULE)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)



def line_texts(path: Path) -> list[str]:
    """读一路产物里的**非空文本**行（噪声段不写行，因此产物里本来就没有空行）。

    产物还没出现（或正被写线程追加的那一瞬读不动）时返回空表：这个助手既被断言用、
    也被 `wait_for()` 的探针用，探针**不能因为「文件还没建出来」就抛出去** ——
    那会把「再等一会」变成一条红。
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    return [
        match.group(0)
        for match in (LINE_RE.match(line) for line in raw.splitlines())
        if match
    ]


def stamp_of(line: str) -> str:
    return LINE_RE.match(line).group(1)


class SegmentFeed:
    """把音频按「一段一段的节奏」喂进一路 VAD，并记下**每一段自己的起止写入时刻**。

    **合成时钟**：起点取 `time.time()`，此后按**音频时长**往前走（一块 0.3 秒的音频就前进
    0.3 秒）。静默判据（V1 `vad.py:57` 的「离上一块语音多久了」）因此能在即时喂入的小块上
    真的成立 —— 用真实墙钟连喂七块只过去几毫秒，那条判据永远不成立。这是**节拍模拟**，
    不是「用 sleep 凑时序」：用例一秒都不用等。

    **合成时钟与真实墙钟的偏离，正是这一手能验出「时间戳取自哪里」的原因**：喂完一整段
    （4 块语音 + 静默）真实只过去几毫秒，而合成时钟已经走了 1.8 秒。于是断言「这一行的
    时间戳 = 切出它的那一块被写入的时刻」对「改成 ASR 返回时再取」是敏感的
    （那会取到真实墙钟，差着一整个段的时长）。

    每一段记成 `(start, end)`：

      - `start` = **本段**第一块音频被写入的时刻（上一段切出去之后重新起算）；
      - `end`   = **切出本段的那一块静默**被写入的时刻 —— 也就是流水线盖章该取的那一刻
        （`feed()` 在这一块上判定成立、`emit` 就在这一块的处理里发生）。

    窗口因此只覆盖**这一段自己的音频**，不再被整串 12 块静默或者上一段的块拉长。
    """

    def __init__(
        self, pipeline: TranscriptionPipeline, source: str, *, chunk_seconds: float = 0.3
    ) -> None:
        self.pipeline = pipeline
        self.source = source
        self.chunk_seconds = float(chunk_seconds)
        self.clock = time.time()
        #: 每一段的 `(start, end)`，按切出来的顺序排
        self.windows: list[tuple[float, float]] = []
        self._open_at: float | None = None

    # -- 内部 -------------------------------------------------------------

    def _record(self, pcm: bytes, at: float) -> None:
        self.pipeline._enqueue(self.source, at, pcm)
        self.windows.append((at if self._open_at is None else self._open_at, at))
        self._open_at = None

    def _feed(self, chunk: bytes) -> None:
        at = self.clock
        if self._open_at is None:
            self._open_at = at
        self.pipeline._vads[self.source].feed(
            chunk, lambda pcm, at=at: self._record(pcm, at), at
        )
        self.clock += len(chunk) / (SAMPLE_RATE * SAMPLE_WIDTH)

    # -- 用例用的三个动作 --------------------------------------------------

    def speech(self, pattern: str) -> None:
        """喂一串块（`s` 语音 / `0` 静默），每块的音频时长都是 `chunk_seconds`。"""
        for ch in pattern:
            self._feed(
                speech_pcm(self.chunk_seconds) if ch == "s" else silent_pcm(self.chunk_seconds)
            )

    def cut(self, *, max_chunks: int = 12) -> float:
        """喂静默块直到这一段被切出来；返回**切出它的那一块静默写入的时刻**。

        静默多久够由这一路 VAD 的 `silence_timeout` 说了算（用例经
        `make_pipeline(silence_timeout=…)` 注入），这里只按块往前走、最多喂 `max_chunks` 块。
        """
        before = len(self.windows)
        for _ in range(max_chunks):
            self._feed(silent_pcm(self.chunk_seconds))
            if len(self.windows) > before:
                return self.windows[-1][1]
        raise AssertionError(f"{max_chunks} 块静默都没把这一路手上那段切出来")

    def flush(self) -> None:
        """把还在手上的尾巴交出来（`stop()` 之前调；没有尾巴就什么都不做）。"""
        vad = self.pipeline._vads[self.source]
        if not vad.has_pending:
            return
        at = self.clock
        if self._open_at is None:
            self._open_at = at
        vad.flush(lambda pcm, at=at: self._record(pcm, at))


class LineRecorder:
    """`line_sink` 替身：按顺序记下每一条 `TranscriptLine`（连带它带的时间戳）。"""

    def __init__(self) -> None:
        self.lines: list[object] = []

    def __call__(self, line) -> None:
        self.lines.append(line)

    def for_source(self, source: str) -> list[object]:
        return [line for line in self.lines if getattr(line, "source", None) == source]

    def stamps(self, source: str) -> list[float]:
        return [float(getattr(line, "ts", 0.0)) for line in self.for_source(source)]


def clock_stamp(moment: float) -> str:
    """合成时钟上的某一刻 → `HH:MM`（与流水线 `_stamp()` 同一口径）。"""
    return datetime.fromtimestamp(moment).strftime("%H:%M")


def wait_for(predicate, *, timeout: float = WAIT_TIMEOUT_SECONDS, what: str = "条件"):
    """同步等待（测试一律同步写；不加固定 sleep 凑时序）。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.01)
    raise AssertionError(f"等待超时（{timeout}s）：{what}（最后观察值：{last!r}）")


def make_pipeline(
    session_dir: Path,
    transcriber,
    *,
    timeout: float = 20.0,
    budget_fn=None,
    line_sink=None,
    silence_timeout: float = TEST_SILENCE_TIMEOUT_SECONDS,
):
    """一条两路、假 VAD、假模型的流水线（`speech_pcm` 的振幅保证过噪声预筛）。

    `silence_timeout` 注入的是**分段器自己的判据**（`VoiceActivitySegmenter` 的构造参数）：
    生产值是 2.0 秒，用例把它压到 0.3 秒 —— 走真归档的那几条因此不必为「等判据成立」
    各睡 2 秒（整文件耗时是有一条线的：单个测试文件 ≤10 秒）。
    """
    del session_dir  # 只用于调用点的可读性：场次目录在 `start()` 时给
    return TranscriptionPipeline(
        transcriber=transcriber,
        line_sink=line_sink if line_sink is not None else (lambda line: None),
        # 每路一个 `VoiceActivitySegmenter`，它背后的 VAD 是那个确定性的假替身
        vad_factory=lambda: VoiceActivitySegmenter(
            silence_timeout=silence_timeout, vad_backend=FakeVad()
        ),
        segment_timeout=timeout,
        read_chunk_bytes=FRAME_BYTES,
        idle_seconds=0.005,
        archive_poll_seconds=0.005,
        budget_fn=budget_fn,
    )


def wait_for_no_asr_threads(*, timeout: float = 5.0) -> None:
    """等到进程里**一条 `asr-` 线程都不剩**（用例收场与开场都用它）。

    同一进程里连跑多条用例时，上一条用例留下的流水线线程会继续轮询它自己的归档路径 ——
    资源没回收，而且下一条用例想数「本流水线起了几条线程」时会被它们干扰。
    这条助手把「等收队落定」这件事收成一处（事件驱动：轮询到条件成立就走，不空等）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not [t for t in threading.enumerate() if t.name.startswith("asr-")]:
            return
        time.sleep(0.01)
    left = [t.name for t in threading.enumerate() if t.name.startswith("asr-")]
    raise AssertionError(f"{timeout}s 之后还有转写线程没退出：{left}")


@pytest.fixture(autouse=True)
def no_stray_asr_threads():
    """用例**开场与收场都**先等转写线程收干净（`asr-` 线程是本节点的资源，归本文件管）。"""
    wait_for_no_asr_threads()
    try:
        yield
    finally:
        wait_for_no_asr_threads()


@pytest.fixture()
def session_dir(tmp_path: Path) -> Path:
    path = tmp_path / "260922"
    path.mkdir()
    return path


# ---------------------------------------------------------------------------
# 队列语义（无界 + 单 worker 顺序消费）
# ---------------------------------------------------------------------------


def test_segment_queue_is_unbounded_and_single_worker(session_dir: Path):
    """段队列**无界**（`maxsize == 0`），且**本流水线**只起了一个 worker 与两路读线程。

    只看 `asr-` 开头的新线程：同一个进程里别的用例可能留着自己的流水线（全量跑时会），
    按名字前缀差分出来的那几条只可能来自本用例刚起的这一条。
    """
    pipeline = make_pipeline(session_dir, FakeTranscriber())
    assert pipeline.segments.maxsize == 0, "段队列必须无界（禁止 busy-drop）"
    before = {thread.name for thread in threading.enumerate() if thread.name.startswith("asr-")}
    assert not before, f"开场该一条 asr- 线程都没有（autouse 夹具等过）：{sorted(before)}"
    pipeline.start("both", str(session_dir))
    try:
        started = {
            thread.name for thread in threading.enumerate() if thread.name.startswith("asr-")
        }
        assert started == {"asr-worker", "asr-reader-mic", "asr-reader-loopback"}, (
            f"本流水线该只起这三条线程，实际起了：{sorted(started)}"
        )
    finally:
        pipeline.stop()


def test_segments_under_half_a_second_are_never_enqueued(session_dir: Path):
    """**不足半秒的段不识别、也不落行**（判据出处是 V1 `mnotes_core/pipeline.py:127`）。

    **数值口径按 task.md 的 0.5 秒**：V1 那一行写的是 `len(seg_bytes) < SAMPLE_RATE * 0.5`，
    拿**字节数**去比 `16000 × 0.5 = 8000`（按字节折算是 0.25 秒）—— 那是 V1 自身的一处
    **量纲笔误**，而它那行注释写的是 `< 0.5s 跳过`。V2 取注释与 task.md 的意图：
    `int(SAMPLE_RATE * 0.5) * SAMPLE_WIDTH = 16000` 字节，也就是真的半秒。
    这条用例断的是「判据在位、且丢的是尾音」（丢的是 `0.2–0.5` 秒这一段，不跟 V1 的笔误走）。

    这是队列语义那条硬要求允许的**唯一**一条丢弃路径（丢的是尾音），判据住在 `_enqueue` 的
    入口那一格 —— 从队列出口取段的用例一条都碰不到它，所以要在这里直接投一段 0.2 秒的语音。
    对照组是后面那一段 0.6 秒的语音：它照常进队、照常落成一行（证明这个入口不是把什么都挡了）。
    """
    transcriber = FakeTranscriber()
    pipeline = make_pipeline(session_dir, transcriber)
    pipeline.start("mic", str(session_dir))
    try:
        pipeline._enqueue("mic", time.time(), speech_pcm(0.2))  # 0.2 秒 < MIN_SEGMENT_SECONDS
        assert pipeline.segments_seen == 0, "不足半秒的段不许进队列"
        assert pipeline.segments.qsize() == 0
        pipeline._enqueue("mic", time.time(), speech_pcm(0.6))  # 对照组：够半秒
        wait_for(
            lambda: line_texts(session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source="mic")) or None,
            what="够半秒的那一段落成一行",
        )
    finally:
        pipeline.stop()

    lines = line_texts(session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source="mic"))
    assert len(lines) == 1, f"只有够半秒的那一段该落行：{lines}"
    assert pipeline.segments_seen == 1
    assert len(transcriber.calls) == 1, "不足半秒的段连模型都不该惊动"


def test_placeholders_are_the_frozen_shapes_without_speaker_markup():
    """三档占位符的形状逐条钉住：双层方括号、**不含 `**`**、下游正则的 `spk` 组为 `None`。"""
    assert _format_failure("timeout", duration=20.4, timed_out=True) == "[[识别失败: 超时 20s]]"
    assert _format_failure("loop") == "[[识别失败: 重复循环]]"
    assert _format_failure("empty") == "[[识别失败: 无输出]]"
    assert _format_failure("error") == "[[识别失败: 错误]]"
    assert FAILURE_LABELS["timeout"] == "识别失败: 超时"

    for placeholder in ("[[识别失败: 超时 20s]]", "[[识别失败: 重复循环]]", "[[识别失败: 无输出]]"):
        assert "**" not in placeholder
        match = LINE_RE.match(f"[09:31] {placeholder}")
        assert match is not None, placeholder
        assert match.group("spk") is None, placeholder
        assert match.group("text") == placeholder


# ---------------------------------------------------------------------------
# 进程内跑一条流水线（两路各出若干行 + 落盘格式 + 时间戳是采集时刻）
# ---------------------------------------------------------------------------


class SleepingTranscriber:
    """每一段先睡 `sleep_seconds` 再给一条成功结果（**拿它把「返回时刻」推后**）。

    用途：验时间戳的取值点。段在「读到这一块那一刻」就盖了章，模型慢只影响这一行**什么时候**
    写进产物，**不影响它带的时间戳** —— 这条判据是本单元的根因防线
    （V1 的事故就是「等 ASR 返回后再取时间戳」，于是一个退化段把后面几分钟压成一堆）。

    `entries` 记下每一次**进入**识别的时刻：判据「盖章早于模型被进入」用它，
    它比「盖章 ≈ 返回时刻 - sleep」这种反推稳得多（反推要信 `sleep` 的精度）。
    """

    def __init__(self, *, sleep_seconds: float = 0.6) -> None:
        self.sleep_seconds = float(sleep_seconds)
        self.entries: list[float] = []
        self.returns: list[float] = []

    def transcribe_segment(self, audio_int16):  # noqa: ARG002
        from meeting.asr.glm import TranscribeResult

        self.entries.append(time.time())
        time.sleep(self.sleep_seconds)
        self.returns.append(time.time())
        return TranscribeResult(text="慢模型回来的一行。", ok=True, reason="ok")


def test_pipeline_writes_two_transcript_files_with_capture_timestamps(session_dir: Path):
    """两路各出两行、落盘格式对、**每行的时间戳就是它那一段被切出来那一刻的采集时刻**。

    判据取的是**这一段自己的写入窗口**（`SegmentFeed.windows`）：

      - 左端 = 本段第一块音频被写入的时刻；
      - 右端 = **切出本段的那一块静默**被写入的时刻 —— 流水线盖章该取的就是这一刻。

    两条断言一起上：`line.ts` 与窗口右端**逐微秒对齐**（合成时钟比真实墙钟早跑完一整段，
    所以「改成 ASR 返回时再取」会差出一整个段的时长，这两条立刻红），
    落盘的分钟级戳也必须是右端那一刻的分钟。
    """
    transcriber = FakeTranscriber()
    recorder = LineRecorder()
    pipeline = make_pipeline(session_dir, transcriber, line_sink=recorder)
    pipeline.start("both", str(session_dir))
    feeds: dict[str, SegmentFeed] = {}
    try:
        for source in ("mic", "loopback"):
            feed = SegmentFeed(pipeline, source)
            for _ in range(2):
                feed.speech("s" * 4)
                feed.cut()
                feed.flush()
            feeds[source] = feed
        wait_for(lambda: transcriber.calls or None, what="假模型被调到")
        wait_for(
            lambda: all(
                (session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source=source)).exists()
                and len(line_texts(session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source=source))) >= 2
                for source in ("mic", "loopback")
            ),
            what="两路产物各至少两行",
        )
    finally:
        pipeline.stop()

    for source in ("mic", "loopback"):
        path = session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source=source)
        raw_lines = path.read_text(encoding="utf-8").splitlines()
        assert raw_lines, f"{source} 这一路该有落盘内容"
        for line in raw_lines:
            assert STAMPED_LINE_RE.match(line), f"{source} 的行形状不对：{line!r}"
        assert all("**" not in line for line in raw_lines)

        windows = feeds[source].windows
        assert len(windows) == 2, f"{source} 该切出两段：{windows}"
        # 窗口是**一段一个**的：后一段的起点在前一段切出去之后（不是整串共用一个大窗口）
        assert windows[0][0] < windows[0][1] < windows[1][0] < windows[1][1], windows

        lines = line_texts(path)
        assert len(lines) == len(windows), f"{source} 行数与段数对不上：{lines}"
        for line, (start, end) in zip(lines, windows):
            # **时间戳 = 切出这一段的那一刻的采集时刻**（合成时钟上的右端，逐微秒对齐）
            assert stamp_of(line) == clock_stamp(end), (
                f"{source} 的时间戳 {stamp_of(line)} 不是这一段被切出来那一刻"
                f"（{clock_stamp(end)}）：{line!r}（窗口 {start}–{end}）"
            )
        emitted = recorder.for_source(source)
        assert len(emitted) == len(windows), f"{source} 行出口与段数对不上：{emitted}"
        for line, (start, end) in zip(emitted, windows):
            assert float(line.ts) == pytest.approx(end, abs=1e-6), (
                f"{source} 的 line.ts={line.ts} 不等于这一段被切出来那一刻 {end}"
                f"（窗口 {start}–{end}）—— 「ASR 返回时再取」会差出一整个段的时长"
            )

    # 两路各写各的文件：内容互不串路（两路各两段，共四次识别）
    assert len(transcriber.calls) >= 4, transcriber.calls


def test_line_timestamp_is_the_capture_moment_while_the_model_is_slow(session_dir: Path):
    """**时间戳的取值点在「读到这一块」那一刻**：模型慢只推迟落盘，不挪动时间戳。

    这一条走**真归档 + 真时间**（读线程的时间戳取的是墙钟，合成时钟到这里就不作数了）：
    写三块语音 → 等静默判据成立（分段）→ 慢模型再睡 0.6 秒才返回。

    判据成对：盖章时刻必须**早于模型被进入**（`entries[0] + 0.05` 的余量），
    同时又要贴着「切出这一段的那一块静默被写进归档」的那一刻（±0.3 秒的读线程延迟）。
    模型返回比盖章晚 0.5 秒以上 —— 于是「时间戳 = ASR 返回时刻」那条退化写法两处都会红。
    整条用例的等待约 1 秒，在 5 秒看门之内。
    """
    transcriber = SleepingTranscriber(sleep_seconds=0.6)
    recorder = LineRecorder()
    pipeline = make_pipeline(session_dir, transcriber, line_sink=recorder)
    pipeline.start("mic", str(session_dir))
    try:
        # 段 1 的归档（`meeting/session.py` 的 `archive_name()`；尾读按段序号找文件）
        archive = session_dir / archive_name("mic", 1)
        handle = open(archive, "ab", buffering=0)  # 采集侧的样子：无缓冲追加写
        try:
            for chunk in (speech_pcm(0.3), speech_pcm(0.3), speech_pcm(0.3)):
                handle.write(chunk)
            # 让「离上一块语音超过 0.3 秒」这条判据成立（判据本身要求的等待）
            time.sleep(TEST_SILENCE_TIMEOUT_SECONDS + 0.1)
            silence_written = time.time()
            handle.write(silent_pcm(0.6))
        finally:
            handle.close()
        wait_for(lambda: transcriber.returns or None, what="慢模型返回")
        wait_for(
            lambda: line_texts(session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source="mic")) or None,
            what="这一行落盘",
        )
    finally:
        pipeline.stop()

    line = line_texts(session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source="mic"))[0]
    ts = recorder.stamps("mic")[0]
    entry = transcriber.entries[0]
    # ① 盖章早于（至多齐平于）模型被进入 —— 取返回时刻就会晚整整一个 sleep
    assert ts <= entry + 0.05, (
        f"时间戳 {ts} 落在模型被进入（{entry}）之后 0.05 秒以上：这一行盖的不是采集时刻"
    )
    # ② 盖章贴着「切出这一段的那一块静默被写进归档」的那一刻（读线程延迟 ≤0.3 秒）
    assert silence_written - 0.05 <= ts <= silence_written + 0.3, (
        f"时间戳 {ts} 不在那一块静默的写入窗口 [{silence_written - 0.05}, "
        f"{silence_written + 0.3}] 里"
    )
    assert stamp_of(line) == clock_stamp(ts), "落盘的分钟级戳与行出口的 ts 必须同源"
    # 秒级证据：模型返回时刻比盖章晚整整一个 sleep —— 取返回时刻就会晚 0.6 秒
    assert transcriber.returns[0] - ts >= 0.5, "这一幕要求模型真的慢，否则这条判据没有意义"


def test_pipeline_reads_from_the_archive_on_disk(session_dir: Path):
    """**音频从盘上来**：真读线程尾读 `meeting_audio_<source>_<段号>.pcm` 的新字节（不碰采集对象）。

    这一条走的是**真时间**：静默判据只有在墙钟真的走过那 0.3 秒之后才成立（用例注入的
    窗口，生产值是 2.0 秒）。模型这一侧故意慢 0.3 秒：判据「盖章早于模型被进入」
    因此咬得住「改成 ASR 返回时再取」这条退化。
    """
    transcriber = SleepingTranscriber(sleep_seconds=0.3)
    recorder = LineRecorder()
    pipeline = make_pipeline(session_dir, transcriber, line_sink=recorder)
    pipeline.start("both", str(session_dir))
    try:
        archive = session_dir / archive_name("mic", 1)
        # 采集侧的样子：无缓冲追加写（`meeting/capture/archive.py` 的 `open_pcm()`）
        written: list[float] = []
        handle = open(archive, "ab", buffering=0)
        try:
            for chunk in (speech_pcm(0.3), speech_pcm(0.3), speech_pcm(0.3)):
                written.append(time.time())
                handle.write(chunk)
            time.sleep(TEST_SILENCE_TIMEOUT_SECONDS + 0.1)  # 让静默判据真的成立
            written.append(time.time())
            handle.write(silent_pcm(0.6))
            written.append(time.time())
        finally:
            handle.close()
        wait_for(
            lambda: line_texts(session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source="mic")) or None,
            what="读线程从归档里读出第一段",
        )
    finally:
        pipeline.stop()

    path = session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source="mic")
    lines = line_texts(path)
    assert lines, "归档里有语音，产物里就该有一行"
    ts = recorder.stamps("mic")[0]
    cut_written = written[3]  # 切出第一段的那一块静默被写进归档的时刻
    assert cut_written - 0.05 <= ts <= cut_written + 0.3, (
        f"时间戳 {ts} 不贴着那一块静默的写入时刻 {cut_written}（读线程延迟该在 0.3 秒内）"
    )
    assert ts <= transcriber.entries[0] + 0.05, (
        f"时间戳 {ts} 落在模型被进入（{transcriber.entries[0]}）之后："
        "盖章该取读到这一块那一刻；取到 ASR 返回时就会晚出这一段模型耗时"
    )
    assert transcriber.returns[0] - ts >= 0.25, "模型慢过 0.3 秒；取返回时刻就会晚一整段"
    assert stamp_of(lines[0]) == clock_stamp(ts), "落盘的分钟级戳与行出口的 ts 必须同源"


def test_pipeline_keeps_reading_across_segments(session_dir: Path):
    """段 2 出现后读头**自己接过去**：按序拼接 = 连续时间轴，两段的语音各出一行。

    「分段会静默杀死会中转写」是分段落地最大的坑（读线程只认一个句柄、永远等同一个
    文件长出新字节）。这里把那口棺材钉死：段 1 出行之后落下段 2 的文件，第二段的语音
    照样出第二行，中间不需要任何重启。
    """
    transcriber = SleepingTranscriber(sleep_seconds=0.0)
    recorder = LineRecorder()
    pipeline = make_pipeline(session_dir, transcriber, line_sink=recorder)
    pipeline.start("mic", str(session_dir))
    transcript = session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source="mic")
    try:
        # 段 1：三块语音 + 真等静默判据成立 + 一块静默（把段切出来）
        with open(session_dir / archive_name("mic", 1), "ab", buffering=0) as handle:
            for _ in range(3):
                handle.write(speech_pcm(0.3))
            time.sleep(TEST_SILENCE_TIMEOUT_SECONDS + 0.1)
            handle.write(silent_pcm(0.6))
        wait_for(lambda: line_texts(transcript) or None, what="段 1 的第一行")
        # 段 2：一个新的段文件出现，读头要接过去
        with open(session_dir / archive_name("mic", 2), "ab", buffering=0) as handle:
            for _ in range(3):
                handle.write(speech_pcm(0.3))
            time.sleep(TEST_SILENCE_TIMEOUT_SECONDS + 0.1)
            handle.write(silent_pcm(0.6))
        wait_for(lambda: (lambda rows: rows if len(rows) >= 2 else None)(line_texts(transcript)),
                 what="段 2 的第二行")
    finally:
        pipeline.stop()

    lines = line_texts(transcript)
    assert len(lines) == 2
    # 两行的分钟级戳与各自被读到的那一刻同源（跨段不换章：盖章点还是「读到」那一刻）
    stamps = recorder.stamps("mic")
    assert len(stamps) == 2 and stamps[0] <= stamps[1]


def test_a_pause_window_of_digital_silence_produces_no_lines(session_dir: Path):
    """暂停窗口（整段的数字零）**一行转写都不产生**——静音不进正文（第 8 条的隐私线）。

    暂停写成同文件的数字零之后，这条由 VAD 与噪声预筛顺带保证；这里用真归档 + 真时间
    把它钉住：语音 → 一整段零 → 语音，产物里恰好两行，零窗一行都没有。
    """
    transcriber = SleepingTranscriber(sleep_seconds=0.0)
    recorder = LineRecorder()
    pipeline = make_pipeline(session_dir, transcriber, line_sink=recorder)
    pipeline.start("mic", str(session_dir))
    transcript = session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source="mic")
    try:
        with open(session_dir / archive_name("mic", 1), "ab", buffering=0) as handle:
            handle.write(speech_pcm(0.3) * 3)
            # 真等静默判据成立，再喂暂停窗口：第一块零把上一段切出来，其余的零没有内容
            time.sleep(TEST_SILENCE_TIMEOUT_SECONDS + 0.1)
            handle.write(silent_pcm(0.9))
            time.sleep(TEST_SILENCE_TIMEOUT_SECONDS + 0.1)
            handle.write(speech_pcm(0.3) * 3)
            time.sleep(TEST_SILENCE_TIMEOUT_SECONDS + 0.1)
            handle.write(silent_pcm(0.6))
        wait_for(
            lambda: (lambda rows: rows if len(rows) >= 2 else None)(line_texts(transcript)),
            what="两段语音的两行",
        )
    finally:
        pipeline.stop()

    lines = line_texts(transcript)
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# 每段日志的排队延迟（`wait=`：流水线落后的前兆）
# ---------------------------------------------------------------------------


def test_segment_log_reports_the_queue_wait_as_it_grows(session_dir: Path):
    """每段日志的 `wait=` 是**这一段在队列里等了多久**，并且会随排队增长。

    `docs/specs.md:143-149` 的字段里，`wait` 是唯一一条「流水线落后」的可观测前兆
    （V1 `mnotes_core/pipeline.py:140` 的 `waited = time.time() - captured_at`）。
    它要成立的前提是**两支钟同源**：盖章点写的是墙钟（`tail_archive()` 的
    `on_chunk(data, time.time())`），取段那一刻也必须读墙钟 —— 混进单调钟之后
    `max(0.0, …)` 会把读数恒定夹成 `0.0`，这条前兆在 log 里就永远显示不出落后。

    造法：按住 worker 之后，第二段的盖章点故意落在 0.4 秒之前 —— 它只能等第一段做完。
    读数若恒为 0.0，这条用例立刻红（受控破坏自证：把那一行改回 `time.monotonic()`）。
    """
    blocker = BlockingTranscriber(hold_seconds=3.0)
    pipeline = make_pipeline(session_dir, blocker)
    pipeline.start("mic", str(session_dir))
    try:
        pipeline._enqueue("mic", time.time(), speech_pcm(1.0))
        wait_for(lambda: blocker.started.is_set() or None, what="假模型把 worker 按住")
        with capture_pipeline_log() as captured:
            # 第二段的盖章点在 0.4 秒之前：它得排在第一条后面，排队延迟因此约 0.4 秒
            pipeline._enqueue("mic", time.time() - 0.4, speech_pcm(1.0))
            blocker.release.set()
            wait_for(lambda: pipeline.segments_done >= 2 or None, what="两段都被处理完")
    finally:
        blocker.release.set()
        pipeline.stop()

    waits = [
        float(match.group(1))
        for match in (WAIT_RE.search(message) for message in captured.messages)
        if match is not None
    ]
    assert len(waits) >= 2, f"两段各该有一条带 wait= 的日志：{captured.messages}"
    assert max(waits) >= 0.35, (
        f"排队延迟必须随排队增长（读数恒 0.0 就是混用了两支钟）：{captured.messages}"
    )


# ---------------------------------------------------------------------------
# 三档失败：各自落成规定的方括号形状
# ---------------------------------------------------------------------------


def test_failed_segments_land_as_placeholders(session_dir: Path, monkeypatch):
    """超时 / 判环 / 无输出三档各自落成占位符；**不带 `**`**、不带部分文本。"""
    from meeting.asr.glm import TranscribeResult

    script = [
        TranscribeResult(text="", ok=False, reason="timeout", duration=20.4, timed_out=True),
        TranscribeResult(text="", ok=False, reason="loop", tokens=400),
        TranscribeResult(text="", ok=True, reason="ok"),
    ]
    pipeline = make_pipeline(session_dir, FakeTranscriber(script))
    pipeline.start("mic", str(session_dir))
    try:
        feed = SegmentFeed(pipeline, "mic")
        for _ in range(3):
            feed.speech("s" * 4)
            feed.cut()
            feed.flush()
        wait_for(
            lambda: len(line_texts(session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source="mic"))) >= 3
            or None,
            what="三档失败各落一行",
        )
    finally:
        pipeline.stop()

    lines = line_texts(session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source="mic"))
    assert lines[:3] == [
        f"[{stamp_of(lines[0])}] [[识别失败: 超时 20s]]",
        f"[{stamp_of(lines[1])}] [[识别失败: 重复循环]]",
        f"[{stamp_of(lines[2])}] [[识别失败: 无输出]]",
    ]
    for line in lines:
        assert "**" not in line
        assert LINE_RE.match(line).group("spk") is None
    assert pipeline.failures == {"timeout": 1, "loop": 1, "empty": 1}


def test_noise_segment_writes_no_line_at_all(session_dir: Path, monkeypatch):
    """噪声段：**静音，不写行**（连占位符也不写），也不调模型。"""
    monkeypatch.setattr(
        f"{PIPELINE_MODULE}.glm_module._is_noise_segment", lambda audio: True, raising=True
    )
    transcriber = FakeTranscriber()
    pipeline = make_pipeline(session_dir, transcriber)
    pipeline.start("mic", str(session_dir))
    try:
        feed = SegmentFeed(pipeline, "mic")
        feed.speech("s" * 4)
        feed.cut()
        feed.flush()
        wait_for(lambda: pipeline.segments_done >= 1 or None, what="这一段被 worker 处理掉")
    finally:
        pipeline.stop()

    assert transcriber.calls == [], "噪声段不该浪费一次识别"
    path = session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source="mic")
    assert not path.exists() or path.read_text(encoding="utf-8") == ""


def test_raising_model_writes_the_error_placeholder_without_partial_text(session_dir: Path, monkeypatch):
    """模型抛错 → `[[识别失败: 错误]]`，**成功档的内部异常文本一个字都不许漏出来**。"""
    monkeypatch.setattr(
        f"{PIPELINE_MODULE}.glm_module._is_noise_segment", lambda audio: False, raising=True
    )
    transcriber = RaisingTranscriber()
    pipeline = make_pipeline(session_dir, transcriber)
    pipeline.start("mic", str(session_dir))
    try:
        feed = SegmentFeed(pipeline, "mic")
        feed.speech("s" * 4)
        feed.cut()
        feed.flush()
        wait_for(lambda: transcriber.calls or None, what="假模型抛错那一次被调到")
        wait_for(
            lambda: line_texts(session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source="mic")) or None,
            what="占位符落盘",
        )
    finally:
        pipeline.stop()

    line = line_texts(session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source="mic"))[0]
    assert line.endswith("[[识别失败: 错误]]")
    assert "假模型按要求抛错" not in line
    assert "Traceback" not in line


# ---------------------------------------------------------------------------
# 收尾预算（`30 + n × (timeout + 5)`，下限 30、封顶 300）
# ---------------------------------------------------------------------------


def test_drain_budget_matches_the_formula_and_the_cap(session_dir: Path):
    """预算是 `30 + n × (timeout + 5)`；下限 30、封顶 300（与 `compute_drain_budget` 同源）。

    积压量由**先把 worker 按住、再往队列里堆**造出来：不按住的话 worker 会一路把段吃掉，
    `stop()` 读到的积压量就成了一个随调度抖动的数（那正是「用 sleep 凑时序」的坑）。
    """
    assert compute_drain_budget(0, 20.0) == 30
    assert compute_drain_budget(2, 20.0) == 30 + 2 * 25
    assert compute_drain_budget(40, 20.0) == 300, "封顶 300"

    seen: list[tuple[int, float]] = []

    def spy(backlog: int, timeout: float) -> float:
        """预算算盘：记一笔，再按真公式算。

        记下来的**就是 `stop()` 那一刻看到的积压量** —— worker 全程被按住，队列因此是满的，
        判据不必跟调度节奏抢时间（这就是「按住再用」而不是「用 sleep 凑」的差别）。
        """
        seen.append((int(backlog), float(timeout)))
        return compute_drain_budget(backlog, timeout)

    blocker = BlockingTranscriber(hold_seconds=1.0)
    pipeline = make_pipeline(session_dir, blocker, timeout=20.0, budget_fn=spy)
    pipeline.start("mic", str(session_dir))
    try:
        pipeline._enqueue("mic", time.time(), speech_pcm(1.0))  # 这一段会被 worker 拿走
        wait_for(lambda: blocker.started.is_set() or None, what="假模型已经把 worker 按住")
        for _ in range(2):
            pipeline._enqueue("mic", time.time(), speech_pcm(1.0))
        note = pipeline.stop()
    finally:
        blocker.release.set()
        pipeline.stop()

    assert seen, "预算算盘必须被调到一次"
    backlog, timeout = seen[0]
    assert timeout == 20.0, f"段长取配置里的 asr.timeout_seconds：{seen}"
    assert backlog >= 2, f"按住 worker 之后队列里该堆着段：{seen}"
    assert note["budget"] == compute_drain_budget(backlog, 20.0)


def test_drain_budget_reports_unfinished_segments_honestly(session_dir: Path):
    """预算耗完还有剩段 → **如实记账**（log 里那一条要点名「几条没做完 + 音频完整」）。

    预算注入成 0.05 秒（真公式的下限是 30 秒，用例不真等它）；worker 被假模型按住，
    队列里剩下的段因此在预算内做不完 —— 这一档正是要验的那一档。
    """
    blocker = BlockingTranscriber(hold_seconds=3.0)
    pipeline = make_pipeline(
        session_dir,
        blocker,
        timeout=1.0,
        # 注入一只「预算 0.02 秒」的算盘：真公式的下限是 30 秒，用例不真等它
        budget_fn=lambda backlog, timeout: 0.02,
    )
    pipeline.start("mic", str(session_dir))
    try:
        pipeline._enqueue("mic", time.time(), speech_pcm(1.0))
        wait_for(lambda: blocker.started.is_set() or None, what="假模型已经把 worker 按住")
        for _ in range(4):
            pipeline._enqueue("mic", time.time(), speech_pcm(1.0))
        with capture_pipeline_log() as captured:
            note = pipeline.stop()
    finally:
        blocker.release.set()
        pipeline.stop()

    assert note["budget"] == 0.02
    assert note["unfinished"] >= 1, f"队列里该还有没做完的段：{note}"
    assert "还有" in note["note"] and "尾巴" in note["note"]
    honesty = [message for message in captured.messages if "收尾预算用尽" in message]
    assert honesty, f"预算耗尽那一行必须落 log：{captured.messages}"
    assert "音频归档完整" in honesty[0]
    assert str(note["unfinished"]) in honesty[0]


def test_stop_counts_the_segment_still_in_the_workers_hands(session_dir: Path):
    """**真实预算**下也验得到「没做完」：worker 手上那一段必须算进 `unfinished`。

    这一条**没有注入任何小预算**：预算走的是真公式，队列那一刻是空的，
    算出来就是下限 `compute_drain_budget(0, 20.0) == 30.0`。
    「没做完」的那一段正是被 worker `get()` 走、还在模型里跑的那一段 ——
    只看 `qsize()` 的写法在这一幕里报 0，那句「如实记账」的 warning 一个字都不会落
    （受控破坏自证：把 `unfinished` 还原成 `self.segments.qsize()` → 这条红）。
    """
    model = StopInsideTheModel()
    pipeline = make_pipeline(session_dir, model)
    model.pipeline = pipeline
    pipeline.start("mic", str(session_dir))
    with capture_pipeline_log() as captured:
        pipeline._enqueue("mic", time.time(), speech_pcm(1.0))
        wait_for(lambda: model.stopped.is_set() or None, what="在飞的那一段里把 stop() 跑完")
    pipeline.stop()  # 幂等：真正的收尾已经在 worker 线程里做过了

    note = model.note
    assert note is not None, "stop() 该把记账字典交回来"
    assert note["budget"] == compute_drain_budget(0, 20.0), (
        f"这一条走真公式（没有注入小预算）：{note}"
    )
    assert note["segments_seen"] == 1 and note["segments_done"] == 0, (
        f"此刻那一段还在 worker 手上：{note}"
    )
    assert note["unfinished"] == 1, f"在飞的那一段必须算进「没做完」：{note}"
    assert "还有 1 段没做完" in note["note"], note["note"]
    honesty = [message for message in captured.messages if "收尾预算用尽" in message]
    assert honesty, f"预算耗尽那一行必须落 log：{captured.messages}"
    assert "音频归档完整" in honesty[0]
    # 收尾不静默丢内容：那一段照样落成一行（`_write_line` 会重开已经关掉的句柄）
    wait_for(
        lambda: line_texts(session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source="mic")) or None,
        what="在飞的那一段照样落成一行",
    )


def test_stop_is_idempotent_and_closes_the_transcript_files(session_dir: Path):
    """`stop()` 幂等；退出来之后两路产物文件都关掉了（还能再读到内容）。"""
    pipeline = make_pipeline(session_dir, FakeTranscriber())
    pipeline.start("both", str(session_dir))
    feed = SegmentFeed(pipeline, "mic")
    feed.speech("s" * 4)
    feed.cut()
    feed.flush()
    wait_for(
        lambda: line_texts(session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source="mic")) or None,
        what="第一行落盘",
    )
    first = pipeline.stop()
    second = pipeline.stop()
    assert second["unfinished"] == 0
    assert first["segments_done"] >= 1
    assert pipeline._handles == {}, "收尾必须把手上的产物句柄关干净"
    # 句柄关掉之后文件仍是可读的正常产物（追加模式 + 逐行 flush 的结果）
    assert line_texts(session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source="mic"))


def test_segmenter_caps_a_single_segment_at_the_configured_length(session_dir: Path, monkeypatch):
    """单段硬上限按 `asr.max_segment_seconds` 秒：超了立即出段再续，**一段都不丢**。

    混着验两件事：`max_segment_seconds` 显式给定时以它为准（热读不去覆盖调用方的决定），
    以及收尾 `flush()` 把最后那截不满上限的尾巴也交出来 —— 尾巴丢掉就等于丢内容。
    """
    monkeypatch.setattr(
        "meeting.asr.pipeline.config_module.get",
        lambda key, default=None: 1 if key == "asr.max_segment_seconds" else config_default(key, default),
    )
    segmenter = VoiceActivitySegmenter(vad_backend=FakeVad(), max_segment_seconds=1)
    assert segmenter.cap_bytes == SAMPLE_RATE * SAMPLE_WIDTH, "显式给的段长不该被热读覆盖"
    segments: list[bytes] = []
    audio = speech_pcm(4.0)
    segmenter.feed(audio, segments.append, time.time())
    segmenter.flush(segments.append)
    assert not segmenter.has_pending
    assert len(segments) >= 4, f"4 秒语音按 1 秒上限切，至少四段：{len(segments)}"
    # 上限判在帧边界上，因此满段最多超出一帧（960 字节）
    for segment in segments:
        assert len(segment) <= SAMPLE_RATE * SAMPLE_WIDTH + FRAME_BYTES
    total = sum(len(segment) for segment in segments)
    # **一段都不许丢**：只允许少掉末尾那截不满一帧的零头（30ms 以下，识别也用不上）
    assert len(audio) - total < FRAME_BYTES, f"丢了整整一帧以上：{len(audio)} -> {total}"
    assert total == len(audio) - 320, "切段只该少掉末尾那个不满帧的零头"