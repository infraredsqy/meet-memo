"""电平换算（纯函数）—— 采集侧交给服务的「原始电平事实」（`dca4539b` phase2 §3）。

本模块**不 import 声卡库、不起线程、不读配置**：给它一块 int16 样本，它交回三个 dBFS 数。
采集引擎每 0.5 秒调一次，服务侧的 `push_level()` / `alerts.py` 只认交出去的那三个数。

口径（`meeting/state.py` 的 `push_level()` 与 `meeting/backends/dev.py` 已经定死，三处一致）：

  - 三个数**都是 dBFS**（负数）；
  - `dbfs` = 这一帧窗口的**均方根**；
  - `peak` = 窗口内**绝对值最大样本**；
  - `bar = max(dbfs, peak)`（服务侧一根柱取的就是 `peak`，这里的 `bar` 与它同源）；
  - 静音窗口要落到 `alert.silence_dbfs`（默认 `-60`）以下，写 `SILENCE_DBFS`（`-120.0`）兜底。

**为什么是「自己的钟」**：帧时间戳由采集侧给（`time.monotonic()`），`alerts.py` 拿它做差累计
静默秒数，不拿墙钟做差 —— 慢机器上因此不虚报。本模块只出电平，时间戳由调用方带上。

**这一块是新写的**：V1 里没有电平事实（它的 TUI 直接看队列深度与 VAD），所以本模块没有
对照源，也不该有「搬自 V1」的出处注释。
"""

from __future__ import annotations

import numpy as np

#: 全零 / 空窗口兜底的 dBFS（静音窗口必须落到 `alert.silence_dbfs` 默认值 `-60` 以下）
SILENCE_DBFS = -120.0

#: int16 的满量程（dBFS 的 0 dB 参考；样本 `-32768` 正好落到 `SILENCE_DBFS`）
FULL_SCALE = 32768.0

#: 均方根的下限（只为挡住 `log10(0)`；它在 dBFS 上远低于 `SILENCE_DBFS`，不影响结果）
_RMS_FLOOR = 1e-12


def to_dbfs(amplitude: float) -> float:
    """把 int16 量级的幅度折算成 dBFS（`0` → `SILENCE_DBFS`，不抛错、不返回 `-inf`）。"""
    value = abs(float(amplitude))
    if value <= 0.0:
        return SILENCE_DBFS
    return max(SILENCE_DBFS, 20.0 * float(np.log10(value / FULL_SCALE)))


def rms_dbfs(samples: np.ndarray) -> float:
    """一块 int16 样本的**均方根** dBFS（空块给 `SILENCE_DBFS`）。"""
    values = np.asarray(samples, dtype=np.float64).reshape(-1)
    if values.size == 0:
        return SILENCE_DBFS
    mean_square = float(np.mean(np.square(values)))
    if mean_square <= 0.0:
        return SILENCE_DBFS
    return max(SILENCE_DBFS, 20.0 * float(np.log10(max(mean_square, _RMS_FLOOR) ** 0.5 / FULL_SCALE)))


def peak_dbfs(samples: np.ndarray) -> float:
    """一块 int16 样本里**绝对值最大样本**的 dBFS（空块给 `SILENCE_DBFS`）。"""
    values = np.asarray(samples, dtype=np.float64).reshape(-1)
    if values.size == 0:
        return SILENCE_DBFS
    return to_dbfs(float(np.max(np.abs(values))))


def frame_level(samples: np.ndarray) -> tuple[float, float, float]:
    """一个电平窗口 → `(dbfs, peak, bar)`（三处口径的唯一落点）。

    `bar = max(dbfs, peak)`：`state.py` 的一根柱取的是这一帧的 `peak`，而 `bar` 是它的
    同源读数，两者因此永远一致。
    """
    rms_value = rms_dbfs(samples)
    peak_value = peak_dbfs(samples)
    return rms_value, peak_value, max(rms_value, peak_value)


__all__ = [
    "FULL_SCALE",
    "SILENCE_DBFS",
    "frame_level",
    "peak_dbfs",
    "rms_dbfs",
    "to_dbfs",
]
