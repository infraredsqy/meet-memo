"""`meeting/importlock.py` 的契约用例：进程级互斥、同线程重入、零重依赖。

全部快档：无 sleep 轮询、无真子进程、无真模型 —— 「假加载段」就是
`with heavy_imports():` 包住的一段登记代码；线程同步全走事件与屏障。
"""

from __future__ import annotations

import importlib
import sys
import threading

from meeting import importlock

#: 等待事件/屏障/线程收尾的统一上限（秒）——超时即失败，不等第二遍
_TIMEOUT = 5.0


def test_heavy_imports_serializes_concurrent_threads() -> None:
    """两条线程同时进假加载段：第二条必须等第一条**完全退出**临界区才进得去。

    顺序记录 `order` 是互斥的决定性证据：锁正确时第二条的进入必然排在
    `first-out` 之后；互斥失效时它会插到中间（两条线程同时在临界区内）。
    """
    order: list[str] = []
    release_first = threading.Event()
    first_inside = threading.Event()
    second_at_door = threading.Barrier(2, timeout=_TIMEOUT)

    def first() -> None:
        with importlock.heavy_imports():
            order.append("first-in")
            first_inside.set()
            assert release_first.wait(timeout=_TIMEOUT)
            order.append("first-out")

    def second() -> None:
        # 与主线程会合：此刻第二条线程已站在锁门口，而第一条还持着锁
        second_at_door.wait()
        with importlock.heavy_imports():
            order.append("second-in")

    t1 = threading.Thread(target=first, name="fake-heavy-loader-1")
    t1.start()
    assert first_inside.wait(timeout=_TIMEOUT), "第一条线程没进得了临界区"

    t2 = threading.Thread(target=second, name="fake-heavy-loader-2")
    t2.start()
    assert second_at_door.wait(timeout=_TIMEOUT), "第二条线程没到锁门口"
    assert "second-in" not in order, (
        "锁被第一条持着时第二条进了临界区（互斥失效）"
    )

    release_first.set()
    t1.join(timeout=_TIMEOUT)
    t2.join(timeout=_TIMEOUT)
    assert not t1.is_alive() and not t2.is_alive(), "假加载线程没在时限内收尾"

    assert order == ["first-in", "first-out", "second-in"], order


def test_heavy_imports_reenters_on_the_same_thread() -> None:
    """同一线程嵌套进入不死锁（选 `RLock` 的理由）：加载链内可能彼此调用。"""
    depth: list[int] = []
    with importlock.heavy_imports():
        with importlock.heavy_imports():
            depth.append(2)
    assert depth == [2]


def test_importing_importlock_pulls_no_heavy_dependency() -> None:
    """import `meeting.importlock` 这一步自己不拉重依赖。

    照 `tests/test_asr_glm.py` 的「全新解释器零重依赖」断言口径，但**不起子进程**：
    改用 import 前后 `sys.modules` 的差集，断言「这次 import 自己没有拉进任何重依赖」
    —— 本进程里重依赖可能早被别的用例拉进表里，差集不受它们影响；对一个纯标准库
    小模块，这与全新解释器等价。
    """
    sys.modules.pop("meeting.importlock", None)
    before = frozenset(sys.modules)
    module = importlib.import_module("meeting.importlock")
    fresh = set(sys.modules) - before
    heavy = sorted(fresh & {"torch", "transformers", "numpy", "webrtcvad", "av"})
    assert not heavy, f"import meeting.importlock 拉进了重依赖：{heavy}"
    assert callable(module.heavy_imports)
