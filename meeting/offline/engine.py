"""MOSSTD（MOSS-Transcribe-Diarize）离线引擎封装与输出解析。

这一层只做「一条音频路径进去，一串带 `Sxx` 标签的分段出来」：模型加载、消息构造、
处理器调用、生成、解码、解析。**不做说话人编号规范化**（那是 `meeting/offline/speakers.py`
的事），也不写任何场次产物。

两条来自实测的硬约束写在这里，改之前先读完：

  1. **波形以 ndarray 传进 processor，绝不传文件路径。** `transformers.audio_utils.load_audio`
     对 ndarray 直接返回、不要求任何解码后端；而字符串路径（mp3 与 wav 都一样）会落到
     `requires_backends(..., ["librosa"])`，本机没装 librosa 就当场抛错。波形因此由
     `meeting/offline/audio.py` 用 PyAV 解好再传进来。
  2. **`[start][Sxx]text[end]` 的解析是本模块自己实现的**，不依赖第三方包：官方那份
     `transcript_parser.py` 只住在 V1 的临时 scratch 里（不在发行树、不在 PyPI）。

重依赖惰性：`torch` / `transformers` / `numpy` 一律在函数体内 import
（`meeting/asr/glm.py` 的既有纪律）。`dtype` 的合法取值与 `_resolve_device` 的形态照抄
官方 helper（`moss_transcribe_diarize/inference_utils.py:50-71`），那是本项目实测跑通的那一份。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

log = logging.getLogger("meeting.offline.engine")

#: 官方 helper 的提示词（`inference_utils.py:13-17` 的 `DEFAULT_PROMPT`，逐字）
DEFAULT_PROMPT = (
    "请将音频转写为文本，每一段需以起始时间戳和说话人编号"
    "（[S01]、[S02]、[S03]…）开头，正文为对应的语音内容，"
    "并在段末标注结束时间戳，以清晰标明该段语音范围。"
)

#: 处理器一次能吃下的最大 token 数（照官方 helper 的 `prepare_inputs` 缺省）
MAX_LENGTH = 131072

#: 精度档位名 → `torch.dtype` 属性名（照官方 helper 的 `dtype_from_name`，只留本机实测的两档）
DTYPE_NAMES: dict[str, str] = {
    "bf16": "bfloat16",
    "bfloat16": "bfloat16",
    "fp16": "float16",
    "float16": "float16",
    "fp32": "float32",
    "float32": "float32",
}

#: 段头 `[起始][Sxx]`：正文里那些孤立的方括号不会长成这个样子，所以它是段与段之间唯一的
#: 可靠接缝。两侧的空白一律用**懒惰**的 `\s*?`：贪婪的话 `head.end()` 会吃进下一段的
#: 前导空白，正文与收口位置就都错位了。`head.start("head")` 是段头的真正起点。
_HEAD_RE = re.compile(
    r"\s*?(?P<head>\[(?P<start>\d+(?:\.\d+)?)\]\s*?\[(?P<speaker>S\d+)\])\s*?"
)

#: 像时间戳的方括号：整格都是数字与小数点（`[0.48]`、`[12]`；`[笑]`、`[abc]` 不算）
_TIME_BRACKET_RE = re.compile(r"\[(?P<stamp>[\d.]+)\]")


@dataclass(frozen=True, slots=True)
class OfflineSegment:
    """离线转写的一段：秒级区间 + 引擎原始说话人标签 + 正文。"""

    start: float
    end: float
    speaker: str
    text: str


class OfflineEngine(Protocol):
    """离线引擎的协议：换引擎时上层（钩子、产物落盘）不动。

    实现只需给出 `transcribe`：吃音频文件路径，交回带 `Sxx` 原始标签的分段列表。
    """

    def transcribe(self, audio_path: str | Path) -> list[OfflineSegment]: ...


# ---------------------------------------------------------------------------
# 输出解析（自己实现，不 vendor 官方那份 transcript_parser.py）
# ---------------------------------------------------------------------------


def parse_transcript(text: str) -> list[OfflineSegment]:
    """解析官方紧凑格式 `[start][Sxx]text[end]`，返回分段列表。

    例：`[0.48][S01]大家好[2.31]` → `OfflineSegment(0.48, 2.31, "S01", "大家好")`。

    做法是**一次线性扫描**（`_HEAD_RE` 找段头、`_TIME_BRACKET_RE` 找结束标签），
    正文那一格不靠正则的贪婪/懒惰去猜。容错口径（会后整段跑，一段坏输出不该让整场作废）：

      - `[结束]` 取该段正文之后**第一个严格晚于起点的**「像时间戳」的方括号：不晚于起点的
        那些（正文里引用的更早时间）留给正文；一个都挑不出来、正文里却有这样的方括号时，
        这一段的时间序是坏的（`[0.5][S01]正文[0.2]`），整段丢掉；
      - **结束标签缺失**时（这一段的正文里根本没有像时间戳的方括号）照样收下这一段，
        `end` 退回 `start`——模型少写一个 `[...]` 时不丢正文；
      - 段头之前、两个段之间的游离文本一律丢掉：那不是转写内容；
      - 正文为空的段丢掉。

    纯文本处理，无重依赖。
    """
    out: list[OfflineSegment] = []
    cursor = 0
    while True:
        # 用 `search` 而不是 `match`：段头之前的游离文本（模型的开场白、残留标点）
        # 直接跳过——它不是转写内容，也不该让后面所有段都读不出来。
        head = _HEAD_RE.search(text, cursor)
        if head is None:
            break
        start_token = head.group("start")
        speaker = head.group("speaker")
        body_start = head.end()

        # 这一段的 `[结束]`：正文之后**第一个严格晚于起点的**「像时间戳」的方括号。
        # 不晚于起点的那些（正文里引用的更早时间、`[0.5][S01]正文[0.2]` 这种倒挂输出）
        # 留给正文，不当边界。
        start = _to_float(start_token)
        bracket = None
        last_bracket = None
        for candidate in _TIME_BRACKET_RE.finditer(text, body_start):
            last_bracket = candidate
            stamp = _to_float(candidate.group("stamp"))
            if stamp is not None and start is not None and stamp > start:
                bracket = candidate
                break

        if bracket is None:
            if last_bracket is not None:
                # 正文里有像时间戳的方括号、却没有一个晚于起点：这一段的时间序是坏的
                # （`[0.5][S01]正文[0.2]`），丢掉它，别把坏时间带下去。
                # ⚠ 必须把扫描位置推过这些方括号，否则下一轮会在同一个段头上原地打转。
                cursor = last_bracket.end() if last_bracket is not None else len(text)
                continue
            # 模型漏写了 `[结束]`：正文收口在下一个段头（没有就收到文本末尾），
            # 下一轮的扫描位置就是那个段头。
            next_head = _HEAD_RE.search(text, body_start)
            body_end = next_head.start("head") if next_head is not None else len(text)
            cursor = next_head.start("head") if next_head is not None else len(text)
            end_token = None
        else:
            body_end = bracket.start()
            end_token = bracket.group("stamp")
            # 下一轮从**结束方括号之后**接着扫（不是从正文末尾：那会把结束标签自己
            # 当成下一个段头的起点，于是整条链在这里断掉）。
            cursor = bracket.end()

        segment = _build(start_token, speaker, text[body_start:body_end], end_token)
        if segment is not None:
            out.append(segment)
    return out


def _to_float(token: str) -> float | None:
    """时间戳字符串 → 秒（认不出的返回 `None`）。"""
    try:
        return float(token)
    except ValueError:
        return None


def _build(start_token: str, speaker: str, body: str, end_token: str | None) -> OfflineSegment | None:
    """按口径造一段（`end_token` 为 `None` 时 `end` 退回 `start`）；不合口径的返回 `None`。

    `end < start`、`start` 不是数字、正文为空这三种一律丢掉：宁可少一段，不猜时间。
    """
    start = _to_float(start_token)
    if start is None:
        return None
    text = body.strip()
    if not text:
        return None
    end = _to_float(end_token) if end_token else None
    if end is None:
        end = start
    if end < start:
        return None
    return OfflineSegment(start=start, end=end, speaker=speaker, text=text)


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class MossOfflineEngine:
    """MOSSTD 引擎：模型**惰性加载**，第一次 `transcribe` 时才落盘。

    没装模型依赖的机器上 `import meeting.offline.engine` 照样成立，`meeting web` 的
    启动链也不会被拖慢（同 `meeting/asr/glm.py` 的纪律）。

    `model` 既接受 HF 仓库 id，也接受**本地快照绝对路径**（后者是本机实测走通的那条）。
    """

    def __init__(self, *, model: str, dtype: str = "bfloat16",
                 device: str = "auto", max_new_tokens: int = 8192) -> None:
        self.model = str(model)
        self.dtype = dtype
        self.max_new_tokens = int(max_new_tokens)
        self.device = _resolve_device(device)
        self._torch_dtype = None
        self.processor = None
        self.transcriber = None
        #: 这一台引擎**第一次加载**失手的原话（`"TypeName: 原话"`）；没失手时是 `None`。
        #: 它同时是那道「不重试」的闸：一次加载失手之后每一趟 `transcribe()` 当场把它
        #: 原样抛出来，不回到 `_load_models()` 再试一遍（见 `_load()`）。
        self._load_failed: str | None = None

    # -- 加载 ---------------------------------------------------------------

    @property
    def load_error(self) -> str | None:
        """这一台引擎的加载失手了没有（失手时交回原话，否则交回 `None`）。

        调用方据此把「引擎加载失败」与「引擎跑这一块时抛错」分成两档：前者是一整趟
        都没开始，后者是某一块没成（`runner.REASON_ENGINE_LOAD_ERROR` 那一档）。
        """
        return self._load_failed

    def _load(self) -> None:
        """惰性加载：第一次 `transcribe()` 才落盘，加载失败**立成一档、不重试**。

        `transcriber is not None` 才认「已经加载好了」——只有加载成功才会给它赋值。
        第一次失手之后 `_load_failed` 记着原话，`_load()` 不再回到 `_load_models()`：
        原来那版「抛出去就忘」在同一个场次目录的第二路上会把同一句加载错误再犯一次
        （两路各一次、日志里看不出这是同一件事），重试一条真的加载不动的模型还要
        再花一次显存与时间。**这一档只说「加载失手过一次」**：据此拒掉后续调用，
        与「这一块跑不动」是两件事（见 `load_error`）。
        """
        if self.transcriber is not None:
            return
        if self._load_failed is not None:
            raise RuntimeError(self._load_failed)
        try:
            self.processor, self.transcriber, self._torch_dtype = _load_models(
                self.model, dtype=self.dtype, device=self.device
            )
        except Exception as exc:  # 加载失手：记下原话，下一次调用当场拒，不重试
            self._load_failed = f"{type(exc).__name__}: {exc}"
            log.error("MOSSTD 加载失败（%s）：%s", self.model, self._load_failed)
            raise

    # -- 契约 ---------------------------------------------------------------

    def transcribe(self, audio_path: str | Path) -> list[OfflineSegment]:
        """跑一遍整段音频，返回引擎原始标签的分段列表（不做编号规范化）。"""
        import torch

        self._load()
        waveform = _decode_audio(audio_path)
        messages = build_messages(waveform)
        inputs = prepare_inputs(self.processor, messages)
        text = self._generate(inputs, torch=torch)
        log.info("MOSSTD 转写完成：%s（%d 字符）", audio_path, len(text))
        return parse_transcript(text)

    def _generate(self, inputs, *, torch) -> str:
        """生成并解码成原始文本（照官方 helper 的形态，逐处对应 `inference_utils.py`）。"""
        processor = self.processor
        transcriber = self.transcriber
        inputs = inputs.to(self.device)
        prompt_len = int(inputs["attention_mask"][0].sum().item())

        generation_config = _generation_config(
            transcriber, max_new_tokens=self.max_new_tokens, do_sample=False
        )
        with torch.inference_mode(), _autocast(torch, self.device, self._torch_dtype):
            outputs = transcriber.generate(
                input_ids=inputs["input_ids"],
                attention_mask=inputs["attention_mask"],
                input_features=inputs["input_features"],
                audio_feature_lengths=inputs["audio_feature_lengths"],
                audio_chunk_mapping=inputs["audio_chunk_mapping"],
                generation_config=generation_config,
            )
        generated = outputs[0][prompt_len:]
        return processor.tokenizer.decode(generated, skip_special_tokens=True).strip()


# ---------------------------------------------------------------------------
# 消息与处理器调用（公开成函数，好让用例在假件上验这一段）
# ---------------------------------------------------------------------------


def _decode_audio(path):
    """解音频（`meeting/offline/audio.py` 的入口）。单列一个函数是为了给用例留接缝。"""
    from meeting.offline import audio as audio_module

    return audio_module.decode_audio(path)


def build_messages(waveform, *, prompt: str = DEFAULT_PROMPT) -> list[dict]:
    """构造官方形态的消息：`[{"role": "user", "content": [音频, 提示词]}]`。

    音频那一项带的是**波形本身**（float32 一维 ndarray）；传路径会落到 `load_audio`
    的 librosa 分支上（见模块头的第 1 条）。
    """
    return [
        {
            "role": "user",
            "content": [
                {"type": "audio", "audio": waveform},
                {"type": "text", "text": (prompt or DEFAULT_PROMPT).strip() or DEFAULT_PROMPT},
            ],
        }
    ]


def prepare_inputs(processor, messages: Sequence[dict], *, max_length: int = MAX_LENGTH):
    """`apply_chat_template` → `processor(text=..., audio=[波形], return_tensors="pt")`。

    实测产出的键：`input_ids` / `attention_mask` / `input_features` /
    `audio_feature_lengths` / `audio_chunk_mapping`（生成那一步逐个用上）。
    """
    text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    audios = [
        item["audio"]
        for message in messages
        for item in (message["content"] if not isinstance(message["content"], str) else [])
        if item.get("type") == "audio"
    ]
    return processor(text=text, audio=audios, max_length=max_length, return_tensors="pt")


def _generation_config(model, *, max_new_tokens: int, do_sample: bool):
    """官方的做法：拿模型自带 `generation_config` 的深拷贝，就地改两个字段。"""
    import copy

    config = copy.deepcopy(model.generation_config)
    config.max_new_tokens = int(max_new_tokens)
    config.do_sample = bool(do_sample)
    return config


def _autocast(torch, device, dtype):
    """CUDA + 半精度时开 autocast，其余情况退到 `no_grad()`（照官方 helper）。"""
    if str(device).startswith("cuda") and dtype in (torch.float16, torch.bfloat16):
        return torch.amp.autocast("cuda", dtype=dtype)
    return torch.no_grad()


# ---------------------------------------------------------------------------
# 加载与设备（照官方 helper 的 `dtype_from_name` / `resolve_device`）
# ---------------------------------------------------------------------------


def _torch_dtype_from_name(torch, name: str):
    """精度档位名 → `torch.dtype`（认不出的名字当场报错，不静默降级）。"""
    attr = DTYPE_NAMES.get(str(name).lower())
    if attr is None:
        raise ValueError(f"不支持的精度档位：{name!r}（可选 {'/'.join(sorted(DTYPE_NAMES))}）")
    return getattr(torch, attr)


def _resolve_device(name: str) -> str:
    """`auto` → 有 CUDA 就用 `cuda:0`，否则 `cpu`。"""
    import torch

    if name == "auto":
        return "cuda:0" if torch.cuda.is_available() else "cpu"
    return str(name)


def _load_from_hub(loader, model: str, **kwargs):
    """本地快照优先加载：先 `local_files_only=True`，缓存真缺文件时才回退联网。

    与 `meeting/asr/glm.py` 的 `_load_from_hub` 同一个理由：HF 仓库 id 那条路必先发一次
    HEAD 要 commit metadata，本地缓存再完整也绕不过；本地快照绝对路径则由
    `local_files_only=True` 直接命中。**只捕 `OSError`**（缓存不完整是它），别的异常照抛。
    """
    try:
        return loader.from_pretrained(model, local_files_only=True, **kwargs)
    except OSError as e:
        log.warning("本地加载失败（%s: %s），回退联网下载", type(e).__name__, e)
        return loader.from_pretrained(model, **kwargs)


def _load_models(model: str, *, dtype: str, device: str):
    """加载 Processor 与模型权重，返回 `(processor, model_obj, torch_dtype)`。

    重依赖全在函数体内惰性 import。**这是本模块唯一的模型入口**，也是测试的接缝：
    用例用 `monkeypatch.setattr(meeting.offline.engine, "_load_models", fake)` 打在
    **同一份模块对象**上。
    """
    import torch
    from transformers import AutoModelForCausalLM, AutoProcessor

    log.info("加载 MOSSTD：%s（dtype=%s device=%s）", model, dtype, device)
    torch_dtype = _torch_dtype_from_name(torch, dtype)
    processor = _load_from_hub(AutoProcessor, model, trust_remote_code=True)
    transcriber = _load_from_hub(
        AutoModelForCausalLM,
        model,
        trust_remote_code=True,
        dtype="auto",
        attn_implementation="sdpa",
    )
    transcriber = transcriber.to(dtype=torch_dtype).to(device).eval()
    log.info("MOSSTD 就绪：%s", device)
    return processor, transcriber, torch_dtype


def release_engine(engine: object) -> bool:
    """**让出离线引擎占的显存**：丢掉模型引用，然后把 PyTorch 缓存还给系统。

    离线链这一侧的 `release_gpu()` 对应物（会中那一份在 `meeting/backends/transcribe.py`，
    落点是收尾第 4 步与暂停；`docs/design-intent.md` 第 21 条「跑完、打断、终止，都当场
    结束」）。调用点在 `run_session()` 的 `finally`：跑成、失败、被打断三条出口都过它。

    为什么引用计数之外还要这一步：`run_session` 返回后引擎对象随引用计数消失，权重张量
    交还的是 **PyTorch 的缓存分配器**——它把空闲块留在自己手里等下次复用，**不会自动**
    还给驱动。服务进程常驻，`nvidia-smi` 里那一格就一直白占；`torch.cuda.empty_cache()`
    才把预留显存还给系统。先丢引用、后清缓存，与会中 `release_gpu()` 同一个次序。

    逐条语义（照会中先例）：

      - **没加载过的引擎是 no-op**：`processor` / `transcriber` 两格本来就是空（假件、
        没跑过任何块的引擎），只尝试清缓存；
      - **幂等**：连调几次安全，第二次起两格已经是空；
      - **`torch` 在函数体内惰性 import**，没装 torch、没卡、`empty_cache()` 自己抛错
        都只算「这一趟没能清缓存」，不影响引用已经丢掉。

    返回 `True` = 这一次真的丢掉了引用；`False` = 本来就没有可丢的。
    """
    dropped = False
    for attr in ("processor", "transcriber"):
        if getattr(engine, attr, None) is not None:
            try:
                setattr(engine, attr, None)
                dropped = True
            except Exception:  # pragma: no cover - 属性抹不掉只算这一格没成
                log.warning("离线引擎的 %s 引用抹不掉（显存释放打折扣）", attr, exc_info=True)
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # 没装 torch / 没 CUDA / 清缓存失败：引用已丢，安静收场
        log.info("torch.cuda.empty_cache() 跳过（无 CUDA 或依赖缺席，不影响引用释放）")
    return dropped
