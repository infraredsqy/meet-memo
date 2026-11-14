"""ASR 核心 —— 四层防护 + 噪声预筛 + GLM 单段识别。

`guards.py` 是判定内核，**零重依赖**；`glm.py` 是模型层，重依赖（torch / transformers /
numpy / webrtcvad）一律在**函数体内**惰性 import，因此 `import meeting.asr.glm` 不会把
任何一条重依赖拖进 `sys.modules`。

本包只交付「一串 int16 样本进去，一段文本或一个失败原因出来」：**不建队列、不起线程、不写文件、
不碰服务**。
"""

from __future__ import annotations

__all__: list[str] = []
