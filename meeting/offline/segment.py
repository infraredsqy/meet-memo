"""离线切段：把归档的整段 PCM 切成段列表（会中与离线共用同一套切段规则）。

`numpy` 与 `webrtcvad` 都在函数体内惰性 import；采样率与 VAD 档位走形参（默认 16000 / 1），
本模块不认识配置模块。
"""

from __future__ import annotations

from meeting.offline.audio import SAMPLE_RATE

# 离线路径这一组形状事实的唯一出处住 `meeting/offline/audio.py`
FRAME_MS = 30
FRAME_BYTES = int(SAMPLE_RATE * FRAME_MS / 1000) * 2
SILENCE_TIMEOUT = 2.0  # 与 VADProcessor 保持一致
MIN_SEGMENT_MS = 500

# 实时与离线共用同一个 VAD 档
VAD_MODE = 1


def segment_pcm(raw: bytes, *, max_segment_seconds: float,
                sample_rate: int = SAMPLE_RATE, vad_mode: int = VAD_MODE
                ) -> list[tuple[float, float, "np.ndarray"]]:
    """按 VADProcessor 的规则切分（静默 2s 或段长硬上限）。

    返回 [(起始秒, 段长秒, int16 ndarray)]。
    """
    import webrtcvad
    import numpy as np

    vad = webrtcvad.Vad(vad_mode)
    nf = len(raw) // FRAME_BYTES
    cap_frames = max_segment_seconds * 1000 / FRAME_MS
    spans: list[tuple[int, int]] = []
    cur = 0
    last_speech = 0
    for i in range(nf):
        b = raw[i * FRAME_BYTES:(i + 1) * FRAME_BYTES]
        if len(b) < FRAME_BYTES:
            break
        if vad.is_speech(b, sample_rate):
            last_speech = i
            cur += 1
        elif cur > 0:
            cur += 1
            if (i - last_speech) * FRAME_MS / 1000 > SILENCE_TIMEOUT:
                spans.append(((i + 1) * FRAME_MS - cur * FRAME_MS, cur * FRAME_MS))
                cur = 0
        if cur >= cap_frames:
            spans.append(((i + 1) * FRAME_MS - cur * FRAME_MS, cur * FRAME_MS))
            cur = 0
    if cur:
        spans.append((nf * FRAME_MS - cur * FRAME_MS, cur * FRAME_MS))

    out = []
    for start_ms, len_ms in spans:
        if len_ms < MIN_SEGMENT_MS or start_ms < 0:
            continue
        a = int(start_ms / 1000 * sample_rate)
        n = int(len_ms / 1000 * sample_rate)
        audio = np.frombuffer(raw[a * 2:(a + n) * 2], dtype=np.int16).copy()
        if len(audio):
            out.append((start_ms / 1000.0, len_ms / 1000.0, audio))
    return out
