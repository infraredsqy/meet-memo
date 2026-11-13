"""`tests/test_transcriber_release.py` —— 会中模型**让出显存**的四条语义与收尾接线的顺序。

这一份盯的是收尾第 4 步 `post` 之前那一次交接：`TranscriberBackend.release_gpu()` 让会中
模型（GLM-ASR）退出显存，`meeting/service/runtime.py` 的 `_drain_post()` 在 `launch()` **之前**
调它 —— 会后离线链（MOSSTD）的加载紧接着 `launch()` 就开始，放在它之后等于没做。

四条语义各有独立断言（互不顺带覆盖）：

  ① **没加载过就 no-op**：不报错、不 import `torch`、不写日志、不碰 `empty_cache()`；
  ② **幂等**：连调两次只有第一次真的登记与清缓存，第二次安静地返回；
  ③ **有界等待**：在飞推理收干净时不等满上界；真卡住时到点就照旧丢引用 + 记一条 warning，
     不抛、不静默（上界由用例注入的小值显式钉住）；
  ④ **释放后才 `empty_cache()`**：清缓存的那一刻，模型引用与它内部那两格已经空了。

接线上另有三条：释放排在 `launch()` **之前**（**列表顺序断言**，不靠固定 `sleep` 赌时序）、
替身后端（没有这个方法）走防御式取法是 no-op、释放抛异常时 `post` 仍落 `done`。

**没有一条用例加载真模型、碰 GPU 或拉起会后链**：模型是注入的假件（`processor` /
`transcriber` 两格 + `transcribe_segment()`），`torch` 是一个记事的假模块。
"""

from __future__ import annotations

import contextlib
import logging
import re
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from meeting.asr.glm import TranscribeResult
from meeting.asr.pipeline import DRAIN_BUDGET_MIN_SECONDS
from meeting.backends.transcribe import (
    DRAIN_STOP_SLACK_SECONDS,
    RELEASE_GPU_WAIT_SECONDS,
    TranscriberBackend,
)

#: 用例的看门（秒）：单条用例的墙钟合计要留在「单个测试文件 ≤ 5 秒」那条线以内
CASE_LIMIT_SECONDS = 3.0

#: ③ 里注入的小上界（秒）：释放等在飞推理的窗口压到几十毫秒，用例因此量得出「到点就放手」
#: 而不用真等生产值（5 秒）。受控破坏成「无界等待」时这一条会一直挂到模型的放行事件
#: （`hold_seconds`）才回来，实测耗时当场越过下面那条上界。
TEST_WAIT_SECONDS = 0.05


class FakeTorch:
    """假 `torch` 模块：只记下 `cuda.empty_cache()` 被喊了几次、喊的那一刻现场长什么样。"""

    def __init__(self, *, on_empty_cache=None) -> None:
        self.calls = 0
        self.cuda = SimpleNamespace(empty_cache=self._empty_cache)
        self._on_empty_cache = on_empty_cache

    def _empty_cache(self) -> None:
        self.calls += 1
        if self._on_empty_cache is not None:
            self._on_empty_cache()


class FakeGlm:
    """假识别器：`GlmAsrTranscriber` 在释放眼里就是这个公开释放口 + 一个识别方法。"""

    def __init__(self, *, text: str = "假模型认出来的一行。") -> None:
        self.processor = object()
        self.transcriber = object()
        self.text = text
        self.calls = 0

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

    def transcribe_segment(self, audio_int16):  # noqa: ARG002
        self.calls += 1
        return TranscribeResult(text=self.text, ok=True, reason="ok", tokens=7)


class BlockingGlm(FakeGlm):
    """按住的识别器：进了 `transcribe_segment()` 就等放行（或等到 `hold_seconds` 到点）。"""

    def __init__(self, *, hold_seconds: float = 3.0) -> None:
        super().__init__()
        self.hold_seconds = float(hold_seconds)
        self.entered = threading.Event()
        self.release = threading.Event()
        #: 每一次收工的时刻（用例拿它与释放的返回时刻比先后）
        self.returns: list[float] = []

    def transcribe_segment(self, audio_int16):
        self.calls += 1
        self.entered.set()
        self.release.wait(self.hold_seconds)
        self.returns.append(time.monotonic())
        return TranscribeResult(text="按住之后才回来的一行。", ok=True, reason="ok")


def loaded_backend(model) -> TranscriberBackend:
    """一个**已经加载过模型**的后端（模型是注入的假件；不加载真模型、不碰 GPU）。"""
    backend = TranscriberBackend(transcriber_factory=lambda: model)
    assert backend._transcriber_or_create() is model
    return backend


#: 被测 logger 的名字
TRANSCRIBE_LOGGER_NAME = "meeting.backends.transcribe"


class CapturedLog(logging.Handler):
    """直接挂到被测 logger 上的记录器，用来看这一路真的写了什么。

    为什么不走 pytest 的 `caplog`：项目的"meeting"那一层 logger 是 `propagate = False`
    （`meeting/logging_setup.py` 第 58 行，为的是不污染 torch / transformers 的日志），
    记录冒不到根上，`caplog` 因此一条都收不到。这里自己挂 handler，判据直接读实测文本。
    """

    #: 落进记录里的 ANSI 颜色串（用例只关心文字）
    _ANSI = re.compile(r"\x1b\[[0-9;]*m")

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def text(self) -> str:
        return "\n".join(self._ANSI.sub("", record.getMessage()) for record in self.records)

    def messages(self) -> list[str]:
        return [self._ANSI.sub("", record.getMessage()) for record in self.records]

    def released(self) -> list[str]:
        return [text for text in self.messages() if "让出显存" in text]


@contextlib.contextmanager
def recording_log(logger_name: str = TRANSCRIBE_LOGGER_NAME):
    """在 `logger_name` 这一路上挂一个记录器（用完摘掉、级别与禁用状态照原样放回）。"""
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


def run_in_background(target) -> threading.Thread:
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# ① 没加载过就 no-op
# ---------------------------------------------------------------------------


def test_release_without_a_loaded_model_is_a_silent_noop(monkeypatch):
    """`meeting post` 那个独立入口的现场：进程里没加载过会中模型，释放安静地什么都不做。

    判据三条一起断：**没报错**、**没碰 `empty_cache()`**、**一条日志都没写**（释放这件事
    不留噪音）。`torch` 在这里是一个记事的假模块 —— 真去 import 真 `torch` 的话，一个
    「没加载过」的调用会白拉几百兆的重依赖进来。
    """
    fake_torch = FakeTorch()
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    backend = TranscriberBackend()

    with recording_log() as log:
        began = time.monotonic()
        result = backend.release_gpu()
        elapsed = time.monotonic() - began
        written = log.messages()

    assert result is False, "没有模型可丢时该如实报 False"
    assert fake_torch.calls == 0, "没加载过模型时不该去碰 empty_cache()"
    assert written == [], f"没加载过模型时释放该一条日志都不写：{written}"
    assert elapsed < CASE_LIMIT_SECONDS, f"no-op 该当场返回，实测 {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# ② 幂等
# ---------------------------------------------------------------------------


def test_release_is_idempotent(monkeypatch):
    """连调两次安全：只有第一次真的丢引用、真的清缓存，第二次安静地返回。

    `shutdown()` 之后那条路上还会再走一次释放（`stop()` 的幂等与它不是同一件事），
    所以第二格要的是一次安静的成功返回：既不重复 `empty_cache()`，也不重复登记。
    """
    fake_torch = FakeTorch()
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    model = FakeGlm()
    backend = loaded_backend(model)

    with recording_log() as log:
        first = backend.release_gpu()
        after_first = len(log.released())
        second = backend.release_gpu()
        recorded = log.released()

    assert first is True and second is False, "第二次调该走 no-op 那一格（返回 False）"
    assert after_first == 2, f"第一次释放该记「释放前 + 释放后」两条：{recorded}"
    assert len(recorded) == 2, f"第二次调不该再记一条：{recorded}"
    assert fake_torch.calls == 1, f"第二次调不该再清一次缓存：{fake_torch.calls}"
    assert backend._transcriber is None
    assert model.processor is None and model.transcriber is None


# ---------------------------------------------------------------------------
# ③ 有界等待：收干净了就不等满；卡住了到点就放手（丢引用 + 一条 warning）
# ---------------------------------------------------------------------------


def test_release_waits_for_inflight_inference_then_drops_the_reference(monkeypatch):
    """在飞推理**已经收干净**的那一档：释放当场就放手，不等满上界，也不打超时 warning。

    这是「等待」这件事的正面证据：等在飞计数上（`_transcribe_segment()` 收工即通知），
    不用固定 sleep 凑时序。
    """
    monkeypatch.setattr(
        "meeting.backends.transcribe.RELEASE_GPU_WAIT_SECONDS", TEST_WAIT_SECONDS, raising=True
    )
    fake_torch = FakeTorch()
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    model = FakeGlm()
    backend = loaded_backend(model)

    with recording_log() as log:
        began = time.monotonic()
        result = backend.release_gpu()
        elapsed = time.monotonic() - began
        written = log.messages()

    assert result is True
    assert backend._transcriber is None and model.processor is None
    assert not [text for text in written if "没等到" in text], written
    assert elapsed < CASE_LIMIT_SECONDS, f"没有在飞推理时释放该很快返回，实测 {elapsed:.3f}s"
    assert fake_torch.calls == 1


def test_release_gives_up_at_its_own_small_bound_and_still_drops_the_reference(monkeypatch):
    """在飞推理卡住的那一档：到点**照旧丢引用**并记一条 warning —— 不抛、不静默。

    **受控破坏**：把上界换成「一直等」（或自适应收尾预算那一档），这一条会在模型的放行事件
    （`hold_seconds`）之后才回来，实测耗时越过下面第一条上界，当场红。
    """
    monkeypatch.setattr(
        "meeting.backends.transcribe.RELEASE_GPU_WAIT_SECONDS", TEST_WAIT_SECONDS, raising=True
    )
    fake_torch = FakeTorch()
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    model = BlockingGlm(hold_seconds=3.0)
    backend = loaded_backend(model)

    worker = run_in_background(lambda: backend._transcribe_segment(_FAKE_AUDIO))
    assert model.entered.wait(CASE_LIMIT_SECONDS), "假模型该已经进到推理里"

    with recording_log() as log:
        began = time.monotonic()
        result = backend.release_gpu()
        elapsed = time.monotonic() - began
        written = log.messages()

    assert elapsed < CASE_LIMIT_SECONDS, (
        f"上界到点就该放手：注入的上界 {TEST_WAIT_SECONDS}s，实测等了 {elapsed:.3f}s"
    )
    assert result is True, "等不到也要照旧丢引用（如实登记，不静默）"
    assert backend._transcriber is None and model.processor is None
    warnings = [text for text in written if "没等到在飞推理收干净" in text]
    assert len(warnings) == 1, f"上界到点该记一条 warning：{written}"
    # 那一条要写清「等的是多久」（≈ 用例注入的上界；日志按一位小数写秒）
    waited_bound = float(re.search(r"在 ([\d.]+)s 内没等到", warnings[0]).group(1))
    assert 0.0 < waited_bound <= 0.5, (
        f"warning 里该写上界（注入的 {TEST_WAIT_SECONDS}s），实测写的是 {waited_bound}s：{warnings[0]}"
    )
    assert fake_torch.calls == 1, "超时那一档同样要清一次缓存"

    model.release.set()  # 放行那一段，用例收尾不留挂着的线程
    worker.join(timeout=CASE_LIMIT_SECONDS)
    assert not worker.is_alive()


#: 新加的那一条用多久（秒）：真的等一次在飞推理（**不靠固定 sleep 赌时序** ——
#: 放行是事件驱动的，这里只是给等待本身留出够用的窗口）
INFLIGHT_HOLD_SECONDS = 0.3

#: 新加的那一条注入的上界（秒）：明显大于上面那个窗口，于是「等到了」与「上界到点」两档
#: 在这一条里区分得开
INFLIGHT_WAIT_SECONDS = 2.0


def test_release_really_waits_until_the_inflight_segment_returns(monkeypatch):
    """**等待这件事真的在发生**：释放一直等到那一段推理收工才放手。

    与前面那条「到点就放手」成对：这一条要的是「不许不等」。现场是——在飞那一段被按住，
    释放随即开跑，**放行发生在释放返回之前**，于是返回那一刻的读数是「收干净」。
    判据取**两条实测时刻的先后**：那一段推理的收工时刻（`model.returns`）不晚于释放的返回
    时刻。把等待整个去掉（`RELEASE_GPU_WAIT_SECONDS` 置 0、或让 `_wait_for_inflight_inference()`
    直接返回 `True`）时，返回时刻会落在放行之前，这一条当场红。
    """
    monkeypatch.setattr(
        "meeting.backends.transcribe.RELEASE_GPU_WAIT_SECONDS", INFLIGHT_WAIT_SECONDS, raising=True
    )
    fake_torch = FakeTorch()
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    model = BlockingGlm(hold_seconds=INFLIGHT_HOLD_SECONDS * 4)
    backend = loaded_backend(model)

    worker = run_in_background(lambda: backend._transcribe_segment(_FAKE_AUDIO))
    assert model.entered.wait(CASE_LIMIT_SECONDS), "假模型该已经进到推理里"

    with recording_log() as log:
        # 放行是**事件**：等在飞那一段的收工时刻上，不靠固定 sleep 赌时序
        model.release.set()
        began = time.monotonic()
        assert backend.release_gpu() is True
        released_at = time.monotonic()
        written = log.messages()

    worker.join(timeout=CASE_LIMIT_SECONDS)
    assert not worker.is_alive()
    assert model.returns, "那一段推理该真的收工了（等待的意义就在这一刻）"
    assert model.returns[-1] <= released_at, (
        f"释放该等到那一段收工才返回：收工 {model.returns[-1]:.4f} / "
        f"返回 {released_at:.4f}（等了 {released_at - began:.4f}s）"
    )
    assert backend._transcriber is None and model.processor is None
    assert not [text for text in written if "没等到在飞推理收干净" in text], (
        f"这一档该是「等到了」，不该出现超时 warning：{written}"
    )
    assert fake_torch.calls == 1


# ---------------------------------------------------------------------------
# ④ 释放之后才 `empty_cache()`
# ---------------------------------------------------------------------------


def test_empty_cache_runs_only_after_the_references_are_gone(monkeypatch):
    """清缓存的那一刻现场：后端那一格与模型内部那两格**都已经空了**。

    这一条把「先丢引用、后清缓存」的顺序钉在**实测的现场读数**上（假 `torch` 在
    `empty_cache()` 被喊的那一刻把三格抄下来），而不是源码里的先后行号。
    """
    model = FakeGlm()
    backend = loaded_backend(model)
    snapshot: dict[str, object] = {}

    def capture() -> None:
        snapshot["backend_slot"] = backend._transcriber
        snapshot["processor"] = model.processor
        snapshot["transcriber"] = model.transcriber

    fake_torch = FakeTorch(on_empty_cache=capture)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    with recording_log() as log:
        assert backend.release_gpu() is True
        recorded = log.released()

    assert fake_torch.calls == 1, "有 torch 在场时该清一次缓存"
    assert snapshot == {"backend_slot": None, "processor": None, "transcriber": None}, (
        f"`empty_cache()` 该在引用丢掉**之后**才跑，那一刻的现场是：{snapshot}"
    )
    assert len(recorded) == 2, f"释放前后各一条 INFO：{recorded}"


def test_empty_cache_is_skipped_when_torch_is_absent(monkeypatch):
    """无卡 / 没装 `torch` 的机器：释放照常完成，只是不清缓存（`empty_cache()` 那一步跳过）。

    同一台机器上跑测试时 `torch` 可能不在 `sys.modules` 里，也可能被别的用例装了进去 ——
    这一条把它显式摘掉，让「没有 torch」这一档是**用例自己定的事实**。
    """
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    model = FakeGlm()
    backend = loaded_backend(model)

    with recording_log() as log:
        assert backend.release_gpu() is True
        recorded = log.released()

    assert backend._transcriber is None and model.processor is None
    assert "torch" not in sys.modules, "清缓存这一步不该顺手把 torch import 进来"
    completed = [text for text in recorded if "empty_cache()=跳过" in text]
    assert len(completed) == 1, f"这一档该如实写「跳过」：{recorded}"


def test_a_new_segment_after_the_release_goes_through_the_same_factory(monkeypatch):
    """释放之后下一段经**同一个 factory** 再造一次（丢掉引用就够，没有别的持有点）。

    这一条同时钉住两件事：释放真的把后端那一格清空了（不是只清模型内部两格），
    以及「再造一份」走的就是构造时注入的那个 factory（没有 `lru_cache`、没有模块级缓存）。
    """
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    created: list[FakeGlm] = []

    def factory() -> FakeGlm:
        model = FakeGlm(text=f"第 {len(created) + 1} 份模型")
        created.append(model)
        return model

    backend = TranscriberBackend(transcriber_factory=factory)
    first = backend._transcriber_or_create()
    assert created == [first]

    with recording_log():
        assert backend.release_gpu() is True
    assert backend._transcriber is None, "释放要把后端那一格清空"
    assert first.processor is None and first.transcriber is None

    second = backend._transcriber_or_create()
    assert len(created) == 2 and second is created[1] and second is not first
    assert second.transcriber is not None, "新造的这一份是可以用的"


class FakePipeline:
    """替身流水线：`health()` 那一趟要用的几个读数（释放不许把它们改掉）。"""

    behind_threshold = 20

    def worker_alive(self) -> bool:
        return True

    def readers_alive(self) -> bool:
        return True

    def backlog_depth(self) -> int:
        return 0


#: ③ 用的一段假音频（内容无关紧要：假模型不看它）
_FAKE_AUDIO = b"\x00\x00" * 1600


def test_release_touches_neither_stopped_nor_health(monkeypatch):
    """释放不碰 `_stopped`，也不动 `health()` 的读数（`post` 之外的登记名一个都不新增）。

    收尾期间转写健康度每 0.5 秒一拍：释放若顺手把 `_stopped` 置真，`ended` 阶段的那一拍会
    把 `transcript.health` 推成 `stopped` 并推一次 `revision` —— 那是一个可见行为变化，
    本单元不引入。判据取释放前后的**同一格读数**（`_stopped` 与 `health()` 各一格）。
    """
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    backend = loaded_backend(FakeGlm())
    backend._started = True
    backend._stopped = False
    backend._pipeline = FakePipeline()

    before = backend.health()

    with recording_log():
        assert backend.release_gpu() is True

    assert backend._stopped is False, "释放不该把 `_stopped` 置真"
    assert backend.health() == before, "释放不该动 health() 的读数"
    assert before == "normal", f"这一档该是正常的 `normal`，实测 {before!r}"


@pytest.mark.parametrize("module_name", ["meeting.backends.transcribe"])
def test_release_keeps_the_module_toplevel_light(module_name):
    """`release_gpu()` 的落地没有把 `torch` 搬到模块顶层（AST 判据与设计那条同源）。"""
    import ast
    import importlib
    import inspect

    module = importlib.import_module(module_name)
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    heavy = names & {"torch", "transformers", "numpy", "webrtcvad", "sounddevice", "soundcard"}
    assert not heavy, f"{module_name} 顶层 import 了重依赖：{heavy}"


def test_the_wait_bound_is_a_small_constant_not_the_adaptive_drain_budget():
    """上界是一个写死的小常量，且明显小于收尾预算的下限（35 秒那一档）。

    「用 `drain_join_budget()` 那类自适应预算去等」会顶穿第 4 步的 deadline：`post` 从
    `done` 翻 `failed`，`has_failed_step` 再把整场落 `failed`。
    """
    assert 0.0 < RELEASE_GPU_WAIT_SECONDS <= 5.0, (
        f"等待上界该是一个小值，实测 {RELEASE_GPU_WAIT_SECONDS}s"
    )
    assert RELEASE_GPU_WAIT_SECONDS < DRAIN_BUDGET_MIN_SECONDS + DRAIN_STOP_SLACK_SECONDS, (
        "释放等的上界必须明显小于收尾预算的下限那一档"
    )
