"""`meeting/offline/runner.py` 的契约用例：一场会跑完，三份产物 + 状态文件落地。

所有用例一律注入**假引擎**（`tests/conftest.py` 的 5 秒看门狗与「不许加载真模型」两条
硬要求在跑）；音频是现场合成的最小 PCM（内容不重要，引擎是假的）。

要盯住的五条：

  - 两路隔离：`sources=["mic","loopback"]` 时两路各自成文件，`mic:S01` 与 `loopback:S01`
    同时存在且不相等；
  - 一路音频缺失 / 0 字节：那一路如实记录，另一路照常产出，整场仍落 `done`；
  - 引擎抛错：`state == "failed"` 且 `error` 有内容，**异常不许逃出 `run_session`**；
  - 引擎解析不出来（`post.engine` 指向不存在的东西）：整场如实失败，异常同样不外传；
  - 分块（`post.chunk_seconds`）：过阈值的音频真被切成连续不重叠的块、块内标签带 `cNN:`
    命名空间**且没有任何跨块合并**、每块的时间戳平移回整场时间轴、有一块没成时那一路落
    `partial` 并把缺失区间写进转写 / 对照表 / 报告 / 状态文件。

分块那几条的音频用 `sparse_pcm()` 造（`seek` 到目标长度再写一个字节）：一份 1000 秒的
假 PCM 因此当场就有、不占盘，假引擎也只读它需要的那几十个字节。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meeting.offline import artifacts
from meeting.offline.engine import OfflineSegment
from meeting.offline.runner import (
    CHUNK_DIR_NAME,
    REASON_ALL_CHUNKS_FAILED,
    REASON_EMPTY_PCM,
    REASON_ENGINE_ERROR,
    REASON_NO_AUDIO,
    REASON_NO_SEGMENTS,
    declared_sources,
    plan_chunks,
    read_chunk_seconds,
    resolve_audio,
    resolve_engine,
    run_session,
    write_chunk_wav,
)

SAMPLE_RATE = 16000


class FakeEngine:
    """假引擎：按 `script` 交回分段，也可以按 `boom` 抛错、或按 `gate` 卡住不动。

    它记下每次收到的音频路径（用例据此证明两路**分别**调用、没有先混音）。

    「慢引擎」那一档用 `gate`（一条闸门 `threading.Event`）来当：真实引擎的一次整段
    推理是**有明确终点的长活儿**，闸门由用例放行，比按秒数干等更贴近它。
    """

    def __init__(self, script=None, *, boom: str | None = None, gate=None,
                 device: str = "auto") -> None:
        self.script = script
        self.boom = boom
        self.gate = gate
        #: 真引擎构造时就把 `auto` 解析成 `cuda:0` / `cpu` 存在这一格上；产物读的是它
        self.device = device
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
    """最小的一份配置：只有会后那四个键（`runner` 只读它们）。"""

    def __init__(self, **values) -> None:
        self.values = {
            "post.engine": "auto",
            "post.model": "OpenMOSS-Team/MOSS-Transcribe-Diarize",
            "post.dtype": "bfloat16",
            # 缺省与 `config.DEFAULT_VALUES` 同值：用例的 3 秒音频一块装得下，走单趟
            "post.chunk_seconds": 450,
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


def report_row(root: Path, source: str) -> list[str]:
    """运行报告「逐路」表里这一路那一行（末一格是终态）。

    列序在 `artifacts.write_report()` 里定死：
    `| 路 | 音频 | 秒数 | 块数 | 段数 | 类数 | 类内秒数 | 墙钟 | 实时率 | 状态 |`
    """
    for line in (root / artifacts.REPORT_NAME).read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 10 and cells[0] == source:
            return cells
    raise AssertionError(f"报告里没有 {source} 那一行")


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


# ---------------------------------------------------------------------------
# 五、分块（`post.chunk_seconds`）：真被切开、块内命名空间、时间轴、缺段露出来
# ---------------------------------------------------------------------------

#: 分块用例统一用这一档块长（10 秒，够短到用例里跑得完，也够长到一片一段装得下）
CHUNK_SECONDS = 10.0


def sparse_pcm(session: Path, source: str, seconds: float) -> Path:
    """造一份**指定长度**的归档 PCM，并且**每一块里都有信号**。

    用例不需要真音频（引擎是假的），只需要「文件字节数 = 时长」这一个事实；但分块那条路
    会把每块写成 wav，而用例整段零值音频写出来的分块就是一片零，谁也分不清「切对了位置」
    与「切出一堆静音」。做法：先 `seek` 到末尾写出长度（稀疏文件，当场就有、不占盘），
    再按 0.5 秒一块把带信号的字节块写在**稀疏区之上**（长度只按真正写下的那些算）。
    """
    path = session / f"meeting_audio_{source}.pcm"
    total = int(seconds * SAMPLE_RATE) * 2
    pattern = b"\x01\x01" * 4096  # 8192 字节的非零样本（int16 的 257），每 4 秒铺一块
    with open(path, "wb") as handle:
        handle.seek(total - 2)
        handle.write(b"\x01\x00")
        step = len(pattern)
        for begin in range(0, total - step, step * 2):
            handle.seek(begin)
            handle.write(pattern)
    assert path.stat().st_size == total
    return path


def read_chunk_wav(path: Path) -> bytes:
    """读回临时分块 wav 的**原始帧字节**（用例据此确认切出来的是那块音频）。"""
    import wave

    with wave.open(str(path), "rb") as handle:
        return handle.readframes(handle.getnframes())


def test_write_chunk_wav_keeps_the_signal(tmp_path) -> None:
    """`write_chunk_wav()` 的 float32 → int16 换算不把信号压成零（`phase3` 踩过的那个坑）。

    这一条**刻意不放进产品代码**：真正安静的块写出来就是零，拿「全零」当错误判据会把
    安静的会议误判成故障。这条换算的正确性因此由用例直接盯。
    """
    import numpy as np

    wave_data = np.linspace(-0.5, 0.5, 3200, dtype=np.float32)
    target = write_chunk_wav(tmp_path / "chunk_01.wav", wave_data)
    raw = read_chunk_wav(target)

    assert len(raw) == 3200 * 2
    samples = np.frombuffer(raw, dtype="<i2")
    assert samples.any(), "写出来的 wav 是数字静音（float32 波形没乘 32767）"
    # 满量程那一档按 32767 换算，不过冲
    assert samples.max() == 16383 and samples.min() == -16383
    # 零就是零（安静的那几帧照实写零）
    silent = np.zeros(160, dtype=np.float32)
    quiet = np.frombuffer(read_chunk_wav(write_chunk_wav(tmp_path / "q.wav", silent)), dtype="<i2")
    assert not quiet.any()


class SlicingEngine:
    """假引擎：按收到的分块文件认出它是第几块，交回那一块的标签，并记下每次调用。

    块的归属读**文件名**里的序号（`chunk_02.wav` → 第 2 块；`runner.chunk_file()` 就用这个
    命名），再拿 `ranges` 核对这一块的字节数对不对 —— 于是「第几块被调了」「这一块是不是
    切在该在的位置」两件事都查得出来。每块交回 `S01` 与 `S02` 各一段，末块再多一段 `S03`。
    """

    #: 引擎抛错那一档：`boom_chunks` 里的块号抛，别的块照常交回
    def __init__(self, ranges, *, boom_chunks=(), empty_chunks=(), device="cuda:0") -> None:
        self.ranges = list(ranges)
        self.boom_chunks = set(boom_chunks)
        self.empty_chunks = set(empty_chunks)
        self.device = device
        self.calls: list[str] = []
        self.chunk_bytes: dict[str, bytes] = {}

    def _chunk_index(self, audio_path) -> int:
        raw = read_chunk_wav(Path(audio_path))
        self.chunk_bytes[str(audio_path)] = raw
        index = int(Path(audio_path).stem.split("_")[-1])
        begin, end, listed = next(item for item in self.ranges if item[2] == index)
        assert len(raw) == end - begin, (
            f"第 {index} 块的字节数不对：切出 {len(raw)}，该在 {begin}-{end}（{end - begin}）"
        )
        return index

    def transcribe(self, audio_path):
        self.calls.append(str(audio_path))
        index = self._chunk_index(audio_path)
        if index in self.boom_chunks:
            raise RuntimeError(f"第 {index} 块显存不够")
        if index in self.empty_chunks:
            return []
        produced = [
            OfflineSegment(1.0, 2.0, "S01", f"块 {index} 的甲"),
            OfflineSegment(2.5, 3.0, "S02", f"块 {index} 的乙"),
        ]
        # 末块多一句：某一块独有的标签因此查得出来
        if index == max(item[2] for item in self.ranges):
            produced.append(OfflineSegment(4.0, 4.5, "S03", f"块 {index} 的丙"))
        return produced


def chunk_ranges(seconds: float, *, chunk_seconds: float = CHUNK_SECONDS):
    """按 `runner` 的同一条切法算出每块覆盖的字节区间：`[(起点, 终点, 块号), …]`。"""
    step = int(chunk_seconds * SAMPLE_RATE) * 2
    total = int(seconds * SAMPLE_RATE) * 2
    out = []
    for index, begin in enumerate(range(0, total, step), 1):
        out.append((begin, min(begin + step, total), index))
    return out


def chunked_session(tmp_path, *, seconds=30.0, sources=("mic",), chunk_seconds=CHUNK_SECONDS):
    """合成一个分块用的场次目录（音频是真长度、内容是零），交回 `(目录, 块区间表)`。"""
    session = tmp_path / f"chunked-{int(seconds)}"
    session.mkdir(parents=True, exist_ok=True)
    for source in sources:
        sparse_pcm(session, source, seconds)
    (session / "session.json").write_text(
        json.dumps(
            {
                "session_id": session.name,
                "dir": str(session),
                "mode": list(sources)[0] if len(sources) == 1 else "both",
                "state": "done",
                "sources": [{"id": source} for source in sources],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return session, chunk_ranges(seconds, chunk_seconds=chunk_seconds)


def test_plan_chunks_splits_only_above_the_threshold() -> None:
    """切法本身：不超块长就是一块（单趟），超了就按块长连续切、末尾那块截到音频末尾。"""
    short = plan_chunks(449.0, 450.0)
    assert short.single_pass and short.namespace is False
    assert [(step.index, step.offset, step.seconds) for step in short.chunks] == [
        (1, 0.0, 449.0)
    ]

    long = plan_chunks(1000.0, 450.0)
    assert long.single_pass is False and long.namespace is True
    assert [(step.index, step.offset, step.seconds) for step in long.chunks] == [
        (1, 0.0, 450.0),
        (2, 450.0, 450.0),
        (3, 900.0, 100.0),
    ]
    # 连续、不重叠：后一块的起点正好等于前一块的终点
    for earlier, later in zip(long.chunks, long.chunks[1:]):
        assert earlier.offset + earlier.seconds == later.offset
    assert long.chunks[-1].offset + long.chunks[-1].seconds == 1000.0

    # `0` = 显式关掉分块：整段一次跑
    off = plan_chunks(5943.3, 0.0)
    assert off.single_pass and off.namespace is False
    assert off.chunks[0].seconds == 5943.3


def test_read_chunk_seconds_reads_the_config_key() -> None:
    """块长从 `post.chunk_seconds` 读；`0` 表示关掉，配置缺席时退回实测安全档 450。"""
    assert read_chunk_seconds(FakeConfig()) == 450
    assert read_chunk_seconds(FakeConfig(**{"post.chunk_seconds": 0})) == 0.0
    assert read_chunk_seconds(FakeConfig(**{"post.chunk_seconds": 600})) == 600.0
    # 残缺的配置对象（没有这一格 / 值不是数字）：退回默认档，不抛
    class Bare:
        def get(self, key, default=None):
            return default

    assert read_chunk_seconds(Bare()) == 450.0
    assert read_chunk_seconds(FakeConfig(**{"post.chunk_seconds": "很久"})) == 450.0


def test_long_audio_is_chunked_and_short_audio_is_not(tmp_path, monkeypatch) -> None:
    """过阈值的音频真被切成三块逐块调引擎；短音频仍然整段一次跑。"""
    session, ranges = chunked_session(tmp_path, seconds=30.0)
    engine = SlicingEngine(ranges)

    summary = run_session(session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS}))

    assert len(engine.calls) == 3, engine.calls
    assert [Path(call).name for call in engine.calls] == [
        "chunk_01.wav",
        "chunk_02.wav",
        "chunk_03.wav",
    ]
    # 每块的长度与切法一致（10s / 10s / 10s），没有重叠；切出来的是那块音频（有信号）。
    # 末块正好把音频读到末尾（`SlicingEngine` 会拿字节区间核对每一次切出来的长度）。
    lengths = [len(engine.chunk_bytes[call]) for call in engine.calls]
    assert lengths == [10 * SAMPLE_RATE * 2] * 3
    assert all(any(engine.chunk_bytes[call]) for call in engine.calls)
    assert summary["transcripts"]["mic"]["segments"] == 7
    # 跑完不留临时块
    assert not (session / CHUNK_DIR_NAME).exists()

    # 短音频：一块装得下 → 单趟，引擎拿到的是原始归档路径
    short_session = tmp_path / "short"
    short_session.mkdir()
    synth_session(short_session, sources=("mic",), seconds=3.0)
    short_engine = FakeEngine()
    run_session(short_session, engine=short_engine, config=FakeConfig())
    assert [Path(call).name for call in short_engine.calls] == ["meeting_audio_mic.pcm"]


def test_chunking_is_off_when_the_config_says_zero(tmp_path) -> None:
    """`post.chunk_seconds = 0` 是显式关掉：30 秒音频也整段一次跑（不切块）。"""
    session, _ = chunked_session(tmp_path, seconds=30.0)
    engine = FakeEngine()

    run_session(session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": 0}))

    assert [Path(call).name for call in engine.calls] == ["meeting_audio_mic.pcm"]
    assert not (session / CHUNK_DIR_NAME).exists()


def test_chunk_local_labels_are_namespaced_and_never_merged(tmp_path, monkeypatch) -> None:
    """块内编号带 `cNN:` 命名空间，**没有任何代码路径跨块合并标签**。

    三处断言合起来把「不合并」这件事钉死：

      1. `number_speakers()` 亲眼看到的每一个键都带块命名空间（在它身上挂观察者）；
      2. 每块的 `label_raw` 全落在自己那一块的命名空间里，跨块的集合交集为空；
      3. 类数 = 各块类数之和（同一块的 `S01` 与别块的 `S01` 各占一个全局编号，
         于是同一个人被算成两个编号 —— spec 明写的取舍）。
    """
    from meeting.offline import runner as runner_module
    from meeting.offline import speakers as speakers_module

    session, ranges = chunked_session(tmp_path, seconds=30.0)
    engine = SlicingEngine(ranges)
    seen_keys: list[set[str]] = []
    real_number_speakers = speakers_module.number_speakers

    def watching_number_speakers(source, segments):
        seen_keys.append({str(segment.speaker) for segment in segments})
        return real_number_speakers(source, segments)

    monkeypatch.setattr(runner_module, "number_speakers", watching_number_speakers)

    run_session(session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS}))

    # 1. 编号那一步看到的键全是带命名空间的（`cNN:Sxx`），一个裸 `Sxx` 都没有
    assert len(seen_keys) == 1, "同一路只该编号一次（一次跑完整路）"
    assert all(key.startswith("c") and key[1:3].isdigit() and ":" in key for key in seen_keys[0])
    assert seen_keys[0] == {"c01:S01", "c01:S02", "c02:S01", "c02:S02", "c03:S01", "c03:S02", "c03:S03"}

    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    # 2. 每块的原始标签落在自己那一块的命名空间里
    chunk_count = len(chunk_ranges(30.0))
    chunk_of = {f"c{index:02d}": set() for index in range(1, chunk_count + 1)}
    for row in document["speakers"]:
        chunk_of[row["label_raw"][:3]].add(row["label_raw"])
    assert chunk_of == {
        "c01": {"c01:S01", "c01:S02"},
        "c02": {"c02:S01", "c02:S02"},
        "c03": {"c03:S01", "c03:S02", "c03:S03"},
    }
    # 块与块之间的原始标签集合两两不相交（没有一处把别块的标签映射进来）
    assert chunk_of["c01"] & chunk_of["c02"] == set()
    assert chunk_of["c01"] & chunk_of["c03"] == set()
    assert chunk_of["c02"] & chunk_of["c03"] == set()

    # 3. 全局编号按「去重后的带命名空间键」逐个发：7 个键 → S01…S07，类数 = 各块之和
    locals_in_order = [row["local"] for row in document["speakers"]]
    assert locals_in_order == ["S01", "S02", "S03", "S04", "S05", "S06", "S07"]
    assert len(locals_in_order) == len(set(locals_in_order))
    assert len(document["speakers"]) == 2 + 2 + 3
    # 同一个块内的 `S01` 与 `S02` 拿到了不同的全局编号，别块的 `S01` 又是第三个编号
    c01 = {row["label_raw"]: row["local"] for row in document["speakers"] if row["label_raw"].startswith("c01")}
    c02 = {row["label_raw"]: row["local"] for row in document["speakers"] if row["label_raw"].startswith("c02")}
    assert c01["c01:S01"] != c01["c01:S02"]
    assert c01["c01:S01"] != c02["c02:S01"]
    # 转写里用的是全局编号，一行都不带 `cNN:`
    lines = artifacts.read_transcript_lines(session / "meeting_offline_transcript_mic.md")
    assert {row["speaker"] for row in lines} == {
        "S01", "S02", "S03", "S04", "S05", "S06", "S07",
    }
    # 对照表逐块记着这一路切了几块、每块多少段
    assert [item["index"] for item in document["chunks"]] == [1, 2, 3]
    assert [item["segments"] for item in document["chunks"]] == [2, 2, 3]
    assert [item["offset_seconds"] for item in document["chunks"]] == [0.0, 10.0, 20.0]
    assert document["audio"]["chunks"] == 3
    assert document["audio"]["partial"] is False


def test_chunk_timestamps_move_onto_the_whole_recording_timeline(tmp_path) -> None:
    """每块的时间戳都加上该块偏移：第 2、3 块的段落报的是原录音上的位置。"""
    session, ranges = chunked_session(tmp_path, seconds=30.0)
    engine = SlicingEngine(ranges)

    run_session(session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS}))

    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    assert [(item["start"], item["end"]) for item in document["segments"]] == [
        # 第 1 块（0s 起）：块内 1.0–2.0 与 2.5–3.0
        (1.0, 2.0), (2.5, 3.0),
        # 第 2 块（10s 起）：挪到 11.0–12.0 与 12.5–13.0
        (11.0, 12.0), (12.5, 13.0),
        # 第 3 块（20s 起）：挪到 21.0–22.0、22.5–23.0、24.0–24.5
        (21.0, 22.0), (22.5, 23.0), (24.0, 24.5),
    ]
    # 转写正文照旧分钟级：20 秒那一块的第一句落在 00:00、第三块的开头落在 00:21
    lines = artifacts.read_transcript_lines(session / "meeting_offline_transcript_mic.md")
    assert [row["stamp"] for row in lines] == [
        "00:00", "00:00", "00:00", "00:00", "00:00", "00:00", "00:00",
    ]
    # 换一条 20 分钟以上的音频，`[HH:MM]` 才能看出块间偏移（第 2 块从 00:10 起）
    long_session, long_ranges = chunked_session(
        tmp_path / "long", seconds=1250.0, chunk_seconds=600.0
    )
    long_engine = SlicingEngine(long_ranges)
    run_session(
        long_session, engine=long_engine, config=FakeConfig(**{"post.chunk_seconds": 600.0})
    )
    long_document = artifacts.read_speakers_artifact(
        long_session / "meeting_offline_speakers_mic.json"
    )
    # 第 2 块（600s 起）的第一段：块内 1.0–2.0 → 整场 601.0–602.0
    starts = [item["start"] for item in long_document["segments"]]
    assert starts[:5] == [1.0, 2.5, 601.0, 602.5, 1201.0]
    # 末块只有 50 秒（1200–1250s），不是整整 600 秒
    assert long_document["chunks"][-1]["seconds"] == 50.0
    assert long_document["audio"]["seconds"] == 1250.0
    long_lines = artifacts.read_transcript_lines(
        long_session / "meeting_offline_transcript_mic.md"
    )
    stamps = {row["stamp"] for row in long_lines}
    assert "00:10" in stamps   # 第 2 块里 601 秒那一段落在第 10 分钟
    assert "00:20" in stamps   # 第 3 块从 1200 秒起，落第 20 分钟
    assert "00:00" in stamps   # 第 1 块


def test_a_failed_chunk_surfaces_as_partial_with_the_missing_span(tmp_path) -> None:
    """有一块没成 → 那一路落 `partial`，缺的那一段在四个地方都写得出来。

    这是 C1 的另一半：从前一块抛错只留在报告里，转写照样缺 10 秒却报 `done`。
    现在的行为是：产物照落（跑成的部分不废掉），但**缺了哪一段**必须露在
    转写第一行、对照表、运行报告、状态文件四处。
    """
    session, ranges = chunked_session(tmp_path, seconds=30.0)
    engine = SlicingEngine(ranges, boom_chunks=(2,))

    summary = run_session(session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS}))

    status = read_status(session)
    # 整场仍落 `done`（产物可用、这是终态），但 `partial` 立起来、`error` 说清缺哪一段
    assert status["state"] == "done"
    assert status["partial"] is True
    assert summary["partial"] is True
    assert "mic" in status["error"]
    assert "10.0–20.0s" in status["error"]
    assert REASON_ENGINE_ERROR in status["error"]
    # 缺的那一段确实没有转写：只有第 1、3 块的内容
    lines = artifacts.read_transcript_lines(session / "meeting_offline_transcript_mic.md")
    assert [row["text"] for row in lines] == [
        "块 1 的甲", "块 1 的乙", "块 3 的甲", "块 3 的乙", "块 3 的丙",
    ]
    # 转写第一行是那条注释（读方跳过它，人一眼看得到缺了哪一段）
    head = (session / "meeting_offline_transcript_mic.md").read_text(encoding="utf-8").splitlines()[0]
    assert head.startswith(artifacts.TRANSCRIPT_NOTE_PREFIX)
    assert "10.0–20.0s" in head
    # 对照表：`audio.missing_spans` + 逐块的失败原因
    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    assert document["audio"]["partial"] is True
    assert document["audio"]["missing_spans"] == [[10.0, 20.0]]
    assert [item["error"] for item in document["chunks"]] == [
        None,
        "engine_error：RuntimeError: 第 2 块显存不够",
        None,
    ]
    assert [item["segments"] for item in document["chunks"]] == [2, 0, 3]
    assert summary["transcripts"]["mic"]["missing_spans"] == [[10.0, 20.0]]
    # 报告单独一节写明缺了哪几段
    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    assert "## 缺了哪几段（这一路的转写不完整）" in report
    assert "mic：缺 1 段（10.0–20.0s）" in report
    assert "这一场的转写不完整" in report
    assert "engine_error：1" in report


def test_every_chunk_failing_takes_the_source_down(tmp_path) -> None:
    """每一块都没成 → 这一路落 `failed`，两块产物照落、空表上明写终态与原因。

    M8：这一路在场次目录里**留下的是一份说明「一段都没转出来」的空表**。留下旧表不动
    会让读方把上一趟的成功结果当成这一趟的（`250906` 两路跑进同一个目录时撞过这一类事）。
    """
    session, ranges = chunked_session(tmp_path, seconds=30.0)
    engine = SlicingEngine(ranges, boom_chunks=(1, 2, 3))

    summary = run_session(session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS}))

    status = read_status(session)
    assert status["state"] == "failed"
    assert REASON_ALL_CHUNKS_FAILED in (status["error"] or "")
    assert REASON_ENGINE_ERROR in (status["error"] or "")
    assert "0.0–10.0s" in (status["error"] or "") and "20.0–30.0s" in (status["error"] or "")
    # 摘要里这一路带着终态：空表不是「跑成了但没人说话」，读方从这一格就看得出来
    row = summary["transcripts"]["mic"]
    assert row["state"] == "failed"
    assert row["segments"] == 0 and row["speaker_count"] == 0
    assert row["missing_spans"] == [[0.0, 10.0], [10.0, 20.0], [20.0, 30.0]]
    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    assert "all_chunks_failed" in report
    assert "缺 3 段" in report

    # 对照表：空表 + `state = "failed"` + 原因，三块的失败逐块记着
    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    assert document["state"] == "failed"
    assert REASON_ALL_CHUNKS_FAILED in (document["error"] or "")
    assert document["segments"] == [] and document["speakers"] == []
    assert [item["error"] is not None for item in document["chunks"]] == [True, True, True]
    assert document["audio"]["partial"] is True
    assert document["audio"]["missing_spans"] == [[0.0, 10.0], [10.0, 20.0], [20.0, 30.0]]
    # 转写：只有第一行那条注释，正文一行都没有（读方跳过注释 → 0 段）
    transcript_path = session / "meeting_offline_transcript_mic.md"
    body = transcript_path.read_text(encoding="utf-8")
    assert body.splitlines()[0].startswith(artifacts.TRANSCRIPT_NOTE_PREFIX)
    assert "缺 3 段" in body.splitlines()[0]
    assert artifacts.read_transcript_lines(transcript_path) == []
    # 报告那一行按终态写：整路全丢不许写成 `done`
    assert report_row(session, "mic")[-1] == "failed"
    # 状态文件把这两份产物的名字记着（它们确实在场，内容是空表）
    assert status["artifacts"]["mic"]["speakers"] == "meeting_offline_speakers_mic.json"


def test_a_chunked_class_count_is_not_a_headcount(tmp_path) -> None:
    """M3：分块模式下 `speakers` 的长度是**各块类数之和**，产物与报告都要把这件事说出来。

    实测口径：三块各出自 `cNN:Sxx` 命名空间的类数之和等于 `speakers` 的长度，
    `headcount` 那一格把「这个数从 3 块里来、读不出人数」明写给机器，
    报告的「类数与人数」一节把同一句话写给读报告的人。
    """
    session, ranges = chunked_session(tmp_path, seconds=30.0)
    engine = SlicingEngine(ranges)

    run_session(session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS}))

    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    per_chunk: dict[str, set[str]] = {}
    for row in document["speakers"]:
        per_chunk.setdefault(row["label_raw"][:3], set()).add(row["label_raw"])
    assert document["headcount"] == {
        "meaningful": False,
        "class_count": len(document["speakers"]),
        "chunk_count": 3,
        "basis": "sum_of_chunk_classes",
    }
    # 那个数确实是各块类数之和（不是任何一块内部的类数）
    assert document["headcount"]["class_count"] == sum(len(keys) for keys in per_chunk.values())
    assert max(len(keys) for keys in per_chunk.values()) < document["headcount"]["class_count"]

    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    assert "## 类数与人数" in report
    assert "切成 3 块跑" in report
    assert "各块类数之和" in report
    assert "headcount.meaningful = false" in report
    assert f"`speakers` 的长度（{len(document['speakers'])}）" in report


def test_a_single_pass_class_count_stays_a_class_count(tmp_path) -> None:
    """M3 的另一半：整段一次跑时那一格说的是「模型在这段音频里分出的类数」，不加分块的警告。"""
    session = synth_session(tmp_path / "single", sources=("mic",), seconds=3.0)

    run_session(session, engine=FakeEngine(), config=FakeConfig())

    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    assert document["headcount"] == {
        "meaningful": True,
        "class_count": 2,
        "chunk_count": 1,
        "basis": "single_pass_classes",
    }
    assert document["state"] == "done"
    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    assert "整段一次跑（1 次引擎调用）" in report
    assert "headcount.meaningful = true" in report


def test_a_single_pass_speakers_table_says_it_is_done(tmp_path) -> None:
    """终态那一格在跑成的那一路上写 `done`（M8 只在「每一块都没跑成」那一档改口径）。"""
    session = synth_session(tmp_path / "single", sources=("mic",), seconds=3.0)

    run_session(session, engine=FakeEngine(), config=FakeConfig())

    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    assert document["state"] == "done"
    assert document["error"] is None
    assert report_row(session, "mic")[-1] == "done"


def test_a_partly_failed_source_keeps_its_usable_table(tmp_path) -> None:
    """一块没成、别的块都在 → 表格仍然可用：`state = done` + `partial` + 缺失区间。"""
    session, ranges = chunked_session(tmp_path, seconds=30.0)
    engine = SlicingEngine(ranges, boom_chunks=(2,))

    run_session(session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS}))

    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    assert document["state"] == "done"
    assert document["audio"]["partial"] is True
    assert document["audio"]["missing_spans"] == [[10.0, 20.0]]
    assert document["segments"], "缺一块不代表这一路一段都没有"
    assert report_row(session, "mic")[-1] == "done"


def test_a_chunk_returning_nothing_counts_as_that_chunk_failing(tmp_path) -> None:
    """某一块一段都没交回 → 按那一块失败记（与单趟的 `no_segments` 同一档口径）。"""
    session, ranges = chunked_session(tmp_path, seconds=30.0)
    engine = SlicingEngine(ranges, empty_chunks=(2,))

    run_session(session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS}))

    status = read_status(session)
    assert status["state"] == "done" and status["partial"] is True
    assert "10.0–20.0s" in (status["error"] or "")
    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    assert document["chunks"][1]["error"].startswith(REASON_NO_SEGMENTS)
    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    assert f"{REASON_NO_SEGMENTS}：1" in report


def test_the_transcript_note_is_not_read_as_a_transcript_line(tmp_path) -> None:
    """那条注释行不占段号、不是正文：读方跳过它，别的行照旧逐字读出来。"""
    note = artifacts.incomplete_note([(10.0, 20.0), (30.0, 40.0)])
    assert note.startswith(artifacts.TRANSCRIPT_NOTE_PREFIX)
    assert artifacts.is_transcript_note(note)
    assert "10.0–20.0s" in note and "30.0–40.0s" in note

    path = tmp_path / "t.md"
    artifacts.write_transcript(
        path,
        [note, artifacts.format_transcript_line(0.0, "S01", "甲")],
    )
    rows = artifacts.read_transcript_lines(path)
    assert rows == [{"stamp": "00:00", "speaker": "S01", "text": "甲"}]


def test_two_sources_chunk_independently(tmp_path) -> None:
    """两路各自切、各自编号：`mic` 的 `c01:S01` 与 `loopback` 的 `c01:S01` 不相等。"""
    session = tmp_path / "both"
    session.mkdir()
    for source in ("mic", "loopback"):
        sparse_pcm(session, source, 20.0)
    (session / "session.json").write_text(
        json.dumps(
            {
                "session_id": "both",
                "dir": str(session),
                "mode": "both",
                "state": "done",
                "sources": [{"id": "mic"}, {"id": "loopback"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    engine = SlicingEngine(chunk_ranges(20.0))

    run_session(session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS}))

    mic = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    loop = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_loopback.json")
    mic_ids = {row["id"] for row in mic["speakers"]}
    loop_ids = {row["id"] for row in loop["speakers"]}
    # 20 秒切两块，每块 `S01` / `S02`、末块再多一个 `S03` → 5 个编号
    assert mic_ids == {"mic:S01", "mic:S02", "mic:S03", "mic:S04", "mic:S05"}
    assert loop_ids == {"loopback:S01", "loopback:S02", "loopback:S03", "loopback:S04", "loopback:S05"}
    assert mic_ids & loop_ids == set()
    # 两路各自的块内标签一样（同一个 `c01:S01`），全局编号也各自从 S01 起 —— 互不相认
    assert [row["label_raw"] for row in mic["speakers"]] == [
        row["label_raw"] for row in loop["speakers"]
    ]
    assert [row["local"] for row in mic["speakers"]] == [
        row["local"] for row in loop["speakers"]
    ]
