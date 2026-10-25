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
（`meeting/asr/glm.py` 的既有纪律）；`torch` / `transformers` 的首次 import 包在
`meeting/importlock.py` 的进程级锁里（与会中 ASR 的首导互斥，理由见 `_load_models()`；
`transcribe()` 前置与 `_resolve_device()` 的 `import torch` 是同一把锁）。
`dtype` 的合法取值与 `_resolve_device` 的形态照抄
官方 helper（`moss_transcribe_diarize/inference_utils.py:50-71`），那是本项目实测跑通的那一份。
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from meeting.importlock import heavy_imports

log = logging.getLogger("meeting.offline.engine")


class EngineShutdownInterrupted(RuntimeError):
    """进程正在退出（`sys.is_finalizing()`），会后链的模型加载不再发起：daemon 线程在
    退出期继续首导会被拦腰打断成不知所云的 `ImportError`，如实抛本异常、状态文件落 failed。
    """

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


def parse_transcript(text: str) -> list[OfflineSegment]:
    """解析官方紧凑格式 `[start][Sxx]text[end]`，返回分段列表（一次线性扫描）。

    容错口径（一段坏输出不许让整场作废）：`[结束]` 取正文之后第一个**严格晚于起点**的
    像时间戳的方括号，一个都挑不出来而正文里却有方括号 → 时间序坏、整段丢；结束标签
    缺失照样收下、`end` 退回 `start`（不丢正文）；段头之前的游离文本与空正文段丢掉。
    """
    out: list[OfflineSegment] = []
    cursor = 0
    while True:
        # 用 search 而不是 match：段头之前的游离文本（开场白、残留标点）直接跳过。
        head = _HEAD_RE.search(text, cursor)
        if head is None:
            break
        start_token = head.group("start")
        speaker = head.group("speaker")
        body_start = head.end()

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
                # 正文里有像时间戳的方括号、却没有一个晚于起点：时间序坏，整段丢。
                # ⚠ 必须把扫描位置推过这些方括号，否则下一轮在同一个段头上原地打转。
                cursor = last_bracket.end() if last_bracket is not None else len(text)
                continue
            # 模型漏写 `[结束]`：正文收口在下一个段头（没有就收到文本末尾）。
            next_head = _HEAD_RE.search(text, body_start)
            body_end = next_head.start("head") if next_head is not None else len(text)
            cursor = next_head.start("head") if next_head is not None else len(text)
            end_token = None
        else:
            body_end = bracket.start()
            end_token = bracket.group("stamp")
            # 下一轮从结束方括号之后接着扫（从正文末尾会把结束标签自己当成下一个段头）。
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


class MossOfflineEngine:
    """MOSSTD 引擎：模型**惰性加载**，第一次 `transcribe` 时才落盘（没装模型依赖的机器
    上 import 本模块照样成立）。`model` 接受 HF 仓库 id，也接受本地快照绝对路径。
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
        #: 这一台引擎第一次加载失手的原话（没失手是 None）。它同时是「不重试」的闸：
        #: 一次失手后每趟 transcribe() 当场原样抛，不回 _load_models() 再试。
        self._load_failed: str | None = None

    # -- 加载 ---------------------------------------------------------------

    @property
    def load_error(self) -> str | None:
        """加载失手时交回原话，否则 None。调用方据此把「加载失败」（一整趟没开始）与
        「跑某一块时抛错」分成两档（runner.REASON_ENGINE_LOAD_ERROR）。
        """
        return self._load_failed

    def _load(self) -> None:
        """惰性加载：第一次 `transcribe()` 才落盘，加载失败**立成一档、不重试**。
        `transcriber is not None` 才认「已加载好」。不重试的理由：同一场次目录的第二路
        会把同一句加载错误再犯一次，重试一条真加载不动的模型还多花一次显存与时间。
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
        # torch 首导进进程级锁（与 _load_models() 同一把；RLock 同线程可重入）。
        with heavy_imports():
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


def _decode_audio(path):
    """解音频（`meeting/offline/audio.py` 的入口）；单列出来是给用例留接缝。"""
    from meeting.offline import audio as audio_module

    return audio_module.decode_audio(path)


def build_messages(waveform, *, prompt: str = DEFAULT_PROMPT) -> list[dict]:
    """构造官方形态的消息：`[{"role": "user", "content": [音频, 提示词]}]`。
    音频带的是**波形本身**（float32 一维 ndarray）——传路径会落到 load_audio 的
    librosa 分支上（见模块头第 1 条硬约束）。
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


def _torch_dtype_from_name(torch, name: str):
    """精度档位名 → `torch.dtype`（认不出的名字当场报错，不静默降级）。"""
    attr = DTYPE_NAMES.get(str(name).lower())
    if attr is None:
        raise ValueError(f"不支持的精度档位：{name!r}（可选 {'/'.join(sorted(DTYPE_NAMES))}）")
    return getattr(torch, attr)


def _resolve_device(name: str) -> str:
    """`auto` → 有 CUDA 就用 `cuda:0`，否则 `cpu`。"""
    # torch 首导进进程级锁（与别的加载链并发首启时串行化）；is_available() 在锁外。
    with heavy_imports():
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

    重依赖全在函数体内惰性 import，且整段 import 包进 `heavy_imports()`（进程级锁）：
    会中 ASR 与本模块在各自后台线程首次 import transformers，两线程同时首导同一模块时
    其一可能见到半初始化的模块对象（260928-2 实测 `ImportError: cannot import name
    AutoModelForCausalLM`）。import 前后各一条带线程名的 debug 日志，下次偶现好定案。
    进程退出期当场抛 `EngineShutdownInterrupted`，不触任何 import。
    用例的接缝：`monkeypatch.setattr(meeting.offline.engine, "_load_models", fake)`
    打在同一份模块对象上。
    """
    if sys.is_finalizing():
        raise EngineShutdownInterrupted(
            "进程正在退出，会后链中断，可从工作台重新发起"
        )
    thread = threading.current_thread().name
    log.debug("会后引擎加载：准备首次 import torch/transformers（thread=%s）", thread)
    with heavy_imports():
        import torch
        from transformers import AutoModelForCausalLM, AutoProcessor
    log.debug("会后引擎加载：torch/transformers import 完成（thread=%s）", thread)

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
    """**让出离线引擎占的显存**（会中 release_gpu() 的对应物，design-intent 第 21 条）：
    丢掉模型引用再 `torch.cuda.empty_cache()`。引用计数之外还要这一步，是因为 PyTorch
    缓存分配器不会自动把预留显存还给驱动，服务进程常驻就会一直白占。没加载过是 no-op、
    幂等；torch 惰性 import，缺依赖只算「没能清缓存」。返回是否真的丢了引用。
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
