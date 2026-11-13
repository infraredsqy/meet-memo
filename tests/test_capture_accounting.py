"""`tests/test_capture_accounting.py` —— 归档核对逻辑本身，以及把它拆穿的反例。

核对回答一个问题：**归档里躺着的 PCM，能不能信？** 口径（设计口径 §1 / §3）：

  - 逐帧：把归档 PCM 按 0.5 秒一帧重新切片、用 `meeting.capture.levels.frame_level`
    **重跑一遍电平换算**——帧序号从 0 连续编号、一帧不落（一帧不落 = 丢 0 秒），
    顺手交出「有声帧 / 静音帧」两个计数；
  - 逐样本：样本数与「喂进去的块数 × 每块样本数」相符；
  - 逐字节：与「喂进去的期望字节序列」逐字节一致——丢一块，后面全部错位，第一处
    分歧的样本序号直接指认被丢的那一块（光数总数逮不住换序、逮不住等长替换）；
  - 与墙钟：「start 到 stop 之间的墙钟秒数 × 16000」的偏差在一个采集块（1600 样本
    = 0.1 秒）以内。

**期望侧从哪儿来**：本模块只收调用方给的 `expected_bytes` / `expected_samples`——
端到端里它们来自假源当场记下的喂块记录（`tests/_capture_faulty.py` 的 `fed` 与
`expected_archive_bytes()`），没有一个自己编的常数。

**反例的必要性**：一个只会说「全对」的核对函数没有价值。本文件后半段用已知洞
（中段丢块、换序、截断奇字节、缺尾帧、纯静音段、空归档）逐条验它报得出、报得准。
本文件里核对函数是**纯函数**（只吃字节、不碰设备、不起线程），端到端那一场在
`tests/test_capture_e2e.py` 里复用同一组函数。
"""

from __future__ import annotations

import numpy as np

from tests._capture_fakes import audit_pcm, replay_level_frames, samples_within_one_block

# ---------------------------------------------------------------------------
# 反例与正例（核对函数自己的验收）
# ---------------------------------------------------------------------------

#: 一块 0.1 秒的样本（核对用例自己的序列生成器；与 `_capture_faulty` 的规则无关——
#: 核对函数吃的只是「字节 + 期望侧」，它不关心期望侧是哪家假源记下的）
CHUNK_SAMPLES = 1600


def _chunk(value: int, samples: int = CHUNK_SAMPLES) -> bytes:
    return np.full(samples, value, dtype=np.int16).tobytes()


def _stream(values: list[int]) -> bytes:
    """按给定的每块常值把一段 PCM 流拼出来。"""
    return b"".join(_chunk(value) for value in values)


def test_a_complete_sequence_passes_every_check():
    """正例：字节逐一对上、样本数对上、帧序号连续、有声/静音计数与剧本一致。"""
    # 17 块（3.4 秒）：0–4 有声、5–9 静音、10–14 有声、15–16 有声
    values = [2000 + index for index in range(5)] + [0] * 5 + [2010 + index for index in range(5)] + [2025, 2026]
    stream = _stream(values)
    audit = audit_pcm(
        stream,
        expected_bytes=stream,
        expected_samples=len(values) * CHUNK_SAMPLES,
    )
    assert audit.even_bytes and audit.trailing_bytes == 0
    assert audit.samples == len(values) * CHUNK_SAMPLES
    assert audit.sample_gap == 0
    assert audit.byte_match is True and audit.mismatched_bytes == 0
    assert audit.frames == 3 and audit.leftover_samples == 2 * CHUNK_SAMPLES
    assert audit.frame_indices == [0, 1, 2]
    assert audit.missing_frame_indices == []
    assert (audit.voiced_frames, audit.silent_frames) == (2, 1)
    assert audit.seconds == 1.7


def test_a_dropped_middle_chunk_is_named_by_sample_index():
    """反例（中段丢一块）：样本缺口 -1600，第一处分歧的样本序号正落在被丢那块的门口。"""
    values = [1000 + index for index in range(10)]
    hole_at = 7
    intact = _stream(values)
    with_hole = _stream(values[:hole_at] + values[hole_at + 1 :])
    audit = audit_pcm(
        with_hole,
        expected_bytes=intact,
        expected_samples=len(values) * CHUNK_SAMPLES,
    )
    assert audit.byte_match is False
    assert audit.sample_gap == -CHUNK_SAMPLES
    assert audit.first_mismatch_sample == hole_at * CHUNK_SAMPLES
    assert audit.mismatched_bytes == 2 * len(_chunk(values[-1]))  # 错位波及第 7、8 两块


def test_a_swapped_pair_with_matching_totals_is_caught():
    """反例（等长换序）：总数与长度全对，逐字节比对仍然逮住它——光数总数不够。"""
    values = [1000, 1001, 1002, 1003]
    intact = _stream(values)
    swapped = _stream([1000, 1001, 1003, 1002])
    audit = audit_pcm(swapped, expected_bytes=intact, expected_samples=4 * CHUNK_SAMPLES)
    assert audit.sample_gap == 0  # 样本数一个不差
    assert audit.byte_match is False
    assert audit.first_mismatch_sample == 2 * CHUNK_SAMPLES


def test_an_odd_trailing_byte_is_reported_and_dropped_from_framing():
    """反例（残字节）：一个孤儿字节让 even_bytes 翻假；重放只吃完整样本，不硬凑。"""
    stream = _stream([1000] * 5) + b"\x07"
    audit = audit_pcm(stream, expected_samples=5 * CHUNK_SAMPLES)
    assert audit.even_bytes is False and audit.trailing_bytes == 1
    assert audit.samples == 5 * CHUNK_SAMPLES  # 残字节成不了样本
    assert audit.frames == 1 and audit.frame_indices == [0]


def test_a_partial_tail_is_leftover_not_a_frame():
    """尾部不足一帧：记 leftover，不硬凑成帧；帧序号照样从 0 连续。"""
    audit = audit_pcm(_stream([1000] * 7), expected_samples=7 * CHUNK_SAMPLES)
    assert audit.frames == 1
    assert audit.leftover_samples == 2 * CHUNK_SAMPLES
    assert audit.frame_indices == [0]
    assert audit.missing_frame_indices == []


def test_silence_is_counted_but_never_reported_as_loss():
    """反例（静音段）：字节完整、样本数齐——静音是内容，丢块是事故，两者分开登记。"""
    values = [1000, 0, 0, 1003]  # 中间两块静音
    stream = _stream(values)
    audit = audit_pcm(stream, expected_bytes=stream, expected_samples=4 * CHUNK_SAMPLES)
    assert audit.byte_match is True and audit.sample_gap == 0
    assert audit.frames == 0  # 0.4 秒凑不满一个 0.5 秒帧，但静音样本已经在册
    # 整帧静音：一段有声帧 + 一段全零帧，两个计数各归各家
    zoned = audit_pcm(_stream([1000] * 5 + [0] * 5), expected_samples=10 * CHUNK_SAMPLES)
    assert zoned.frames == 2
    assert (zoned.voiced_frames, zoned.silent_frames) == (1, 1)


def test_an_empty_archive_audits_to_zero_without_drama():
    """空归档（未选中的那一路）：0 帧、0 计数、偶数字节，核对不炸。"""
    audit = audit_pcm(b"", expected_bytes=b"", expected_samples=0)
    assert audit.total_bytes == 0 and audit.samples == 0 and audit.seconds == 0.0
    assert audit.frames == 0 and audit.frame_indices == []
    assert audit.voiced_frames == 0 and audit.silent_frames == 0
    assert audit.even_bytes is True and audit.byte_match is True


def test_a_frame_replay_reruns_the_level_math_on_archived_bytes():
    """重放真的重算：常值 8000 的一帧，均方根落在理论值上；全零帧落到 -120。"""
    loud = _chunk(8000, samples=8000)
    readings = replay_level_frames(loud + _chunk(0, samples=8000))
    assert [reading.index for reading in readings] == [0, 1]
    expected_dbfs = 20.0 * float(np.log10(8000 / 32768.0))
    assert abs(readings[0].dbfs - expected_dbfs) < 0.01
    assert readings[0].bar == max(readings[0].dbfs, readings[0].peak)
    assert readings[1].dbfs == -120.0 and readings[1].peak == -120.0


def test_missing_frames_are_listed_when_the_archive_is_short():
    """少了一整帧还多：期望帧里没人顶的那些序号被点名（帧序号层面的空洞记录）。"""
    audit = audit_pcm(
        _stream([1000] * 5),  # 0.5 秒 = 1 帧
        expected_samples=15 * CHUNK_SAMPLES,  # 本该有 3 帧
    )
    assert audit.frames == 1
    assert audit.missing_frame_indices == [1, 2]


def test_the_wall_clock_deviation_bound_is_one_block():
    """墙钟核对的分界：差一个块（1600 样本）算过，再多半块就翻脸。"""
    assert samples_within_one_block(16000, 1.0)  # 一秒整
    assert samples_within_one_block(16000 + 1600, 1.0)  # 恰好多一个块
    assert samples_within_one_block(16000 - 1600, 1.0)  # 恰好少一个块
    assert samples_within_one_block(16000 + 1601, 1.0) is False
    assert samples_within_one_block(16000 - 3200, 1.0) is False
