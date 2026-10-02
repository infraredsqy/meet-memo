"""纪要钩子（`41465462` phase2）：会后纪要生成那一格的解析落点。

`registry.resolve_minutes_hook()` 按 `minutes.hook` 找的就是本模块的 `MinutesHook`
（`DEFAULT_PATHS["minutes.hook"]` 那条约定路径）。**拉起它的地方有两处**：
`meeting post` 那一趟 `run_session()` 的尾巴（`_announce_minutes()`，三份离线产物与
状态文件落定之后），以及工作台「直接出纪要」按钮（`post_minutes`）在同步认过账之后的直调。

## 契约（与 `meeting/backends/post.py` 同一条）

  - `launch(session_dir)` **返回即算启动成功**：活儿交给一条 **daemon 线程**，
    调用方不等它跑完；
  - **返回之前先把「这一层在飞」落到盘上**：那一版 `meeting_minutes_status.json`
    （`state = "running"`）由 `meeting/minutes/status.py::mark_running()` 同步写，
    于是「同一场会不会被拉起两个子会话」这件事在**跨进程**下也读得出来 —— 后到的那一趟
    读到这一版就按现成的 `running` 拒跑档收场（`--force` 是出路）。落点选在这里的理由：
    拉起这个动作的出口不止一处（`meeting/offline/runner.py::_announce_minutes()` 的两支之外，
    `post_minutes` 按钮也直调 `launch()`），而线程体那一版 `running` 落在
    `launch()` 返回**之后**，等它写下来再判就晚了；
  - 盘上已经停着一版 `running` 时，`launch()` 当场抛 `AlreadyRunningError`：
    这一趟不起线程、不拉子会话；带 `force=True`（`meeting post <目录> --force` 那一趟）
    时照常放行 —— `--force` 是 `running` 那一档给出的出路；
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

#: 「这一层已经在飞」那一档（`meeting/minutes/status.py` 定义，本模块原样转出）：
#: `launch()` 读到盘上停着一版 `running` 时抛它，调用方据此按拒跑档收场。
#: 转出它是为了让调用方**不必**自己去 import `meeting.minutes.status`（那一包是重的那一头，
#: `meeting/offline/runner.py` 的惰性纪律一个字不动）。
from meeting.minutes.status import AlreadyRunningError  # noqa: F401  (转出给调用方)

_log = logging.getLogger("meeting.backends.minutes")

#: 后台线程的名字（出现在线程转储与 log 里，一眼能认出是哪一条）
THREAD_NAME = "minutes-final"

#: 状态文件里 `state` 的三档（与 `meeting/minutes/status.py` 同名同义）
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"

#: `minutes.hook` 的缺省取值（与 `meeting/minutes/runner.py::DEFAULT_HOOK` 同一格同值）
DEFAULT_HOOK = "auto"

#: 再入闸的环境变量名（子会话里看到它就不再拉起 DSH）
CHILD_FLAG = "MEETING_MINUTES_CHILD"


class MinutesHook:
    """会后纪要生成的钩子：**起线程就返回**。

    `minutes.hook` 的四档取值里 `auto` 落在这里；`null` 落 `registry._AbsentMinutesHook`
    （缺席档，那是另一个档位的事）。
    """

    def claim(self, session_dir: str) -> None:
        """**同步**地把「这一层在飞」落到盘上；盘上已经停着一版 `running` 就当场抛
        `AlreadyRunningError`（`force=True` 那一档请走 `launch()`）。

        它调的是 `meeting/minutes/status.py::mark_running()` 那一把**既有**的 claim 锁
        （`mark_running` 的全部语义一字不改，本方法只是它的一次同步调用）：调用返回时
        `meeting_minutes_status.json` 已经读得出 `state = "running"`，于是「同一场挂不上
        两个子会话」这件事**在动作本体里就答得出来**，不必等线程体那条路。

        为什么要它（`post_minutes` 的重复按下那一档）：`launch()` 起完线程就返回，
        运行时的在飞读数毫秒级回到空，光靠那一格判不出「另一趟还在飞」。动作本体先调本
        方法，拒跑档因此如实答得出来（409），而不是回一个「已起」的假回执。
        """
        target = str(session_dir or "")
        if not target:
            raise ValueError("claim() 需要场次目录（收到空值）")
        self._mark_running(target, force=False)

    def launch(self, session_dir: str, *, force: bool = False, claimed: bool = False) -> None:
        """`meeting post` 的离线尾巴与「直接出纪要」按钮调它，**返回即算启动成功**（不等它跑完）。

        `session_dir` 是这一场的场次目录。**起线程之前先把「这一层在飞」同步写到盘上**
        （`meeting/minutes/status.py::mark_running()`：那一版 `state = "running"` 在本方法
        返回之前就已经落定），随后起一条 daemon 线程跑
        `meeting.minutes.runner.run(session_dir)`，随即返回。

        那一版落盘是**跨进程**看得见的：按钮那一趟住在服务进程里，用户随后手敲的
        `meeting post` 是另一个进程 —— 后者读到这一版就按现成的 `running` 拒跑档收场
        （`meeting/cli.py` 的 `_layer_is_running()` 与 `meeting/offline/runner.py` 的
        `_minutes_gate()` 判据原样在场），同一场因此挂不上两个子会话。

        盘上已经停着一版 `running`（另一趟正在飞：它自己那一版就是在这里落的）时，
        这一趟**当场抛 `AlreadyRunningError`**：没有线程、没有子会话被拉起来。`mark_running()`
        里的 claim 锁把「读现状 + 写 running」裹成一段原子操作，两趟几乎同时进来时只有一个
        拿得到锁，另一趟读到在飞的那一版。

        `force = True`（`meeting post <目录> --force` 那一趟）**跳过那条在飞判据**：
        `--force` 的既有语义就是无视现状重跑，它是 `running` 那一档给出的出路，这一格
        因此照常放行（不是堵死它）。

        `claimed = True` 表示**调用方已经同步调过 `claim()`**（`post_minutes` 那一趟就是
        这么接的）：盘上那一版 `running` 已经落稳，这里不再重复 claim —— 重复一次会读到
        自己刚写下的那一版、把这一趟自己拒掉。
        """
        target = str(session_dir or "")
        if not target:
            # 没有场次目录就没有落点：如实报错（调用方那一层用 try/except 兜住，只记日志）
            raise ValueError("launch() 需要场次目录（收到空值）")
        if not claimed:
            self._mark_running(target, force=force)
        thread = threading.Thread(
            target=self._run,
            args=(target,),
            name=THREAD_NAME,
            daemon=True,
        )
        thread.start()

    def _mark_running(self, session_dir: str, *, force: bool = False) -> None:
        """把「这一层在飞」写到盘上（**函数体内 import**：本模块顶层只有标准库）。

        `branch` 取 `minutes.hook` 的生效取值映射出来的那一条（与线程体那一版同一个求法，
        因此两版只差 `mode` / `artifacts` / `dsh` 那几格的真值）：**那一刻还没探 DSH web
        服务端在不在跑**，因此这一版记的是配置里写的那个词，`runner` 探完之后落的下一版
        才记实际走的 `web` / `headless`。已经有一版 `running` 在盘上、而这一趟又没带
        `--force` 时，`mark_running()` 抛 `AlreadyRunningError`，这里**原样上抛**：
        拒绝这一趟就是它的语义，调用方据此按拒跑档收场。
        """
        from meeting.minutes import launch as launch_module
        from meeting.minutes import status as status_module

        status_module.mark_running(
            session_dir,
            branch=launch_module.configured_branch(minutes_hook_selector()),
            force=force,
        )

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


def minutes_hook_selector() -> str:
    """`minutes.hook` 的生效取值（`auto` / `null` / `dev` / `module:Class`）。

    `meeting.config` 在**函数体内** import：本模块顶层的惰性纪律一个字不动
    （`import meeting.backends.minutes` 仍然只拉标准库）。配置层抽风时退回缺省档 ——
    这一格只喂「实际走的启动分支」那一个读数，值不对不该挡住拉起。
    """
    try:
        from meeting import config as config_module

        value = config_module.get("minutes.hook")
    except Exception:  # pragma: no cover - 配置层还没起来时走缺省档
        return DEFAULT_HOOK
    return DEFAULT_HOOK if value is None else str(value)


__all__ = [
    "AlreadyRunningError",
    "CHILD_FLAG",
    "DEFAULT_HOOK",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_RUNNING",
    "THREAD_NAME",
    "MinutesHook",
    "minutes_hook_selector",
]
