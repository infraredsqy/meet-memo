"""纪要这一层的 claim 锁：**「同一场不挂两趟」的跨进程闸**。

「这一层在飞」的持久落点是 `meeting_minutes_status.json` 里那一版 `running`
（那份状态文件的形状与读写归 `meeting/minutes/status.py`）；本模块只管把
「读一遍现状 + 写一版 `running`」裹成**跨进程原子**的那把瞬态锁，以及拉起方
（`meeting/backends/minutes.py::MinutesHook` 的 `claim()` / `launch()`）在
**建线程之前**同步调用的 `mark_running()`。
"""

from __future__ import annotations

import logging
import os
import time as _time
from contextlib import suppress
from pathlib import Path
from typing import Any

from meeting.minutes.status import (
    BRANCH_HEADLESS,
    MODE_UNKNOWN,
    STATE_RUNNING,
    STATUS_NAME,
    StatusError,
    finished_at_for,
    now_iso,
    read_status,
    status_path,
    write_session_status,
)

_log = logging.getLogger("meeting.minutes.claim")

#: 「在飞」那一版落盘时的**claim 锁**名（住在场次目录根，与状态文件并列）。
#:
#: 它保证「读一遍现状 + 写一版 `running`」这件事**跨进程原子**：两个 `meeting post`
#: 前后脚进来时，只有一个拿得到它，另一个在锁在场期间读到的就是那一版 `running`。
#: 它是**瞬态**文件（写完那一版 `running` 就删掉），产物清单里没有它的名字：进程被杀
#: 也只是把它留在盘上，下一趟按下面那段有界等待兜住 —— 锁在场而状态已经离开 `running`
#: 时，说明那一趟已经收工或压根没起来，接着拿锁就行）。
LOCK_NAME = "meeting_minutes_status.lock"

#: 拿 claim 锁失败时最多再试几次（每次之间让出 `LOCK_WAIT_SECONDS`）
LOCK_ATTEMPTS = 3

#: 拿 claim 锁失败时每次让出的时长（秒）：只为盖住「锁刚建出来、那一版 `running` 还没写完」
#: 那个**微秒级**的缝，不是用来等一整条纪要链的。
LOCK_WAIT_SECONDS = 0.05


class AlreadyRunningError(RuntimeError):
    """**纪要这一层已经在飞了**：状态文件停在 `running`，这一趟拒绝再拉起一个。

    它是「同一个场次目录上再拉一次子会话」这条路的**终点**：抛出它的那一趟没有起线程、
    没有拉起任何子会话，调用方据此把这一趟按 `running` 拒跑档收场（`--force` 是出路）。
    继承 `RuntimeError`：这一档说的是运行期状态冲突（同一个目录上已经有一趟在跑），
    与 `StatusError` 那类「文件内容不合契约」的值错误各说各的一层。
    """


def lock_path(session_dir: str | Path) -> Path:
    """claim 锁的落点（场次目录根，与状态文件并列）。"""
    return Path(session_dir) / LOCK_NAME


def mark_running(
    session_dir: str | Path,
    *,
    branch: str = BRANCH_HEADLESS,
    started_at: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """**把「这一层在飞」写到盘上**，返回那一版状态；已经在飞时抛 `AlreadyRunningError`。

    这是纪要那一层「正在跑」这件事的**唯一持久落点**，供拉起方（
    `meeting/backends/minutes.py::MinutesHook` 的 `claim()` / `launch()`）在**建线程之前**
    同步调用一次：调用返回时，盘上那份 `meeting_minutes_status.json` 已经读得出
    `state = "running"`，于是另一趟（用户随后手敲的 `meeting post` 是另一个进程；
    工作台「直接出纪要」按钮那一趟也在起线程之前经 `claim()` 同步走到这里）当场
    就能看见「上一趟还在跑」，按现成的 `running` 拒跑档收场 —— 拉起与拒跑因此落在同一份
    记录里，谁也漏不掉。

    **读一遍现状 + 写一版 `running` 这件事跨进程原子**：整段由 `LOCK_NAME` 那把
    `O_CREAT | O_EXCL` 的 claim 锁裹住，同一时刻只有一个进程进得来。拿不到锁时先让出
    `LOCK_WAIT_SECONDS` 再读一遍状态文件（那一版 `running` 就在锁里写，最多几十微秒的缝）：

      - 读到的状态停在 `running` —— 另一趟正在飞，抛 `AlreadyRunningError`（**不覆盖它的记录**）；
      - 读到别的取值（上一趟的 `done` / `failed`、或者压根没有这份文件）—— 上一趟已经收工，
        接着拿锁就行（进程崩在半路时锁会留在盘上，这一段有界等待正是为它准备的）。

    `force = True` 时**跳过上面那条在飞判据**：`--force` 的语义就是「无视这一层的现状、
    重跑」（它是本轮唯一会再次花钱的开关），于是同一条在飞判据在 `force` 那一档照常放行，
    这一趟把那一版 `running` 原子覆盖成自己的（`finished_at` 仍是空串，读方看到的还是
    「正在跑」，只是记录换成了这一趟的）。

    写出去的形状走现成的 `build_status()` / `write_status()`：四档取值域、四个必填字段、
    原子替换一个字都不动，这一版与 `runner` 线程体随后落的那一版**同形**，因此线程体那一版
    是原子覆盖（幂等：谁来写都读到同一份形态）。`finished_at` 明写成空串（`runner.py` 里
    「先落 `running` 再拉起 DSH」那一段同一条口径），读方据此分得出「正在跑」。
    """
    session = Path(session_dir)
    session.mkdir(parents=True, exist_ok=True)
    moment = now_iso() if started_at is None else str(started_at)
    lock = lock_path(session)
    attempts = max(1, int(LOCK_ATTEMPTS))
    skip_check = bool(force)

    for attempt in range(attempts):
        if _claim_lock(lock):
            try:
                if not skip_check and _peek_state(session) == STATE_RUNNING:
                    raise AlreadyRunningError(_already_running_reason(session))
                return _write_in_flight(session, moment=moment, branch=branch)
            finally:
                with suppress(OSError):
                    lock.unlink(missing_ok=True)
        if not skip_check and _peek_state(session) == STATE_RUNNING:
            raise AlreadyRunningError(_already_running_reason(session))
        if attempt + 1 < attempts:
            _time.sleep(LOCK_WAIT_SECONDS)

    # 有界等待走完、锁仍在盘上（上一趟崩在半路留下的残锁），而状态文件此刻读不到「在飞」：
    # 接着往下走，由调用方真的把这一趟拉起来 —— 「上一趟崩了」那条出路必须留着。
    _log.warning(
        "claim 锁留在盘上（上一趟可能崩在半路），状态文件读不到在飞，这一趟照常拉起：%s", lock
    )
    return _write_in_flight(session, moment=moment, branch=branch)


def _already_running_reason(session: Path) -> str:
    """拒跑那一档的理由（点名状态文件与它的落点）。"""
    return (
        f"纪要这一层已经在飞（{STATUS_NAME} 停在 {STATE_RUNNING}）："
        f"这一趟不再拉起子会话：{session}"
    )


def _write_in_flight(session: Path, *, moment: str, branch: str) -> dict[str, Any]:
    """落那一版「在飞」的状态文件（形状与 `runner` 线程体那一版同形，取值为空的那几格）。

    「在飞版 `finished_at` 空串」的口径从 `finished_at_for()` 取（唯一出处）。
    """
    return write_session_status(
        session,
        state=STATE_RUNNING,
        mode=MODE_UNKNOWN,
        branch=str(branch),
        started_at=moment,
        finished_at=finished_at_for(STATE_RUNNING),
        artifacts={},
        dsh={},
    )


def _claim_lock(lock: Path) -> bool:
    """试着把 claim 锁建出来（`O_CREAT | O_EXCL`：跨进程原子，成没成只有一个是真）。"""
    try:
        handle = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with suppress(OSError):
        os.write(handle, f"{os.getpid()} {now_iso()}\n".encode("utf-8"))
    with suppress(OSError):
        os.close(handle)
    return True


def _peek_state(session: Path) -> str | None:
    """读一眼盘上那一版的 `state`；文件不在场 / 读不动时交回 `None`（按「还没起来」算）。"""
    try:
        return str(read_status(status_path(session)).get("state") or "")
    except (OSError, StatusError, ValueError):
        return None


__all__ = [
    "AlreadyRunningError",
    "LOCK_ATTEMPTS",
    "LOCK_NAME",
    "LOCK_WAIT_SECONDS",
    "lock_path",
    "mark_running",
]
