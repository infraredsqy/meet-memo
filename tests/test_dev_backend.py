"""dev 采集替身。

**非产品路径**：dev 替身默认关闭，只有把 `capture.backend` 配成 `dev`（或 `MEETING_CAPTURE=dev`）
才会被解析到。它提供确定性合成电平（种子 = 模块级常量 `DEV_LEVEL_SEED`）与三档可注入故障。

三档故障（`silence` / `stall` / `lost`）的窗口语义、`after_seconds` 到点生效 / 恢复、
对同一路替换注入的时序行为没有直测。
"""

from __future__ import annotations

import time

import pytest

from tests import _helpers as helpers
from meeting.backends.base import LevelFrame
from meeting.backends.dev import (
    DEV_FAULT_DEFAULT_SECONDS,
    DEV_FAULT_KINDS,
    DEV_LEVEL_SEED,
    DevCaptureBackend,
)

pytestmark = pytest.mark.slow


def test_two_instances_produce_the_same_level_sequence():
    """同种子同帧序：两个实例前若干帧的电平序列**逐帧相同**。"""
    first = DevCaptureBackend()
    second = DevCaptureBackend()
    for source in ("mic", "loopback"):
        sequence_a = first.level_sequence(source, 12)
        sequence_b = second.level_sequence(source, 12)
        assert sequence_a == sequence_b
        assert len(sequence_a) == 12
    # 两路各自的序列不同（同一路才要求相同）
    assert first.level_sequence("mic", 5) != first.level_sequence("loopback", 5)


def test_finalize_only_keeps_books(tmp_path):
    """**不写真实音频**：`finalize()` 只登记，盘上一份文件都不落地。

    「只登记」的判据落在两处：调用计数加一；把**真实存在的场次目录**交给替身之后，
    这一口子跑完目录里依旧空无一物（`iterdir()` 一个条目都没有）。
    （替身不再有 `transcode()`：转码归 `meeting/capture/transcode.py` 的独立工序，
    替身没有真实 PCM 可转。）
    """
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    backend = DevCaptureBackend()
    backend.start("both", str(session_dir))
    backend.stop()
    backend.finalize()
    assert backend.finalize_calls == 1
    assert backend._session_dir == str(session_dir)  # noqa: SLF001 - 替身只把它记着，不拿它落盘
    assert list(session_dir.iterdir()) == [], [p.name for p in session_dir.iterdir()]


def test_inject_validates_source_and_fault():
    """非法 `source` / `fault` / `duration_seconds` 一律抛 `ValueError`（端点转 400）。"""
    backend = DevCaptureBackend()
    with pytest.raises(ValueError):
        backend.inject_fault(source="keyboard", fault="silence")
    with pytest.raises(ValueError):
        backend.inject_fault(source="mic", fault="explode")
    with pytest.raises(ValueError):
        backend.inject_fault(source="mic", fault="silence", duration_seconds=0)
    with pytest.raises(ValueError):
        backend.inject_fault(source="mic", fault="silence", after_seconds=-1)
    assert DEV_FAULT_KINDS == ("silence", "stall", "lost")
    assert DEV_FAULT_DEFAULT_SECONDS == 30.0


