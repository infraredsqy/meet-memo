"""ASR 核心（设计）—— 四层防护 + 噪声预筛 + GLM 单段识别。

分工（与 `meeting/capture/` 同一套口径：纯逻辑与重依赖分开住，各自能被单测）：

    guards.py —— 判定内核：token 级判环 `_LoopGuard` / 墙钟超时 `_DeadlineStop` /
                 文本级剥离 `_strip_repeat_loops` + `_loop_coverage`。**零重依赖**
                 （纯标准库 + duck-typed 张量），模块顶层一行 numpy / torch 都没有。
    glm.py    —— 模型层：`GlmAsrTranscriber`（惰性加载 + `transcribe_segment`）、
                 `TranscribeResult`、`_is_noise_segment`、`_load_from_hub`。重依赖
                 （torch / transformers / numpy / webrtcvad）一律在**函数体内**惰性 import，
                 因此 `import meeting.asr.glm` 不会把任何一条重依赖拖进 `sys.modules`。

四层防护的顺序与各自的层（`docs/v1-lineage.md` 的 A1 / A2 / A3）：

    1. `no_repeat_ngram_size=8`（`glm.py`，字符级 tokenizer 下等价于禁 8 连字，首次失败自动降级）
    2. `_LoopGuard`（`guards.py`，挂 `logits_processor`，命中即压 EOS）
    3. `_DeadlineStop`（`guards.py`，挂 `StoppingCriteria`）+ 总耗时兜底，三判据取或
    4. `_strip_repeat_loops`（`guards.py`，文本级兜底）

参数一律从 `meeting.config` 的 `asr.*` 五个热读键读出（`asr.timeout_seconds` /
`asr.max_new_tokens` / `asr.repeat_max_unit` / `asr.repeat_min_coverage`；`asr.max_segment_seconds`
是设计的 VAD 用的），本包**不自己存参数、也不碰 `config.json`**。

本包只交付「一串 int16 样本进去，一段文本或一个失败原因出来」：**不建队列、不起线程、不写文件、
不碰服务**（那是设计的 `meeting/asr/pipeline.py` 与 `meeting/backends/transcribe.py`）。
"""

from __future__ import annotations

#: 本包不在这里 re-export 任何名字：下游一律按模块取用
#: （`from meeting.asr.glm import GlmAsrTranscriber` / `from meeting.asr.guards import _strip_repeat_loops`），
#: 空 `__all__` 与隔壁 `meeting/capture/__init__.py` 同一条口径 —— 免得 `import meeting.asr`
#: 顺手把整包拉进来，也让「谁依赖谁」在 import 语句上看得见。
__all__: list[str] = []
