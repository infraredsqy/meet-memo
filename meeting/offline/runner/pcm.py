"""归档 PCM 的字节级读取与换算、数字静音门。

四件事住这里：

- **满量程换算**（`full_scale_counts()` / `samples_to_count_floats()`）：归一化波形
  （`[-1, 1]`）与 int16 计数放上同一条**浮点、不截断**的尺子，int16 与 float32 落同一条
  分界——直接 `int()` 会把真有声的 float 波形截成全零；
- **PCM 字节读取**（`pcm_sample_chunk_counts()` / `_pcm_span_scan()`）：跨段按字节定位
  读，内存里永远只有一个块；
- **数字静音门**（`is_digital_silence()` / `measure_span()`）：峰值低于半格
  （`SILENT_PEAK_FLOOR`）且长度够 `MIN_GATE_SAMPLES` 的块不送引擎——引擎在样本全零的块
  上会编出整段假正文；判不了（解不开、读不全）照常送引擎并记
  `gate = "unjudged"`，与健康运行分得开；
- **临时 wav 写盘**（`write_chunk_wav()`）：float32 必须先乘 32767 再转 int16，直接
  `.astype("<i2")` 会把 `[-1, 1]` 波形截成全 0，写出一份数字静音的 wav。

与 `meeting/asr/glm.py` 的 `_is_noise_segment` 分工：会中预筛按概率丢，会后闸门按全零丢。
重依赖惰性：`numpy` 一律在函数体内 import。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence, TYPE_CHECKING

from meeting.offline.audio import PCM_SUFFIX, PCM_SAMPLE_WIDTH, SAMPLE_RATE as PCM_SAMPLE_RATE

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，运行时不 import（惰性纪律）
    import numpy as np

_log = logging.getLogger("meeting.offline.runner.pcm")

#: 数字静音判据：峰值低于半格（**开区间**，正好半格算有声）算静音。峰值按
#: `samples_to_count_floats()` 换算成满量程格数且不截断，int16 与 float32 落同一条
#: 分界。峰值为 ±1 照样可能有声，不能拿「有非零样本」当判据。
SILENT_PEAK_FLOOR = 0.5

#: 门只判够 0.1 秒的一段：更短的不替引擎下结论，宁可多花一次引擎调用。
MIN_GATE_SAMPLES = PCM_SAMPLE_RATE // 10

#: 按字节块扫 PCM：内存里一次只握住一个块，会再长也压得住。
_PCM_SCAN_BLOCK_BYTES = 1 << 22

#: 翻译表把非零字节映成 `\x01`：`block.translate(...).count(1)` 一次算出非零字节数。
_PCM_SCAN_TABLE = bytes(0 if index == 0 else 1 for index in range(256))

#: 门在一段音频上的三种结论（`ChunkRun.gate` 与逐路 `gate` 都用这三个词）
GATE_SILENT = "silent"
GATE_NOT_SILENT = "not_silent"
GATE_UNJUDGED = "unjudged"

#: 门凭什么判的（产物 `gate_method` 词表）：
#: - `pcm_bytes`：按字节扫归档 PCM；
#: - `waveform`：量长度时解好的波形上切片（单趟跑的 MP3 落这一格）；
#: - `decode_at_plan`：量长度时解过、没成（**从不**表示门现解过）；
#: - `decode_now`：门现解一次（产品路径到不了，留给直接调 `measure_span()` 的调用方）。
GATE_METHOD_PCM = "pcm_bytes"
GATE_METHOD_WAVEFORM = "waveform"
GATE_METHOD_DECODE_AT_PLAN = "decode_at_plan"
GATE_METHOD_DECODE_NOW = "decode_now"


def full_scale_counts(samples: Any) -> Any:
    """归一化波形（`[-1, 1]`）→ **满量程格数**（clip ±1 × 32767，浮点、不截断）。

    满量程换算的唯一出处：写 wav（`write_chunk_wav()`）、
    判据（`samples_to_count_floats()`）与重放写盘（`meeting/offline/replay.py`）都从
    这一条换算，32767 与 32768 的分歧在别处长不出来。乘 32767 而不是 32768：±1 满量程
    对称映回 ±32767，不过冲 int16 的范围。
    """
    import numpy as np

    data = np.asarray(samples)
    return np.clip(data, -1.0, 1.0) * np.float32(32767.0)


def write_chunk_wav(path: Path | str, samples: "np.ndarray") -> Path:
    """把一段 16k 单声道波形写成 wav（引擎的 `decode_audio()` 吃它）。

    ⚠ **float32 必须先乘 32767 再转 int16**：直接 `.astype("<i2")` 会把 `[-1, 1]`
    波形截成全 0，写出一份数字静音的 wav——模型对静音照样吐 `[0.5][S01]…`。
    换算走 `full_scale_counts()`（满量程换算的唯一出处）。
    「写出来全零」不作产品代码错误判据：真正安静的会议写出来本来就是零，
    自检只在**调引擎之前**的静音门上做。`wave` / `numpy` 函数体内 import（惰性纪律）。
    """
    import wave

    import numpy as np

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    wave_data = np.asarray(samples, dtype=np.float32)
    pcm = full_scale_counts(wave_data).astype("<i2")
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(PCM_SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
    return target


def _audio_paths(path: "str | Path | Sequence[str | Path]") -> list[Path]:
    """把「一个路径或一串段路径」归一成段路径表（段是本层时间轴的唯一形状）。"""
    if isinstance(path, (list, tuple)):
        return [Path(item) for item in path]
    return [Path(path)]


def _decode_samples(path: "str | Path | Sequence[str | Path]") -> "np.ndarray":
    """解出整段 16k 单声道 float32 波形；给段路径表时逐段解、按序拼接。
    单列出来是给用例换掉真解码的接缝（conftest 不碰真模型）。"""
    from meeting.offline.audio import decode_audio

    paths = _audio_paths(path)
    if len(paths) == 1:
        return decode_audio(paths[0])
    import numpy as np

    pieces = [decode_audio(item) for item in paths]
    return np.concatenate(pieces) if pieces else np.empty(0, dtype=np.float32)


def _pcm_sample_count(paths: "Path | str | Sequence[Path | str]") -> int:
    """一段列 PCM 的样本数（各段字节数之和 ÷ 2，不读内容）。"""
    return sum(
        os.path.getsize(path) // PCM_SAMPLE_WIDTH for path in _audio_paths(paths)
    )


def pcm_sample_chunk_counts(
    paths: "Path | str | Sequence[Path | str]", *, offset: float, seconds: float
) -> Any:
    """段列表拼成的时间轴上 `[offset, offset + seconds)` 读成 int16 计数。
    跨段的块按各段长度切着读（字节接字节就是同一条轴）；读不全时交回空数组
    （切块那一步照抛、门那一步放行）。"""
    import numpy as np

    series = _audio_paths(paths)
    start = int(round(float(offset) * PCM_SAMPLE_RATE))
    end = int(round((float(offset) + float(seconds)) * PCM_SAMPLE_RATE))
    skip = max(0, start)
    want = max(0, end - start)
    pieces: list[Any] = []
    for path in series:
        if want <= 0:
            break
        try:
            available = os.path.getsize(path) // PCM_SAMPLE_WIDTH
        except OSError:
            available = 0
        if skip >= available:
            skip -= available
            continue
        take = min(available - skip, want)
        with open(path, "rb") as handle:
            handle.seek(skip * PCM_SAMPLE_WIDTH)
            raw = handle.read(take * PCM_SAMPLE_WIDTH)
        got = len(raw) // PCM_SAMPLE_WIDTH
        if got:
            pieces.append(np.frombuffer(raw, dtype="<i2").astype(np.int32))
            want -= got
        skip = 0
        if got < take:
            break  # 这一段比标称短：时间轴到这里为止
    if not pieces:
        return np.empty(0, dtype=np.int32)
    return np.concatenate(pieces)


def _pcm_span_scan(
    paths: "Path | str | Sequence[Path | str]", *, offset: float, seconds: float
) -> bool | None:
    """按字节块扫一段：读到非零字节 `False`，读满全零 `True`；没读全或读不动交回 `None`
    ——门只对「确实读到全零」下结论。见到第一个非零字节就返回，全零才读满。
    跨段偏移按各段长度逐段折算。
    """
    series = _audio_paths(paths)
    start = max(0, int(round(float(offset) * PCM_SAMPLE_RATE)))
    count = max(0, int(round(float(seconds) * PCM_SAMPLE_RATE)))
    remaining = count * PCM_SAMPLE_WIDTH
    skip = start * PCM_SAMPLE_WIDTH
    try:
        for path in series:
            if remaining <= 0:
                break
            size = os.path.getsize(path)
            if skip >= size:
                skip -= size
                continue
            with open(path, "rb") as handle:
                handle.seek(skip)
                skip = 0
                while remaining > 0:
                    block = handle.read(min(_PCM_SCAN_BLOCK_BYTES, remaining))
                    if not block:
                        break
                    if block.translate(_PCM_SCAN_TABLE).count(1):
                        return False
                    remaining -= len(block)
    except OSError:  # 读不到按「可能有声」放行，引擎那一层照样报错
        _log.warning("会后离线：数字静音门读不动 %s，照常送引擎", series, exc_info=True)
        return None
    if remaining > 0:
        return None
    return True


def samples_to_count_floats(samples: Any) -> Any:
    """归一化波形 → **满量程格数**（浮点，**不截断**）：判据要把 int16 与 float32 放上
    同一条「半格」分界，截断会把半格上下的样本一起抹成 0（见模块头）。换算走
    `full_scale_counts()`（满量程换算的唯一出处）。
    """
    import numpy as np

    data = np.asarray(samples)
    if data.dtype.kind == "f":
        return full_scale_counts(data)
    if data.dtype.kind in "iu":
        # 只升位到 int32（后续 abs 不溢出）；绝对值与取峰在 `_peak_counts()`
        return data.astype(np.int32)
    raise TypeError(f"波形的 dtype 该是 float 或 int，收到 {data.dtype}")


def _peak_counts(scaled: Any) -> float:
    """一串**满量程格数**（浮点或整数）的峰值（空数组记 0）。"""
    import numpy as np

    if scaled.size == 0:
        return 0.0
    # 先 abs、再升 int64 取最大：int16 的 -32768 不会被 abs 溢出吃掉
    magnitudes = np.abs(scaled)
    if magnitudes.dtype.kind == "f":
        return float(np.max(magnitudes))
    return float(np.max(magnitudes.astype(np.int64)))


def is_digital_silence(samples: Any) -> bool:
    """这一段波形是不是**数字静音**：样本全零（峰值 < `SILENT_PEAK_FLOOR` 半格，开区间）
    且长度够 `MIN_GATE_SAMPLES`（样本不足时不替引擎下结论）；空波形算静音。入参
    int16 计数或 `[-1, 1]` float32 都收，同一把浮点尺子。与 `meeting/asr/glm.py` 的
    `_is_noise_segment` 的分工见模块头：会中预筛按概率丢，会后闸门按全零丢。
    """
    scaled = samples_to_count_floats(samples)
    if scaled.size == 0:
        return True
    if int(scaled.size) < MIN_GATE_SAMPLES:
        return False
    return _peak_counts(scaled) < float(SILENT_PEAK_FLOOR)


@dataclass(slots=True)
class MeasureResult:
    """门对一段音频的结论：`silent = True/False` 是判过的两种答案，`None` 是**判不了**
    （照常送引擎）——布尔值说不出「我没判」，产物靠它把没门可用的运行露出来。
    """

    method: str
    silent: bool | None
    detail: str | None = None

    @property
    def judged(self) -> bool:
        return self.silent is not None


def _span_samples(waveform: Any, *, offset: float, seconds: float) -> Any | None:
    """已解好的波形上取 `[offset, offset + seconds)` 那一段；取不全（没读全）交回 `None`。"""
    import numpy as np

    data = np.asarray(waveform)
    if data.size == 0:
        # 空波形算静音（见 `is_digital_silence()`）
        return data
    start = max(0, int(round(float(offset) * PCM_SAMPLE_RATE)))
    count = max(0, int(round(float(seconds) * PCM_SAMPLE_RATE)))
    if start + count > int(data.size):
        return None
    return data[start:start + count]


def measure_span(
    path: "str | Path | Sequence[str | Path]",
    *,
    offset: float,
    seconds: float,
    waveform: Any = None,
    waveform_length: int | None = None,
    decode_attempted: bool = False,
) -> MeasureResult:
    """门在一段音频上的结论，按成本从低到高三条路（前一条用得上就不走后面）：

      1. 归档 PCM 按字节扫那一段（见到第一个非零字节就出结果）；
      2. 已解好的波形切片判（`_plan_for_source()` 量长度时解的那份，单趟 MP3 走这条）；
      3. 现解一次音频文件——产品路径到不了，留给直接调用方。

    `waveform_length` 是已解波形的总样本数，「这一段没读全」与「读到全零」靠它分开。
    `decode_attempted=True` 表示量长度那一步已经解过一次没成，这里照「判不了」登记、
    不再解第二遍（同一个坏文件解两次只会把同一句话打两遍日志）。
    """
    audio_paths = _audio_paths(path)
    audio_path = audio_paths[0]
    if audio_path.suffix.lower() == PCM_SUFFIX:
        count = max(0, int(round(float(seconds) * PCM_SAMPLE_RATE)))
        if count < MIN_GATE_SAMPLES:
            return MeasureResult(
                method=GATE_METHOD_PCM,
                silent=False,
                detail="这一段短于 0.1 秒，样本不足时不替引擎下结论",
            )
        verdict = _pcm_span_scan(audio_paths, offset=offset, seconds=seconds)
        if verdict is None:
            return MeasureResult(
                method=GATE_METHOD_PCM,
                silent=None,
                detail=(
                    f"归档 PCM 上这一段没读全（要 {count} 个样本，"
                    f"{audio_path.name} 比这个区间短或者读不动）"
                ),
            )
        return MeasureResult(method=GATE_METHOD_PCM, silent=verdict)

    if waveform_length is not None:
        piece = _span_samples(waveform, offset=offset, seconds=seconds)
        if piece is None:
            return MeasureResult(
                method=GATE_METHOD_WAVEFORM,
                silent=None,
                detail=(
                    f"解好的波形只有 {int(waveform_length)} 个样本，"
                    "盖不住这一段的区间"
                ),
            )
        return MeasureResult(
            method=GATE_METHOD_WAVEFORM, silent=is_digital_silence(piece)
        )

    if decode_attempted:
        return MeasureResult(
            method=GATE_METHOD_DECODE_AT_PLAN,
            silent=None,
            detail="这个文件解不出波形（量长度那一步已经试过一次）",
        )

    try:
        decoded = _decode_samples(audio_paths)
    except Exception as exc:  # 解不开：门判不了，照常送引擎
        return MeasureResult(
            method=GATE_METHOD_DECODE_NOW,
            silent=None,
            detail=f"{type(exc).__name__}: {exc}",
        )
    start = max(0, int(round(float(offset) * PCM_SAMPLE_RATE)))
    count = max(0, int(round(float(seconds) * PCM_SAMPLE_RATE)))
    if int(decoded.size) and start + count > int(decoded.size):
        return MeasureResult(
            method=GATE_METHOD_DECODE_NOW,
            silent=None,
            detail=(
                f"解出来只有 {int(decoded.size)} 个样本，盖不住这一段的区间"
                f"（{start} + {count}）"
            ),
        )
    return MeasureResult(
        method=GATE_METHOD_DECODE_NOW,
        silent=is_digital_silence(decoded[start:start + count]),
    )
