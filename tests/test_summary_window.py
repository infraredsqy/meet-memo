"""`meeting/summary/window.py` 的契约用例：缓冲、按时间戳切窗口、装配成文本。

盯的是四件事（对应设计口径的完成判定与给复审的自检项）：

  - **窗口按时间戳切**：`5m` 取的是「末行 `ts` 往前 300 秒」那一段连续尾部，
    同一个 `ts` 上挤着的一批行**整批同进同出**（事故那一场里时间戳被压进同一分钟，
    按行数切就会切错内容）；
  - **缓冲的行为**：`seq` 从 0 起单调递增、`lines()` 交回的是副本（改不动缓冲）、
    到顶从头丢最老的、`clear()` 之后从零开始；
  - **线程安全**：多个线程同时 `append()`，一条都不丢、`seq` 不重号；
  - **装配**：`render_window()` 把窗口变成一行一句的文本，原句逐字保留、行尾标来源。

全部用例都不碰网络、不写盘，单条压在看门线以内。
"""

from __future__ import annotations

import threading
import time

import pytest

from meeting.backends.base import TranscriptLine
from meeting.summary.window import (
    BufferedLine,
    TranscriptBuffer,
    WINDOW_LABELS,
    render_window,
    select_window,
    source_label,
)

#: 用例的墙钟上限（与 `tests/conftest.py` 那道看门同一个数）
CASE_WALL_CLOCK_LIMIT_SECONDS = 5.0

#: 样本的锚点时刻：**取此刻**。窗口按契约锚在缓冲末行的 `ts` 上，样本因此都从此刻往前铺开，
#: 读起来就是「最近几分钟」那一份真实形状。
ANCHOR_TS = time.time()

#: 秒数 → 一行（造样本时用）
MINUTE = 60.0


def _line(seq: int, ts: float, text: str, source: str = "mic") -> BufferedLine:
    """造一条缓冲里的行（用例的样本工厂）。"""
    return BufferedLine(seq=seq, ts=ts, source=source, text=text, kind="line")


def _fact(ts: float, text: str, source: str = "mic") -> TranscriptLine:
    """造一条转写事实（喂缓冲时用）。"""
    return TranscriptLine(ts=ts, source=source, text=text, kind="line")


def test_buffer_assigns_monotonic_sequence_from_zero() -> None:
    """`seq` 从 0 起、单调递增，`ts` / `source` / `text` 原样收下。"""
    buffer = TranscriptBuffer()
    buffer.append(_fact(ANCHOR_TS, "第一句"))
    buffer.append(_fact(ANCHOR_TS + 1.5, "第二句", source="loopback"))

    lines = buffer.lines()
    assert [(item.seq, item.ts, item.source, item.text, item.kind) for item in lines] == [
        (0, ANCHOR_TS, "mic", "第一句", "line"),
        (1, ANCHOR_TS + 1.5, "loopback", "第二句", "line"),
    ]
    assert len(buffer) == 2


def test_buffer_lines_returns_a_copy() -> None:
    """`lines()` 交回副本：调用方清空它、往里塞东西都动不到缓冲里那一份。"""
    buffer = TranscriptBuffer()
    buffer.append(_fact(ANCHOR_TS, "第一句"))

    handed_out = buffer.lines()
    handed_out.clear()
    handed_out.append(_line(99, ANCHOR_TS, "伪造的一句"))

    assert len(buffer) == 1
    assert buffer.lines()[0].text == "第一句"


def test_buffer_drops_the_oldest_lines_when_it_hits_the_ceiling() -> None:
    """到顶时从头丢最老的那些，`seq` 照旧往前数（时间窗口取的是尾部，丢头不影响它）。"""
    buffer = TranscriptBuffer(max_lines=3)
    for index in range(5):
        buffer.append(_fact(ANCHOR_TS + index, f"第 {index} 句"))

    lines = buffer.lines()
    assert [item.text for item in lines] == ["第 2 句", "第 3 句", "第 4 句"]
    assert [item.seq for item in lines] == [2, 3, 4]
    assert len(buffer) == 3


def test_buffer_clear_starts_over_from_zero() -> None:
    """`clear()` 之后缓冲是空的，`seq` 也从 0 重新数（新的一场会）。"""
    buffer = TranscriptBuffer()
    buffer.append(_fact(ANCHOR_TS, "上一场的一句"))
    buffer.clear()
    buffer.append(_fact(ANCHOR_TS + 10, "这一场的第一句"))

    assert len(buffer) == 1
    assert buffer.lines()[0].seq == 0


def test_buffer_rejects_a_ceiling_below_one() -> None:
    """`max_lines` 至少是 1（0 会让每一行进来就被丢掉，那是配置错误）。"""
    with pytest.raises(ValueError):
        TranscriptBuffer(max_lines=0)


def test_buffer_append_from_many_threads_keeps_every_line() -> None:
    """四个线程各喂 50 行：一条都不丢、`seq` 是 0..199 的一个排列。"""
    buffer = TranscriptBuffer()
    per_thread = 50
    threads = [
        threading.Thread(
            target=lambda worker=worker: [
                buffer.append(_fact(ANCHOR_TS + worker, f"{worker}-{index}"))
                for index in range(per_thread)
            ]
        )
        for worker in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=CASE_WALL_CLOCK_LIMIT_SECONDS)

    lines = buffer.lines()
    assert len(lines) == 4 * per_thread
    assert sorted(item.seq for item in lines) == list(range(4 * per_thread))


def test_select_window_takes_the_tail_by_timestamp() -> None:
    """`5m` 取的是末行往前 300 秒那一段：第 4 分钟及以前的行整条落在窗口外。"""
    lines = [
        _line(0, ANCHOR_TS, "第 0 分钟"),
        _line(1, ANCHOR_TS + 1 * MINUTE, "第 1 分钟"),
        _line(2, ANCHOR_TS + 4 * MINUTE, "第 4 分钟"),
        _line(3, ANCHOR_TS + 6 * MINUTE, "第 6 分钟"),
        _line(4, ANCHOR_TS + 8 * MINUTE, "第 8 分钟"),
    ]

    selected = select_window(lines, window_seconds=300.0)

    # 末行是 8 分钟 → cutoff = 3 分钟；第 4 分钟那一行（ts >= cutoff）留下，
    # 第 1 分钟那一行落在窗口外，它前面那些更早的一并不要
    assert [item.text for item in selected] == ["第 4 分钟", "第 6 分钟", "第 8 分钟"]


def test_select_window_keeps_a_whole_batch_on_one_timestamp() -> None:
    """同一个 `ts` 上挤着的一批行整批同进同出：边界不许从中间断开。"""
    batch_ts = ANCHOR_TS + 10 * MINUTE
    lines = [
        _line(0, ANCHOR_TS, "很久以前"),
        _line(1, batch_ts, "同一刻的第一句"),
        _line(2, batch_ts, "同一刻的第二句"),
        _line(3, batch_ts, "同一刻的第三句"),
    ]

    selected = select_window(lines, window_seconds=1.0)

    # cutoff = 末行 ts − 1；那一批的 ts 相等，因此三条一起进窗口
    assert [item.text for item in selected] == ["同一刻的第一句", "同一刻的第二句", "同一刻的第三句"]


def test_select_window_drops_the_whole_batch_when_it_sits_before_the_cutoff() -> None:
    """整批落在边界之前时，也是整批一起丢（半批都不要）。"""
    batch_ts = ANCHOR_TS
    lines = [
        _line(0, batch_ts, "旧批次一"),
        _line(1, batch_ts, "旧批次二"),
        _line(2, ANCHOR_TS + 10 * MINUTE, "最近一句"),
    ]

    selected = select_window(lines, window_seconds=60.0)

    assert [item.text for item in selected] == ["最近一句"]


def test_select_window_anchors_on_the_last_timestamp_even_when_now_is_far_ahead() -> None:
    """末行 `ts` 明显早于此刻时，窗口**照旧取到那几行**（锚点取末行的 `ts`）。

    场景：实时转写停转（或者说话变稀），缓冲里那 5 行的末行比此刻早 20 分钟。
    传进来的 `now_ts` 与末行 `ts` 差得很远时，窗口的边界必须仍然落在**末行往前
    `window_seconds`** 处 —— 拿此刻当锚点会把窗口切空，用户随后收到一个静默的空结果。
    """
    last_ts = ANCHOR_TS - 20 * MINUTE
    lines = [_line(index, last_ts - (4 - index), f"停转前第 {index} 句") for index in range(5)]

    selected = select_window(lines, window_seconds=900.0, now_ts=ANCHOR_TS)

    assert [item.text for item in selected] == [f"停转前第 {index} 句" for index in range(5)]


def test_select_window_keeps_the_tail_that_sits_before_now() -> None:
    """同一条口径的另一半：早于 `now_ts − window_seconds` 的那一段仍然按**末行**切。

    末行 = 此刻 − 20 分钟、窗口 150 秒：cutoff 落在末行往前两分半处，因此最后三行留下、
    更早两行整条落在窗口外 —— 与此刻隔多远都不改变这个边界。
    """
    last_ts = ANCHOR_TS - 20 * MINUTE
    lines = [_line(index, last_ts - (4 - index) * 60.0, f"第 {index} 句") for index in range(5)]

    selected = select_window(lines, window_seconds=150.0, now_ts=ANCHOR_TS)

    # 行间距 1 分钟：末行往前 150 秒 → 第 2、3、4 句留下，第 1 句落在窗口外
    assert [item.text for item in selected] == ["第 2 句", "第 3 句", "第 4 句"]


def test_select_window_full_takes_every_line() -> None:
    """`window_seconds=None`（整场）把缓冲里的全部行按原序收下。"""
    lines = [_line(index, ANCHOR_TS + index, f"第 {index} 句") for index in range(6)]

    selected = select_window(lines, window_seconds=None)

    assert [item.text for item in selected] == [f"第 {index} 句" for index in range(6)]
    # 交回的是同一批对象组成的新列表，输入那份一个字没动
    assert len(lines) == 6


def test_select_window_on_an_empty_buffer_is_empty() -> None:
    """缓冲空着时，给了锚点与没给锚点都交回空窗口（刚开会不会凭空切出内容）。"""
    assert select_window([], window_seconds=300.0, now_ts=ANCHOR_TS) == []
    assert select_window([], window_seconds=300.0) == []
    assert select_window([], window_seconds=None) == []


def test_select_window_rejects_a_non_positive_span() -> None:
    """0 秒或负数的窗口是调用方写错了：当场报错，别悄悄退化成「一行都不给」或「全给」。"""
    lines = [_line(0, ANCHOR_TS, "一句")]

    with pytest.raises(ValueError):
        select_window(lines, window_seconds=0.0)
    with pytest.raises(ValueError):
        select_window(lines, window_seconds=-5.0)


def test_render_window_keeps_the_body_verbatim_and_labels_the_source() -> None:
    """一行一句：`[HH:MM:SS] 正文 · 来源`；识别失败的占位符原样留着（缺口要能被讲出来）。"""
    lines = [
        _line(0, ANCHOR_TS, "本地这一句"),
        _line(1, ANCHOR_TS + 1, "[[识别失败: 超时 20s]]", source="loopback"),
    ]

    rendered = render_window(lines).splitlines()

    assert rendered[0].endswith("本地这一句 · 本地")
    assert rendered[1].endswith("[[识别失败: 超时 20s]] · 远端")
    assert rendered[0].startswith("[") and rendered[0].count(":") == 2


def test_render_window_on_an_empty_window_is_an_empty_string() -> None:
    """空窗口装配出空串（调用方在窗口为空时压根不会走到这一步，这里钉住形状）。"""
    assert render_window([]) == ""


def test_source_labels_cover_the_two_paths_and_the_unknown_one() -> None:
    """两路的人话标签逐字对齐设计章 2.3；认不出的来源报 `未知`，不编一个中文名给它。"""
    assert source_label("mic") == "本地"
    assert source_label("loopback") == "远端"
    assert source_label("something-else") == "未知"
    assert WINDOW_LABELS == {"5m": "5 分钟", "15m": "15 分钟", "30m": "30 分钟", "full": "整场"}
