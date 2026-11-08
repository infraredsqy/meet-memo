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
    gate_headline,
    read_status,
)
from tests.test_offline_runner.test_chunking import (
    SlicingEngine,
    read_chunk_wav,
)

#: 慢档标记（68927c27 收尾轮）：会后离线链的契约验收，归收尾跑。
pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# 六、数字静音门：没声的块不调引擎，安静**但有声**的块照调
#
# 实测那一档（`250906` 环回）是**样本全为零**的五块（第 8/16/17/18/19 块，
# `volumedetect` 读 `-91.0 dB`），而引擎在它们身上编出了占那一路输出 69.32% 的假正文。
# 这一节的用例分两半：一半盯「全零跳过」，另一半盯那条**安全性质**——
# 只要有任何一个非零样本，这一块就必须照常送进引擎（丢掉真内容比这个 bug 更坏）。
# ---------------------------------------------------------------------------


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
    整份转写一个字不剩。换算必须先过 `samples_to_count_floats()`（满量程格数、不截断）。

    三档都验：有信号的 float32 波形不判静音；全零的 float32 波形判静音；
    int16 计数与等价的 float32 得同一个答案。
    """
    import numpy as np

    from meeting.offline.runner import is_digital_silence, samples_to_count_floats

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
    # int16 计数过判据那把尺子原样升位（不截断、不缩放）
    assert int(np.max(np.abs(samples_to_count_floats(counts)))) == 12345
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
    # 关键是 `.pcm` 之外的后缀**不会**被门按字节判静音（「判不了」与「没读到的 PCM」都放行）。
    assert runner_module.measure_span(mp3, offset=0.0, seconds=30.0).silent is None
    assert runner_module.measure_span(
        session / "meeting_audio_mic_001.pcm", offset=0.0, seconds=30.0
    ).silent is None


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

    assert runner_module.measure_span(path, offset=0.0, seconds=10.0).silent is False
    assert runner_module.measure_span(path, offset=10.0, seconds=10.0).silent is True
    assert runner_module.measure_span(path, offset=20.0, seconds=10.0).silent is True
    # 短于 0.1 秒的那一档不判静音（样本不足，交给引擎）
    assert runner_module.measure_span(path, offset=25.0, seconds=0.05).silent is False
    # 要的那一段**没读全**（文件比它短）时也交给引擎：少了这一条，
    # 「读不到」与「读到全零」会撞成同一个答案，一段没读到的音频就被当成静音跳过
    assert runner_module.measure_span(path, offset=25.0, seconds=10.0).silent is None
    assert runner_module.measure_span(path, offset=100.0, seconds=5.0).silent is None
    # 整个文件都读不到非零 → 静音
    all_zero = tmp_path / "zero.pcm"
    all_zero.write_bytes(b"\x00" * (int(10.0 * SAMPLE_RATE) * 2))
    assert runner_module.measure_span(all_zero, offset=0.0, seconds=10.0).silent is True
    assert runner_module.measure_span(all_zero, offset=0.0, seconds=12.0).silent is None


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

    from meeting.offline.runner import is_digital_silence

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
    # 切块与写 wav 那条路仍用**整数**换算（`write_chunk_wav()` 的满量程换算），判据这一层不碰它
    scale_probe = write_chunk_wav(tmp_path / "scale.wav", np.float32(0.2) * np.ones(4, np.float32))
    assert int(np.max(np.abs(np.frombuffer(read_chunk_wav(scale_probe), dtype="<i2")))) == 6553


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
    from meeting.offline.runner.pcm import _peak_counts

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
    assert _peak_counts(scaled_at_floor) == floor
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
    照样带着它 —— 这一行是报告的第一句，读的人会以为有段因为没声被丢了，而逐块登记里
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


