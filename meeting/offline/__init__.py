"""会后离线转写与说话人聚类（`7c6952e5` 的引擎内核与产物）。

六个模块各管一段：

  - `meeting.offline.audio`：把任意容器解成 16k 单声道 float32 波形（PyAV）；
  - `meeting.offline.segment`：整段 PCM 按 30ms 帧 / 静默 2.0s / 段长上限切段
    （搬自 V1 `mnotes_core/replay.py:28-69`）；
  - `meeting.offline.engine`：MOSSTD 引擎封装与 `[start][Sxx]text[end]` 解析；
  - `meeting.offline.speakers`：每路各自编号（`S01`、`S02`…）与汇总；
  - `meeting.offline.artifacts`：三份产物 + 状态文件的写入与字段契约；
  - `meeting.offline.runner`：把上面这些串成 `run_session(session_dir)`。

**本包不 import 任何重依赖**：`torch` / `transformers` / `av` / `webrtcvad` 一律在函数体内
惰性 import（照 `meeting/asr/glm.py` 的既有纪律）。包级 import 只该拉进标准库与包内模块，
否则「没装模型依赖的机器上 import 本包」这件事会直接失败。这条由
`tests/test_offline_engine.py` 与 `tests/test_post_hook.py` 在**全新解释器**里盯着。
"""
