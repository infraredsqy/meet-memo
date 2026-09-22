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

import threading

import pytest

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
        "audio": {"file": "meeting_audio_loopback.pcm", "seconds": 300.0},
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
    assert document["audio"]["file"] == "meeting_audio_loopback.pcm"
    for item in document["segments"]:
        assert set(item) == {"start", "end", "speaker", "local", "text"}
    for row in document["speakers"]:
        assert set(row) == {"id", "local", "label_raw", "segments", "seconds"}


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
        audio_file="meeting_audio_mic.pcm",
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
        error="no_audio：meeting_audio_loopback.pcm 与 meeting_audio_loopback.mp3 都不在场",
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
    assert "no_audio：meeting_audio_loopback.pcm" in text


def test_status_file_round_trips(tmp_path) -> None:
    """状态文件读得回来，八格（含 `schema_version` 与 `error`）都在。"""
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


def test_status_file_rejects_an_unknown_state(tmp_path) -> None:
    """状态只认三档，写别的一律抛（不许在盘上留下一版读不懂的状态）。"""
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
    real_replace = artifacts.os.replace
    attempts = []

    def flaky_replace(src, dst):
        attempts.append(dst)
        if len(attempts) < 3:
            raise PermissionError(32, "另一个程序正在使用此文件")
        return real_replace(src, dst)

    monkeypatch.setattr(artifacts.os, "replace", flaky_replace)
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

    monkeypatch.setattr(artifacts.os, "replace", always_busy)
    with pytest.raises(PermissionError):
        artifacts.write_text_atomic(tmp_path / "x.md", "正文", retries=2)


#: 并发写那一条里每个写方写的轮数（够撞上「共用一份临时文件」那个窗口）
CONCURRENT_WRITE_ROUNDS = 20


def test_concurrent_writers_to_one_target_both_succeed(tmp_path) -> None:
    """两个写方同时写同一个目标：两边都成功，盘上留下的是一份**读得回来**的内容（审查问题 m1）。

    旧写法把临时文件叫 `<名字>.tmp`：两个写方共用它，先落地的 `os.replace` 把它消费掉，
    另一边的 `os.replace` 随即抛 `FileNotFoundError` —— 那条异常逃出了只认
    `PermissionError` 的重试。现场正有这种并发写方：会后主链在写状态文件，收尾那一步
    `meeting/backends/post.py` 的 `_mark_failed()` 也在写同一格。

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
