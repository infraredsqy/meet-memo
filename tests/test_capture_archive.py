"""`tests/test_capture_archive.py` —— PCM 归档写入器与 PCM→MP3 转码（`meeting/capture/archive.py`）。

**无硬件**：这里只碰文件与 `pydub` / `ffmpeg`（`ffmpeg` 在本机 PATH 上）。三块：

  1. `open_pcm()` —— 追加模式 + **无缓冲**（`buffering=0`，强杀最多丢最后一个块）；
  2. `PcmArchiveWriter` —— 自己的写线程、取一块写一块、`close()` 幂等且带 `fsync`；
  3. `convert_to_mp3()` —— 非空转出可解的 MP3、空的**不产出** MP3、转不动时**PCM 原样留下**。
"""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from meeting.capture.archive import (
    EMPTY_PCM_BYTES,
    MP3_BITRATE,
    PcmArchiveWriter,
    convert_to_mp3,
    mp3_path_for,
    open_pcm,
)

#: 一块 0.1 秒的 16k / 单声道 / int16 样本（3200 字节，与真实采集块同尺寸）
CHUNK_BYTES = 3200


def pcm_chunk(value: int = 1000, frames: int = 1600) -> bytes:
    """一块可预期的 int16 裸 PCM（每个样本都是 `value`）。"""
    return np.full(frames, value, dtype=np.int16).tobytes()


def pcm_payload(chunks: int = 3, value: int = 1000) -> bytes:
    """`chunks` 块拼起来的一整段 PCM。"""
    return pcm_chunk(value) * chunks


# ---------------------------------------------------------------------------
# open_pcm()
# ---------------------------------------------------------------------------


def test_open_pcm_writes_without_buffering(tmp_path: Path):
    """无缓冲写：`write()` 一返回，字节就已经在盘上（不等缓冲区满、不等进程退出）。

    `buffering=0` 交回的是**原始文件对象**（`io.FileIO`），它连 `buffering` 这个属性都没有 ——
    这一点正是「无缓冲」的凭据。
    """
    path = tmp_path / "meeting_audio_mic.pcm"
    with open_pcm(path) as handle:
        assert isinstance(handle, io.FileIO)
        assert not hasattr(handle, "buffering")
        handle.write(pcm_chunk())
        # 句柄还开着，此刻盘上就该有这 3200 字节
        assert path.stat().st_size == CHUNK_BYTES


def test_open_pcm_truncates_stale_leftovers(tmp_path: Path):
    """段文件**只装本段**：同名残留（上一场崩在同一目录的半截段）被截掉，不被悄悄接上。

    「续接 = 追加到旧文件末尾」是 V1 的旧口径，已被 `docs/design-intent.md` 第 9 条翻案：
    续接是下一个序号的新段文件，不追加旧文件。这里钉住翻案后的形状——第二次打开写出的
    内容从字节 0 开始，旧内容一个字节都不留。
    """
    path = tmp_path / "meeting_audio_loopback_001.pcm"
    with open_pcm(path) as handle:
        handle.write(pcm_chunk(111))
    with open_pcm(path) as handle:
        handle.write(pcm_chunk(222))
    data = path.read_bytes()
    assert len(data) == CHUNK_BYTES
    assert data == pcm_chunk(222)


# ---------------------------------------------------------------------------
# PcmArchiveWriter
# ---------------------------------------------------------------------------


def test_writer_writes_every_fed_chunk(tmp_path: Path):
    """喂几块就写几块：字节数按 32000 字节/秒对得上（核对口径）。"""
    path = tmp_path / "meeting_audio_mic.pcm"
    writer = PcmArchiveWriter(path, label="mic")
    writer.open()
    try:
        for _ in range(5):
            writer.feed(pcm_chunk())
    finally:
        writer.close()
    assert writer.bytes_written == CHUNK_BYTES * 5
    assert writer.chunks_written == 5
    assert path.read_bytes() == pcm_payload(5)


def test_writer_creates_the_file_before_any_chunk_arrives(tmp_path: Path):
    """开场就把文件建出来：未选中的那一路因此也是一个 0 字节文件（不是缺一个文件）。"""
    path = tmp_path / "meeting_audio_loopback.pcm"
    writer = PcmArchiveWriter(path, label="loopback")
    writer.open()
    try:
        assert path.exists()
        assert path.stat().st_size == 0
    finally:
        writer.close()
    assert path.stat().st_size == 0


def test_writer_writes_on_its_own_thread(tmp_path: Path):
    """写侧有自己的线程：喂块的那一方不等磁盘，写线程把块落到盘上。"""
    path = tmp_path / "meeting_audio_mic.pcm"
    writer = PcmArchiveWriter(path, label="mic")
    writer.open()
    try:
        thread_name = writer._thread.name if writer._thread is not None else ""
        assert writer._thread is not None
        assert writer._thread is not threading.current_thread()
        assert thread_name == "archive-mic"
        writer.feed(pcm_chunk())
        deadline = time.monotonic() + 2.0
        while writer.bytes_written < CHUNK_BYTES and time.monotonic() < deadline:
            time.sleep(0.01)
        assert writer.bytes_written == CHUNK_BYTES
    finally:
        writer.close()


def test_writer_close_is_idempotent_and_flushes_what_is_queued(tmp_path: Path):
    """收队：还压在队列里的块照样写完，重复 `close()` 不重复写、不抛错。"""
    path = tmp_path / "meeting_audio_loopback.pcm"
    writer = PcmArchiveWriter(path, label="loopback")
    writer.open()
    for _ in range(4):
        writer.feed(pcm_chunk())
    writer.close()
    first = path.stat().st_size
    writer.close()
    writer.close()
    assert first == CHUNK_BYTES * 4
    assert path.stat().st_size == first
    assert writer.bytes_written == CHUNK_BYTES * 4


def test_writer_two_writers_do_not_share_a_queue(tmp_path: Path):
    """两路各自一条写线程：一路喂块不会写进另一路的文件（「一路出事另一路照跑」的底子）。"""
    mic = PcmArchiveWriter(tmp_path / "meeting_audio_mic.pcm", label="mic")
    loop = PcmArchiveWriter(tmp_path / "meeting_audio_loopback.pcm", label="loopback")
    mic.open()
    loop.open()
    try:
        mic.feed(pcm_chunk(11))
        loop.feed(pcm_chunk(22))
    finally:
        mic.close()
        loop.close()
    assert mic.path.read_bytes() == pcm_chunk(11)
    assert loop.path.read_bytes() == pcm_chunk(22)


def test_writer_feed_ignores_empty_chunks(tmp_path: Path):
    """空块不入队（读侧偶尔交回空块时，字节数不该被它扰动）。"""
    writer = PcmArchiveWriter(tmp_path / "meeting_audio_mic.pcm", label="mic")
    writer.open()
    try:
        writer.feed(b"")
        writer.feed(pcm_chunk())
    finally:
        writer.close()
    assert writer.chunks_written == 1


def test_writer_silence_feed_writes_zeros_and_keeps_the_ledger_split(tmp_path: Path):
    """静音块落盘的是**等长数字零**：内容量与静音量分开记，总量是落盘字节数。

    「暂停 = 写静音」的隐私线落在写侧这一格：队列里就算投进来真音频，
    `silence=True` 的块到盘上也是零（`docs/design-intent.md` 第 8 条）。
    """
    path = tmp_path / "meeting_audio_mic_001.pcm"
    writer = PcmArchiveWriter(path, label="mic")
    writer.open()
    try:
        writer.feed(pcm_chunk(7))
        writer.feed(pcm_chunk(9), silence=True)
        writer.feed(pcm_chunk(11))
    finally:
        writer.close()

    data = path.read_bytes()
    assert len(data) == CHUNK_BYTES * 3
    assert data[:CHUNK_BYTES] == pcm_chunk(7)
    assert data[CHUNK_BYTES : 2 * CHUNK_BYTES] == bytes(CHUNK_BYTES)  # 逐样本为零
    assert data[2 * CHUNK_BYTES :] == pcm_chunk(11)
    assert writer.bytes_written == CHUNK_BYTES * 3
    assert writer.content_bytes == CHUNK_BYTES * 2
    assert writer.silence_bytes == CHUNK_BYTES


def test_writer_survives_a_simulated_hard_kill(tmp_path: Path):
    """模拟强杀：不调 `close()`，句柄随进程消失 —— 无缓冲写让最后一个块也已经在盘上。

    这里用「不关句柄、直接从盘上读」来验：无缓冲（`buffering=0`）时前一拍的块已经落盘，
    丢的只会是还在队列里的那一块（§3 的取舍）。
    """
    path = tmp_path / "meeting_audio_mic.pcm"
    writer = PcmArchiveWriter(path, label="mic")
    writer.open()
    for _ in range(3):
        writer.feed(pcm_chunk())
    deadline = time.monotonic() + 2.0
    while writer.bytes_written < CHUNK_BYTES * 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    # 不调 close()：句柄还开着，盘上已经是完整的三块
    assert path.stat().st_size == CHUNK_BYTES * 3
    writer.close()


# ---------------------------------------------------------------------------
# convert_to_mp3()
# ---------------------------------------------------------------------------


def test_mp3_path_swaps_the_extension(tmp_path: Path):
    """产物与 PCM 同目录同名换扩展名（`meeting_audio_mic.pcm` → `.mp3`）。"""
    pcm = tmp_path / "meeting_audio_mic.pcm"
    assert mp3_path_for(pcm) == tmp_path / "meeting_audio_mic.mp3"
    assert mp3_path_for("meeting_audio_loopback.pcm").name == "meeting_audio_loopback.mp3"


def test_non_empty_pcm_becomes_a_decodable_mp3(tmp_path: Path):
    """非空 PCM 转出**可解**的 MP3：`ffmpeg` 读得回来，时长与 PCM 对得上。"""
    pcm = tmp_path / "meeting_audio_mic.pcm"
    # 1 秒 440 Hz 正弦（16k / int16）
    t = np.arange(16000, dtype=np.float64)
    pcm.write_bytes((12000 * np.sin(2 * np.pi * 440.0 * t / 16000.0)).astype(np.int16).tobytes())
    mp3 = mp3_path_for(pcm)

    assert convert_to_mp3(pcm, mp3) is True
    assert mp3.exists()
    assert mp3.stat().st_size > 0

    from pydub import AudioSegment

    decoded = AudioSegment.from_file(mp3)
    assert abs(len(decoded) - 1000) < 150  # 1 秒 ±150ms（MP3 帧边界）


def test_transcode_keeps_the_pcm(tmp_path: Path):
    """**转完不删 PCM**（与 V1 的唯一差别）：`session.json` 声明的是那份 PCM。"""
    pcm = tmp_path / "meeting_audio_loopback.pcm"
    payload = pcm_payload(4)
    pcm.write_bytes(payload)
    mp3 = mp3_path_for(pcm)

    assert convert_to_mp3(pcm, mp3) is True
    assert pcm.exists()
    assert pcm.read_bytes() == payload


def test_empty_pcm_produces_no_mp3_and_stays(tmp_path: Path):
    """空文件（该源无音频输入）：不产出 MP3，0 字节的 PCM 也留在场面上。"""
    pcm = tmp_path / "meeting_audio_mic.pcm"
    pcm.write_bytes(b"")
    mp3 = mp3_path_for(pcm)

    assert convert_to_mp3(pcm, mp3) is False
    assert not mp3.exists()
    assert pcm.exists()
    assert pcm.stat().st_size == 0


def test_one_byte_pcm_counts_as_empty(tmp_path: Path):
    """残缺到连一个 int16 样本都装不下的文件按空处理（`EMPTY_PCM_BYTES` 的判据）。"""
    pcm = tmp_path / "meeting_audio_mic.pcm"
    pcm.write_bytes(b"\x01")
    assert pcm.stat().st_size < EMPTY_PCM_BYTES
    assert convert_to_mp3(pcm, mp3_path_for(pcm)) is False
    assert not mp3_path_for(pcm).exists()


def test_a_missing_pcm_raises_and_leaves_nothing_behind(tmp_path: Path):
    """PCM 不在时原样抛错（调用方据此把该步记 `failed`），不产出半截 MP3。"""
    pcm = tmp_path / "meeting_audio_mic.pcm"
    mp3 = mp3_path_for(pcm)
    with pytest.raises(OSError):
        convert_to_mp3(pcm, mp3)
    assert not mp3.exists()


def test_ffmpeg_failure_keeps_the_pcm_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`ffmpeg` 不在 / 转码抛错：**PCM 原样留下**，异常如实往上抛（该步记 `failed`）。

    假件挂在 `pydub.AudioSegment.from_raw` 上（`convert_to_mp3()` 是函数内 import，
    拿到的是模块属性，所以这一手改得动）—— 这就是「`ffmpeg` 缺席时 `pydub` 抛错」那一档。
    """
    import pydub

    pcm = tmp_path / "meeting_audio_mic.pcm"
    payload = pcm_payload(3)
    pcm.write_bytes(payload)

    def boom(*args, **kwargs):
        raise FileNotFoundError("假件：ffmpeg 不在 PATH 上")

    monkeypatch.setattr(pydub.AudioSegment, "from_raw", staticmethod(boom))
    with pytest.raises(FileNotFoundError):
        convert_to_mp3(pcm, mp3_path_for(pcm))
    assert pcm.read_bytes() == payload
    assert not mp3_path_for(pcm).exists()


def test_transcode_bitrate_default_is_v1_sixty_four_k(tmp_path: Path):
    """码率默认值照 V1（`64k`），产物落在同名 `.mp3` 上。"""
    assert MP3_BITRATE == "64k"
    pcm = tmp_path / "meeting_audio_mic.pcm"
    pcm.write_bytes(pcm_payload(8))
    mp3 = mp3_path_for(pcm)
    assert convert_to_mp3(pcm, mp3) is True
    assert mp3.exists()
    # 64k 的 1 秒音频体积在 8KB 上下（粗口径：只验「不是空壳、也不是原始 PCM」）
    assert 1000 < mp3.stat().st_size < pcm.stat().st_size


def test_no_temporary_files_are_left_in_the_session_dir(tmp_path: Path):
    """转码不在场次目录里留临时文件（产物只有 `.pcm` 与 `.mp3` 两份）。"""
    pcm = tmp_path / "meeting_audio_mic.pcm"
    pcm.write_bytes(pcm_payload(4))
    convert_to_mp3(pcm, mp3_path_for(pcm))
    assert sorted(item.name for item in tmp_path.iterdir()) == [
        "meeting_audio_mic.mp3",
        "meeting_audio_mic.pcm",
    ]


def test_open_pcm_keeps_the_bytes_identical_after_a_round_trip(tmp_path: Path):
    """写进去的字节与读回来的字节一模一样（裸 PCM 无头，追加安全无损）。"""
    path = tmp_path / "meeting_audio_mic.pcm"
    payload = bytes(range(256)) * 4
    with open_pcm(path) as handle:
        handle.write(payload)
    assert path.read_bytes() == payload


# ---------------------------------------------------------------------------
# 与 V1 的四处差别：每一处都逐条钉住（搬运动作的「diff 一遍」那一节）
# ---------------------------------------------------------------------------


def test_from_raw_gets_the_frozen_pcm_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`from_raw` 拿到的三个形状参数逐字是 V1 那一份（16k / 2 字节 / 单声道）。

    V1 原文是 `sample_width=2, frame_rate=SAMPLE_RATE, channels=1`；这里把常量写成了
    `SAMPLE_WIDTH` / `CHANNELS`，值必须与 V1 相同 —— 参数一旦漂移，转出来的 MP3 会
    变速或变调，而听感上的错很难被别的断言发现。
    """
    import pydub

    seen: dict[str, object] = {}

    def fake_from_raw(path, **kwargs):
        seen.update(kwargs)
        return _NullSegment()

    class _NullSegment:
        def export(self, *args, **kwargs):  # pragma: no cover - 只验参数
            return None

    pcm = tmp_path / "meeting_audio_mic.pcm"
    pcm.write_bytes(pcm_payload(2))
    monkeypatch.setattr(pydub.AudioSegment, "from_raw", staticmethod(fake_from_raw))
    assert convert_to_mp3(pcm, mp3_path_for(pcm)) is True
    assert seen == {"sample_width": 2, "frame_rate": 16000, "channels": 1}


def test_transcode_never_unlinks_the_pcm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """**转码这一路一次都不 `unlink`**（V1 在结尾与空文件两处各删一次）。

    「转完不删 PCM」是 §3 里预先批准的那一处有意改动：`session.json` 的
    `sources[*].archive` 声明的是那份 `.pcm`。这里把 `Path.unlink` 全程换成一个记录器，
    转码走完一遍之后它必须是空的 —— 空文件那一档同理。
    """
    calls: list[str] = []
    real_unlink = Path.unlink

    def spy(self, *args, **kwargs):
        calls.append(str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", spy)

    filled = tmp_path / "meeting_audio_mic.pcm"
    filled.write_bytes(pcm_payload(4))
    assert convert_to_mp3(filled, mp3_path_for(filled)) is True

    empty = tmp_path / "meeting_audio_loopback.pcm"
    empty.write_bytes(b"")
    assert convert_to_mp3(empty, mp3_path_for(empty)) is False

    assert calls == []
    assert filled.exists() and empty.exists()
