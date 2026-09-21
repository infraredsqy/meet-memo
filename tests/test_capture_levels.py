"""`tests/test_capture_levels.py` —— 电平换算（`meeting/capture/levels.py`）。

**纯函数、无硬件、无文件**：这一段验的是「三个数怎么算出来的」，口径与
`meeting/state.py` 的 `push_level()`、`meeting/backends/dev.py` 的 `_emit_frame()` 对得上：

  - 三个数都是 dBFS（负数）；
  - `dbfs` 用这一帧窗口的均方根、`peak` 用窗口内绝对值最大样本、`bar = max(dbfs, peak)`；
  - 静音窗口落到 `alert.silence_dbfs`（默认 `-60`）以下，写 `-120.0` 兜底。
"""

from __future__ import annotations

import numpy as np

from meeting.capture.levels import (
    FULL_SCALE,
    SILENCE_DBFS,
    frame_level,
    peak_dbfs,
    rms_dbfs,
    to_dbfs,
)

#: 服务侧静默告警的默认阈值（`alert.silence_dbfs`）——静音窗口必须落到它下面
DEFAULT_SILENCE_DBFS = -60.0


def sine(amplitude: float, count: int = 16000) -> np.ndarray:
    """一段 int16 正弦（`amplitude` 是峰值；用来验「均方根比峰值低约 3 dB」）。"""
    t = np.arange(count, dtype=np.float64)
    return (amplitude * np.sin(2.0 * np.pi * 440.0 * t / 16000.0)).astype(np.int16)


# ---------------------------------------------------------------------------
# 单个数：to_dbfs()
# ---------------------------------------------------------------------------


def test_to_dbfs_anchors_at_full_scale():
    """满量程是 0 dBFS 的参考点：`FULL_SCALE` 处算出来正好是 0。"""
    assert to_dbfs(FULL_SCALE) == 0.0
    assert abs(to_dbfs(32767)) < 0.001


def test_to_dbfs_is_negative_and_monotonic():
    """幅度越小读数越低（都是负数）：每差 10 倍就是 20 dB。"""
    assert to_dbfs(3276.8) == -20.0
    assert to_dbfs(327.68) == -40.0
    assert to_dbfs(100.0) > to_dbfs(10.0)


def test_to_dbfs_never_returns_negative_infinity():
    """0（或负的幅度）落到 `SILENCE_DBFS`：不许出现 `-inf`（那是 JSON 装不下的东西）。"""
    assert to_dbfs(0) == SILENCE_DBFS
    assert to_dbfs(-0.0) == SILENCE_DBFS
    assert np.isfinite(to_dbfs(0))


# ---------------------------------------------------------------------------
# 一块样本：rms_dbfs() / peak_dbfs()
# ---------------------------------------------------------------------------


def test_silent_window_sits_below_the_alert_threshold():
    """全零窗口必须落到 `alert.silence_dbfs` 下面（`-120` 兜底）。"""
    zeros = np.zeros(1600, dtype=np.int16)
    assert rms_dbfs(zeros) == SILENCE_DBFS
    assert peak_dbfs(zeros) == SILENCE_DBFS
    assert rms_dbfs(zeros) < DEFAULT_SILENCE_DBFS
    assert peak_dbfs(zeros) < DEFAULT_SILENCE_DBFS


def test_empty_window_is_silence_not_an_error():
    """空窗口（这一拍还没有素材）按静音算，不抛错。"""
    empty = np.empty(0, dtype=np.int16)
    assert rms_dbfs(empty) == SILENCE_DBFS
    assert peak_dbfs(empty) == SILENCE_DBFS
    assert frame_level(empty) == (SILENCE_DBFS, SILENCE_DBFS, SILENCE_DBFS)


def test_rms_is_about_three_db_below_peak_for_a_sine():
    """正弦的均方根比峰值低约 3 dB（`−3.01 dB` 是 `1/√2` 的读数）。

    窗口取整数个周期（4400 个样本 = 121 个 440 Hz 周期）：窗口里留半截波形时，
    均方根与峰值之比就不再是 `1/√2`，读数会偏离 3 dB（实测偏 1.3 dB）—— 这是窗口边界
    落在哪里的事，换算本身没有错。
    """
    wave = sine(20000.0, count=4400)
    rms_value = rms_dbfs(wave)
    peak_value = peak_dbfs(wave)
    # 峰值读数比均方根读数高约 3 dB（两个都是负数，差取正）
    assert 2.7 < (peak_value - rms_value) < 3.3


def test_peak_uses_the_largest_absolute_sample():
    """峰值取的是**绝对值最大**的样本：一个负的大样本照样算数。"""
    samples = np.array([1, 2, -30000, 4, 5], dtype=np.int16)
    assert peak_dbfs(samples) == to_dbfs(30000)


def test_reading_grows_with_the_signal():
    """电平原样跟着幅度走（quiet < loud）。"""
    quiet = rms_dbfs(sine(500.0))
    loud = rms_dbfs(sine(20000.0))
    assert quiet < loud
    assert loud > DEFAULT_SILENCE_DBFS


def test_a_constant_window_makes_rms_equal_peak():
    """常值窗口的均方根与峰值是同一个数（`x` 的 RMS 就是 `|x|`）。"""
    constant = np.full(1600, 3276, dtype=np.int16)
    assert abs(rms_dbfs(constant) - peak_dbfs(constant)) < 0.01


# ---------------------------------------------------------------------------
# 一帧：frame_level()
# ---------------------------------------------------------------------------


def test_frame_level_returns_rms_peak_and_their_maximum():
    """三件事：`dbfs` 是均方根、`peak` 是峰值、`bar` 取两者的大者。"""
    wave = sine(20000.0)
    dbfs, peak, bar = frame_level(wave)
    assert dbfs == rms_dbfs(wave)
    assert peak == peak_dbfs(wave)
    assert bar == max(dbfs, peak)


def test_frame_level_bar_never_sits_below_either_reading():
    """`bar` 永远是这一帧里最显眼的那个数（服务侧一根柱取的就是它）。"""
    for samples in (np.zeros(8, dtype=np.int16), sine(300.0, 64), sine(30000.0, 64)):
        dbfs, peak, bar = frame_level(samples)
        assert bar >= dbfs
        assert bar >= peak


def test_frame_level_needs_no_writable_array():
    """只读数组照样算（电平这一路不改动样本）。"""
    wave = sine(1000.0, 32)
    wave.setflags(write=False)
    dbfs, peak, _bar = frame_level(wave)
    assert np.isfinite(dbfs) and np.isfinite(peak)


def test_all_three_readings_are_finite_numbers():
    """三个数都得是有限值（JSON 装得下、告警判据比得了）。"""
    for samples in (
        np.zeros(4, dtype=np.int16),
        np.array([-32768, 32767], dtype=np.int16),
        sine(12345.0, 128),
    ):
        for value in frame_level(samples):
            assert isinstance(value, float)
            assert np.isfinite(value)
            assert value <= 0.0
