"""`meeting/offline/segment.py` 的契约用例：V1 切段接缝搬过来之后的数值与规则。

`docs/legacy-assets.md` 纪律 9 明写 `replay._segment` **零测试覆盖**，V1 那 27 个用例
对搬错、搬漏一个都不报警，所以这一层要自己补网。素材一律**现场合成的 PCM**
（前面静音 + 一段音 + 长静音），不碰真实会议音频。

四条要盯的规则（数值一个都不许改）：

  - 30ms 帧（`FRAME_BYTES == 960`，即 480 样本）；
  - 静默 **2.0s** 收段；
  - 丢掉 <**500ms** 的段；
  - 段长硬上限生效（长语音按上限切）。

**切出来的段长怎么算（`legacy-assets` 纪律 11：数字要带口径）**：语音块用一条固定随机流
的高斯噪声（RMS 3000），`webrtcvad` 档位 1 会把它整块判成语音。V1 的规则是
「静默**超过** 2.0s 才收段」，而 `cur` 在这 2.0s 里一直跟着涨，所以走静默这条路的段长
一定 ≥ 2.0s。走**收尾**这条路的段（正文没被 2.0s 静默截断，段尾就在音频末尾附近）才可能短，
`MIN_SEGMENT_MS` 那道判据正是作用在这里——本文件用「静音 + 语音块 + 短静音」的输入
把这条路单独演一遍。
"""

from __future__ import annotations

import inspect

import numpy as np
import pytest

from meeting.offline import segment as seg

#: 一帧的样本数（480）与字节数（960）
FRAME_SAMPLES = int(seg.SAMPLE_RATE * seg.FRAME_MS / 1000)
FRAME_BYTES = FRAME_SAMPLES * 2
#: 语音块的 RMS 档位：实测 `webrtcvad` 在档位 1 下逐帧判成语音（单频正弦做不到这一点）
NOISE_RMS = 3000
#: 随机流种子（每次取噪声都从同一条流往下走，因此同一段 PCM 里两个语音块不会一模一样，
#: 结果仍然是确定的）
SEED = 20260917
_RNG = np.random.default_rng(SEED)


def tone(frames: int, rms: int = NOISE_RMS) -> np.ndarray:
    """`frames` 帧的"语音"：高斯噪声，逐帧都判成语音。"""
    samples = _RNG.standard_normal(frames * FRAME_SAMPLES) * rms
    return samples.astype(np.int16)


def silence(frames: int) -> np.ndarray:
    """`frames` 帧的数字静音（全零样本）。"""
    return np.zeros(frames * FRAME_SAMPLES, dtype=np.int16)


def pcm(*parts: np.ndarray) -> bytes:
    """把若干 int16 片段接成一整段 PCM 字节流。"""
    return np.concatenate(parts).astype(np.int16).tobytes()


def frame_seconds(frames: float) -> float:
    """`frames` 帧合多少秒（用例里的期望值都按它算，不手写常数）。"""
    return frames * seg.FRAME_MS / 1000.0


def spans_of(*parts: np.ndarray, cap: float = 600.0) -> list[tuple[float, float, np.ndarray]]:
    """切一段合成 PCM，返回 `[(起始秒, 段长秒, 样本)]`。"""
    return seg.segment_pcm(pcm(*parts), max_segment_seconds=cap)


# ---------------------------------------------------------------------------
# 一、常量本身（数值契约）
# ---------------------------------------------------------------------------


def test_frame_bytes_is_960() -> None:
    """30ms 帧 @ 16kHz 是 **960 字节 = 480 样本**（`legacy-assets` 的 B8 写死的那个数）。"""
    assert seg.FRAME_MS == 30
    assert seg.SAMPLE_RATE == 16000
    assert FRAME_SAMPLES == 480
    assert seg.FRAME_BYTES == FRAME_BYTES == 960


def test_silence_timeout_is_two_seconds() -> None:
    """静默收段阈值是 **2.0 秒**。"""
    assert seg.SILENCE_TIMEOUT == 2.0


def test_min_segment_ms_is_500() -> None:
    """丢掉短于 **500ms** 的段。"""
    assert seg.MIN_SEGMENT_MS == 500


def test_vad_mode_defaults_to_one() -> None:
    """默认 VAD 档位是 **1**（`legacy-assets` 的 B8，实时与离线共用同一档）。"""
    assert seg.VAD_MODE == 1
    params = inspect.signature(seg.segment_pcm).parameters
    assert params["vad_mode"].default == 1
    assert params["sample_rate"].default == 16000


def test_max_segment_seconds_is_keyword_only() -> None:
    """段长上限是**必给的具名参数**（`segment_pcm(raw, max_segment_seconds=...)`）。"""
    params = inspect.signature(seg.segment_pcm).parameters
    assert params["max_segment_seconds"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["max_segment_seconds"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# 二、静默收段（走 2.0s 那条路，段长必然 ≥ 2.0s）
# ---------------------------------------------------------------------------


def test_silence_only_input_yields_nothing() -> None:
    """整段静音切出零段（静音起不了段）。"""
    assert spans_of(silence(600)) == []


def test_silence_after_speech_closes_the_segment() -> None:
    """前 3 帧静音 + 60 帧语音 + 90 帧尾静音 → 一段，起始落在静音之后。

    实测这一段是 3.96 秒（语音块 + VAD 的滞后 + 静默判据越过的那一帧），
    断言按「一次实测值 ± 0.3 秒」写，用来盯住切段位置没有被搬歪。
    """
    spans = spans_of(silence(3), tone(60), silence(90))

    assert len(spans) == 1
    start, length, samples = spans[0]
    assert start == pytest.approx(0.09, abs=0.1)
    assert length == pytest.approx(3.96, abs=0.3)
    assert samples.dtype == np.int16
    assert samples.size == int(length * seg.SAMPLE_RATE)


def test_tail_silence_longer_than_the_timeout_does_not_grow_the_segment() -> None:
    """尾静音再长，段长也不跟着长：4s 尾静音与 8s 尾静音切出的段一样长。"""
    short_tail = spans_of(silence(3), tone(60), silence(133))
    long_tail = spans_of(silence(3), tone(60), silence(267))

    assert len(short_tail) == len(long_tail) == 1
    assert short_tail[0][1] == pytest.approx(long_tail[0][1], abs=frame_seconds(1))
    assert short_tail[0][0] == pytest.approx(long_tail[0][0], abs=frame_seconds(1))


def test_a_gap_of_two_seconds_does_not_split_while_a_longer_gap_does() -> None:
    """静默阈值是「**超过** 2.0s 才收」：2.0s 的间隔出一段，2.5s 的间隔出两段。"""
    kept = spans_of(silence(5), tone(30), silence(67), tone(30), silence(20))
    split = spans_of(silence(5), tone(30), silence(84), tone(30), silence(20))

    assert len(kept) == 1
    assert len(split) == 2
    assert split[1][0] > split[0][0] + split[0][1]


# ---------------------------------------------------------------------------
# 三、丢掉 <500ms 的段（走收尾那条路）
# ---------------------------------------------------------------------------


def test_the_tail_segment_is_dropped_when_it_is_shorter_than_500ms() -> None:
    """语音块贴近音频末尾、后面只跟 0.1s 静音时，收尾段短于 500ms，**整段丢掉**。"""
    assert spans_of(silence(600), tone(8), silence(3)) == []


def test_the_tail_segment_survives_once_it_passes_500ms() -> None:
    """同一段语音块，后面跟 0.4s 静音就出段了（0.63s > 500ms），起始就是语音块起点。"""
    spans = spans_of(silence(600), tone(8), silence(13))

    assert len(spans) == 1
    start, length, _ = spans[0]
    assert start == pytest.approx(frame_seconds(600), abs=frame_seconds(2))
    assert length == pytest.approx(0.63, abs=0.06)
    assert length > seg.MIN_SEGMENT_MS / 1000.0


def test_the_speech_block_at_the_very_end_of_the_audio_yields_nothing() -> None:
    """语音块一直顶到音频末尾（后面一点静音都没有）时切不出段。"""
    assert spans_of(silence(600), tone(8)) == []


@pytest.mark.parametrize("block_frames", [2, 4, 8, 16, 32, 60])
def test_no_emitted_segment_is_shorter_than_500ms(block_frames: int) -> None:
    """任何输入下**切出来的每一段都不短于 500ms**（那道判据的成品口径）。"""
    spans = spans_of(silence(3), tone(block_frames), silence(90))

    assert all(length >= seg.MIN_SEGMENT_MS / 1000.0 for _, length, _ in spans)


# ---------------------------------------------------------------------------
# 四、段长硬上限
# ---------------------------------------------------------------------------


def test_max_segment_seconds_caps_a_long_utterance() -> None:
    """10 秒连续语音按 2.0 秒上限切：每段都 ≈ 上限（整数帧向上取），覆盖总时长不减。

    `cap_frames = 2.0 * 1000 / 30 = 66.67` 是小数，判据 `cur >= cap_frames` 在整数帧上
    落到 67 帧（2.01 秒）——这是 V1 逐字搬过来的行为，用例照实断言。
    """
    spans = spans_of(tone(334), cap=2.0)

    assert len(spans) == 5
    assert all(length == pytest.approx(frame_seconds(67), abs=1e-6) for _, length, _ in spans[:-1])
    assert spans[-1][1] == pytest.approx(frame_seconds(66), abs=1e-6)
    assert sum(length for _, length, _ in spans) == pytest.approx(frame_seconds(334), abs=1e-6)


def test_max_segment_seconds_above_the_utterance_keeps_it_whole() -> None:
    """上限高于语音本身时不切（同一段 PCM，上限 600s 出一段）。"""
    spans = spans_of(tone(334), cap=600.0)

    assert len(spans) == 1
    assert spans[0][1] == pytest.approx(frame_seconds(334), abs=frame_seconds(8))


def test_cap_splits_each_long_block_independently() -> None:
    """两段长语音各自按上限切，接缝不跨块（每块都被切齐、块间静默不并进段长）。"""
    spans = spans_of(silence(10), tone(60), silence(300), tone(120), silence(300), cap=4.0)

    assert len(spans) == 2
    assert spans[0][1] == pytest.approx(frame_seconds(132), abs=frame_seconds(3))
    assert spans[1][1] == pytest.approx(frame_seconds(134), abs=frame_seconds(3))
    assert spans[1][0] == pytest.approx(frame_seconds(370), abs=frame_seconds(3))


# ---------------------------------------------------------------------------
# 五、整段与样本的对应（段长与样本数同源）
# ---------------------------------------------------------------------------


def test_segment_samples_match_start_and_length() -> None:
    """每段的样本数与它自己的 `(起始, 段长)` 对得上，且内容就是整段里那一段字节。"""
    parts = (silence(10), tone(60), silence(300), tone(120), silence(300))
    raw = pcm(*parts)
    whole = np.frombuffer(raw, dtype=np.int16)

    spans = seg.segment_pcm(raw, max_segment_seconds=4.0)

    assert len(spans) == 2
    for start, length, samples in spans:
        assert samples.size == int(length * seg.SAMPLE_RATE)
        offset = int(start * seg.SAMPLE_RATE)
        assert np.array_equal(samples, whole[offset:offset + samples.size])


def test_segmenter_is_deterministic() -> None:
    """同一份输入跑两遍，切出同样的段（会后允许重跑，结果要稳）。"""
    raw = pcm(silence(10), tone(60), silence(300), tone(120), silence(300))

    first = seg.segment_pcm(raw, max_segment_seconds=4.0)
    second = seg.segment_pcm(raw, max_segment_seconds=4.0)

    assert [(s, l) for s, l, _ in first] == [(s, l) for s, l, _ in second]
    assert all(np.array_equal(a, b) for (_, _, a), (_, _, b) in zip(first, second))
