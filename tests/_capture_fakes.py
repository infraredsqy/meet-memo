"""`tests/_capture_fakes.py` —— capture 测试层的共用假件与归档核对工具（**pytest 不收集本文件**）。

定位与挂件方式照 `tests/_drain_fakes.py`（跨文件共用假件的唯一落点）：`FakeCaptureSource`
（含 `block()` 与块长常量）是引擎层共用的假采集源，按计划给块、可指定在第几块抛错；
`audit_pcm()` 一族是归档的纯函数核对（逐帧电平重放 / 逐样本计数 / 逐字节比对 / 墙钟对齐），
端到端与反例用例共用同一组。原先分别住在 `test_capture_engine.py` 与
`test_capture_accounting.py`、被各用例文件互相 import 的正本搬到这里，用法一律
`from tests._capture_fakes import ...`。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np

from meeting.capture.levels import frame_level


#: 一块 0.1 秒的 16k / 单声道 / int16 样本（3200 字节；一秒 32000 字节的由来）
CHUNK_FRAMES = 1600
CHUNK_BYTES = CHUNK_FRAMES * 2


def block(value: int = 1200, frames: int = CHUNK_FRAMES) -> bytes:
    """一块可预期的 int16 裸 PCM（每个样本都是 `value`）。"""
    return np.full(frames, value, dtype=np.int16).tobytes()


class FakeCaptureSource:
    """假采集源：按计划给块、可指定在第几块抛错、可指定给多少块（喂完就空转）。

    `read()` 的三种回法都对得上 `CaptureSource` 协议：给块、给空（这一拍没有新块）、
    抛错（这一路出事）。

    `start()` 与真源同一档：`auto_start=True`（后端那边造出来的源）时由调用方显式起，
    `auto_start=False`（引擎自己起的那些用例）时 `start()` 什么也不做 —— 引擎的读线程
    本来就不等源起没起，假源因此两种用法都顺手。
    """

    def __init__(
        self,
        name: str,
        *,
        chunk: bytes | None = None,
        chunks: int | None = None,
        error_at: int | None = None,
        error: BaseException | None = None,
        poll_seconds: float = 0.005,
        pace_seconds: float = 0.0,
        auto_start: bool = True,
    ) -> None:
        self.name = name
        self.chunk = block() if chunk is None else chunk
        self.chunks = chunks
        self.error_at = error_at
        self.error = error or RuntimeError(f"假源 {name} 按计划炸了")
        self.poll_seconds = poll_seconds
        #: 交一块之后歇多久（秒）。0 = 能多快给多快（引擎自己的用例要的就是这个）；
        #: 端到端那些用例把它设成 0.1，与真采集一个块 0.1 秒的节奏对齐 —— 否则假源会以
        #: 每秒几百兆的速度把盘写满，收尾的 join 也就不是在验收尾了。
        self.pace_seconds = pace_seconds
        self.auto_start = auto_start
        self.fed = 0
        self.reads = 0
        self.started = False
        self.stopped = False
        self._stop = threading.Event()
        self._latest = np.empty(0, dtype=np.int16)

    # -- CaptureSource 协议 ------------------------------------------------

    def start(self) -> None:
        """起这一路的采集（后端会在造完源之后调它；引擎自己起的那些用例不调）。"""
        self.started = True
        if not self.auto_start:
            return

    def read(self, timeout: float) -> bytes | None:
        self.reads += 1
        if self._stop.is_set():
            return None
        if self.error_at is not None and self.fed >= self.error_at:
            raise self.error
        if self.chunks is not None and self.fed >= self.chunks:
            time.sleep(min(timeout, self.poll_seconds))
            return b""
        self.fed += 1
        samples = np.frombuffer(self.chunk, dtype=np.int16)
        self._latest = samples
        if self.pace_seconds:
            time.sleep(self.pace_seconds)
        return self.chunk

    def latest_samples(self) -> np.ndarray:
        return self._latest

    def stop(self) -> None:
        self.stopped = True
        self._stop.set()

    # -- 给用例看的读数 ----------------------------------------------------

    @property
    def bytes_fed(self) -> int:
        return self.fed * len(self.chunk)


#: 归档的 PCM 形状（16k / 单声道 / int16 小端裸 PCM；与 `meeting/capture/archive.py` 一致）
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2

#: 电平重放的帧长（秒）—— 与采集侧 0.5 秒一帧的刻度同拍
DEFAULT_FRAME_INTERVAL_SECONDS = 0.5

#: 「这一帧算静音」的 dBFS 分界（取产品告警阈值 `alert.silence_dbfs` 的默认值；
#: 电平重放沿用产品的语义：低于它 = 静音帧）
SILENCE_DBFS_THRESHOLD = -60.0

#: 一个采集块的样本数（0.1 秒 @16k；「偏差在一个采集块以内」指的就是它）
BLOCK_SAMPLES = 1600


# ---------------------------------------------------------------------------
# 核对函数（纯函数；端到端文件从这里 import 同一组）
# ---------------------------------------------------------------------------


@dataclass
class FrameReading:
    """一帧重放出来的电平（照采集时刻的口径：`(dbfs, peak, bar)` 三件套 + 帧序号）。"""

    index: int
    dbfs: float
    peak: float
    bar: float


@dataclass
class PcmAudit:
    """一份归档 PCM 的核对结论（逐项都可以直接当断言用）。"""

    #: 字节面
    total_bytes: int
    even_bytes: bool
    trailing_bytes: int  # 非 0 = 挂着一个成不了样本的残字节（如实报，不悄悄丢）
    #: 样本面
    samples: int
    seconds: float
    #: 帧面（0.5 秒一帧；尾部不足一帧的样本记 leftover，**不成帧、也不算丢帧**）
    frame_interval_seconds: float
    frame_samples: int
    frames: int
    leftover_samples: int
    frame_indices: list[int] = field(default_factory=list)
    missing_frame_indices: list[int] = field(default_factory=list)
    #: 电平重放（产品的静音语义：dbfs ≤ 阈值算静音帧）
    voiced_frames: int = 0
    silent_frames: int = 0
    #: 与期望侧的核对（没给期望侧就是 None：这两栏「没验」与「验过全对」要分得开）
    expected_samples: int | None = None
    sample_gap: int | None = None  # 样本数 − 期望样本数（负 = 少了样本）
    byte_match: bool | None = None
    first_mismatch_sample: int | None = None  # 第一处分歧落在第几个样本上
    mismatched_bytes: int | None = None


def replay_level_frames(
    data: bytes | bytearray,
    *,
    frame_interval_seconds: float = DEFAULT_FRAME_INTERVAL_SECONDS,
) -> list[FrameReading]:
    """把归档 PCM 读回来，按帧长切片、逐帧重跑电平换算。

    帧序号从 0 连续编号；最后不足一帧的尾数留给自己（`audit_pcm` 的
    `leftover_samples` 报它的数量），**不硬凑成一帧**。奇数字节的残尾同样不参与
    （它成不了 int16 样本，`audit_pcm` 会把这件事报出来）。
    """
    payload = bytes(data)
    trailing = len(payload) % BYTES_PER_SAMPLE
    usable = payload[: len(payload) - trailing]
    samples = np.frombuffer(usable, dtype=np.int16)
    frame_samples = round(frame_interval_seconds * SAMPLE_RATE)
    readings: list[FrameReading] = []
    for index in range(len(samples) // frame_samples):
        window = samples[index * frame_samples : (index + 1) * frame_samples]
        dbfs, peak, bar = frame_level(window)
        readings.append(FrameReading(index=index, dbfs=dbfs, peak=peak, bar=bar))
    return readings


def audit_pcm(
    data: bytes | bytearray,
    *,
    expected_bytes: bytes | bytearray | None = None,
    expected_samples: int | None = None,
    frame_interval_seconds: float = DEFAULT_FRAME_INTERVAL_SECONDS,
    silence_dbfs_threshold: float = SILENCE_DBFS_THRESHOLD,
) -> PcmAudit:
    """对一份归档 PCM 做完上面那四层核对，交回一份 `PcmAudit`。"""
    payload = bytes(data)
    trailing_bytes = len(payload) % BYTES_PER_SAMPLE
    usable = payload[: len(payload) - trailing_bytes]
    samples_array = np.frombuffer(usable, dtype=np.int16)
    frame_samples = round(frame_interval_seconds * SAMPLE_RATE)
    frames = int(len(samples_array) // frame_samples)
    leftover = int(len(samples_array) - frames * frame_samples)

    readings = replay_level_frames(
        usable, frame_interval_seconds=frame_interval_seconds
    )
    frame_indices = [reading.index for reading in readings]
    voiced = sum(
        1 for reading in readings if reading.dbfs > silence_dbfs_threshold
    )

    sample_gap: int | None = None
    if expected_samples is not None:
        sample_gap = len(samples_array) - int(expected_samples)

    byte_match: bool | None = None
    first_mismatch_sample: int | None = None
    mismatched_bytes: int | None = None
    if expected_bytes is not None:
        expected = bytes(expected_bytes)
        byte_match = payload == expected
        if byte_match:
            mismatched_bytes = 0
        else:
            common = min(len(payload), len(expected))
            diffs = sum(
                1 for index in range(common) if payload[index] != expected[index]
            )
            # 长度差本身也是「不一致」的一部分（多出来的字节没有对应的期望侧）
            mismatched_bytes = diffs + abs(len(payload) - len(expected))
            diverge = common  # 一方先结束：分歧点就在公共段的尽头
            for index in range(common):
                if payload[index] != expected[index]:
                    diverge = index
                    break
            first_mismatch_sample = diverge // BYTES_PER_SAMPLE

    # 期望样本数在场时，「本该有而没有」的帧序号如实列出（帧序号 = 样本序号 // 帧长）
    missing: list[int] = []
    if expected_samples is not None:
        expected_frames = int(expected_samples) // frame_samples
        present = set(frame_indices)
        missing = [index for index in range(expected_frames) if index not in present]

    return PcmAudit(
        total_bytes=len(payload),
        even_bytes=trailing_bytes == 0,
        trailing_bytes=trailing_bytes,
        samples=int(len(samples_array)),
        seconds=len(samples_array) / SAMPLE_RATE,
        frame_interval_seconds=frame_interval_seconds,
        frame_samples=frame_samples,
        frames=frames,
        leftover_samples=leftover,
        frame_indices=frame_indices,
        missing_frame_indices=missing,
        voiced_frames=voiced,
        silent_frames=len(readings) - voiced,
        expected_samples=expected_samples,
        sample_gap=sample_gap,
        byte_match=byte_match,
        first_mismatch_sample=first_mismatch_sample,
        mismatched_bytes=mismatched_bytes,
    )


def samples_within_one_block(
    samples: int,
    wall_seconds: float,
    *,
    block_samples: int = BLOCK_SAMPLES,
    sample_rate: int = SAMPLE_RATE,
) -> bool:
    """「样本数」与「墙钟秒数 × 采样率」的偏差是否在一个采集块以内。

    采集按块落盘（一块 0.1 秒 = 1600 样本），墙钟是连续量：两者的差被一个块宽吸收
    （任务书 §3 的口径）。边界值算在内（恰好差一个块 = 通过）。
    """
    return abs(int(samples) - float(wall_seconds) * sample_rate) <= block_samples
