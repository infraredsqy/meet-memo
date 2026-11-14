"""转写行数的口径（`count_lines()`：`splitlines()` 数法，**空行计入**）。

这个数是行号区间表与分段契约的**分母**：`total_lines` 与末段 `line_end` 都按它对齐，
因此全项目只能有一种数法。
"""

from __future__ import annotations


def count_lines(text: str) -> int:
    return len(text.splitlines())


__all__ = [
    "count_lines",
]
