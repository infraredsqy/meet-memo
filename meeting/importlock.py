"""进程级的重型首导锁：串行化跨线程的 torch / transformers 等重依赖首次 import。

会中 ASR 与会后引擎都要在各自的后台线程里函数体内首次 import transformers：两条线程
同时首导同一模块时，其一可能见到半初始化的模块对象，`from transformers import
AutoModelForCausalLM` 当场 `ImportError`（单独导入却是好的）。
两处共用这把进程级 `RLock`（`heavy_imports()`）串行化重型首导；退出期不再发起新加载
由各加载入口的 `sys.is_finalizing()` 守卫负责。选 `RLock` 是因为加载链内可能同线程嵌套进入。

纯标准库：任何 import 图都能安全带上它。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

_HEAVY_IMPORT_LOCK = threading.RLock()


@contextmanager
def heavy_imports():
    with _HEAVY_IMPORT_LOCK:
        yield


__all__ = ["heavy_imports"]
