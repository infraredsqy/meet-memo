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
    verdict_of,
)

pytestmark = pytest.mark.slow


def test_engine_error_lands_in_the_status_file_without_escaping(tmp_path) -> None:
    """引擎抛错 → `state == "failed"`、`error` 有内容，**异常不外传**。"""
    session = synth_session(tmp_path / "250919")
    engine = FakeEngine(boom="显存不够")

    summary = run_session(session, engine=engine, config=FakeConfig())

    status = read_status(session)
    assert status["state"] == "failed"
    assert REASON_ENGINE_ERROR in (status["error"] or "")
    assert "RuntimeError: 显存不够" in (status["error"] or "")
    assert summary["state"] == "failed"
    # 两路都坏：两路在 `artifacts` 里**各有一格**（都带着 `state = failed`）
    assert {row["state"] for row in status["artifacts"].values()} == {"failed"}
    assert set(status["artifacts"]) == {"mic", "loopback"}
    assert status["artifacts"]["mic"]["reason"] == REASON_ENGINE_ERROR
    assert not (session / "meeting_offline_transcript_mic.md").exists()
    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    # 失败原因分布按原因聚合：两路各一次，因此计数是 2
    assert f"{REASON_ENGINE_ERROR}：2" in report
    assert "## 实时率" in report


def test_engine_load_failure_lands_as_its_own_reason(tmp_path, monkeypatch) -> None:
    """真引擎那台机器上**第一次加载就失败**：这一趟如实落成失败、写明是加载失败。

    惰性加载失手按 `engine_load_error` 登记，与「某一块没跑成」的 `engine_error` 在原因词上
    分得开；而且**只加载一次、不重试**（第二路不再敲一遍模型）。

    注入接缝用的是 `meeting.offline.engine._load_models`
    （全链路只有它一处是模型入口）——真模型一个字节都不加载。
    """
    from meeting.offline import engine as engine_module

    session = synth_session(tmp_path / "250919")
    attempts: list[str] = []

    def failing_load(model, *, dtype, device):
        attempts.append(model)
        raise OSError("模型快照读不动（合成的失败，没有真模型参与）")

    monkeypatch.setattr(engine_module, "_load_models", failing_load)

    summary = run_session(session, config=FakeConfig())

    status = read_status(session)
    assert status["state"] == "failed"
    assert REASON_ENGINE_LOAD_ERROR in (status["error"] or "")
    assert "模型快照读不动" in (status["error"] or "")
    # 逐路那一档同样写「加载失败」这个词档，读方一眼看得出模型压根没起来
    assert set(status["artifacts"]) == {"mic", "loopback"}
    for row in status["artifacts"].values():
        assert row["state"] == "failed"
        assert row["reason"] == REASON_ENGINE_LOAD_ERROR
        assert REASON_ENGINE_LOAD_ERROR in row["error"]
    assert summary["state"] == "failed"
    # **不重试**：两路共用同一台引擎，但加载只发生了一次
    assert len(attempts) == 1, attempts
    # 加载失败不落任何产物文件
    assert not (session / "meeting_offline_transcript_mic.md").exists()
    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    assert f"{REASON_ENGINE_LOAD_ERROR}：2" in report


def test_a_whole_recording_of_silence_never_even_loads_the_model(tmp_path, monkeypatch) -> None:
    """整路全静音：模型**一次都不加载**，终态照旧 `done` + `silent: true`。

    不必用就不许把模型拉起来占显存。这一条钉住的是：一块都不必送引擎的那一趟，
    连加载那一下都不发生。
    """
    from meeting.offline import engine as engine_module

    session, _ranges = silence_session(tmp_path, silent_chunks=(1, 2, 3))
    loads: list[str] = []

    def counted_load(model, *, dtype, device):
        loads.append(model)
        raise OSError("就算被叫起来也加载不动（合成的失败，没有真模型参与）")

    monkeypatch.setattr(engine_module, "_load_models", counted_load)

    summary = run_session(
        session, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS})
    )

    # 真引擎的模型入口一次都没被敲（这一格的计数就是「模型被拉起来几次」）
    assert loads == [], "整路全静音那一场不该把模型拉起来：一次加载都不该发生"
    assert summary["state"] == "done"
    assert summary["partial"] is False
    assert summary["silent"] is True
    status = read_status(session)
    assert status["state"] == "done"
    assert status["silent"] is True
    assert status["error"] is None
    assert REASON_ENGINE_LOAD_ERROR not in json.dumps(status, ensure_ascii=False)
    # 产物照落：这一场是一份空转写配一条「没声」注释
    transcript = session / "meeting_offline_transcript_mic.md"
    assert transcript.exists()
    assert artifacts.read_transcript_lines(transcript) == []


def test_a_source_with_a_block_to_send_still_lands_as_engine_load_error(tmp_path) -> None:
    """真有块要送引擎的那一趟：第一次加载失手照旧**独立成 `engine_load_error` 档**。

    这一条与上一条是一对：那一条钉「不必用就不许把模型拉起来」，这一条钉「真要用的那一档
    不许被削弱」。场景挑的是最容易翻车的一档 —— 第 1 块是数字静音（跳过引擎）、第 2、3 块
    有信号：探测推迟到第 2 块那一刻，加载失手即收场。`loads == 1` 同时说明探针**没有**在
    进循环之前先跑一遍、也没有对第 3 块再探第二遍。
    """
    session, _ranges = silence_session(tmp_path, silent_chunks=(1,), name="mixed-load")
    engine = LoadingEngine(load_error="模型快照读不动（合成的失败，没有真模型参与）")

    summary = run_session(
        session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS})
    )

    assert engine.loads == 1, "加载只该探这一次（第 2 块那一刻）"
    assert engine.calls == [], "模型没起来时一次 transcribe 都不该发生"
    assert summary["state"] == "failed"
    assert REASON_ENGINE_LOAD_ERROR in (summary["error"] or "")
    status = read_status(session)
    assert status["state"] == "failed"
    assert REASON_ENGINE_LOAD_ERROR in (status["error"] or "")
    row = status["artifacts"]["mic"]
    assert row["state"] == "failed"
    assert row["reason"] == REASON_ENGINE_LOAD_ERROR
    assert REASON_ENGINE_LOAD_ERROR in row["error"]
    # 加载失败不落任何产物文件（这一格与「跑成」分得开）
    assert not (session / "meeting_offline_transcript_mic.md").exists()


def test_an_exception_escaping_run_session_still_leaves_a_failed_status(
    tmp_path, monkeypatch
) -> None:
    """从 `run_session` 逃出去的异常：状态文件落 `failed`、写明原因，异常照旧上抛。

    这一层自己兜住，谁拉起这一趟都一样。

    **两路都没轮到时逐路那一格的词档是 `not_run`**：现开的那一格 `reasons` 空、`error`
    是 `null`（引擎一次都没被喊过），兜成 `engine_error` 会把「压根没跑」说成「引擎跑这一块
    时抛错」，而那一格自己没话可对。读了这一格就知道「是没轮到」而不是「跑了没成」；
    真原因在顶层 `error` 那句上。
    """
    from meeting.offline import runner as runner_module

    session = synth_session(tmp_path / "250919")

    def exploding_engine_descriptor(config=None, engine=None):
        raise RuntimeError("描述符这一步炸了（合成）")

    monkeypatch.setattr(
        runner_module, "engine_descriptor", exploding_engine_descriptor
    )

    with pytest.raises(RuntimeError, match="描述符这一步炸了"):
        run_session(session, engine=FakeEngine(), config=FakeConfig())

    status = read_status(session)
    assert status["state"] == "failed"
    assert status["finished_at"]
    assert "描述符这一步炸了" in (status["error"] or "")
    assert status["sources"] == ["mic", "loopback"]
    # 两路都没跑到：逐路各有一格 `failed`，读方看得出这一场缺的是全部
    assert {row["state"] for row in status["artifacts"].values()} == {"failed"}
    # 「没跑到」与「跑了没成」在这一格上分得开：专属词 + 空的 `error`（引擎一次都没喊过）
    assert set(status["artifacts"]) == {"mic", "loopback"}
    assert {row["reason"] for row in status["artifacts"].values()} == {REASON_NOT_RUN}
    assert REASON_NOT_RUN != REASON_ENGINE_ERROR
    assert {row["error"] for row in status["artifacts"].values()} == {None}
    # 顶层那一格照旧写着这一趟抛的是什么（真原因在那里，逐路那一格只说「没跑到」）
    assert status["artifacts"]["mic"]["state"] == "failed"
    assert status["artifacts"]["mic"]["error"] is None


def test_the_state_cell_alone_separates_one_dead_source_from_two_healthy_ones(
    tmp_path,
) -> None:
    """**只看 `state` 那一档**就能分开「两路都成」与「一路整条失败」，并看出缺的是哪一路。

    `verdict_of()` 是读方那一条判据的落点（只读 `state` 与 `artifacts.<source>.state`）。
    """
    healthy = synth_session(tmp_path / "healthy")
    run_session(healthy, engine=FakeEngine(), config=FakeConfig())

    half = synth_session(tmp_path / "half")

    class MicDeadEngine(FakeEngine):
        def transcribe(self, audio_path):
            if "mic" in Path(audio_path).name:
                self.calls.append(str(audio_path))
                raise RuntimeError("这一路解不出来")
            return super().transcribe(audio_path)

    run_session(half, engine=MicDeadEngine(), config=FakeConfig())

    both_bad = synth_session(tmp_path / "both-bad")
    run_session(both_bad, engine=FakeEngine(boom="两路都解不出来"), config=FakeConfig())

    healthy_status, half_status, dead_status = (
        read_status(healthy),
        read_status(half),
        read_status(both_bad),
    )
    healthy_verdict = verdict_of(healthy_status)
    half_verdict = verdict_of(half_status)
    dead_verdict = verdict_of(dead_status)

    assert healthy_verdict == ("done", [])
    assert half_verdict == ("partial", ["mic"]), "一路整条失败：缺的那一路读得出来"
    assert dead_verdict == ("failed", ["mic", "loopback"]), "两路都没成：整场落 `failed`"
    assert len({healthy_verdict[0], half_verdict[0], dead_verdict[0]}) == 3, "三档互不相同"
    # **顶上那一格自己也说得出口**（这一条是本轮 m1 的落点）：三种场面各落自己那一档，
    # 读方不看逐路那几格、只读 `state` 这**一格**，也分得开「有一路整条没交回」与
    # 「两路都成」—— 从前这一格三种场面里前两种都写 `done`。
    assert healthy_status["state"] == "done"
    assert half_status["state"] == "partial"
    assert dead_status["state"] == "failed"
    assert len({healthy_status["state"], half_status["state"], dead_status["state"]}) == 3


def test_one_broken_source_does_not_take_the_other_down(tmp_path) -> None:
    """一路引擎抛错、另一路正常 → **读方只看 `state` 那一档就分得开，缺的是哪一路也看得出来**。

    两路在 `artifacts` 里各有一格、各带 `state` / `reason` / `error`；这一路整条没交回
    就是 `partial`，与「两路都成」那一档（`done`）分得开。
    """
    session = synth_session(tmp_path / "250919")

    class HalfBrokenEngine(FakeEngine):
        def transcribe(self, audio_path):
            name = Path(audio_path).name
            self.calls.append(str(audio_path))
            if "mic" in name:
                raise RuntimeError("这一路解不出来")
            return [
                OfflineSegment(0.0, 1.0, "S01", "远端的一句"),
            ]

    run_session(session, engine=HalfBrokenEngine(), config=FakeConfig())

    status = read_status(session)
    # 一路跑成、一路整条失败：整场落 `partial`（产物可用、这是终态，而这一档说得出口）
    assert status["state"] == "partial"
    assert "mic" in (status["error"] or "")
    assert REASON_ENGINE_ERROR in (status["error"] or "")
    assert verdict_of(status) == ("partial", ["mic"])
    # 「缺的是哪一路」只看 `artifacts.<source>.state` 就读得出来，不必解析 `error` 那句话
    per_source = {source: row["state"] for source, row in status["artifacts"].items()}
    assert per_source == {"mic": "failed", "loopback": "done"}
    assert status["artifacts"]["mic"]["reason"] == REASON_ENGINE_ERROR
    assert status["artifacts"]["mic"]["error"] and "这一路解不出来" in status["artifacts"]["mic"]["error"]
    # 失败那一路照旧一份产物文件都不落（不留空文件充数）；成功那一路的产物名在那一格里
    assert status["artifacts"]["mic"]["transcript"] is None
    assert status["artifacts"]["loopback"]["transcript"] == "meeting_offline_transcript_loopback.md"
    assert (session / "meeting_offline_speakers_loopback.json").exists()


def test_engine_returning_nothing_lands_failed_for_that_source(tmp_path) -> None:
    """引擎跑通但一段都没交回 → 那一路记 `no_segments`（不落一份空转写充数）。"""
    session = synth_session(tmp_path / "250919")

    run_session(session, engine=FakeEngine([]), config=FakeConfig())

    status = read_status(session)
    assert status["state"] == "failed"
    assert REASON_NO_SEGMENTS in (status["error"] or "")
    assert not (session / "meeting_offline_transcript_mic.md").exists()


def test_an_unresolvable_engine_selector_lands_failed(tmp_path) -> None:
    """`post.engine` 指向不存在的东西 → 整场如实失败，异常同样不外传。

    这一档属于**加载**那一类（引擎压根没造出来），因此落 `engine_load_error`：
    与「引擎跑这一块时抛错」分开，读方从原因词上看得出模型没起来。
    """
    session = synth_session(tmp_path / "250919")

    summary = run_session(
        session,
        config=FakeConfig(**{"post.engine": "meeting.offline.nope:NoSuchEngine"}),
    )

    status = read_status(session)
    assert status["state"] == "failed"
    assert REASON_ENGINE_LOAD_ERROR in (status["error"] or "")
    assert "NoSuchEngine" in (status["error"] or "") or "nope" in (status["error"] or "")
    assert summary["state"] == "failed"
    assert {row["reason"] for row in status["artifacts"].values()} == {
        REASON_ENGINE_LOAD_ERROR
    }


def test_resolve_engine_hands_back_the_moss_implementation() -> None:
    """`post.engine = auto` 解析到本节点的引擎类；`null` 当场拒绝（这一层没有替身）。"""
    from meeting.offline.engine import MossOfflineEngine

    assert resolve_engine("auto")[0] is MossOfflineEngine
    assert resolve_engine("meeting.offline.engine:MossOfflineEngine")[0] is MossOfflineEngine
    with pytest.raises(LookupError):
        resolve_engine("null")
    with pytest.raises(LookupError):
        resolve_engine("meeting.offline.engine")  # 不是 module:Class 形状


def test_run_session_needs_a_session_dir() -> None:
    """场次目录是空值时当场抛 `ValueError`（没有落点就不装作跑过）。"""
    with pytest.raises(ValueError):
        run_session("")
    with pytest.raises(ValueError):
        run_session(None)  # type: ignore[arg-type]


def test_session_without_declared_sources_still_writes_the_report(tmp_path) -> None:
    """`session.json` 缺席时按两路全集跑：报告照样落地，两路都如实记成缺音频。"""
    session = tmp_path / "bare"
    session.mkdir()

    summary = run_session(session, engine=FakeEngine(), config=FakeConfig())

    assert summary["state"] == "failed"
    assert (session / artifacts.REPORT_NAME).exists()
    assert read_status(session)["sources"] == ["mic", "loopback"]


