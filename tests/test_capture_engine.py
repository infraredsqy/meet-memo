"""`tests/test_capture_engine.py` —— 会话采集引擎（`meeting/capture/engine.py`）。

**全程用注入的假源**（`CaptureSource` 协议的实现），一个真声卡入口都不碰：假源能造出
「每块内容可指定、第 N 块抛错、块数可指定」这几种情形，覆盖面比录几秒真声音更全。

验的是生产代码这条路径：读线程、写线程、电平刻度、收尾落盘全部是真的，换掉的只有最外面
那一层声卡库。五块：

  1. 归档核对：两路 PCM 都长起来，字节数按 **32000 字节/秒** 对得上喂进去的块数；
  2. 单路死亡：一路的源抛错 → 该路一条 `SourceLost`，**另一路的字节数继续增长**；
  3. `mode` 过滤：`mode="mic"` 时 loopback 不读、不发帧，但**整段时间写静音**
     （两路时间轴等长，`docs/design-intent.md` 第 9 条）；
  4. 暂停与关一路：暂停 = 所有路写数字静音（逐样本为零）、不录内容、段不切段；
     用户主动关一路 = 那一路写静音、**不报 `SourceLost`**，另一路照常；
  5. 段记录与电平刻度：段起止戳、在录时长（内容字节 ÷ 32000）、权威时长 = 各段之和；
     每 `frame_interval` 秒**无条件**发一帧（静音时也发），`SourceFacts` 跟着走。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from meeting.backends.base import FrameGap, LevelFrame, SourceFacts, SourceLost
from meeting.capture.engine import (
    CAPTURE_MODES,
    SILENCE_CHUNK_BYTES,
    SOURCE_IDS,
    CaptureSession,
    sources_for_mode,
)
from meeting.session import archive_name

from tests._capture_fakes import CHUNK_BYTES, CHUNK_FRAMES, FakeCaptureSource, block

pytestmark = pytest.mark.slow

#: 「安静的一路照样发帧」那条用例的等待口径：至少等到的电平帧数，与等待的超时（秒）。
#: `Event.wait()` 被 Windows 定时器粒度上取整到 ~62ms，
#: 等到 3 帧约 0.19 秒 —— 5 秒超时有 ~26 倍余量。
MIN_SILENCE_FRAMES = 3
FRAME_WAIT_TIMEOUT_SECONDS = 5.0

#: 竞态用例里的等待上限（秒）：接管与重开都是事件驱动的即时到达，2 秒有约一个数量级
#: 的余量，也给「单条用例 ≤5 秒」那条看门留出余裕
RACE_WAIT_TIMEOUT_SECONDS = 2.0


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


# ① 归档核对


def test_sources_for_mode_is_the_frozen_domain():
    """`both` / `mic` / `loopback` 三档；`both` 就是两路。"""
    assert CAPTURE_MODES == ("both", "mic", "loopback")
    assert sources_for_mode("both") == SOURCE_IDS
    assert sources_for_mode("mic") == ("mic",)
    assert sources_for_mode("loopback") == ("loopback",)


def test_both_routes_fill_their_own_archive(tmp_path: Path):
    """两路各自长起来：字节数 = 块数 × 3200（一秒 32000 字节的核对口径）。"""
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


# ② 单路死亡：另一路照跑


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


# ③ mode 过滤


def test_mic_mode_leaves_the_loopback_route_alone(tmp_path: Path):
    """`mode="mic"`：loopback 不读、不发帧，但**整段时间写静音**（两路时间轴等长）。

    「某一路没开」也是「那一路写静音」（`docs/design-intent.md` 第 9 条）：未选那一路的
    文件不再是 0 字节，落盘的全是数字零，且不记入内容量——权威时长只由 mic 那一路的内容
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
    # 时间轴等长：未选那一路落的全是静音字节，且与它的字节数核对得上
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


# ③½ 暂停与关一路：写静音，不录内容；段不切段


def test_pause_writes_silence_and_does_not_record_content(tmp_path: Path):
    """暂停期间两路都写数字零、内容量停住；恢复后内容照录；**段不切段**。

    盘上核对**在收尾结算之后**做（不跟写线程的登记时序赛跑）：内容块全是源给的那一块、
    静音全是零且总量等于写侧的静音量——两句话合起来就是「暂停 = 写静音，不录内容」。
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

    # 盘上核对（**结算后**对，不跟写线程的登记时序赛跑）：内容块都是源给的那一块、
    # 暂停静音逐样本为零且总量等于静音量——暂停只是把中间那截换成了零
    data = (tmp_path / archive_name("mic")).read_bytes()
    content_total = session.content_bytes("mic")
    silence_total = session.silence_bytes("mic")
    assert silence_total >= 3 * CHUNK_BYTES
    assert content_total % CHUNK_BYTES == 0 and silence_total % CHUNK_BYTES == 0
    assert data.count(block(900)) * CHUNK_BYTES + silence_total == len(data)
    assert data.replace(block(900), b"") == bytes(silence_total), (
        "暂停窗口必须逐样本为零（第 8 条的隐私线）"
    )
    # 记录自洽：总字节数 = 内容 + 静音；暂停没有把段切开
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


def test_pause_keeps_the_beat_and_records_no_frame_gap(tmp_path: Path):
    """暂停期间电平帧照发（既有依赖）→ 帧空洞自查记录一拍不缺 → **暂停不构成空洞**。

    空洞记录（gil-stall-hole）记的是「发帧线程的节拍断了多久」：暂停只把内容换成静音，
    刻度线一拍不停，所以整段暂停在记录里不留痕。恢复之后也不补记 —— 节拍从没断过。
    """
    mic = FakeCaptureSource("mic", chunk=block(900), pace_seconds=0.005)
    session, facts, _sources = make_session(tmp_path, mic=mic, frame_interval=0.05)
    session.start()
    assert wait_for(lambda: len(only(facts, LevelFrame)) >= MIN_SILENCE_FRAMES,
                    timeout=FRAME_WAIT_TIMEOUT_SECONDS)
    session.pause()
    # 暂停窗里打点线程照常发帧（等到若干拍再核对，别跟打点时序赛跑）
    paused_before = len(only(facts, LevelFrame))
    assert wait_for(
        lambda: len(only(facts, LevelFrame)) >= paused_before + MIN_SILENCE_FRAMES,
        timeout=FRAME_WAIT_TIMEOUT_SECONDS,
    ), "暂停期间电平帧停发了（这本身就会把暂停误报成采集停）"
    session.resume()
    assert wait_for(
        lambda: len(only(facts, LevelFrame)) >= paused_before + 2 * MIN_SILENCE_FRAMES,
        timeout=FRAME_WAIT_TIMEOUT_SECONDS,
    )
    session.finalize()
    assert only(facts, FrameGap) == [], "节拍没断，暂停与恢复都不留空洞"


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


class _DeactivateRaceSource:
    """把「关路动作落进读线程顶层检查之后、`read()` 之内」的窗口钉死的注入源。

    `read()` 一进门就点亮 `entered`（读线程此刻已越过 `_read_loop` 顶层的
    `_user_closed` 检查），然后等 `release`；主线程在这一空档里调 `deactivate()`
    （置位 + 停源），再放行 `read()` —— 停源之后的那一次读就交回 `None` 或抛错，
    正是竞态命中时读线程手里的那一拍。
    """

    name = "mic"

    def __init__(self, *, outcome: object) -> None:
        #: `None` = 停源后的瞬时读空；`BaseException` = 停源引发的读错
        self.outcome = outcome
        self.entered = threading.Event()
        self.release = threading.Event()
        self.stopped = False

    def start(self) -> None:
        pass

    def read(self, timeout: float) -> bytes | None:
        self.entered.set()
        self.release.wait(5.0)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome  # type: ignore[return-value]

    def latest_samples(self) -> np.ndarray:
        return np.empty(0, dtype=np.int16)

    def stop(self) -> None:
        self.stopped = True


@pytest.mark.parametrize("outcome_name", ["read_returns_none", "read_raises"])
def test_closing_a_route_mid_read_keeps_the_silence_metronome(tmp_path, outcome_name):
    """关路动作落进「顶层检查之后、`read()` 之内」的窗口：读线程不死、静音节拍器接管。

    验的是关路与读线程轮询的竞态：`_read_loop` 顶层判过 `_user_closed` 才进的
    `read()`，此刻 `deactivate()` 置位并停源，停源让 `read()` 交回 `None`（或抛错）
    —— 这一拍不是「流结束」，是停源的瞬时读空；读线程必须转上静音节拍器（旧代码在
    `data is None` / `except` 分支无条件 return：读线程死亡、静音不涨、重开一路后
    这一路永远录不到内容而 `stream_alive` 仍报 true）。判据取三步实测：接管后静音
    继续涨、读线程还活着、重开一路后录得到内容。
    """
    outcome = RuntimeError("停源引发的读错") if outcome_name == "read_raises" else None
    mic = _DeactivateRaceSource(outcome=outcome)
    session, facts, _sources = make_session(tmp_path, mode="mic", mic=mic)
    session.start()
    try:
        assert mic.entered.wait(FRAME_WAIT_TIMEOUT_SECONDS), "读线程该已经进到 read() 里"
        # 读线程已越过顶层检查（_user_closed 还没置位）：把关路动作塞进这个窗口
        session.deactivate_source("mic")
        assert mic.stopped is True
        mic.release.set()

        # 静音节拍器接管：关掉之后静音继续涨（读线程死了它就永远不涨）
        assert wait_for(
            lambda: session.silence_bytes("mic") >= CHUNK_BYTES,
            timeout=RACE_WAIT_TIMEOUT_SECONDS,
        ), "关路之后静音节拍器该接管（读线程不该死在 data is None / except 分支上）"
        assert session.workers["mic"]._threads[0].is_alive(), "读线程该还活着（转在节拍器上）"
        assert only(facts, SourceLost) == [], "主动关掉的那一路不报故障"

        # 重开这一路：读线程活着才接得上新源、录得到内容（reactivate 的前提）
        fresh = FakeCaptureSource("mic", chunk=block(900))
        fresh.start()
        session.activate_source("mic", fresh)
        assert wait_for(
            lambda: session.content_bytes("mic") >= CHUNK_BYTES,
            timeout=RACE_WAIT_TIMEOUT_SECONDS,
        ), "重开之后这一路该重新录到内容"
    finally:
        session.finalize()


def test_segment_account_before_finalize_has_no_closing_stamp(tmp_path: Path):
    """还在录时段记录没有终点戳、权威时长为 0（没写完的段没有尾可收）。"""
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


# ④ 电平刻度与事实


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
    `Event.wait()` 受 15.6ms 定时器粒度影响**只会上取整**，0.2 秒窗口里只够 3–6 拍，
    于是这条会随机红。现在改成「帧数到了就返回」的等待，并按实测节拍留足余量：
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


# 收尾：finalize / transcode / stop


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


def test_init_closes_opened_writers_when_a_later_open_fails(tmp_path: Path, monkeypatch):
    """第二路 `open()` 抛错：已 `open()` 的第一路 writer 被 `close()`，原异常照上抛。

    构造逐路 `open()`（建段文件 + 起写线程），第二路炸了时构造原样上抛 ——
    第一路的写线程与句柄若没人收就是实打实的泄漏。`start()` 有「抛错之前自己拉起来的
    东西自己收干净」的纪律，构造补上同一条：收尾（`close()`）失败只记 warning，
    上抛的主因仍是构造失败的那个原异常。
    """
    from meeting.capture import engine as engine_module

    real_open = engine_module.PcmArchiveWriter.open
    real_close = engine_module.PcmArchiveWriter.close
    opened: list[str] = []
    closed: list[engine_module.PcmArchiveWriter] = []

    def flaky_open(self):
        if opened:  # 炸在第二路（loopback）上：第一路（mic）已经 open 过
            raise RuntimeError("第二路 open 按计划炸了")
        opened.append(self.label)
        real_open(self)

    def spy_close(self, **kwargs):
        closed.append(self)
        real_close(self, **kwargs)

    monkeypatch.setattr(engine_module.PcmArchiveWriter, "open", flaky_open)
    monkeypatch.setattr(engine_module.PcmArchiveWriter, "close", spy_close)

    with pytest.raises(RuntimeError, match="第二路 open"):
        CaptureSession(
            mode="both",
            session_dir=str(tmp_path),
            sources={},
            emit=lambda fact: None,
        )

    assert opened == ["mic"], f"炸在第二路的 open 上，第一路已开：{opened}"
    assert [w.label for w in closed] == ["mic", "loopback"], (
        f"已 open 的第一路必须被收干净（半开的第二路也容忍 close）：{closed}"
    )
    # 收尾是真收尾：两路写侧的句柄都已交还（close 走完的读数）
    assert all(writer._handle is None for writer in closed)

