"""离线切段：把归档的整段 PCM 切成段列表（会中与离线共用同一套切段规则）。

**搬自 V1 `mnotes_core/replay.py:28-69`**（原件见 V1 仓库同名文件；照
`docs/v1-lineage.md` 第 11 章「原 1.0 第 3 / 4 / 5 条」，先建、diff 过、在本单元结束时删掉）。
V1 那一份是会后离线回放的切段接缝（`v1-lineage` 的 A4），数值与会中断句同规则：
30ms 帧、静默 2.0s 收段、丢掉 <500ms 的段、`VAD_MODE=1`。

搬过来动了两处，都属原 1.0 第 4 条的「去重依赖」，数值一个都没改：

  1. V1 从 `mnotes_core.config` 读 `SAMPLE_RATE` 与 `VAD_MODE`，这里改成形参
     （默认 16000 / 1）——本模块不认识 V1 的配置模块；
  2. V1 顶层 `import numpy as np`，这里 `numpy` 与 `webrtcvad` 都在函数体内惰性 import
     （`meeting/asr/glm.py` 的既有纪律）。

V1 `replay.py:72-80` 的 `_percentiles` **没有搬**：那是隔壁的百分位统计（B16 报告契约里
的段耗时分布），与切段逻辑分开（`v1-lineage` 的 A4 写着这一条）。

函数体逐字来自 V1，只有一处名字替换：原来的 `SAMPLE_RATE` 与 `VAD_MODE` 换成同名形参
`sample_rate` 与 `vad_mode`。
"""

from __future__ import annotations

from meeting.offline.audio import SAMPLE_RATE

# 搬自 V1 mnotes_core/replay.py:23-25（V1 的 `SAMPLE_RATE` 来自 `mnotes_core/config.py:35`，
# 同值 16000；离线路径这一组形状事实的唯一出处住 `meeting/offline/audio.py`）
FRAME_MS = 30
FRAME_BYTES = int(SAMPLE_RATE * FRAME_MS / 1000) * 2  # 960
SILENCE_TIMEOUT = 2.0  # 与 VADProcessor 保持一致
MIN_SEGMENT_MS = 500  # V1 函数体里那个字面量 500 的名字（数值未改）

# 搬自 V1 mnotes_core/config.py 的 VAD_MODE（`v1-lineage` 的 B8：实时与离线共用）
VAD_MODE = 1


# 搬自 V1 mnotes_core/replay.py:28-69
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
