"""并发敏感的原子写：两包共用（`meeting/offline/artifacts.py` 与
`meeting/minutes/inputs.py` 原先各留一份逐行同构的实现，现在只留这一份）。

写一份文本产物：先写本调用独占的临时文件，再 `os.replace` 换上去。收尾是后台线程在跑、
界面与下游随时可能来读，半份文件比慢一点更坏。

临时文件名每个写方各不相同（`_tmp_path_for()`）：同一目标上并发两个写方（如服务侧会后链
与另一进程 `meeting post` 的补记录）共用一个 `<名字>.tmp` 时，先落地的 `os.replace` 会把
临时文件消费掉，另一边随即抛 `FileNotFoundError`。

Windows 上一份别人正拿在手里的文件会让 `os.replace` 抛 `PermissionError`
（`WinError 32`）：状态文件在收尾那几十秒里被反复重写、又被界面轮询读——这一档
与瞬时失败共用同一条短退避；重试用尽时原样抛出，由调用方如实登记。

纯标准库：`meeting.minutes` 整包「只用标准库」的不变量不被碰到。
"""

from __future__ import annotations

import os
import threading
import time as _time
import uuid
from contextlib import suppress
from pathlib import Path

#: `os.replace` 的瞬时失败里，Windows 那一档的 `winerror`：拒绝访问 / 共享冲突 / 锁冲突
_TRANSIENT_WINERRORS = frozenset({5, 32, 33})

#: 退避重试的次数（同一份文件被界面读着的那几十毫秒里重试几次就够）
REPLACE_RETRIES = 5


def _tmp_path_for(target: Path) -> Path:
    """这次调用独占的临时文件路径（`<目标名>.<pid>.<线程 id>.<随机串>.tmp`）。

    随机串兜住线程 id / pid 被复用（线程退出后 ident 会被下一条接着用）——
    只靠 pid + ident 在长跑的收尾线程池里仍会撞名。
    """
    return target.with_name(
        f"{target.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    )


def _is_transient_replace_error(exc: OSError) -> bool:
    """`os.replace` 的这次失败是不是瞬时那一档（退避重试还有意义）。

    界面读与收尾写的窗口都很短：`PermissionError` / `FileNotFoundError`（目标正被拿着、
    或正处在被换走 / 被删的瞬间）重试即可；部分路径上共享与锁冲突落的是裸 `OSError`
    （winerror 32 / 33）。其余异常（磁盘满、路径不存在、只读盘）一次都不重试。
    """
    if isinstance(exc, (PermissionError, FileNotFoundError)):
        return True
    return getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS


def write_text_atomic(path: Path | str, text: str, *, retries: int = REPLACE_RETRIES) -> Path:
    """写一份文本产物：先写一份**本调用独占的**临时文件，再 `os.replace` 换上去。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path_for(target)
    tmp.write_text(text, encoding="utf-8")
    attempts = max(1, int(retries))
    try:
        for attempt in range(attempts):
            try:
                os.replace(tmp, target)
            except OSError as exc:
                if attempt + 1 >= attempts or not _is_transient_replace_error(exc):
                    raise
                _time.sleep(0.02 * (attempt + 1))
            else:
                return target
        return target  # pragma: no cover - 上面那一支已经返回或抛出
    finally:
        # 重试用尽那一支会留下这次调用自己的临时文件：收拾掉，不给目标目录留残骸
        # （成功那一支已经把它换走了，这里是空操作）。
        with suppress(OSError):
            tmp.unlink(missing_ok=True)


__all__ = [
    "REPLACE_RETRIES",
    "write_text_atomic",
]
