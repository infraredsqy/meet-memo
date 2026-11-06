"""`meeting/minutes/lines.py` 的契约用例：行数口径（`splitlines()`，空行计入）。

这一层只回答「一共几行」：行号区间表与分段契约的分母都是它，全项目只能有一种数法，
空行也计入（一条空行在源文件里占一个行号）。
"""

from __future__ import annotations

from meeting.minutes.lines import count_lines

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


def test_count_lines_counts_blank_lines_too() -> None:
    """行数口径是 `splitlines()`：空行计入（`SAMPLE` 里有一条空行）。"""
    assert count_lines(SAMPLE) == 8
    assert count_lines("") == 0
    assert count_lines("a\n\nb") == 3
