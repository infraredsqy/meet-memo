"""转写行的解析（`41465462` spec「需求冻结 · 输入是什么」那一张表里的两种行形）。

这个模块服务 spec 里这一条：两条转写的行解析 —— 会中临时稿的 `[HH:MM] 正文` 与会后权威稿的
`[HH:MM] **S01**: 正文` 由**同一个**正则一起吃掉，下游（切块、契约校验、提示词渲染）因此拿到的
是同一份 `TranscriptLine`，不必各自再认一次格式。

**为什么不复用 `meeting/offline/artifacts.py` 的 `_LINE_RE`**：那一条要求行首必须是粗体标签，
会中临时稿那些无标签行它一条都吃不下去（它服务的是会后那一份权威稿的读者）。两条转写要在这一层
并排读，就得出一个两种行形都收得下的解析器，落在这里。

**覆盖范围**（出处 V1 `.work/handoff/split_raw.py:12-13` 的原正则，本轮重写）：

  - `[21:32] **远端**: 好啦，那我开始了。` —— 带粗体说话人标签；
  - `[21:32] 好啦，那我开始了。` —— 无标签（会中临时稿的常态）；
  - `[21:32:05] …` —— 带秒的时间戳（V1 正则支持它，本轮照样吃）。

行号口径与项目其它地方一致：一律 `splitlines()` 的数法，**空行计入**（`count_lines()`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 一行转写的形状：`[HH:MM]` / `[HH:MM:SS]` + 可选的 `**说话人**:` + 正文。
#:
#: 三处细节与 V1 原正则一致，都是有意的：
#:   - 时间戳只认**两位数**的时分（不认 `9:05`），正文里出现的 `[3]` 这类短方括号因此不会被
#:     误当成时间戳；
#:   - 说话人标签要求行内**确实出现了** `**…**:` 才收成非空，`\s*` 让 `**远端**：` 这种
#:     冒号前带空格的写法也进得来；
#:   - 正文用 `.*` 原样收下，内部空格、方括号、省略号一律不动。
LINE_RE = re.compile(
    r"^\[(\d{2}:\d{2}(?::\d{2})?)\]\s*(?:\*\*(?P<speaker>.+?)\*\*\s*:?\s*)?(?P<body>.*)$"
)

#: 时间戳那一格（不吃正文，只回答「这一行是不是一条转写」）。
STAMP_RE = re.compile(r"^\[(\d{2}:\d{2}(?::\d{2})?)\]")

#: 识别失败占位符的整行形状（`[[识别失败: 超时 20s]]`）。
#:
#: **它是一条裸行**：占位符里没有 `**` 包裹的说话人标签，解析出来 `speaker is None`。这条判据
#: 是「占位符不许被当成说话人」的回归网：一旦正则放宽到「方括号里的东西都算标签」，这句话就会
#: 变成纪要里的一个人名。
PLACEHOLDER_RE = re.compile(r"^\[\[[^\]]*\]\]$")


@dataclass(frozen=True)
class TranscriptLine:
    """一条解析出来的转写行。

    `stamp` 是**原样的时间戳字符串**（`HH:MM` 或 `HH:MM:SS`），这里不做归一化：产物里怎么写、
    下游引用时就要能逐字对回去。`speaker` 无标签时为 `None`（解析层把空字符串也收成 `None`，
    免得下游要同时判两种「没有」）。`body` 是正文，首尾空白已裁掉。
    """

    stamp: str
    speaker: str | None
    body: str


def parse_line(text: str) -> TranscriptLine | None:
    """解析一行：匹配不上返回 `None`。

    返回 `None` 的那几档是**有意**的：标题行（`# 会议纪要 — …`）、空行、V1 那种
    `<!-- chunk_01: lines 1-100 -->` 注释行，它们都是转写文件里真实存在的行，
    但它们不是一条发言，收进来会让行号区间表把标题算成一段内容。

    正文为空的行（`[21:32] `）照常收下：会中实时稿里确实会出现时间戳在、话还没落下来的行。
    """
    matched = LINE_RE.match(text)
    if matched is None:
        return None
    speaker = (matched.group("speaker") or "").strip()
    return TranscriptLine(
        stamp=matched.group(1),
        speaker=speaker or None,
        body=matched.group("body").strip(),
    )


def parse_lines(text: str) -> list[TranscriptLine]:
    """整份转写里**能解析的那些行**（跳过标题行、空行、注释行）。

    行号不在返回值里：`parse_lines()` 的序与 `splitlines()` 的序一致，要行号时自己数
    （`enumerate(..., start=1)`）。切块与行号区间表都不需要解析成功，它们按原始行切。
    """
    lines: list[TranscriptLine] = []
    for raw in text.splitlines():
        parsed = parse_line(raw)
        if parsed is not None:
            lines.append(parsed)
    return lines


def count_lines(text: str) -> int:
    """转写的总行数（`splitlines()` 口径，**空行计入**）。

    这个数是行号区间表与分段契约的**分母**：`total_lines` 与末段 `line_end` 都按它对齐，
    因此全项目只能有一种数法。`meeting/minutes/inputs.py`（phase2）报每一条输入的行数时
    走的也是这一个函数。
    """
    return len(text.splitlines())


__all__ = [
    "LINE_RE",
    "PLACEHOLDER_RE",
    "STAMP_RE",
    "TranscriptLine",
    "count_lines",
    "parse_line",
    "parse_lines",
]
