"""GLM-ASR 的加载与**单段识别** —— 四层防护的挂载点。

搬自 V1 `mnotes_core/recorder.py`（原件见 V1 仓库
`C:/Users/<user>/tools/meeting-notes/mnotes_core/recorder.py`；照 `docs/v1-lineage.md`
第 11 章「原 1.0 第 5 条」，搬完那份工作副本就删掉了）：

  - `_NO_REPEAT_NGRAM_SUPPORTED` `:27-29`      → 降级记忆（四层防护的第 1 层）
  - `TranscribeResult` `:32-44`                → `TranscribeResult`
  - `MODEL_ID` `:175`                          → `MODEL_ID`
  - `_is_noise_segment` `:252-273`             → `_is_noise_segment`（ASR 前置噪声预筛）
  - `_load_from_hub` `:276-295`                → `_load_from_hub`（离线优先，只捕 `OSError`）
  - `_load_transcriber` `:321-341`             → `_load_models`（两层 `_load_from_hub`）
  - `transcribe_segment` `:495-598`（核心 `:512-597`） → `GlmAsrTranscriber.transcribe_segment`

只交付「一串 int16 样本进去，一段文本或一个失败原因出来」：本模块**不碰线程、不碰队列、不碰服务**。
V1 的 `MeetingRecorder` 把采集与识别挤在一个类里（`:298-342` 是识别那一半，`:343-493` 是设备与
sounddevice / soundcard 那一半），V2 只搬识别那一半 —— 采集在 `meeting/capture/`，两件事不再同住
一个对象（本单元不搬 `recorder.py:371-493`，见 `phase1/task.md` 的「边界」一节）。

**模块顶层只有标准库与包内 import**（新包的硬约束，`phase1/task.md` 的「必须保住的形状」第 5 条）：
`torch` / `transformers` / `numpy` / `webrtcvad` 一律在函数体内惰性 import，否则
`transcriber.backend = auto` 在没装模型依赖的机器上会解析失败，`meeting web` 的启动链也会被拖慢。

搬过来时动了这几处，其余逐字照搬（出处注释逐段标出）：

  1. logger 名：V1 的 `logging.getLogger("meeting")` → `"meeting.asr.glm"`；
  2. `tune = load_asr_tunables()`（V1 `:516-518`、`:591-592`）→ `meeting.config` 的
     `asr.timeout_seconds` / `asr.max_new_tokens` / `asr.repeat_max_unit` /
     `asr.repeat_min_coverage` —— 同一批热读键、默认值逐字相同（20 / 400 / 40 / 0.5）；
  3. `SAMPLE_RATE`（V1 从 `mnotes_core/config.py` import）→ 本模块的模块级常量（值同为 16000），
     好让 `_is_noise_segment` 的函数体逐字照搬；
  4. `_load_from_hub` 去掉 V1 的 `console=None` 形参与那两行 Rich 进度打印（`:293-294`）——
     V2 没有终端界面（`docs/v1-lineage.md` 的 F6：Rich 终端配色不搬）；
  5. 重依赖改为函数体内惰性 import（V1 是模块顶层 `import numpy as np` / `import torch`）；
  6. `transcribe_segment` 的形参注解去掉了 V1 的 `np.ndarray`：注解在
     `from __future__ import annotations` 下虽然不求值，留着它等于把一个未定义的名字写进签名。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from meeting import config as config_module
from meeting.asr.guards import _DeadlineStop, _LoopGuard, _strip_repeat_loops

#: 本项目自己的 logger（V1 那一行是 `logging.getLogger("meeting")`）
log = logging.getLogger("meeting.asr.glm")

# GLM-ASR 模型仓库 id。本地 HF cache 已存全量副本，加载走 _load_from_hub 的离线优先。
# 搬自 V1 mnotes_core/recorder.py:175
MODEL_ID = "zai-org/GLM-ASR-Nano-2512"

# 搬自 V1 mnotes_core/recorder.py:27-29
# no_repeat_ngram_size 是否可用：变体模型的自定义 generate 若不认该参数，
# 首次失败后置 False，不再每段都试一遍（避免每段都白跑一次生成）。
_NO_REPEAT_NGRAM_SUPPORTED = True

# 搬自 V1 mnotes_core/config.py:35（`SAMPLE_RATE = 16000`，V1 的 `recorder.py` 从那里 import）
# 采集与识别的采样率：16000 Hz / 单声道 / int16（`docs/v1-lineage.md` 的 B8）。
SAMPLE_RATE = 16000


# 搬自 V1 mnotes_core/recorder.py:32-44
@dataclass
class TranscribeResult:
    """单段转写结果（含失败原因与耗时，供流水线记日志与落盘占位符）。"""
    text: str = ""
    ok: bool = True
    reason: str = "ok"          # ok | timeout | loop | empty | noise
    duration: float = 0.0       # ASR 墙钟耗时（秒）
    tokens: int = 0             # 生成的 token 数
    timed_out: bool = False

    @property
    def failed(self) -> bool:
        return not self.ok


# 搬自 V1 mnotes_core/recorder.py:252-273
def _is_noise_segment(audio_int16) -> bool:
    """判断一段音频是否为近静音/环境噪音（无真实人声）。

    语音帧占比 < 0.15 且 能量(RMS) < 150 且 语音帧 < 5，三者 AND 才判噪音。
    真实语音几乎不可能同时满足三条，误杀率极低。

    **命中的语义是「这一段落是静音，不写行」**：V1 的调用点（`:512-514`）返回
    `ok=True, reason="noise"` —— 它算一次成功的「什么都没识别出来」，不是失败。
    """
    import webrtcvad
    import numpy as np
    if audio_int16 is None or len(audio_int16) < 480:
        return True  # 不足一帧（30ms @ 16kHz）
    vad = webrtcvad.Vad(1)
    frame_len = 480
    speech_frames = 0
    total_frames = 0
    for i in range(0, len(audio_int16) - frame_len + 1, frame_len):
        total_frames += 1
        if vad.is_speech(audio_int16[i:i + frame_len].tobytes(), SAMPLE_RATE):
            speech_frames += 1
    if total_frames == 0:
        return True
    ratio = speech_frames / total_frames
    rms = float(np.sqrt(np.mean(audio_int16.astype(np.float32) ** 2)))
    return ratio < 0.15 and rms < 150 and speech_frames < 5


# 搬自 V1 mnotes_core/recorder.py:276-295
def _load_from_hub(loader, **kwargs):
    """本地缓存优先加载模型/Processor，缓存缺文件时才回退联网下载。

    不能直接用 `loader.from_pretrained(MODEL_ID)`：`hf_hub_download` 在
    revision 是分支名（"main"）时**必先发 HEAD** 向 hub 要 commit metadata，
    本地缓存再完整也绕不过（`huggingface_hub/file_download.py` 里
    `REGEX_COMMIT_HASH.match(revision)` 不成立，就直接落到 metadata 请求）。
    网络一断，缓存里没有的文件——本仓库的 `chat_template.json` 就属于此类，
    上游只有 `.jinja` 版——会在重试耗尽后抛异常，整个加载失败，而本地其实够用。

    `local_files_only=True` 只查缓存、不发请求；缓存真缺文件时抛 OSError，
    此时才回退联网（新机器首次装机仍能自动下载）。
    """
    try:
        return loader.from_pretrained(MODEL_ID, local_files_only=True, **kwargs)
    except OSError as e:
        log.warning("本地缓存不完整（%s: %s），回退联网下载", type(e).__name__, e)
        return loader.from_pretrained(MODEL_ID, **kwargs)


# 搬自 V1 mnotes_core/recorder.py:321-341（`MeetingRecorder._load_transcriber` 的识别那一半）
def _load_models():
    """加载 Processor 与模型权重，返回 `(processor, transcriber)`。

    重依赖全在函数体内惰性 import。**这是本模块唯一的模型入口**，也是测试的接缝：
    用例用 `monkeypatch.setattr(meeting.asr.glm, "_load_models", fake)` 打在**同一份模块对象**上
    —— 别在别处写 `from meeting.asr.glm import _load_models` 那种绑到另一个模块对象的引用，
    那正是 V1 那个「测试一边通过一边什么都没验」的坑（`docs/v1-lineage.md` 的 C9 / E9）。

    处理器无额外 kwargs；模型 `dtype=torch.bfloat16, device_map="auto"`。
    """
    from transformers import GlmAsrForConditionalGeneration, AutoProcessor
    import torch

    log.info("loading GLM-ASR model...")
    # 加载耗时：第二场会（或 `release_gpu()` 之后重载）到底要等多久，今天只有这一条读数。
    # 形状与 `meeting/backends/transcribe.py` 的释放日志同源（两处都写秒）。
    started = time.monotonic()
    processor = _load_from_hub(AutoProcessor)
    transcriber = _load_from_hub(
        GlmAsrForConditionalGeneration,
        dtype=torch.bfloat16,
        device_map="auto",
    )
    log.info("GLM-ASR loaded on %s", transcriber.device)
    log.info("GLM-ASR load took %.2fs", time.monotonic() - started)
    return processor, transcriber


class GlmAsrTranscriber:
    """GLM-ASR 单段识别（识别那一半；采集与断句不在这里）。

    模型**惰性加载**：第一次 `transcribe_segment` 时才 `_load_models()`，
    没装模型依赖的机器上 `import meeting.asr.glm` 照样成立、也不会拖慢启动链。
    """

    def __init__(self) -> None:
        self.processor = None
        self.transcriber = None

    # 搬自 V1 mnotes_core/recorder.py:321-323（`_load_transcriber` 的入口三行）
    def _load_transcriber(self):
        if self.transcriber is not None:
            return
        self.processor, self.transcriber = _load_models()

    # 搬自 V1 mnotes_core/recorder.py:495-598（核心段 `:512-597`）
    def transcribe_segment(self, audio_int16) -> TranscribeResult:
        """转写一段音频，返回 TranscribeResult（含失败原因与耗时）。

        四层防护（任一层单独都不够，组合起来才能把退化循环的代价压到 <20 秒）：
          1. `no_repeat_ngram_size=8`   禁 8-gram 重复（tokenizer 是字符级 ≈ 禁 8 连字）
          2. `_LoopGuard`（logits_processor）每次采样检尾部窗口循环，命中即强制 EOS
          3. `_DeadlineStop`（StoppingCriteria）墙钟硬超时，超时即中止本次识别
          4. `_strip_repeat_loops` 文本级兜底剥离

        超时/判环时**不返回部分文本**——部分文本正是垃圾的来源；返回 ok=False 让
        上层写占位符，保证时间线不断且缺口可见。
        """
        import numpy as np
        import torch

        if self.transcriber is None:
            self._load_transcriber()

        if _is_noise_segment(audio_int16):
            log.info("dropped noise segment")
            return TranscribeResult(text="", ok=True, reason="noise")

        # 改自 V1 `:516-518` 的 `tune = load_asr_tunables()`：V1 每段重读 `config.json` 的
        # `asr` 段；V2 走 `meeting.config` 的同一批 `asr.*` 热读键（默认值逐字相同）。
        timeout_s = float(config_module.get("asr.timeout_seconds"))
        max_new = int(config_module.get("asr.max_new_tokens"))

        audio = audio_int16.squeeze().astype(np.float32) / 32768.0
        inputs = self.processor.apply_transcription_request(audio)
        inputs = inputs.to(self.transcriber.device, dtype=self.transcriber.dtype)

        pad_id = getattr(self.transcriber.generation_config, "eos_token_id", None)
        if isinstance(pad_id, list):
            pad_id = pad_id[0]

        from transformers import StoppingCriteriaList
        deadline = _DeadlineStop(timeout_s)
        # 超时同时挂两处（StoppingCriteria + logits_processor）：实测本模型
        # （无自定义 generate，走 GenerationMixin）两个钩子都是每 token 调用一次，
        # 双保险可防任一钩子在别的 transformers 版本里失效；再加总耗时兜底判定。
        guard = _LoopGuard(eos_token_id=pad_id, timeout_seconds=timeout_s)

        kwargs = dict(
            do_sample=False,
            max_new_tokens=max_new,
            pad_token_id=pad_id,
            stopping_criteria=StoppingCriteriaList([deadline]),
            logits_processor=[guard],
        )
        global _NO_REPEAT_NGRAM_SUPPORTED
        if _NO_REPEAT_NGRAM_SUPPORTED:
            kwargs["no_repeat_ngram_size"] = 8

        t0 = time.monotonic()
        outputs = None
        try:
            with torch.no_grad():
                outputs = self.transcriber.generate(**inputs, **kwargs)
        except (TypeError, ValueError) as e:
            # 变体模型的自定义 generate 可能不认 no_repeat_ngram_size：只回退这一次，
            # 之后不再尝试（否则每段都白跑一遍生成）
            if "no_repeat_ngram_size" in kwargs:
                log.warning("no_repeat_ngram_size 不被支持(%s)，本次会议不再使用", e)
                _NO_REPEAT_NGRAM_SUPPORTED = False
                kwargs.pop("no_repeat_ngram_size", None)
                with torch.no_grad():
                    outputs = self.transcriber.generate(**inputs, **kwargs)
            else:
                raise
        duration = time.monotonic() - t0

        if outputs is None:
            return TranscribeResult(text="", ok=False, reason="empty",
                                    duration=duration)

        n_new = int(outputs.shape[1] - inputs.input_ids.shape[1])
        text = self.processor.batch_decode(
            outputs[:, inputs.input_ids.shape[1]:],
            skip_special_tokens=True,
        )[0].strip()

        # 硬超时优先判定：掐掉的段一律判失败，绝不落盘半截垃圾。
        # 两道判据：StoppingCriteria 标记，或实际耗时已越过预算（兜底——即便
        # 某个版本的 generate 两个钩子都没生效，也不会把超时段当成成功）。
        over_budget = duration > timeout_s + 1.0
        if deadline.timed_out or guard.timed_out or over_budget:
            log.warning(
                "ASR timeout %.1fs (limit %.0fs, %d tokens, guard=%s deadline=%s): segment failed",
                duration, timeout_s, n_new, guard.timed_out, deadline.timed_out)
            return TranscribeResult(text="", ok=False, reason="timeout",
                                    duration=duration, tokens=n_new, timed_out=True)

        if guard.detected:
            log.warning("ASR loop detected at token %s (%.1fs, %d chars): segment failed",
                        guard.detected_at, duration, len(text))
            return TranscribeResult(text="", ok=False, reason="loop",
                                    duration=duration, tokens=n_new)

        # 改自 V1 `:591-592` 的 `tune["repeat_max_unit"]` / `tune["repeat_min_coverage"]`，
        # 理由同上（同一批热读键）。
        stripped = _strip_repeat_loops(
            text,
            max_unit=config_module.get("asr.repeat_max_unit"),
            min_coverage=config_module.get("asr.repeat_min_coverage"),
        )
        if len(text) > 0 and len(stripped) < len(text) * 0.5:
            # 文本级兜底命中：留下前导正文+一组单位，标记但不判失败（信息仍有价值）
            log.warning("repeat loop stripped by text filter: %d -> %d chars",
                        len(text), len(stripped))
        return TranscribeResult(text=stripped, ok=True, reason="ok",
                                duration=duration, tokens=n_new)
