"""`meeting/offline/runner.py` 的契约用例：一场会跑完，三份产物 + 状态文件落地。

所有用例一律注入**假引擎**（`tests/conftest.py` 的 5 秒看门狗与「不许加载真模型」两条
硬要求在跑）；音频是现场合成的最小 PCM（内容不重要，引擎是假的）。

要盯住的四条：

  - 两路隔离：`sources=["mic","loopback"]` 时两路各自成文件，`mic:S01` 与 `loopback:S01`
    同时存在且不相等；
  - 一路音频缺失 / 0 字节：那一路如实记录，另一路照常产出，整场仍落 `done`；
  - 引擎抛错：`state == "failed"` 且 `error` 有内容，**异常不许逃出 `run_session`**；
  - 引擎解析不出来（`post.engine` 指向不存在的东西）：整场如实失败，异常同样不外传。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meeting.offline import artifacts
from meeting.offline.engine import OfflineSegment
from meeting.offline.runner import (
    REASON_EMPTY_PCM,
    REASON_ENGINE_ERROR,
    REASON_NO_AUDIO,
    REASON_NO_SEGMENTS,
    declared_sources,
    resolve_audio,
    resolve_engine,
    run_session,
)

SAMPLE_RATE = 16000


class FakeEngine:
    """假引擎：按 `script` 交回分段，也可以按 `boom` 抛错、或按 `gate` 卡住不动。

    它记下每次收到的音频路径（用例据此证明两路**分别**调用、没有先混音）。

    「慢引擎」那一档用 `gate`（一条闸门 `threading.Event`）来当：真实引擎的一次整段
    推理是**有明确终点的长活儿**，闸门由用例放行，比按秒数干等更贴近它。
    """

    def __init__(self, script=None, *, boom: str | None = None, gate=None) -> None:
        self.script = script
        self.boom = boom
        self.gate = gate
        self.calls: list[str] = []

    def transcribe(self, audio_path):
        if self.gate is not None:
            self.gate.wait(timeout=10.0)
        self.calls.append(str(audio_path))
        if self.boom:
            raise RuntimeError(self.boom)
        if self.script is not None:
            return list(self.script)
        return [
            OfflineSegment(0.0, 1.0, "S01", f"{Path(audio_path).name} 的一句"),
            OfflineSegment(1.0, 2.5, "S02", f"{Path(audio_path).name} 的另一句"),
        ]


class FakeConfig:
    """最小的一份配置：只有会后那三个键（`runner` 只读它们）。"""

    def __init__(self, **values) -> None:
        self.values = {
            "post.engine": "auto",
            "post.model": "OpenMOSS-Team/MOSS-Transcribe-Diarize",
            "post.dtype": "bfloat16",
        }
        self.values.update(values)

    def get(self, key, default=None):
        return self.values.get(key, default)


def synth_session(
    root: Path,
    *,
    sources=("mic", "loopback"),
    seconds=3.0,
    empty=(),
    missing=(),
    session_id="250919",
) -> Path:
    """合成一个场次目录：两路 16k 单声道 int16 裸 PCM + 一份最小的 `session.json`。"""
    root.mkdir(parents=True, exist_ok=True)
    for source in sources:
        path = root / f"meeting_audio_{source}.pcm"
        if source in missing:
            continue
        if source in empty:
            path.write_bytes(b"")
            continue
        path.write_bytes(b"\x00\x01" * int(seconds * SAMPLE_RATE))
    # `session.json` 声明的是**这一场开过的路**（文件在不在场由 `missing` / `empty` 说了算）
    declared = list(sources)
    (root / "session.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "dir": str(root),
                "mode": "both" if len(declared) > 1 else (declared or ["mic"])[0],
                "state": "done",
                "sources": [
                    {"id": source, "archive": f"meeting_audio_{source}.pcm"}
                    for source in declared
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return root


def read_status(root: Path) -> dict:
    return artifacts.read_status(root / artifacts.STATUS_NAME)


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
        assert document["audio"]["file"] == f"meeting_audio_{source}.pcm"
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
        "meeting_audio_mic.pcm",
        "meeting_audio_loopback.pcm",
    ]


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
    """loopback 的归档不在场 → 那一路记 `no_audio`，mic 照常产出，整场仍 `done`。"""
    session = synth_session(tmp_path / "250919", missing=("loopback",))
    engine = FakeEngine()

    summary = run_session(session, engine=engine, config=FakeConfig())

    status = read_status(session)
    assert status["state"] == "done"
    assert REASON_NO_AUDIO in (status["error"] or "")
    assert "loopback" in (status["error"] or "")
    # 缺席那一路不落产物（也不留一份空文件充数）
    assert not (session / "meeting_offline_transcript_loopback.md").exists()
    assert "loopback" not in status["artifacts"]
    # 另一路照常
    assert (session / "meeting_offline_transcript_mic.md").exists()
    assert "mic" in status["artifacts"]
    # 引擎只被调了一次，收到的是 mic 那一路
    assert [Path(call).name for call in engine.calls] == ["meeting_audio_mic.pcm"]
    # 报告里两路各一行，失败那一行的原因查得到
    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    assert "| loopback | （缺）" in report
    assert f"{REASON_NO_AUDIO}：" in report
    assert summary["transcripts"]["mic"]["segments"] == 2
    assert "loopback" not in summary["transcripts"]


def test_an_empty_pcm_falls_back_to_the_mp3(tmp_path) -> None:
    """归档 PCM 是 0 字节、同目录有 MP3 → 走 MP3（收尾转码的产物），并记一笔。"""
    session = synth_session(tmp_path / "250919", empty=("loopback",))
    mp3 = session / "meeting_audio_loopback.mp3"
    mp3.write_bytes(b"ID3 fake mp3 bytes")
    engine = FakeEngine()

    path, note = resolve_audio(session, "loopback")
    assert path == str(mp3)
    assert "MP3" in (note or "")

    run_session(session, engine=engine, config=FakeConfig())

    assert read_status(session)["state"] == "done"
    assert (session / "meeting_offline_transcript_loopback.md").exists()
    assert {Path(call).name for call in engine.calls} == {
        "meeting_audio_mic.pcm",
        "meeting_audio_loopback.mp3",
    }


def test_an_empty_pcm_without_an_mp3_fails_that_source_only(tmp_path) -> None:
    """归档 PCM 是 0 字节、又没有 MP3 → 那一路记 `empty_pcm`，另一路照常，整场仍 `done`。"""
    session = synth_session(tmp_path / "250919", empty=("mic",))
    engine = FakeEngine()

    summary = run_session(session, engine=engine, config=FakeConfig())

    status = read_status(session)
    assert status["state"] == "done"
    assert REASON_EMPTY_PCM in (status["error"] or "")
    assert "mic" not in status["artifacts"]
    assert "loopback" in status["artifacts"]
    assert summary["transcripts"]["loopback"]["segments"] == 2


def test_both_sources_unavailable_lands_failed(tmp_path) -> None:
    """两路都没有音频 → 整场落 `failed`（一份产物都出不来，如实报）。"""
    session = synth_session(tmp_path / "250919", missing=("loopback",))
    (session / "meeting_audio_mic.pcm").unlink()

    run_session(session, engine=FakeEngine(), config=FakeConfig())

    status = read_status(session)
    assert status["state"] == "failed"
    assert status["error"] and REASON_NO_AUDIO in status["error"]
    assert status["finished_at"]


# ---------------------------------------------------------------------------
# 四、引擎抛错：异常不许逃出 `run_session`
# ---------------------------------------------------------------------------


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
    assert status["artifacts"] == {}
    assert not (session / "meeting_offline_transcript_mic.md").exists()
    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    # 失败原因分布按原因聚合：两路各一次，因此计数是 2
    assert f"{REASON_ENGINE_ERROR}：2" in report
    assert "## 实时率" in report


def test_one_broken_source_does_not_take_the_other_down(tmp_path) -> None:
    """一路引擎抛错、另一路正常 → 整场落 `done`，坏的那一路记 `engine_error`。"""
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
    assert status["state"] == "done"
    assert "mic" in (status["error"] or "")
    assert REASON_ENGINE_ERROR in (status["error"] or "")
    assert "mic" not in status["artifacts"]
    assert "loopback" in status["artifacts"]
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
    """`post.engine` 指向不存在的东西 → 整场如实失败，异常同样不外传。"""
    session = synth_session(tmp_path / "250919")

    summary = run_session(
        session,
        config=FakeConfig(**{"post.engine": "meeting.offline.nope:NoSuchEngine"}),
    )

    status = read_status(session)
    assert status["state"] == "failed"
    assert REASON_ENGINE_ERROR in (status["error"] or "")
    assert "NoSuchEngine" in (status["error"] or "") or "nope" in (status["error"] or "")
    assert summary["state"] == "failed"


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
