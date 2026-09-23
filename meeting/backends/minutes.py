"""纪要钩子（`41465462` phase2）：会后自动跑纪要生成的那一格。

`registry.resolve_minutes_hook()` 按 `minutes.hook` 找的就是本模块的 `MinutesHook`
（`DEFAULT_PATHS["minutes.hook"]` 那条约定路径）。挂在 `meeting/offline/runner.py` 的
`run_session()` 尾巴上（三份离线产物与状态文件落定之后）。

## 契约（与 `meeting/backends/post.py` 同一条）

  - `launch(session_dir)` **返回即算启动成功**：活儿交给一条 **daemon 线程**，
    调用方（离线那一层的尾巴）不等它跑完；
  - 线程里的任何异常都落进 `meeting_minutes_status.json`，
    **不许把异常抛回收尾流程** —— 纪要这一层炸了不许影响离线那三份产物；
  - **重依赖在函数体内 import**：本模块顶层只有标准库，`import meeting.backends.minutes`
    因此保持轻量（`meeting web` 的启动链不会被拖慢）。

类的 `__module__` 是 `meeting.backends.minutes` —— `registry.capabilities_of()` 靠它区分替身
与本实现（写进别的模块名下会被当成替身）。**不实现 `finalize` / `transcode`**：那两项是采集
后端的能力。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

_log = logging.getLogger("meeting.backends.minutes")

#: 后台线程的名字（出现在线程转储与 log 里，一眼能认出是哪一条）
THREAD_NAME = "minutes-final"

#: 状态文件里 `state` 的三档（与 `meeting/minutes/status.py` 同名同义）
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"

#: 再入闸的环境变量名（子会话里看到它就不再拉起 DSH）
CHILD_FLAG = "MEETING_MINUTES_CHILD"


class MinutesHook:
    """会后纪要生成的钩子：**起线程就返回**。

    `minutes.hook` 的四档取值里 `auto` 落在这里；`null` 落 `registry._AbsentMinutesHook`
    （缺席档，那是另一个档位的事）。
    """

    def launch(self, session_dir: str) -> None:
        """离线那一层的尾巴调它，**返回即算启动成功**（不等它跑完）。

        `session_dir` 是这一场的场次目录。起一条 daemon 线程跑
        `meeting.minutes.runner.run(session_dir)`，随即返回。
        """
        target = str(session_dir or "")
        if not target:
            # 没有场次目录就没有落点：如实报错（调用方那一层用 try/except 兜住，只记日志）
            raise ValueError("launch() 需要场次目录（收到空值）")
        thread = threading.Thread(
            target=self._run,
            args=(target,),
            name=THREAD_NAME,
            daemon=True,
        )
        thread.start()

    # -- 线程体 -------------------------------------------------------------

    def _run(self, session_dir: str) -> None:
        """线程体：跑完一次纪要生成；**任何异常都落进状态文件，绝不外传**。

        `runner.run()` 自己会把状态文件落成终态。它整个抛出来时（进程被杀、磁盘写不进去
        这类），这里把状态文件补成 `failed` 并写上原因，那一格于是不会永远停在 `running`。
        """
        try:
            from meeting.minutes.runner import run

            run(session_dir)
        except BaseException as exc:  # 后台线程的异常没有别的落点，兜到底
            _log.exception("会后纪要生成失败：%s", session_dir)
            self._mark_failed(session_dir, exc)

    def _mark_failed(self, session_dir: str, exc: BaseException) -> None:
        """把状态文件补成 `failed`（**尽力而为**：连这一步都失败就只留一条 log）。"""
        try:
            from meeting.minutes import status as status_module

            path = status_module.status_path(session_dir)
            existing: dict = {}
            if path.exists():
                try:
                    existing = status_module.read_status(path)
                except status_module.StatusError:
                    existing = {}
            status_module.write_session_status(
                session_dir,
                state=STATE_FAILED,
                mode=str(existing.get("mode") or "none"),
                branch=str(existing.get("branch") or "headless"),
                started_at=str(existing.get("started_at") or _now_iso()),
                error=f"{type(exc).__name__}: {exc}",
                artifacts=dict(existing.get("artifacts") or {}),
                dsh=dict(existing.get("dsh") or {}),
            )
        except Exception:  # pragma: no cover - 兜底那一步失败时不掩盖原异常
            _log.exception("会后纪要状态文件补写失败（原异常照上面那条 log）")


def _now_iso() -> str:
    """现在的 ISO 时刻（与 `meeting/offline/runner.py` 的 `_now_iso()` 同形）。"""
    return datetime.now().isoformat(timespec="seconds")


__all__ = [
    "CHILD_FLAG",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_RUNNING",
    "THREAD_NAME",
    "MinutesHook",
]
