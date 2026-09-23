"""`meeting/minutes/lines.py` 的契约用例：两种行形、带秒时间戳、占位符是裸行。

盯的是三件事：

  - 一条正则同时吃会中临时稿（`[HH:MM] 正文`）与会后权威稿（`[HH:MM] **S01**: 正文`），
    外加 V1 那份原正则支持的 `[HH:MM:SS]`；
  - 标题行、空行、注释行**一条都收不进来**（`parse_line` 返回 `None`），因为它们一旦进了
    行号区间表就会把标题算成一段发言；
  - `[[识别失败: 超时 20s]]` 这条占位符解析出来 `speaker is None` —— 它是「占位符不许被当成
    说话人标签」的回归网。
"""

from __future__ import annotations

from meeting.minutes.lines import (
    PLACEHOLDER_RE,
    TranscriptLine,
    count_lines,
    parse_line,
    parse_lines,
)

#: 一份合成转写：标题、带标签行、无标签行、占位符、空行、注释行都在里面
SAMPLE = "\n".join(
    [
        "# 会议纪要 — 2025-09-19 21:32",
        "",
        "[21:32] **远端**: 好啦，那我开始了。",
        "[21:32] 好啦，那我开始了。",
        "[21:33:07] **S01**: 带秒的时间戳也该吃。",
        "[[识别失败: 超时 20s]]",
        "<!-- chunk_01: lines 1-100 -->",
        "[21:34] **远端**: 换一个人说。",
    ]
)


def test_parse_line_reads_both_shapes() -> None:
    """带标签与无标签两种行形由同一条正则收下，`stamp` 保持原样。"""
    labeled = parse_line("[21:32] **远端**: 好啦，那我开始了。")
    plain = parse_line("[21:32] 好啦，那我开始了。")

    assert labeled == TranscriptLine(stamp="21:32", speaker="远端", body="好啦，那我开始了。")
    assert plain == TranscriptLine(stamp="21:32", speaker=None, body="好啦，那我开始了。")


def test_parse_line_reads_a_timestamp_with_seconds() -> None:
    """`[HH:MM:SS]` 这种带秒的形状照样吃（V1 原正则支持它）。"""
    parsed = parse_line("[21:33:07] **S01**: 带秒的时间戳也该吃。")

    assert parsed is not None
    assert parsed.stamp == "21:33:07"
    assert parsed.speaker == "S01"
    assert parsed.body == "带秒的时间戳也该吃。"


def test_parse_line_keeps_the_body_verbatim() -> None:
    """正文里的标点、数字、方括号、英文原样留下，只有首尾空白被裁掉。"""
    parsed = parse_line("[09:05] 这一条 说 [R12] 与 R13，还有 3 个数字。  ")

    assert parsed is not None
    assert parsed.body == "这一条 说 [R12] 与 R13，还有 3 个数字。"


def test_parse_line_drops_a_heading_and_a_blank_line() -> None:
    """标题行与空行走 `None` 这一档（它们不是一条发言）。"""
    assert parse_line("# 会议纪要 — 2025-09-19 21:32") is None
    assert parse_line("") is None
    assert parse_line("   ") is None


def test_parse_line_drops_a_comment_line() -> None:
    """`<!-- chunk_01: lines 1-100 -->` 这类注释行同样解析不出来。"""
    assert parse_line("<!-- chunk_01: lines 1-100 -->") is None


def test_placeholder_is_a_bare_line_with_no_speaker() -> None:
    """占位符行是**裸行**：`speaker is None`，`body` 是那串方括号文本。

    这一条是说话人归属的底线：占位符里没有 `**` 包裹的标签，解析器一旦把它算成说话人，
    `[[识别失败: 超时 20s]]` 就会变成纪要附录里的一个人名。
    """
    parsed = parse_line("[21:35] [[识别失败: 超时 20s]]")

    assert parsed is not None
    assert parsed.speaker is None
    assert parsed.body == "[[识别失败: 超时 20s]]"
    assert PLACEHOLDER_RE.match(parsed.body) is not None


def test_placeholder_speaker_becomes_none_when_the_label_is_empty() -> None:
    """`**` 里面是空白时收成 `None`（空字符串不许漏到下游）。"""
    parsed = parse_line("[21:35] **   **: 正文")

    assert parsed is not None
    assert parsed.speaker is None
    assert parsed.body == "正文"


def test_parse_lines_skips_the_unparsable_lines() -> None:
    """整份解析：四条能解析的行按顺序出来，标题 / 空行 / 占位符 / 注释都不在返回值里。

    占位符**带时间戳**时是能解析的（它是正文的一部分），这里单独有一条用例盯着它。
    """
    parsed = parse_lines(SAMPLE)

    assert [(item.stamp, item.speaker, item.body) for item in parsed] == [
        ("21:32", "远端", "好啦，那我开始了。"),
        ("21:32", None, "好啦，那我开始了。"),
        ("21:33:07", "S01", "带秒的时间戳也该吃。"),
        ("21:34", "远端", "换一个人说。"),
    ]


def test_parse_lines_keeps_a_placeholder_that_carries_a_stamp() -> None:
    """带时间戳的占位符是一条正文：收下它，`speaker` 仍是 `None`。"""
    parsed = parse_lines("[21:35] [[识别失败: 超时 20s]]")

    assert len(parsed) == 1
    assert parsed[0].speaker is None
    assert parsed[0].body == "[[识别失败: 超时 20s]]"


def test_count_lines_counts_blank_lines_too() -> None:
    """行数口径是 `splitlines()`：空行计入（`SAMPLE` 里有一条空行）。"""
    assert count_lines(SAMPLE) == 8
    assert count_lines("") == 0
    assert count_lines("a\n\nb") == 3


def test_line_numbering_follows_splitlines_order() -> None:
    """行号就是 `splitlines()` 的序：第 3 行才是第一条正文。"""
    numbered = {
        index: parse_line(raw)
        for index, raw in enumerate(SAMPLE.splitlines(), start=1)
        if parse_line(raw) is not None
    }

    assert sorted(numbered) == [3, 4, 5, 8]
