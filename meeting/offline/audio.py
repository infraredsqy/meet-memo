"""把任意音频容器解成 16k 单声道 float32 波形（会后离线路径的入口）。

**一帧一帧地用 PyAV 解**，不走 `transformers.audio_utils.load_audio` 的字符串路径：
那一条在本机（没装 `librosa`）会落到 `requires_backends(..., ["librosa"])` 上直接抛错，
mp3 与 wav 都一样。`meeting/offline/engine.py` 因此把**波形以 ndarray 传进 processor**，
本模块就是产出那个 ndarray 的地方。

重依赖惰性：`av` 与 `numpy` 都在函数体内 import（`meeting/asr/glm.py` 的既有纪律）。
"""

from __future__ import annotations

from pathlib import Path

#: 离线路径的采样率：与采集/会中转写同一档（`docs/legacy-assets.md` 的 B8）
SAMPLE_RATE = 16000

#: 归档 PCM 的后缀（`meeting/offline/runner.py` 的静音门与切块按同一个后缀分流）
PCM_SUFFIX = ".pcm"

#: 归档 PCM 的原始格式：16k 单声道有符号 16 位小端
#: （`meeting/capture/archive.py` 写出来的就是 `SAMPLE_RATE` / `CHANNELS=1` / `SAMPLE_WIDTH=2`）
PCM_FORMAT = "s16le"


def decode_audio(path: str | Path) -> "np.ndarray":
    """把任意容器（mp3/wav/…）解成 16k 单声道 float32 一维波形。

    实现照 PyAV 手册的常规走法：`av.open` → 第一条音频流 →
    `AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)` → 逐帧
    `to_ndarray().reshape(-1)` 拼接 → `/32768.0` 转 float32。

    两处按 PyAV 的接口如实处理，都是解出完整波形必需的：

      - `resample()` 在 PyAV 里返回**帧列表**（旧版本返回单帧），两种形态都收下；
      - 收尾要再 `resample(None)` 把重采样器内部缓冲的尾巴冲出来，否则末尾会短一截。

    本机实测：99 分 03.3 秒的那条 mp3 走这条路得到 `(95092800,)` float32。

    归档 PCM（`meeting_audio_<source>.pcm`）是**无头的裸流**，开容器那一步按后缀分流
    （见 `_open_container()`）：探测在它上面必然失败，只有显式给出格式才解得开。
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

    归档 PCM（`meeting_audio_<source>.pcm`）是**无头的裸流**，`av.open()` 的探测在它上面
    必然失败（实测 `InvalidDataError: Invalid data found when processing input`）——
    这与文件好坏无关：同一个文件按下面的显式格式打开，整份样本都能解出来（`260928` 那份
    900224 字节的归档 PCM 解出 450112 个样本，`450112 * 2 == 900224`）。形状是已知的
    （`SAMPLE_RATE` / 单声道 / `PCM_FORMAT`），如实告诉 PyAV 就是正确的开法。

    分流只认后缀，口径与 `meeting/offline/runner.py` 的静音门、切块**同一条**
    （那条路已经在 `.pcm` 与非 `.pcm` 之间分流，这里不另立规矩）；其余容器一个字不动，
    仍旧交给 PyAV 探测。
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
