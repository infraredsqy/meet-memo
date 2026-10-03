"""进程级的重型首导锁：串行化**跨线程**的 torch / transformers 等重依赖首次 import。

会中 ASR（`meeting/asr/glm.py`）与会后引擎（`meeting/offline/engine.py`）都要在各自的
后台线程里**函数体内首次** import transformers：两条线程同时首导同一个模块时，其一可能
见到半初始化的模块对象，`from transformers import AutoModelForCausalLM` 当场
`ImportError`（260928-2 那一场实测：单独导入却是好的）；进程收尾还可能把仍在跑的
daemon 线程拦腰打断，让 import 断在半路。两处因此共用这一把**进程级** `RLock`
（`heavy_imports()`），把重型首导串行化；退出期不再发起新的加载则由各加载入口的
`sys.is_finalizing()` 守卫负责（见 `meeting/offline/engine.py` 的
`EngineShutdownInterrupted`）。

选 `RLock` 而不是 `Lock`：加载链内可能嵌套进入（引擎加载路径彼此调用时同一线程
二次进入不该死锁）。

纯标准库（`threading` / `contextlib`）：任何 import 图都能安全带上它。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

#: 进程级互斥：全进程的重型首导共用这一把（RLock 允许同一线程嵌套进入）
_HEAVY_IMPORT_LOCK = threading.RLock()


@contextmanager
def heavy_imports():
    """把重型依赖的首次 import 包进这把锁：同一时刻全进程只有一条线程在首导。"""
    with _HEAVY_IMPORT_LOCK:
        yield


__all__ = ["heavy_imports"]
