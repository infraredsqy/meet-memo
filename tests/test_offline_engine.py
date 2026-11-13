"""`meeting/offline/engine.py` 的契约用例：输出解析、引擎接线、惰性导入。

**一条都不加载真模型**（`tests/conftest.py` 的 5 秒看门狗 + 真模型加载远超它）：
引擎那一段用假 processor / 假模型打在 `_load_models` 这个接缝上，音频解码也换成
返回固定波形的假件。真引擎只走一条一次性 MOSSTD 冒烟脚本，跑完即弃。

三块要盯的：

  - `parse_transcript` 吃 `[start][Sxx]text[end]`（多段、带空格的正文、标签切换、
    缺结束标签、坏时间戳、正文里的方括号），**自己实现**，不依赖第三方包；
  - 引擎把**波形 ndarray** 送进 processor（不是路径），并且 `generate` 收到的是
    `input_ids` / `attention_mask` / `input_features` / `audio_feature_lengths` /
    `audio_chunk_mapping` 五个键，取输出时按 `attention_mask` 的和切掉提示词；
  - `import meeting.offline.engine` 在**全新解释器**里不拉 `torch` / `transformers` /
    `av` / `webrtcvad`（照 `tests/test_asr_glm.py` 的既有做法）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from transformers import GenerationConfig

from meeting.offline import engine as engine_module
from meeting.offline.engine import (
    DEFAULT_PROMPT,
    OfflineSegment,
    parse_transcript,
)
from tests import _helpers as helpers

#: 假模型交回的提示词长度（`prompt_len`，也是取输出时的切点）
PROMPT_TOKENS = 7
#: 假模型生成的 token id（解码由假 tokenizer 按记录下的长度交回正文）
GENERATED_TOKENS = 12


# ---------------------------------------------------------------------------
# 一、`parse_transcript`：官方紧凑格式
# ---------------------------------------------------------------------------


def test_parse_transcript_reads_one_segment() -> None:
    """`[0.48][S01]大家好[end]` 给出正确的 start / end / speaker / text。"""
    segments = parse_transcript("[0.48][S01]大家好[2.31]")

    assert segments == [OfflineSegment(start=0.48, end=2.31, speaker="S01", text="大家好")]


def test_parse_transcript_reads_multiple_segments_and_label_switches() -> None:
    """多段连排、标签在段之间切换：段序、标签、区间逐个对上。"""
    raw = (
        "[0.48][S01]大家好[2.31]"
        "[2.31][S01]今天开会[4.02]"
        "[4.10][S02]我先说两句[7.55]"
        "[7.60][S01]好[8.00]"
    )

    segments = parse_transcript(raw)

    assert [(s.start, s.end, s.speaker, s.text) for s in segments] == [
        (0.48, 2.31, "S01", "大家好"),
        (2.31, 4.02, "S01", "今天开会"),
        (4.10, 7.55, "S02", "我先说两句"),
        (7.60, 8.00, "S01", "好"),
    ]


def test_parse_transcript_keeps_spaces_inside_the_body() -> None:
    """正文里的空格与英文原样留下，只有首尾空白被裁掉。"""
    segments = parse_transcript("[1.0][S03]  hello world, 这里有空格 [2.5]")

    assert len(segments) == 1
    assert segments[0].text == "hello world, 这里有空格"
    assert segments[0].speaker == "S03"


def test_parse_transcript_keeps_brackets_inside_the_body() -> None:
    """正文里出现方括号（含带数字的）不会被当成段的边界。"""
    segments = parse_transcript("[0.5][S01]他说[笑]了两声 [0.5] 就停了[3.0]")

    assert len(segments) == 1
    assert segments[0].text == "他说[笑]了两声 [0.5] 就停了"
    assert (segments[0].start, segments[0].end, segments[0].speaker) == (0.5, 3.0, "S01")


def test_parse_transcript_accepts_multi_line_output() -> None:
    """段之间换行（模型按行吐）照样解析。"""
    segments = parse_transcript("[0.1][S01]第一段[1.1]\n[1.2][S02]第二段[2.2]\n")

    assert [(s.speaker, s.text) for s in segments] == [("S01", "第一段"), ("S02", "第二段")]


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "没有任何标签的一段话",
        "[0.5][S01]正文[0.2]",       # end < start
        "[abc][S01]正文[1.0]",       # 起始不是时间戳
        "[0.5][X01]正文[1.0]",       # 说话人标签形态不对
        "[0.5][S01][1.0]",           # 正文为空
        "[0.5][S01]   [1.0]",        # 正文只有空白
    ],
)
def test_parse_transcript_drops_malformed_input(raw: str) -> None:
    """坏输入一律丢掉：起止倒挂、标签不合法、正文为空。

    ⚠ 另外两档**不在**这一档（正文要留下）：见
    `test_parse_transcript_keeps_a_final_segment_missing_its_end_tag`、
    `test_parse_transcript_ignores_free_text_before_a_segment` 与
    `test_parse_transcript_keeps_the_body_when_trailing_noise_follows`。
    """
    assert parse_transcript(raw) == []


def test_parse_transcript_ignores_free_text_before_a_segment() -> None:
    """段头之前挂着游离文本时，游离文本丢掉、那一段照样收下。"""
    segments = parse_transcript("开场白[0.5][S01]正文[1.0]")

    assert [(s.start, s.end, s.speaker, s.text) for s in segments] == [
        (0.5, 1.0, "S01", "正文"),
    ]


def test_parse_transcript_keeps_the_body_when_trailing_noise_follows() -> None:
    """结束标签后面跟着的**不是时间戳方括号**时不判坏段：正文留下，噪声丢掉。

    事后整段跑时，模型偶尔会在末尾多吐几个字或一串标点；因为这点尾巴丢掉一整段真实转写，
    代价比留下那一段大得多。
    """
    segments = parse_transcript("[0.5][S01]正文[9.9]尾部杂音")

    assert [(s.start, s.end, s.speaker, s.text) for s in segments] == [
        (0.5, 9.9, "S01", "正文"),
    ]


def test_parse_transcript_keeps_a_final_segment_missing_its_end_tag() -> None:
    """最后一段少了 `[结束]` 时正文不丢：正文取到下一个段头之前，end 落在那个时间戳上。"""
    raw = "[0.5][S01]完整的一段[2.0][2.5][S02]结尾少了一个标签"

    segments = parse_transcript(raw)

    assert [(s.start, s.end, s.speaker) for s in segments] == [
        (0.5, 2.0, "S01"),
        (2.5, 2.5, "S02"),
    ]
    assert segments[1].text == "结尾少了一个标签"


def test_parse_transcript_keeps_a_lone_final_segment_without_end_tag() -> None:
    """整份输出只有一段、且少了 `[结束]`：正文留下，`end` 退回 `start`。"""
    segments = parse_transcript("[12.5][S03]只有这一段，没有结束标签")

    assert [(s.start, s.end, s.speaker) for s in segments] == [(12.5, 12.5, "S03")]
    assert segments[0].text == "只有这一段，没有结束标签"


# ---------------------------------------------------------------------------
# 二、引擎：假 processor + 假模型（不加载真模型）
# ---------------------------------------------------------------------------


class _FakeTensor:
    """假张量：够 `outputs[0][n:]`、`attention_mask[0].sum().item()` 与 `.shape` 用。"""

    def __init__(self, values, shape=None) -> None:
        self.values = list(values)
        self._shape = tuple(shape) if shape is not None else (1, len(self.values))

    @property
    def shape(self) -> tuple[int, ...]:
        return self._shape

    def numel(self) -> int:
        return len(self.values)

    def sum(self) -> "_FakeTensor":
        return _FakeTensor([sum(self.values)], shape=())

    def item(self):
        return self.values[0]

    def __getitem__(self, index):
        if isinstance(index, tuple):
            _, cols = index
            return _FakeTensor(self.values[cols])
        if isinstance(index, slice):
            return _FakeTensor(self.values[index])
        if index == 0 and self._shape and self._shape[0] == 1:
            return _FakeTensor(self.values, shape=(len(self.values),))
        raise AssertionError(f"假张量没实现这种下标：{index!r}")


class _FakeInputs(dict):
    """假 `BatchFeature`：既能 `**inputs` 散开，也能按属性取（真件两样都支持）。"""

    def __init__(self, prompt_tokens: int = PROMPT_TOKENS) -> None:
        rows = {
            "input_ids": _FakeTensor([3] * prompt_tokens),
            "attention_mask": _FakeTensor([1] * prompt_tokens),
            "input_features": _FakeTensor([0.0] * 4),
            "audio_feature_lengths": _FakeTensor([4]),
            "audio_chunk_mapping": _FakeTensor([0] * 4),
        }
        super().__init__(rows)
        for name, tensor in rows.items():
            setattr(self, name, tensor)
        self.moves: list = []

    def to(self, *args, **kwargs):
        self.moves.append((args, kwargs))
        return self


class _FakeTokenizer:
    """假 tokenizer：只解码，记下被要求解码的 token。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.decoded: list[list[int]] = []
        self.skip_flags: list[bool] = []

    def decode(self, tokens, skip_special_tokens: bool = False) -> str:
        self.decoded.append(list(tokens.values))
        self.skip_flags.append(skip_special_tokens)
        return self.text


class _FakeProcessor:
    """假 processor：`apply_chat_template` + `__call__` + `tokenizer`。"""

    def __init__(self, text: str) -> None:
        self.tokenizer = _FakeTokenizer(text)
        self.feature_extractor = SimpleNamespace(sampling_rate=16000)
        self.templates: list[tuple] = []
        self.calls: list[dict] = []
        self.result = _FakeInputs()

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=False) -> str:
        self.templates.append((messages, tokenize, add_generation_prompt))
        return "<|user|>音频<|assistant|>"

    def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.result


class _FakeModel:
    """假模型：记下 `generate` 收到的 kwargs，交回「提示词 + 生成」的 token 串。"""

    def __init__(self) -> None:
        self.generation_config = GenerationConfig()
        self.calls: list[dict] = []
        self.outputs = _FakeTensor(
            [3] * PROMPT_TOKENS + list(range(100, 100 + GENERATED_TOKENS))
        )
        self.moves: list = []

    def to(self, dtype=None, device=None):
        self.moves.append((dtype, device))
        return self

    def eval(self):
        return self

    def generate(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.outputs


#: 解出来的固定波形（`decode_audio` 的假件交回它，用来验「以 ndarray 进 processor」）
WAVEFORM = np.linspace(-0.5, 0.5, 1600, dtype=np.float32)


@pytest.fixture()
def fake_engine(monkeypatch: pytest.MonkeyPatch):
    """装好假 processor / 假模型 / 假解码的引擎（返回 `(engine, processor, model)`）。"""
    processor = _FakeProcessor("[0.48][S01]大家好[2.31][2.31][S02]今天开会[4.02]")
    model = _FakeModel()
    decoded: list = []

    def fake_load_models(model_name, *, dtype, device):
        decoded.append((model_name, dtype, device))
        return processor, model, None

    def fake_decode_audio(path):
        decoded.append(("audio", str(path)))
        return WAVEFORM

    monkeypatch.setattr(engine_module, "_load_models", fake_load_models)
    monkeypatch.setattr(engine_module, "_decode_audio", fake_decode_audio)
    engine = engine_module.MossOfflineEngine(model="local/snapshot", max_new_tokens=64)
    engine.processor = processor
    engine.transcriber = model
    engine._decoded = decoded
    return engine, processor, model


def test_engine_transcribes_through_the_fake_model(fake_engine) -> None:
    """整条链走通：分段解析出来、正文与标签对得上。"""
    engine, processor, model = fake_engine

    segments = engine.transcribe("meeting_audio_loop.mp3")

    assert [(s.speaker, s.text) for s in segments] == [("S01", "大家好"), ("S02", "今天开会")]
    assert engine._decoded[0] == ("audio", "meeting_audio_loop.mp3")
    assert len(model.calls) == 1


def test_engine_passes_the_waveform_array_not_a_path(fake_engine) -> None:
    """processor 收到的是**波形 ndarray**（不在先转成路径/字符串），模板消息里那一项也是它。"""
    engine, processor, model = fake_engine

    engine.transcribe("some.mp3")

    call = processor.calls[0]
    assert isinstance(call["audio"], list) and len(call["audio"]) == 1
    assert isinstance(call["audio"][0], np.ndarray)
    assert call["audio"][0].dtype == np.float32
    assert call["audio"][0] is WAVEFORM
    assert call["return_tensors"] == "pt"
    assert call["max_length"] == engine_module.MAX_LENGTH

    messages, tokenize, add_generation_prompt = processor.templates[0]
    assert tokenize is False
    assert add_generation_prompt is True
    content = messages[0]["content"]
    assert content[0] == {"type": "audio", "audio": WAVEFORM}
    assert content[1] == {"type": "text", "text": DEFAULT_PROMPT}
    assert messages[0]["role"] == "user"


def test_engine_passes_the_five_processor_keys_to_generate(fake_engine) -> None:
    """`generate` 收到五个键（`input_ids` / `attention_mask` / `input_features` /
    `audio_feature_lengths` / `audio_chunk_mapping`）加一份 generation_config。"""
    engine, processor, model = fake_engine

    engine.transcribe("a.wav")

    kwargs = model.calls[0]
    assert set(kwargs) == {
        "input_ids",
        "attention_mask",
        "input_features",
        "audio_feature_lengths",
        "audio_chunk_mapping",
        "generation_config",
    }
    assert kwargs["generation_config"].max_new_tokens == 64
    assert kwargs["generation_config"].do_sample is False


def test_engine_strips_the_prompt_tokens_before_decoding(fake_engine) -> None:
    """取输出时按 `attention_mask` 的和切掉提示词（`prompt_len` 那一刀），再解码。"""
    engine, processor, model = fake_engine

    engine.transcribe("a.wav")

    assert processor.tokenizer.decoded == [[100 + i for i in range(GENERATED_TOKENS)]]
    assert processor.tokenizer.skip_flags == [True]


def test_engine_moves_the_batch_feature_to_the_resolved_device(fake_engine) -> None:
    """整份输入按解析出来的设备搬一次（`auto` 在没卡时落到 `cpu`）。"""
    engine, processor, model = fake_engine

    engine.transcribe("a.wav")

    assert len(processor.result.moves) == 1
    args, kwargs = processor.result.moves[0]
    assert list(args) + list(kwargs.values()) == [engine.device]
    assert engine.device in ("cpu", "cuda:0")


def test_engine_is_lazy_and_loads_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型**惰性加载**：构造时不加载；两次 `transcribe` 只加载一次。"""
    loaded: list = []
    processor = _FakeProcessor("[0.1][S01]一[0.2]")
    model = _FakeModel()

    def fake_load_models(model_name, *, dtype, device):
        loaded.append((model_name, dtype, device))
        return processor, model, None

    monkeypatch.setattr(engine_module, "_load_models", fake_load_models)
    monkeypatch.setattr(
        engine_module, "_decode_audio", lambda path: WAVEFORM
    )
    engine = engine_module.MossOfflineEngine(model="m", dtype="bfloat16")

    assert engine.processor is None and engine.transcriber is None
    engine.transcribe("a.wav")
    engine.transcribe("b.wav")

    assert loaded == [("m", "bfloat16", engine.device)]


def test_engine_load_failure_is_kept_and_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """**第一次加载就失败**：原话留在 `load_error` 上，后续每一趟当场拒、不再敲一遍加载。

    「偶发」那一档的两个落点都在这里：失手这件事**读得出来**（`load_error` 非空，
    调用方据此把这一路记成加载失败），而且**不重试**（同一个场次目录的第二路、
    同一条链上的下一次调用都不会让那句加载错误再犯一次）。
    """
    attempts: list = []
    processor = _FakeProcessor("[0.1][S01]一[0.2]")

    def flaky_load(model_name, *, dtype, device):
        attempts.append((model_name, dtype, device))
        if len(attempts) == 1:
            raise OSError("第一次读不动模型快照")
        return processor, _FakeModel(), None

    monkeypatch.setattr(engine_module, "_load_models", flaky_load)
    monkeypatch.setattr(engine_module, "_decode_audio", lambda path: WAVEFORM)
    engine = engine_module.MossOfflineEngine(model="m")

    assert engine.load_error is None
    with pytest.raises(OSError, match="第一次读不动模型快照"):
        engine.transcribe("a.wav")

    assert engine.load_error == "OSError: 第一次读不动模型快照"
    # 第二次调用：当场拒（原话照旧），加载接缝一次都不再进
    with pytest.raises(RuntimeError, match="第一次读不动模型快照"):
        engine.transcribe("b.wav")
    assert len(attempts) == 1


def test_engine_passes_model_dtype_and_device_to_the_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """`model` / `dtype` 原样传给加载接缝（`model` 既收仓库 id，也收本地快照绝对路径）。"""
    captured: list = []

    def fake_load_models(model_name, *, dtype, device):
        captured.append((model_name, dtype, device))
        return _FakeProcessor(""), _FakeModel(), None

    monkeypatch.setattr(engine_module, "_load_models", fake_load_models)
    monkeypatch.setattr(engine_module, "_decode_audio", lambda path: WAVEFORM)
    snapshot = r"C:\cache\hf\models--OpenMOSS-Team--MOSS-Transcribe-Diarize\snapshots\abc123"

    engine = engine_module.MossOfflineEngine(
        model=snapshot, dtype="bfloat16", device="cpu", max_new_tokens=8192
    )
    engine.transcribe("a.wav")

    assert captured == [(snapshot, "bfloat16", "cpu")]
    assert engine.max_new_tokens == 8192


# ---------------------------------------------------------------------------
# 三、精度档位与设备解析（照官方 helper 的形态）
# ---------------------------------------------------------------------------


def test_torch_dtype_names_cover_the_four_supported_levels() -> None:
    """四档名字都认（`bf16` / `bfloat16` / `fp16` / `float16` / `fp32` / `float32`）。"""
    import torch

    from meeting.offline.engine import _torch_dtype_from_name

    for name in ("bf16", "bfloat16"):
        assert _torch_dtype_from_name(torch, name) is torch.bfloat16
    for name in ("fp16", "float16"):
        assert _torch_dtype_from_name(torch, name) is torch.float16
    for name in ("fp32", "float32"):
        assert _torch_dtype_from_name(torch, name) is torch.float32


def test_torch_dtype_rejects_an_unknown_level() -> None:
    """认不出的档位当场报错，不静默降级成别的精度。"""
    import torch

    from meeting.offline.engine import _torch_dtype_from_name

    with pytest.raises(ValueError, match="精度档位"):
        _torch_dtype_from_name(torch, "int8")


def test_resolve_device_passes_through_explicit_names() -> None:
    """`device` 明写时按原样用（`auto` 之外的值不做二次判断）。"""
    assert engine_module._resolve_device("cpu") == "cpu"
    assert engine_module._resolve_device("cuda:1") == "cuda:1"


def test_default_engine_construction_touches_no_heavy_model() -> None:
    """构造引擎不加载任何东西（惰性），`OfflineEngine` 协议只要求 `transcribe`。"""
    engine = engine_module.MossOfflineEngine(model="m")

    assert engine.processor is None and engine.transcriber is None
    assert callable(engine.transcribe)
    assert hasattr(engine_module.OfflineEngine, "transcribe")


# ---------------------------------------------------------------------------
# 四、全新解释器里的 import 链：重依赖一个都不许被拉进来
# ---------------------------------------------------------------------------

#: 在子进程里跑的一行：只 import 被测模块，再报出**重依赖**。
_IMPORT_CHAIN_CODE = (
    "import sys, meeting.offline.engine as e; "
    "heavy = [m for m in ('torch', 'transformers', 'av', 'webrtcvad', 'librosa') "
    "if m in sys.modules]; "
    "print('heavy=' + ','.join(heavy)); "
    "print('seam=' + e.MossOfflineEngine.__name__ + '/' + e.OfflineSegment.__name__)"
)


def test_import_chain_pulls_no_heavy_dependency() -> None:
    """`import meeting.offline.engine` 只该拉进标准库与包内模块。

    **必须换一个全新解释器**：本进程里 `numpy` / `torch` 早被别的用例与
    `tests/conftest.py` 的声卡兜底拉进 `sys.modules` 了，同进程断言等于没验。
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
        f"`import meeting.offline.engine` 把重依赖拉进了 sys.modules：{lines[0]!r}\n{output}"
    )
    assert lines[1] == "seam=MossOfflineEngine/OfflineSegment", output


def test_package_import_pulls_no_heavy_dependency() -> None:
    """`import meeting.offline`（包标记）同样只拉标准库与包内模块。"""
    code = (
        "import sys, meeting.offline as p; "
        "heavy = [m for m in ('torch', 'transformers', 'av', 'webrtcvad', 'librosa') "
        "if m in sys.modules]; "
        "print('heavy=' + ','.join(heavy))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(helpers.REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(helpers.REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    output = (result.stdout or "") + (result.stderr or "")
    assert result.returncode == 0, output
    assert (result.stdout or "").strip() == "heavy=", output


# ---------------------------------------------------------------------------
# 退出守卫：进程正在退出时不发起任何 import（260928-2 那场 (b) 机制的守门用例）
# ---------------------------------------------------------------------------


def test_load_models_refuses_to_import_while_interpreter_is_finalizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sys.is_finalizing()` 为真时 `_load_models` 当场抛本模块的专用异常。

    守卫在一切 import **之前**：退出期再首导 torch/transformers 只会被拦腰打断成
    一句莫名 `ImportError`（daemon 线程），如实报「进程正在退出，会后链中断」，
    状态文件那一格才有诚实的话可写。
    """
    monkeypatch.setattr(sys, "is_finalizing", lambda: True)

    with pytest.raises(
        engine_module.EngineShutdownInterrupted, match="进程正在退出"
    ):
        engine_module._load_models("whatever", dtype="bfloat16", device="cpu")


# ---------------------------------------------------------------------------
# release_engine（的 d6）：丢引用 + 请 torch 把缓存还给系统
# ---------------------------------------------------------------------------


def test_release_engine_drops_model_references_and_reports_the_drop() -> None:
    """带着权重的那一格：processor / transcriber 两格引用丢掉，交回 True。"""
    engine = SimpleNamespace(processor=object(), transcriber=object())
    assert engine_module.release_engine(engine) is True
    assert engine.processor is None
    assert engine.transcriber is None
    # 幂等：第二次起没有可丢的（False），再来一遍也不抛
    assert engine_module.release_engine(engine) is False


def test_release_engine_survives_a_bare_fake_and_reports_no_drop() -> None:
    """假引擎（两格都缺席）：安静 no-op、交回 False，torch 缺席也不抛。"""
    class Bare:
        pass

    assert engine_module.release_engine(Bare()) is False
