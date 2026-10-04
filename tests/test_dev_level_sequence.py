"""`tests/test_dev_level_sequence.py` —— dev 替身合成电平的增量缓存（复审 #3）。

`DevCaptureBackend._run()` 每拍调 `level_sequence(source, tick + 1)` 取末位；过去每次
都从种子重建整条序列（单帧 O(tick)、整场累计 O(tick²)）。现在 `level_sequence()` 走
每路一份的增量缓存：没超过缓存就回前缀、超过才从缓存续生 —— 这三条纯进程内用例
钉住「续生与从种子重算逐值同前缀」这条不变量（同种子同帧序逐帧相同的一个推论）。

lost / stall 那一档用**合成的取数位点**模拟（`_run()` 的 `tick` 逐拍走、断流的那一拍
不取数，续帧时取到的末位必须还是全程序列的那一位 —— 不起真线程、不等真墙钟）。
"""

from __future__ import annotations

import pytest

from meeting.backends.dev import DevCaptureBackend


def test_level_sequence_is_prefix_stable_for_larger_counts():
    """`level_sequence(src, N) == level_sequence(src, M)[:N]`（M > N）：缓存续生不偏移。"""
    backend = DevCaptureBackend()
    # 先取小段（起缓存），再放大 count：放大的那段必须与前缀同值（= 从种子重算）
    for source in ("mic", "loopback"):
        for small in (1, 5, 12):
            small_head = backend.level_sequence(source, small)
            grown = backend.level_sequence(source, 40)
            assert small_head == grown[:small], f"{source} count={small}"
    # 每路缓存独立：mic 与 loopback 是两条种子流，不串台
    assert backend.level_sequence("mic", 40) != backend.level_sequence("loopback", 40)


def test_no_level_is_regenerated_when_the_count_did_not_grow():
    """count 没超过缓存就不再续生：反复取同一前缀逐次相同（不是从种子另起一条）。"""
    backend = DevCaptureBackend()
    first = backend.level_sequence("mic", 7)
    for _ in range(3):
        assert backend.level_sequence("mic", 7) == first


def test_the_stream_stays_on_the_full_prefix_after_lost_and_stall_ticks():
    """注入 lost / stall 各一拍（合成线程那一拍不取数）之后续帧，
    取到的末位仍与全程序列同前缀 —— 取数位点不漂。"""
    backend = DevCaptureBackend()
    full = backend.level_sequence("mic", 40)
    # 开场两拍在飞（tick 0、1 各发一帧）
    assert backend.level_sequence("mic", 1)[-1] == full[0]
    assert backend.level_sequence("mic", 2)[-1] == full[1]
    # lost 一拍：这一路整段断流（不取数）；stall 一拍：两路整体停发（不取数）——
    # `tick` 照走，续帧那一拍按 tick+1 取末位
    assert backend.level_sequence("mic", 4)[-1] == full[3]
    assert backend.level_sequence("mic", 40) == full


if __name__ == "__main__":  # pragma: no cover - 便于单文件直接跑
    raise SystemExit(pytest.main([__file__, "-q"]))
