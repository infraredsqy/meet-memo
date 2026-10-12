"""`meeting/offline/artifacts.py` 的契约用例：四份产物的写入与字段契约。

要盯住的是**字段契约的拒绝分支**（`schema_version` 缺失 / 主版本不认识 / `source` 缺失），
它们得是真用例在跑，有真实的读方调用，读方当场抛错。

三处精度分工也各自钉一条：

  - 转写的行格式 `[HH:MM] **S01**: 正文`，时间戳恰好 5 个字符（**分钟级**）；
  - JSON 里 `start` / `end` 是**秒级浮点**（读得回小数）；
  - 汇总的段数与秒数加总与 `segments` 对得上。

纯标准库，不加载任何模型。
"""

from __future__ import annotations

import json as _json
import threading

import pytest

from meeting import atomicio
from meeting.offline import artifacts


def _document(**overrides) -> dict:
    """一份合契约的对照表（用例按需改其中一格）。"""
    base = {
        "session_id": "250919",
        "source": "loopback",
        "engine": {
            "model": "OpenMOSS-Team/MOSS-Transcribe-Diarize",
            "dtype": "bfloat16",
            "device": "auto",
            "selector": "auto",
        },
        "audio": {"file": "meeting_audio_loopback_001.pcm", "seconds": 300.0},
        "segments": [
            {"start": 0.48, "end": 3.2, "local": "S01", "text": "第一句"},
            {"start": 3.2, "end": 5.0, "local": "S02", "text": "第二句"},
            {"start": 65.5, "end": 70.0, "local": "S01", "text": "第三句"},
        ],
        "speakers": [
            {"local": "S01", "label_raw": "S01", "segments": 2, "seconds": 7.22},
            {"local": "S02", "label_raw": "S07", "segments": 1, "seconds": 1.8},
        ],
    }
    base.update(overrides)
    return artifacts.build_speakers_document(**base)


# ---------------------------------------------------------------------------
# 一、对照表：形状与必填字段
# ---------------------------------------------------------------------------


def test_document_carries_the_contract_fields() -> None:
    """顶层必填键与逐项必填键都在（写入方按契约填，读方按契约读）。"""
    document = _document()

    assert document["schema_version"] == artifacts.SCHEMA_VERSION
    assert document["source"] == "loopback"
    assert document["session_id"] == "250919"
    assert document["engine"]["model"]
    assert document["audio"]["file"] == "meeting_audio_loopback_001.pcm"
    for item in document["segments"]:
        assert set(item) == {"start", "end", "speaker", "local", "text"}
    for row in document["speakers"]:
        assert set(row) == {"id", "local", "label_raw", "segments", "seconds"}
    # 单趟跑时 `chunks` 是空数组：没有分块这件事本身也看得出来
    assert document["chunks"] == []


def test_chunk_rows_are_recorded_per_block() -> None:
    """分块跑时逐块记账落进 `chunks`，失败的那块带着原因（缺段因此查得到出处）。"""
    document = _document(
        chunks=[
            {"index": 1, "offset_seconds": 0.0, "seconds": 450.0, "segments": 12, "error": None},
            {
                "index": 2,
                "offset_seconds": 450.0,
                "seconds": 450.0,
                "segments": 0,
                "error": "engine_error：RuntimeError: 显存不够",
            },
        ]
    )

    assert [row["index"] for row in document["chunks"]] == [1, 2]
    assert document["chunks"][0]["error"] is None
    assert document["chunks"][1]["error"].startswith("engine_error")
    assert document["chunks"][1]["offset_seconds"] == 450.0


def test_headcount_says_what_the_class_count_means() -> None:
    """M3：`speakers` 数组长度怎么读写在 `headcount` 上，两种跑法各一套说法。

    整段一次跑：这个数是模型分出的类数；分块跑：它是**各块类数之和**，
    同一个人在两块里各拿一个编号，读它当人数会错。
    """
    single = _document()
    assert single["headcount"] == {
        "meaningful": True,
        "class_count": 2,
        "chunk_count": 1,
        "basis": "single_pass_classes",
    }

    chunked = _document(
        chunks=[
            {"index": 1, "offset_seconds": 0.0, "seconds": 450.0, "segments": 2, "error": None},
            {"index": 2, "offset_seconds": 450.0, "seconds": 450.0, "segments": 1, "error": None},
        ]
    )
    assert chunked["headcount"] == {
        "meaningful": False,
        "class_count": 2,
        "chunk_count": 2,
        "basis": "sum_of_chunk_classes",
    }
    # 那一格一直跟得上 `speakers` 的长度（两处从同一个 `summary` 现算，不会各说各话）
    assert chunked["headcount"]["class_count"] == len(chunked["speakers"])


def test_state_and_error_land_in_the_table() -> None:
    """M8：这一路的终态与失败原因写在对照表上，读方不必回头读状态文件或报告。"""
    ok = _document()
    assert ok["state"] == "done"
    assert ok["error"] is None

    failed = _document(state="failed", error="all_chunks_failed：2/2 块没跑成")
    assert failed["state"] == "failed"
    assert failed["error"].startswith("all_chunks_failed")


def test_the_table_still_reads_without_the_new_fields() -> None:
    """新加的两格是**可选的**：上一版写出来的表（没有 `state` / `headcount`）照样读得回来。"""
    legacy = _document()
    for key in ("state", "error", "headcount"):
        legacy.pop(key)

    assert artifacts.parse_speakers_document(legacy) is legacy
    assert artifacts.parse_speakers_document(_json.dumps(legacy))["source"] == "loopback"


def test_ids_are_namespaced_with_the_source() -> None:
    """`id` 与 `segments[*].speaker` 一律带 `source:` 前缀：跨路不串靠形状。"""
    document = _document()
    mic = _document(source="mic")

    assert [row["id"] for row in document["speakers"]] == ["loopback:S01", "loopback:S02"]
    assert [item["speaker"] for item in document["segments"]] == [
        "loopback:S01",
        "loopback:S02",
        "loopback:S01",
    ]
    mic_ids = {row["id"] for row in mic["speakers"]}
    loop_ids = {row["id"] for row in document["speakers"]}
    # 两路的编号集合完全不相交：「这条路的一号等于那条路的一号」表达不出来
    assert mic_ids & loop_ids == set()
    assert "mic:S01" in mic_ids and "loopback:S01" in loop_ids


def test_segments_keep_second_level_floats() -> None:
    """JSON 里的 `start` / `end` 是秒级浮点（读得回小数）。"""
    document = _document()

    assert document["segments"][0]["start"] == 0.48
    assert document["segments"][0]["end"] == 3.2
    assert document["segments"][2]["start"] == 65.5
    assert any(
        float(item["start"]) % 1 for item in document["segments"]
    ), "至少有一段的起始秒带小数"


def test_speaker_summary_totals_match_the_segments() -> None:
    """汇总的段数与秒数加总与 `segments` 对得上（段数与逐段累加的秒数）。"""
    document = _document()

    assert sum(row["segments"] for row in document["speakers"]) == len(document["segments"])
    expected = round(
        sum(item["end"] - item["start"] for item in document["segments"]), 3
    )
    assert round(sum(row["seconds"] for row in document["speakers"]), 3) == expected


def test_speaker_rows_keep_the_raw_engine_label() -> None:
    """`label_raw` 记引擎原始标签、`local` 记规范化编号：两者都留着，改编号规则时可追溯。"""
    from meeting.offline.engine import OfflineSegment
    from meeting.offline.speakers import number_speakers

    spans = number_speakers(
        "loopback",
        [
            OfflineSegment(0.0, 1.0, "S07", "甲"),
            OfflineSegment(1.0, 2.0, "S02", "乙"),
            OfflineSegment(2.0, 3.0, "S07", "丙"),
        ],
    )
    rows = artifacts.speaker_rows(spans, raw_labels=["S07", "S02", "S07"])

    # 行序走「限定名首次出现的顺序」，label_raw 按编号对齐（不按位置对齐）
    assert [(row["local"], row["label_raw"], row["segments"]) for row in rows] == [
        ("S01", "S07", 2),
        ("S02", "S02", 1),
    ]


# ---------------------------------------------------------------------------
# 二、读方的三个拒绝面（每条都真的去读一次）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda doc: doc.pop("schema_version"), id="schema_version-缺失"),
        pytest.param(lambda doc: doc.update(schema_version=2), id="主版本不认识"),
        pytest.param(lambda doc: doc.pop("source"), id="source-缺失"),
        pytest.param(lambda doc: doc.update(source="  "), id="source-空白"),
        pytest.param(lambda doc: doc.pop("segments"), id="segments-缺失"),
        pytest.param(lambda doc: doc.pop("speakers"), id="speakers-缺失"),
        pytest.param(
            lambda doc: doc["segments"].append({"start": 1.0, "end": 2.0, "text": "缺 speaker"}),
            id="segment-缺必填键",
        ),
    ],
)
def test_reader_refuses_a_document_that_breaks_the_contract(mutate) -> None:
    """契约不满足 → 读方抛 `ArtifactContractError` 并如实说明是哪一格（任何情况下都不猜）。"""
    document = _document()
    mutate(document)

    with pytest.raises(artifacts.ArtifactContractError):
        artifacts.parse_speakers_document(document)


def test_reader_reports_which_field_is_missing() -> None:
    """报错文案说得清是哪一格：`schema_version` 与 `source` 各报各的。"""
    document = _document()
    document.pop("schema_version")
    with pytest.raises(artifacts.ArtifactContractError) as info:
        artifacts.parse_speakers_document(document)
    assert "schema_version" in str(info.value)

    document = _document()
    document.pop("source")
    with pytest.raises(artifacts.ArtifactContractError) as info:
        artifacts.parse_speakers_document(document)
    assert "source" in str(info.value)
    assert "两路" in str(info.value)


def test_reader_refuses_an_unknown_major_version_by_number() -> None:
    """主版本号不认识时报出读到的那个号与本实现认识的号。"""
    document = _document()
    document["schema_version"] = 7

    with pytest.raises(artifacts.ArtifactContractError) as info:
        artifacts.parse_speakers_document(document)
    assert "7" in str(info.value)
    assert str(artifacts.KNOWN_SCHEMA_MAJOR) in str(info.value)


def test_reader_refuses_garbage() -> None:
    """自由文本、列表、顶层不是对象的 JSON 一并拒掉。"""
    for bad in ("这不是 JSON", "[]", "3"):
        with pytest.raises(artifacts.ArtifactContractError):
            artifacts.parse_speakers_document(bad)
    with pytest.raises(artifacts.ArtifactContractError):
        artifacts.parse_speakers_document(["不是对象"])


def test_reader_accepts_the_file_it_wrote(tmp_path) -> None:
    """写出去的必须读得回来：读回的字段与写进去的逐格相等。"""
    target = tmp_path / "meeting_offline_speakers_loopback.json"
    document = _document()

    artifacts.write_speakers(target, document)
    read_back = artifacts.read_speakers_artifact(target)

    assert read_back == document
    assert read_back["source"] == "loopback"
    assert read_back["segments"][0]["speaker"] == "loopback:S01"


def test_writer_refuses_to_write_a_broken_document(tmp_path) -> None:
    """写入前先过一遍读方契约：半份 / 缺字段的对照表不让它落盘。"""
    document = _document()
    document.pop("source")

    with pytest.raises(artifacts.ArtifactContractError):
        artifacts.write_speakers(tmp_path / "broken.json", document)
    assert not (tmp_path / "broken.json").exists()


@pytest.mark.parametrize(
    "bad_source",
    [
        pytest.param("", id="空字符串"),
        pytest.param("   ", id="全空白"),
        pytest.param("\t\n", id="制表与换行"),
        pytest.param(None, id="None"),
    ],
)
def test_builder_refuses_an_empty_source(bad_source) -> None:
    """公开的写入侧入口自己拦空 `source`：不许组出限定名退化成 `:S01` 的那种对照表。

    `build_speakers_document()` 是模块的公开入口（不经过 `write_speakers()` 也能被调用），
    读方那条判据在写入侧同样成立 —— 少了它，`\":S01\"` 这种形状有办法先出现在内存里。
    """
    with pytest.raises(artifacts.ArtifactContractError) as info:
        _document(source=bad_source)

    assert "source" in str(info.value)


def test_builder_keeps_a_valid_source_verbatim() -> None:
    """合法的 `source` 原样进限定名（这道闸只拦空值，不改写别的）。"""
    document = _document(source="loopback")

    assert document["source"] == "loopback"
    assert [row["id"] for row in document["speakers"]] == ["loopback:S01", "loopback:S02"]


# ---------------------------------------------------------------------------
# 三、转写：行格式与分钟级时间戳
# ---------------------------------------------------------------------------


def test_transcript_lines_are_minute_level_with_a_bold_speaker(tmp_path) -> None:
    """每行 `[HH:MM] **S01**: 正文`，时间戳恰好 5 个字符。"""
    target = tmp_path / "meeting_offline_transcript_loopback.md"
    artifacts.write_transcript(
        target,
        [
            artifacts.format_transcript_line(0.48, "S01", "第一句"),
            artifacts.format_transcript_line(65.5, "S02", "第二句"),
            artifacts.format_transcript_line(5943.3, "S01", "第 99 分钟那句"),
        ],
    )

    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "[00:00] **S01**: 第一句"
    assert lines[1] == "[00:01] **S02**: 第二句"
    assert lines[2] == "[01:39] **S01**: 第 99 分钟那句"
    for line in lines:
        assert len(line.split("]")[0]) == 6, line  # "[" + 5 个字符
        assert line[1:6].count(":") == 1
    assert target.read_text(encoding="utf-8").endswith("\n")


def test_transcript_reader_parses_the_lines_back(tmp_path) -> None:
    """读回的每行带 `stamp` / `speaker` / `text` 三格。"""
    target = tmp_path / "meeting_offline_transcript_mic.md"
    artifacts.write_transcript(
        target,
        [
            artifacts.format_transcript_line(0.0, "S01", "甲"),
            artifacts.format_transcript_line(125.0, "S02", "乙"),
        ],
    )

    rows = artifacts.read_transcript_lines(target)

    assert [row["stamp"] for row in rows] == ["00:00", "00:02"]
    assert [row["speaker"] for row in rows] == ["S01", "S02"]
    assert [row["text"] for row in rows] == ["甲", "乙"]


def test_transcript_reader_refuses_a_line_that_does_not_match(tmp_path) -> None:
    """行格式不合契约 → 读方抛错（不静默跳过那一行）。"""
    target = tmp_path / "broken.md"
    target.write_text("[00:00] S01: 少了粗体\n", encoding="utf-8")

    with pytest.raises(artifacts.ArtifactContractError) as info:
        artifacts.read_transcript_lines(target)
    assert "第 1 行" in str(info.value)


def test_minute_stamp_floor_and_negative_clamp() -> None:
    """分钟级取整向下，负数收成 `00:00`。"""
    assert artifacts.minute_stamp(59.999) == "00:00"
    assert artifacts.minute_stamp(60.0) == "00:01"
    assert artifacts.minute_stamp(-3.0) == "00:00"
    assert artifacts.minute_stamp(3600.0) == "01:00"


def test_transcript_line_without_a_speaker_drops_the_bold() -> None:
    """说话人为空时不加粗（沿用项目既有行格式：只有说话人非空才带粗体）。"""
    assert artifacts.format_transcript_line(0.0, "", "无标签正文") == "[00:00] 无标签正文"


def test_empty_transcript_is_an_empty_file(tmp_path) -> None:
    """0 段就是 0 字节文件（不写一个空行，免得读方读出一段空正文）。"""
    target = tmp_path / "empty.md"
    artifacts.write_transcript(target, [])

    assert target.read_bytes() == b""
    assert artifacts.read_transcript_lines(target) == []


# ---------------------------------------------------------------------------
# 四、报告与状态文件
# ---------------------------------------------------------------------------


def _metrics() -> list[artifacts.SourceMetrics]:
    good = artifacts.SourceMetrics(
        source="mic",
        audio_file="meeting_audio_mic_001.pcm",
        audio_seconds=120.0,
        state="done",
        segments=4,
        speakers=2,
        seconds=100.0,
        wall_seconds=30.0,
        rtf=0.25,
    )
    bad = artifacts.SourceMetrics(
        source="loopback",
        state="failed",
        error="no_audio：meeting_audio_loopback_001.pcm 与 meeting_audio_loopback_001.mp3 都不在场",
        reasons={"no_audio": 1},
    )
    return [good, bad]


def test_report_carries_the_b16_sections(tmp_path) -> None:
    """报告里三节齐（段耗时分布 / 失败原因分布 / 实时率）+ 本场基本事实一行。"""
    target = tmp_path / "meeting_offline_report.md"
    artifacts.write_report(
        target,
        session_id="250919",
        session_dir=tmp_path,
        engine={
            "model": "OpenMOSS-Team/MOSS-Transcribe-Diarize",
            "dtype": "bfloat16",
            "device": "auto",
            "selector": "auto",
        },
        metrics=_metrics(),
        total_wall_seconds=30.5,
    )

    text = target.read_text(encoding="utf-8")
    for heading in ("## 段耗时分布", "## 失败原因分布", "## 实时率"):
        assert heading in text
    assert "250919" in text
    assert "OpenMOSS-Team/MOSS-Transcribe-Diarize" in text
    assert "bfloat16" in text
    assert "120.0s" in text
    assert "30.500s" in text
    # 实时率 + V1 的对照基线
    assert "0.250×" in text
    assert f"{artifacts.V1_RTF_BASELINE:.2f}×" in text
    # 失败那一档如实落进原因分布
    assert "no_audio：1" in text
    assert "no_audio：meeting_audio_loopback_001.pcm" in text


def test_the_session_realtime_factor_divides_by_the_wall_clock(tmp_path) -> None:
    """C1：整场那一格的算式是**音频合计 ÷ 总耗时**（括号里逐字给出）。

    这一格曾经印的就是 `总耗时`（`120.0s` 音频跑 30.5 秒 → 印成 `30.500×`），
    与括号里的算式、与逐路那一格都对不上。这里断言算出来的值本身。
    """
    target = tmp_path / "meeting_offline_report.md"
    artifacts.write_report(
        target,
        session_id="250919",
        session_dir=tmp_path,
        engine={"model": "m", "dtype": "bfloat16", "device": "auto", "selector": "auto"},
        metrics=_metrics(),
        total_wall_seconds=30.5,
    )

    text = target.read_text(encoding="utf-8")
    assert "- 整场：3.934×（音频合计 120.0s / 总耗时 30.500s" in text
    assert "整场：30.500×" not in text
    # 逐路那一格是另一条口径（墙钟 ÷ 音频），两格互为倒数，这一条不变
    assert "mic：0.250×" in text


def test_a_zero_wall_clock_does_not_get_used_as_a_divisor(tmp_path) -> None:
    """总耗时为 0 时整场那一格落「算不出」：既不印总耗时自己，也不拿 0 当除数。"""
    target = tmp_path / "meeting_offline_report.md"
    artifacts.write_report(
        target,
        session_id="synthetic-session",
        session_dir=tmp_path,
        engine={"model": "m", "dtype": "bfloat16", "device": "auto", "selector": "auto"},
        metrics=[
            artifacts.SourceMetrics(
                source="mic", audio_seconds=90.0, state="done", segments=3, speakers=2
            )
        ],
        total_wall_seconds=0.0,
    )

    text = target.read_text(encoding="utf-8")
    assert "- 整场：算不出（总耗时 0.000s，音频合计 90.0s）" in text
    assert "整场：0.000×" not in text


def _gate(**overrides) -> dict:
    """逐路那一格 `gate`（用例按需改其中一格）。"""
    base = {
        "applied": True,
        "method": "pcm_bytes",
        "spans_total": 3,
        "spans_judged": 3,
        "spans_unjudged": 0,
        "unjudged_spans": [],
        "unjudged_reason": None,
        "detail": "3 块判过，没有一块是数字静音（一块都没跳过引擎）",
    }
    base.update(overrides)
    return base


def _gate_chunk(index: int, gate: str, reason: str) -> dict:
    """逐块那一行的形状（与 `runner.ChunkRun.to_dict()` 同形，报告逐块那一节读它）。"""
    offset = float((index - 1) * 10)
    return {
        "index": index,
        "offset_seconds": offset,
        "seconds": 10.0,
        "span": [offset, offset + 10.0],
        "segments": 2,
        "wall_seconds": 1.0,
        "error": None,
        "silent": gate == "silent",
        "reason": reason,
        "gate": gate,
        "gate_method": "pcm_bytes",
        "gate_detail": (
            "归档 PCM 上这一段没读全（文件比这个区间短）" if gate == "unjudged" else None
        ),
        "zero_span_segments": 0,
    }


def _headline(tmp_path, metric: artifacts.SourceMetrics, name: str) -> str:
    """跑一次 `write_report()`，交回开头那一行「数字静音门：…」。"""
    target = tmp_path / f"report-{name}.md"
    artifacts.write_report(
        target,
        session_id="250919",
        session_dir=tmp_path,
        engine={"model": "m", "dtype": "bfloat16", "device": "auto", "selector": "auto"},
        metrics=[metric],
        total_wall_seconds=3.0,
    )
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.startswith("- 数字静音门："):
            return line
    raise AssertionError("报告里没有「数字静音门」那一行")


def test_the_report_headline_only_claims_a_silent_skip_that_happened(tmp_path) -> None:
    """报告开头那一行**只说这一趟真发生的事**：跳过几段就写几段，没跳过的不许说跳过。

    改前那一行无条件写着「（峰值全零的跳过引擎）」：一次整段都有声的运行（一块都没跳过）
    照样带着它 —— 这一行是读报告的人第一眼看到的东西，第一句就与逐块记账对不上。
    三档各钉一个**整行**字符串：一段都没跳过、真跳过一段、判不了那一档（照旧）。
    """
    # 1. 三段都有声：整行里不许出现「有段因为没声被跳过」这种说法
    quiet = artifacts.SourceMetrics(
        source="mic",
        audio_file="meeting_audio_mic_001.pcm",
        audio_seconds=30.0,
        state="done",
        segments=5,
        speakers=2,
        seconds=12.0,
        wall_seconds=3.0,
        rtf=0.1,
        gate=_gate(),
    )
    line = _headline(tmp_path, quiet, "nothing-skipped")
    assert line == (
        "- 数字静音门：在 3 段音频上都给出了结论，用的尺子是 pcm_bytes"
        "（没有一段是数字静音，一段都没跳过引擎）"
    )
    assert "峰值全零" not in line

    # 2. 真跳过一段：跳过的段数写在那一行上
    hushed = artifacts.SourceMetrics(
        source="mic",
        audio_file="meeting_audio_mic_001.pcm",
        audio_seconds=30.0,
        state="done",
        segments=5,
        speakers=2,
        seconds=12.0,
        wall_seconds=3.0,
        rtf=0.1,
        silent_spans=[(10.0, 20.0)],
        gate=_gate(detail="3 块判过，其中 1 块是数字静音（跳过了引擎）"),
    )
    line = _headline(tmp_path, hushed, "one-skipped")
    assert line == (
        "- 数字静音门：在 3 段音频上都给出了结论，用的尺子是 pcm_bytes"
        "（其中 1 段是数字静音，跳过了引擎）"
    )
    assert "峰值全零" not in line

    # 3. 判不了那一档照旧：那一行仍走「没做成判断」，一个字都没动
    blocked = artifacts.SourceMetrics(
        source="mic",
        audio_file="meeting_audio_mic_001.pcm",
        audio_seconds=30.0,
        state="done",
        segments=5,
        speakers=2,
        seconds=12.0,
        wall_seconds=3.0,
        rtf=0.1,
        unjudged_spans=[(20.0, 30.0)],
        chunks=[
            _gate_chunk(1, "not_silent", "transcribed"),
            _gate_chunk(2, "not_silent", "transcribed"),
            _gate_chunk(3, "unjudged", "gate_unjudged"),
        ],
        gate=_gate(
            applied=False,
            spans_judged=2,
            spans_unjudged=1,
            unjudged_spans=[[20.0, 30.0]],
            unjudged_reason="归档 PCM 上这一段没读全（文件比这个区间短）",
            detail="1 块的静音与否判不了，照常送了引擎",
        ),
    )
    line = _headline(tmp_path, blocked, "unjudged")
    assert line == (
        "- 数字静音门：⚠ **没做成判断**：2 / 3 段判过，其余几段照常送了引擎（mic 第 3 块）"
    )


def test_status_file_round_trips(tmp_path) -> None:
    """状态文件读得回来，十二格（含 `schema_version` / `partial` / `silent` / `gate` / `error`）都在。"""
    target = tmp_path / "meeting_offline_status.json"
    artifacts.write_status(
        target,
        session_id="250919",
        state="running",
        started_at="2025-09-19T20:00:00",
        sources=["mic", "loopback"],
        artifacts={},
    )
    running = artifacts.read_status(target)

    assert running == {
        "schema_version": artifacts.STATUS_SCHEMA_VERSION,
        "session_id": "250919",
        "state": "running",
        "started_at": "2025-09-19T20:00:00",
        "finished_at": None,
        "sources": ["mic", "loopback"],
        "artifacts": {},
        "partial": False,
        # 没声跳过那一档的两格：跑成的那一趟是 `False` / 空数组，读方按它们分得开
        # 「整场都转好了」与「有几段本来就没有音频」
        "silent": False,
        "silent_spans": [],
        # 门那一格在 `running` 那一版还没有可报的（空映射）：它由跑完那一次写全
        "gate": {},
        "error": None,
    }

    artifacts.write_status(
        target,
        session_id="250919",
        state="failed",
        started_at="2025-09-19T20:00:00",
        sources=["mic"],
        artifacts={"mic": {"transcript": "a.md", "speakers": "a.json", "report": "r.md"}},
        finished_at="2025-09-19T20:10:00",
        error="engine_error：RuntimeError: 显存不够",
    )
    done = artifacts.read_status(target)

    assert done["state"] == "failed"
    assert done["finished_at"] == "2025-09-19T20:10:00"
    assert done["error"].startswith("engine_error")
    assert done["artifacts"]["mic"]["report"] == "r.md"


def test_status_file_carries_whether_the_gate_was_applied(tmp_path) -> None:
    """门做成没做成判断进状态文件顶层与逐路：`applied = false` 时列出**哪几段**判不了。

    这一格要解决的问题：一次门没用上的运行，原先与一次健康的运行在状态文件里一个样
    （都是 `state: done`、`partial: false`、`silent: false`），下游读不出这一场有一段
    音频没人判过静音。现在 `applied` 与 `unjudged_spans` 把这件事说出来。
    """
    target = tmp_path / "meeting_offline_status.json"
    per_source_gate = {
        "applied": False,
        "spans_total": 3,
        "spans_judged": 2,
        "spans_unjudged": 1,
        "unjudged_spans": [[20.0, 30.0]],
        "unjudged_reason": "av.error.InvalidDataError: 解不出来",
        "detail": "1 块的静音与否判不了，照常送了引擎",
    }
    artifacts.write_status(
        target,
        session_id="250906",
        state="done",
        started_at="2025-09-19T20:00:00",
        sources=["loopback"],
        artifacts={"loopback": {"transcript": "t.md", "gate": per_source_gate}},
        finished_at="2025-09-19T21:00:00",
        gate={
            "applied": False,
            "spans_total": 3,
            "spans_judged": 2,
            "spans_unjudged": 1,
            "unjudged_spans": {"loopback": [[20.0, 30.0]]},
        },
    )

    document = artifacts.read_status(target)
    assert document["gate"]["applied"] is False
    assert document["gate"]["unjudged_spans"] == {"loopback": [[20.0, 30.0]]}
    assert document["artifacts"]["loopback"]["gate"]["applied"] is False
    assert document["artifacts"]["loopback"]["gate"]["unjudged_spans"] == [[20.0, 30.0]]

    # 门在每一段上都判过的那一趟（没写 `gate` 时是空映射，与 `running` 那一版同形）
    artifacts.write_status(
        target,
        session_id="250906",
        state="done",
        started_at="2025-09-19T20:00:00",
        sources=["loopback"],
        artifacts={},
    )
    assert artifacts.read_status(target)["gate"] == {}


def test_status_file_carries_the_silent_spans_of_the_whole_session(tmp_path) -> None:
    """没声跳过的那几段进状态文件顶层：`state` 仍是 `done`、`partial` 仍是 `false`。

    这三格必须分得开：`partial` 说「有块没跑成」、`silent` 说「那块本来就没有音频」。
    把没声混进 `partial` 或 `error`，读方就会以为这一场坏过。
    """
    target = tmp_path / "meeting_offline_status.json"
    artifacts.write_status(
        target,
        session_id="250906",
        state="done",
        started_at="2025-09-19T20:00:00",
        sources=["loopback"],
        artifacts={"loopback": {"transcript": "t.md", "speakers": "s.json", "report": "r.md"}},
        finished_at="2025-09-19T21:00:00",
        silent=True,
        silent_spans=[(2940.0, 3390.0), (6540.0, 6990.0)],
    )

    document = artifacts.read_status(target)
    assert document["state"] == "done"
    assert document["partial"] is False
    assert document["error"] is None
    assert document["silent"] is True
    assert document["silent_spans"] == [[2940.0, 3390.0], [6540.0, 6990.0]]


def test_status_file_rejects_an_unknown_state(tmp_path) -> None:
    """状态只认五档（`running` / `done` / `partial` / `failed` / `interrupted`），写别的一律抛
    （不许在盘上留下一版读不懂的状态）。`interrupted` 是「打断会后」节点（b1c6a58e）加的
    那一档：跑到块边界被人停的终态。"""
    artifacts.write_status(
        tmp_path / "status.json",
        session_id="250919",
        state="interrupted",
        started_at="2025-09-19T20:00:00",
        sources=["mic"],
        artifacts={},
        finished_at="2025-09-19T20:01:00",
        error="interrupted：跑到第 2/5 块被停下",
    )
    document = artifacts.read_status(tmp_path / "status.json")
    assert document["state"] == "interrupted"
    assert document["finished_at"] == "2025-09-19T20:01:00"
    assert "跑到第 2/5 块" in (document["error"] or "")
    with pytest.raises(ValueError):
        artifacts.write_status(
            tmp_path / "status.json",
            session_id="250919",
            state="halfway",
            started_at="now",
            sources=["mic"],
            artifacts={},
        )


def test_status_reader_reports_a_missing_file(tmp_path) -> None:
    """状态文件不在场时读方抛错（调用方据此判「还没跑」）。"""
    with pytest.raises(artifacts.ArtifactContractError):
        artifacts.read_status(tmp_path / "nope.json")


def test_writes_are_atomic_leaving_no_tmp_behind(tmp_path) -> None:
    """写盘走 `.tmp` + `os.replace`：写完不留残骸。"""
    target = tmp_path / "meeting_offline_report.md"
    artifacts.write_report(
        target,
        session_id="250919",
        session_dir=tmp_path,
        engine={"model": "m", "dtype": "bfloat16", "device": "auto", "selector": "auto"},
        metrics=_metrics(),
        total_wall_seconds=1.0,
    )

    assert target.exists()
    assert [item.name for item in tmp_path.iterdir() if item.suffix == ".tmp"] == []


def test_atomic_write_retries_while_the_target_is_held(tmp_path, monkeypatch) -> None:
    """目标文件被别人拿在手里时（Windows 的 `WinError 32`）退避重试，不把写盘让掉。"""
    target = tmp_path / "meeting_offline_status.json"
    # 实现住在 `meeting/atomicio.py`（两包共用），`os.replace` 的桩打在它身上
    real_replace = atomicio.os.replace
    attempts = []

    def flaky_replace(src, dst):
        attempts.append(dst)
        if len(attempts) < 3:
            raise PermissionError(32, "另一个程序正在使用此文件")
        return real_replace(src, dst)

    monkeypatch.setattr(atomicio.os, "replace", flaky_replace)
    artifacts.write_status(
        target,
        session_id="250919",
        state="running",
        started_at="2025-09-19T20:00:00",
        sources=["mic"],
        artifacts={},
    )

    assert len(attempts) == 3
    assert artifacts.read_status(target)["state"] == "running"
    assert [item.name for item in tmp_path.iterdir() if item.suffix == ".tmp"] == []


def test_atomic_write_gives_up_after_the_retries(tmp_path, monkeypatch) -> None:
    """一直拿不到就如实抛出去（调用方据此记账，不静默丢一次写盘）。"""

    def always_busy(src, dst):
        raise PermissionError(32, "另一个程序正在使用此文件")

    monkeypatch.setattr(atomicio.os, "replace", always_busy)
    with pytest.raises(PermissionError):
        artifacts.write_text_atomic(tmp_path / "x.md", "正文", retries=2)


#: 并发写那一条里每个写方写的轮数（够撞上「共用一份临时文件」那个窗口）
CONCURRENT_WRITE_ROUNDS = 20


def test_concurrent_writers_to_one_target_both_succeed(tmp_path) -> None:
    """两个写方同时写同一个目标：两边都成功，盘上留下的是一份**读得回来**的内容（审查问题 m1）。

    旧写法把临时文件叫 `<名字>.tmp`：两个写方共用它，先落地的 `os.replace` 把它消费掉，
    另一边的 `os.replace` 随即抛 `FileNotFoundError` —— 那条异常逃出了只认
    `PermissionError` 的重试。现场正有这种并发写方：会后主链在写状态文件，另一个进程里
    `meeting post` 那一趟上抛前的补账（`_mark_offline_failed()` → `mark_failed_if_unset()`）
    也在写同一格。

    两个写方各写 20 轮（`session_id` 各不相同，好认出最后留下的是谁那一版），收尾断言
    三件事：没有哪一边抛错、盘上那份读得回来且取值来自其中一个写方、临时文件一个不留。
    """
    target = tmp_path / artifacts.STATUS_NAME
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            for _round in range(CONCURRENT_WRITE_ROUNDS):
                artifacts.write_status(
                    target,
                    session_id=f"writer-{index}",
                    state="running",
                    started_at="2025-09-19T20:00:00",
                    sources=["mic"],
                    artifacts={},
                )
        except BaseException as exc:  # 线程里的异常没有别的落点，收进列表让断言去报
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not any(thread.is_alive() for thread in threads), "两个写方都该在 20 秒内写完"
    assert errors == [], f"并发写不该有哪一边失败：{errors!r}"
    status = artifacts.read_status(target)
    assert status["session_id"] in {"writer-0", "writer-1"}, status
    assert status["state"] == "running", status
    assert [item.name for item in tmp_path.iterdir() if item.suffix == ".tmp"] == []
