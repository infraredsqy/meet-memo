"""``meeting.service.runtime`` 模块级的公共冻结名字与 daemon 线程池类（叶子模块）。

存放收尾执行器线程数常量、daemon 工作线程池 ``_DaemonThreadPoolExecutor`` 与动作层冻结名
（``ACTION_CODES`` / ``ACTION_NAMES`` / ``SESSION_MODES`` / ``DEFAULT_SESSION_MODE`` / ``ActionError``），
供 runtime 各 mixin 模块原样引用；本文件不 import 任何 ``meeting.*``。
"""
from __future__ import annotations

import threading
import weakref
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
from concurrent.futures.thread import _worker as _threadpool_worker
from typing import Any, TypedDict
from typing import TypeGuard
#: 收尾三步的同步调用池里的工作线程条数（审查问题 m7）。
#:
#: 三步在**编排**上严格串行，而线程占用上会叠：某一步被按剩余预算判超时之后，它的等待被
#: 放弃、已提交的那次同步调用照跑到底，后续步的调用因此需要另有一条线程可用（说明与判据见
#: `_DaemonThreadPoolExecutor` 与 `tests/test_drain.py` 的 ⑤k）。取 4 的来路：收尾三步各有同步
#: 调用，最坏情形是「前面几步被放弃后仍在飞 + 当前步要开跑」，4 条线程把这一幕整个装下，
#: 同时仍是一个小规模、daemon、不登记 `_threads_queues` 的池。
DRAIN_EXECUTOR_MAX_WORKERS = 4


class _DaemonThreadPoolExecutor(_ThreadPoolExecutor):
    """工作线程是 **daemon** 的线程池（同步实现丢进工作线程的那些调用专用）。

    三处调用点：**收尾三步**（`_drain_wait()`）、**会中总结那一次模型调用**
    （`_summary_wait()`，`1987cb20` phase2 的复审裁定）、**会中模型的收 / 放**
    （`_submit_model_lifecycle()`，`10770b41`）。三处各起一台池（收尾那台 4 条线程、
    会中总结那台按 `SUMMARY_EXECUTOR_MAX_WORKERS`、会中模型的收 / 放那台按
    `MODEL_EXECUTOR_MAX_WORKERS` 取 1 条），工作线程都是本类的 daemon 线程。

    **为什么必须是 daemon**（phase3 §3.5 行为表第二行「**立即中止**」）：第二次 `Ctrl+C`
    的语义是「先落盘（`draining` + `interrupted: true`）、再 `cancel()` 收尾 task、随即请求
    关停」。`cancel()` 中止的是 `_drain_wait()` 里那次 `wait_for` 的**等待**，已经交给工作
    线程的那次同步调用（`launch()` / `finalize()` / 场次 log 的 flush 与摘除）**照跑到底**；
    解释器收尾时等这些工作线程，于是「立即中止」被在飞的同步步拖着走（审查实测：第二次
    `Ctrl+C` 之后进程还要挂 1.5–3.0 秒）。把收尾三步的工作线程换成 daemon，解释器收尾不再
    等它们，「立即中止」的时延因此与在飞步骤的剩余时长**无关**。
    （判据见 `tests/test_shutdown_signal.py` 第 ③ 条：在飞的那一步阻塞
    `SHUTDOWN_BLOCKING_HOOK_SECONDS` = 2.0 秒，第二次按下之后的退出时延按
    `INTERRUPT_EXIT_LATENCY_LIMIT_SECONDS` = 1.0 秒断；修好之后实测 0.17–0.44 秒。）

    **不破坏「先落盘、再中止」的顺序**：`mark_interrupted()` 那一版盘上内容由事件循环
    这一侧的 `cancel()` **之前**写完（`SessionStore.write_session_json()` 的临时文件 +
    `os.replace`）。daemon 线程只可能让「收尾 `log` 步的结束标记」少写一半 —— 那一半不是
    重启自洽的读入口（重启读的是 `session.json`），而 `session.json` 的每一版都是原子写，
    因此**不会留下半截坏文件**（同目录的 `.tmp` 残留可接受，下一次原子写会覆盖它）。

    **daemon 之外还要做的一件事（实测过才写的）**：`concurrent.futures.thread` 在模块
    import 时经 `threading._register_atexit()` 登记了 `_python_exit()`，它在解释器退出时
    把**每一台**登记过的池的工作线程都 `join()` 一遍 —— 这一遍**不看 `daemon` 标记**，
    于是「只把线程设成 daemon」并不够（实测：8 秒的在飞同步步仍然把进程拖满 8 秒，
    改成下面的写法之后同一现场 0.5 秒内收场）。
    本类因此**不把工作线程登记进 `_threads_queues`**：退出时没人给它发哨兵、它是 daemon、
    `threading._shutdown()` 只等非 daemon 线程，进程于是立刻退；而正常路径上的收场由
    `shutdown(wait=False)` 负责（它自己会往队列里放哨兵、不需要那张登记表）。

    **只给收尾三步用**：事件循环的默认执行器一律不动（`asyncio.to_thread()` 那一路照旧），
    于是「收尾之外还有谁在用线程池」这件事不受本类影响。

    **三处调用点各起一台池（`__init__` 的两个入参因此可传）**：收尾三步那台取
    `DRAIN_EXECUTOR_MAX_WORKERS` = 4、线程名前缀 `drain-step`；会中总结那台取
    `SUMMARY_EXECUTOR_MAX_WORKERS` = 2、前缀 `summary-call`；会中模型「收 / 放」那台取
    `MODEL_EXECUTOR_MAX_WORKERS` = 1、前缀 `model-lifecycle`。三台**互不占线程**：
    会中总结按下的那几秒里，收尾三步的调用照旧有四条线程可用，反过来也一样。

    **工作线程取 `DRAIN_EXECUTOR_MAX_WORKERS` 条（审查问题 m7 的直接修复）**：三步的调用
    在**编排**上是**严格串行**的（§3.1 的顺序写死），而「被放弃的步」与「后续步」在**线程
    占用**上会叠在一起。某一步被 `wait_for` 按剩余预算判超时之后，它的**等待**被放弃，
    已经提交给工作线程的那一次同步调用却照跑到底（§3.5 明写这一条：`cancel()` 中止的只是
    等待，那次调用仍跑到返回）。线程数只有 1 时，后续步骤的调用只能排在它后面等 —— 收尾的
    **后续步**于是被前一步的剩余时长顶住，`log` 那一步会以「拿到
    `DRAIN_LOG_MIN_BUDGET_SECONDS` 却在队列里等光」的形式落 `failed`，本该「正常收尾」的一档
    被拖成收尾失败档。池扩到能容纳「被放弃的那一步 + 后续步」并行推进的规模之后，一步超时
    不再堵住后续步的**可用性**（判据见 `tests/test_drain.py` 的 ⑤k）；「一趟收尾的等待总量
    以那条 deadline 为界」这条口径一个字不改 —— 它由 `_drain_wait()` 的等待上限保证，
    与线程数无关。
    """

    def __init__(self, *, max_workers: int = DRAIN_EXECUTOR_MAX_WORKERS,
                 thread_name_prefix: str = "drain-step") -> None:
        super().__init__(max_workers=max_workers, thread_name_prefix=thread_name_prefix)

    def _adjust_thread_count(self) -> None:
        """与标准库同一套起线程逻辑，两处不同：线程是 **daemon**、**不登记**进退出时的 join 表。

        线程名与弱引用回调逐行照抄标准库（少了弱引用回调，池被回收时工作线程不会醒）。
        """
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = "%s_%d" % (self._thread_name_prefix or self, num_threads)
            thread = threading.Thread(
                name=thread_name,
                target=_threadpool_worker,
                args=(
                    weakref.ref(self, weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                ),
                daemon=True,
            )
            thread.start()
            self._threads.add(thread)
            # 标准库在这里还会写 `_threads_queues[thread] = self._work_queue`；
            # 本类**故意不写**（见类说明：那一张表是解释器退出时无条件 join 的来源）。


# ---------------------------------------------------------------------------
# 动作层的错误（`code` 取值只用 `ACTION_CODES` 里已冻结的那九个串，不自造）
# ---------------------------------------------------------------------------

#: 动作失败的 `code` 全集（phase2 §3.6.2；`1987cb20` phase2 补上会中总结那两档）
ACTION_CODES: tuple[str, ...] = (
    "already_recording",
    "draining",
    "capture_unavailable",
    "not_recording",
    "invalid_setting",
    "persist_failed",
    "action_unavailable",
    "transcript_unavailable",
    "summary_failed",
)

#: 动作名清单（phase2 §3.13 + `dca4539b` 的 `reprobe` + `1987cb20` phase2 的 `summarize`
#: + 工作台收编节点的 `workspace_open` / `workspace_remove` + 会后两个按钮
#: `post_offline` / `post_minutes` + 暂停节点的 `pause` / `resume` / `set_source`
#: + 人名节点的 `speaker_rename` + 打断会后的 `post_stop`
#: + 转码独立工序的三只动作 `transcode_run` / `transcode_pause` / `transcode_resume`）
ACTION_NAMES: tuple[str, ...] = (
    "start",
    "stop",
    "mute_session",
    "settings_update",
    "reprobe",
    "dev_inject",
    "summarize",
    "workspace_open",
    "workspace_remove",
    "post_offline",
    "post_minutes",
    "post_stop",
    "transcode_run",
    "transcode_pause",
    "transcode_resume",
    "pause",
    "resume",
    "set_source",
    "speaker_rename",
)

#: `start` 的 `mode` 取值域与缺省
SESSION_MODES: tuple[str, ...] = ("both", "mic", "loopback")
DEFAULT_SESSION_MODE = "both"

#: 三种活各自的人话名（拒跑句与读数句用；`transcode` 是转码独立工序那一档）
KIND_LABELS: dict[str, str] = {
    "offline": "会后精细稿",
    "minutes": "纪要",
    "transcode": "转码",
}


def name_of_kind(kind: str) -> str:
    """一种活的人话名（不认识的取值原样交回，不猜）。"""
    return KIND_LABELS.get(str(kind), str(kind))


class ActionError(Exception):
    """一次动作的失败（带机器可读的 `code`、给人看的 `message` 与 `detail`）。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status = int(status)
        self.detail = dict(detail or {})
        super().__init__(message)


# ---------------------------------------------------------------------------
# 会后一趟的现场读数（`ServiceRuntime.post_job` 这一格的形状）
# ---------------------------------------------------------------------------


class PostJob(TypedDict):
    """一趟会后 / 转码活在飞时的现场（**内存事实**，按 `(kind, dir)` 一趟一格）：

    不写快照、不落盘 —— 跨进程的那一格在 `meeting/service/postjobs.py`（只记 offline /
    minutes 两档）。七个键：`thread` / `cancel` 由起线程那一步给（`_start_post_job()`）；
    `progress` 由离线那趟自己塞（`OfflineProgressLedger`，本叶子模块不 import 会后那一包，
    标 `Any`）；`cancel` 是 offline 档的 `Event`（纪要那趟 `None`；转码档是**暂停旗标**）。
    标 `Any` 的格子照现状的访问姿势放宽（`thread` 判 `.is_alive()`、`cancel` 过
    `isinstance` 一道闸）。

    **进程级一刀切已废**（转码搬出收尾节点）：同一时刻可以有多趟在飞（不同场次、不同
    种类），唯一的槽判据是 `(kind, dir)` —— 同一种重活在同一场上只能有一趟。
    """

    kind: str
    dir: str
    thread: Any
    started_at: str
    finished_at: str | None
    progress: Any
    cancel: threading.Event | None


def _slot_alive(job: "PostJob | dict[str, Any] | None") -> bool:
    """一格 `(kind, dir)` 登记还活着吗：是 dict、`thread` 在、线程没死。

    `runtime_post` / `runtime_transcode` 两个 mixin 共用这一份判据（「在飞」的读法
    全仓只有这一处，不另抄）。
    """
    if not isinstance(job, dict):
        return False
    thread = job.get("thread")
    return thread is not None and thread.is_alive()


def _job_matches(job: PostJob | None, directory: str) -> TypeGuard[PostJob]:
    """会后那趟的现场还记不记**这一场**：是 dict、`dir` 字符串化后等于 `directory`。

    `_post_job_progress()` / `_post_flight_view()` / `_offline_job_running()` 三处逐字
    同形的公共判据（isinstance + dir 相等）收在这一处；各处余下的判据——
    `kind` 档位、`thread.is_alive()` 在飞——**留在各自那处**（三处的完整判据不同形，
    复审 #2 的裁定：只抽公共部分，行为逐字不变）。
    """
    return isinstance(job, dict) and str(job.get("dir")) == str(directory)
