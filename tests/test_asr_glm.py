"""`tests/test_asr_glm.py` —— `meeting/asr/glm.py` 与 `meeting/asr/guards.py` 的契约用例。

**全程注入假模型，一条都不加载真模型、不开真声卡**（`tests/conftest.py` 的 autouse 兜底也在
这一层之上）。用例覆盖的是「一串 int16 样本进去，一段文本或一个失败原因出来」这条契约：

  - **四层防护真的挂上了**：`generate` 收到的那份 kwargs 里，
    `no_repeat_ngram_size=8` / `logits_processor=[_LoopGuard]`（**普通 list**）/
    `stopping_criteria=StoppingCriteriaList([_DeadlineStop])` 三样逐条断言；
  - **降级记忆**：首次 `TypeError` → 置模块级开关 `False`、去掉该参数重跑一次；
    开关本来就是 `False` 时异常照抛（V1 `mnotes_core/recorder.py:542-561` 的语义）；
  - **噪声预筛**：命中即 `ok=True, reason="noise"`（「这一段落是静音，不写行」，
    不是失败），并且**省掉那一次生成**（假模型的 `generate` 一次都没被调）；
  - **超时与判环都不落部分文本**：两者都交回 `text=""` 加失败原因；
  - **文本级兜底**：拿 V1 那条真实坏行（`tests/v1/fixtures_real_loops.py` 的 `LOOP_LINE_283`，
    2515 字符的截断副本）当模型输出喂进去，断言出来的文本被砍到 <60 字符；
  - **参数一律从配置读出**：写一份 `config.json`、`config.load()` 之后断言 kwargs 里的
    `max_new_tokens` 与超时秒数跟着变（`docs/legacy-assets.md` 的 C9：要验就写 `config.json`，
    不 monkeypatch 配置函数）；
  - **重依赖不进 `sys.modules`**：`import meeting.asr.glm` 在**一个全新解释器**里跑，
    `torch` / `transformers` / `numpy` / `webrtcvad` 一个都不许出现
    （本进程里 `numpy` 早被 `tests/conftest.py` 的声卡兜底拉进来了，同进程断言等于没验）。

`_DeadlineStop` 与 `_LoopGuard` 的**超时那一半**在 V1 是零测试覆盖（整套搬过来的
`tests/v1/test_repeat_filter.py` 只覆盖判环那一半），补在这里。
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from meeting import config as config_module
from meeting.asr import glm
from meeting.asr.guards import _DeadlineStop, _LoopGuard
from tests import _helpers as helpers
from tests.v1.fixtures_real_loops import LOOP_LINE_283, NORMAL_LINES

#: 假 Processor 交回的 prompt token 个数（`n_new = outputs 长度 - 它`）
PROMPT_TOKENS = 5
#: 假模型的 EOS id（`TranscribeResult` 与两个钩子都按它收尾）
EOS_ID = 99
#: 一帧音频的样本数（30ms @ 16kHz，`_is_noise_segment` 的 `frame_len`）
FRAME_LEN = 480
#: 一段 1 秒的 16k 单声道音频
ONE_SECOND = 16000


# ---------------------------------------------------------------------------
# 假件（都只实现被测代码真正用到的那几样）
# ---------------------------------------------------------------------------


class _FakeTensor:
    """假张量：`shape` 与 `outputs[:, n:]` 这一种二维切片。"""

    def __init__(self, tokens: list[int]) -> None:
        self.tokens = list(tokens)

    @property
    def shape(self) -> tuple[int, int]:
        return (1, len(self.tokens))

    def __getitem__(self, index):
        rows, cols = index
        assert rows == slice(None), "被测代码只用 `outputs[:, n:]` 这一种形状"
        return _FakeTensor(self.tokens[cols])


class _FakeInputs(dict):
    """假 `BatchFeature`：`generate(**inputs, **kwargs)` 要能散开（dict 那一半），
    被测代码随后还要按属性取 `inputs.input_ids`（真 `BatchFeature` 两样都支持）。"""

    def __init__(self, n_prompt: int = PROMPT_TOKENS) -> None:
        tensor = _FakeTensor([0] * n_prompt)
        super().__init__(input_ids=tensor)
        self.input_ids = tensor
        self.moves: list[tuple] = []

    def to(self, device, dtype=None):
        self.moves.append((device, dtype))
        return self


class _FakeProcessor:
    """假 Processor：`apply_transcription_request()` + `batch_decode()`。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list = []
        self.decoded: list[list[int]] = []

    def apply_transcription_request(self, audio):
        self.requests.append(audio)
        return _FakeInputs()

    def batch_decode(self, tokens, skip_special_tokens: bool = False) -> list[str]:
        self.decoded.append(list(tokens.tokens))
        return [self.text]


class _FakeModel:
    """假模型：记下每一次 `generate` 收到的 kwargs，再由 `on_generate` 决定这一跑的行为。"""

    def __init__(self, *, new_tokens: int = 7, on_generate=None) -> None:
        self.device = "cpu"
        self.dtype = "float32"
        self.generation_config = SimpleNamespace(eos_token_id=EOS_ID)
        self.calls: list[dict] = []
        self._new_tokens = new_tokens
        self._on_generate = on_generate

    def generate(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self._on_generate is not None:
            self._on_generate(kwargs)
        return _FakeTensor([1] * (PROMPT_TOKENS + self._new_tokens))


class _Clock:
    """可控的单调时钟（替 `time.monotonic`）：`advance` 就是「这一跑烧掉的墙钟」。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# 夹具与装配
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _defaults_config():
    """本文件的用例跑在**代码默认值**上，不依赖同进程里别人留下的生效配置。

    跑完把原来的生效配置原样放回（同进程里 `dev_runtime` / `app_client` 这些夹具会
    `set_current()`，它们自己也会还原，这里只是不让本文件的读数受它们影响）。
    """
    previous = config_module.current()
    config_module.set_current(None)
    try:
        yield
    finally:
        config_module.set_current(previous)


def _wire(monkeypatch, *, text: str = "今天会上定了三件事", new_tokens: int = 7, on_generate=None):
    """把 `_load_models` 换成假加载器（**打在 `meeting.asr.glm` 这份模块对象上**）。

    返回 `(transcriber, processor, model, loaded)`；`loaded` 记下加载器被调了几次。
    """
    processor = _FakeProcessor(text)
    model = _FakeModel(new_tokens=new_tokens, on_generate=on_generate)
    loaded: list[int] = []

    def fake_load_models():
        loaded.append(len(loaded) + 1)
        return processor, model

    monkeypatch.setattr(glm, "_load_models", fake_load_models)
    return glm.GlmAsrTranscriber(), processor, model, loaded


def _silence() -> "object":
    import numpy as np

    return np.zeros(ONE_SECOND, dtype=np.int16)


def _loud_noise() -> "object":
    """一块 RMS 远大于 150 的宽带噪声：三条判据里「RMS < 150」这一条必然不成立。"""
    import numpy as np

    rng = np.random.default_rng(20260917)
    return (rng.standard_normal(ONE_SECOND) * 1000.0).astype(np.int16)


# ---------------------------------------------------------------------------
# 一、新解释器里的 import 链：重依赖一个都不许被拉进来
# ---------------------------------------------------------------------------

#: 在子进程里跑的一行：只 import 被测模块，再报出**重依赖**与接缝名。
_IMPORT_CHAIN_CODE = (
    "import sys, meeting.asr.glm as g; "
    "heavy = [m for m in ('torch', 'transformers', 'numpy', 'webrtcvad') if m in sys.modules]; "
    "print('heavy=' + ','.join(heavy)); "
    "print('seam=' + g.GlmAsrTranscriber.__name__)"
)


def test_import_chain_pulls_no_heavy_dependency():
    """`import meeting.asr.glm` 只该拉进标准库与包内模块（`phase1/task.md` 完成判定第 4 条）。

    **必须换一个全新解释器**：本进程里 `numpy` 早被 `tests/conftest.py` 的声卡兜底
    （`_no_real_audio_capture` 会 import sounddevice）拉进 `sys.modules` 了，
    同进程里断言「numpy 不在」只会验到夹具的导入次序。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(helpers.REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_CHAIN_CODE],
        cwd=str(helpers.REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    output = (result.stdout or "") + (result.stderr or "")
    assert result.returncode == 0, f"全新解释器里 import 失败：\n{output}"
    lines = (result.stdout or "").strip().splitlines()
    assert lines[0] == "heavy=", (
        f"`import meeting.asr.glm` 把重依赖拉进了 sys.modules：{lines[0]!r}\n{output}"
    )
    assert lines[1] == "seam=GlmAsrTranscriber", output


# ---------------------------------------------------------------------------
# 二、测试接缝本身：patch 必须打在被调用的那份模块对象上
# ---------------------------------------------------------------------------


def test_loader_seam_is_the_module_namespace_itself():
    """`_load_models` 是 `meeting.asr.glm` 的模块级名字，`_load_transcriber` 从**这份**字典里取它。

    这条盯的是 `docs/legacy-assets.md` 的 C9 / E9 那个坑：一旦有人在 `glm.py` 里写
    `from ... import _load_models`（把函数对象绑到另一个模块对象上），
    `monkeypatch.setattr(meeting.asr.glm, "_load_models", fake)` 就会变成**一边通过一边什么都没验**。
    """
    assert isinstance(glm.__dict__.get("_load_models"), type(lambda: None))
    assert glm.GlmAsrTranscriber._load_transcriber.__globals__ is glm.__dict__


# ---------------------------------------------------------------------------
# 三、成功路径：文本 / token 数 / 惰性加载只加载一次
# ---------------------------------------------------------------------------


def test_transcribe_segment_returns_text_and_token_count(monkeypatch):
    """顺风路径：拿到文本、`tokens` 是**新生成**的 token 数、耗时与失败位都对。"""
    transcriber, processor, model, loaded = _wire(monkeypatch, text="今天会上定了三件事",
                                                  new_tokens=7)

    result = transcriber.transcribe_segment(_loud_noise())

    assert result.ok is True and result.failed is False
    assert result.reason == "ok"
    assert result.text == "今天会上定了三件事"
    assert result.tokens == 7
    assert result.timed_out is False
    assert result.duration >= 0.0
    assert len(model.calls) == 1 and len(processor.requests) == 1
    assert processor.decoded == [[1] * 7], "解码的应当是 outputs[:, prompt_len:] 那一段"

    # 惰性加载只做一次：第二段不再调 `_load_models`
    transcriber.transcribe_segment(_loud_noise())
    assert loaded == [1], f"模型被加载了 {len(loaded)} 次"
    assert len(model.calls) == 2


def test_transcribe_result_failed_property():
    """`failed` 就是 `not ok`（占位符那一侧的判据）。"""
    assert glm.TranscribeResult().failed is False
    assert glm.TranscribeResult(text="", ok=False, reason="timeout").failed is True


# ---------------------------------------------------------------------------
# 四、四层防护真的挂上了（generate 收到的那份 kwargs）
# ---------------------------------------------------------------------------


def test_generate_kwargs_carry_the_four_layers(monkeypatch):
    """三样挂载物逐条断言：`no_repeat_ngram_size` / **普通 list** 的 `logits_processor` /
    `StoppingCriteriaList` 里的 `_DeadlineStop`。"""
    from transformers import StoppingCriteriaList

    transcriber, _processor, model, _loaded = _wire(monkeypatch)
    transcriber.transcribe_segment(_loud_noise())
    kwargs = model.calls[0]

    # 第 1 层：禁 8 连字（字符级 tokenizer）
    assert kwargs["no_repeat_ngram_size"] == 8

    # 第 2 层：`logits_processor` 用**普通 list** 装判环钩子。
    # transformers 5.x 会对 `LogitsProcessorList` 内省签名，普通 list 才是这里要的形状。
    assert type(kwargs["logits_processor"]) is list
    assert len(kwargs["logits_processor"]) == 1
    guard = kwargs["logits_processor"][0]
    assert isinstance(guard, _LoopGuard)
    assert guard.eos_token_id == EOS_ID
    assert guard._deadline is not None, "判环钩子必须同时带墙钟 deadline"

    # 第 3 层：`StoppingCriteriaList` 里的墙钟硬超时（与判环钩子用同一个 timeout_s）
    stopping = kwargs["stopping_criteria"]
    assert isinstance(stopping, StoppingCriteriaList)
    assert len(stopping) == 1
    assert isinstance(stopping[0], _DeadlineStop)
    # 两处超时钩子用**同一个** `timeout_s`（默认 20 秒，`phase1/task.md` 的形状第 3 条）
    assert stopping[0].seconds == 20.0
    assert guard._deadline - time.monotonic() == pytest.approx(20.0, abs=1.0)

    # 其余生成参数照 V1
    assert kwargs["do_sample"] is False
    assert kwargs["max_new_tokens"] == 400
    assert kwargs["pad_token_id"] == EOS_ID


def test_segment_parameters_come_from_config(monkeypatch, tmp_path):
    """参数一律从配置读出：写一份 `config.json`，kwargs 跟着变（C9 的口径：不 patch 配置函数）。"""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"asr.max_segment_seconds": 1, "asr.timeout_seconds": 5, "asr.max_new_tokens": 33}',
        encoding="utf-8",
    )
    loaded_config = config_module.load(environ={}, path=str(config_path))
    config_module.set_current(loaded_config)

    transcriber, _processor, model, _loaded = _wire(monkeypatch)
    transcriber.transcribe_segment(_loud_noise())
    kwargs = model.calls[0]

    assert kwargs["max_new_tokens"] == 33
    assert kwargs["stopping_criteria"][0].seconds == 5.0
    assert 0.0 < kwargs["logits_processor"][0]._deadline - time.monotonic() <= 5.0


# ---------------------------------------------------------------------------
# 五、噪声预筛（第 0 层：省掉一次生成）
# ---------------------------------------------------------------------------


def test_silence_segment_is_skipped_as_noise_and_never_generates(monkeypatch):
    """静音段：`ok=True, reason="noise"`（成功的「什么都没识别出来」），且**一次生成都不发**。"""
    transcriber, processor, model, loaded = _wire(monkeypatch)

    result = transcriber.transcribe_segment(_silence())

    assert (result.ok, result.reason, result.text, result.tokens) == (True, "noise", "", 0)
    assert result.failed is False, "噪声不是失败：上层不写占位符、也不写行"
    assert model.calls == [], "命中噪声就应当省掉那次生成"
    assert processor.requests == []
    assert loaded == [1], "噪声判定在加载之后（与 V1 的调用次序一致）"


def test_segment_shorter_than_one_frame_is_noise():
    """不足一帧（30ms @ 16kHz）一律判噪声（V1 `recorder.py:259-260`）。"""
    import numpy as np

    assert glm._is_noise_segment(np.zeros(FRAME_LEN - 1, dtype=np.int16)) is True
    assert glm._is_noise_segment(None) is True


def test_loud_noise_is_not_treated_as_silence(monkeypatch):
    """三条判据是 AND：RMS 远大于 150 的宽带噪声不该被当成静音（不误杀真实语音那一侧）。"""
    transcriber, _processor, model, _loaded = _wire(monkeypatch, text="有内容的转写")

    result = transcriber.transcribe_segment(_loud_noise())

    assert result.reason == "ok" and result.text == "有内容的转写"
    assert len(model.calls) == 1


# ---------------------------------------------------------------------------
# 六、超时与判环：都不落部分文本
# ---------------------------------------------------------------------------


def test_over_budget_duration_fails_the_segment(monkeypatch):
    """总耗时越过 `timeout_s + 1.0` 的兜底判据：交回 `timeout` 与空文本（不落半截垃圾）。

    第三处兜底判据是 `duration > timeout_s + 1.0`（V1 `recorder.py:577-583`），
    这里用可控时钟把这一跑「烧」掉 25 秒（默认超时 20 秒），一次 `sleep` 都不花。
    """
    clock = _Clock()
    monkeypatch.setattr(time, "monotonic", clock)

    def slow(kwargs):
        clock.advance(25.0)

    transcriber, _processor, model, _loaded = _wire(monkeypatch, text="半截垃圾文本",
                                                    new_tokens=9, on_generate=slow)

    result = transcriber.transcribe_segment(_loud_noise())

    assert (result.ok, result.reason, result.text) == (False, "timeout", "")
    assert result.timed_out is True
    assert result.tokens == 9
    assert result.duration == pytest.approx(25.0)


def test_loop_guard_hit_fails_the_segment_without_partial_text(monkeypatch):
    """判环钩子在 `generate` 里被驱动到 `detected`：整段判失败、不交回任何文本。"""
    seen: dict = {}

    def drive(kwargs):
        guard = kwargs["logits_processor"][0]
        ids = [0]
        for _ in range(120):
            ids = ids + [10, 11, 12, 13]
            guard(ids, None)
            if guard.detected:
                break
        seen["guard"] = guard

    transcriber, _processor, _model, _loaded = _wire(monkeypatch, text="写啊哈，你可以" * 200,
                                                     on_generate=drive)

    result = transcriber.transcribe_segment(_loud_noise())

    assert seen["guard"].detected is True, "假模型没能把判环钩子驱动到命中"
    assert (result.ok, result.reason, result.text) == (False, "loop", "")
    assert result.timed_out is False


def test_deadline_stop_expires_and_takes_extra_kwargs():
    """`_DeadlineStop`（V1 零测试覆盖）：到点返回 `True`，且是个接受多余 kwargs 的鸭子类型对象。"""
    fresh = _DeadlineStop(30.0)
    assert fresh(input_ids=None, scores=None) is False
    assert fresh.timed_out is False and fresh.calls == 1
    assert 0.0 < fresh.remaining <= 30.0

    expired = _DeadlineStop(-1.0)  # 负秒数 = 已经到点（不花任何 sleep）
    assert expired.remaining == 0.0
    assert expired(input_ids=None, scores=None, extra="StoppingCriteriaList 会多传东西") is True
    assert expired.timed_out is True and expired.calls == 1


def test_deadline_stop_rejects_none_seconds():
    """`float(seconds)` 不接受 `None`（`phase1/task.md` 的「必须保住的形状」第 2 条）。"""
    with pytest.raises(TypeError):
        _DeadlineStop(None)


def test_loop_guard_call_signature_is_exactly_three_names():
    """**硬形状**：`_LoopGuard.__call__` 的参数恰好是 `(self, input_ids, scores)`，一个都不多。

    为什么单独立一条：transformers 5.x 的 `LogitsProcessorList` 会**内省签名**
    （`logits_process.py:87-93`：参数个数 > 2 时，把第 3 个起的参数名当成必传 kwargs 去校验），
    多写一个 `**kwargs` 就会在 `generate` 内部抛
    `ValueError: Make sure that all the required parameters ... are passed`，
    **而堆栈指向 transformers**，很容易被误判成「这个模型不支持那个参数」
    （`docs/legacy-assets.md` 的 C8 / E8、`phase1/task.md` 的「必须保住的形状」第 1 条）。

    这条形状原先**没有任何用例盯着**（phase1 复审 minor-5：把签名改成
    `(self, input_ids, scores, **kwargs)` 之后 30 条用例全绿）—— 运行时零影响的那类改动
    会在真机 `generate` 里才炸，所以这里按参数**逐个点名**，`VAR_KEYWORD` 也一并禁掉：
    `inspect.signature` 对 `*args` / `**kwargs` 会报 `VAR_POSITIONAL` / `VAR_KEYWORD`，
    光比名字列表会漏掉它们。
    """
    parameters = list(inspect.signature(_LoopGuard.__call__).parameters.values())
    assert [item.name for item in parameters] == ["self", "input_ids", "scores"], (
        f"`_LoopGuard.__call__` 的参数必须恰好是 (self, input_ids, scores)：{parameters}"
    )
    kinds = {item.name: item.kind for item in parameters}
    assert kinds["self"] is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert kinds["input_ids"] is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert kinds["scores"] is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert not any(
        item.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        for item in parameters
    ), f"不许出现 *args / **kwargs：{parameters}"


def test_loop_guard_deadline_forces_eos_on_a_real_tensor():
    """`_LoopGuard` 的**超时**那一半（V1 零覆盖）：到点即把概率全压到 EOS。"""
    import torch

    guard = _LoopGuard(eos_token_id=7, timeout_seconds=-1.0)
    scores = torch.zeros((1, 16))
    out = guard([0], scores)

    assert guard.timed_out is True
    assert float(out[0, 7]) == 0.0
    assert float(out[0, 0]) == float("-inf")


# ---------------------------------------------------------------------------
# 七、文本级兜底（第 4 层）：真实坏行必须被砍掉
# ---------------------------------------------------------------------------


def test_text_level_fallback_strips_the_real_loop_line(monkeypatch):
    """假模型吐出 V1 那条真实坏行 → `transcribe_segment` 交回的文本被砍到 <60 字符。

    这条把第 4 层接在 `transcribe_segment` 里验：断言盯住**长度与前导正文本身**
    （光断言「没抛错」等于没验）。fixture 是截断副本（2515 字符），精确的 3333 / 周期 5 / ×663
    只在 V1 `meeting-notes\\.work\\handoff\\meeting_raw.md` 的 L283。
    """
    transcriber, _processor, _model, _loaded = _wire(monkeypatch, text=LOOP_LINE_283)

    result = transcriber.transcribe_segment(_loud_noise())

    assert result.ok is True, "文本级兜底命中不算失败（留下的前导正文仍有价值）"
    assert result.reason == "ok"
    assert len(result.text) < 60, f"循环垃圾没被砍掉：出来 {len(result.text)} 字符"
    assert result.text.startswith("这样赢赢哦，这空的，我现在这样，我"), "前导正文应当保留"
    assert len(LOOP_LINE_283) > 2000, "前提：喂进去的确实是那条真实坏行的副本"


def test_normal_transcript_line_survives_the_fallback(monkeypatch):
    """正常转写行原样返回（不误杀那一侧，同一条路径上再走一遍）。"""
    line = NORMAL_LINES[0]
    transcriber, _processor, _model, _loaded = _wire(monkeypatch, text=line)

    assert transcriber.transcribe_segment(_loud_noise()).text == line


# ---------------------------------------------------------------------------
# 八、`no_repeat_ngram_size` 的降级记忆
# ---------------------------------------------------------------------------


def test_unsupported_no_repeat_ngram_falls_back_once_and_remembers(monkeypatch):
    """首次 `TypeError` → warning + 置开关 `False` + **去掉该参数重跑一次**；之后不再带上它。"""
    monkeypatch.setattr(glm, "_NO_REPEAT_NGRAM_SUPPORTED", True, raising=False)

    def refuse(kwargs):
        if "no_repeat_ngram_size" in kwargs:
            raise TypeError("no_repeat_ngram_size 不被这个自定义 generate 支持")

    transcriber, _processor, model, _loaded = _wire(monkeypatch, text="回退后的文本",
                                                    on_generate=refuse)

    result = transcriber.transcribe_segment(_loud_noise())

    assert result.ok is True and result.text == "回退后的文本"
    assert len(model.calls) == 2, "降级路径要重跑一次生成"
    assert model.calls[0]["no_repeat_ngram_size"] == 8
    assert "no_repeat_ngram_size" not in model.calls[1]
    assert glm._NO_REPEAT_NGRAM_SUPPORTED is False, "降级要记进模块级开关"

    # 记忆生效：下一段一次就成，且不再带那个参数
    transcriber.transcribe_segment(_loud_noise())
    assert len(model.calls) == 3
    assert "no_repeat_ngram_size" not in model.calls[2]


def test_other_generate_errors_propagate_when_switch_is_off(monkeypatch):
    """开关本来就是 `False` 时**异常照抛**（不再吞、也不再重跑）。"""
    monkeypatch.setattr(glm, "_NO_REPEAT_NGRAM_SUPPORTED", False, raising=False)

    def boom(kwargs):
        assert "no_repeat_ngram_size" not in kwargs
        raise TypeError("这一跑与那个参数无关")

    transcriber, _processor, model, _loaded = _wire(monkeypatch, on_generate=boom)

    with pytest.raises(TypeError):
        transcriber.transcribe_segment(_loud_noise())
    assert len(model.calls) == 1


# ---------------------------------------------------------------------------
# 九、`_load_from_hub`：离线优先，只捕 `OSError`
# ---------------------------------------------------------------------------


def test_load_from_hub_tries_local_cache_first_then_falls_back():
    """先 `local_files_only=True`；只有 `OSError` 才回退联网（新机器首次装机仍能下载）。"""
    calls: list[tuple[str, dict]] = []

    class _Loader:
        @staticmethod
        def from_pretrained(model_id, **kwargs):
            calls.append((model_id, kwargs))
            if kwargs.get("local_files_only"):
                raise OSError("本地缓存缺文件")
            return "loaded"

    assert glm._load_from_hub(_Loader, dtype="bfloat16") == "loaded"
    assert calls == [
        (glm.MODEL_ID, {"local_files_only": True, "dtype": "bfloat16"}),
        (glm.MODEL_ID, {"dtype": "bfloat16"}),
    ]


def test_load_from_hub_does_not_swallow_non_oserror():
    """不是缓存缺文件（`ValueError` 之类）→ 照抛，不拿联网下载去糊。"""
    calls: list[dict] = []

    class _Loader:
        @staticmethod
        def from_pretrained(model_id, **kwargs):
            calls.append(kwargs)
            raise ValueError("不是缓存问题")

    with pytest.raises(ValueError):
        glm._load_from_hub(_Loader)
    assert len(calls) == 1 and calls[0] == {"local_files_only": True}
