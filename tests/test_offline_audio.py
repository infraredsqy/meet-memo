"""`meeting/offline/audio.py` 的契约用例：把容器解成 16k 单声道 float32 波形。

**素材现场生成**：用标准库 `wave` 写一段 16k 单声道 int16 的 wav（不引第三方），
归档 PCM 那一档直接 `write_bytes()` 写无头裸流（`_write_raw_pcm()`），
**不依赖任何真实会议音频**（真素材只走一条一次性 MOSSTD 冒烟脚本，跑完即弃）。

解码走 PyAV，因此本文件会真的拉 `av` 进来 —— 与 `meeting/offline/engine.py` 的
惰性 import 不冲突：那条纪律要求「import 模块」不拉重依赖，调用解码函数时当然要拉。
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from meeting.offline import audio as audio_module
from meeting.offline import segment as segment_module

#: 造文件时用的采样率（与离线路径同一档）
RATE = 16000
#: 前半段 / 后半段的样本数：两段各有一个样本值，用来验换算公式与顺序
PAIR = 16


def _write_wav(path: Path, samples: np.ndarray, *, rate: int = RATE, channels: int = 1) -> Path:
    """把 int16 样本写成 16k 单声道 wav（标准库 `wave`，不引第三方）。"""
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.astype("<i2").tobytes())
    return path


def _write_raw_pcm(path: Path, samples: np.ndarray) -> Path:
    """把 int16 样本写成**无头**的裸 PCM（采集侧 `meeting_audio_<source>.pcm` 的形状）。"""
    path.write_bytes(samples.astype("<i2").tobytes())
    return path


def test_decode_audio_reads_a_headerless_pcm_archive(tmp_path: Path) -> None:
    """归档 PCM（无头 16k 单声道 s16le）解得开，换算仍是 `/32768.0`。

    PyAV 的容器探测在无头裸流上必然失败（`InvalidDataError`），`decode_audio()` 因此必须
    按后缀 `.pcm` 显式给出格式 —— `260928` 那一场就是在这里丢掉整条 mic 转写的。
    """
    samples = np.array([16384, -16384, 0, 32767, -32768] * PAIR, dtype=np.int16)
    path = _write_raw_pcm(tmp_path / "meeting_audio_mic.pcm", samples)

    waveform = audio_module.decode_audio(path)

    assert waveform.dtype == np.float32
    assert waveform.ndim == 1
    assert waveform.shape == (samples.size,)
    assert waveform[0] == pytest.approx(0.5, abs=1e-6)
    assert waveform[4] == pytest.approx(-1.0, abs=1e-6)


def test_decode_audio_still_probes_non_pcm_containers(tmp_path: Path) -> None:
    """非 `.pcm` 的容器照旧走 PyAV 探测：同一段样本的 wav 与裸 PCM 解出同一条波形。"""
    samples = np.array([16384, -16384, 0, 32767, -32768] * PAIR, dtype=np.int16)
    wav = _write_wav(tmp_path / "levels.wav", samples)
    pcm = _write_raw_pcm(tmp_path / "meeting_audio_mic.pcm", samples)

    np.testing.assert_array_equal(
        audio_module.decode_audio(wav), audio_module.decode_audio(pcm)
    )


def test_decode_audio_returns_16k_mono_float32(tmp_path: Path) -> None:
    """解码出来的是一维 float32 波形，长度等于源样本数，取值落在 `[-1, 1)`。"""
    samples = np.array([16384, -16384, 0, 32767, -32768] * PAIR, dtype=np.int16)
    path = _write_wav(tmp_path / "tone.wav", samples)

    waveform = audio_module.decode_audio(path)

    assert waveform.dtype == np.float32
    assert waveform.ndim == 1
    assert waveform.shape == (samples.size,)
    assert float(waveform.min()) >= -1.0
    assert float(waveform.max()) < 1.0


def test_decode_audio_maps_int16_to_unit_scale(tmp_path: Path) -> None:
    """换算按 `/32768.0`：正满量程落在 32767/32768，负满量程落在 -1.0。"""
    samples = np.array([16384, -16384, 32767, -32768], dtype=np.int16)
    path = _write_wav(tmp_path / "levels.wav", samples)

    waveform = audio_module.decode_audio(path)

    assert waveform[0] == pytest.approx(0.5, abs=1e-6)
    assert waveform[1] == pytest.approx(-0.5, abs=1e-6)
    assert waveform[2] == pytest.approx(32767 / 32768.0, abs=1e-6)
    assert waveform[3] == pytest.approx(-1.0, abs=1e-6)


def test_decode_audio_rejects_container_without_audio_stream(tmp_path: Path) -> None:
    """没有音频流的容器如实报错（`ValueError`），不当成空波形混过去。"""
    path = tmp_path / "empty.wav"
    _write_wav(path, np.zeros(0, dtype=np.int16))

    with pytest.raises(ValueError):
        audio_module.decode_audio(path)


def test_decoded_waveform_feeds_the_segmenter(tmp_path: Path) -> None:
    """解码结果是切段的输入形态：转回 int16 之后 `segment_pcm` 能吃、能切出段。

    期望值按切段规则自己算（口径见 `tests/test_offline_segment.py` 的文件头）：一段
    2 秒语音 + 两侧 4 秒静音，起始落在前导静音之后，段长 = 语音 + VAD 滞后 + 静默判据
    越过的那一帧。
    """
    rng = np.random.default_rng(20260917)
    speech = (rng.standard_normal(RATE * 2) * 3000).astype(np.int16)
    pad = np.zeros(int(RATE * segment_module.SILENCE_TIMEOUT * 2), dtype=np.int16)
    path = _write_wav(tmp_path / "speech.wav", np.concatenate([pad, speech, pad]))

    waveform = audio_module.decode_audio(path)
    raw = (waveform * 32768.0).astype(np.int16).tobytes()
    spans = segment_module.segment_pcm(raw, max_segment_seconds=60.0)

    assert len(spans) == 1
    start, length, samples = spans[0]
    assert start == pytest.approx(segment_module.SILENCE_TIMEOUT * 2, abs=0.1)
    assert samples.dtype == np.int16
    assert length == pytest.approx(4.17, abs=0.2)
    assert samples.size == int(length * segment_module.SAMPLE_RATE)
