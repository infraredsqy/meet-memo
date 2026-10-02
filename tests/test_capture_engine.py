"""`tests/test_capture_engine.py` —— 会话采集引擎（`meeting/capture/engine.py`）。

**全程用注入的假源**（`CaptureSource` 协议的实现），一个真声卡入口都不碰：假源能造出
「每块内容可指定、第 N 块抛错、块数可指定」这几种情形，覆盖面比录几秒真声音更全。

验的是生产代码这条路径：读线程、写线程、电平刻度、收尾落盘全部是真的，换掉的只有最外面
那一层声卡库。五块：

  1. 归档对账：两路 PCM 都长起来，字节数按 **32000 字节/秒** 对得上喂进去的块数；
  2. 单路死亡：一路的源抛错 → 该路一条 `SourceLost`，**另一路的字节数继续增长**；
  3. `mode` 过滤：`mode="mic"` 时 loopback 不读、不发帧，但**整段时间写静音**
     （两路时间轴等长，`docs/design-intent.md` 第 9 条）；
  4. 暂停与关一路：暂停 = 所有路写数字静音（逐样本为零）、不录内容、段不切段；
     用户主动关一路 = 那一路写静音、**不报 `SourceLost`**，另一路照常；
  5. 段账与电平刻度：段起止戳、在录时长（内容字节 ÷ 32000）、权威时长 = 各段之和；
     每 `frame_interval` 秒**无条件**发一帧（静音时也发），`SourceFacts` 跟着走。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from meeting.backends.base import LevelFrame, SourceFacts, SourceLost
from meeting.capture.engine import (
    CAPTURE_MODES,
    SILENCE_CHUNK_BYTES,
    SOURCE_IDS,
    CaptureSession,
    sources_for_mode,
)
from meeting.session import archive_name

#: 慢档标记：`pytest.mark.slow` 的归位判据与实测数据见 `docs/service.md` 第 7 章。
pytestmark = pytest.mark.slow

#: 一块 0.1 秒的 16k / 单声道 / int16 样本（3200 字节；一秒 32000 字节的由来）
CHUNK_FRAMES = 1600
CHUNK_BYTES = CHUNK_FRAMES * 2

#: 「安静的一路照样发帧」那条用例的等待口径：至少等到的电平帧数，与等待的超时（秒）。
#: 本机实测 0.05 的 `frame_interval` 约 15 拍/秒（`Event.wait()` 被 Windows 定时器粒度
#: 上取整到 ~62ms），等到 3 帧约 0.19 秒 —— 5 秒超时有 ~26 倍余量。
MIN_SILENCE_FRAMES = 3
FRAME_WAIT_TIMEOUT_SECONDS = 5.0


def block(value: int = 1200, frames: int = CHUNK_FRAMES) -> bytes:
    """一块可预期的 int16 裸 PCM（每个样本都是 `value`）。"""
    return np.full(frames, value, dtype=np.int16).tobytes()


class FakeCaptureSource:
    """假采集源：按计划给块、可指定在第几块抛错、可指定给多少块（喂完就空转）。

    `read()` 的三种回法都对得上 `CaptureSource` 协议：给块、给空（这一拍没有新块）、
    抛错（这一路出事）。

    `start()` 与真源同一档：`auto_start=True`（后端那边造出来的源）时由调用方显式起，
    `auto_start=False`（引擎自己起的那些用例）时 `start()` 什么也不做 —— 引擎的读线程
    本来就不等源起没起，假源因此两种用法都顺手。
    """

    def __init__(
        self,
        name: str,
        *,
        chunk: bytes | None = None,
        chunks: int | None = None,
        error_at: int | None = None,
        error: BaseException | None = None,
        poll_seconds: float = 0.005,
        pace_seconds: float = 0.0,
        auto_start: bool = True,
    ) -> None:
        self.name = name
        self.chunk = block() if chunk is None else chunk
        self.chunks = chunks
        self.error_at = error_at
        self.error = error or RuntimeError(f"假源 {name} 按计划炸了")
        self.poll_seconds = poll_seconds
        #: 交一块之后歇多久（秒）。0 = 能多快给多快（引擎自己的用例要的就是这个）；
        #: 端到端那些用例把它设成 0.1，与真采集一个块 0.1 秒的节奏对齐 —— 否则假源会以
        #: 每秒几百兆的速度把盘写满，收尾的 join 也就不是在验收尾了。
        self.pace_seconds = pace_seconds
        self.auto_start = auto_start
        self.fed = 0
        self.reads = 0
        self.started = False
        self.stopped = False
        self._stop = threading.Event()
        self._latest = np.empty(0, dtype=np.int16)

    # -- CaptureSource 协议 ------------------------------------------------

    def start(self) -> None:
        """起这一路的采集（后端会在造完源之后调它；引擎自己起的那些用例不调）。"""
        self.started = True
        if not self.auto_start:
            return

    def read(self, timeout: float) -> bytes | None:
        self.reads += 1
        if self._stop.is_set():
            return None
        if self.error_at is not None and self.fed >= self.error_at:
            raise self.error
        if self.chunks is not None and self.fed >= self.chunks:
            time.sleep(min(timeout, self.poll_seconds))
            return b""
        self.fed += 1
        samples = np.frombuffer(self.chunk, dtype=np.int16)
        self._latest = samples
        if self.pace_seconds:
            time.sleep(self.pace_seconds)
        return self.chunk

    def latest_samples(self) -> np.ndarray:
        return self._latest

    def stop(self) -> None:
        self.stopped = True
        self._stop.set()

    # -- 给用例看的读数 ----------------------------------------------------

    @property
    def bytes_fed(self) -> int:
        return self.fed * len(self.chunk)


def make_session(
    tmp_path: Path,
    *,
    mode: str = "both",
    frame_interval: float = 0.05,
    mic: FakeCaptureSource | None = None,
    loopback: FakeCaptureSource | None = None,
    requested: tuple[str, ...] | None = None,
):
    """起一场（假源 + 真引擎）：返回 `(会话, 事实表, 两路的假源)`。"""
    facts: list[object] = []
    sources = {
        "mic": mic if mic is not None else FakeCaptureSource("mic", auto_start=False),
        "loopback": loopback
        if loopback is not None
        else FakeCaptureSource("loopback", auto_start=False),
    }
    session = CaptureSession(
        mode=mode,
        session_dir=str(tmp_path),
        sources=sources,
        emit=facts.append,
        frame_interval=frame_interval,
        requested=requested,
    )
    return session, facts, sources


def only(facts: list[object], kind: type) -> list[object]:
    return [fact for fact in facts if isinstance(fact, kind)]


def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------------
# ① 归档对账
# ---------------------------------------------------------------------------


def test_sources_for_mode_is_the_frozen_domain():
    """`both` / `mic` / `loopback` 三档；`both` 就是两路。"""
    assert CAPTURE_MODES == ("both", "mic", "loopback")
    assert sources_for_mode("both") == SOURCE_IDS
    assert sources_for_mode("mic") == ("mic",)
    assert sources_for_mode("loopback") == ("loopback",)


def test_both_routes_fill_their_own_archive(tmp_path: Path):
    """两路各自长起来：字节数 = 块数 × 3200（一秒 32000 字节的对账口径）。"""
    mic = FakeCaptureSource("mic", chunks=6)
    loop = FakeCaptureSource("loopback", chunks=4)
    session, _facts, _sources = make_session(tmp_path, mic=mic, loopback=loop)
    session.start()
    assert wait_for(lambda: mic.fed >= 6 and loop.fed >= 4)
    session.finalize()

    assert session.bytes_written("mic") == 6 * CHUNK_BYTES
    assert session.bytes_written("loopback") == 4 * CHUNK_BYTES
    assert (tmp_path / archive_name("mic")).stat().st_size == 6 * CHUNK_BYTES
    assert (tmp_path / archive_name("loopback")).stat().st_size == 4 * CHUNK_BYTES


def test_archive_bytes_match_what_was_fed(tmp_path: Path):
    """盘上的字节与喂进去的字节逐字相同（裸 PCM，没有头、没有填充）。"""
    payload = np.arange(CHUNK_FRAMES, dtype=np.int16).tobytes()
    mic = FakeCaptureSource("mic", chunk=payload, chunks=3)
    session, _facts, _sources = make_session(tmp_path, mode="mic", mic=mic)
    session.start()
    assert wait_for(lambda: session.bytes_written("mic") >= 3 * CHUNK_BYTES)
    session.finalize()
    assert (tmp_path / archive_name("mic")).read_bytes() == payload * 3


def test_second_of_audio_is_thirty_two_kilobytes(tmp_path: Path):
    """一秒的 16k / int16 / 单声道是 32000 字节：10 块（1 秒）正好 32000 字节。"""
    mic = FakeCaptureSource("mic", chunks=10)
    session, _facts, _sources = make_session(tmp_path, mode="mic", mic=mic)
    session.start()
    assert wait_for(lambda: mic.fed >= 10)
    session.finalize()
    assert session.bytes_written("mic") == 32000


def test_both_archives_exist_from_the_first_moment(tmp_path: Path):
    """开场那一刻两路归档文件就都在（未选中的那一路是 0 字节）。"""
    session, _facts, _sources = make_session(tmp_path, mode="mic")
    for source in SOURCE_IDS:
        assert (tmp_path / archive_name(source)).exists()
    assert (tmp_path / archive_name("mic")).stat().st_size == 0
    session.stop()


# ---------------------------------------------------------------------------
# ② 单路死亡：另一路照跑
# ---------------------------------------------------------------------------


def test_one_dead_route_reports_source_lost_and_the_other_keeps_writing(tmp_path: Path):
    """一路的源读崩：该路一条 `SourceLost`，另一路的归档**字节数继续增长**。"""
    mic = FakeCaptureSource("mic")
    loop = FakeCaptureSource("loopback", error_at=3)
    session, facts, _sources = make_session(tmp_path, mic=mic, loopback=loop)
    session.start()

    assert wait_for(lambda: len(only(facts, SourceLost)) == 1, timeout=3.0)
    assert only(facts, SourceLost)[0].source == "loopback"
    assert "假源 loopback 按计划炸了" in only(facts, SourceLost)[0].reason

    # 死亡那一刻的字节数，之后还要继续涨（mic 那一路没受影响）
    after_death = session.bytes_written("mic")
    assert wait_for(lambda: session.bytes_written("mic") > after_death, timeout=3.0)
    before_finalize = session.bytes_written("mic")
    session.finalize()
    assert session.bytes_written("mic") >= before_finalize
    assert session.bytes_written("loopback") == 3 * CHUNK_BYTES


def test_a_dead_route_leaves_a_decodable_partial_archive(tmp_path: Path):
    """死掉那一路的 PCM 照样可解（写下去的那三块原样留在盘上）。"""
    loop = FakeCaptureSource("loopback", chunk=block(7), error_at=3)
    session, facts, _sources = make_session(
        tmp_path, mode="loopback", loopback=loop
    )
    session.start()
    assert wait_for(lambda: len(only(facts, SourceLost)) == 1)
    session.finalize()
    data = (tmp_path / archive_name("loopback")).read_bytes()
    assert data == block(7) * 3
    assert len(data) % 2 == 0  # int16 对齐，pydub 读得回来


def test_start_does_not_raise_when_a_route_dies(tmp_path: Path):
    """一路死掉不牵连开场：`start()` 不抛错，另一路照采。"""
    mic = FakeCaptureSource("mic", chunks=4)
    loop = FakeCaptureSource("loopback", error_at=0)
    session, facts, _sources = make_session(tmp_path, mic=mic, loopback=loop)
    session.start()  # 不抛
    assert wait_for(lambda: len(only(facts, SourceLost)) == 1)
    assert session.bytes_written("mic") > 0
    session.finalize()


def test_mark_lost_is_reported_once_per_route(tmp_path: Path):
    """某一路本场没起来：由后端调 `mark_lost()` 报一条 `SourceLost`（另一路不受影响）。"""
    mic = FakeCaptureSource("mic", chunks=3)
    loop = FakeCaptureSource("loopback", chunks=3)
    session, facts, _sources = make_session(tmp_path, mic=mic, loopback=loop)
    session.begin_source("mic", mic)
    session.begin_source("loopback", loop)
    session.mark_lost("loopback", "这一路的端点打不开（假件）")
    session.start()
    assert wait_for(lambda: mic.fed >= 3)
    session.finalize()
    lost = only(facts, SourceLost)
    assert len(lost) == 1
    assert lost[0].source == "loopback"
    assert "端点打不开" in lost[0].reason
    assert session.bytes_written("mic") > 0


def test_a_dead_route_stops_reporting_itself_alive(tmp_path: Path):
    """死掉之后 `SourceFacts.stream_alive` 如实转假（事实不撒谎）。"""
    loop = FakeCaptureSource("loopback", error_at=1)
    session, facts, _sources = make_session(tmp_path, mode="loopback", loopback=loop)
    session.start()
    assert wait_for(lambda: len(only(facts, SourceLost)) == 1)
    assert wait_for(
        lambda: any(
            isinstance(fact, SourceFacts)
            and fact.source == "loopback"
            and fact.stream_alive is False
            for fact in facts
        )
    )
    session.finalize()


# ---------------------------------------------------------------------------
# ③ mode 过滤
# ---------------------------------------------------------------------------


def test_mic_mode_leaves_the_loopback_route_alone(tmp_path: Path):
    """`mode="mic"`：loopback 不读、不发帧，但**整段时间写静音**（两路时间轴等长）。

    「某一路没开」也是「那一路写静音」（`docs/design-intent.md` 第 9 条）：未选那一路的
    文件不再是 0 字节，落盘的全是数字零，且不记内容账——权威时长只由 mic 那一路的内容
    字节说话。
    """
    mic = FakeCaptureSource("mic", chunks=4)
    loop = FakeCaptureSource("loopback")
    session, facts, _sources = make_session(tmp_path, mode="mic", mic=mic, loopback=loop)
    session.start()
    assert wait_for(lambda: mic.fed >= 4)
    # 静音节拍器按真实时长写零（等的是时间本身，但判据挂在可观察的字节数上）
    assert wait_for(lambda: session.silence_bytes("loopback") >= 3 * SILENCE_CHUNK_BYTES)
    session.finalize()

    assert loop.reads == 0
    assert loop.fed == 0
    # 时间轴等长：未选那一路落的全是静音字节，且与它的字节数对上账
    assert session.bytes_written("loopback") > 0
    assert session.silence_bytes("loopback") == session.bytes_written("loopback")
    assert session.content_bytes("loopback") == 0
    assert (tmp_path / archive_name("loopback")).stat().st_size == session.bytes_written("loopback")
    loop_data = (tmp_path / archive_name("loopback")).read_bytes()
    assert loop_data == bytes(len(loop_data))  # 逐样本为零
    assert {fact.source for fact in only(facts, LevelFrame)} == {"mic"}
    # 权威时长只数该采那一路的内容：mic 有内容、loopback 全静音
    assert session.content_bytes("mic") > 0
    records = session.segment_records()
    assert records[0]["sources"] == ["mic"]
    assert records[0]["duration_seconds"] == pytest.approx(
        session.content_bytes("mic") / 32000, abs=0.001
    )


def test_loopback_mode_leaves_the_mic_route_alone(tmp_path: Path):
    """反过来一样：`mode="loopback"` 时 mic 那一路一个块都不读（整段时间写静音）。"""
    mic = FakeCaptureSource("mic")
    loop = FakeCaptureSource("loopback", chunks=4)
    session, facts, _sources = make_session(
        tmp_path, mode="loopback", mic=mic, loopback=loop
    )
    session.start()
    assert wait_for(lambda: loop.fed >= 4)
    assert wait_for(lambda: session.silence_bytes("mic") >= 3 * SILENCE_CHUNK_BYTES)
    session.finalize()

    assert mic.reads == 0
    assert session.content_bytes("mic") == 0
    assert session.bytes_written("mic") == session.silence_bytes("mic") > 0
    assert {fact.source for fact in only(facts, LevelFrame)} == {"loopback"}


def test_an_unselected_route_gets_only_a_silence_pacer(tmp_path: Path):
    """未选中的那一路没有读线程、也没有刻度线程，只有**静音节拍器**那一条。"""
    session, _facts, _sources = make_session(tmp_path, mode="mic")
    session.start()
    time.sleep(0.05)
    names = [thread.name for thread in session.workers["loopback"]._threads]
    assert names == ["capture-silence-loopback"]
    assert session.workers["mic"]._threads != []
    session.stop()


# ---------------------------------------------------------------------------
# ③½ 暂停与关一路：写静音，不录内容；段不切段
# ---------------------------------------------------------------------------


def test_pause_writes_silence_and_does_not_record_content(tmp_path: Path):
    """暂停期间两路都写数字零、内容账停住；恢复后内容照录；**段不切段**。

    盘上对账**在收尾结算之后**做（不跟写线程的记账时序赛跑）：内容块全是源给的那一块、
    静音全是零且总量等于写侧的静音账——两句话合起来就是「暂停 = 写静音，不录内容」。
    """
    mic = FakeCaptureSource("mic", chunk=block(900), pace_seconds=0.005)
    loop = FakeCaptureSource("loopback", chunk=block(3100), pace_seconds=0.005)
    session, _facts, _sources = make_session(tmp_path, mic=mic, loopback=loop)
    session.start()
    assert wait_for(lambda: session.content_bytes("mic") >= 3 * CHUNK_BYTES)
    assert not session.paused
    content_before_pause = session.content_bytes("mic")
    session.pause()
    assert session.paused
    assert wait_for(
        lambda: session.silence_bytes("mic") >= 3 * CHUNK_BYTES,
        timeout=FRAME_WAIT_TIMEOUT_SECONDS,
    )
    session.resume()
    assert not session.paused
    assert wait_for(lambda: session.content_bytes("mic") > content_before_pause)
    session.finalize()

    # 盘上对账（**结算后**对，不跟写线程的记账时序赛跑）：内容块都是源给的那一块、
    # 暂停静音逐样本为零且总量等于静音账——暂停只是把中间那截换成了零
    data = (tmp_path / archive_name("mic")).read_bytes()
    content_total = session.content_bytes("mic")
    silence_total = session.silence_bytes("mic")
    assert silence_total >= 3 * CHUNK_BYTES
    assert content_total % CHUNK_BYTES == 0 and silence_total % CHUNK_BYTES == 0
    assert data.count(block(900)) * CHUNK_BYTES + silence_total == len(data)
    assert data.replace(block(900), b"") == bytes(silence_total), (
        "暂停窗口必须逐样本为零（第 8 条的隐私线）"
    )
    # 账目自洽：总账 = 内容 + 静音；暂停没有把段切开
    assert session.bytes_written("mic") == content_total + silence_total
    records = session.segment_records()
    assert len(records) == 1
    assert records[0]["index"] == 1
    assert records[0]["started_at"] and records[0]["ended_at"]
    pauses = records[0]["pauses"]
    assert len(pauses) == 1
    assert pauses[0]["start"] and pauses[0]["end"]
    # 在录时长只数内容：暂停的静音一格都不算；取两路内容的较大者（同一段时间轴）
    expected_duration = max(
        session.content_bytes(source) for source in ("mic", "loopback")
    ) / 32000
    assert records[0]["duration_seconds"] == pytest.approx(expected_duration, abs=0.001)
    assert session.audio_duration_seconds() == records[0]["duration_seconds"]


def test_pause_is_idempotent_and_resume_without_pause_is_a_no_op(tmp_path: Path):
    """重复 `pause()` 只记一条区间；没暂停时 `resume()` 什么都不改。"""
    mic = FakeCaptureSource("mic", chunk=block(900), pace_seconds=0.005)
    session, _facts, _sources = make_session(tmp_path, mode="mic", mic=mic)
    session.start()
    assert wait_for(lambda: session.content_bytes("mic") >= CHUNK_BYTES)
    session.pause()
    session.pause()
    assert wait_for(lambda: session.silence_bytes("mic") >= CHUNK_BYTES)
    session.resume()
    session.resume()
    session.finalize()
    assert len(session.segment_records()[0]["pauses"]) == 1


def test_pause_before_start_or_after_finalize_is_a_no_op(tmp_path: Path):
    """没起采 / 已收尾时 `pause()` 不动作（不记区间、不留半个状态）。"""
    mic = FakeCaptureSource("mic", chunks=3)
    session, _facts, _sources = make_session(tmp_path, mode="mic", mic=mic)
    session.pause()
    assert not session.paused
    session.start()
    assert wait_for(lambda: mic.fed >= 3)
    session.finalize()
    session.pause()
    assert not session.paused
    assert session.segment_records()[0]["pauses"] == []


def test_deactivating_a_route_writes_silence_without_source_lost(tmp_path: Path):
    """用户主动关一路：那一路改写静音、**不报 `SourceLost`**，另一路照常录内容。"""
    mic = FakeCaptureSource("mic", chunk=block(900), pace_seconds=0.005)
    loop = FakeCaptureSource("loopback", chunk=block(3100), pace_seconds=0.005)
    session, facts, sources = make_session(tmp_path, mic=mic, loopback=loop)
    session.start()
    assert wait_for(lambda: session.content_bytes("loopback") >= 2 * CHUNK_BYTES)
    mic_content_at_close = session.content_bytes("mic")
    session.deactivate_source("loopback")
    session.deactivate_source("loopback")  # 幂等
    assert sources["loopback"].stopped is True  # 真源被释放
    assert session.workers["loopback"].user_closed is True
    # 静音在涨（时间轴不断），mic 的内容也在涨
    assert wait_for(
        lambda: session.silence_bytes("loopback") >= 3 * CHUNK_BYTES,
        timeout=FRAME_WAIT_TIMEOUT_SECONDS,
    )
    assert wait_for(lambda: session.content_bytes("mic") > mic_content_at_close)
    session.finalize()

    assert only(facts, SourceLost) == []  # 主动关不报故障（与「真死」区分的地方）
    loop_data = (tmp_path / archive_name("loopback")).read_bytes()
    tail = loop_data[-3 * CHUNK_BYTES :]
    assert tail == bytes(len(tail))  # 关掉之后写的是数字零
    # mic 全程内容、loopback 关掉后全静音：权威时长 = mic 的内容时长
    assert session.content_bytes("loopback") < session.bytes_written("loopback")
    assert session.segment_records()[0]["duration_seconds"] == pytest.approx(
        session.content_bytes("mic") / 32000, abs=0.001
    )


def test_segment_account_before_finalize_has_no_closing_stamp(tmp_path: Path):
    """还在录时段账没有终点戳、权威时长为 0（没写完的段没有账可结）。"""
    mic = FakeCaptureSource("mic", chunk=block(900), pace_seconds=0.005)
    session, _facts, _sources = make_session(tmp_path, mode="mic", mic=mic)
    session.start()
    assert wait_for(lambda: session.content_bytes("mic") >= CHUNK_BYTES)
    record = session.segment_records()[0]
    assert record["started_at"] and record["ended_at"] is None
    assert record["duration_seconds"] is None
    assert record["sources"] == ["mic"]
    assert session.audio_duration_seconds() == 0.0
    assert session.archives_for("mic") == [archive_name("mic", 1)]
    session.finalize()
    assert session.segment_records()[0]["ended_at"] is not None
    assert session.audio_duration_seconds() > 0.0


# ---------------------------------------------------------------------------
# ④ 电平刻度与事实
# ---------------------------------------------------------------------------


def test_level_frames_keep_coming_on_their_own_rhythm(tmp_path: Path):
    """每 `frame_interval` 秒一帧、**一路一帧**：停一会儿就能看到好几帧。"""
    mic = FakeCaptureSource("mic", chunks=4)
    session, facts, _sources = make_session(tmp_path, mode="mic", mic=mic, frame_interval=0.02)
    session.start()
    time.sleep(0.25)
    session.finalize()

    frames = only(facts, LevelFrame)
    assert len(frames) >= 5
    assert all(frame.source == "mic" for frame in frames)
    stamps = [frame.ts for frame in frames]
    assert stamps == sorted(stamps)  # 采集侧自己的钟：单调
    assert stamps[-1] - stamps[0] >= 0.08


def test_a_quiet_route_keeps_sending_silence_frames(tmp_path: Path):
    """静下来的那一路**照样发帧**（发的是 `-120 dBFS`）：停发会被误判成「采集停了」。

    这条守的是承重契约：服务侧拿 `max(2.0s, 3 × frame_interval)` 判 `recording_alive`，
    「没有波形」与「采集停了」在这一处必须分得开（R2 要消灭的正是后者那种静默失败）。

    **等法按事件驱动**：早先这一条是 `frame_interval=0.02` + `time.sleep(0.2)` + `>= 4`，
    在 Windows 上站不住 —— `_tick_loop()` 用 `self._stop.wait(frame_interval)` 打点，
    `Event.wait()` 受 15.6ms 定时器粒度影响**只会上取整**（本机实测 `wait(0.02)` 实际睡
    ~23–32ms、`wait(0.05)` ~62ms），0.2 秒窗口里只够 3–6 拍，于是这条会随机红。现在改成
    「帧数到了就返回」的等待，并按实测节拍留足余量：0.05 的间隔本机实测约 15 拍/秒，
    等到 3 帧约需 0.19 秒，5 秒超时有 ~26 倍余量。静音源也给了节拍（`pace_seconds`）：
    不带节拍的假源会以远快于真采集的速度灌块、与打点线程抢 GIL。
    """
    #: 一块全零素材：源还在给块，只是没有波形。节拍照真采集一个块 0.1 秒的节奏走。
    silent = FakeCaptureSource("mic", chunk=block(0), pace_seconds=0.1)
    session, facts, _sources = make_session(tmp_path, mode="mic", mic=silent, frame_interval=0.05)
    session.start()
    try:
        # 帧跟着时间走：读块被节拍压着，这里等的只是打点线程。
        assert wait_for(
            lambda: len(only(facts, LevelFrame)) >= MIN_SILENCE_FRAMES,
            timeout=FRAME_WAIT_TIMEOUT_SECONDS,
        ), (
            f"{FRAME_WAIT_TIMEOUT_SECONDS:.1f} 秒里只等到 "
            f"{len(only(facts, LevelFrame))} 帧电平事实（阈值 {MIN_SILENCE_FRAMES}）"
        )
    finally:
        session.finalize()

    frames = only(facts, LevelFrame)
    assert all(frame.dbfs == -120.0 for frame in frames)
    assert all(frame.peak == -120.0 for frame in frames)
    assert all(frame.bar == -120.0 for frame in frames)
    # 电平帧真的随时间在发（最后一帧与第一帧之间隔着实测节拍量级的时间）
    assert frames[-1].ts - frames[0].ts >= 0.06
    # ……而源一直在供上全零块：静音这一档里素材照给，只是没有波形
    assert silent.fed >= 2


def test_a_route_with_no_chunks_at_all_still_ticks(tmp_path: Path):
    """一路一个块都没来（还没出声）：刻度照走，发的是静音帧。"""
    quiet = FakeCaptureSource("mic", chunks=0)
    session, facts, _sources = make_session(tmp_path, mode="mic", mic=quiet, frame_interval=0.02)
    session.start()
    time.sleep(0.2)
    session.finalize()

    frames = only(facts, LevelFrame)
    assert len(frames) >= 4
    assert {frame.dbfs for frame in frames} == {-120.0}
    assert quiet.fed == 0


def test_frames_carry_three_dbfs_readings_and_a_monotonic_clock(tmp_path: Path):
    """一帧三个数（`dbfs` / `peak` / `bar`）+ 采集侧的钟；`bar` 是两者的大者。"""
    loud = FakeCaptureSource("mic", chunk=block(20000), chunks=6)
    session, facts, _sources = make_session(
        tmp_path, mode="mic", mic=loud, frame_interval=0.02
    )
    session.start()
    assert wait_for(lambda: loud.fed >= 6)
    time.sleep(0.06)
    session.finalize()

    frames = only(facts, LevelFrame)
    assert frames
    for frame in frames:
        assert isinstance(frame.ts, float)
        assert frame.dbfs <= 0.0 and frame.peak <= 0.0
        assert frame.bar == max(frame.dbfs, frame.peak)
    # 有素材的那几帧不是静音读数（20000 的常值块 ≈ -4.3 dBFS）
    assert any(frame.peak > -10.0 for frame in frames)


def test_every_level_frame_is_followed_by_a_liveness_fact(tmp_path: Path):
    """电平帧之外还跟着流存活事实（`SourceFacts`），两路各自一条。"""
    mic = FakeCaptureSource("mic", chunks=4)
    loop = FakeCaptureSource("loopback", chunks=4)
    session, facts, _sources = make_session(
        tmp_path, mic=mic, loopback=loop, frame_interval=0.02
    )
    session.start()
    time.sleep(0.15)
    session.finalize()

    levels = only(facts, LevelFrame)
    alive = [fact for fact in only(facts, SourceFacts) if fact.stream_alive is True]
    assert levels
    assert len(alive) >= len(levels)
    assert {fact.source for fact in alive} == set(SOURCE_IDS)


def test_facts_only_come_on_the_tick(tmp_path: Path):
    """刻度之外不发事实：读侧取块不产生任何帧（帧节奏由刻度线程一处保证）。"""
    mic = FakeCaptureSource("mic", chunks=2)
    session, facts, _sources = make_session(tmp_path, mode="mic", mic=mic, frame_interval=5.0)
    session.start()
    assert wait_for(lambda: mic.fed >= 2)
    session.finalize()
    assert only(facts, LevelFrame) == []
    assert only(facts, SourceFacts) == []
    assert session.bytes_written("mic") == 2 * CHUNK_BYTES


# ---------------------------------------------------------------------------
# 收尾：finalize / transcode / stop
# ---------------------------------------------------------------------------


def test_finalize_stops_the_sources_and_the_threads(tmp_path: Path):
    """`finalize()` 真的停流 + join：源被停、两条线程都退出了。"""
    mic = FakeCaptureSource("mic")
    session, _facts, sources = make_session(tmp_path, mode="mic", mic=mic)
    session.start()
    time.sleep(0.08)
    session.finalize()

    assert sources["mic"].stopped is True
    assert all(not thread.is_alive() for thread in session.workers["mic"]._threads)


def test_finalize_is_idempotent(tmp_path: Path):
    """连调两次 `finalize()`（`stop()` 再来一次也一样）不重复写、不抛错。"""
    mic = FakeCaptureSource("mic", chunks=3)
    session, _facts, _sources = make_session(tmp_path, mode="mic", mic=mic)
    session.start()
    assert wait_for(lambda: mic.fed >= 3)
    session.finalize()
    size = (tmp_path / archive_name("mic")).stat().st_size
    session.finalize()
    session.stop()
    session.finalize()
    assert (tmp_path / archive_name("mic")).stat().st_size == size


def test_finalize_after_stop_writes_the_same_bytes(tmp_path: Path):
    """`stop()` 就是「没落过盘就先 `finalize()`」：两条次序的产物一致。"""
    mic = FakeCaptureSource("mic", chunks=3)
    session, _facts, _sources = make_session(tmp_path, mode="mic", mic=mic)
    session.start()
    assert wait_for(lambda: mic.fed >= 3)
    session.stop()
    assert session.bytes_written("mic") == 3 * CHUNK_BYTES


def test_transcode_produces_one_mp3_per_filled_segment(tmp_path: Path):
    """收尾第 2 步：每路**写了字节的段**各出一个 MP3，PCM 原样留着。"""
    mic = FakeCaptureSource("mic", chunks=8)
    loop = FakeCaptureSource("loopback", chunks=8)
    session, _facts, _sources = make_session(tmp_path, mic=mic, loopback=loop)
    session.start()
    assert wait_for(lambda: mic.fed >= 8 and loop.fed >= 8)
    session.finalize()
    session.transcode()

    for source in SOURCE_IDS:
        assert (tmp_path / archive_name(source).replace(".pcm", ".mp3")).exists()
        assert (tmp_path / archive_name(source)).exists()


def test_transcode_skips_the_route_with_no_audio(tmp_path: Path):
    """收尾那一刻一个字节都没写到的段不产出它的 MP3（照 V1 的空文件口径）。"""
    session, _facts, _sources = make_session(tmp_path, mode="mic")
    session.finalize()  # start() 都没调：两路都是 0 字节
    session.transcode()

    assert not (tmp_path / "meeting_audio_mic_001.mp3").exists()
    assert not (tmp_path / "meeting_audio_loopback_001.mp3").exists()
    assert (tmp_path / archive_name("mic")).exists()


def test_transcode_raises_but_keeps_the_pcm(tmp_path: Path, monkeypatch):
    """转不动时原样抛错：该步据此记 `failed`，PCM 留在场面上。"""
    from meeting.capture import archive as archive_module

    mic = FakeCaptureSource("mic", chunks=4)
    session, _facts, _sources = make_session(tmp_path, mode="mic", mic=mic)
    session.start()
    assert wait_for(lambda: mic.fed >= 4)
    session.finalize()
    payload = (tmp_path / archive_name("mic")).read_bytes()

    def boom(*args, **kwargs):
        raise RuntimeError("假件：转码失败")

    monkeypatch.setattr(archive_module, "convert_to_mp3", boom)
    import pytest

    with pytest.raises(RuntimeError):
        session.transcode()
    assert (tmp_path / archive_name("mic")).read_bytes() == payload


def test_start_is_idempotent(tmp_path: Path):
    """重复 `start()` 不会拉起第二套线程。"""
    mic = FakeCaptureSource("mic", chunks=3)
    session, _facts, _sources = make_session(tmp_path, mode="mic", mic=mic)
    session.start()
    threads = list(session.workers["mic"]._threads)
    session.start()
    assert session.workers["mic"]._threads == threads
    session.finalize()


def test_stop_before_any_start_is_harmless(tmp_path: Path):
    """`start()` 都没跑就收场（`start()` 抛错之前自己收干净那条路）不抛错。"""
    session, _facts, _sources = make_session(tmp_path, mode="mic")
    session.stop()
    session.stop()
    assert (tmp_path / archive_name("mic")).exists()
