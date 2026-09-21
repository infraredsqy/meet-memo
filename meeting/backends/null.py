"""`null` 替身（phase1 §3.10，方案 §7.4 的一半）。

**显式配置时的采集替身，保证服务不假装能录**：
  - 只有把 `capture.backend` 配成 `null`（或解析失败回退）才会解析到这里；
    默认取值 `auto` 解析到真采集后端（`meeting.backends.capture:CaptureBackend`）。
    替身这一档下服务照常起来、界面照常发，`开始录音` 给出「采集后端未接入」
    的明确说明 —— 不假装能录。
  - 本文件**不许**为了测试方便长出 `dev.py` 的合成逻辑（合成电平与故障注入归 phase2 的
    `backends/dev.py`；`capture.backend = dev` 在 phase1 走「导入失败 → 回退 null + warning」）。
"""

from __future__ import annotations

from typing import Any, Callable

from meeting.backends.base import (
    DEFAULT_FRAME_INTERVAL_SECONDS,
    SOURCE_IDS,
    SourceProbe,
    TranscriptLine,
)

#: 两路的人话标签（`mic` = 本地、`loopback` = 远端）
_SOURCE_LABELS = {"mic": "本地", "loopback": "远端"}

#: 起采集时的失败原因（一句话给界面看）
CAPTURE_UNAVAILABLE_REASON = "采集后端未接入：当前是 null 替身，不会真的录音"
CAPTURE_UNAVAILABLE_CODE = "capture_unavailable"


class CaptureUnavailable(RuntimeError):
    """采集后端接不上时抛出的错误（`code` 供界面 / 动作端点转成错误信封）。"""

    code = CAPTURE_UNAVAILABLE_CODE

    def __init__(self, message: str = CAPTURE_UNAVAILABLE_REASON) -> None:
        self.message = message
        super().__init__(message)


class NullCaptureBackend:
    """显式配 `null` 时的采集替身：**两路都 `unprobed`**，`start` 抛 `capture_unavailable`。

    `finalize()` / `transcode()` / `stop()` 是安全空实现。
    """

    def __init__(self) -> None:
        # 帧间隔的声明位（协议默认 0.5s）
        self.frame_interval = DEFAULT_FRAME_INTERVAL_SECONDS
        self._emit: Callable[[Any], None] | None = None
        self._config: Any = None

    def bind(self, emit: Callable[[Any], None], *, config: Any = None) -> None:
        """记下事实出口与只读配置视图（runtime 在 `probe()` 之前调一次）。"""
        self._emit = emit
        self._config = config

    def probe(self) -> list[SourceProbe]:
        """如实报「未探测」：两路各一条 `unprobed`，`device_name` 为 `None`。"""
        return [
            SourceProbe(
                source=source,
                status="unprobed",
                device_name=None,
                detail=f"{_SOURCE_LABELS[source]}这一路还没接采集后端，未探测",
            )
            for source in SOURCE_IDS
        ]

    def start(self, mode: str, session_dir: str) -> None:
        """起采集 —— null 替身如实抛错，不假装能录。"""
        raise CaptureUnavailable()

    def finalize(self) -> None:
        """安全空实现。"""

    def transcode(self) -> None:
        """安全空实现。"""

    def stop(self) -> None:
        """安全空实现。"""


class NullTranscriberBackend:
    """缺席时的转写默认实现：恒 `normal` + 0 行 + `backlog=0` + `segment_timeout=20.0`。

    起停是安全空实现（**起步失败不阻塞录音**：转写坏了可以重跑，R1 的立场）。
    """

    def __init__(self) -> None:
        self._line_sink: Callable[[TranscriptLine], None] | None = None
        self._config: Any = None

    def bind(self, line_sink: Callable[[TranscriptLine], None], *, config: Any = None) -> None:
        self._line_sink = line_sink
        self._config = config

    def start(self, mode: str, session_dir: str) -> None:
        """安全空实现（没有转写后端，也就没有可起的转写）。"""

    def stop(self) -> None:
        """安全空实现。"""

    def health(self) -> str:
        return "normal"

    def backlog_depth(self) -> int:
        return 0

    def segment_timeout(self) -> float:
        return 20.0


__all__ = [
    "CAPTURE_UNAVAILABLE_CODE",
    "CAPTURE_UNAVAILABLE_REASON",
    "CaptureUnavailable",
    "NullCaptureBackend",
    "NullTranscriberBackend",
]
