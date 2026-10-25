"""`meeting/minutes/contract.py` 的契约用例：按 source 各判一遍的连续性、读盘三档错误。

盯的是五件事：

  - 连续性**按 `source` 各判一遍**（两路的行号空间互不相干，拼成一条链去判是错的）；
  - 失败抛的是自己的错误类 `SegmentationContractError`，消息里点得出 source 与段号；
  - 段数**按 source 各自** ≤ `max_segments`；
  - 方案的落点在 `working/` 那一层（本包源码里没有一处说它落在某条 source 的子目录下）；
  - 新包在**全新解释器**里 import 之后 `sys.modules` 里没有重依赖。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from tests import _helpers as helpers

from meeting.minutes import __main__ as minutes_main
from meeting.minutes.contract import (
    DEFAULT_MAX_SEGMENTS,
    SCHEMA_VERSION,
    SEGMENTATION_NAME,
    SegmentationContractError,
    expected_lines_from_chunks,
    expected_lines_from_inputs,
    load_segmentation,
    validate_segmentation,
)

#: 本包源码的目录（`working/<source>/` 那一档错口径不许在里面出现，见下面那条用例）
_MINUTES_PACKAGE = Path(minutes_main.__file__).resolve().parent

#: 两条输入各自的真实行数（切块那一步数出来的）
EXPECTED = {"loopback": 20, "mic": 12}


def _segment(segment_id: int, source: str, start: int, end: int, **extra) -> dict:
    """造一段（必填字段齐，`title` 照 V1 的形状给一句短文）。"""
    segment = {
        "id": segment_id,
        "title": f"第 {segment_id} 段",
        "source": source,
        "line_start": start,
        "line_end": end,
        "chunks": extra.pop("chunks", [1]),
        "key_topics": extra.pop("key_topics", ["话题"]),
        "is_off_topic": extra.pop("is_off_topic", False),
        "summary": extra.pop("summary", "这一段讲了点什么。"),
    }
    segment.update(extra)
    return segment


def _plan(segments: list[dict], **extra) -> dict:
    """造一份方案（顶层字段照 V1 的 `SEGMENT_SCHEMA`）。"""
    payload = {"schema_version": SCHEMA_VERSION, "segments": segments}
    payload.update(extra)
    return payload


@pytest.fixture()
def two_source_plan() -> dict:
    """两条输入各两段、各自从 1 起、末段各自盖到本条的总行数。"""
    return _plan(
        [
            _segment(1, "loopback", 1, 8),
            _segment(2, "loopback", 9, 20),
            _segment(3, "mic", 1, 5),
            _segment(4, "mic", 6, 12),
        ]
    )


# ---------------------------------------------------------------------------
# 一、通过的那一档
# ---------------------------------------------------------------------------


def test_two_sources_pass_with_per_source_continuity(two_source_plan: dict) -> None:
    """两条输入各判各的：换 source 时连续性重新从 1 起算，照样通过。"""
    assert validate_segmentation(two_source_plan, expected_lines=EXPECTED) is True


def test_segments_may_come_in_a_shuffled_order(two_source_plan: dict) -> None:
    """段在文件里乱序也能过（校验按 `line_start` 排序后再判连续性）。"""
    shuffled = _plan(list(reversed(two_source_plan["segments"])))

    assert validate_segmentation(shuffled, expected_lines=EXPECTED) is True


def test_single_source_plan_passes() -> None:
    """只喂一条时方案只有一条 source，照样过（降级路径）。"""
    payload = _plan([_segment(1, "loopback", 1, 20)])

    assert validate_segmentation(payload, expected_lines={"loopback": 20}) is True


def test_declared_total_lines_is_checked_when_no_expected_lines_given() -> None:
    """没有独立事实可核时，退回方案自己声明的 `total_lines`。"""
    payload = _plan([_segment(1, "loopback", 1, 18)], total_lines=20)

    with pytest.raises(SegmentationContractError, match="末段盖到第 18 行"):
        validate_segmentation(payload)


def test_optional_fields_are_free_to_omit() -> None:
    """可选字段（`chunks` / `key_topics` / `is_off_topic` / `summary`）不在也算过。"""
    payload = _plan(
        [
            {
                "id": 1,
                "title": "只有必填字段的一段",
                "source": "mic",
                "line_start": 1,
                "line_end": 12,
            }
        ]
    )

    assert validate_segmentation(payload, expected_lines={"mic": 12}) is True


# ---------------------------------------------------------------------------
# 二、失败的那几档：消息里点得出 source 与段号
# ---------------------------------------------------------------------------


def test_a_gap_inside_one_source_is_rejected(two_source_plan: dict) -> None:
    """漏行：`loopback` 的第 9 行没人盖，消息里点出 source 与段号。"""
    broken = _plan([_segment(1, "loopback", 1, 8), _segment(2, "loopback", 10, 20)])

    with pytest.raises(SegmentationContractError) as excinfo:
        validate_segmentation(broken, expected_lines=EXPECTED)

    message = str(excinfo.value)
    assert "source 'loopback' 段 2" in message
    assert "应当是 9" in message


def test_an_overlap_inside_one_source_is_rejected() -> None:
    """重叠：第 2 段从第 5 行起，与第 1 段盖到的第 8 行撞上。"""
    broken = _plan([_segment(1, "mic", 1, 8), _segment(2, "mic", 5, 12)])

    with pytest.raises(SegmentationContractError, match="漏行或重叠"):
        validate_segmentation(broken, expected_lines={"mic": 12})


def test_continuity_is_judged_per_source_not_across_sources() -> None:
    """两路各一段、各自从 1 起，各自盖到本条的总行数 —— 这**是**对的。

    把两路拼成一条链去判的实现会在这里判红（`mic` 的第 1 段接在 `loopback` 末段之后），
    这条用例就是那个错法的回归网。
    """
    payload = _plan([_segment(1, "loopback", 1, 20), _segment(2, "mic", 1, 12)])

    assert validate_segmentation(payload, expected_lines=EXPECTED) is True


def test_a_broken_second_source_names_that_source(two_source_plan: dict) -> None:
    """出错的是第二条 source 时，消息里点的是它，`loopback` 的名字不出现在里面。"""
    broken = _plan(
        [
            _segment(1, "loopback", 1, 20),
            _segment(2, "mic", 1, 5),
            _segment(3, "mic", 7, 12),
        ]
    )

    with pytest.raises(SegmentationContractError) as excinfo:
        validate_segmentation(broken, expected_lines=EXPECTED)

    message = str(excinfo.value)
    assert "source 'mic' 段 3" in message
    assert "loopback" not in message


def test_a_short_last_segment_is_rejected() -> None:
    """末段没盖到最后一行：报出少了几行。"""
    payload = _plan([_segment(1, "loopback", 1, 17)])

    with pytest.raises(SegmentationContractError, match="少了 3 行"):
        validate_segmentation(payload, expected_lines={"loopback": 20})


def test_a_long_last_segment_is_rejected() -> None:
    """末段盖过了总行数：报出多了几行。"""
    payload = _plan([_segment(1, "loopback", 1, 25)])

    with pytest.raises(SegmentationContractError, match="多了 5 行"):
        validate_segmentation(payload, expected_lines={"loopback": 20})


def test_a_plan_that_starts_late_is_rejected() -> None:
    """第一段从第 2 行起：第 1 行没人盖，报错里说得出「上一条盖到第 0 行」。"""
    payload = _plan([_segment(1, "loopback", 2, 20)])

    with pytest.raises(SegmentationContractError, match="应当是 1"):
        validate_segmentation(payload, expected_lines={"loopback": 20})


def test_too_many_segments_is_rejected_per_source() -> None:
    """段数**按 source 各自**判：`mic` 有 3 段、上限 2，报错里说的是 `mic`。"""
    payload = _plan(
        [
            _segment(1, "loopback", 1, 20),
            _segment(2, "mic", 1, 4),
            _segment(3, "mic", 5, 8),
            _segment(4, "mic", 9, 12),
        ]
    )

    with pytest.raises(SegmentationContractError, match="source 'mic' 有 3 段，超过上限 2"):
        validate_segmentation(payload, expected_lines=EXPECTED, max_segments=2)


def test_a_backwards_range_is_rejected() -> None:
    """`line_end < line_start`：区间倒挂。"""
    payload = _plan([_segment(1, "mic", 8, 3)])

    with pytest.raises(SegmentationContractError, match="行号区间倒挂"):
        validate_segmentation(payload, expected_lines={"mic": 12})


@pytest.mark.parametrize("field", ["id", "title", "source", "line_start", "line_end"])
def test_every_required_segment_field_is_required(field: str) -> None:
    """五处必填字段逐个缺一次，每次都报缺的是哪一个。"""
    segment = _segment(1, "mic", 1, 12)
    del segment[field]

    with pytest.raises(SegmentationContractError, match=f"缺字段.*'{field}'"):
        validate_segmentation(_plan([segment]), expected_lines={"mic": 12})


def test_an_empty_title_is_rejected() -> None:
    """`title` 是空串（或只有空白）不算填了。"""
    payload = _plan([_segment(1, "mic", 1, 12, title="   ")])

    with pytest.raises(SegmentationContractError, match="title 要是非空字符串"):
        validate_segmentation(payload, expected_lines={"mic": 12})


def test_a_segment_without_a_source_is_rejected() -> None:
    """段里没有 `source`：一份方案要说清每段出自哪一路。"""
    segment = _segment(1, "mic", 1, 12)
    del segment["source"]

    with pytest.raises(SegmentationContractError, match=r"缺字段 \['source'\]"):
        validate_segmentation(_plan([segment]), expected_lines={"mic": 12})


def test_a_non_string_source_is_rejected() -> None:
    """`source` 在、但它是空串或非字符串：同样拒收（分组读不出它是哪一路）。"""
    payload = _plan([_segment(1, "mic", 1, 12)])
    payload["segments"][0]["source"] = ""

    with pytest.raises(SegmentationContractError, match="source 要是非空字符串"):
        validate_segmentation(payload, expected_lines={"mic": 12})


def test_a_mismatched_source_is_rejected() -> None:
    """段写的 `source` 不在输入清单里，报错里点得出是它（分组按这一格，写错了就串路）。"""
    payload = _plan([_segment(1, "mic", 1, 12)])
    payload["segments"][0]["source"] = "loopback"

    with pytest.raises(SegmentationContractError, match="source 'loopback' 不在输入清单里"):
        validate_segmentation(payload, expected_lines={"mic": 12})


def test_a_non_integer_chunk_reference_is_rejected() -> None:
    """`chunks` 里的每一项要是整数（`True` 这种 `bool` 也归到拒收那一档）。"""
    payload = _plan([_segment(1, "mic", 1, 12, chunks=[1, "2"])])

    with pytest.raises(SegmentationContractError, match="chunks 里每一项要是整数"):
        validate_segmentation(payload, expected_lines={"mic": 12})

    truthy = _plan([_segment(1, "mic", 1, 12, chunks=[True])])
    with pytest.raises(SegmentationContractError, match="chunks 里每一项要是整数"):
        validate_segmentation(truthy, expected_lines={"mic": 12})


def test_chunks_must_be_a_list() -> None:
    """`chunks` 在的时候要是列表。"""
    payload = _plan([_segment(1, "mic", 1, 12, chunks=1)])

    with pytest.raises(SegmentationContractError, match="chunks 要是列表"):
        validate_segmentation(payload, expected_lines={"mic": 12})


def test_an_unknown_source_is_rejected() -> None:
    """方案里出现了输入清单里没有的 source。"""
    payload = _plan([_segment(1, "speaker", 1, 12)])

    with pytest.raises(SegmentationContractError, match="不在输入清单里"):
        validate_segmentation(payload, expected_lines={"mic": 12})


def test_a_source_without_any_segment_is_rejected() -> None:
    """输入清单里有、方案里一段都没有的那条 source 要点名。"""
    payload = _plan([_segment(1, "loopback", 1, 20)])

    with pytest.raises(SegmentationContractError, match=r"输入清单里的 \['mic'\]"):
        validate_segmentation(payload, expected_lines=EXPECTED)


def test_duplicate_segment_ids_inside_one_source_are_rejected() -> None:
    """同一条 source 里两段用了同一个 `id`。"""
    payload = _plan([_segment(1, "mic", 1, 4), _segment(1, "mic", 5, 12)])

    with pytest.raises(SegmentationContractError, match="段 id 1 重复"):
        validate_segmentation(payload, expected_lines={"mic": 12})


def test_an_unknown_schema_version_is_rejected() -> None:
    """读方不认识的 `schema_version` 一律拒收，任何情况下都不许猜。"""
    payload = _plan([_segment(1, "mic", 1, 12)], schema_version=2)

    with pytest.raises(SegmentationContractError, match="schema_version 2 读方不认识"):
        validate_segmentation(payload, expected_lines={"mic": 12})


def test_an_empty_segment_list_is_rejected() -> None:
    """一段都没有的方案不算方案。"""
    with pytest.raises(SegmentationContractError, match="至少要有一段"):
        validate_segmentation(_plan([]), expected_lines={"mic": 12})


def test_a_non_object_payload_is_rejected() -> None:
    """顶层不是对象 / `segments` 不是列表，两档都当场拦下。"""
    with pytest.raises(SegmentationContractError, match="segments 要是列表"):
        validate_segmentation({"schema_version": 1, "segments": "nope"})  # type: ignore[arg-type]


def test_a_bad_max_segments_is_rejected() -> None:
    """`max_segments` 要是 ≥1 的整数。"""
    with pytest.raises(SegmentationContractError, match="max_segments"):
        validate_segmentation(_plan([_segment(1, "mic", 1, 12)]), max_segments=0)


# ---------------------------------------------------------------------------
# 二、`total_lines` 的两种形状：整数只服务单 source，多 source 要逐 source 的对象
# ---------------------------------------------------------------------------


def test_total_lines_as_a_per_source_object_covers_unequal_sources() -> None:
    """双 source、行数不同、`total_lines` 是**对象**且逐条正确 → 过。

    这就是 `session-dual`（loopback 120 行 / mic 63 行）那一档：两路的行数空间各算各的，
    顶层一格按 source 声明，谁也不会被另一条的行数判错。
    """
    payload = _plan(
        [
            _segment(1, "loopback", 1, 8),
            _segment(2, "loopback", 9, 20),
            _segment(3, "mic", 1, 12),
        ],
        total_lines={"loopback": 20, "mic": 12},
    )

    assert validate_segmentation(payload) is True


def test_total_lines_object_missing_a_source_is_rejected() -> None:
    """对象里**漏了**一条 source → 报错（不许悄悄退化成「那一条不核」）。"""
    payload = _plan(
        [
            _segment(1, "loopback", 1, 20),
            _segment(2, "mic", 1, 12),
        ],
        total_lines={"loopback": 20},
    )

    with pytest.raises(SegmentationContractError) as excinfo:
        validate_segmentation(payload)

    message = str(excinfo.value)
    assert "total_lines 里少了 ['mic']" in message
    assert "每一条 source 都在里面" in message


def test_total_lines_as_an_integer_on_a_multi_source_plan_is_rejected() -> None:
    """多 source 场次上写整数 → 报错，并点名说清该怎么写。

    这一格此前会被当成**每一条** source 的末段期望用：填 120 判 mic 多了、填 63 判 loopback
    少了，只有不写才过——而「不写」那一支直接 `continue`，连「每行都落进某一段」也一起不核。
    """
    payload = _plan(
        [_segment(1, "loopback", 1, 20), _segment(2, "mic", 1, 12)],
        total_lines=20,
    )

    with pytest.raises(SegmentationContractError) as excinfo:
        validate_segmentation(payload)

    message = str(excinfo.value)
    assert "total_lines 是整数 20" in message
    assert "覆盖了 2 条 source" in message
    assert "逐 source 的对象" in message and '"loopback": 120, "mic": 63' in message
    assert "expected_lines" in message


def test_a_single_source_plan_with_a_correct_integer_total_lines_passes() -> None:
    """单 source + 整数 `total_lines` 且对得上 → 照旧过（单路场次照样能自证）。"""
    payload = _plan([_segment(1, "loopback", 1, 20)], total_lines=20)

    assert validate_segmentation(payload) is True


def test_a_single_source_plan_with_a_wrong_integer_total_lines_is_rejected() -> None:
    """单 source + 整数 `total_lines` 但对不上 → 报「少了 N 行」。"""
    payload = _plan([_segment(1, "loopback", 1, 17)], total_lines=20)

    with pytest.raises(SegmentationContractError, match="少了 3 行"):
        validate_segmentation(payload)


def test_expected_lines_wins_over_a_contradicting_total_lines() -> None:
    """`expected_lines` 在场时它是唯一权威：故意让两者矛盾，仍以 `expected_lines` 为准。

    这里两条 source 的末段分别盖到 20 / 12，与 `expected_lines` 一致；`total_lines` 那一格
    故意写成反过来的 `{"loopback": 12, "mic": 20}`。校验通过说明比对用的是独立事实，
    不是方案自述。
    """
    payload = _plan(
        [_segment(1, "loopback", 1, 20), _segment(2, "mic", 1, 12)],
        total_lines={"loopback": 12, "mic": 20},
    )

    assert validate_segmentation(payload, expected_lines=EXPECTED) is True


def test_expected_lines_still_rejects_a_bad_total_lines_shape() -> None:
    """`total_lines` 这一档即使不参与比对，**坏形状照样拒收**（形状是契约的一部分）。"""
    payload = _plan([_segment(1, "loopback", 1, 20)], total_lines="20 行")

    with pytest.raises(SegmentationContractError, match="total_lines 要是整数"):
        validate_segmentation(payload, expected_lines=EXPECTED)


@pytest.mark.parametrize(
    ("declared", "match"),
    [
        ("20", "total_lines 要是整数"),
        ([20, 12], "total_lines 要是整数"),
        (-20, "≥1 的整数"),
        ({"loopback": "20"}, r"total_lines 的 'loopback' 那一格"),
    ],
)
def test_a_bad_total_lines_shape_is_rejected(declared: object, match: str) -> None:
    """坏形状四种逐条拒收：字符串 / 列表 / 负数 / 成员不是整数，消息里说清是哪一格。"""
    payload = _plan([_segment(1, "loopback", 1, 20)], total_lines=declared)

    with pytest.raises(SegmentationContractError, match=match):
        validate_segmentation(payload)


def test_a_one_line_gap_is_caught_with_a_per_source_total_lines_object() -> None:
    """**覆盖完整性**的回归网：没有 `expected_lines`、`total_lines` 是对象这一档下，
    某条 source 少盖一行（末段 `line_end` 差 1）必须被抓住。

    旧实现在这一档上写整数会被判成「多 source 没有合法取值」，写不出整数就整段 `continue`
    —— 这一条就是那个「静默不核」的回归网。
    """
    payload = _plan(
        [_segment(1, "loopback", 1, 20), _segment(2, "mic", 1, 11)],
        total_lines={"loopback": 20, "mic": 12},
    )

    with pytest.raises(SegmentationContractError) as excinfo:
        validate_segmentation(payload)

    message = str(excinfo.value)
    assert "source 'mic' 的末段盖到第 11 行" in message
    assert "少了 1 行" in message


def test_a_source_with_no_declared_total_lines_at_all_is_not_silently_skipped() -> None:
    """顶层整格缺席时只剩「按 source 各判连续性」这一层，末段核对确实无从做起。

    这一档放过的只有单 source 场次：多 source 上写整数会当场报错（见上面那条），
    因此「静默放过多 source」这条路已经堵上。
    """
    payload = _plan([_segment(1, "loopback", 1, 12)])

    assert validate_segmentation(payload) is True


# ---------------------------------------------------------------------------
# 三、`inputs.json` 那一格：逐 source 的真实行数从哪来
# ---------------------------------------------------------------------------


def test_expected_lines_from_inputs_uses_the_live_transcript() -> None:
    """划段的分母是**会中稿**那一份：两份都在时取会中稿的行数，缺席时退回会后稿。

    这一条与分段方案契约同一口径：分母按这一路在场那份稿来（两份都在按会中稿，
    只有会后稿的场次按会后稿）：会中稿每一路都有（离线那一趟是可选的），
    两份稿的行号空间又各算各的。缺 `lines` 或两份都不在的那一条
    整个不出现（不猜）。
    """
    times = 13
    document = {
        "schema_version": 1,
        "mode": "both",
        "sources": [
            {
                "source": "loopback",
                "kind": "both",
                "live": {"file": "meeting_transcript_loopback.md", "lines": times},
                "offline": {"file": "meeting_offline_transcript_loopback.md", "lines": 120},
            },
            {
                "source": "mic",
                "kind": "live_only",
                "live": {"file": "meeting_transcript_mic.md", "lines": 63},
            },
            {
                "source": "only_offline",
                "kind": "offline_only",
                "offline": {"file": "meeting_offline_transcript_only_offline.md", "lines": 44},
            },
            {"source": "extra", "kind": "live_only", "live": {"file": "x.md"}},
        ],
    }

    assert expected_lines_from_inputs(document) == {
        "loopback": times,
        "mic": 63,
        "only_offline": 44,
    }


def test_expected_lines_from_inputs_rejects_a_broken_source_list() -> None:
    """清单的 `sources` 不是列表 / 某一条不是对象 → 当场报错（不许猜）。"""
    with pytest.raises(SegmentationContractError, match="sources 要是列表"):
        expected_lines_from_inputs({"sources": "loopback"})

    with pytest.raises(SegmentationContractError, match="sources 第 1 条要是对象"):
        expected_lines_from_inputs({"sources": [1]})


def test_the_check_subcommand_checks_coverage_against_inputs_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`check` 找到方案旁边的 `inputs.json` 时，按逐 source 的真实行数核覆盖完整性。

    这一条同时锁住 `meeting.minutes check` 的行为：多 source 场次上「少盖一行」在这里
    必须报错，而不是因为方案没写 `total_lines` 就一路放过。
    """
    working = tmp_path / "working"
    working.mkdir()
    path = working / SEGMENTATION_NAME
    path.write_text(
        json.dumps(
            _plan([_segment(1, "loopback", 1, 20), _segment(2, "mic", 1, 12)]),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (working / "inputs.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "both",
                "sources": [
                    {
                        "source": "loopback",
                        "kind": "live_only",
                        "live": {"file": "meeting_transcript_loopback.md", "lines": 20},
                    },
                    {
                        "source": "mic",
                        "kind": "live_only",
                        "live": {"file": "meeting_transcript_mic.md", "lines": 12},
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert minutes_main._check_main([str(path)]) == 0
    assert "契约通过：2 段，覆盖 ['loopback', 'mic']" in capsys.readouterr().out

    # 少盖一行：同一份方案，`mic` 的末段差 1 行。清单给出的事实因此把它抓住。
    short = _plan([_segment(1, "loopback", 1, 20), _segment(2, "mic", 1, 11)])
    path.write_text(json.dumps(short, ensure_ascii=False), encoding="utf-8")

    assert minutes_main._check_main([str(path)]) == 1
    assert "少了 1 行" in capsys.readouterr().err


def test_the_check_subcommand_falls_back_when_inputs_json_is_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """没有 `inputs.json` 时退回方案自述那一档：单 source + 整数照样自证。"""
    path = tmp_path / SEGMENTATION_NAME
    path.write_text(
        json.dumps(_plan([_segment(1, "loopback", 1, 20)], total_lines=20), ensure_ascii=False),
        encoding="utf-8",
    )

    assert minutes_main._check_main([str(path)]) == 0
    assert "按方案自己声明的 total_lines" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 四、读盘入口：三种能分辨的错误
# ---------------------------------------------------------------------------


def test_load_segmentation_reads_a_valid_file(tmp_path: Path) -> None:
    """读得出来就返回那份 `dict`。"""
    path = tmp_path / "segmentation.json"
    payload = _plan([_segment(1, "mic", 1, 12)])
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded = load_segmentation(path, expected_lines={"mic": 12})

    assert loaded["schema_version"] == SCHEMA_VERSION
    assert loaded["segments"][0]["title"] == "第 1 段"


def test_load_segmentation_reports_a_missing_file(tmp_path: Path) -> None:
    """文件不在 → `FileNotFoundError`（调用方据此报「方案还没交回来」）。"""
    with pytest.raises(FileNotFoundError):
        load_segmentation(tmp_path / SEGMENTATION_NAME, expected_lines={"mic": 12})


def test_load_segmentation_reports_broken_json(tmp_path: Path) -> None:
    """JSON 坏掉 → 自己的错误类，消息里带 `JSON` 两个字。"""
    path = tmp_path / "segmentation.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")

    with pytest.raises(SegmentationContractError, match="JSON 解析失败"):
        load_segmentation(path, expected_lines={"mic": 12})


def test_load_segmentation_reports_a_non_object_top_level(tmp_path: Path) -> None:
    """顶层是数组 → 同样是自己的错误类，消息里说得清是「顶层要是对象」。"""
    path = tmp_path / "segmentation.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(SegmentationContractError, match="顶层要是对象"):
        load_segmentation(path, expected_lines={"mic": 12})


def test_load_segmentation_reports_a_contract_failure(tmp_path: Path) -> None:
    """契约不过 → 自己的错误类，消息里点得出 source 与段号。"""
    path = tmp_path / "segmentation.json"
    broken = _plan([_segment(1, "mic", 1, 5), _segment(2, "mic", 7, 12)])
    path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SegmentationContractError, match="source 'mic' 段 2"):
        load_segmentation(path, expected_lines={"mic": 12})


def test_segmentation_lands_in_the_working_directory(tmp_path: Path) -> None:
    """方案的落点就是 `working/` 那一层（`working/segmentation.json`，**一份**）。

    `SEGMENTATION_NAME` 与那一层是各处的共同口径（契约校验、`check` 入口），
    所以这一条把它们一起钉住；`_inputs_near` 另外三条用例盖两种调用写法与「找不到清单」。
    """
    assert SEGMENTATION_NAME == "segmentation.json"

    working = tmp_path / "working"
    working.mkdir()
    (working / "inputs.json").write_text("{}", encoding="utf-8")

    assert minutes_main._inputs_near(working / SEGMENTATION_NAME) == (working / "inputs.json")


def test_the_check_subcommand_also_recognizes_a_nested_plan_path(tmp_path: Path) -> None:
    """某条 source 子目录下的那一种**调用写法**照样认：先看方案那一层，再看 `working/`。"""
    working = tmp_path / "working"
    nested = working / "loopback"
    nested.mkdir(parents=True)
    (working / "inputs.json").write_text("{}", encoding="utf-8")

    assert minutes_main._inputs_near(nested / SEGMENTATION_NAME) == (working / "inputs.json")


def test_the_check_subcommand_reports_no_inputs_json_when_there_is_none(
    tmp_path: Path,
) -> None:
    """两层都没有 `inputs.json` → `None`（`check` 据此退回方案自述那一档），不猜路径。"""
    working = tmp_path / "working"
    working.mkdir()

    assert minutes_main._inputs_near(working / SEGMENTATION_NAME) is None


#: 方案那一份被写成 `working/<source>/segmentation.json` 的样子。命中要同时满足两件事：
#: 一行里**在讲方案**（`SEGMENTATION_NAME` 或它的字面 `segmentation.json` 或「方案」两个字），
#: 并且紧跟其后就是那条带 source 一格的路径。
#:
#: 一条不算：切块的块与区间表本来就落 `working/<source>/`（那是真事，不在这条用例的范围里）。
#: 这个形状是本包此前的错口径（`contract.py` 一个算路径的帮手 + 两处注释长这样），按它来盯。
_PHANTOM_PATH = re.compile(
    r"(SEGMENTATION_NAME|segmentation\.json|方案)[^\n]{0,40}`?working[/\\]"
    r"(<source>|\{source\})[^`]{0,60}segmentation"
)


def test_no_module_advertises_a_segmentation_plan_below_a_source_directory() -> None:
    """本包的源码里没有一个字把 `working/<source>/` 说成方案那一份的落点。

    这条钉的是那个坑本身：`contract.py` 里曾有一个算路径的帮手 + 两处注释把它写成 spec 钉死
    的落点，而契约层与 `check` 都只把 `working/` 这一份当落点，照着它写就写进一个没人读的
    地方。落点口径改了，这里就先报错。
    """
    hits = [
        f"{path.name}:{number}: {line.strip()}"
        for path in sorted(_MINUTES_PACKAGE.glob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _PHANTOM_PATH.search(line)
    ]

    assert hits == [], f"这些行又把方案写在某条 source 的子目录下了：{hits}"


def test_expected_lines_from_chunks_reads_the_chunk_reports(tmp_path: Path) -> None:
    """真实行数只有一种取法：切块结果里的 `total_lines`（**逐份稿各答一格**）。"""
    from meeting.minutes.chunk import chunk_session, discover_inputs

    session = tmp_path / "250919"
    session.mkdir()
    (session / "meeting_transcript_loopback.md").write_text(
        "\n".join(f"[21:{minute:02d}] 第 {minute} 句。" for minute in range(13)) + "\n",
        encoding="utf-8",
    )
    (session / "meeting_offline_transcript_loopback.md").write_text(
        "\n".join(f"[21:{minute:02d}] **S01**: 第 {minute} 句。" for minute in range(9)) + "\n",
        encoding="utf-8",
    )
    reports = chunk_session(session, discover_inputs(session), chunk_lines=10)

    assert expected_lines_from_chunks(reports) == {"loopback": {"live": 13, "offline": 9}}


def test_expected_lines_from_chunks_rejects_a_report_without_total_lines() -> None:
    """拿不到总行数时当场报错（不许拿一个猜的数去校验契约）。"""
    with pytest.raises(SegmentationContractError, match="没有 total_lines"):
        expected_lines_from_chunks({"mic": {"live": object()}})

    with pytest.raises(SegmentationContractError, match="该是逐份稿的对象"):
        expected_lines_from_chunks({"mic": object()})


# ---------------------------------------------------------------------------
# 四、全新解释器里的 import 链：重依赖一个都不许被拉进来
# ---------------------------------------------------------------------------

#: 在子进程里跑的一行：import 整包与四个模块，再报出**重依赖**
_IMPORT_CHAIN_CODE = (
    "import sys; "
    "import meeting.minutes.lines, meeting.minutes.chunk; "
    "import meeting.minutes.contract, meeting.minutes.prompt; "
    "heavy = [m for m in ('torch', 'transformers', 'numpy', 'av', 'webrtcvad', "
    "'starlette', 'sounddevice') if m in sys.modules]; "
    "print('heavy=' + ','.join(heavy)); "
    "print('index=' + meeting.minutes.chunk.INDEX_NAME); "
    "print('mode=' + str(meeting.minutes.chunk.mode_for(())))"
)

#: 全新解释器里 `import meeting.minutes`（包标记）那一档
_PACKAGE_IMPORT_CODE = (
    "import sys, meeting.minutes as p; "
    "heavy = [m for m in ('torch', 'transformers', 'numpy', 'av', 'webrtcvad', "
    "'starlette', 'sounddevice') if m in sys.modules]; "
    "print('heavy=' + ','.join(heavy)); "
    "print('doc=' + str(bool(p.__doc__)))"
)


def _run_fresh(code: str) -> str:
    """在**全新解释器**里跑一段代码，返回它的全部输出。

    必须换一个全新解释器：本进程里 `numpy` / `starlette` / `torch` 早被别的用例与
    `tests/conftest.py` 的兜底夹具拉进 `sys.modules` 了，同进程断言等于没验。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(helpers.REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(helpers.REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    output = (result.stdout or "") + (result.stderr or "")
    assert result.returncode == 0, f"全新解释器里 import 失败：\n{output}"
    return result.stdout or ""


def test_import_chain_pulls_no_heavy_dependency() -> None:
    """四个模块一起 import，`sys.modules` 里没有一件重依赖。"""
    lines = _run_fresh(_IMPORT_CHAIN_CODE).strip().splitlines()

    assert lines[0] == "heavy=", f"`meeting.minutes` 把重依赖拉进了 sys.modules：{lines[0]!r}"
    assert lines[1] == "index=00_index.md"
    assert lines[2] == "mode=None"


def test_package_import_pulls_no_heavy_dependency() -> None:
    """`import meeting.minutes`（包标记）同样只拉标准库与包内模块。"""
    lines = _run_fresh(_PACKAGE_IMPORT_CODE).strip().splitlines()

    assert lines[0] == "heavy="
    assert lines[1] == "doc=True"
