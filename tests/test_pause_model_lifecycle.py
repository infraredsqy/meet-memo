"""`tests/test_pause_model_lifecycle.py` —— 暂停当场结束会中模型，继续时再拉起来（第 21 条）。

`docs/design-intent.md` 第 21 条点名的四个时刻里，**暂停**是这一个节点管的那一格：按下暂停，
会中那个模型当场退出显存（不是「陪着整段休息」），按继续时再把它拉回来。两半分别落在：

  - `meeting/backends/transcribe.py` 的 `acquire_gpu()`（与既有的 `release_gpu()` 成对）；
  - `meeting/service/runtime.py` 的 `pause_session()` / `resume_session()`：经
    `_submit_model_lifecycle()` 把这两次调用**提交到后台的单线程执行器**，按钮当场应答
    （释放要等在飞的推理收干净、拉起之后紧接着是权重的加载，这两段工夫花在事件循环上会把
    心跳与 SSE 一起按住）。

四组读数：

  ① **后端那一半的语义**：没释放过就是安静 no-op（不重复造第二份识别器）、释放之后经
     **同一个 factory** 重造一份、造不出来只留一条 warning（继续这个动作照常）；
  ② **暂停窗那一截仍然一行都不产**：把「内容 → 暂停窗（数字零）→ 内容」喂进**生产的切段器**，
     中间插一次 `release_gpu()` / `acquire_gpu()`，看识别器手上会收到什么 —— 暂停窗那 1.2 秒
     连续零整截都进不去，继续之后的素材照常出段、照常经**新造的那一份**识别；
  ③ **接线**：经真的运行时动作（`pause_session()` / `resume_session()`）驱动，替身后端按顺序
     收到 release 与 acquire，幂等动作不重复交；真后端上那两个方法确实经这两条线被喊到；
  ④ **不把事件循环钉住**：替身把释放 / 拉起各自按住 3 秒，动作本身的墙钟仍是毫秒级。

**没有一条用例加载真模型、碰 GPU 或打开真实录音设备**：模型是注入的假件（`transcriber_factory`
造一份登记用的替身）、`torch` 是一个记事的假模块、采集走 `dev` 替身（`capture.backend = dev`），
切段器背后是确定性的振幅 VAD 替身。
"""

from __future__ import annotations

import contextlib
import logging
import struct
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from meeting import config as config_module
from meeting.asr.glm import TranscribeResult
from meeting.asr.pipeline import SAMPLE_RATE, VoiceActivitySegmenter
from meeting.backends.transcribe import TranscriberBackend

# ---------------------------------------------------------------------------
# 常量与假件
# ---------------------------------------------------------------------------

#: 用例的看门（秒）：单条用例的墙钟要留在「单个测试文件 ≤5 秒」那条线以内
CASE_LIMIT_SECONDS = 3.0

#: 动作本身该多快回来（秒）：替身把模型那两次调用各按住 3 秒，动作的墙钟仍在毫秒级
ACTION_ANSWER_LIMIT_SECONDS = 0.5

#: 替身按住一次模型调用多久（秒）：明显大于上面那条上界，「动作等了它」当场读得出来
HOLD_SECONDS = 3.0

#: 等一次后台调用落地的窗口（秒）
SETTLE_TIMEOUT_SECONDS = 5.0

#: 假采集的节奏：一块 0.1 秒（与 `tests/_capture_faulty.py` 的 `CHUNK_BYTES` 同值）
BLOCK_BYTES = 3200
BLOCK_SECONDS = 0.1

#: 假源的振幅：过 VAD 替身的阈值（1000）就是语音
SPEECH_AMPLITUDE = 2000

#: 切段器的静默出段时长（秒）：生产值是 2.0，这里压到 0.3 秒，暂停窗的读数几毫秒内验得出来
SEGMENTER_SILENCE_SECONDS = 0.3

#: 交到识别器手上的音频里，最长的一截静音允许有多长（秒）：VAD 自己的收尾窗口
#: （`SEGMENTER_SILENCE_SECONDS`）+ 喂进去那一拍的量化（一块 0.1 秒）+ 一帧（30ms）。
#: 暂停窗在盘上是 1.2 秒的连续零，明显长过这条上界 —— 于是「整截暂停进了识别器」当场红
RECOGNIZER_SILENCE_BOUND_SECONDS = SEGMENTER_SILENCE_SECONDS + BLOCK_SECONDS + 0.05

#: 暂停窗写成多少块数字静音（12 块 = 1.2 秒）
PAUSE_WINDOW_BLOCKS = 12

#: 两个 logger 的名字（用例直接挂 handler 读实测文本）
TRANSCRIBE_LOGGER_NAME = "meeting.backends.transcribe"
RUNTIME_LOGGER_NAME = "meeting.service.runtime"


class FakeTorch:
    """假 `torch` 模块：只记 `cuda.empty_cache()` 被喊了几次（用例不碰真显卡）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.cuda = SimpleNamespace(empty_cache=self._empty_cache)

    def _empty_cache(self) -> None:
        self.calls += 1


class FakeGlm:
    """假识别器：`TranscriberBackend` 眼里就是这个公开释放口 + 一个识别方法。"""

    def __init__(self, *, text: str = "假模型认出来的一行。") -> None:
        self.processor = object()
        self.transcriber = object()
        self.text = text
        self.calls = 0
        self.audio: list[bytes] = []

    def has_model_references(self) -> bool:
        return self.processor is not None or self.transcriber is not None

    def release_model_references(self) -> bool:
        """与真模型同一份语义：丢自己那两格，交回是否真丢掉了。"""
        dropped = False
        if self.processor is not None:
            self.processor = None
            dropped = True
        if self.transcriber is not None:
            self.transcriber = None
            dropped = True
        return dropped

    def transcribe_segment(self, audio_int16):
        self.calls += 1
        self.audio.append(bytes(audio_int16))
        return TranscribeResult(text=self.text, ok=True, reason="ok", tokens=7)


class AmplitudeVad:
    """确定性的 VAD 替身：一帧里最大样本绝对值过阈值就当语音（`webrtcvad.Vad` 同形接口）。"""

    def __init__(self, threshold: int = 1000) -> None:
        self.threshold = int(threshold)

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        del sample_rate
        samples = struct.unpack(f"<{len(frame) // 2}h", frame)
        return max(abs(value) for value in samples) >= self.threshold


class CapturedLog(logging.Handler):
    """直接挂到被测 logger 上的记录器（`meeting` 那一层 `propagate = False`，`caplog` 收不到）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def text(self) -> str:
        return "\n".join(record.getMessage() for record in self.records)


@contextlib.contextmanager
def recording_log(logger_name: str):
    """在 `logger_name` 这一路上挂一个记录器（用完摘掉、级别照原样放回）。"""
    logger = logging.getLogger(logger_name)
    recorder = CapturedLog()
    previous_level = logger.level
    logger.addHandler(recorder)
    logger.setLevel(logging.DEBUG)
    logging.disable(logging.NOTSET)
    try:
        yield recorder
    finally:
        logger.removeHandler(recorder)
        logger.setLevel(previous_level)


def loaded_backend(factory) -> TranscriberBackend:
    """一个**已经加载过模型**的后端（模型是注入的假件；不加载真模型、不碰 GPU）。"""
    backend = TranscriberBackend(transcriber_factory=factory)
    assert backend._transcriber_or_create() is not None
    return backend


def _speech_block() -> bytes:
    return struct.pack(f"<{BLOCK_BYTES // 2}h", *([SPEECH_AMPLITUDE] * (BLOCK_BYTES // 2)))


def _silent_block() -> bytes:
    return bytes(BLOCK_BYTES)


def _longest_zero_run(pcm: bytes) -> float:
    """一段 PCM 里最长的那一截数字静音有多长（秒）。"""
    best = run = 0
    for index in range(len(pcm) // 2):
        if pcm[index * 2 : index * 2 + 2] == b"\x00\x00":
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best / SAMPLE_RATE


class SegmentFeed:
    """**生产的切段器** + 确定性 VAD 替身 + 合成时钟（判据是「离上一块语音多久了」）。

    转写侧尾读的就是归档那份音频；切段器只把 VAD 判成语音的那些帧攒成一段交给识别器 ——
    一段都没切出来，那一截就没有转写行。用例经它读「识别器手上会收到什么」。
    """

    def __init__(self) -> None:
        self.segmenter = VoiceActivitySegmenter(
            silence_timeout=SEGMENTER_SILENCE_SECONDS,
            vad_backend=AmplitudeVad(),
        )
        self.clock = 0.0
        self.handed: list[bytes] = []

    def _feed(self, block: bytes) -> None:
        self.segmenter.feed(block, self.handed.append, self.clock)
        self.clock += BLOCK_SECONDS

    def speech(self, blocks: int) -> None:
        for _ in range(blocks):
            self._feed(_speech_block())

    def silence(self, blocks: int) -> None:
        for _ in range(blocks):
            self._feed(_silent_block())


# ---------------------------------------------------------------------------
# ① 后端那一半：acquire_gpu() 的四条语义
# ---------------------------------------------------------------------------


def test_acquire_without_a_release_is_a_silent_noop(monkeypatch):
    """**已经在手就什么都不做**：不重复造识别器、不写日志、不碰缓存。

    判据取三格实测读数：工厂只被调过一次、返回 `False`、这一路上一条日志都没有
    —— 反复按继续不该造出第二份权重（每份约 4.3 GiB）。
    """
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    created: list[FakeGlm] = []

    def factory() -> FakeGlm:
        model = FakeGlm(text=f"第 {len(created) + 1} 份模型")
        created.append(model)
        return model

    backend = loaded_backend(factory)
    with recording_log(TRANSCRIBE_LOGGER_NAME) as log:
        result = backend.acquire_gpu()
        written = log.text()

    assert result is False, "已经在手时该如实报 False（那一次真的什么都没做）"
    assert len(created) == 1, f"不该再造第二份识别器：{len(created)}"
    assert written == "", f"已经在手时该一条日志都不写：{written!r}"


def test_acquire_after_a_release_rebuilds_the_recognizer_via_the_same_factory(monkeypatch):
    """释放之后**经同一个 factory 重造一份**，并且下一轮释放照样管得着它。

    这一条是「继续 → 重新拉起来」的正面证据：造出来的东西与「释放之后下一段要转写时」
    同源同形（同一个 factory、同一把锁），第二轮释放照样把新那一份的内部两格清空。
    """
    monkeypatch.setitem(sys.modules, "torch", FakeTorch())
    created: list[FakeGlm] = []

    def factory() -> FakeGlm:
        model = FakeGlm(text=f"第 {len(created) + 1} 份模型")
        created.append(model)
        return model

    backend = loaded_backend(factory)
    first = created[0]
    assert backend.release_gpu() is True
    assert backend._transcriber is None and first.processor is None

    with recording_log(TRANSCRIBE_LOGGER_NAME) as log:
        assert backend.acquire_gpu() is True
        written = log.text()

    assert len(created) == 2, f"继续该重造一份识别器：{len(created)}"
    second = created[1]
    assert backend._transcriber is second, "后端那一格该握着新造的那一份"
    assert second is not first
    assert backend._gpu_released is False, "新造的这一份复位了「已经释放过」，下一轮释放照样管得着"
    assert "会中模型已重新拉起" in written, f"重拉该留一条 log：{written!r}"

    # 下一轮（例如按了暂停又按结束录音）释放照旧管得着这一份新的
    assert backend.release_gpu() is True
    assert backend._transcriber is None
    assert second.processor is None and second.transcriber is None


def test_a_factory_that_cannot_come_back_leaves_the_resume_working(monkeypatch):
    """**拉不起来不抛**：如实记一条 warning，返回 `False`，继续这个动作照常。

    真机上的现场是依赖没装 / 权重不在本地缓存 / 显存不够：那几档都不许把「继续」这个按钮
    拖失败（R1 的立场），下一次真出段时照旧经同一条路再试一遍。
    """
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    created: list[FakeGlm] = []

    def factory() -> FakeGlm:
        if created:
            raise RuntimeError("假替身按要求：第二份拉不起来")
        model = FakeGlm()
        created.append(model)
        return model

    backend = loaded_backend(factory)
    assert backend.release_gpu() is True

    with recording_log(TRANSCRIBE_LOGGER_NAME) as log:
        result = backend.acquire_gpu()
        written = log.text()

    assert result is False, "拉不起来时该如实报 False"
    assert backend._transcriber is None, "没拉起来就照旧空着（下一次出段时再试）"
    assert "没能重新拉起" in written, f"该留一条 warning：{written!r}"


def test_acquire_touches_neither_the_running_pipeline_nor_health(monkeypatch):
    """释放 → 拉起这一轮里，**流水线一个字不动**（它在跑、健康度照旧）。

    「暂停期间不产新的转写行」靠的正是这件事：读线程与 worker 都没被停过，尾读的游标因此
    不会回退、暂停窗那一段也不会被重新喂一遍。释放 / 拉起只动「模型在不在手上」那一格。
    """
    monkeypatch.delitem(sys.modules, "torch", raising=False)

    class FakePipeline:
        behind_threshold = 20

        def worker_alive(self) -> bool:
            return True

        def readers_alive(self) -> bool:
            return True

        def backlog_depth(self) -> int:
            return 0

    backend = loaded_backend(FakeGlm)
    pipeline = FakePipeline()
    backend._started = True
    backend._stopped = False
    backend._pipeline = pipeline

    before = backend.health()
    assert before == "normal", f"这一档该是正常的 `normal`，实测 {before!r}"

    assert backend.release_gpu() is True
    assert backend.acquire_gpu() is True

    assert backend._pipeline is pipeline, "流水线对象一个字没换"
    assert backend._stopped is False, "释放与拉起都不该把 `_stopped` 置真"
    assert backend.health() == before, "健康度读数不该因为这一轮而变"


# ---------------------------------------------------------------------------
# ② 暂停窗那一截：一行都不产（跨过一次释放与一次重拉）
# ---------------------------------------------------------------------------


def test_a_pause_window_still_reaches_no_recognizer_across_the_release_and_reacquire(
    monkeypatch,
):
    """暂停窗（整段的数字零）**一段都不交给识别器** —— 释放与重拉这一轮不破坏它。

    现场是这一场会的真实形状：**内容 → 暂停 → 继续 → 内容**。暂停那一刻模型退出显存，
    暂停窗那一截（1.2 秒连续零）谁都没收到，继续之后新造的那一份接着转。
    判据四格：段数、最长零段、识别器的调用次数、以及「暂停窗喂完手上还是一条段都没有」。
    """
    monkeypatch.setitem(sys.modules, "torch", FakeTorch())
    created: list[FakeGlm] = []

    def factory() -> FakeGlm:
        model = FakeGlm(text=f"第 {len(created) + 1} 份模型")
        created.append(model)
        return model

    backend = TranscriberBackend(transcriber_factory=factory)
    first = backend._transcriber_or_create()  # 本场已经在转写：模型在手
    feed = SegmentFeed()

    # ① 暂停之前录到的那一段内容：照常转写（VAD 的收尾窗口跟着暂停的头几块一起把它切出来）
    feed.speech(3)
    feed.silence(4)
    assert len(feed.handed) == 1, f"暂停之前那一段该被切出来：{len(feed.handed)}"
    backend._transcribe_segment(feed.handed[0])
    assert first.calls == 1

    # ② 按下暂停：会中模型当场退出显存
    assert backend.release_gpu() is True
    assert backend._transcriber is None
    handed_at_pause = len(feed.handed)

    # ③ 暂停窗那一截：整段的数字零 —— 一段都不许交到识别器手上（因此那一截没有转写行）
    feed.silence(PAUSE_WINDOW_BLOCKS)
    assert len(feed.handed) == handed_at_pause, (
        f"暂停窗那 {PAUSE_WINDOW_BLOCKS * BLOCK_SECONDS:.1f} 秒连续零不该切出任何一段："
        f"{len(feed.handed) - handed_at_pause} 段"
    )

    # ④ 按下继续：经同一个 factory 重造一份，继续之后的素材照常经它转写
    assert backend.acquire_gpu() is True
    assert len(created) == 2 and backend._transcriber is created[1]
    feed.speech(3)
    feed.silence(6)
    assert len(feed.handed) == 2, f"继续之后该照常切出段：{len(feed.handed)}"
    backend._transcribe_segment(feed.handed[1])
    assert created[1].calls == 1, "继续之后那一段该由**新造的那一份**识别"
    assert first.calls == 1, "旧的那一份不该再收到音频"

    # ⑤ 交到识别器手上的音频里，最长的一截静音只有 VAD 自己的收尾窗口那么大：
    #    暂停窗那 1.2 秒连续零整截都没进去
    for index, pcm in enumerate(feed.handed):
        longest = _longest_zero_run(pcm)
        assert longest <= RECOGNIZER_SILENCE_BOUND_SECONDS, (
            f"第 {index + 1} 段交到识别器手上的音频里带着 {longest:.2f} 秒连续静音"
            f"（暂停窗那 {PAUSE_WINDOW_BLOCKS * BLOCK_SECONDS:.1f} 秒零本不该整截进识别器）"
        )


# ---------------------------------------------------------------------------
# ③ 接线：经真的运行时动作（暂停 / 继续）驱动
# ---------------------------------------------------------------------------


class RecordingSpy:
    """替身转写后端：只把两次生命周期调用按顺序记进事件列表。"""

    def __init__(self) -> None:
        self.events: list[str] = []

    def release_gpu(self) -> bool:
        self.events.append("release")
        return True

    def acquire_gpu(self) -> bool:
        self.events.append("acquire")
        return True


@contextlib.contextmanager
def live_runtime(tmp_path: Path, monkeypatch, *, extra: dict | None = None):
    """装配一个**进程内**的运行时：`dev` 采集替身 + 一场真开着的会话（不起服务、不碰声卡）。

    形状照 `tests/test_post_hook.py` 的 `_post_wiring()`：解析后端 → 起一场 → 交给用例。
    收场时把模块级配置放回原样，并收掉用例自己那台单线程池。
    """
    from meeting.service.app import build_runtime

    home = tmp_path / "home-pause-model"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("MEETING_CAPTURE", raising=False)
    previous = config_module.current()
    applied = {
        **config_module.DEFAULT_VALUES,
        "log.dir": str(home / "logs"),
        "capture.backend": "dev",
        **(extra or {}),
    }
    runtime = build_runtime(
        config_module.Config(applied=applied, sources={key: "默认" for key in applied}, path="")
    )
    runtime._resolve_backends()
    workspace = tmp_path / "ws-pause-model"
    workspace.mkdir(exist_ok=True)
    runtime.start_session(dir=str(workspace))
    try:
        yield runtime
    finally:
        config_module.set_current(previous)
        runtime._shutdown_executor_slot("_model_executor", label="会中模型生命周期")


def _settle_model_calls(runtime) -> None:
    """等那条单线程链把**已经提交的**几次调用做完（FIFO 哨兵，不靠固定 sleep 赌时序）。"""
    executor = runtime._model_executor
    assert executor is not None, "暂停 / 继续该把调用提交到那台单线程池上"
    done = threading.Event()
    executor.submit(done.set)
    assert done.wait(SETTLE_TIMEOUT_SECONDS), "模型那两次调用没在窗口内做完"


def test_pausing_hands_the_model_back_and_resuming_pulls_it_up_again(tmp_path, monkeypatch):
    """**暂停 → 释放；继续 → 拉起**，按这个顺序各一次，幂等动作不重复交。

    事件列表排出的先后就是顺序证据（先 release 后 acquire）；再按一次暂停 / 一次继续
    （都在暂停 / 继续中的幂等档）之后列表一格不长 —— 不会把同一件事做第二遍。
    """
    with live_runtime(tmp_path, monkeypatch) as runtime:
        spy = RecordingSpy()
        runtime.transcriber_backend = spy

        runtime.pause_session()
        _settle_model_calls(runtime)
        assert spy.events == ["release"], f"暂停该让模型退出显存：{spy.events}"
        assert runtime.snapshot.session.paused is True

        # 幂等：已经在暂停中时再按一次，不重复释放
        runtime.pause_session()
        _settle_model_calls(runtime)
        assert spy.events == ["release"], f"第二次暂停不该再释放一遍：{spy.events}"

        runtime.resume_session()
        _settle_model_calls(runtime)
        assert spy.events == ["release", "acquire"], f"继续该把模型重新拉起来：{spy.events}"
        assert runtime.snapshot.session.paused is False

        # 幂等：没在暂停中时再按一次继续，不重复拉起
        runtime.resume_session()
        _settle_model_calls(runtime)
        assert spy.events == ["release", "acquire"], f"第二次继续不该再拉一遍：{spy.events}"


class HeldModelCalls:
    """按住的替身：两次调用各自「进了」就报信，然后等用例放行（读数是「动作没等它」）。"""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.release_entered = threading.Event()
        self.acquire_entered = threading.Event()
        self.release_gate = threading.Event()
        self.acquire_gate = threading.Event()

    def release_gpu(self) -> bool:
        self.events.append("release")
        self.release_entered.set()
        self.release_gate.wait(HOLD_SECONDS)
        return True

    def acquire_gpu(self) -> bool:
        self.events.append("acquire")
        self.acquire_entered.set()
        self.acquire_gate.wait(HOLD_SECONDS)
        return True


def test_the_pause_and_resume_actions_answer_without_waiting_for_the_model(tmp_path, monkeypatch):
    """替身把释放 / 拉起各按住 3 秒，两个动作的墙钟仍是毫秒级（**没把事件循环钉住**）。

    读数是两条实测时刻：动作返回的耗时，以及「这一刻那次调用确实还被按着」。
    把提交改成直接调用（在事件循环上等），这两条耗时会当场涨到替身按住的那个 3 秒 ——
    那几秒里 `/api/heartbeat`、`/api/state` 与三个 SSE 通道会一起停摆，界面的心跳探测
    因此判成「失联」（第 17 条那一条致命级）。
    """
    spy = HeldModelCalls()
    with live_runtime(tmp_path, monkeypatch) as runtime:
        runtime.transcriber_backend = spy

        began = time.monotonic()
        runtime.pause_session()
        paused_elapsed = time.monotonic() - began
        assert spy.release_entered.wait(SETTLE_TIMEOUT_SECONDS), "释放该已经在后台那条线程上开跑"
        assert not spy.release_gate.is_set(), "替身正按着这一次释放（这一刻它还没做完）"
        assert runtime.snapshot.session.paused is True
        assert paused_elapsed < ACTION_ANSWER_LIMIT_SECONDS, (
            f"暂停该当场应答：替身按着释放 {HOLD_SECONDS}s，实测这一下花了 {paused_elapsed:.3f}s"
        )

        spy.release_gate.set()  # 放行那一次释放，用例收尾不留挂着的调用
        _settle_model_calls(runtime)

        began = time.monotonic()
        runtime.resume_session()
        resumed_elapsed = time.monotonic() - began
        assert spy.acquire_entered.wait(SETTLE_TIMEOUT_SECONDS), "拉起该已经在后台那条线程上开跑"
        assert runtime.snapshot.session.paused is False
        assert resumed_elapsed < ACTION_ANSWER_LIMIT_SECONDS, (
            f"继续该当场应答：替身按着拉起 {HOLD_SECONDS}s，实测这一下花了 {resumed_elapsed:.3f}s"
        )
        spy.acquire_gate.set()
        _settle_model_calls(runtime)

    assert spy.events == ["release", "acquire"], spy.events


def test_the_product_transcriber_backend_really_takes_both_lanes(tmp_path, monkeypatch):
    """真后端上那两个方法**确实经这两条线被喊到**（`getattr` 里的名字与实现上的名字对得上）。

    替身后端那几条用例挡不住「名字写错一个字母」这一档：写错时防御式取法会安静跳过、
    而动作照常成功。这一条走产品解析出来的 `TranscriberBackend`：真后端此刻没有加载过
    会中模型，于是释放是 no-op 那一格、拉起把识别器造回来（构造不加载权重，不碰显卡）。
    """
    events: list[str] = []
    with live_runtime(tmp_path, monkeypatch) as runtime:
        assert isinstance(runtime.transcriber_backend, TranscriberBackend), (
            f"`transcriber.backend = auto` 该解析到这个真实现，实测 "
            f"{type(runtime.transcriber_backend).__name__}"
        )
        real_release = TranscriberBackend.release_gpu
        real_acquire = TranscriberBackend.acquire_gpu

        def recording_release(self):
            events.append("release")
            return real_release(self)

        def recording_acquire(self):
            events.append("acquire")
            return real_acquire(self)

        monkeypatch.setattr(TranscriberBackend, "release_gpu", recording_release)
        monkeypatch.setattr(TranscriberBackend, "acquire_gpu", recording_acquire)

        runtime.pause_session()
        _settle_model_calls(runtime)
        runtime.resume_session()
        _settle_model_calls(runtime)

    assert events == ["release", "acquire"], f"真后端上这两条线该各被喊到一次：{events}"


def test_a_backend_without_the_lifecycle_members_pauses_and_resumes_as_usual(tmp_path, monkeypatch):
    """替身后端（生产那一份 `null` 档）没有这两个成员：暂停与继续照常，且**一个线程都不起**。

    协议里没有这两个方法（`docs/backend-protocols.md` 把它们记成可选成员），
    防御式取法因此是必须的：取不到就跳过，动作一个字都不许失败。
    """
    from meeting.backends.null import NullTranscriberBackend

    with live_runtime(tmp_path, monkeypatch, extra={"transcriber.backend": "null"}) as runtime:
        assert isinstance(runtime.transcriber_backend, NullTranscriberBackend)
        runtime.pause_session()
        assert runtime.snapshot.session.paused is True
        runtime.resume_session()
        assert runtime.snapshot.session.paused is False
        assert runtime._model_executor is None, "那一档没什么可做的：这条链惰性创建、一个线程都没起"


class ExplodingModelCalls:
    """两次调用都抛错的替身：动作照常成功，事故落到 log 上。"""

    def release_gpu(self) -> bool:
        raise RuntimeError("假替身按要求在释放时抛错")

    def acquire_gpu(self) -> bool:
        raise RuntimeError("假替身按要求在拉起时抛错")


def test_a_raising_model_call_does_not_fail_the_button(tmp_path, monkeypatch):
    """模型那一侧出事**不许把暂停 / 继续这两个动作拖失败**：状态照常，事故如实进 log。

    与收尾第 4 步释放抛错那一档同姿态（如实登记、不静默、不把别的步骤带走）。
    """
    with live_runtime(tmp_path, monkeypatch) as runtime:
        runtime.transcriber_backend = ExplodingModelCalls()
        with recording_log(RUNTIME_LOGGER_NAME) as log:
            runtime.pause_session()
            _settle_model_calls(runtime)
            assert runtime.snapshot.session.paused is True, "释放抛错不该让暂停这个动作失败"
            runtime.resume_session()
            _settle_model_calls(runtime)
            assert runtime.snapshot.session.paused is False, "拉起抛错不该让继续这个动作失败"
            written = log.text()

    assert "会中模型让出显存（暂停期间不占显卡）失败" in written, f"该如实记一条：{written!r}"
    assert "会中模型重新拉起（继续之后接着用）失败" in written, f"该如实记一条：{written!r}"
