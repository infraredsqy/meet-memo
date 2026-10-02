"""`tests/test_transcriber_backend.py` —— 转写后端的协议实现与**解耦证明**。

`9e5db89c` phase2 的验收在这里落成用例：

  - **协议**：`TranscriberBackend` 交出 `bind` / `start` / `stop` / `health` /
    `backlog_depth` / `segment_timeout`，并且**模块顶层不 import 重依赖**；
  - **解析**：`transcriber.backend = auto` 落到 `meeting.backends.transcribe:TranscriberBackend`
    （真实现、`has_implementation is True`），`dev` 按 key 分流到 `DevTranscriberBackend`；
  - **三态与两个计数**：`normal` / `behind` / `stopped` 与积压量、单段超时；
  - **解耦证明（本单元的核心验收）**：把模型换成一条会睡很久的、和一条会抛错的，
    断言 **采集归档的字节数照涨、文件还在写**、随后到来的行时间戳仍贴近采集时刻
    （没被挤成一堆）、服务仍在应答 `/api/heartbeat`。

**没有一条用例加载真模型、打开真声卡**：模型是注入的替身，VAD 是确定性的能量阈值替身；
起服务的用例走 `tests/_helpers.app_client()`（进程内 TestClient + 家目录隔离）。
"""

from __future__ import annotations

import logging
import re
import struct
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

import meeting.backends.transcribe as transcribe_module
from meeting.asr.pipeline import (
    BACKLOG_BEHIND_THRESHOLD,
    FRAME_BYTES,
    MIN_SEGMENT_SECONDS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    TRANSCRIPT_FILE_TEMPLATE,
    VoiceActivitySegmenter,
    validate_transcript_health,
)
from meeting.backends.base import SOURCE_IDS, TranscriptLine, TranscriberBackend as Protocol
from meeting.backends.dev import DevTranscriberBackend
from meeting.backends.registry import (
    DEV_TRANSCRIBER_PATH,
    resolve_transcriber_backend,
)
from meeting.backends.transcribe import (
    DEFAULT_SEGMENT_TIMEOUT_SECONDS,
    DRAIN_STOP_SLACK_SECONDS,
    TranscriberBackend,
)
from meeting.service.runtime import compute_drain_budget
from meeting.session import archive_name
from tests import _helpers as helpers

#: 慢档标记：`pytest.mark.slow` 的归位判据与实测数据见 `docs/service.md` 第 7 章。
pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# 常量与假件
# ---------------------------------------------------------------------------

#: 用例的显式等待上限（秒；单条用例合计 ≤5 秒的看门之内）
WAIT_TIMEOUT_SECONDS = 3.0

#: 用例注入的静默窗口（秒）——生产值是 `SILENCE_TIMEOUT_SECONDS = 2.0`（`meeting/asr/pipeline.py`）。
#: 判据本身没变（还是「离上一块语音多久了」），只是把**等它成立**的那段时间压短：
#: 六条用例各等一次 2.2 秒的话，整文件就顶破了「单个测试文件 ≤10 秒」那条线。
TEST_SILENCE_TIMEOUT_SECONDS = 0.3

#: 假 VAD 的判定阈值（一帧里最大样本绝对值超过它就当语音）
SPEECH_AMPLITUDE = 4000

#: 假模型的默认台词
FAKE_TEXT = "假模型认出来的一行。"

#: 产物行的落盘形状（`[HH:MM] 文本`）——与 `tests/test_asr_pipeline.py` 的 `STAMPED_LINE_RE` 同源
STAMPED_LINE_RE = re.compile(r"^\[\d{2}:\d{2}\] ")


def speech_pcm(seconds: float, amplitude: int = 12000) -> bytes:
    """`seconds` 秒的「语音」（常量非零样本，够假 VAD 判成语音、也过得了噪声预筛）。"""
    samples = int(SAMPLE_RATE * seconds)
    return struct.pack(f"<{samples}h", *([amplitude] * samples))


def silence_pcm(seconds: float) -> bytes:
    return b"\x00\x00" * int(SAMPLE_RATE * seconds)


class FakeVad:
    """确定性的 VAD 后端（与 `test_asr_pipeline.py` 的同一手法）。"""

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        del sample_rate
        samples = struct.unpack(f"<{len(frame) // 2}h", frame)
        return max(abs(value) for value in samples) >= SPEECH_AMPLITUDE


class FakeModel:
    """假模型替身：每一次调用都按脚本给一条结果（用完脚本就给成功结果）。"""

    def __init__(self, script: list | None = None) -> None:
        self.script = list(script or [])
        self.calls = 0

    def transcribe_segment(self, audio_int16):
        from meeting.asr.glm import TranscribeResult

        self.calls += 1
        if self.script:
            item = self.script.pop(0)
            if callable(item):
                return item()
            return item
        return TranscribeResult(text=FAKE_TEXT, ok=True, reason="ok", tokens=7)


class HeldModel:
    """**被按住的模型**：进入时点亮 `entered`，一直等到 `release` 被设上才返回。"""

    def __init__(self, *, hold_seconds: float = 3.0) -> None:
        self.hold_seconds = float(hold_seconds)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.entries: list[float] = []
        self.returns: list[float] = []
        self.calls = 0

    def transcribe_segment(self, audio_int16):  # noqa: ARG002
        from meeting.asr.glm import TranscribeResult

        self.calls += 1
        self.entries.append(time.time())
        self.entered.set()
        self.release.wait(self.hold_seconds)
        self.returns.append(time.time())
        return TranscribeResult(text="按住之后才回来的一行。", ok=True, reason="ok")

    def held_seconds(self) -> float:
        """第一段被按住的实际时长（秒）——「模型真的慢过」这条判据用它。"""
        if not self.entries or not self.returns:
            return 0.0
        return max(0.0, self.returns[0] - self.entries[0])


class BrokenModel:
    """**会抛错的模型**：模型这一侧崩掉，流水线照旧要往下走。"""

    def __init__(self, message: str = "假模型按要求抛错") -> None:
        self.message = message
        self.calls = 0

    def transcribe_segment(self, audio_int16):  # noqa: ARG002
        self.calls += 1
        raise RuntimeError(self.message)


class StubTranscriberBackend:
    """三态与积压量都由用例摆布的**替身转写后端**（读数固定、想改就改）。

    用途只有一处：把「读快照这个动作会不会推着 `revision` / `state` 广播走」这件事
    从真流水线的调度里剥出来 —— 真后端读一次快照只能促成**一次**健康度迁移，
    替身这里则可以让「健康度一直 `normal`、只有积压量在变」这一幕反复出现。
    """

    def __init__(self, *, health: str = "normal", backlog: int = 0) -> None:
        self.health_value = health
        self.backlog_value = int(backlog)
        #: `health()` + `backlog_depth()` 被调用的次数（谁在刷快照这件事由它说话：
        #: 「读快照是纯读」的判据就落在「读几次都不该让这个数变」上）
        self.calls = 0

    def bind(self, line_sink, *, config=None) -> None:  # noqa: ARG002 - 替身不产出
        return None

    def start(self, mode: str, session_dir: str) -> None:  # noqa: ARG002
        return None

    def stop(self) -> None:
        return None

    def health(self) -> str:
        self.calls += 1
        return self.health_value

    def backlog_depth(self) -> int:
        self.calls += 1
        return self.backlog_value

    def segment_timeout(self) -> float:
        return DEFAULT_SEGMENT_TIMEOUT_SECONDS


def wait_for(predicate, *, timeout: float = WAIT_TIMEOUT_SECONDS, what: str = "条件"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.01)
    raise AssertionError(f"等待超时（{timeout}s）：{what}（最后观察值：{last!r}）")


def wait_until(predicate, *, timeout: float = WAIT_TIMEOUT_SECONDS) -> bool:
    """等一个条件成立；超时就返回 `False`（**不抛**）。

    与 `wait_for()` 的差别只有一个：这一条给「条件**不该**在某个窗口里成立」的判据用
    （成立与否由调用方自己断），因此超时不是错误、只是这一次的观察结果。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def make_backend(
    model,
    *,
    session_dir: Path | None = None,
    budget_fn=None,
    silence_timeout: float = TEST_SILENCE_TIMEOUT_SECONDS,
) -> TranscriberBackend:
    """造一个注入了假模型的转写后端（**不加载真模型**）。

    `silence_timeout` 注入的是 `VoiceActivitySegmenter` 自己的构造参数（分段判据本身照旧），
    用例因此不必为「等静默判据成立」各睡 2 秒。
    """
    del session_dir  # 场次目录由 `start()` 给
    return TranscriberBackend(
        transcriber_factory=lambda: model,
        vad_factory=lambda: VoiceActivitySegmenter(
            silence_timeout=silence_timeout, vad_backend=FakeVad()
        ),
        budget_fn=budget_fn,
        read_chunk_bytes=FRAME_BYTES,
    )


class FakePipeline:
    """替身流水线：只交出 `TranscriberBackend.stop()` 需要的那几个读数与方法。

    用途有两处，都是**把「等的上界就是那个数」钉成实测**：

      - `FakePipeline` 写死 5.0 秒的那一档（`stop()` 一直等到 `finish` 被设上或
        `drain_seconds` 到点），写死的上界于是必须等满 5 秒；而按 `drain_join_budget()`
        算出来的那个数（这里给 0.25 秒的显式上界）会先一步返回；
      - `reader_slack_seconds > 0` 那一档模拟**内层的最坏档**：真 `pipeline.stop()` 在
        worker join 之前要先逐路 join 读线程（每路最多 `READER_JOIN_TIMEOUT_SECONDS`），
        这一段是外层余量必须盖住的额外开销。`reader_slack_seconds` 单独记时，`drain_seconds`
        从「那一段跑完之后」才开始算 —— 于是「外层的上界有没有把读线程这一段算进去」
        可以单独验（复审 m5）。
    """

    def __init__(
        self,
        *,
        backlog: int = 0,
        drain_seconds: float = 5.0,
        reader_slack_seconds: float = 0.0,
    ) -> None:
        self._backlog = int(backlog)
        self.drain_seconds = float(drain_seconds)
        self.reader_slack_seconds = float(reader_slack_seconds)
        self.finish = threading.Event()
        self.stopped = threading.Event()
        self.notes: list[dict] = []
        self.entered = threading.Event()
        self.done = threading.Event()
        #: 收尾线程**真的进了** `stop()` 的那一刻（`join` 要是等满了它，这个时刻就晚于
        #: `stop()` 的返回时刻；`join` 上界生效的话它落在返回之后不久）
        self.entered_at: float | None = None
        #: 收尾线程**真的跑完**的那一刻（外层 `join` 有没有等满内层，拿它与返回时刻比）
        self.finished_at: float | None = None

    # -- `TranscriberBackend` 用到的那几格 ---------------------------------

    def backlog_depth(self) -> int:
        return self._backlog

    def worker_alive(self) -> bool:
        return True

    def readers_alive(self) -> bool:
        return True

    def close_transcripts_when_drained(self, *, timeout: float | None = None) -> bool:  # noqa: ARG002
        """替身没有产物句柄可关：如实报「收干净了」（`TranscriberBackend._drain()` 会调它）。"""
        return True

    # -- 收尾 -------------------------------------------------------------

    def stop(self) -> dict:
        self.stopped.set()
        self.entered_at = time.monotonic()  # **先**记时刻：join 有没有等满看它落在返回的前后
        self.entered.set()
        if self.reader_slack_seconds > 0:
            # 「逐路 join 读线程 + 送 VAD 尾段」那一段（内层在 worker join **之前**的开销）
            self.finish.wait(self.reader_slack_seconds)
        # 等到用例放行（或上界兜不住时的 `drain_seconds`）：模拟「在预算内尽量做完」
        self.finish.wait(self.drain_seconds)
        note = {
            "budget": 0.0,
            "unfinished": 0,
            "note": "替身流水线：收尾按剧本跑完",
            "segments_seen": 0,
            "segments_done": 0,
            "failures": {},
            "reader_errors": {},
        }
        self.notes.append(note)
        self.finished_at = time.monotonic()
        self.done.set()
        return note


def stub_backend(pipeline: FakePipeline, *, segment_timeout: float = 20.0) -> TranscriberBackend:
    """把替身流水线挂进一个真 `TranscriberBackend`（只换「流水线」那一环）。"""
    backend = TranscriberBackend()
    backend._pipeline = pipeline
    backend._segment_timeout = float(segment_timeout)
    return backend


class Recorder:
    """`line_sink` 替身：把收到的每一行按顺序记下来。"""

    def __init__(self) -> None:
        self.lines: list[object] = []

    def __call__(self, line) -> None:
        self.lines.append(line)

    def texts(self) -> list[str]:
        return [str(getattr(line, "text", "")) for line in self.lines]


class ArchiveWriter:
    """按采集侧的样子往归档里追加音频（`open_pcm()` 的 `buffering=0` 追加写）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "ab", buffering=0)

    def write(self, data: bytes) -> None:
        self._handle.write(data)

    def size(self) -> int:
        return self.path.stat().st_size

    def close(self) -> None:
        self._handle.close()


def write_until_segment(
    archive: ArchiveWriter,
    *,
    speech_seconds: float = 0.6,
    trailing_silence_seconds: float = TEST_SILENCE_TIMEOUT_SECONDS + 0.05,
) -> None:
    """写够「一段语音 + 静默」，让读线程 + VAD 真的把它切出来。

    **切段的这一步只能由墙钟促成**，因为 VAD 的判据是「喂进来的这一刻，离上一块语音多久了」
    （V1 `vad.py:57`：`now - self._last_speech > SILENCE_TIMEOUT`）。这条判据只在
    **收到新块**时才被评估一次，所以光写静默、或者在最后一块之后干等，都切不出段 ——
    必须在「最后一块语音」与「下一块静默」之间真的隔过那个静默窗口（V1 的实时流本来就是
    这个节奏，30ms 一帧连续喂；这里用几块大块模拟，块与块之间等够时间）。
    窗口的大小由 `make_backend(silence_timeout=…)` 注入（用例给 0.3 秒，生产是 2.0 秒）。
    """
    chunk = speech_pcm(0.3)
    for _ in range(max(1, round(speech_seconds / 0.3))):
        archive.write(chunk)
        time.sleep(0.02)
    # 让「离上一块语音」超过静默阈值（判据本身要求的等待，不是凑时序）
    time.sleep(max(0.0, trailing_silence_seconds))
    for _ in range(2):
        archive.write(silence_pcm(0.3))
        time.sleep(0.05)


#: 端到端用例自造源的块长（0.1 秒的 16k 单声道 int16 = 3200 字节，与真采集同速）
SCHEDULED_CHUNK_FRAMES = 1600
SCHEDULED_CHUNK_BYTES = SCHEDULED_CHUNK_FRAMES * SAMPLE_WIDTH

#: 端到端用例里「语音」那一档的样本常值（过得了 `FakeVad` 的阈值，也就过得了噪声预筛）
SCHEDULED_VOICE_AMPLITUDE = 12000


class ScheduledSource:
    """按剧本给块的采集源（`meeting.capture.engine.CaptureSource` 协议）。

    剧本是 `[("voice", 6), ("silence", 5), …]` 这样的 (种类, 块数) 序列：语音块给常量样本、
    静音块给全零，块与块之间按 `pace_seconds` 歇一口气（真采集一个块 0.1 秒的节奏）。
    喂完剧本之后这一路**继续活着但不再出块**（音频终点钉在确定的块边界上，判据因此不随
    「用例收尾时赶没赶上下一块」翻转）。

    用途在两条端到端用例上（`test_a_held_model_never_touches_the_real_capture_path` 与
    `test_a_raising_model_keeps_the_archive_and_the_service_alive`）：它们要一段**确定的**
    「语音 → 静默」，好让切段的那一刻可预测（`tests/_capture_faulty.py` 的假源是为电平
    对账设计的常量样本，真 `webrtcvad` 在它上面判定不稳）。采集引擎、写线程、无缓冲追加
    写盘全是生产代码。
    """

    def __init__(self, name: str, schedule: list, *, pace_seconds: float = 0.1) -> None:
        self.name = name
        self.pace_seconds = float(pace_seconds)
        self.fed = 0
        self.plan: list[bytes] = []
        for kind, count in schedule:
            payload = (
                struct.pack(f"<{SCHEDULED_CHUNK_FRAMES}h", *([SCHEDULED_VOICE_AMPLITUDE] * SCHEDULED_CHUNK_FRAMES))
                if kind == "voice"
                else b"\x00\x00" * SCHEDULED_CHUNK_FRAMES
            )
            self.plan.extend([payload] * int(count))
        self._stop = threading.Event()
        self._latest = b""

    def start(self) -> None:
        return None

    def read(self, timeout: float) -> bytes | None:
        if self._stop.is_set():
            return None
        if self.fed >= len(self.plan):
            time.sleep(min(timeout, 0.005))
            return b""
        data = self.plan[self.fed]
        self.fed += 1
        self._latest = data
        if self.pace_seconds > 0:
            time.sleep(self.pace_seconds)
        return data

    def latest_samples(self):
        import numpy as np

        return np.frombuffer(self._latest or b"\x00\x00", dtype=np.int16)

    def stop(self) -> None:
        self._stop.set()


class GatedSource(ScheduledSource):
    """剧本照给，但可以**落闸**（从此不再出块），也可以按块数**丢掉**接下来那几块。

    两者模拟的是同一件事：采集这一路被拖住、新音频进不来（落闸走「`read()` 给空」那条，
    丢块走「该给的那一块给不出」那条）。

    给 m6 那条端到端用例的**受控破坏**用：落闸 / 丢块之后，真采集引擎写进归档的字节不再
    随窗口增长 —— 于是「按住模型期间归档照涨」这条判据当场红。这一刀切在**产品采集路径
    的读侧**，用例自己一行字节都不写。
    """

    def __init__(self, name: str, schedule: list, *, pace_seconds: float = 0.1) -> None:
        super().__init__(name, schedule, pace_seconds=pace_seconds)
        self.gate = threading.Event()
        self.drop_from: int | None = None
        self.drop_count = 0

    def read(self, timeout: float) -> bytes | None:
        if self.gate.is_set():
            time.sleep(min(timeout, 0.005))
            return b""
        if self.drop_from is not None and self.fed >= self.drop_from:
            self.drop_count += 1
            time.sleep(min(timeout, 0.005))
            return b""
        return super().read(timeout)


def _start_capture_session(runtime, sessions: dict, sources: dict, mode, session_dir, *, frame_interval):
    """用真 `CaptureSession` 起这一场的采集（装配照 `tests/test_capture_e2e.py` 的 `live()` 那条路）。"""
    from meeting.capture.engine import CaptureSession

    capture = CaptureSession(
        mode=mode,
        session_dir=str(session_dir),
        sources=sources,
        emit=runtime.bind_capture(runtime.capture_backend),
        frame_interval=frame_interval,
    )
    sessions["current"] = capture
    capture.start()
    return capture


def transcript_lines(session_dir: Path, source: str) -> list[str]:
    path = session_dir / TRANSCRIPT_FILE_TEMPLATE.format(source=source)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [line for line in raw.splitlines() if line]


def stamp_of(line: str) -> str:
    """一行里那个 `HH:MM`（**分钟级** —— 这是产物自己的落盘精度）。"""
    assert STAMPED_LINE_RE.match(line), f"这一行的形状不对：{line!r}"
    return line[1:6]


def minute_of(wall_clock: float) -> str:
    """一个墙钟时刻对应的 `HH:MM`（与产物落盘用的格式化口径同一处）。"""
    return datetime.fromtimestamp(wall_clock).strftime("%H:%M")


@pytest.fixture()
def session_dir(tmp_path: Path) -> Path:
    path = tmp_path / "260922"
    path.mkdir()
    return path


# ---------------------------------------------------------------------------
# 协议与解析
# ---------------------------------------------------------------------------


def test_backend_implements_the_frozen_protocol():
    """协议成员的形状逐条在位（`runtime_isinstance` 判据用的是同一个 Protocol）。"""
    backend = TranscriberBackend()
    assert isinstance(backend, Protocol), "TranscriberBackend 必须满足 backends.base 的协议"
    for name in ("bind", "start", "stop", "health", "backlog_depth", "segment_timeout"):
        assert callable(getattr(backend, name)), name


def test_module_toplevel_imports_stay_light():
    """**模块顶层不 import 重依赖**（没装模型依赖的机器上这个模块仍要能 import 成功）。"""
    import ast
    import inspect

    for module in (transcribe_module, __import__("meeting.asr.pipeline", fromlist=["x"])):
        tree = ast.parse(inspect.getsource(module))
        names: set[str] = set()
        for node in tree.body:  # **只看模块顶层**：函数体内的惰性 import 不算
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        heavy = names & {"torch", "transformers", "numpy", "webrtcvad", "sounddevice", "soundcard"}
        assert not heavy, f"{module.__name__} 顶层 import 了重依赖：{heavy}"


def test_auto_resolves_to_the_real_transcriber_backend():
    """**完成判定 1**：`auto` 交出真实现（路径、在场标记、实例类型三样都对）。"""
    resolution = resolve_transcriber_backend()
    assert resolution.target == "meeting.backends.transcribe:TranscriberBackend"
    assert resolution.has_implementation is True
    assert isinstance(resolution.implementation, TranscriberBackend)
    assert resolution.skipped() is False


def test_dev_selector_splits_by_key():
    """`dev` 一档**按 key 分流**：转写落 `DevTranscriberBackend`，采集照旧落 `DevCaptureBackend`。"""
    from meeting.backends.registry import resolve_capture_backend

    transcriber = resolve_transcriber_backend("dev")
    assert transcriber.target == DEV_TRANSCRIBER_PATH
    assert isinstance(transcriber.implementation, DevTranscriberBackend)
    assert resolve_capture_backend("dev").target == "meeting.backends.dev:DevCaptureBackend"


def test_transcript_health_domain_is_its_own_three_values():
    """转写健康度走**转写域**的校验器：三取值，越界落 `stopped`（采集域那个校验器认的是
    探针七取值、越界落 `probe_failed`，两个域各归各的）。"""
    assert [validate_transcript_health(value) for value in ("normal", "behind", "stopped")] == [
        "normal", "behind", "stopped",
    ]
    # 采集域的七取值落到这里就是越界：如实落 stopped，绝不长出 probe_failed 这种第四个值
    assert validate_transcript_health("probe_failed") == "stopped"
    assert validate_transcript_health("capturing") == "stopped"
    assert validate_transcript_health(None) == "stopped"


def test_dev_transcriber_emits_synthetic_lines_and_stops_cleanly():
    """dev 转写替身：会中出合成行、两路轮换；`stop()` 之后**一行都不再产**。"""
    recorder = Recorder()
    backend = DevTranscriberBackend(interval_seconds=0.05, initial_delay_seconds=0.05)
    backend.bind(recorder)
    backend.start("both", "<dev-session>")
    try:
        wait_for(lambda: len(recorder.lines) >= 2 or None, what="dev 替身出两行")
        sources = [getattr(line, "source", "") for line in recorder.lines[:2]]
        assert sources == ["mic", "loopback"], f"两路轮换：{sources}"
        for line in recorder.lines:
            assert isinstance(line, TranscriptLine)
            assert line.kind == "line"
            assert line.text
        assert backend.health() == "normal"
    finally:
        backend.stop()

    count = len(recorder.lines)
    time.sleep(0.25)
    assert len(recorder.lines) == count, "stop() 之后不该再有新行"


# ---------------------------------------------------------------------------
# 三态与两个计数
# ---------------------------------------------------------------------------


def test_health_backlog_and_timeout_follow_the_pipeline(session_dir: Path, monkeypatch):
    """`normal` → `behind` → `stopped` 三态，以及积压量与单段超时的读数。"""
    from meeting import config as config_module

    real_get = config_module.get
    monkeypatch.setattr(
        transcribe_module.config_module,
        "get",
        lambda key, default=None: 7.5 if key == "asr.timeout_seconds" else real_get(key, default),
    )
    model = HeldModel(hold_seconds=3.0)
    recorder = Recorder()
    backend = make_backend(model)
    backend.bind(recorder)
    assert backend.health() == "stopped", "起之前如实报 stopped"

    backend.start("both", str(session_dir))
    archive = ArchiveWriter(session_dir / archive_name("mic", 1))
    try:
        assert backend.health() == "normal"
        assert backend.segment_timeout() == 7.5, "单段超时取配置里的 asr.timeout_seconds"
        assert backend.backlog_depth() == 0

        write_until_segment(archive)
        wait_for(lambda: model.entered.is_set() or None, what="假模型被调到（worker 被按住）")
        # worker 被按住，后面来的段只能堆在队列里
        for _ in range(BACKLOG_BEHIND_THRESHOLD + 2):
            backend._pipeline._enqueue("mic", time.time(), speech_pcm(1.0))
        assert backend.backlog_depth() > BACKLOG_BEHIND_THRESHOLD
        assert backend.health() == "behind", "积压压过门槛就是 behind"
    finally:
        model.release.set()
        backend.stop()

    assert backend.health() == "stopped", "停过之后如实报 stopped"


def test_health_reports_stopped_when_the_model_blows_up(session_dir: Path):
    """模型崩过 → `stopped`；而那一段照旧落成规定的占位符（**不静默丢**）。"""
    model = BrokenModel()
    backend = make_backend(model)
    recorder = Recorder()
    backend.bind(recorder)
    backend.start("mic", str(session_dir))
    archive = ArchiveWriter(session_dir / archive_name("mic", 1))
    try:
        write_until_segment(archive)
        wait_for(lambda: model.calls or None, what="假模型被调到（抛错那一次）")
        wait_for(lambda: transcript_lines(session_dir, "mic") or None, what="占位符落盘")
        assert backend.health() == "stopped"
    finally:
        backend.stop()


# ---------------------------------------------------------------------------
# 解耦证明（本单元的核心验收）
# ---------------------------------------------------------------------------




def test_transcription_model_runs_on_a_worker_thread(session_dir: Path):
    """结构性断言（`68927c27` 收尾轮）：模型在 worker 线程上被调，与主线程不同一条。

    原来的「解耦证明」（按住模型若干秒、看归档照涨）按机主的敏捷口径收走——
    「不在同一条线程」这个结构事实用一次调用就能钉死：模型被调到的线程 id ≠ 主线程。
    这条边界在，慢模型就拖不住写归档的那条线；哪天有人把转写改成同步塞进采集路径，这里当场红。
    """
    seen = {}

    class ThreadRecordingModel:
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.release = threading.Event()

        def transcribe_segment(self, audio_int16):  # noqa: ARG002
            from meeting.asr.glm import TranscribeResult

            seen["thread"] = threading.get_ident()
            self.entered.set()
            self.release.wait(1.0)
            return TranscribeResult(text="线程记录完毕。", ok=True, reason="ok")

    model = ThreadRecordingModel()
    backend = make_backend(model)
    backend.bind(Recorder())
    backend.start("mic", str(session_dir))
    try:
        archive = ArchiveWriter(session_dir / archive_name("mic", 1))
        write_until_segment(archive)
        wait_for(lambda: model.entered.is_set() or None, what="假模型被调到")
    finally:
        model.release.set()
        backend.stop()

    assert seen.get("thread") not in (None, threading.get_ident()), (
        "模型必须跑在与主线程不同的 worker 线程上"
    )
# ---------------------------------------------------------------------------
# 服务侧快照：积压量写进快照，却不给 `revision` 投票（契约第 3.3 节）
# ---------------------------------------------------------------------------

#: 这一条投给服务的那一行转写正文
ONE_TRANSCRIPT_LINE = "按住模型期间来的一行。"


def action(client, name: str, args: dict | None = None):
    """打一个动作信封（与界面同一条路径：`/api/actions` + 本地防线头）。"""
    body: dict = {"action": name}
    if name == "start" and (args or {}).get("dir") is None:
        # `start` 必须显式带目标工作区目录：没给时由测试层预建一个（产品没有默认落点）
        root = getattr(client, "place_root", None)
        assert root is not None, "打 start 却没法预建工作区：给 client.place_root 或显式传 dir"
        args = {"dir": helpers.allocate_start_dir(root), **(args or {})}
    if args is not None:
        body["args"] = args
    return client.post("/api/actions", json=body, headers={"X-Meeting-Service": "1"})


def stable_revision(client, *, tries: int = 20, gap: float = 0.05) -> int:
    """等到 `revision` 连着两次读数一样，再拿它当起点。

    采集侧起来那一小会自有它的迁移（与本条判据无关），先让它落定，
    后面那一次比较说的才是「积压量变化有没有推着 `revision` 走」。
    """
    previous = int(client.get("/api/state").json()["revision"])
    for _ in range(tries):
        time.sleep(gap)
        current = int(client.get("/api/state").json()["revision"])
        if current == previous:
            return current
        previous = current
    raise AssertionError("revision 一直在涨，量不到一个稳定的起点")


def settled_health(client, expected: str, *, timeout: float = 2.0) -> dict:
    """等 `transcript.health` 落到 `expected` **且**不再动，返回那一刻的快照。

    起因（复审 m3）：快照里 `transcript` 的初值取自**真后端**（空闲档是 `stopped`），
    而用例随后会把转写后端换成替身（报 `normal`）—— 那一次迁移由健康度节拍带去提交，
    最长要等 `TRANSCRIBER_HEALTH_INTERVAL_SECONDS`（0.5 秒）。判据说「这一刻之后没有
    任何迁移」之前，先把这一拍等掉。
    """
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        last = client.get("/api/state").json()
        if last["transcript"]["health"] == expected:
            first = int(last["revision"])
            time.sleep(0.05)
            again = client.get("/api/state").json()
            if again["transcript"]["health"] == expected and int(again["revision"]) == first:
                return again
        time.sleep(0.05)
    raise AssertionError(
        f"{timeout}s 内 transcript.health 没落到 {expected!r} 并稳住（最后观察值：{last!r}）"
    )


def state_with_backlog(client, backlog: int):
    """读 `/api/state`；`buffered_lines` 到位了就把整份快照交出来，否则给 `None`（供等待用）。"""
    state = client.get("/api/state").json()
    return state if int(state["transcript"]["buffered_lines"]) == backlog else None


def state_with_health(client, health: str):
    """读 `/api/state`；`transcript.health` 到位了就把整份快照交出来，否则给 `None`。"""
    state = client.get("/api/state").json()
    return state if state["transcript"]["health"] == health else None


def _no_transcript_line_allowed(line) -> None:  # noqa: ARG001
    """插在 `runtime._apply_transcript_line` 上的守卫：**有一行到达就当场报红**。

    「worker 停摆之后不经任何新行也能看到 `health == "stopped"`」这条判据里，
    「没有新行」是**判据本身的一部分**：让这个守卫替换掉那个方法，一行若真的来了，
    用例红在「判据失效」上，而不是悄悄把断言变成空转。
    """
    raise AssertionError(f"这条用例要求观察窗里一行都不来，实际来了一行：{line!r}")


def test_backlog_changes_reach_the_snapshot_without_touching_the_revision(session_dir: Path):
    """**积压量变化不参与状态迁移**：`revision` 原地不动，`buffered_lines` 照旧写进快照。

    契约第 3.3 节（`docs/contract.md:208`）：一行到来时服务侧只更新 `transcript.last_line_at`
    并推那一帧，**不参与状态迁移、不涨 `revision`**；`state` / `snapshot` 的帧级 `id` 与
    `data.revision` 是同一次提交的那个数，界面靠「`id` 变了就是状态变了」判断。积压量几乎
    每一行都在变，让它触发 `commit()` 等于给 `state` 通道加一份高频抖动，那句承诺当场失效
    （`phase2/task.md` §7：这两个计数只在 `/debug` 与 log 里看着用）。

    这一条**没有注入 stub 后端**：转写侧就是真实现（`TranscriberBackend` + 真流水线 +
    被按住的假模型），积压量取自段队列里真堆着的三段；
    行走的是 runtime 自己包好的出口（`backend._deliver` → `bind_transcriber` 的 sink），
    与生产路径同一条。读数取自 `/api/state`。
    """
    model = HeldModel(hold_seconds=5.0)
    backend = make_backend(model)
    home = Path(session_dir).parent / "home-revision"
    home.mkdir(parents=True, exist_ok=True)
    values = {
        "capture.backend": "dev",
        "web.dist_dir": str(helpers.missing_dist_dir(home)),
    }

    with helpers.app_client(home, values=values) as client:
        client.place_root = Path(session_dir).parent / "out"
        client.place_root.mkdir(parents=True, exist_ok=True)
        runtime = client.app.state.runtime
        # 换掉解析出来的转写后端（与 `test_a_raising_model_…` 同一手法：只换模型那一环）
        runtime.transcriber_backend = backend
        backend.bind(runtime.bind_transcriber(backend), config=runtime._config_view())
        assert action(client, "start", {"mode": "both"}).status_code == 200
        revision = stable_revision(client)

        pipeline = backend._pipeline
        pipeline._enqueue("mic", time.time(), speech_pcm(1.0))  # 这一段立刻被 worker 拿走
        wait_for(lambda: model.entered.is_set() or None, what="假模型把 worker 按住")
        before = client.get("/api/state").json()
        assert int(before["transcript"]["buffered_lines"]) == 0, before["transcript"]
        assert int(before["revision"]) == revision

        for _ in range(3):
            pipeline._enqueue("mic", time.time(), speech_pcm(1.0))
        assert backend.backlog_depth() == 3, "按住 worker 期间段只能堆在队列里"
        try:
            backend._deliver(
                TranscriptLine(
                    ts=time.time(), source="mic", text=ONE_TRANSCRIPT_LINE, kind="line"
                )
            )
            state = wait_for(
                lambda: state_with_backlog(client, 3), what="积压量写进快照（/api/state 读得到）"
            )
            assert int(state["revision"]) == revision, (
                "积压量从 0 变 3 是计数在动，transcript 通道不参与状态迁移："
                f"revision 不该从 {revision} 走到 {state['revision']}"
            )
            assert state["transcript"]["last_line_at"] is not None
        finally:
            # 先放行再收场：worker 还扣着一段，收尾预算的下限是 30 秒，不放行会白等
            model.release.set()
        assert action(client, "stop").status_code == 200

    assert backend.health() == "stopped"


def test_snapshot_reads_do_not_refresh_and_do_not_push_the_revision(session_dir: Path):
    """**读快照是纯读**：`GET /api/state` 既不自己刷新 `transcript`、
    也不涨 `revision` / 不广播 `state` 帧。

    为什么单独钉这一条：`transcript.health` 的刷新点除了「一行到来」与「一场开始」，
    还有一条低频节拍（worker 停摆那一档没有行可等，见
    `test_worker_stall_reaches_the_snapshot_without_any_new_line`）。
    刷新会经 `commit()` 提交（`revision +1` + 一次 `state` 广播），所以它**绝不能挂在读路径上**：
    让只读请求去改状态，一来把契约给界面的那句「`id` 变了就是状态变了」变成假命题，
    二来 `GET /api/heartbeat` 与随后的 `GET /api/state` 会读到两个不同的 `revision`
    （`tests/test_heartbeat.py` 与 `tests/test_static.py` 各有判据咬住这一档）。

    这一条用替身转写后端（**先起一场**、健康度由用例摆布）把这件事从真流水线的
    调度里剥出来，读的**是替身自己的调用计数**：
      - 读快照**不许碰转写后端**：读两遍 `/api/state`，`backend.calls` 一次都不该变
        （把刷新挂回读路径上，这条当场红：读一次就调一次 `health()` / `backlog_depth()`）；
      - `revision` 与订阅者队列里的 `state` 帧**一次都没动**（积压量变不是状态迁移，
        即使节拍把新积压量写进了快照，也不许提交）。

    受控破坏：把刷新挂回 `_snapshot_json()`（或把 `buffered_lines` 那一格的 `changed`
    置回 `True` 并让读路径也调一次）→ 这条用例红。
    """
    home = Path(session_dir).parent / "home-read-jitter"
    home.mkdir(parents=True, exist_ok=True)
    values = {
        "capture.backend": "dev",
        "web.dist_dir": str(helpers.missing_dist_dir(home)),
    }
    backend = StubTranscriberBackend(backlog=1)

    with helpers.app_client(home, values=values) as client:
        runtime = client.app.state.runtime
        runtime.transcriber_backend = backend
        subscriber = runtime.hub.register()
        try:
            # 起点就是 `stopped`：这一条**没有起场次**，而健康度节拍随 `start_session()` 起
            # （复审 m3：它在空闲态先提交一次 `stopped` 会多出一条时序依赖），因此空闲态这一格
            # 停在 `startup()` 那一次刷出来的真值上、不会被替身改写。下面要验的是「读快照不
            # 改状态」，与这一格停在哪一档无关。
            def state_frames() -> list[str]:
                drained = []
                while True:
                    item = subscriber.take()
                    if item is None:
                        return drained
                    if item[0] == "state":
                        drained.append(item[1])

            stable_revision(client)
            settled = settled_health(client, "stopped")
            assert state_frames() == [], "起点该是干净的：没有待发的 state 帧"
            revision = int(settled["revision"])
            seen_before = backend.calls
            for _ in range(3):
                backend.backlog_value += 1  # 读之前把积压量换掉：读路径若也刷，计数会跟着涨
                state = client.get("/api/state").json()
                assert state["transcript"]["health"] == "stopped", state["transcript"]
                assert int(state["revision"]) == revision, (
                    "健康度没迁移，读快照不该把 revision 从 "
                    f"{revision} 推走（现在是 {state['revision']}）"
                )
            assert backend.calls == seen_before, (
                "读快照一个字都不该问转写后端（读路径是纯读）："
                f"{seen_before} -> {backend.calls}"
            )
            assert state_frames() == [], (
                "读快照不该广播 `state` 帧（广播=有人提交了，而这一刻没有任何状态迁移）"
            )
        finally:
            runtime.hub.unregister(subscriber)

    # 健康度真的迁移时，`revision` 照旧要动（这一格是「迁移才提交」的另一半）
    with helpers.app_client(home, values=values) as client:
        client.place_root = Path(session_dir).parent / "out"
        client.place_root.mkdir(parents=True, exist_ok=True)
        runtime = client.app.state.runtime
        backend = StubTranscriberBackend(backlog=0, health="stopped")
        runtime.transcriber_backend = backend
        # 先起一场：健康度节拍随 `start_session()` 起（见 `_ensure_transcriber_health_ticker()`），
        # 「迁移 -> 提交」这条判据要有节拍在跑才有意义。起场次那一刻刷出来的是替身报的
        # `stopped`，起点因此就是 `stopped`。
        assert action(client, "start", {"mode": "both"}).status_code == 200
        revision = int(settled_health(client, "stopped")["revision"])
        backend.health_value = "normal"
        state = wait_for(
            lambda: state_with_health(client, "normal"),
            what="健康度迁移之后快照里读到 normal",
        )
        assert int(state["revision"]) > revision, (
            "`health` 的迁移是提交资格：revision 该动（这一格证明上面那条不是「什么都不提交」）"
        )
        assert action(client, "stop").status_code == 200


def _feed_vad_speech(archive: ArchiveWriter, seconds: float = 0.3) -> None:
    """往归档里写一小段语音（**不**等静默窗口）：让这一路 VAD 手上真的压着一段。

    与 `write_until_segment()` 的差别是它刻意**不**喂静默 —— 这一段因此留在 VAD 手上
    （`has_pending is True`），只有 `pipeline.stop()` 的那次 `vad.flush()` 才会把它送出。
    """
    archive.write(speech_pcm(seconds))
    time.sleep(0.05)


def test_stop_is_idempotent_and_joins_the_worker(session_dir: Path):
    """`stop()` 幂等；收尾之后**本流水线**的 worker 收队（`health()` 如实落 `stopped`）。"""
    model = FakeModel()
    backend = make_backend(model)
    backend.bind(Recorder())
    backend.start("both", str(session_dir))
    pipeline = backend._pipeline
    archive = ArchiveWriter(session_dir / archive_name("mic", 1))
    try:
        write_until_segment(archive)
        wait_for(lambda: model.calls or None, what="假模型被调到")
        assert pipeline.worker_alive() is True
    finally:
        archive.close()
        backend.stop()
        backend.stop()  # 幂等

    assert backend.health() == "stopped"
    # 收队的是**这一条流水线自己的** worker（不数全进程同名线程：别的用例可能留着自己的）
    wait_for(lambda: not pipeline.worker_alive() or None, what="本流水线的 worker 收队")
    assert not any(thread.is_alive() for thread in pipeline._reader_threads.values())


def test_transcript_handles_reopened_after_stop_are_closed_again(session_dir: Path, monkeypatch):
    """**`stop()` 返回之后才写出来的那一行，重开的句柄也要有人关**（复审 m4）。

    修的是这样一件事：worker 在预算耗完那一刻手上还扣着一段时，`pipeline.stop()` 末尾那次
    `_close_handles()` 关不掉**之后**才写的行 —— `_write_line()` 发现句柄已空就重开一个并
    记回 `self._handles`，而第二次 `stop()` 走的是幂等那一格（直接返回），于是那个句柄此后
    没人关：同一进程里连发生一次就漏一个 fd（测试进程、长驻服务里连开多场都会撞上）。
    复审的探针读数就是这一档：`stop()` 返回之后 `_handles = {'mic': <open>}`。

    **这一条为什么要注入一个很小的 `budget_fn`**：`stop()` 只有在「worker 手上那一段还没
    做完」时才返回得比 worker 早（内层 `worker.join(budget + 1.0)` 超时）。用真预算
    （下限 30 秒）跑这一档要等 30 秒，判据本身与预算的大小无关 —— 把预算压到 0.1 秒／段，
    同一档就在毫秒级重现（`budget = 段数 × 0.1`，仍然按积压量走，形状没变）。

    **三段判据**：

      1. `stop()` 返回时 worker 还在模型手里、收尾线程还在跑，而 `_close_handles()` 那一次
         关闭**已经发生**（内层那次收队走完了）—— 也就是后面写出来的行必然要重开句柄；
      2. `stop()` 返回之后 worker 才落那一行（`_write_line()` 重开句柄，实测计数 ≥1）；
      3. 收尾线程（含它最后那次关闭）跑完之后 `_handles` **空**、重开出来的那个句柄已关闭，
         而那一行照旧落在产物里（关闭不是靠吞掉尾巴换来的）。

    受控破坏：把 `_drain()` 里那次 `pipeline.close_transcripts_when_drained()` 去掉
    （= 修之前那一版）→ 第 ③ 段的 `_handles` 里留着那个重开的句柄 → 用例红。
    """
    model = HeldModel(hold_seconds=2.5)  # 只有 release 才放行；hold 只作兜底
    # 噪声预筛（`_is_noise_segment`，真 `webrtcvad`）在这一条的常量样本上判定不稳，
    # 命中就等于「不写行」—— 这条要的是**确定性**的「进模型 → 写行」，因此把预筛让开
    # （phase1 的预筛本身由 `tests/test_asr_glm.py` 与 `test_asr_pipeline.py` 覆盖）
    monkeypatch.setattr(
        transcribe_module.glm_module, "_is_noise_segment", lambda audio: False
    )
    # 预算压到 0.1 秒／段（形状照 `compute_drain_budget`：只把比例缩小）
    tiny_budget = lambda backlog, timeout: max(0.05, int(backlog) * 0.1)  # noqa: E731
    backend = make_backend(model, budget_fn=tiny_budget)
    backend.bind(Recorder())
    backend.start("mic", str(session_dir))
    pipeline = backend._pipeline
    archive = ArchiveWriter(session_dir / archive_name("mic", 1))  # 给写行那一步一个真目录
    reopen_count = 0
    reopened: list = []
    closes: list[float] = []
    drain_thread_box: dict[str, threading.Thread] = {}
    real_close = pipeline._close_handles
    real_write = pipeline._write_line

    def spy_close() -> None:
        """观察点：收尾路径上每一次关闭都记一笔（判据 ① 与 ③ 都靠它）。"""
        closes.append(time.monotonic())
        real_close()

    def spy_write(source: str, captured_at: float, text: str) -> None:
        """观察点：`_write_line()` 第一次看到句柄已空 = worker 在收尾之后重开了一个。"""
        nonlocal reopen_count
        if pipeline._handles.get(source) is None:
            reopen_count += 1
        real_write(source, captured_at, text)
        handle = pipeline._handles.get(source)
        if handle is not None:
            reopened.append(handle)

    # 把 `asr-stop` 那条收尾线程的**线程对象**抓下来：`stop()` 返回之前会把 `backend._thread`
    # 置空，只靠那个属性拿不到「收尾跑完了没有」。这里挂一个只认名字的 `Thread.start` 观察点。
    real_thread_start = threading.Thread.start

    def spy_thread_start(self) -> None:
        if self.name == "asr-stop":
            drain_thread_box.setdefault("thread", self)
        real_thread_start(self)

    monkeypatch.setattr(threading.Thread, "start", spy_thread_start)

    pipeline._close_handles = spy_close
    pipeline._write_line = spy_write
    try:
        pipeline._enqueue("mic", time.time(), speech_pcm(1.0))
        wait_for(lambda: model.entered.is_set() or None, what="假模型把 worker 按住")
        # 再压一段进队列：worker 放行之后会处理它，而那一刻 `_handles` 已经被
        # `pipeline.stop()` 末尾那次关闭清空 —— `_write_line()` 于是重开一个
        # （复审 m4 指认的那条路径；生产上正是「预算耗完时队列里还有段」那一档）
        pipeline._enqueue("mic", time.time(), speech_pcm(1.0))
        assert pipeline._worker_thread is not None and pipeline._worker_thread.is_alive(), (
            "这一刻 worker 还被模型扣着（判据的前提）"
        )
        backend.stop(drain_join_seconds=0.25)  # 显式小上界：`stop()` 一定先于 worker 返回
        # ① `stop()` 返回时：内层那次收队已经走完（关过一次）、worker 还在模型手里。
        #    内层收队里读线程那一段与归档落盘可能各要几十毫秒，等它一下（判据不是「多快」）
        wait_until(lambda: closes or None, timeout=1.0)
        assert pipeline._worker_thread.is_alive(), "这一刻 worker 该还被模型扣着"
        assert closes, "内层那次收队该已经关过一次"
    finally:
        model.release.set()
        drain = drain_thread_box.get("thread")
        if drain is not None:
            drain.join(timeout=WAIT_TIMEOUT_SECONDS + 1.0)
        # **等 worker 自己退出**再下断言：收尾线程只保证「它自己跑完了」，而 worker 手上
        # 那一段是放行之后才写完的（修之前那条路径正是「收尾线程早走一步、句柄没人关」）
        worker = pipeline._worker_thread
        if worker is not None:
            worker.join(timeout=WAIT_TIMEOUT_SECONDS + 1.0)

    drain = drain_thread_box.get("thread")
    assert drain is not None, "这一档里收尾线程本来就该起来（`asr-stop`）"
    assert not drain.is_alive(), "放行之后收尾线程自己跑得完"
    # ② 那一行是 `stop()` 返回之后才写的（重开句柄这条路径真的走到了）
    assert reopen_count >= 1, (
        "这一档里 worker 该在收尾关闭之后才写那一行（判据的前提）：重开计数 "
        f"{reopen_count}、产物句柄 {pipeline._handles!r}"
    )
    # ③ 收尾线程把那个重开的句柄关掉了（`_handles` 空 = 收队之后没有人在写；
    #    `reopened` 里那几个都 `closed=True` = 关闭不是靠「句柄还开着」蒙过去的）
    assert pipeline._handles == {}, (
        f"收尾之后重开的产物句柄必须有人关：还剩 {pipeline._handles!r}"
    )
    assert reopened, "这一档里 `_write_line()` 该真的开过句柄（判据的前提）"
    still_open = [handle for handle in reopened if not handle.closed]
    assert still_open == [], (
        f"重开出来的句柄该被关掉（`closed=True`），还开着 {len(still_open)} 个："
        f"{[handle.name for handle in still_open]}"
    )
    assert transcript_lines(session_dir, "mic"), (
        "那一行照旧要落在产物里（关闭不是靠吞掉尾巴换来的）"
    )
    archive.close()
    backend.stop()  # 幂等：第二次什么都不做（重开句柄那条路正是靠这一点才漏掉的）


def test_reader_threads_tail_both_archives(session_dir: Path):
    """两路归档各有一条读线程在尾读（`mode=both`）—— 音频从**盘上**来，不碰采集对象。

    两路**同时**写（各一条线程）：静默判据要等的那 2 秒因此只等一次，用例仍在 5 秒看门之内。
    """
    backend = make_backend(FakeModel())
    backend.bind(Recorder())
    backend.start("both", str(session_dir))
    archives = {
        source: ArchiveWriter(session_dir / archive_name(source, 1)) for source in SOURCE_IDS
    }
    errors: list[BaseException] = []

    def feed(source: str) -> None:
        try:
            write_until_segment(archives[source], speech_seconds=0.6)
        except BaseException as exc:  # noqa: BLE001 - 线程里的异常带回主线程再报
            errors.append(exc)

    try:
        writers = [threading.Thread(target=feed, args=(source,)) for source in SOURCE_IDS]
        for thread in writers:
            thread.start()
        for thread in writers:
            thread.join(timeout=WAIT_TIMEOUT_SECONDS + 2.0)
        assert not errors, errors
        for source in SOURCE_IDS:
            wait_for(
                lambda source=source: transcript_lines(session_dir, source) or None,
                what=f"{source} 这一路从归档里读出内容",
            )
            assert transcript_lines(session_dir, source), source
    finally:
        for archive in archives.values():
            archive.close()
        backend.stop()
