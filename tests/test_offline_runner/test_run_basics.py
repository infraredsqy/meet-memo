from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from meeting.offline import artifacts
from meeting.offline.engine import OfflineSegment

from meeting.offline.runner import (
    CHUNK_DIR_NAME,
    REASON_ALL_CHUNKS_FAILED,
    REASON_EMPTY_PCM,
    REASON_ENGINE_ERROR,
    REASON_ENGINE_LOAD_ERROR,
    REASON_NO_AUDIO,
    REASON_NO_SEGMENTS,
    REASON_NOT_RUN,
    declared_sources,
    plan_chunks,
    read_chunk_seconds,
    resolve_audio,
    resolve_engine,
    run_session,
    write_chunk_wav,
)

from tests._offline_fakes import (
    CHUNK_SECONDS,
    SAMPLE_RATE,
    FakeConfig,
    FakeEngine,
    LoadingEngine,
    chunk_ranges,
    silence_session,
    sparse_pcm,
    synth_session,
)

from tests.test_offline_runner._common import (
    read_status,
)

#: 慢档标记（收尾轮）：会后离线链的契约验收，归收尾跑。
pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# 一、一场会跑完：三份产物 + 状态文件
# ---------------------------------------------------------------------------


def test_run_session_writes_every_artifact(tmp_path) -> None:
    """三份产物 + 状态文件落地，`state == "done"`，字段齐、读得回来。"""
    session = synth_session(tmp_path / "250919")
    engine = FakeEngine(
        [
            OfflineSegment(0.48, 3.2, "S01", "甲"),
            OfflineSegment(65.5, 70.0, "S01", "乙"),
        ]
    )

    summary = run_session(session, engine=engine, config=FakeConfig())

    status = read_status(session)
    assert status["state"] == "done"
    assert status["error"] is None
    assert status["finished_at"]
    assert status["sources"] == ["mic", "loopback"]
    assert status["session_id"] == "250919"
    assert summary["state"] == "done"

    for source in ("mic", "loopback"):
        transcript = session / f"meeting_offline_transcript_{source}.md"
        speakers = session / f"meeting_offline_speakers_{source}.json"
        assert transcript.exists(), source
        assert speakers.exists(), source
        assert status["artifacts"][source]["transcript"] == transcript.name
        assert status["artifacts"][source]["speakers"] == speakers.name
        assert status["artifacts"][source]["report"] == artifacts.REPORT_NAME

        document = artifacts.read_speakers_artifact(speakers)
        assert document["source"] == source
        assert document["session_id"] == "250919"
        assert document["engine"]["model"] == "OpenMOSS-Team/MOSS-Transcribe-Diarize"
        assert document["engine"]["dtype"] == "bfloat16"
        assert document["audio"]["file"] == f"meeting_audio_{source}_001.pcm"
        assert document["audio"]["seconds"] == 3.0
        assert [item["speaker"] for item in document["segments"]] == [
            f"{source}:S01",
            f"{source}:S01",
        ]
        lines = artifacts.read_transcript_lines(transcript)
        assert [row["text"] for row in lines] == ["甲", "乙"]
        assert [row["stamp"] for row in lines] == ["00:00", "00:01"]
        assert [row["speaker"] for row in lines] == ["S01", "S01"]

    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    assert "## 实时率" in report
    assert "## 失败原因分布" in report
    assert "## 段耗时分布" in report
    assert "（无失败）" in report
    # 两路各自查得到
    assert "| mic |" in report and "| loopback |" in report


def test_run_session_calls_the_engine_once_per_source(tmp_path) -> None:
    """**两路分别调用引擎**（不先混音）：调用参数是两路各自的归档路径。"""
    session = synth_session(tmp_path / "250919")
    engine = FakeEngine()

    run_session(session, engine=engine, config=FakeConfig())

    assert [Path(call).name for call in engine.calls] == [
        "meeting_audio_mic_001.pcm",
        "meeting_audio_loopback_001.pcm",
    ]


def test_artifacts_record_the_device_the_engine_resolved(tmp_path) -> None:
    """产物 `engine.device` 记**引擎实际解析出来的设备**，不是配置里那个 `auto` 选择器。"""
    session = synth_session(tmp_path / "250919")
    engine = FakeEngine(device="cuda:0")

    summary = run_session(session, engine=engine, config=FakeConfig())

    assert summary["engine"]["device"] == "cuda:0"
    assert summary["engine"]["selector"] == "auto"
    for source in ("mic", "loopback"):
        document = artifacts.read_speakers_artifact(
            session / f"meeting_offline_speakers_{source}.json"
        )
        assert document["engine"]["device"] == "cuda:0"
        assert document["engine"]["selector"] == "auto"
    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    assert "设备：`cuda:0`" in report


def test_descriptor_falls_back_to_the_selector_without_an_engine() -> None:
    """拿不到引擎实例时如实退回选择器字符串，`resolve_device_of` 对残缺形状交回 `None`。"""
    from meeting.offline.runner import engine_descriptor, resolve_device_of

    descriptor = engine_descriptor(FakeConfig())
    assert descriptor["device"] == "auto"
    assert descriptor["model"] == "OpenMOSS-Team/MOSS-Transcribe-Diarize"
    assert descriptor["dtype"] == "bfloat16"

    assert resolve_device_of(FakeEngine(device="cpu")) == "cpu"
    assert resolve_device_of(object()) is None
    assert resolve_device_of(FakeEngine(device="  ")) is None


def test_status_is_running_before_the_sources_run(tmp_path, monkeypatch) -> None:
    """起跑那一刻状态文件已经是 `running`（钩子返回即算启动，界面上当场可读）。"""
    session = synth_session(tmp_path / "250919")
    seen: list[str] = []

    class WatchingEngine(FakeEngine):
        def transcribe(self, audio_path):
            seen.append(read_status(session)["state"])
            return super().transcribe(audio_path)

    run_session(session, engine=WatchingEngine(), config=FakeConfig())

    assert seen and set(seen) == {"running"}
    assert read_status(session)["state"] == "done"


# ---------------------------------------------------------------------------
# 二、两路隔离
# ---------------------------------------------------------------------------


def test_two_sources_get_separate_files_and_never_collide(tmp_path) -> None:
    """两路各自成文件，`mic:S01` 与 `loopback:S01` 同时存在且不相等。"""
    session = synth_session(tmp_path / "250919")
    engine = FakeEngine([OfflineSegment(0.0, 1.0, "S02", "同一句")])

    run_session(session, engine=engine, config=FakeConfig())

    mic = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    loop = artifacts.read_speakers_artifact(
        session / "meeting_offline_speakers_loopback.json"
    )

    mic_ids = {row["id"] for row in mic["speakers"]}
    loop_ids = {row["id"] for row in loop["speakers"]}
    assert mic_ids == {"mic:S01"}
    assert loop_ids == {"loopback:S01"}
    assert mic_ids & loop_ids == set()
    assert mic_ids != loop_ids
    # 同一批引擎标签在两路各自规范化成 S01（编号互不相认）
    assert mic["speakers"][0]["local"] == loop["speakers"][0]["local"] == "S01"
    assert mic["speakers"][0]["label_raw"] == loop["speakers"][0]["label_raw"] == "S02"
    # 两个文件各自写着各自的 source
    assert mic["source"] == "mic"
    assert loop["source"] == "loopback"


def test_declared_sources_comes_from_session_json(tmp_path) -> None:
    """这一场跑哪几路读 `session.json` 的 `sources`；没有那份文件时按两路全集。"""
    session = synth_session(tmp_path / "250919", sources=("mic",))
    assert declared_sources(session) == ("mic",)

    bare = tmp_path / "bare"
    bare.mkdir()
    assert declared_sources(bare) == ("mic", "loopback")


# ---------------------------------------------------------------------------
# 三、一路音频不可用：如实记录，另一路照常产出
# ---------------------------------------------------------------------------


def test_a_missing_source_is_recorded_and_the_other_still_runs(tmp_path) -> None:
    """loopback 的归档不在场 → 那一路记 `no_audio`，mic 照常产出，整场落 `partial`。

    顶层那一档与「两路都成」分得开：这里有一路整条没交回，`state` 就落 `partial`；
    缺的是哪一路另由 `artifacts.<source>` 那一格回答。
    """
    session = synth_session(tmp_path / "250919", missing=("loopback",))
    engine = FakeEngine()

    summary = run_session(session, engine=engine, config=FakeConfig())

    status = read_status(session)
    assert status["state"] == "partial"
    assert REASON_NO_AUDIO in (status["error"] or "")
    assert "loopback" in (status["error"] or "")
    # 缺席那一路不落产物（也不留一份空文件充数），但**在状态文件里有一格**：
    # `state = failed` + `reason = no_audio`，读方只看逐路那一档就知道缺的是哪一路
    assert not (session / "meeting_offline_transcript_loopback.md").exists()
    failed_row = status["artifacts"]["loopback"]
    assert failed_row["state"] == "failed"
    assert failed_row["reason"] == REASON_NO_AUDIO
    assert failed_row["transcript"] is None and failed_row["speakers"] is None
    assert failed_row["error"] and REASON_NO_AUDIO in failed_row["error"]
    # 另一路照常
    assert (session / "meeting_offline_transcript_mic.md").exists()
    assert "mic" in status["artifacts"]
    # 引擎只被调了一次，收到的是 mic 那一路
    assert [Path(call).name for call in engine.calls] == ["meeting_audio_mic_001.pcm"]
    # 报告里两路各一行，失败那一行的原因查得到
    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    assert "| loopback | （缺）" in report
    assert f"{REASON_NO_AUDIO}：" in report
    assert summary["transcripts"]["mic"]["segments"] == 2
    # 摘要那一格**两路都在**（没跑成的那一路也在，带着它自己的终态与原因）
    assert summary["transcripts"]["loopback"]["state"] == "failed"
    assert summary["transcripts"]["loopback"]["reason"] == REASON_NO_AUDIO


def test_an_empty_pcm_falls_back_to_the_mp3(tmp_path) -> None:
    """归档 PCM 是 0 字节、同目录有 MP3 → 走 MP3（收尾转码的产物），并记一笔。"""
    session = synth_session(tmp_path / "250919", empty=("loopback",))
    mp3 = session / "meeting_audio_loopback_001.mp3"
    mp3.write_bytes(b"ID3 fake mp3 bytes")
    engine = FakeEngine()

    path, note = resolve_audio(session, "loopback")
    assert path == [str(mp3)]
    assert "MP3" in (note or "")

    run_session(session, engine=engine, config=FakeConfig())

    assert read_status(session)["state"] == "done"
    assert (session / "meeting_offline_transcript_loopback.md").exists()
    assert {Path(call).name for call in engine.calls} == {
        "meeting_audio_mic_001.pcm",
        "meeting_audio_loopback_001.mp3",
    }


def test_an_empty_pcm_without_an_mp3_fails_that_source_only(tmp_path) -> None:
    """归档 PCM 是 0 字节、又没有 MP3 → 那一路记 `empty_pcm`，另一路照常，整场落 `partial`。"""
    session = synth_session(tmp_path / "250919", empty=("mic",))
    engine = FakeEngine()

    summary = run_session(session, engine=engine, config=FakeConfig())

    status = read_status(session)
    assert status["state"] == "partial"
    assert REASON_EMPTY_PCM in (status["error"] or "")
    assert status["artifacts"]["mic"]["state"] == "failed"
    assert status["artifacts"]["mic"]["reason"] == REASON_EMPTY_PCM
    assert "loopback" in status["artifacts"]
    assert summary["transcripts"]["loopback"]["segments"] == 2


def test_both_sources_unavailable_lands_failed(tmp_path) -> None:
    """两路都没有音频 → 整场落 `failed`（一份产物都出不来，如实报）。"""
    session = synth_session(tmp_path / "250919", missing=("loopback",))
    (session / "meeting_audio_mic_001.pcm").unlink()

    run_session(session, engine=FakeEngine(), config=FakeConfig())

    status = read_status(session)
    assert status["state"] == "failed"
    assert status["error"] and REASON_NO_AUDIO in status["error"]
    assert status["finished_at"]


