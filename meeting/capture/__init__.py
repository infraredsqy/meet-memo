"""采集层（`dca4539b`）—— 设备选择与真实适配。

分工（两件事分开放，`devices.py` 因此能被单测、也能在没有声卡库的机器上 import）：

    devices.py —— 设备模型与挑选策略：**纯逻辑，不 import 声卡库**
    sources.py —— 真实适配层：`sounddevice` / `soundcard`，**函数内惰性 import**

采集引擎（两路读线程 / 写线程 / 0.5 秒电平帧 / PCM 归档与转码）是下一个执行单元的活，
本包这一段只交设备层。约定导入路径 `meeting.backends.capture:CaptureBackend` 见
`meeting/backends/base.py` 与 `meeting/backends/registry.py`。
"""

from __future__ import annotations

__all__: list[str] = []
