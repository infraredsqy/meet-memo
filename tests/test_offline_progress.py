"""逐块进度记录与它的现场读数（`meeting/offline/progress.py` + `runner.run_session(progress=…)`）。

本文件盯住四件事：

  - **一块跑完，那一块的字就在盘上**：记录里的行在每一块跑完之后就该到，不必等整路跑完；
  - **分母按路各算一份**：`chunks_total` 来自这一路的切块计划，静音跳过的块与失败的块
    都算"跑过了一块"，四档结果各记各的；
  - **正式稿的写时机一个字不改**：跑到一半时场次目录里没有
    `meeting_offline_transcript_<source>.md`（`has_offline` 那一格不许因此提前变真）；
  - **读数是现场读数**：跑到一半去读，读得到"第几块 / 共几块、正在跑哪一路、已经跑了多久"。

全部用假引擎；「跑到一半」那一档靠 `FakeEngine(gate=…)` 把某一路卡在块中间，
不靠真等待（`tests/conftest.py` 的单条用例 5 秒看门狗）。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from meeting.offline import artifacts
from meeting.offline import progress as progress_module
from meeting.offline.engine import OfflineSegment
from meeting.offline.progress import ChunkProgress, OfflineProgressLedger
from meeting.offline.runner import run_session
from meeting.workspace import has_offline_transcript
from tests.test_offline_runner import CHUNK_SECONDS, FakeConfig, FakeEngine, sparse_pcm, synth_session

SAMPLE_RATE = 16000
#: 块卡住时的放行/等待上限（等到了就立刻回，不赌固定时长）
GATE_TIMEOUT_SECONDS = 5.0


def chunked_session(tmp_path: Path, *, seconds: float = 25.0) -> Path:
    """两路各造 25 秒带信号的归档 PCM（块长 10 秒 → 每路 3 块）。"""
    session = synth_session(tmp_path / "250906")
    for source in ("mic", "loopback"):
        sparse_pcm(session, source, seconds)
    return session


def chunked_config() -> FakeConfig:
    return FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS})


def read_ledger(session: Path) -> tuple[dict | None, list[dict]]:
    return progress_module.read_progress(progress_module.ledger_path(session))


def ledger_rows(session: Path, source: str) -> list[dict]:
    _header, rows = read_ledger(session)
    return [row for row in rows if row.get("source") == source]


class GatedEngine(FakeEngine):
    """按**第几次**调用放行的假引擎：第 `block_at` 次调用卡在闸门上（从 1 数起）。"""

    def __init__(self, gate: threading.Event, *, block_at: int) -> None:
        super().__init__()
        self._gate = gate
        self._block_at = int(block_at)
        self._calls = 0
        self.reached = threading.Event()

    def transcribe(self, audio_path):
        self._calls += 1
        if self._calls == self._block_at:
            self.reached.set()
            self._gate.wait(timeout=GATE_TIMEOUT_SECONDS)
        return super().transcribe(audio_path)


class CountingEngine(FakeEngine):
    """按调用序号逐块换内容的假引擎（每一块的文字一眼看得出是哪一块的）。

    分块跑时引擎拿到的是**那一块的音频**，所以一段真实引擎的 `start` 落在块内（0 起算）。
    这里照实模拟：第 N 次调用交回 `[0,1]` 与 `[1,2]` 两段，各带一句写明是第几块的话。
    """

    def transcribe(self, audio_path):
        self.calls.append(str(audio_path))
        number = len(self.calls)
        return [
            OfflineSegment(0.0, 1.0, "S01", f"第 {number} 块的一句"),
            OfflineSegment(1.0, 2.0, "S02", f"第 {number} 块的另一句"),
        ]


# ---------------------------------------------------------------------------
# 一、一块跑完，那一块的字与落点就在盘上
# ---------------------------------------------------------------------------


def test_every_chunk_lands_in_the_ledger_with_its_text(tmp_path) -> None:
    """整趟跑完：两路各 3 行，行上有这一块的文字、落点与结果档。"""
    session = chunked_session(tmp_path)

    run_session(
        session,
        engine=CountingEngine(),
        config=chunked_config(),
        progress=OfflineProgressLedger(session, session_id="250906",
                                       sources=["mic", "loopback"]),
    )

    header, rows = read_ledger(session)
    assert header is not None
    assert header["kind"] == "ledger"
    assert header["session_id"] == "250906"
    assert header["sources"] == ["mic", "loopback"]
    assert len(rows) == 6, rows

    mic = ledger_rows(session, "mic")
    assert [row["chunk"] for row in mic] == [1, 2, 3]
    assert {row["chunks_total"] for row in mic} == {3}
    assert [row["state"] for row in mic] == ["transcribed"] * 3
    assert [row["span"] for row in mic] == [[0.0, 10.0], [10.0, 20.0], [20.0, 25.0]]
    # 文字按正式稿同一套行格式写下来，块内编号带 `cNN:` 命名空间（还没整路编号）
    assert mic[0]["text"].splitlines() == [
        "[00:00] **c01:S01**: 第 1 块的一句",
        "[00:00] **c01:S02**: 第 1 块的另一句",
    ]
    assert mic[1]["text"].startswith("[00:00] **c02:S01**: 第 2 块的一句")
    assert mic[2]["text"].startswith("[00:00] **c03:S01**: 第 3 块的一句")
    assert mic[0]["segments"] == 2
    assert mic[0]["error"] is None


def test_the_ledger_is_readable_while_a_source_is_still_running(tmp_path) -> None:
    """mic 跑完第 1 块、卡在第 2 块上：此刻记录里有一行，而正式稿还没出现。"""
    session = chunked_session(tmp_path)
    gate = threading.Event()
    engine = GatedEngine(gate, block_at=2)
    ledger = OfflineProgressLedger(session, session_id="250906",
                                   sources=["mic", "loopback"])
    thread = threading.Thread(
        target=lambda: run_session(
            session, engine=engine, config=chunked_config(), progress=ledger
        ),
        daemon=True,
    )
    thread.start()
    try:
        assert engine.reached.wait(timeout=GATE_TIMEOUT_SECONDS), "引擎没跑到 mic 的第 2 块"

        # 跑到一半的现场读数：第 1 块跑完了、此刻在跑第 2 块
        snapshot = ledger.snapshot()
        assert snapshot["state"] == "running"
        assert snapshot["current_source"] == "mic"
        assert snapshot["elapsed_seconds"] >= 0.0
        mic, loopback = snapshot["sources"]
        assert mic["done"] == 1 and mic["total"] == 3 and mic["current_chunk"] == 2
        # 还没轮到的那一路如实交回"还没开跑"（分母都还没有），别报一个半截的"第 1 块"
        assert loopback["done"] == 0 and loopback["total"] is None
        assert loopback["current_chunk"] is None
        assert mic["counts"] == {"transcribed": 1, "silent": 0, "failed": 0, "unjudged": 0}

        # 盘上那一份记录也是同一个读数：第 1 块的字已经在盘上读得到了
        rows = ledger_rows(session, "mic")
        assert [row["chunk"] for row in rows] == [1]
        # 行格式与正式稿同款，块内编号带 `cNN:` 命名空间（还没整路编号）
        assert rows[0]["text"].startswith("[00:00] **c01:S01**: ")
        assert rows[0]["segments"] == 2
        assert ledger_rows(session, "loopback") == []
        # 而这一路此刻**还没有**正式稿（正式稿整路跑完一次写），R29 那一格照旧是假
        assert not (session / "meeting_offline_transcript_mic.md").exists()
        assert has_offline_transcript(session) is False
    finally:
        gate.set()
        thread.join(timeout=GATE_TIMEOUT_SECONDS)
    assert not thread.is_alive(), "那一趟没收工"

    # 跑完之后：两路各 3 行、状态收在终态上，正式稿这才出现
    _header, rows = read_ledger(session)
    assert len(rows) == 6
    assert ledger.snapshot()["state"] == "done"
    assert has_offline_transcript(session) is True


def test_a_silent_chunk_and_a_failed_chunk_both_count_as_one_chunk_run(tmp_path) -> None:
    """静音跳过的块与引擎抛错的块都算"跑过了一块"，各记各的结果档；分母不因此增减。

    mic：第 1 块（0–10 秒）里有信号，其余全是数字零 → 3 块，中间那一块跳过引擎。
    loopback：有一块引擎抛错，那一块照记 `failed`，同路别的块照跑。
    """
    session = synth_session(tmp_path / "250906")
    with open(session / "meeting_audio_mic_001.pcm", "wb") as handle:
        handle.write(b"\x01\x01" * 4096)  # 第 1 块里有信号
        handle.seek(int(25.0 * SAMPLE_RATE) * 2 - 2)
        handle.write(b"\x01\x00")  # 长度铺到 25 秒，中间那片是数字零
    sparse_pcm(session, "loopback", 25.0)

    class HalfBroken(CountingEngine):
        """loopback 的第 2 块（它的第二次调用）抛错，别的块照常交回内容。"""

        def transcribe(self, audio_path):
            self.calls.append(str(audio_path))
            if len(self.calls) == 4:
                raise RuntimeError("这一块按要求炸了")
            return [OfflineSegment(0.0, 1.0, "S01", f"第 {len(self.calls)} 块的一句")]

    run_session(
        session,
        engine=HalfBroken(),
        config=chunked_config(),
        progress=OfflineProgressLedger(session, session_id="250906",
                                       sources=["mic", "loopback"]),
    )

    mic = ledger_rows(session, "mic")
    assert [row["chunk"] for row in mic] == [1, 2, 3], "静音跳过的那一块也算跑过了一块"
    assert [row["state"] for row in mic] == ["transcribed", "silent", "transcribed"], mic
    assert mic[1]["text"] == ""
    assert mic[1]["error"] is None
    assert {row["chunks_total"] for row in mic} == {3}

    loopback = ledger_rows(session, "loopback")
    assert [row["chunk"] for row in loopback] == [1, 2, 3]
    assert [row["state"] for row in loopback] == ["transcribed", "failed", "transcribed"]
    assert loopback[1]["text"] == ""
    assert "炸了" in loopback[1]["error"]
    assert {row["chunks_total"] for row in loopback} == {3}


def test_a_load_failure_still_leaves_a_row_with_the_denominator(tmp_path) -> None:
    """引擎**加载失手**那一场：记录里照旧有一行，读方从盘上读得到分母（`total` 不从 N 塌成 null）。

    `total` 是这一路切了几块（切块计划定下来的那个数），**不因某一块失败而增减**
    （`docs/contract.md` 4.9）。这一档从前一块都没进逐块循环、记录里只剩表头，进程不在场的
    读方（服务重启后、命令行跑完回头翻这一场）因此看到 `done=0`、`total=null` —— 与
    「这一路一块都没跑」分不开。补的这一行与**兄弟路径**（切法都算不出来那一条，见
    `runner._run_one_source()`）同一个理由，行上的块号取**触发探测的那一块**（加载失手
    就是在这一块上发现的），分母照旧是这一路的总块数。
    """
    from tests.test_offline_runner import LoadingEngine, silence_session

    session, _ranges = silence_session(
        tmp_path, name="load-failure", silent_chunks=(), chunk_seconds=CHUNK_SECONDS
    )
    engine = LoadingEngine(load_error="模型快照读不动（合成的失败，没有真模型参与）")

    run_session(
        session,
        engine=engine,
        config=chunked_config(),
        progress=OfflineProgressLedger(session, session_id=session.name, sources=["mic"]),
    )

    header, rows = read_ledger(session)
    assert header is not None
    assert [row["chunk"] for row in rows] == [1], "加载失手也要在记录里留一行"
    assert rows[0]["state"] == "failed"
    assert rows[0]["chunks_total"] == 3
    assert (rows[0]["error"] or "").startswith("engine_load_error")

    # 进程不在场那一档读盘：分母还在（这一条盯的就是它）
    snapshot = progress_module.snapshot_of(session, sources=["mic"], session_id=session.name)
    mic = snapshot["sources"][0]
    assert mic["total"] == 3, "分母由切块计划定：不许因为加载失败塌成 null"
    assert mic["done"] == 1 and mic["remaining"] == 2
    assert mic["counts"] == {"transcribed": 0, "silent": 0, "failed": 1, "unjudged": 0}


def test_the_ledger_stays_out_of_the_way_when_nobody_asks_for_it(tmp_path) -> None:
    """不给 `progress` 时一个字节都不落（既有那一串调用点的形状不变）。"""
    session = chunked_session(tmp_path)
    run_session(session, engine=FakeEngine(), config=chunked_config())
    assert not progress_module.ledger_path(session).exists()


def test_the_ledger_does_not_touch_the_status_file(tmp_path) -> None:
    """进度记录与 `meeting_offline_status.json` 各写各的：那份状态文件照旧十二格。"""
    session = chunked_session(tmp_path)
    run_session(
        session,
        engine=FakeEngine(),
        config=chunked_config(),
        progress=OfflineProgressLedger(session, session_id="250906",
                                       sources=["mic", "loopback"]),
    )
    status = artifacts.read_status(session / artifacts.STATUS_NAME)
    assert set(status) == {
        "schema_version", "session_id", "state", "started_at", "finished_at",
        "sources", "artifacts", "partial", "silent", "silent_spans", "gate", "error",
    }


# ---------------------------------------------------------------------------
# 二、盘上那份记录的读法与写法
# ---------------------------------------------------------------------------


def test_read_progress_tolerates_a_half_written_ledger(tmp_path) -> None:
    """读到半行、读到空文件、读到不存在的文件：如实交回读得到的那部分，不抛。"""
    session = tmp_path / "250906"
    session.mkdir()
    path = progress_module.ledger_path(session)

    assert progress_module.read_progress(path) == (None, [])

    path.write_text("", encoding="utf-8")
    assert progress_module.read_progress(path) == (None, [])

    header = json.dumps(
        {"kind": "ledger", "schema_version": 1, "session_id": "250906", "sources": ["mic"]},
        ensure_ascii=False,
    )
    row = json.dumps(
        {"source": "mic", "chunk": 1, "chunks_total": 2, "state": "transcribed",
         "text": "一句", "segments": 1, "wall_seconds": 1.0, "span": [0.0, 10.0]},
        ensure_ascii=False,
    )
    path.write_text(f"{header}\n{row}\n{{\"source\": \"mic\", \"chu", encoding="utf-8")
    got_header, got_rows = progress_module.read_progress(path)
    assert got_header is not None and got_header["session_id"] == "250906"
    assert [item["chunk"] for item in got_rows] == [1]


def test_write_progress_is_atomic_and_idempotent(tmp_path) -> None:
    """同一份内容连写两次字节逐字相同；写完的目录里不留 `.tmp` 残骸。"""
    session = tmp_path / "250906"
    session.mkdir()
    path = progress_module.ledger_path(session)
    rows = [ChunkProgress("mic", 1, 0.0, 10.0, "transcribed", "一句").to_row(chunks_total=2)]

    progress_module.write_progress(
        path, session_id="250906", sources=["mic", "loopback"], rows=rows
    )
    first = path.read_bytes()
    progress_module.write_progress(
        path, session_id="250906", sources=["mic", "loopback"], rows=rows
    )
    assert path.read_bytes() == first
    assert [item.name for item in session.iterdir()] == [progress_module.PROGRESS_NAME]


def test_snapshot_of_reads_the_ledger_back_from_disk(tmp_path) -> None:
    """进程里没有现场时（重启后、命令行跑完回头翻）读盘那一份记录拼出同一套读数。"""
    session = tmp_path / "250906"
    session.mkdir()
    path = progress_module.ledger_path(session)
    progress_module.write_progress(
        path,
        session_id="250906",
        sources=["mic", "loopback"],
        rows=[
            ChunkProgress("mic", 1, 0.0, 10.0, "transcribed", "一句", 1, 3.0).to_row(
                chunks_total=2
            ),
            ChunkProgress("mic", 2, 10.0, 10.0, "silent").to_row(chunks_total=2),
        ],
    )

    snapshot = progress_module.snapshot_of(session, sources=["mic", "loopback"],
                                           session_id="250906")
    assert snapshot["state"] == "ledger"
    assert snapshot["elapsed_seconds"] is None, "跨进程算不出单调钟的差值，这一格不猜"
    mic, loopback = snapshot["sources"]
    assert (mic["done"], mic["total"], mic["remaining"]) == (2, 2, 0)
    assert mic["counts"] == {"transcribed": 1, "silent": 1, "failed": 0, "unjudged": 0}
    assert (loopback["done"], loopback["total"]) == (0, None)


def test_the_ledger_write_failure_does_not_take_the_run_down(tmp_path) -> None:
    """落盘写不下去（这里用一个必炸的 writer 替身）：正片照跑，记录里那几行照记。"""
    session = chunked_session(tmp_path)
    calls: list[str] = []

    def exploding_writer(*_args, **_kwargs):
        calls.append("write")
        raise OSError("盘满了")

    ledger = OfflineProgressLedger(
        session, session_id="250906", sources=["mic", "loopback"], writer=exploding_writer
    )
    summary = run_session(
        session, engine=FakeEngine(), config=chunked_config(), progress=ledger
    )

    assert calls, "writer 一次都没被调到"
    assert summary["state"] == "done"
    assert (session / "meeting_offline_transcript_mic.md").exists()
    # 记录没落成，但这不影响现场读数与正片产物
    assert ledger.snapshot()["sources"][0]["done"] == 3


@pytest.mark.parametrize("state", ["transcribed", "silent", "failed", "unjudged"])
def test_every_ledger_row_carries_one_of_the_four_states(tmp_path, state: str) -> None:
    """四档结果各写各的名字（读方按这一格分档，不必从别的字段反推）。"""
    session = tmp_path / "250906"
    session.mkdir()
    path = progress_module.ledger_path(session)
    progress_module.write_progress(
        path,
        session_id="250906",
        sources=["mic"],
        rows=[ChunkProgress("mic", 1, 0.0, 10.0, state).to_row(chunks_total=1)],
    )
    _header, rows = progress_module.read_progress(path)
    assert rows[0]["state"] == state
    assert rows[0]["text"] == ""
