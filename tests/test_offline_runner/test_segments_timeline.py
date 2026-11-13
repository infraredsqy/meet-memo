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
    report_row,
)
from tests.test_offline_runner.test_silence_gate import (
    speech,
)

#: 慢档标记（收尾轮）：会后离线链的契约验收，归收尾跑。
pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# 十三、段列表：几段拼一条时间轴（会后链吃段列表）
# ---------------------------------------------------------------------------


def _write_segment(root: Path, source: str, index: int, seconds: float, *, amplitude: int) -> Path:
    """一路的一段 PCM：常值样本（`amplitude=0` 即整段数字静音）。"""
    path = root / f"meeting_audio_{source}_{index:03d}.pcm"
    path.write_bytes(_constant_pcm(seconds, amplitude))
    return path


def _constant_pcm(seconds: float, amplitude: int) -> bytes:
    import struct

    samples = int(seconds * SAMPLE_RATE)
    return struct.pack(f"<{samples}h", *([amplitude] * samples))


def test_segments_concatenate_into_one_timeline(tmp_path) -> None:
    """两段按序拼 = 连续时间轴：取音频按序交两段、总时长 5 秒、门与切块都跨段读得通。"""
    import numpy as np

    from meeting.offline import runner as runner_module

    session = tmp_path / "250919"
    session.mkdir()
    # 段 1 = 3 秒（前 1 秒有声、后 2 秒静音）；段 2 = 2 秒全有声
    seg1 = session / "meeting_audio_mic_001.pcm"
    seg1.write_bytes(_constant_pcm(1.0, 1000) + _constant_pcm(2.0, 0))
    seg2 = _write_segment(session, "mic", 2, 2.0, amplitude=1000)
    paths = [str(seg1), str(seg2)]

    resolved, note = resolve_audio(session, "mic")
    assert resolved == paths and note is None

    # 时长 = 各段相加（门与切法共用的总数）
    plan, _waveform, _attempted = runner_module._plan_for_source(
        session, "mic", paths, config=FakeConfig(**{"post.chunk_seconds": 2.0})
    )
    assert plan.total_seconds == pytest.approx(5.0)
    assert [step.offset for step in plan.chunks] == [0.0, 2.0, 4.0]

    # 门跨段判：静音区（段 1 的零窗）、有声区、以及**骑在段缝上**的区间
    assert runner_module.measure_span(paths, offset=1.2, seconds=1.0).silent is True
    assert runner_module.measure_span(paths, offset=0.2, seconds=0.5).silent is False
    assert runner_module.measure_span(paths, offset=2.5, seconds=1.0).silent is False
    assert runner_module.measure_span(paths, offset=4.5, seconds=0.4).silent is False
    # 跨段读一块：前半是段 1 的零、后半是段 2 的声
    counts = runner_module.pcm_sample_chunk_counts(paths, offset=2.5, seconds=1.0)
    assert int(counts.size) == SAMPLE_RATE
    assert int(np.max(np.abs(counts[: SAMPLE_RATE // 2]))) == 0
    assert int(np.max(np.abs(counts[SAMPLE_RATE // 2 :]))) == 1000


def test_single_pass_over_multiple_segments_feeds_one_concatenated_wav(tmp_path) -> None:
    """单趟跑但多于一段：各段按序拼成一份临时 wav 喂引擎（引擎拿到的仍是一条轴）。"""
    import struct

    session = tmp_path / "250919"
    session.mkdir()
    seg1 = _write_segment(session, "mic", 1, 1.0, amplitude=300)
    seg2 = _write_segment(session, "mic", 2, 1.0, amplitude=700)
    seen: list[bytes] = []

    class ReadingEngine:
        def transcribe(self, audio_path):
            seen.append(Path(audio_path).read_bytes())
            return [OfflineSegment(0.0, 2.0, "S01", "拼起来的一句话")]

    run_session(session, engine=ReadingEngine(), config=FakeConfig(**{"post.chunk_seconds": 0}))
    # 引擎只被调了一次，拿到的是 `_offline_chunks/` 里的一份 wav；数据区 = 段 1 + 段 2
    assert len(seen) == 1
    payload = seen[0]
    assert payload[:4] == b"RIFF"  # 一份现拼的 wav（引擎的入参总是单一文件）
    data = payload[44:]
    expected = _constant_pcm(1.0, 300) + _constant_pcm(1.0, 700)
    assert data == expected
    assert struct.unpack("<2h", data[:4]) == (300, 300)
    # 跑完临时块照例扫干净
    assert not (session / CHUNK_DIR_NAME).exists()
    assert seg1.exists() and seg2.exists()  # 段文件一个字节都不动


def test_multi_segment_mp3_series_feeds_the_decoded_waveform_not_raw_bytes(tmp_path) -> None:
    """**P1（offline-mp3-fallback-series-corrupt-input）的回归**：单趟多段的 MP3，引擎
    收到的是**已解波形写成的 wav**，不是把各段裸字节硬拼进 wav 容器的坏容器。

    旧行为（改前用自证脚本实证过）：`_materialize_series()` 无条件把各段裸字节按
    int16 PCM 流写进容器——文件头说 PCM、数据区却是 MP3 帧字节，
    `_plan_for_source()` 已解好的波形在这条支路被无视。本条钉在 `_execute_chunks()`
    层面（issue 的最小场景）：归档 PCM 缺席 + MP3 在场 + 多于一段，喂已解波形，
    引擎收到的数据区必须与 `write_chunk_wav(波形)` 逐字节一致、与两段裸字节不同。
    """
    import numpy as np

    from meeting.offline import runner as runner_module

    session = tmp_path / "series-mp3"
    session.mkdir()
    seg1 = session / "meeting_audio_mic_001.mp3"
    seg1.write_bytes(b"ID3" + bytes(range(253)))
    seg2 = session / "meeting_audio_mic_002.mp3"
    seg2.write_bytes(bytes(range(255, -1, -1)))
    waveform = speech(2.0)
    seen: list[bytes] = []

    class ReadingEngine:
        def transcribe(self, audio_path):
            seen.append(Path(audio_path).read_bytes())
            return [OfflineSegment(0.0, 2.0, "S01", "拼起来的一句话")]

    plan = runner_module.plan_chunks(2.0, 0.0)
    _merged, runs, load_failure, stopped = runner_module._execute_chunks(
        session, "mic", [str(seg1), str(seg2)],
        engine=ReadingEngine(), plan=plan, waveform=waveform, decode_attempted=True,
    )

    assert load_failure is None and stopped is False
    assert len(seen) == 1
    assert [run.reason for run in runs] == ["transcribed"]
    assert seen[0][:4] == b"RIFF", "引擎的入参是一份正经的 wav 容器"
    assert seen[0][44:] != seg1.read_bytes() + seg2.read_bytes(), "数据区不是裸 MP3 字节"
    expected = write_chunk_wav(tmp_path / "expected.wav", waveform).read_bytes()
    assert seen[0] == expected, "数据区 = 已解波形的 int16（write_chunk_wav 同一条）"
    assert not (session / CHUNK_DIR_NAME).exists()


def test_multi_segment_mp3_that_cannot_be_decoded_lands_failed_without_the_engine(
    tmp_path, monkeypatch
) -> None:
    """**同一条 P1 的另一半**：多段 MP3 解不出波形时**整路如实记失败**，引擎一次都不调。

    旧行为（改前用自证脚本实证过）：解不出波形的那一趟照样把裸字节拼成 wav 送引擎，
    假引擎还在坏容器上「跑成」了一块。现在坏容器不出手：解不出 → 逐块登记失败 →
    一块都没跑成 → `all_chunks_failed`，整路落 `failed`。
    """
    from meeting.offline import runner as runner_module

    session = tmp_path / "series-mp3-broken"
    session.mkdir()
    (session / "meeting_audio_mic_001.mp3").write_bytes(b"ID3" + b"\x00" * 64)
    (session / "meeting_audio_mic_002.mp3").write_bytes(b"ID3" + b"\x00" * 64)
    (session / "session.json").write_text(
        json.dumps(
            {"session_id": "series-mp3-broken", "sources": [{"id": "mic"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def boom(path):
        raise ValueError("这个容器解不出波形")

    monkeypatch.setattr(runner_module, "_decode_samples", boom)
    engine = FakeEngine()

    summary = run_session(session, engine=engine, config=FakeConfig())

    assert engine.calls == [], "解不出波形的多段 MP3 一次引擎调用都不该发生"
    assert summary["state"] == "failed"
    assert "all_chunks_failed" in (summary["error"] or "")
    status = read_status(session)
    assert status["artifacts"]["mic"]["state"] == "failed"
    assert not (session / CHUNK_DIR_NAME).exists()


def test_a_two_segment_session_feeds_both_segments_of_both_routes(tmp_path) -> None:
    """续接过的一场（每路两段）整链验收：两段都到引擎、audio_seconds 是两段之和。

    spec 完成判定「会后链拿这场会的音频喂模型时，两段都能被读到」的那一条：
    每路的段 1 + 段 2 按序拼成一份 wav 送进（假）引擎，逐字节可比；状态文件与
    运行报告的秒数读数跟着变成两段之和。全程假引擎，一次真模型调用都不发生。
    """
    session = tmp_path / "250919"
    session.mkdir()
    amplitudes = {"mic": (300, 700), "loopback": (120, 560)}
    for source, (first, second) in amplitudes.items():
        _write_segment(session, source, 1, 1.0, amplitude=first)
        _write_segment(session, source, 2, 1.0, amplitude=second)
    seen: list[tuple[str, bytes]] = []

    class ReadingEngine:
        def transcribe(self, audio_path):
            seen.append((Path(audio_path).name, Path(audio_path).read_bytes()))
            return [OfflineSegment(0.0, 2.0, "S01", "两段拼成的一条轴")]

    summary = run_session(
        session, engine=ReadingEngine(), config=FakeConfig(**{"post.chunk_seconds": 0})
    )

    assert summary["state"] == "done"
    # 两路各被调一次；每路拿到的数据区 = 它自己的段 1 + 段 2（按序、逐字节可比）
    assert len(seen) == 2
    expected = {
        source: _constant_pcm(1.0, first) + _constant_pcm(1.0, second)
        for source, (first, second) in amplitudes.items()
    }
    assert sorted(payload[44:] for _name, payload in seen) == sorted(expected.values())
    # 状态文件：整场 done，两路的秒数读数都是两段之和；对照表的 audio 那格同源
    status = read_status(session)
    assert status["state"] == "done"
    for source in ("mic", "loopback"):
        cell = status["artifacts"][source]
        assert cell["state"] == "done"
        assert cell["audio_seconds"] == pytest.approx(2.0)
        document = artifacts.read_speakers_artifact(session / cell["speakers"])
        assert document["audio"]["seconds"] == pytest.approx(2.0)
    # 运行报告逐路那一行的秒数读数同源
    for source in ("mic", "loopback"):
        assert float(report_row(session, source)[2]) == pytest.approx(2.0)
    assert not (session / CHUNK_DIR_NAME).exists()


def test_a_corrupted_wall_clock_changes_no_conclusion(monkeypatch, tmp_path) -> None:
    """故意把墙钟那一栏改错：程序的行为与结论**一个字都不变**（元信息不当判据）。

    两份完全相同的场次目录，一份的 `session.json` 墙钟字段与段戳被改成胡话；
    同一引擎各跑一遍，逐字节比较三份产物与状态文件——除此之外任何代码都不许拿
    墙钟核对（`docs/design-intent.md` 第 6 条；spec 完成判定第 4 条）。
    """
    # status 里 started_at 打的是 runner 运行那一刻的真实墙钟（秒级），跟结论无关；
    # 冻住这条时钟缝，逐字节比较才不用赌两次运行恰好落进同一秒。
    monkeypatch.setattr("meeting.offline.runner._now_iso", lambda: "2025-09-19T21:30:00")
    clean = synth_session(tmp_path / "clean" / "250919")
    corrupt = synth_session(tmp_path / "corrupt" / "250919")

    record = json.loads((corrupt / "session.json").read_text(encoding="utf-8"))
    record["date"] = "1999-99-99"
    record["created_at"] = "1999-12-31T23:59:59+08:00"
    record["started_at"] = "去年"
    record["ended_at"] = "明天"
    record["segments"] = [
        {
            "index": 1,
            "started_at": "比宇宙还早",
            "ended_at": None,
            "sources": ["mic", "loopback"],
            "pauses": [{"start": "永不", "end": "从未"}],
            "duration_seconds": -42.0,
        }
    ]
    record["audio_duration_seconds"] = 12345.0
    (corrupt / "session.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )

    outcomes = []
    for root in (clean, corrupt):
        engine = FakeEngine(
            [OfflineSegment(0.5, 2.5, "S01", "同一句")]
        )
        run_session(root, engine=engine, config=FakeConfig())
        outcomes.append(
            {
                "status": (root / artifacts.STATUS_NAME).read_text(encoding="utf-8"),
                "transcript_mic": (root / "meeting_offline_transcript_mic.md").read_text(encoding="utf-8"),
                "speakers_mic": (root / "meeting_offline_speakers_mic.json").read_text(encoding="utf-8"),
                "transcript_loopback": (root / "meeting_offline_transcript_loopback.md").read_text(encoding="utf-8"),
            }
        )

    assert outcomes[0] == outcomes[1]
    assert read_status(clean)["state"] == "done"


