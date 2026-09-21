"""采集层（`dca4539b`）—— 设备选择与真实适配。

分工（分开放，`devices.py` 因此能被单测、也能在没有声卡库的机器上 import）：

    devices.py —— 设备模型与挑选策略：**纯逻辑，不 import 声卡库**
    sources.py —— 真实适配层：`sounddevice` / `soundcard`，**函数内惰性 import**
    levels.py —— 电平换算纯函数：一块 int16 样本 → `(dbfs, peak, bar)`
    archive.py —— PCM 归档写侧（自己的写线程 + 收尾 `fsync`）与 PCM→MP3 转码
    engine.py —— 会话采集引擎：每路「读线程 + 写线程 + 0.5 秒电平刻度」，收尾编排的落点

约定导入路径 `meeting.backends.capture:CaptureBackend` 见
`meeting/backends/base.py` 与 `meeting/backends/registry.py`。
"""

from __future__ import annotations

__all__: list[str] = []
