"""会后钩子（`7c6952e5` phase2）：会后离线转写那一层的解析落点。

`registry.resolve_post_hook()` 按 `post.hook` 找的就是本模块的 `PostProcessHook`
（`DEFAULT_PATHS["post.hook"]` 那条约定路径）。**它现在没有自动调用方**：收尾第 4 步
不再拉起任何会后动作（那一格做完释放显存就停在 `waiting`），「生成会后精细稿」按钮走
`offline.runner.run_session(..., minutes=False)` 的直路也不经它 —— `post.hook` 在那一侧
只答「这一层接没接」（`null` → 按钮 404 `action_unavailable`）。

## 契约（`docs/backend-protocols.md`）

  - `launch(session_dir)` **返回即算启动成功**：活儿交给一条 **daemon 线程**，
    调用方不等它跑完；
  - 线程里跑的是 `meeting.cli.run_post()` —— **与 `meeting post` 同一段编排**
    （`65dd3e4c` phase1 把「跑会后一套」收成一条共用的函数：音频归档补齐 →
    离线那一趟 → 等纪要终态 → 汇总）；
  - 线程里的任何异常都落进 `meeting_offline_status.json` 的 `error`，
    **不许把异常抛给调用方** —— 跑不跑得成由状态文件回答；
  - 会话级 `session.json` 归收尾流程写，这条后台线程**不去碰它**；
  - **重依赖在函数体内 import**（`torch` / `transformers` / `av` 都在
    `meeting.offline` 的那几个模块里，本模块自己一条都不拉）：
    `import meeting.backends.post` 必须保持轻量，否则 `meeting web` 的启动链会被拖慢。

类的 `__module__` 是 `meeting.backends.post` —— `registry.capabilities_of()` 靠它区分
替身与本实现（写进别的模块名下会被当成替身）。**不实现 `finalize` / `transcode`**：
那两项是采集后端的能力，采集侧那两步的闸门只看采集实现。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

_log = logging.getLogger("meeting.backends.post")

#: 后台线程的名字（出现在线程转储与 log 里，一眼能认出是哪一条）
THREAD_NAME = "post-offline"

#: 状态文件里 `state` 的四档（`meeting.offline.artifacts.STATES` 同值）
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
#: 一路整条没交回、另一路跑成那一档：产物可用（它是终态），顶层那一格与 `done` 分开写
STATE_PARTIAL = "partial"


class PostProcessHook:
    """会后离线转写的钩子：**起线程就返回**。

    `post.hook` 的四档取值里 `auto` 落在这里；`null` 落 `registry._AbsentPostHook`
    （缺席记 `skipped`，那是另一个档位的事）。
    """

    def launch(self, session_dir: str) -> None:
        """**返回即算启动成功**（不等它跑完）。

        当前没有产品路径调用它（收尾第 4 步不再拉起任何会后动作，按钮那一侧走
        `run_session` 直路）；`post.hook` 的解析结论只答「这一层接没接」。
        `session_dir` 是这一场的场次目录。起一条 daemon 线程跑
        `meeting.offline.runner.run_session(session_dir)`，随即返回。
        """
        target = str(session_dir or "")
        if not target:
            # 没有场次目录就没有落点：如实报错（有调用方时按「有实现却跑不成」记账）
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
        """线程体：跑完整条会后链；**任何异常都落进状态文件，绝不外传**。

        `meeting.cli.run_post()` 是「跑会后一套」那一段共用的编排（`meeting post` 那条入口
        走的就是它，本线程体也调它），它自己会把离线状态文件从 `running` 落到终态
        （离线段归 `meeting.offline.runner.run_session()`）。它整个抛出来时（进程被杀、
        磁盘写不进去这类），这里把状态文件补成 `failed` 并写上原因，那一格于是不会永远
        停在 `running`。

        `run_post` 在**函数体内 import**：本模块顶层的惰性纪律一个字不动
        （`import meeting.backends.post` 仍然一条重依赖都不拉）。
        """
        try:
            from meeting.cli import run_post

            run_post(session_dir)
        except BaseException as exc:  # 后台线程的异常没有别的落点，兜到底
            _log.exception("会后离线转写失败：%s", session_dir)
            self._mark_failed(session_dir, exc)

    def _mark_failed(self, session_dir: str, exc: BaseException) -> None:
        """把状态文件补成 `failed`（**尽力而为**：连这一步都失败就只留一条 log）。

        与 `meeting/cli.py` 的 `_mark_offline_failed()` 同一个口径（盘上那一版已经是终态时
        照它自己那一版留着）：异常到这里之前，离线段自己**已经现场写过**一版 ——
        `runner._write_failed_status()` 落 `failed`（`error` 里写着是加载失败还是跑失败），
        一路跑成那种场面落 `partial`。盘上那一版是这两档时就沿用它的 `error` 与
        `partial` / `silent` / `silent_spans` / `gate` 四格：那份账比这里手里的通用串准。
        """
        try:
            from meeting.offline import artifacts

            path = Path(session_dir) / artifacts.STATUS_NAME
            existing = None
            if path.exists():
                try:
                    existing = artifacts.read_status(path)
                except artifacts.ArtifactContractError:
                    existing = None
            disk = existing or {}
            settled = str(disk.get("state") or "") in (STATE_FAILED, STATE_PARTIAL)
            artifacts.write_status(
                path,
                session_id=str(disk.get("session_id") or Path(session_dir).name),
                state=str(disk.get("state")) if settled else STATE_FAILED,
                started_at=str(disk.get("started_at") or _now_iso()),
                sources=list(disk.get("sources") or []),
                artifacts=dict(disk.get("artifacts") or {}),
                finished_at=_now_iso(),
                error=str(disk.get("error") or f"{type(exc).__name__}: {exc}")
                if settled
                else f"{type(exc).__name__}: {exc}",
                partial=bool(disk.get("partial")) if settled else False,
                silent=bool(disk.get("silent")) if settled else False,
                silent_spans=list(disk.get("silent_spans") or []) if settled else None,
                gate=dict(disk.get("gate") or {}) if settled else None,
            )
        except Exception:  # pragma: no cover - 兜底那一步失败时不掩盖原异常
            _log.exception("会后离线状态文件补写失败（原异常照上面那条 log）")


def _now_iso() -> str:
    """现在的 ISO 时刻（与 `meeting/offline/runner.py` 的 `_now_iso()` 同形）。"""
    return datetime.now().isoformat(timespec="seconds")


__all__ = [
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_PARTIAL",
    "STATE_RUNNING",
    "THREAD_NAME",
    "PostProcessHook",
]
