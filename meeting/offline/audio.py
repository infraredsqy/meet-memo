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


def decode_audio(path: str | Path) -> "np.ndarray":
    """把任意容器（mp3/wav/…）解成 16k 单声道 float32 一维波形。

    实现照 PyAV 手册的常规走法：`av.open` → 第一条音频流 →
    `AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)` → 逐帧
    `to_ndarray().reshape(-1)` 拼接 → `/32768.0` 转 float32。

    两处按 PyAV 的接口如实处理，都是解出完整波形必需的：

      - `resample()` 在 PyAV 里返回**帧列表**（旧版本返回单帧），两种形态都收下；
      - 收尾要再 `resample(None)` 把重采样器内部缓冲的尾巴冲出来，否则末尾会短一截。

    本机实测：99 分 03.3 秒的那条 mp3 走这条路得到 `(95092800,)` float32。
    """
    import numpy as np
    import av

    chunks: list = []
    with av.open(str(path)) as container:
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


def _as_frames(resampled) -> list:
    """把 `AudioResampler.resample()` 的返回值统一成帧列表（`None` 收成空列表）。"""
    if resampled is None:
        return []
    if isinstance(resampled, list):
        return resampled
    return [resampled]
