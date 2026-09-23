"""`meeting/minutes/chunk.py` 的契约用例：切块、区间表、重跑清旧块、档位判定。

盯的是四件事：

  - 块文件里**只有原始行**（`"\\n".join(块的行) + "\\n"`），行号因此与源文件对得上；
  - `00_index.md` 的形状照 V1（`# Chunk Index` + 五条 bullet + 区间表），且表里的
    `line_start` / `line_end` / `lines` 与块文件的**真实内容**逐条相等；
  - 重跑（源文件变短）之后，块目录里没有上一次剩下的旧块；
  - 档位按盘上有哪几份转写判定：两条都在 `both`、只有一条 `live_only` / `offline_only`。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meeting.minutes.chunk import (
    INDEX_NAME,
    ChunkError,
    InputSpec,
    build_index_text,
    chunk_session,
    chunk_source,
    discover_inputs,
    main,
    mode_for,
    read_transcript_text,
)

#: 一份合成转写（15 行，含标题行与空行，空行计入行号）
SAMPLE_LINES = [
    "# 会议纪要 — 2025-09-19 21:32",
    "",
    "[21:32] **远端**: 好啦，那我开始了。",
    "[21:33] **远端**: 先讲项目管理。",
    "[21:34] 这一段没有说话人标签。",
    "[21:35] 继续讲。",
    "[21:36] **S01**: 换一个人说。",
    "[21:37] **S01**: 再一句。",
    "[21:38] 无标签行。",
    "[21:39] 还是无标签。",
    "[21:40] **远端**: 回到正题。",
    "[21:41] 收尾一句话。",
    "[21:42] 又一句。",
    "[21:43] 最后一句。",
    "[21:44] 结束。",
]

#: 合成转写的全文（`write_text` 之后读回来的字节与它一致）
SAMPLE_TEXT = "\n".join(SAMPLE_LINES) + "\n"


@pytest.fixture()
def transcript(tmp_path: Path) -> Path:
    """一份落在临时目录里的转写。"""
    path = tmp_path / "meeting_transcript_loopback.md"
    path.write_text(SAMPLE_TEXT, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 一、切块与区间表
# ---------------------------------------------------------------------------


def test_chunk_source_writes_chunks_with_exact_raw_lines(transcript: Path, tmp_path: Path) -> None:
    """5 行一块：块文件内容与源文件逐行相等，文件名从 `chunk_01.md` 起、两位。"""
    report = chunk_source("loopback", transcript, tmp_path / "out", chunk_lines=10)

    assert report.chunk_count == 2
    assert [record.file for record in report.chunks] == ["chunk_01.md", "chunk_02.md"]
    first = (tmp_path / "out" / "chunks" / "chunk_01.md").read_text(encoding="utf-8")
    second = (tmp_path / "out" / "chunks" / "chunk_02.md").read_text(encoding="utf-8")
    assert first == "\n".join(SAMPLE_LINES[:10]) + "\n"
    assert second == "\n".join(SAMPLE_LINES[10:]) + "\n"
    # 块文件里只有原始行：没有头、没有注释
    assert not first.startswith("<!--")
    assert "<!--" not in first


def test_chunk_records_cover_every_line_once(transcript: Path, tmp_path: Path) -> None:
    """区间表里的账：从第 1 行起、闭区间、逐块接上、总和等于源文件行数。"""
    report = chunk_source("loopback", transcript, tmp_path / "out", chunk_lines=10)

    assert report.total_lines == len(SAMPLE_LINES) == 15
    assert [(record.line_start, record.line_end, record.lines) for record in report.chunks] == [
        (1, 10, 10),
        (11, 15, 5),
    ]


def test_index_text_has_the_v1_shape(transcript: Path, tmp_path: Path) -> None:
    """`00_index.md` 的形状：`# Chunk Index` + 五条 bullet + 表头 + 分隔行 + 每块一行。"""
    report = chunk_source("loopback", transcript, tmp_path / "out", chunk_lines=10)

    assert report.index_text.splitlines()[:9] == [
        "# Chunk Index",
        "",
        "- source: meeting_transcript_loopback.md",
        "- total_lines: 15",
        "- chunk_size: 10",
        "- chunk_count: 2",
        "- chunk_dir: chunks/",
        "",
        "| file | line_start | line_end | lines |",
    ]
    assert report.index_text.splitlines()[9] == "| --- | --- | --- | --- |"
    assert report.index_text.splitlines()[10] == "| chunks/chunk_01.md | 1 | 10 | 10 |"
    assert report.index_text.splitlines()[11] == "| chunks/chunk_02.md | 11 | 15 | 5 |"


def test_index_table_matches_the_real_chunk_files(transcript: Path, tmp_path: Path) -> None:
    """逐条核对：表里的三个数与块文件的真实行数 / 源文件对应区间相等，且行数之和等于总数。"""
    out = tmp_path / "out"
    report = chunk_source("loopback", transcript, tmp_path / "out", chunk_lines=10)
    index_text = (out / INDEX_NAME).read_text(encoding="utf-8")
    source_lines = read_transcript_text(transcript).splitlines()

    rows = [
        line for line in index_text.splitlines() if line.startswith("| chunks/")
    ]
    assert len(rows) == report.chunk_count
    total = 0
    for row in rows:
        _, file, line_start, line_end, lines, _ = [cell.strip() for cell in row.split("|")]
        block = (out / file).read_text(encoding="utf-8").splitlines()
        assert len(block) == int(lines)
        assert block == source_lines[int(line_start) - 1 : int(line_end)]
        assert int(line_end) - int(line_start) + 1 == int(lines)
        total += int(lines)
    assert total == len(source_lines)


def test_chunk_source_reads_with_replacement_on_bad_bytes(tmp_path: Path) -> None:
    """坏字节不打断切块：`errors="replace"`（与 V1 脚本同口径）。"""
    path = tmp_path / "meeting_transcript_mic.md"
    path.write_bytes("[21:32] 好".encode("utf-8") + b"\xff\xfe" + "\n".encode("utf-8"))

    report = chunk_source("mic", path, tmp_path / "out", chunk_lines=10)

    assert report.chunk_count == 1
    assert "\ufffd" in (tmp_path / "out" / "chunks" / "chunk_01.md").read_text(encoding="utf-8")


def test_chunk_source_clears_stale_chunks_before_writing(tmp_path: Path) -> None:
    """重跑（源文件变短）之后块目录里没有旧块残留。"""
    path = tmp_path / "meeting_transcript_loopback.md"
    out = tmp_path / "out"
    long_text = "\n".join(f"[21:{minute:02d}] 第 {minute} 句。" for minute in range(30)) + "\n"
    path.write_text(long_text, encoding="utf-8")
    first = chunk_source("loopback", path, out, chunk_lines=10)
    assert first.chunk_count == 3

    short_text = "\n".join(f"[21:{minute:02d}] 短了。" for minute in range(12)) + "\n"
    path.write_text(short_text, encoding="utf-8")
    second = chunk_source("loopback", path, out, chunk_lines=10)

    assert second.chunk_count == 2
    assert sorted(item.name for item in (out / "chunks").glob("chunk_*.md")) == [
        "chunk_01.md",
        "chunk_02.md",
    ]
    assert chunk_source("loopback", path, out, chunk_lines=10).total_lines == 12


def test_chunk_source_leaves_other_files_alone(tmp_path: Path) -> None:
    """清理只动 `chunk_*.md` 这一族：别人的产物与别的文件名一个字节不碰。"""
    path = tmp_path / "meeting_transcript_loopback.md"
    path.write_text(SAMPLE_TEXT, encoding="utf-8")
    out = tmp_path / "out"
    chunk_dir = out / "chunks"
    chunk_dir.mkdir(parents=True)
    keep = chunk_dir / "notes.md"
    keep.write_text("留着", encoding="utf-8")
    (chunk_dir / "chunk_99.md").write_text("旧块", encoding="utf-8")

    chunk_source("loopback", path, out, chunk_lines=10)

    assert keep.read_text(encoding="utf-8") == "留着"
    assert not (chunk_dir / "chunk_99.md").exists()


def test_chunk_source_handles_an_empty_transcript(tmp_path: Path) -> None:
    """空转写给 0 块、表里没有数据行（不抛错：刚开的场次目录就是这个样子）。"""
    path = tmp_path / "meeting_transcript_mic.md"
    path.write_text("", encoding="utf-8")

    report = chunk_source("mic", path, tmp_path / "out", chunk_lines=10)

    assert report.total_lines == 0
    assert report.chunk_count == 0
    assert report.index_text.splitlines()[-1] == "| --- | --- | --- | --- |"


def test_chunk_source_rejects_a_bad_chunk_lines(transcript: Path, tmp_path: Path) -> None:
    """每块行数落在 10–1000：配错的值在切第一块之前就拦下。"""
    for bad in (0, 9, 1001, -5):
        with pytest.raises(ChunkError, match="chunk_lines"):
            chunk_source("loopback", transcript, tmp_path / "out", chunk_lines=bad)
    with pytest.raises(ChunkError, match="整数"):
        chunk_source("loopback", transcript, tmp_path / "out", chunk_lines="100")  # type: ignore[arg-type]


def test_chunk_source_reports_a_missing_transcript(tmp_path: Path) -> None:
    """转写不在时抛自己的错误类，消息里带着路径。"""
    missing = tmp_path / "meeting_transcript_loopback.md"

    with pytest.raises(ChunkError, match="转写读不动"):
        chunk_source("loopback", missing, tmp_path / "out")


def test_build_index_text_is_pure() -> None:
    """区间表的拼装不落盘（读方要的只是那段文本时不必建目录）。"""
    assert build_index_text(transcript="a.md", total_lines=0, chunk_lines=100, chunks=[]).startswith(
        "# Chunk Index\n"
    )


# ---------------------------------------------------------------------------
# 二、场次目录：落盘路径与档位
# ---------------------------------------------------------------------------


def _write_session(session: Path, *, live: bool = True, offline: bool = False, source: str = "loopback") -> None:
    """造一个 V2 形状的场次目录（按需放会中稿与会后稿）。"""
    session.mkdir(parents=True, exist_ok=True)
    if live:
        (session / f"meeting_transcript_{source}.md").write_text(SAMPLE_TEXT, encoding="utf-8")
    if offline:
        (session / f"meeting_offline_transcript_{source}.md").write_text(
            "\n".join(f"[21:{minute:02d}] **S01**: 第 {minute} 句。" for minute in range(12)) + "\n",
            encoding="utf-8",
        )


def test_chunk_session_writes_under_working_per_source(tmp_path: Path) -> None:
    """落盘路径钉死：`working/<source>/chunks/` 与 `working/<source>/00_index.md`。"""
    session = tmp_path / "250919"
    _write_session(session, live=True, offline=True)

    reports = chunk_session(session, discover_inputs(session), chunk_lines=10)

    assert sorted(reports) == ["loopback"]
    working = session / "working" / "loopback"
    assert (working / INDEX_NAME).is_file()
    assert sorted(item.name for item in (working / "chunks").glob("chunk_*.md")) == [
        "chunk_01.md",
        "chunk_02.md",
    ]
    assert reports["loopback"].chunk_count == 2


def test_chunk_session_accepts_the_inputs_json_shape(tmp_path: Path) -> None:
    """`inputs.json` 里那种逐条映射也收（phase2 直接把自己写下的清单交进来）。"""
    session = tmp_path / "250919"
    _write_session(session, live=True, offline=True)
    payload = {
        "schema_version": 1,
        "mode": "both",
        "sources": [
            {
                "source": "loopback",
                "live": {"file": "meeting_transcript_loopback.md", "lines": 15},
                "offline": {"file": "meeting_offline_transcript_loopback.md", "lines": 12},
            }
        ],
    }

    reports = chunk_session(session, payload["sources"], chunk_lines=10)

    # 两条都在时切的是会后那份权威稿
    assert reports["loopback"].transcript == "meeting_offline_transcript_loopback.md"
    assert reports["loopback"].total_lines == 12


def test_chunk_session_and_discover_cover_both_sources(tmp_path: Path) -> None:
    """两路各一条输入时各写各的目录（两路不许撞在一起）。"""
    session = tmp_path / "250919"
    _write_session(session, live=True, source="mic")
    _write_session(session, live=True, source="loopback")

    reports = chunk_session(session, discover_inputs(session), chunk_lines=10)

    assert sorted(reports) == ["loopback", "mic"]
    assert (session / "working" / "mic" / INDEX_NAME).is_file()
    assert (session / "working" / "loopback" / INDEX_NAME).is_file()


def test_chunk_session_reports_a_bare_input(tmp_path: Path) -> None:
    """一条输入里两份文件名都没有时抛错（说不出该切哪一份）。"""
    with pytest.raises(ChunkError, match="没有任何一份转写"):
        chunk_session(tmp_path, [InputSpec(source="mic", live=None, offline=None)])


def test_discover_inputs_finds_only_what_is_on_disk(tmp_path: Path) -> None:
    """只报盘上真有的那几份；两路都没有的 `source` 不出现。"""
    _write_session(tmp_path, live=True, offline=False, source="loopback")

    inputs = discover_inputs(tmp_path)

    assert [spec.source for spec in inputs] == ["loopback"]
    assert inputs[0].live == "meeting_transcript_loopback.md"
    assert inputs[0].offline is None
    assert discover_inputs(tmp_path / "not-here") == ()


def test_mode_for_walks_the_three_degradation_levels(tmp_path: Path) -> None:
    """档位判定：两条都在 `both`、只有会中 `live_only`、只有会后 `offline_only`、都没有 `None`。"""
    both = tmp_path / "both"
    _write_session(both, live=True, offline=True)
    live = tmp_path / "live"
    _write_session(live, live=True, offline=False)
    offline = tmp_path / "offline"
    _write_session(offline, live=False, offline=True)

    assert mode_for(discover_inputs(both)) == "both"
    assert mode_for(discover_inputs(live)) == "live_only"
    assert mode_for(discover_inputs(offline)) == "offline_only"
    assert mode_for(()) is None


def test_input_spec_counts_lines_per_kind(tmp_path: Path) -> None:
    """每份转写的行数按 `splitlines()` 数（两条各数各的，缺席那份记 0）。"""
    _write_session(tmp_path, live=True, offline=True)
    spec = discover_inputs(tmp_path)[0]

    assert spec.line_count(tmp_path, "live") == 15
    assert spec.line_count(tmp_path, "offline") == 12


# ---------------------------------------------------------------------------
# 三、命令行入口（不加载 meeting.config）
# ---------------------------------------------------------------------------


def test_main_chunks_a_session_and_reports_the_mode(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """`main()` 只吃场次目录：切完报每路几块、最后报档位。"""
    session = tmp_path / "250919"
    _write_session(session, live=True, offline=True)

    code = main([str(session), "--chunk-lines", "10"])

    assert code == 0
    printed = capsys.readouterr().out
    assert "loopback: meeting_offline_transcript_loopback.md 12 行 → 2 块" in printed
    assert "档位：both" in printed
    assert (session / "working" / "loopback" / INDEX_NAME).is_file()


def test_main_answers_a_session_without_any_transcript(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """一条转写都没有时退 1 并把「找的是哪两个名字」打在 stderr 上。"""
    session = tmp_path / "250919"
    session.mkdir()

    assert main([str(session)]) == 1
    assert "meeting_offline_transcript_" in capsys.readouterr().err


def test_main_answers_a_missing_session_dir(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """场次目录不在时退 2（用法这一档），不去建目录。"""
    missing = tmp_path / "260920"

    assert main([str(missing)]) == 2
    assert "场次目录不存在" in capsys.readouterr().err
    assert not missing.exists()


def test_minutes_package_never_touches_the_config_chain() -> None:
    """`meeting.minutes` 的源码里不 import 配置链（后端子命令自己站着）。

    这条是**文字检查**（不 import 任何东西）：phase2 的 runner 才需要配置，文字机器这一层
    不该为了几个默认值把家目录、日志、输出根那条链拉进来。
    """
    minutes_dir = Path(__file__).resolve().parents[1] / "meeting" / "minutes"
    for name in ("chunk.py", "lines.py", "contract.py", "prompt.py", "__main__.py"):
        text = (minutes_dir / name).read_text(encoding="utf-8")
        assert "from meeting import config" not in text, f"{name} 里 import 了配置链"
        assert "from meeting.config import" not in text, f"{name} 里 import 了配置链"
        assert "import meeting.config" not in text, f"{name} 里 import 了配置链"


def test_chunks_report_as_dict_is_json_serialisable(tmp_path: Path) -> None:
    """`SourceChunks.as_dict()` 能直接 `json.dumps`（phase2 往 `inputs.json` 里记分块结果）。"""
    _write_session(tmp_path, live=True)
    report = chunk_session(tmp_path, discover_inputs(tmp_path), chunk_lines=10)["loopback"]

    text = json.dumps(report.as_dict(), ensure_ascii=False)

    assert '"chunk_count": 2' in text
    assert '"chunk_dir": "working/loopback/chunks"' in text
    assert '"line_start": 11' in text
