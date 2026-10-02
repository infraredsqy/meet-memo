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

段列表（本节点）：会后链吃的是**段列表**——每路的段文件按序号升序拼 = 连续时间轴；
取音频、量长度、切块、门、单趟拼接全都在这条轴上工作（本目录底部的「段列表」一节）。
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
    """最小的一份配置：只有会后那几个键（`runner` 只读它们）。"""

    def __init__(self, **values) -> None:
        self.values = {
            "post.engine": "auto",
            "post.model": "OpenMOSS-Team/MOSS-Transcribe-Diarize",
            "post.dtype": "bfloat16",
            # 缺省与 `config.DEFAULT_VALUES` 同值：用例的 3 秒音频一块装得下，走单趟
            "post.chunk_seconds": 450,
            # 会后纪要那一层的钩子：测试层收成缺席档（`minutes.hook = null`）——
            # 本机 PATH 上真的有 `dsh`，`auto` 会让每一场跑完离线链的用例都真的拉起一个
            # headless 子会话去跑五阶段。要验钩子的用例自己传 `minutes.hook` 覆盖它。
            "minutes.hook": "null",
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
        path = root / f"meeting_audio_{source}_001.pcm"
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
                    {"id": source, "archive": f"meeting_audio_{source}_001.pcm"}
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


def gate_headline(root: Path) -> str:
    """运行报告开头那一行「数字静音门：…」（那一行是读报告的人第一眼看到的东西）。"""
    for line in (root / artifacts.REPORT_NAME).read_text(encoding="utf-8").splitlines():
        if line.startswith("- 数字静音门："):
            return line
    raise AssertionError("报告里没有「数字静音门」那一行")


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
    assert [Path(call).name for call in engine.calls] == ["meeting_audio_mic_001.pcm"]
    # 报告里两路各一行，失败那一行的原因查得到
    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    assert "| loopback | （缺）" in report
    assert f"{REASON_NO_AUDIO}：" in report
    assert summary["transcripts"]["mic"]["segments"] == 2
    assert "loopback" not in summary["transcripts"]


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
    (session / "meeting_audio_mic_001.pcm").unlink()

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
    path = session / f"meeting_audio_{source}_001.pcm"
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
    assert read_chunk_seconds(FakeConfig(**{"post.chunk_seconds": 0})) == 0.0
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
    assert [Path(call).name for call in short_engine.calls] == ["meeting_audio_mic_001.pcm"]


def test_chunking_is_off_when_the_config_says_zero(tmp_path) -> None:
    """`post.chunk_seconds = 0` 是显式关掉：30 秒音频也整段一次跑（不切块）。"""
    session, _ = chunked_session(tmp_path, seconds=30.0)
    engine = FakeEngine()

    run_session(session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": 0}))

    assert [Path(call).name for call in engine.calls] == ["meeting_audio_mic_001.pcm"]
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


# ---------------------------------------------------------------------------
# 六、数字静音门：没声的块不调引擎，安静**但有声**的块照调
#
# 实测那一档（`250906` 环回）是**样本全为零**的五块（第 8/16/17/18/19 块，
# `volumedetect` 读 `-91.0 dB`），而引擎在它们身上编出了占那一路输出 69.32% 的假正文。
# 这一节的用例分两半：一半盯「全零跳过」，另一半盯那条**安全性质**——
# 只要有任何一个非零样本，这一块就必须照常送进引擎（丢掉真内容比这个 bug 更坏）。
# ---------------------------------------------------------------------------


def silence_session(
    tmp_path,
    *,
    seconds=30.0,
    chunk_seconds=CHUNK_SECONDS,
    silent_chunks=(),
    quiet_chunks=(),
    source="mic",
    name="silence",
    ranges=None,
):
    """合成一个「哪几块是数字静音」写在脸上的场次：`silent_chunks` 那几块全零。

    零时长音频与真音频在盘上只有字节的区别：`silent_chunks` 那几块写全零，
    其余块写一个有信号的字节（`\\x00\\x01` = int16 的 256，与 `synth_session()` 同一档
    最小信号）。`quiet_chunks` 那几块另外只写**一个**样本（`\\x01\\x00` → 16 bit 满量程的
    -90.3 dB），用来验「安静但有声」那一半。交回 `(场次目录, 块区间表)`。

    `ranges` 显式给出块区间表（`chunk_seconds = 0` 那一档算不出块表：整段就是一块）。
    """
    session = tmp_path / name
    session.mkdir(parents=True, exist_ok=True)
    if ranges is None:
        ranges = chunk_ranges(seconds, chunk_seconds=chunk_seconds)
    path = session / f"meeting_audio_{source}_001.pcm"
    payload = bytearray(int(seconds * SAMPLE_RATE) * 2)
    for begin, _end, index in ranges:
        if index in silent_chunks:
            continue  # 全零：这一块就是实测那一档的数字静音
        payload[begin:begin + 2] = b"\x01\x00" if index in quiet_chunks else b"\x00\x01"
    path.write_bytes(bytes(payload))
    (session / "session.json").write_text(
        json.dumps(
            {
                "session_id": session.name,
                "dir": str(session),
                "mode": source,
                "state": "done",
                "sources": [{"id": source}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return session, ranges


def test_a_silent_chunk_never_reaches_the_engine(tmp_path) -> None:
    """数字静音的块**一次引擎调用都不发生**，跳过的区间在四处都露出来。

    这一条是那个缺陷的直接回归：`250906` 那五块全零音频被送进引擎，引擎编出
    20 690 字假正文（占那一路 69.32%），而那一路报的是 `state: done`、`error: null`。
    现在那几块根本不进引擎，并且**跳过的区间照样写在产物里**。
    """
    session, ranges = silence_session(tmp_path, silent_chunks=(2,))
    engine = SlicingEngine(ranges)

    summary = run_session(
        session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS})
    )

    # 1. 第 2 块没有送进引擎（第 1、3 块照调）
    assert [Path(call).name for call in engine.calls] == ["chunk_01.wav", "chunk_03.wav"]
    # 2. 假正文那一段的位置上没有任何字：转写只有第 1、3 块的内容
    lines = artifacts.read_transcript_lines(session / "meeting_offline_transcript_mic.md")
    assert [row["text"] for row in lines] == [
        "块 1 的甲", "块 1 的乙", "块 3 的甲", "块 3 的乙", "块 3 的丙",
    ]
    # 3. 转写第一行是一条「没声」注释（读方跳过它，行格式契约不受影响）
    head = (session / "meeting_offline_transcript_mic.md").read_text(
        encoding="utf-8"
    ).splitlines()[0]
    assert head.startswith(artifacts.TRANSCRIPT_NOTE_PREFIX)
    assert "10.0–20.0s" in head
    # 4. 对照表：跳过的区间在自己的那一格上，缺失区间那一格是空的
    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    assert document["audio"]["silent"] is True
    assert document["audio"]["silent_spans"] == [[10.0, 20.0]]
    assert document["audio"]["missing_spans"] == []
    assert document["audio"]["partial"] is False
    assert document["state"] == "done"
    assert document["error"] is None
    # 逐块那一格记的仍是「成功 / 失败」两档，**跳过那一块不占 `error`**：
    # 「没声」这件事由 `audio.silent_spans` 说话（`chunks` 的五格契约不变）
    assert [item["error"] for item in document["chunks"]] == [None, None, None]
    assert [item["segments"] for item in document["chunks"]] == [2, 0, 3]
    # 5. 整场：`state` 仍是 `done`、`partial` 仍是 `false`，跳过的是哪一段写在 `silent_spans`
    status = read_status(session)
    assert status["state"] == "done"
    assert status["partial"] is False
    assert status["silent"] is True
    assert status["silent_spans"] == [[10.0, 20.0]]
    assert status["artifacts"]["mic"]["silent_spans"] == [[10.0, 20.0]]
    assert status["error"] is None
    assert summary["silent"] is True
    assert summary["silent_spans"] == [[10.0, 20.0]]
    # 6. 报告单列一节，并且那一档与「不完整」分开写
    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    assert "## 没声而跳过的音频（这一档什么都没出错）" in report
    assert "mic：跳过 1 段（10.0–20.0s）" in report
    assert "这一场的转写不完整" not in report
    assert "digital_silence：1" in report
    # 报告里的「引擎调用」数**不含**跳过的那一块：3 块里只调了 2 次
    assert "引擎调用：2 次" in report
    assert "- mic：2 次调用（音频 30.0s），合计" in report
    assert "没声、跳过引擎" in report


def test_a_quiet_but_real_chunk_still_reaches_the_engine(tmp_path) -> None:
    """**安全性质**：安静但有声的块必须照常送引擎（判据只认「峰值恰好为 0」）。

    这一半比上一半更重要：把真内容当静音丢掉，是比引擎编字更坏的错。下面三档都验：

      - 峰值为 ±1 的块（16 bit 满量程的 -90.3 dB，比噪声底还低）—— 照调；
      - 短于 `MIN_GATE_SAMPLES` 的全零块 —— **不判静音**，照调（样本不足时不替引擎下结论）；
      - 一段里只有一个非零样本的块 —— 照调。
    """
    session, ranges = silence_session(tmp_path, quiet_chunks=(2,))
    engine = SlicingEngine(ranges)

    run_session(session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS}))

    assert [Path(call).name for call in engine.calls] == [
        "chunk_01.wav", "chunk_02.wav", "chunk_03.wav",
    ]
    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    assert document["audio"]["silent_spans"] == []
    assert "块 2 的甲" in [row["text"] for row in document["segments"]]

    # 短于 0.1 秒的全零：不算静音（`is_digital_silence()` 那一条长度判据）
    import numpy as np

    from meeting.offline.runner import MIN_GATE_SAMPLES, is_digital_silence

    assert is_digital_silence(np.zeros(10, dtype=np.int16)) is False
    assert is_digital_silence(np.zeros(MIN_GATE_SAMPLES - 1, dtype=np.int16)) is False
    # 只有**一个**非零样本的整段：也不算静音
    one_peak = np.zeros(MIN_GATE_SAMPLES + 500, dtype=np.int16)
    one_peak[7] = 1
    assert is_digital_silence(one_peak) is False
    # 全是零、又够长：静音
    assert is_digital_silence(np.zeros(MIN_GATE_SAMPLES, dtype=np.int16)) is True
    # 空波形：没有音频可吃，算静音
    assert is_digital_silence(np.zeros(0, dtype=np.int16)) is True


def test_a_float_waveform_with_a_real_signal_never_looks_silent(tmp_path) -> None:
    """`decode_audio()` 那种 `[-1, 1]` 的 float32 波形**不许**被判成静音。

    这是复跑真素材第一版踩到的坑：判据拿「计数」比，而 mp3 那条路交上来的是 float32，
    `int()` 把每个小数截成 0，于是一串真有声的音频看着像全零 —— 19 块一条不漏地全被跳过，
    整份转写一个字不剩。换算必须先过 `samples_to_counts()`（与 `write_chunk_wav()` 同一条）。

    三档都验：有信号的 float32 波形不判静音；全零的 float32 波形判静音；
    int16 计数与等价的 float32 得同一个答案。
    """
    import numpy as np

    from meeting.offline.runner import is_digital_silence, samples_to_counts

    seconds = 3.0
    samples = int(seconds * SAMPLE_RATE)
    t = np.arange(samples, dtype=np.float32) / np.float32(SAMPLE_RATE)
    speech = (0.2 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
    assert is_digital_silence(speech) is False
    # 极轻的一段（float32 的 1/32767，也就是 int16 的 1 格）也不算静音
    faintest = np.zeros(samples, dtype=np.float32)
    faintest[10] = 1.0 / 32767.0
    assert is_digital_silence(faintest) is False
    # 全零的 float32 波形照样判静音
    assert is_digital_silence(np.zeros(samples, dtype=np.float32)) is True
    # 两种形态用同一把尺子：同一段音频的 int16 与 float32 交回同一个答案
    counts = np.zeros(samples, dtype=np.int16)
    counts[3] = -12345
    assert is_digital_silence(counts) is False
    assert is_digital_silence(counts.astype(np.float32) / np.float32(32768.0)) is False
    assert int(np.max(np.abs(samples_to_counts(counts)))) == 12345
    # 满量程的**负**样本也不许因为 `abs(-32768)` 溢出而被读成 0
    full_scale = np.zeros(samples, dtype=np.int16)
    full_scale[5] = -32768
    assert is_digital_silence(full_scale) is False


def test_a_silent_whole_recording_is_not_a_failure(tmp_path) -> None:
    """整段全零的单趟：`state` 落 `done`、`partial` 落 `false`，产物照落、0 段。

    这一档与「引擎一段都没交回」必须分得开：前者什么都没出错（那一段本来就没有音频），
    后者是引擎没吐东西。`all_chunks_failed` 那一档因此也要认「一块失败都没有」这个事实。
    """
    session, ranges = silence_session(
        tmp_path, seconds=30.0, chunk_seconds=0.0, silent_chunks=(1,), ranges=[(0, int(30.0 * SAMPLE_RATE) * 2, 1)]
    )
    engine = SlicingEngine(ranges)

    summary = run_session(session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": 0}))

    assert engine.calls == [], "整段全零的单趟不该调引擎"
    assert summary["state"] == "done"
    assert summary["partial"] is False
    assert summary["silent"] is True
    transcript_path = session / "meeting_offline_transcript_mic.md"
    assert transcript_path.exists()
    assert artifacts.read_transcript_lines(transcript_path) == []
    head = transcript_path.read_text(encoding="utf-8").splitlines()[0]
    assert head.startswith(artifacts.TRANSCRIPT_NOTE_PREFIX)
    assert "0.0–30.0s" in head
    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    assert document["state"] == "done"
    assert document["segments"] == [] and document["speakers"] == []
    assert document["audio"]["silent_spans"] == [[0.0, 30.0]]
    assert document["audio"]["partial"] is False
    assert read_status(session)["state"] == "done"


def test_every_chunk_silent_is_not_all_chunks_failed(tmp_path) -> None:
    """分块跑而**每一块**都没声：落的是「跳过」那一档，`all_chunks_failed` 一个都不出现。

    `250906` 那一路的 19 块里有 5 块是这一档。都是全零时整路一段字都没有，
    那是如实的结果（`state = done` + `silent_spans` 覆盖整段），与「19 块全失败」不同。
    """
    session, ranges = silence_session(tmp_path, silent_chunks=(1, 2, 3))
    engine = SlicingEngine(ranges)

    summary = run_session(
        session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS})
    )

    assert engine.calls == []
    assert summary["state"] == "done"
    assert summary["partial"] is False
    assert summary["silent_spans"] == [[0.0, 10.0], [10.0, 20.0], [20.0, 30.0]]
    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    assert document["state"] == "done"
    assert document["error"] is None
    assert "all_chunks_failed" not in json.dumps(document, ensure_ascii=False)
    status = read_status(session)
    assert status["state"] == "done" and status["error"] is None
    assert read_status(session)["silent_spans"] == [[0.0, 10.0], [10.0, 20.0], [20.0, 30.0]]


def test_the_silence_gate_is_off_for_containers_it_cannot_scan(tmp_path) -> None:
    """非 PCM 的容器不做判断（照常送引擎），PCM 上按字节定位扫那一段。

    mp3 那一档要么先解出波形（分块时由 `_execute_chunks()` 解一次）、要么原样交给引擎：
    门只在「确实读到全零」时拦人，读不明就放行。
    """
    from meeting.offline import runner as runner_module

    session, _ = silence_session(tmp_path, silent_chunks=(1, 2, 3), source="mic", name="mp3")
    pcm = session / "meeting_audio_mic_001.pcm"
    mp3 = pcm.with_suffix(".mp3")
    pcm.unlink()
    mp3.write_bytes(b"\x00" * (int(30.0 * SAMPLE_RATE) * 2))
    engine = FakeEngine()
    engine.script = []

    run_session(session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": 0}))

    # 全零的 mp3 走的是「解出来看看」那一步：解不动就如实抛（这一档不在这里验真解码）；
    # 关键是 `.pcm` 之外的后缀**不会**被门按字节判静音。
    assert runner_module.span_is_silent(mp3, offset=0.0, seconds=30.0) is False
    assert runner_module.span_is_silent(session / "meeting_audio_mic_001.pcm", offset=0.0, seconds=30.0) is False


def test_pcm_span_scan_reads_the_span_it_was_asked_for(tmp_path) -> None:
    """按字节扫那一段的判据：非零样本落在区间**之外**时，这一段仍然是静音。

    门按 `offset` / `seconds` 定位（与切块同一套换算），不是「整个文件里有没有非零」：
    末块那 10 秒是全零、而前面 20 秒有信号时，末块必须被跳过。
    """
    from meeting.offline import runner as runner_module

    path = tmp_path / "meeting_audio_mic_001.pcm"
    payload = bytearray(int(30.0 * SAMPLE_RATE) * 2)
    payload[0:2] = b"\x00\x01"  # 只有第 0 秒有声
    path.write_bytes(bytes(payload))

    assert runner_module.pcm_span_is_silent(path, offset=0.0, seconds=10.0) is False
    assert runner_module.pcm_span_is_silent(path, offset=10.0, seconds=10.0) is True
    assert runner_module.pcm_span_is_silent(path, offset=20.0, seconds=10.0) is True
    # 短于 0.1 秒的那一档不判静音（样本不足，交给引擎）
    assert runner_module.pcm_span_is_silent(path, offset=25.0, seconds=0.05) is False
    # 要的那一段**没读全**（文件比它短）时也交给引擎：少了这一条，
    # 「读不到」与「读到全零」会撞成同一个答案，一段没读到的音频就被当成静音跳过
    assert runner_module.pcm_span_is_silent(path, offset=25.0, seconds=10.0) is False
    assert runner_module.pcm_span_is_silent(path, offset=100.0, seconds=5.0) is False
    # 整个文件都读不到非零 → 静音
    all_zero = tmp_path / "zero.pcm"
    all_zero.write_bytes(b"\x00" * (int(10.0 * SAMPLE_RATE) * 2))
    assert runner_module.pcm_span_is_silent(all_zero, offset=0.0, seconds=10.0) is True
    assert runner_module.pcm_span_is_silent(all_zero, offset=0.0, seconds=12.0) is False


def test_zero_duration_segments_are_kept_and_counted(tmp_path) -> None:
    """零时长的段**留着、只数不改**：条数进 `audio.zero_span_segments` 与报告。

    丢不得：解析层对「模型漏写了 `[结束]`」的那一段故意把 `end` 退回 `start`
    （`engine.py` 的 `_build()`），丢掉它就是丢掉一段真正文。实测的假正文**每一段都是
    零时长**，因此这一格同时是有用的异常信号 —— 两种含义都由人来读，机器只如实报数。
    """
    session = synth_session(tmp_path / "zero-span", sources=("mic",), seconds=3.0)
    engine = FakeEngine(
        script=[
            OfflineSegment(1.0, 1.0, "S01", "漏了结束标签的一句"),
            OfflineSegment(2.0, 2.5, "S02", "正常的一句"),
            OfflineSegment(3.0, 3.0, "S01", "又一句漏了结束标签的"),
        ]
    )

    run_session(session, engine=engine, config=FakeConfig())

    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    texts = [row["text"] for row in document["segments"]]
    # 三段一段都没丢，零时长那两段原样留着（正文一个字没动）
    assert texts == ["漏了结束标签的一句", "正常的一句", "又一句漏了结束标签的"]
    zero_rows = [row for row in document["segments"] if row["end"] <= row["start"]]
    assert len(zero_rows) == 2
    assert document["audio"]["zero_span_segments"] == 2
    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    assert "## 零时长的段" in report
    assert "mic：2 段（共 3 段）" in report
    # 跑完那一趟没有零时长段时记 0（不是缺这一格）
    other = synth_session(tmp_path / "no-zero-span", sources=("mic",), seconds=3.0)
    run_session(other, engine=FakeEngine(), config=FakeConfig())
    clean = artifacts.read_speakers_artifact(other / "meeting_offline_speakers_mic.json")
    assert clean["audio"]["zero_span_segments"] == 0


def load_waveform(monkeypatch, waveform) -> None:
    """把「把音频解成波形」这一步换掉（`runner._decode_samples` 那个接缝）。

    `tests/conftest.py` 不许在 pytest 里碰真模型，也不该让一条用例为了一次真解码等上几秒；
    这里注入的是**已经解好的那段波形**，与 `decode_audio()` 的交回值同形
    （16k 单声道 `[-1, 1]` float32）。
    """
    from meeting.offline import runner as runner_module

    monkeypatch.setattr(
        runner_module, "_decode_samples", lambda path: waveform
    )


def mp3_session(root: Path, *, seconds: float, source: str = "mic") -> Path:
    """合成一个「只有 mp3」的场次目录（归档 PCM 缺席，音频走转码产物那一档）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / f"meeting_audio_{source}_001.mp3").write_bytes(b"ID3" + b"\x00" * 64)
    (root / "session.json").write_text(
        json.dumps(
            {
                "session_id": root.name,
                "dir": str(root),
                "mode": source,
                "state": "done",
                "sources": [{"id": source}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return root


def zeros(seconds: float):
    """一段全零的 16k 单声道 float32 波形（实测那一档的数字静音）。"""
    import numpy as np

    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)


def speech(seconds: float):
    """一段有话音的 16k 单声道 float32 波形（与 `decode_audio()` 的交回值同形）。"""
    import numpy as np

    samples = int(seconds * SAMPLE_RATE)
    t = np.arange(samples, dtype=np.float32) / np.float32(SAMPLE_RATE)
    return (0.2 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)


def test_a_silent_single_pass_mp3_never_reaches_the_engine(tmp_path, monkeypatch) -> None:
    """**C1 的直接回归**：单趟跑的 mp3 也过门，全零的那一段碰都不碰引擎。

    审查给的产品路径复现：一个合成场次目录，`meeting_audio_mic_001.mp3` 是一段 400 秒的全零
    音频，配置取默认（`post.chunk_seconds = 450`）。改之前那一段**不经过门**（单趟那条路
    没有解好的波形），引擎被调了一次、在权威底本里留下整段假正文；现在一次调用都不发生，
    整段记成没声跳过。

    400 秒这个长度是有意的：默认块长 450 秒，**短于 450 秒的录音全部走这一条路**，
    也就是每一场短会议。
    """
    seconds = 400.0
    session = mp3_session(tmp_path / "single-pass-silent", seconds=seconds)
    load_waveform(monkeypatch, zeros(seconds))
    engine = FakeEngine()

    summary = run_session(session, engine=engine, config=FakeConfig())

    assert engine.calls == [], "全零的单趟 mp3 一次引擎调用都不该发生"
    assert summary["state"] == "done"
    assert summary["partial"] is False
    assert summary["silent"] is True
    assert summary["silent_spans"] == [[0.0, 400.0]]
    assert summary["gate"]["applied"] is True
    assert summary["gate"]["spans_judged"] == 1

    transcript = session / "meeting_offline_transcript_mic.md"
    assert artifacts.read_transcript_lines(transcript) == []
    head = transcript.read_text(encoding="utf-8").splitlines()[0]
    assert head.startswith(artifacts.TRANSCRIPT_NOTE_PREFIX)
    assert "0.0–400.0s" in head

    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    assert document["audio"]["silent_spans"] == [[0.0, 400.0]]
    assert document["audio"]["gate"]["applied"] is True
    assert document["audio"]["gate"]["spans_judged"] == 1
    assert document["state"] == "done"
    # 逐块那一行自带结论词与门的结论：读方不必从整场那几格反推这一段为什么没有字
    assert [(run["reason"], run["gate"], run["segments"]) for run in document["chunks"]] == [
        ("digital_silence", "silent", 0)
    ]

    status = read_status(session)
    assert status["state"] == "done" and status["partial"] is False
    assert status["silent_spans"] == [[0.0, 400.0]]
    assert status["gate"]["applied"] is True
    assert status["artifacts"]["mic"]["gate"]["spans_judged"] == 1


def test_a_speech_single_pass_mp3_still_transcribes(tmp_path, monkeypatch) -> None:
    """**上一条的对照**：同一条单趟路上真有话音的音频照常送引擎、照常落正文。

    门只在「一个非零样本都没有」时拦人。这一条把「修好 C1 之后单趟 mp3 还能转写」
    钉住：400 秒的 220 Hz 正弦（峰值 0.2，约 6553 个计数）一路放行，引擎调一次，
    正文落在权威底本里。
    """
    seconds = 400.0
    session = mp3_session(tmp_path / "single-pass-speech", seconds=seconds)
    load_waveform(monkeypatch, speech(seconds))
    engine = FakeEngine()

    summary = run_session(session, engine=engine, config=FakeConfig())

    assert len(engine.calls) == 1, "有话音的单趟 mp3 照常调一次引擎"
    assert summary["state"] == "done"
    assert summary["silent"] is False
    assert summary["silent_spans"] == []
    assert summary["gate"]["applied"] is True

    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    assert [run["reason"] for run in document["chunks"]] == ["transcribed"]
    assert [run["gate"] for run in document["chunks"]] == ["not_silent"]
    assert document["segments"], "正文落在权威底本里"
    lines = artifacts.read_transcript_lines(session / "meeting_offline_transcript_mic.md")
    assert len(lines) == len(document["segments"]) == 2


def test_a_single_pass_mp3_that_cannot_be_decoded_says_so(tmp_path, monkeypatch) -> None:
    """单趟 mp3 解不开时**如实记下门没判成**：那一段照常送引擎，产物里看得出这一档。

    这一条盯的是「三个零段情形必须分得开」的另一半：门没用上的那一趟不许与健康的运行
    长得一样。`applied = false` + `unjudged_spans` + 逐块那一行的 `gate_unjudged`
    三处同时露出来，报告开头也单列一行。
    """
    from meeting.offline import runner as runner_module

    session = mp3_session(tmp_path / "single-pass-broken", seconds=0.0)
    engine = FakeEngine()

    def boom(path):
        raise ValueError("这个容器解不出波形")

    monkeypatch.setattr(runner_module, "_decode_samples", boom)
    summary = run_session(session, engine=engine, config=FakeConfig())

    # 解不开也照送引擎（读不到就按可能有声处理），一次调用照样发生
    assert len(engine.calls) == 1
    assert summary["state"] == "done"
    assert summary["gate"]["applied"] is False
    assert summary["gate"]["spans_unjudged"] == 1

    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    assert document["audio"]["gate"]["applied"] is False
    assert document["audio"]["gate"]["unjudged_spans"] == [[0.0, 0.0]]
    assert "解不出波形" in (document["audio"]["gate"]["unjudged_reason"] or "")
    assert [(run["gate"], run["reason"]) for run in document["chunks"]] == [
        ("unjudged", "gate_unjudged")
    ]
    assert "gate_unjudged：1" in (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")

    status = read_status(session)
    assert status["gate"]["applied"] is False
    assert status["gate"]["unjudged_spans"] == {"mic": [[0.0, 0.0]]}
    assert status["artifacts"]["mic"]["gate"]["spans_judged"] == 0


def test_the_gate_answers_the_same_for_a_span_it_cannot_read_whole(tmp_path) -> None:
    """**m1**：两条分支对「这一段没读全」给同一个答案 —— 交给引擎，都不判静音。

    审查的例子：一段 10 秒的全零音频，请求 `[0, 10s)` 之外的区间。PCM 那一支按字节读到
    文件尾就停（读不全 → 放行），波形那一支原先只拿到切片里的那几个样本就下结论
    （0.25 秒的全零 → 判静音），同一个门两个答案。截断的文件因此可能被当成一段安静会议
    跳过 —— 现在两支都交回「判不了」（`silent = None`），由引擎说了算。

    另有一条：**要的那一段整个落在文件之外**（`start + count` 越过波形末尾）时同样放行，
    不然「那一段不在文件里」会被读成「那一段没声」。
    """
    from meeting.offline import runner as runner_module

    pcm = tmp_path / "meeting_audio_mic_001.pcm"
    pcm.write_bytes(b"\x00" * (int(10.0 * SAMPLE_RATE) * 2))
    waveform = zeros(10.0)

    # 整整这一段都在文件里、且全零 → 静音（两支同一个答案）
    assert runner_module.measure_span(pcm, offset=0.0, seconds=10.0).silent is True
    assert runner_module.measure_span(
        pcm, offset=0.0, seconds=10.0, waveform=waveform, waveform_length=waveform.size
    ).silent is True

    # 要的那一段读不全 → 两支都交回「判不了」，交回值只有 True/False 的那一层都放行
    for offset, seconds in ((0.0, 12.0), (5.0, 10.0), (100.0, 5.0)):
        pcm_verdict = runner_module.measure_span(pcm, offset=offset, seconds=seconds)
        wave_verdict = runner_module.measure_span(
            pcm,
            offset=offset,
            seconds=seconds,
            waveform=waveform,
            waveform_length=waveform.size,
        )
        assert pcm_verdict.silent is None, (offset, seconds, pcm_verdict)
        assert wave_verdict.silent is None, (offset, seconds, wave_verdict)
        assert runner_module.span_is_silent(pcm, offset=offset, seconds=seconds) is False
        assert (
            runner_module.span_is_silent(
                "meeting_audio_mic_001.mp3",
                offset=offset,
                seconds=seconds,
                waveform=waveform,
            )
            is False
        )
    # 短于 0.1 秒的那一档照旧不判静音（两条支路都放行）
    assert runner_module.measure_span(pcm, offset=0.0, seconds=0.05).silent is False


def test_the_float_side_of_the_gate_uses_the_documented_rule(tmp_path) -> None:
    """**m2**：float32 与 int16 用同一个分界 —— 峰值满量程半格以上就算有声。

    旧行为：判据先按 `× 32767` **截断**成整数，于是 float 的 `3.0e-5` 与 `3.05e-5`
    （半格上下）一起变成 0、被判静音，只有 `3.06e-5` 才过；而 int16 那一侧的规则是
    「任何一个非零样本就放行」。同一条「峰值恰好为 0」在两种形态上因此分界不一致。

    改法是把判据那把尺子换成**不截断**的 `samples_to_count_floats()`：两侧的分界都落在
    「满量程半格」（float 的 `0.5 / 32767 ≈ 1.526e-5`），`3.06e-5` 与 int16 的 ±1 都算有声。
    """
    import numpy as np

    from meeting.offline.runner import is_digital_silence, samples_to_counts

    length = int(1.0 * SAMPLE_RATE)
    for peak in (3.06e-5, 1.0 / 32767.0, -3.06e-5):
        waveform = np.zeros(length, dtype=np.float32)
        waveform[7] = np.float32(peak)
        assert is_digital_silence(waveform) is False, peak
    # 满量程半格**以下**（0.4 格）与全零仍算静音
    for peak in (0.0, 0.4 / 32767.0):
        waveform = np.zeros(length, dtype=np.float32)
        waveform[7] = np.float32(peak)
        assert is_digital_silence(waveform) is True, peak
    # int16 那一侧的口径一个字没变：±1 放行、全零跳过
    counts = np.zeros(length, dtype=np.int16)
    counts[3] = 1
    assert is_digital_silence(counts) is False
    counts[3] = -1
    assert is_digital_silence(counts) is False
    assert is_digital_silence(np.zeros(length, dtype=np.int16)) is True
    # 同一个「一个样本」在两种形态上给同一个答案（这就是 m2 要的那条一致性）
    for count in (-1, 1):
        single = np.zeros(length, dtype=np.int16)
        single[9] = count
        assert is_digital_silence(single) is False
        assert is_digital_silence(single.astype(np.float32) / np.float32(32767.0)) is False
    # 切块与写 wav 那条路仍用**整数**换算（与 `write_chunk_wav()` 同形），判据这一层不碰它
    assert int(np.max(np.abs(samples_to_counts(np.float32(0.2) * np.ones(4, np.float32))))) == 6553


def test_the_gate_boundary_is_the_documented_half_step_rule(tmp_path) -> None:
    """**C1 / m2 的分界**：文档那条规则与实现逐字对得上，分界按常量现场读出。

    文档写的是「**峰值低于半个 int16 步长算静音**」（`SILENT_PEAK_FLOOR = 0.5` 格），
    因此 `peak < SILENT_PEAK_FLOOR` 这一行是**开区间**：正好落在半格上的样本算有声。

    这一条按**常量**取值、不写死浮点：把这个常量改成别的值时，「低于它」的那一档跟着变，
    而不截断的换算把浮点这一个样本的格数**精确地**还原成 `floor`，于是三档（差一个 ulp /
    正好落在分界上 / 差一个 ulp）在同一个分界上被钉住。写死 `3.06e-5` 那种断言做不到这件事
    —— 常量一漂，用例照绿。
    """
    import numpy as np

    from meeting.offline import runner as runner_module

    floor = float(runner_module.SILENT_PEAK_FLOOR)
    assert floor == 0.5, "文档那条规则说的是半格；常量动了，这一条与文档都要跟着改"
    length = int(1.0 * SAMPLE_RATE)
    at_floor = np.float32(floor) / np.float32(32767.0)  # 正好半格那一个样本
    just_above = np.nextafter(at_floor, np.float32(np.inf))  # 半格往上一个 ulp
    just_below = np.nextafter(at_floor, np.float32(0.0))  # 半格往下一个 ulp

    def gate(peak: float) -> bool:
        waveform = np.zeros(length, dtype=np.float32)
        waveform[7] = np.float32(peak)
        return runner_module.is_digital_silence(waveform)

    # 三个读数的身份先钉住（不然下面那三行可能验的是同一个数）
    assert just_below < at_floor < just_above
    # 换算把半格那一个样本精确还原成 `floor`（不截断那把尺子量出来的就是它）
    scaled_at_floor = runner_module.samples_to_count_floats(np.array([at_floor], np.float32))
    assert runner_module._peak_counts(scaled_at_floor) == floor
    # 差一个 ulp（低于分界）→ 静音；正好落在分界上 → 有声；差一个 ulp（高于分界）→ 有声
    assert gate(just_below) is True, f"{just_below!r} 低于半格，该算静音"
    assert gate(at_floor) is False, f"{at_floor!r} 正好半格，规则说的是「低于」才算静音"
    assert gate(just_above) is False, f"{just_above!r} 高于半格，该算有声"
    # 全零仍是静音的基准那一档
    assert runner_module.is_digital_silence(np.zeros(length, dtype=np.float32)) is True
    # int16 那一侧没有「比 1 格更小的非零值」，±1 都算有声（开闭两读在整数上同解）
    for count in (-1, 1):
        counts = np.zeros(length, dtype=np.int16)
        counts[3] = count
        assert runner_module.is_digital_silence(counts) is False
    # 同一个「一个样本」在两种形态上给同一个答案（浮点 1 格 = int16 的 1 格）
    one_count_float = np.zeros(length, dtype=np.float32)
    one_count_float[9] = np.float32(1.0) / np.float32(32767.0)
    assert runner_module.is_digital_silence(one_count_float) is False
    # 归档 PCM 那一支对同一段音频给同一个答案（按字节扫：见到非零字节就放行）
    pcm = tmp_path / "meeting_audio_mic_001.pcm"
    pcm.write_bytes(b"\x00" * (length * 2))
    assert runner_module.measure_span(pcm, offset=0.0, seconds=1.0).silent is True
    loud = np.zeros(length, dtype=np.int16)
    loud[3] = 1
    pcm.write_bytes(loud.astype("<i2").tobytes())
    assert runner_module.measure_span(pcm, offset=0.0, seconds=1.0).silent is False


def test_the_ruler_the_single_pass_product_path_uses(tmp_path, monkeypatch) -> None:
    """**C2**：单趟产品路径上的尺子名如实记下来（`waveform` / `decode_at_plan`）。

    审查复现的那一条：单趟 mp3 在产物里记的是 `waveform` —— `_plan_for_source()` 为了量
    长度已经把整份音频解成了波形，门接着在那份波形上切片判，因此门口不会现解第二遍，
    也不会记成 `decode`。`decode` 那一档只在「量长度那一步就已经解过、且没解开」时出现，
    换名 `decode_at_plan`，结论必然是「判不了」。

    四条都走产品路径：只有 mp3（尺子 `waveform`）/ 归档 PCM 是 0 字节而音频改走 mp3（尺子
    仍是 `waveform`）/ 音频走归档 PCM（尺子 `pcm_bytes`）/ 量长度那一步解不开（尺子
    `decode_at_plan`）。
    """
    import numpy as np

    seconds = 3.0
    # 1. 归档 PCM 缺席、mp3 解得开 → 尺子 `waveform`
    only_mp3 = mp3_session(tmp_path / "only-mp3", seconds=seconds)
    load_waveform(monkeypatch, speech(seconds))
    run_session(only_mp3, engine=FakeEngine(), config=FakeConfig())
    document = artifacts.read_speakers_artifact(only_mp3 / "meeting_offline_speakers_mic.json")
    assert [run["gate_method"] for run in document["chunks"]] == ["waveform"]
    assert document["audio"]["gate"]["method"] == "waveform"

    # 2. 归档 PCM 是 **0 字节**、音频改走 mp3 → 尺子按喂给引擎的那份音频挑，还是 `waveform`
    with_pcm = synth_session(
        tmp_path / "empty-pcm-with-mp3", sources=("mic",), seconds=seconds, empty=("mic",)
    )
    (with_pcm / "meeting_audio_mic_001.mp3").write_bytes(b"ID3" + b"\x00" * 64)
    run_session(with_pcm, engine=FakeEngine(), config=FakeConfig())
    document = artifacts.read_speakers_artifact(with_pcm / "meeting_offline_speakers_mic.json")
    assert document["audio"]["file"] == "meeting_audio_mic_001.mp3"
    assert [run["gate_method"] for run in document["chunks"]] == ["waveform"]

    # 3. 一份**非空**归档 PCM 而没有 mp3 → 音频走 PCM，尺子是按字节扫那一把
    pcm_only = synth_session(tmp_path / "pcm-only", sources=("mic",), seconds=seconds)
    run_session(pcm_only, engine=FakeEngine(), config=FakeConfig())
    document = artifacts.read_speakers_artifact(pcm_only / "meeting_offline_speakers_mic.json")
    assert document["audio"]["file"] == "meeting_audio_mic_001.pcm"
    assert [run["gate_method"] for run in document["chunks"]] == ["pcm_bytes"]
    # 这一份 PCM 是 `b"\x00\x01"` 重复出来的：每个样本都是 1 格，**有声**，照常送引擎
    assert [run["reason"] for run in document["chunks"]] == ["transcribed"]

    # 4. 量长度那一步解不开 → `decode_at_plan`、结论「判不了」、那一段照常送引擎
    from meeting.offline import runner as runner_module

    broken = mp3_session(tmp_path / "broken-decode", seconds=0.0)
    engine = FakeEngine()

    def boom(path):
        raise ValueError("这个容器解不出波形")

    monkeypatch.setattr(runner_module, "_decode_samples", boom)
    run_session(broken, engine=engine, config=FakeConfig())
    document = artifacts.read_speakers_artifact(broken / "meeting_offline_speakers_mic.json")
    assert len(engine.calls) == 1, "判不了那一段照常送引擎"
    assert [(run["gate"], run["gate_method"], run["reason"]) for run in document["chunks"]] == [
        ("unjudged", "decode_at_plan", "gate_unjudged")
    ]
    # 逐路那一格记的是「这条路凭什么判的」，判不了时为空（判没判成看 `applied`）
    assert document["audio"]["gate"]["method"] is None
    assert document["audio"]["gate"]["applied"] is False
    # 词表里不再有 `decode` 这个词：单趟 mp3 要么走 `waveform`、要么走 `decode_at_plan`
    assert runner_module.GATE_METHOD_DECODE_AT_PLAN == "decode_at_plan"
    assert "decode" not in {
        runner_module.GATE_METHOD_PCM,
        runner_module.GATE_METHOD_WAVEFORM,
        runner_module.GATE_METHOD_DECODE_AT_PLAN,
    }


def test_a_span_that_ran_but_returned_only_zero_length_segments_is_marked(tmp_path) -> None:
    """跑成而**每一段都是零时长**：逐块那一行给 `zero_span_segments`，正文一段不丢。

    这是三个「零段」情形里最像正常的那一个：引擎有交回东西、`error` 是空的，但形状与实测
    那批假正文一模一样（每一段零时长）。整路那一格 (`audio.zero_span_segments`) 原先只有
    总数，看不出**是哪一块**；现在逐块那一行也带一份。
    """
    session = synth_session(tmp_path / "zero-span-chunk", sources=("mic",), seconds=3.0)
    engine = FakeEngine(
        script=[
            OfflineSegment(1.0, 1.0, "S01", "漏了结束标签的一句"),
            OfflineSegment(2.0, 2.0, "S02", "又一句漏了结束标签的"),
        ]
    )

    run_session(session, engine=engine, config=FakeConfig())

    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    assert [row["text"] for row in document["segments"]] == [
        "漏了结束标签的一句", "又一句漏了结束标签的",
    ]
    assert document["audio"]["zero_span_segments"] == 2
    assert [(run["reason"], run["zero_span_segments"]) for run in document["chunks"]] == [
        ("zero_span_segments", 2)
    ]
    assert document["chunks"][0]["error"] is None
    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    assert "zero_span_segments（跑成了，交回的段每一段都是零时长）" in report


def test_the_whole_session_gate_shows_a_span_it_could_not_judge(tmp_path, monkeypatch) -> None:
    """**整场那一格**：门判不了那一段时，状态文件顶层把「哪一路的哪一段」列出来。

    这一条盯的是**从门的结论到产物**这条线：第三块 `[20, 30)` 那一段的静音与否判不了
    （音频比它短），于是整场 `gate.applied` 落 `false`、`unjudged_spans` 里看得出是 `mic`
    的哪一段。少了这几格，一段没人判过静音的音频与一次健康的运行在状态文件里长得一样。
    那一段照常送引擎、三段正文照旧落在产物里 —— 「门没用上」这一档什么都不缺，
    缺的只是「有人判过」这件事。
    """
    from meeting.offline import runner as runner_module

    session, ranges = silence_session(tmp_path, seconds=30.0, name="truncated")
    engine = SlicingEngine(ranges)
    real_measure = runner_module.measure_span

    def unreadable_last_span(path, *, offset, seconds, **kwargs):
        """第三块 `[20, 30)` 上把门打成「判不了」（真去读时文件已经比它短）。

        判据那一层（`measure_span()` 读不全 → `silent = None`）由
        `test_the_gate_answers_the_same_for_a_span_it_cannot_read_whole` 直接验；
        这一条盯的是**从门的结论到产物**这条线：整场 `gate`、逐路 `audio.gate`、
        逐块那一行与报告那一节四处都跟着它走。
        """
        if offset >= 20.0:
            return runner_module.MeasureResult(
                method=runner_module.GATE_METHOD_PCM,
                silent=None,
                detail="归档 PCM 上这一段没读全（文件比这个区间短）",
            )
        return real_measure(path, offset=offset, seconds=seconds, **kwargs)

    monkeypatch.setattr(runner_module, "measure_span", unreadable_last_span)

    summary = run_session(
        session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS})
    )

    assert summary["state"] == "done"
    assert summary["gate"]["applied"] is False
    assert summary["gate"]["spans_unjudged"] == 1
    assert summary["gate"]["unjudged_spans"] == {"mic": [[20.0, 30.0]]}

    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    assert document["audio"]["gate"]["applied"] is False
    assert document["audio"]["gate"]["unjudged_spans"] == [[20.0, 30.0]]
    assert "读全" in (document["audio"]["gate"]["unjudged_reason"] or "")
    # 「一块都没判成」那一档那句话照旧（本轮只改「判过」那一档，见
    # `test_the_gate_detail_says_what_actually_happened_to_the_blocks`）
    assert document["audio"]["gate"]["detail"] == "1 块的静音与否判不了，照常送了引擎"
    assert document["audio"]["gate"]["method"] == "pcm_bytes"
    assert [(run["gate"], run["reason"], run["segments"]) for run in document["chunks"]] == [
        ("not_silent", "transcribed", 2),
        ("not_silent", "transcribed", 2),
        ("unjudged", "gate_unjudged", 3),
    ]
    # 读方从这三处分得开：这一段没人判过静音（`gate`）→ 引擎照常跑、交回三段字
    # （`reason` 说「门没判过」，正文照旧在）→ 那一块音频本来就有内容（`silent_spans` 空）。
    # 「门判不了」这一档**不出现在缺失区间里**：它什么都不缺，缺的只是「有人判过」这件事。
    assert document["state"] == "done"
    assert document["audio"]["partial"] is False
    assert document["audio"]["missing_spans"] == []
    assert document["audio"]["silent_spans"] == []
    assert document["chunks"][2]["gate_detail"]

    status = read_status(session)
    assert status["gate"]["applied"] is False
    assert status["gate"]["unjudged_spans"] == {"mic": [[20.0, 30.0]]}
    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    assert "这一场的门在 mic 1 段上没做成判断" in report
    assert "## 数字静音门（这一段音频的静音与否，判过没有）" in report
    assert "⚠ 没做成判断" in report
    assert "判不了的那 1 段（20.0–30.0s）" in report


def test_the_gate_detail_says_what_actually_happened_to_the_blocks(tmp_path) -> None:
    """逐路 `gate.detail` 那句人话**只说这一趟真发生的事**：判过几块、跳过几块。

    改前那句话是无条件写死的「N 块判过（峰值全零的跳过引擎）」：一次整段都有声的运行
    （实测那一趟块峰值 18 316 计数、一块都没跳过）照样带着它，读产物的人会以为有块因为
    没声被丢了。三档各钉一个字符串：一块没跳过、真跳过一块、一块都判不了（第三档那条
    留在这里不动，`test_the_whole_session_gate_shows_a_span_it_could_not_judge` 也盯着它）。
    """
    # 1. 三段都有声：跳过的块数是 0，那句话里不许出现「有块被跳过」这种说法
    session, ranges = silence_session(tmp_path, name="nothing-skipped")
    engine = SlicingEngine(ranges)

    run_session(
        session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS})
    )

    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    gate = document["audio"]["gate"]
    assert (gate["spans_total"], gate["spans_judged"], gate["spans_unjudged"]) == (3, 3, 0)
    assert gate["detail"] == "3 块判过，没有一块是数字静音（一块都没跳过引擎）"
    assert document["audio"]["silent_spans"] == []
    assert len(engine.calls) == 3, "一块都没跳过：三块都送进了引擎"
    # 同一句话落在三处：对照表 `audio.gate`、状态文件 `artifacts.<source>.gate`、报告那一行
    assert read_status(session)["artifacts"]["mic"]["gate"]["detail"] == gate["detail"]
    assert gate["detail"] in (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")

    # 2. 真跳过一块：判过几块、其中几块跳过，两个数都要写出来
    hushed, hushed_ranges = silence_session(
        tmp_path, name="one-skipped", silent_chunks=(2,)
    )
    hushed_engine = SlicingEngine(hushed_ranges)

    run_session(
        hushed,
        engine=hushed_engine,
        config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS}),
    )

    hushed_gate = artifacts.read_speakers_artifact(
        hushed / "meeting_offline_speakers_mic.json"
    )["audio"]["gate"]
    assert (hushed_gate["spans_total"], hushed_gate["spans_judged"]) == (3, 3)
    assert hushed_gate["detail"] == "3 块判过，其中 1 块是数字静音（跳过了引擎）"
    assert len(hushed_engine.calls) == 2, "那句话里的「1 块」与引擎调用次数对得上"


def test_the_report_headline_says_the_same_thing_as_the_gate_detail(tmp_path) -> None:
    """报告开头那一行与逐路 `gate.detail` 说同一件事：跳过几段就写几段，没跳过的不许说跳过。

    改前那一行无条件写着「（峰值全零的跳过引擎）」：一次整段都有声的运行（一块都没跳过）
    照样带着它 —— 这一行是报告的第一句，读的人会以为有段因为没声被丢了，而逐块记账里
    一块都没跳过。两档各钉一个**整行**字符串，并把块数与真实的引擎调用次数对上。
    """
    # 1. 三段都有声：那一行里不许出现任何「有段被跳过」的说法
    session, ranges = silence_session(tmp_path, name="headline-nothing-skipped")
    engine = SlicingEngine(ranges)

    run_session(session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS}))

    assert len(engine.calls) == 3, "三段都有声：三块都送进了引擎"
    assert gate_headline(session) == (
        "- 数字静音门：在 3 段音频上都给出了结论，用的尺子是 pcm_bytes"
        "（没有一段是数字静音，一段都没跳过引擎）"
    )
    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    assert "峰值全零" not in report
    assert "没声而跳过" not in report
    nothing_skipped = artifacts.read_speakers_artifact(
        session / "meeting_offline_speakers_mic.json"
    )["audio"]["gate"]
    assert nothing_skipped["detail"] == "3 块判过，没有一块是数字静音（一块都没跳过引擎）"

    # 2. 真跳过一块：那一行写的是这一趟真发生的那个数
    hushed, hushed_ranges = silence_session(
        tmp_path, name="headline-one-skipped", silent_chunks=(2,)
    )
    hushed_engine = SlicingEngine(hushed_ranges)

    run_session(
        hushed, engine=hushed_engine, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS})
    )

    assert len(hushed_engine.calls) == 2, "跳过的那一块一次都没进引擎"
    assert gate_headline(hushed) == (
        "- 数字静音门：在 3 段音频上都给出了结论，用的尺子是 pcm_bytes"
        "（其中 1 段是数字静音，跳过了引擎）"
    )
    hushed_gate = artifacts.read_speakers_artifact(
        hushed / "meeting_offline_speakers_mic.json"
    )["audio"]["gate"]
    assert hushed_gate["detail"] == "3 块判过，其中 1 块是数字静音（跳过了引擎）"
    # 报告下面那一行也说同一件事（同一份报告里两处不许互相打架）
    assert "- 这一场有 **没声而跳过** 的音频：mic 1 段" in (
        hushed / artifacts.REPORT_NAME
    ).read_text(encoding="utf-8")


def test_the_gate_says_both_the_unjudged_span_and_the_skipped_block(tmp_path, monkeypatch) -> None:
    """判不了的那一块与没声跳过的那一块**同时**发生时，两件事都要说出来。

    改前 `gate.detail` 只写判不了那一句：这一趟真跳过了一块（引擎少调一次），读者从那一句
    里看不出跳过发生过 —— 「这一趟到底发生了什么」因此缺了一半。句式沿用那两句现成的话，
    判不了那一句本身**一个字都没动**。
    """
    from meeting.offline import runner as runner_module

    session, ranges = silence_session(
        tmp_path, seconds=30.0, silent_chunks=(2,), name="unjudged-and-silent"
    )
    engine = SlicingEngine(ranges)
    real_measure = runner_module.measure_span

    def unreadable_last_span(path, *, offset, seconds, **kwargs):
        """第三块 `[20, 30)` 上把门打成「判不了」（与上一条用例同一手法）。"""
        if offset >= 20.0:
            return runner_module.MeasureResult(
                method=runner_module.GATE_METHOD_PCM,
                silent=None,
                detail="归档 PCM 上这一段没读全（文件比这个区间短）",
            )
        return real_measure(path, offset=offset, seconds=seconds, **kwargs)

    monkeypatch.setattr(runner_module, "measure_span", unreadable_last_span)

    run_session(session, engine=engine, config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS}))

    document = artifacts.read_speakers_artifact(session / "meeting_offline_speakers_mic.json")
    gate = document["audio"]["gate"]
    assert (gate["spans_total"], gate["spans_judged"], gate["spans_unjudged"]) == (3, 2, 1)
    assert gate["applied"] is False
    assert gate["detail"] == (
        "1 块的静音与否判不了，照常送了引擎；2 块判过，其中 1 块是数字静音（跳过了引擎）"
    )
    # 两个数各有出处：跳过的区间在 `silent_spans`、判不了的区间在 `gate.unjudged_spans`，两批不相交
    assert document["audio"]["silent_spans"] == [[10.0, 20.0]]
    assert gate["unjudged_spans"] == [[20.0, 30.0]]
    assert document["audio"]["missing_spans"] == []
    assert document["audio"]["partial"] is False
    assert [(run["gate"], run["reason"]) for run in document["chunks"]] == [
        ("not_silent", "transcribed"),
        ("silent", "digital_silence"),
        ("unjudged", "gate_unjudged"),
    ]
    # 引擎调用两次：没声那一块一次都没跑，判不了的那一块照常跑
    assert [Path(call).name for call in engine.calls] == ["chunk_01.wav", "chunk_03.wav"]
    # 状态文件里是同一句话（两处同源）
    assert read_status(session)["artifacts"]["mic"]["gate"]["detail"] == gate["detail"]
    report = (session / artifacts.REPORT_NAME).read_text(encoding="utf-8")
    assert "mic：跳过 1 段（10.0–20.0s）" in report
    assert "判不了的那 1 段（20.0–30.0s）" in report


def test_every_artifact_field_the_gate_adds_is_on_both_routes(tmp_path) -> None:
    """门加的字段在**单趟与分块两条路**上都出现（形状一致，读方不必分档读）。

    单趟那一档的常量形状钉在这里：`silent` / `silent_spans` / `missing_spans` / `partial`
    / `zero_span_segments` / `gate` 六格两路都有；没声那一档在单趟里是**整段**一个区间
    （一次引擎调用对应一块）。
    """
    expected = {
        "silent",
        "silent_spans",
        "missing_spans",
        "partial",
        "zero_span_segments",
        "gate",
    }
    # 单趟（3 秒、块长 450）
    single = synth_session(tmp_path / "one-pass", sources=("mic",), seconds=3.0)
    run_session(single, engine=FakeEngine(), config=FakeConfig())
    one = artifacts.read_speakers_artifact(single / "meeting_offline_speakers_mic.json")
    assert expected <= set(one["audio"])
    assert one["audio"]["silent"] is False and one["audio"]["silent_spans"] == []
    assert one["audio"]["chunks"] == 1
    assert one["audio"]["gate"]["applied"] is True

    # 分块（30 秒、块长 10）里第 2 块没声
    chunked, ranges = silence_session(tmp_path, silent_chunks=(2,), name="both-routes")
    run_session(
        chunked, engine=SlicingEngine(ranges), config=FakeConfig(**{"post.chunk_seconds": CHUNK_SECONDS})
    )
    many = artifacts.read_speakers_artifact(chunked / "meeting_offline_speakers_mic.json")
    assert expected <= set(many["audio"])
    assert many["audio"]["silent"] is True
    assert many["audio"]["silent_spans"] == [[10.0, 20.0]]
    assert many["audio"]["chunks"] == 3
    assert many["audio"]["gate"]["applied"] is True
    # 逐块那一行的六格在两条路上同形（读方不必按路分档读）
    for run in (one["chunks"] + many["chunks"]):
        assert {
            "reason", "gate", "gate_detail", "zero_span_segments",
        } <= set(run), run
    assert [run["reason"] for run in many["chunks"]] == [
        "transcribed", "digital_silence", "transcribed",
    ]
    # 状态文件逐路那一项也带着这几格（两路都写）
    status = read_status(chunked)
    assert status["artifacts"]["mic"]["silent"] is True
    assert status["artifacts"]["mic"]["silent_spans"] == [[10.0, 20.0]]
    assert status["artifacts"]["mic"]["gate"]["applied"] is True
    assert status["gate"]["applied"] is True


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
    assert runner_module.pcm_span_is_silent(paths, offset=1.2, seconds=1.0) is True
    assert runner_module.pcm_span_is_silent(paths, offset=0.2, seconds=0.5) is False
    assert runner_module.pcm_span_is_silent(paths, offset=2.5, seconds=1.0) is False
    assert runner_module.pcm_span_is_silent(paths, offset=4.5, seconds=0.4) is False
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


def test_a_corrupted_wall_clock_changes_no_conclusion(tmp_path) -> None:
    """故意把墙钟那一栏改错：程序的行为与结论**一个字都不变**（元信息不当判据）。

    两份完全相同的场次目录，一份的 `session.json` 墙钟字段与段戳被改成胡话；
    同一引擎各跑一遍，逐字节比较三份产物与状态文件——除此之外任何代码都不许拿
    墙钟对账（`docs/design-intent.md` 第 6 条；spec 完成判定第 4 条）。
    """
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
