"""转写行数的口径（`count_lines()`：`splitlines()` 数法，**空行计入**）。

这个数是行号区间表与分段契约的**分母**：`total_lines` 与末段 `line_end` 都按它对齐，
因此全项目只能有一种数法。`meeting/minutes/inputs.py`（设计）报每一条输入的行数、
提示词渲染在区间表还没切出来时退数转写本身的行数，走的都是这一个函数。

这一层只回答「一共几行」，不做行格式解析：切块按原始行搬运（`chunk.py`），两条转写的
行形（会中稿 `[HH:MM] 正文` / 会后稿 `[HH:MM] **S01**: 正文`）由各自的写方负责
（`meeting/asr/pipeline.py` 与 `meeting/offline/artifacts.py`）。
"""

from __future__ import annotations


def count_lines(text: str) -> int:
    """转写的总行数（`splitlines()` 口径，**空行计入**）。

    这个数是行号区间表与分段契约的**分母**：`total_lines` 与末段 `line_end` 都按它对齐，
    因此全项目只能有一种数法。`meeting/minutes/inputs.py`（设计）报每一条输入的行数时
    走的也是这一个函数。
    """
    return len(text.splitlines())


__all__ = [
    "count_lines",
]
