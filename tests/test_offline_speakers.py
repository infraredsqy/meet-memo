"""`meeting/offline/speakers.py` 的契约用例：每路编号规范化与汇总。

三条要盯的：

  - **首次出现顺序**决定 `S01` / `S02`（同一批输入反复跑给同一份编号）；
  - `qualified` 形如 `loopback:S01`，`source` 一直在场；
  - **跨路隔离**（R10）：同一批分段分别按 `mic` / `loopback` 编号，两路的 `S01` 各自存在、
    限定名不同，且**没有任何一条路径能把两路合并**——这条要落到具体断言上，
    只比「两路的类数一样」撑不起它。
"""

from __future__ import annotations

import ast
import inspect
import re

from meeting.offline import speakers as speakers_module
from meeting.offline.engine import OfflineSegment
from meeting.offline.speakers import number_speakers, speaker_summary


def seg(speaker: str, start: float, end: float, text: str = "话") -> OfflineSegment:
    """一段带引擎原始标签的分段。"""
    return OfflineSegment(start=start, end=end, speaker=speaker, text=text)


def _module_level_names(module) -> set[str]:
    """模块**源码里真正定义**的名字（import 进来的、`from ... import` 的都不算）。

    只数源码的顶层 `def` / `class` / 赋值目标：这一集合变了，说明模块里多了新的状态或新
    的入口，跨路隔离那条断言就该重新过一遍。
    """
    names: set[str] = set()
    for node in ast.parse(inspect.getsource(module)).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


#: 两路共用的同一批输入（同一个人在两路里都该拿到本路的 S01，互不相认）
SHARED_INPUT = [
    seg("S02", 0.0, 1.0, "第一句"),
    seg("S02", 1.0, 2.5, "第二句"),
    seg("S01", 3.0, 4.0, "换人"),
    seg("S07", 5.0, 5.5, "第三个人"),
    seg("S02", 6.0, 8.0, "回到第一个"),
]


# ---------------------------------------------------------------------------
# 一、首次出现顺序决定编号
# ---------------------------------------------------------------------------


def test_first_appearance_decides_the_numbering() -> None:
    """引擎标签 `S02` / `S01` / `S07` 按首次出现顺序规范化成 `S01` / `S02` / `S03`。"""
    spans = number_speakers("mic", SHARED_INPUT)

    assert [s.local for s in spans] == ["S01", "S01", "S02", "S03", "S01"]


def test_numbering_is_stable_across_runs() -> None:
    """同一批输入反复跑给出同一份编号（顺序确定、无随机）——「同一路内自洽」的可验形态。"""
    first = number_speakers("mic", SHARED_INPUT)
    second = number_speakers("mic", SHARED_INPUT)

    assert first == second


def test_qualified_name_carries_the_source() -> None:
    """`qualified` 一律 `${source}:${local}`，`source` 在每个区间上都在场。"""
    spans = number_speakers("loopback", SHARED_INPUT)

    assert [s.qualified for s in spans] == [
        "loopback:S01",
        "loopback:S01",
        "loopback:S02",
        "loopback:S03",
        "loopback:S01",
    ]
    assert {s.source for s in spans} == {"loopback"}
    for span in spans:
        assert span.qualified == f"{span.source}:{span.local}"


def test_empty_input_numbers_nothing() -> None:
    """没有分段就编不出号，也不该崩。"""
    assert number_speakers("mic", []) == []
    assert speaker_summary([]) == []


def test_spans_keep_the_interval_and_text() -> None:
    """区间与正文原样带过来（编号只换标签，不碰时间与正文）。"""
    spans = number_speakers("mic", SHARED_INPUT)

    assert [(s.start, s.end, s.text) for s in spans] == [
        (s.start, s.end, s.text) for s in SHARED_INPUT
    ]


# ---------------------------------------------------------------------------
# 二、跨路隔离：两路的 S01 各自存在，限定名不同
# ---------------------------------------------------------------------------


def test_same_input_on_two_sources_yields_two_distinct_qualified_names() -> None:
    """同一批分段分别按 `mic` / `loopback` 编号：两路的 `S01` 都在，限定名不同。"""
    mic = number_speakers("mic", SHARED_INPUT)
    loop = number_speakers("loopback", SHARED_INPUT)

    assert "S01" in {s.local for s in mic}
    assert "S01" in {s.local for s in loop}
    assert mic[0].local == loop[0].local == "S01"
    assert mic[0].qualified == "mic:S01"
    assert loop[0].qualified == "loopback:S01"
    assert mic[0].qualified != loop[0].qualified


def test_the_two_sources_share_no_qualified_name_at_all() -> None:
    """两路的限定名**集合完全不相交**：不存在一个同时属于两路的编号。"""
    mic = {s.qualified for s in number_speakers("mic", SHARED_INPUT)}
    loop = {s.qualified for s in number_speakers("loopback", SHARED_INPUT)}

    assert mic == {"mic:S01", "mic:S02", "mic:S03"}
    assert loop == {"loopback:S01", "loopback:S02", "loopback:S03"}
    assert mic & loop == set()


def test_summary_rows_are_scoped_to_one_source() -> None:
    """汇总表里每一行都带 `source`，两路各汇总一份，行数与秒数互不掺杂。"""
    mic = speaker_summary(number_speakers("mic", SHARED_INPUT))
    loop = speaker_summary(number_speakers("loopback", SHARED_INPUT))

    assert [row["qualified"] for row in mic] == ["mic:S01", "mic:S02", "mic:S03"]
    assert [row["qualified"] for row in loop] == ["loopback:S01", "loopback:S02", "loopback:S03"]
    assert {row["source"] for row in mic} == {"mic"}
    assert {row["source"] for row in loop} == {"loopback"}
    assert [row["seconds"] for row in mic] == [row["seconds"] for row in loop]


def test_no_code_path_can_merge_the_two_sources() -> None:
    """**没有任何一条路径能把两路合并**：编号原料只有「本路已见标签 → 本路编号」这一张表。

    这条落到三处具体断言上（只比「两路的类数一样」撑不起跨路隔离）：

      1. 编号函数每次都从**空表**起步——同一个标签在两路里都落到 `S01`，
         说明表不跨调用共享；
      2. 分段的 `speaker` 是唯一的输入，`source` **只进限定名**、不进编号，
         换 `source` 不改变本路的 `local`；
      3. `speakers.py` 的模块级名字只有一张正则、两个函数与一个数据类：
         没有跨调用的可变状态（累积表、缓存），也没有跨 `source` 的比较写法。
    """
    mic_first = number_speakers("mic", SHARED_INPUT)
    loop_first = number_speakers("loopback", SHARED_INPUT)
    mic_again = number_speakers("mic", SHARED_INPUT)

    # 1. 表不跨调用共享：两路都把同一个引擎标签编成 S01，且再跑一次不受前两次影响
    assert mic_first[0].local == loop_first[0].local == "S01"
    assert mic_again == mic_first

    # 2. source 只进限定名：换 source 不改变本路编号，只改变限定名
    assert [s.local for s in mic_first] == [s.local for s in loop_first]
    assert [s.qualified for s in mic_first] != [s.qualified for s in loop_first]

    # 3. 模块级名字逐个点名（多一个名字就要在这里说清它是干什么的）
    assert sorted(_module_level_names(speakers_module)) == [
        "SpeakerSpan",
        "_LABEL_RE",
        "is_engine_label",
        "number_speakers",
        "speaker_summary",
    ]

    # 4. 源码里没有跨 source 的比较写法（`local ==` / `source !=` 之类）
    source = re.sub(r"#.*", "", inspect.getsource(speakers_module))
    for forbidden in ("local ==", "local !=", "qualified ==", "source ==", "source !="):
        assert forbidden not in source, f"出现可疑的跨 source 比较：{forbidden}"


# ---------------------------------------------------------------------------
# 三、汇总：段数与秒数加总正确
# ---------------------------------------------------------------------------


def test_summary_counts_segments_and_seconds_per_speaker() -> None:
    """每类的段数与该类所有区间秒数之和都对得上（段间静默不计入）。"""
    rows = speaker_summary(number_speakers("mic", SHARED_INPUT))

    assert rows == [
        {"source": "mic", "local": "S01", "qualified": "mic:S01", "segments": 3, "seconds": 4.5},
        {"source": "mic", "local": "S02", "qualified": "mic:S02", "segments": 1, "seconds": 1.0},
        {"source": "mic", "local": "S03", "qualified": "mic:S03", "segments": 1, "seconds": 0.5},
    ]
    assert sum(row["seconds"] for row in rows) == 6.0
    assert sum(row["segments"] for row in rows) == len(SHARED_INPUT)


def test_summary_order_follows_the_numbering_order() -> None:
    """汇总行按限定名首次出现的顺序排（与编号顺序一致，便于人读）。"""
    spans = [
        seg("S05", 0.0, 1.0),
        seg("S01", 1.0, 2.0),
        seg("S05", 3.0, 4.0),
    ]

    rows = speaker_summary(number_speakers("loopback", spans))

    assert [row["qualified"] for row in rows] == ["loopback:S01", "loopback:S02"]
    assert [row["segments"] for row in rows] == [2, 1]


def test_summary_ignores_a_reversed_interval() -> None:
    """区间倒挂（end < start）时那一笔算 0 秒，不把秒数算成负的。"""
    spans = number_speakers("mic", [seg("S01", 5.0, 3.0), seg("S01", 1.0, 2.0)])

    rows = speaker_summary(spans)

    assert rows == [
        {"source": "mic", "local": "S01", "qualified": "mic:S01", "segments": 2, "seconds": 1.0},
    ]
