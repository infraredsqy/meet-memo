"""把任意音频容器解成 16k 单声道 float32 波形（会后离线路径的入口）。

**一帧一帧地用 PyAV 解**，不走 `transformers.audio_utils.load_audio` 的字符串路径：
那一条在本机（没装 `librosa`）会落到 `requires_backends(..., ["librosa"])` 上直接抛错，
mp3 与 wav 都一样。`meeting/offline/engine.py` 因此把**波形以 ndarray 传进 processor**，
本模块就是产出那个 ndarray 的地方。

重依赖惰性：`av` 与 `numpy` 都在函数体内 import（`meeting/asr/glm.py` 的既有纪律）。
"""

from __future__ import annotations

from pathlib import Path

#: 离线路径的采样率：与采集/会中转写同一档（`docs/v1-lineage.md` 的 B8）
SAMPLE_RATE = 16000

#: 归档 PCM 的后缀（`meeting/offline/runner/pcm.py` 的静音门与编排主模块的切块按同一个后缀分流）
PCM_SUFFIX = ".pcm"

#: 归档 PCM 的原始格式：16k 单声道有符号 16 位小端
#: （`meeting/capture/archive.py` 写出来的就是 `SAMPLE_RATE` / `CHANNELS=1` / `SAMPLE_WIDTH=2`）
PCM_FORMAT = "s16le"

#: 归档 PCM 的样本宽度（字节）：同一档的 16 位 → 2 字节。runner / replay 的按字节
#: 读数（样本数 = 字节数 ÷ 它）都从这里取，这一组形状事实只住这一处。
PCM_SAMPLE_WIDTH = 2


def decode_audio(path: str | Path) -> "np.ndarray":
    """把任意容器（mp3/wav/…）解成 16k 单声道 float32 一维波形。

    照 PyAV 手册常规走法：`av.open` → 第一条音频流 → `AudioResampler(s16/mono/16k)`
    → 逐帧拼接 → `/32768.0`。两处按 PyAV 接口如实处理：`resample()` 在新版返回**帧列表**
    （`_as_frames()` 两种形态都收）；收尾要 `resample(None)` 冲出重采样器内部缓冲的尾巴，
    否则末尾短一截。归档 PCM 是无头裸流，开容器按后缀分流（见 `_open_container()`）。
    """
    import numpy as np
    import av

    chunks: list = []
    with _open_container(av, path) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError(f"没有音频流：{path}")
        resampler = av.audio.resampler.AudioResampler(
            format="s16", layout="mono", rate=SAMPLE_RATE
        )
        for frame in container.decode(stream):
            for resampled in _as_frames(resampler.resample(frame)):
                chunks.append(resampled.to_ndarray().reshape(-1))
        for resampled in _as_frames(resampler.resample(None)):
            chunks.append(resampled.to_ndarray().reshape(-1))

    if not chunks:
        raise ValueError(f"没有解出任何样本：{path}")
    raw = np.concatenate(chunks)
    return (raw.astype(np.float32) / 32768.0).astype(np.float32, copy=False)


def _open_container(av, path: str | Path):
    """按容器形态开 PyAV 输入：归档 PCM 走**显式格式**，别的容器照旧让 PyAV 探测。

    归档 PCM 是**无头裸流**，`av.open()` 的探测在它上面必然失败（实测
    `InvalidDataError`）——这与文件好坏无关，同一个文件按显式格式打开整份样本都解得出来
    （`260928` 的 900224 字节归档 PCM 解出 450112 样本，`450112 * 2 == 900224`）。
    分流只认后缀，口径与 `meeting/offline/runner/pcm.py` 的静音门、编排主模块的切块同一条。
    """
    if Path(path).suffix.lower() == PCM_SUFFIX:
        return av.open(
            str(path),
            format=PCM_FORMAT,
            options={"sample_rate": str(SAMPLE_RATE), "channels": "1"},
        )
    return av.open(str(path))


def _as_frames(resampled) -> list:
    """把 `AudioResampler.resample()` 的返回值统一成帧列表（`None` 收成空列表）。"""
    if resampled is None:
        return []
    if isinstance(resampled, list):
        return resampled
    return [resampled]
