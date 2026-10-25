"""收尾与关停 mixin：收尾三步编排、强制打断与服务关停（自 meeting/service/runtime.py 机械拆分，逐行照抄）。"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Callable

from meeting import logging_setup
from meeting.asr import pipeline as asr_pipeline
from meeting.backends.registry import BackendResolution
from meeting.service.runtime_shared import ActionError, _DaemonThreadPoolExecutor
from meeting.session import steps_from_ended
from meeting.state import (
    SessionInfo,
    begin_drain_step,
    finish_drain,
    finish_drain_step,
    now_iso,
)

_log = logging.getLogger("meeting.service.runtime")


def _interrupted_message() -> str:
    """强制打断那一档的 log 前缀：**冻结常量原样 + 真实按下次数**。

    §3.5 的行为表把「第一次按下、当时已经停在 `ended·draining`」也划进「等同第二次 `Ctrl+C`」
    这一档，于是常量 `DRAIN_INTERRUPTED_MESSAGE` 末尾那句「用户第二次 Ctrl+C」在用户只按了
    一次时与事实对不上。常量**逐字冻结、不改**；这一档的两行 log 都在它后面补上真实次数
    （取 `meeting/service/app.py` 的按下次数计数器），记录因此不失真。
    """
    from meeting.service import app as service_app

    press = service_app.shutdown_press_count()
    detail = (
        f"本次是第 {press} 次按下"
        if press
        else "本次没有经过信号处理函数（按下次数为 0）"
    )
    return f"{service_app.DRAIN_INTERRUPTED_MESSAGE}（{detail}）"



# 收尾预算（phase3 §3.3；名字由 phase2 冻结）：公式 `budget = 30 + backlog ×
# (segment_timeout + 5)` 原样落地，下限 30 秒、封顶 300 秒；三个数硬编码、不开配置键。

#: 预算下限（秒）
DRAIN_BUDGET_MIN_SECONDS = 30

#: 预算封顶（秒）
DRAIN_BUDGET_MAX_SECONDS = 300

#: 公式里每段的那一笔额外开销（秒）：`backlog × (segment_timeout + 5)`
DRAIN_BUDGET_SEGMENT_OVERHEAD_SECONDS = 5

#: 转写后端缺席时的 `backlog` 缺省
DRAIN_DEFAULT_BACKLOG = 0

#: 转写后端缺席时的 `segment_timeout` 缺省（秒）
DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS = 20.0

#: `log` 步的 flush 与摘除至少拿到的预算（秒）。
#: 共享预算的唯一例外：`log` 在任何构建里都有真实实现、要做 IO，末尾那步把 deadline 耗尽时
#: 否则会把「正常收尾」拖成收尾失败档。**与 `DRAIN_BUDGET_SEGMENT_OVERHEAD_SECONDS` 同值但
#: 语义无关，不许合并**——那个 5 是公式里的每段开销（随积压被乘出来），本常量是共享预算
#: 之外给 `log` 的兜底；合成一个名字的话公式与兜底会一起静默改掉。
DRAIN_LOG_MIN_BUDGET_SECONDS = 5

#: `_drain_wait()` 的轮询/收尾间隔（秒）：等收尾落定时的步进
DRAIN_SETTLE_POLL_SECONDS = 0.05


class DrainLimitExceeded(TimeoutError):
    """某一步超出这一趟收尾的共享预算。

    **只表达超时这一件事**：步骤自身抛出的异常不由本异常承载（那些异常原样上抛到该步的
    `failed` 登记，见 phase3 §3.1 第 5 条与 §3.0 的边界）。
    """


def compute_drain_budget(backlog: int, segment_timeout: float) -> float:
    """`budget = 30 + backlog × (segment_timeout + 5)`（下限 30、封顶 300）。

    输入来自转写后端的 `backlog_depth()` / `segment_timeout()`；转写后端缺席时调用方按
    `DRAIN_DEFAULT_BACKLOG` / `DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS` 取缺省
    （phase3 §3.3；这两个缺省值与原公式出处是方案 §5.3 行 250 / §7.2 行 338）。

    **公式本体在 `meeting/asr/pipeline.py`**（积压量是转写域的量，那个模块也用它算
    自己 `stop()` 的收尾预算），这里只做转交：同一趟收尾的两处读数因此永远同源。
    下面三个常量与那边逐字同值，是**跨 Phase 冻结的名字**（phase3 按它们读记录）。
    """
    return asr_pipeline.compute_drain_budget(backlog, segment_timeout)


class RuntimeDrainMixin:
    """收尾与关停 mixin。"""

    # 动作错误（拒绝路径的 detail 三件套）

    def _failed_steps_detail(self) -> list[dict[str, Any]]:
        if self.snapshot.ended is None:
            return []
        return [
            {"id": step.id, "error": step.error}
            for step in self.snapshot.ended.steps
            if step.status == "failed"
        ]

    def _already_recording_error(self) -> ActionError:
        session = self.snapshot.session
        elapsed = 0.0
        if session is not None:
            elapsed = (
                session.elapsed_seconds
                if session.frozen
                else self._session_elapsed_seconds(session)
            )
        return ActionError(
            "already_recording",
            "已经有一场在录",
            status=409,
            detail={
                "session_id": None if session is None else session.id,
                "dir": None if session is None else session.dir,
                "elapsed_seconds": round(float(elapsed), 3),
            },
        )

    def _draining_error(self, session: SessionInfo | None) -> ActionError:
        return ActionError(
            "draining",
            "上一场还在收尾（或收尾失败后停留），这一场不能开",
            status=409,
            detail={
                "session_id": None if session is None else session.id,
                "dir": None if session is None else session.dir,
                "failed_steps": self._failed_steps_detail(),
                "log_path": self.log_path,
            },
        )

    def _reject_start_during_drain(self) -> None:
        """`start` 的收尾闸（收窄后）：只挡两档。

        本进程收尾 task 真在飞且 flush 尚未落定 → 409（归档还没落稳，音频不可再生，
        `docs/design-intent.md` 第 1 条）；历史失败档（收尾已走完、停在 `ended·draining`
        的失败现场）→ 409，失败步明细照旧带回。flush 已落定而尾巴（log / post）还在飞的
        一档放行（转码更不阻挡）——放行那一档由 routes 经 `settle_drain_tail()` 等尾巴落定。
        """
        if self.drain_in_progress():
            if not self.flush_settled():
                raise self._draining_error(self.snapshot.session)
            return
        if self._drain_failed_face():
            raise self._draining_error(self.snapshot.session)

    async def settle_drain_tail(self, *, timeout: float | None = None) -> None:
        """routes 的 `start` 在动手前等收尾尾巴落定（flush 已落定、task 还在飞那一档）。

        收尾编排协程与动作处理跑在同一个事件循环上，但每一步的调用都提交到工作线程——
        不等尾巴就开新一场，半路步骤会踩到已被新一场换掉的快照。只等不拒：尾巴落定
        （常态几百毫秒）就放行；等到上限（封顶 30 秒）还在飞才如实拒——那是「收尾卡死」
        的另一档病。
        """
        if not self.drain_in_progress() or not self.flush_settled():
            return
        if timeout is None:
            timeout = min(30.0, float(self._drain_wait_timeout()))
        await self._wait_drain_settled(timeout=timeout)
        if self.drain_in_progress():
            raise ActionError(
                "draining",
                "上一场收尾的尾巴迟迟不落定：稍后再试（或强制打断收尾）",
                status=409,
                detail={"log_path": self.log_path},
            )

    # 收尾编排（三步登记 + 终态判定；预算与失败出路的完整语义归 phase3）

    async def drain(self) -> str:
        """收尾协程（普通协程、持有 task 句柄、可被 `cancel()`）。

        编排 `flush → log → post`（转码已摘出收尾序列，flush 落定即自动在后台另起）；每步
        进入与落定各写一次盘、各提交一次，全部落定后交给 `finish_drain()` 判终态。被取消时
        经 `_settle_step(..., cancelled=True)` 按收尾步骤表落盘（在飞那步照实记 `running`、
        不伪造终态），随后同样走 `finish_drain(cancelled=True)` 出口；`frozen_ended` /
        `frozen_interrupted` 保证先落盘那版 `draining` + `interrupted: true` 不被抹掉
        （§3.5 的三条不覆盖约束）。

        自适应预算（phase3 §3.3）：开头按积压量算总上限 `compute_drain_budget()`，各步共享
        同一条 deadline（经 `_drain_wait()` 包在剩余预算里），绝不阻塞事件循环；唯一例外是
        `log` 步的 flush 与摘除，至少拿到 `DRAIN_LOG_MIN_BUDGET_SECONDS`。
        """
        try:
            await self._run_drain_steps()
        except asyncio.CancelledError:
            _log.warning("收尾被取消：按收尾步骤表落盘，随后走 finish_drain() 判终态")
            self._settle_cancelled_step()
            self._finish_drain(cancelled=True)
            raise
        finally:
            self._drain_task = None
        return self.snapshot.ended.phase if self.snapshot.ended else "draining"

    def _settle_cancelled_step(self) -> bool:
        """取消时的登记（§3.5 第 3 条）：把在飞那一步经 `_settle_step(..., cancelled=True)`
        记一笔，让「不许覆盖 `interrupted` / `state` / `ended_at`」三条真的被执行到。

        不伪造终态是关键：在飞那步照实记 `running`，已落定的不改、`waiting` 维持。没有
        `ended`（重启现场）或各步已全部落定时不补这一笔。在飞的是 `post` 也照实记 `running`
        ——终态判定那格收到取消读数一律保持 `draining`，不会被误读成「收尾走完了」。
        """
        ended = self.snapshot.ended
        if ended is None:
            return False
        in_flight = [step for step in ended.steps if step.status == "running"]
        if len(in_flight) != 1:
            _log.info(
                "取消时没有「正在跑」的收尾步骤要记（running 的步骤数=%s）：已落定的保持原样",
                len(in_flight),
            )
            return False
        step = in_flight[0]
        _log.warning(
            "取消时的登记：把在飞那一步 %s 按它此刻真实的状态记一笔（running 照实记 running，"
            "不伪造终态；已落定的步骤与盘上那一版 interrupted/state/ended_at 都不覆盖）",
            step.id,
        )
        self._settle_step(
            step.id,
            "running",
            "收尾被取消：这一步还没跑完（照实记 running，不伪造终态）",
            None,
            None,
            cancelled=True,
        )
        return True

    def _drain_inputs(self) -> tuple[int, float]:
        """这一趟收尾的预算输入：`(backlog, segment_timeout)`（缺省按 §3.3 取）。"""
        backlog = DRAIN_DEFAULT_BACKLOG
        segment_timeout = DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS
        backend = self.transcriber_backend
        if backend is None:
            return backlog, segment_timeout
        try:
            backlog = int(backend.backlog_depth())
        except Exception:
            _log.exception(
                "转写后端读不出 backlog_depth()（按缺省 %s 算预算）", DRAIN_DEFAULT_BACKLOG
            )
            backlog = DRAIN_DEFAULT_BACKLOG
        try:
            segment_timeout = float(backend.segment_timeout())
        except Exception:
            _log.exception(
                "转写后端读不出 segment_timeout()（按缺省 %ss 算预算）",
                DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS,
            )
            segment_timeout = DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS
        return backlog, segment_timeout

    def _remaining_budget(self, deadline: float | None) -> float:
        """这一趟还剩多少预算（秒）：`deadline` 减此刻单调钟，下限夹 0。

        各步共享同一条 deadline（§3.3）。`deadline is None` 在这里当场抛 `ValueError`、
        不给兜底秒数：上限只能来自 `compute_drain_budget()`，退成 `inf` 是静默退化、
        退成别的保守值会把「谁忘了传」盖住。
        """
        if deadline is None:
            raise ValueError(
                "收尾等待上限必须是一条具体的 deadline（各步共享的那条）："
                "上限只能来自 compute_drain_budget()，收到 None 时这里不会给出任何兜底秒数"
            )
        return max(0.0, deadline - time.monotonic())

    def _drain_wait_timeout(self) -> float:
        """「等这一趟收尾落定」的上限（第一次 `Ctrl+C` 的等待）。

        与本次收尾同源：直接沿用 `drain()` 留下的 `_drain_budget` / `_drain_deadline`，
        不把 `_drain_inputs()` 读第二遍——真转写后端的积压量随收尾下降，再读一次会算出
        更短的上限，等待会在收尾自己 deadline 之前放弃、留下 `draining` 现场而退出码看着
        还是干净档。取值 = 本次 deadline 剩余时间 + `log` 步那条最小预算。
        """
        deadline = self._drain_deadline
        budget = float(
            DRAIN_BUDGET_MIN_SECONDS if self._drain_budget is None else self._drain_budget
        )
        remaining = budget if deadline is None else self._remaining_budget(deadline)
        return remaining + float(DRAIN_LOG_MIN_BUDGET_SECONDS)

    def _drain_executor_or_create(self) -> _DaemonThreadPoolExecutor:
        """取（必要时建）收尾各步的同步执行器：工作线程是 daemon。

        用 `loop.run_in_executor(<这个执行器>, ...)` 而不是 `asyncio.to_thread()`：后者走
        事件循环的默认执行器（非 daemon），第二次 `Ctrl+C` 之后解释器会 join 它，进程被
        在飞的同步步拖住（§3.5 行为表第二行的「立即中止」）。
        """
        if self._drain_executor is None:
            self._drain_executor = _DaemonThreadPoolExecutor()
        return self._drain_executor

    async def _drain_wait(
        self,
        step_id: str,
        call: Callable[[], Any],
        *,
        deadline: float,
        min_budget: float = 0.0,
        invoke: bool = False,
    ) -> tuple[Any, float]:
        """把一步的调用包在剩余预算里：`asyncio.wait_for(<该步调用>, max(<剩余预算>, min_budget))`。

        各步调用无一例外都经这里，等待总量以那条 deadline 为界。`deadline` 必传
        （`None` 时 `_remaining_budget()` 当场抛错）；`min_budget` 只给 `log` 步用。

        同步与协程两条路受同一个预算：同步实现（`invoke=True`）经收尾专用 daemon 执行器
        提交到工作线程再包进 `wait_for`——直接跑在事件循环上会让收尾期间 `/api/state`
        不应答；返回协程的调用经 `ensure_future` 包成任务在循环侧推进。同步 callable 忘传
        `invoke=True` 在开跑之前就刺耳：当场抛 `TypeError`（否则那个同步实现会在事件循环上
        整段跑完，一个 3 秒的 sleep 能把 `/api/state`、SSE 与动作端点一起钉停 3 秒）。

        本包装层只负责超时（超出上限抛 `DrainLimitExceeded`）；步骤自身的异常原样上抛，
        冒到该步的 `failed` 登记。返回 `(返回值, 实测耗时秒数)`。
        """
        if not invoke and not inspect.iscoroutinefunction(call):
            raise TypeError(
                f"收尾步骤 {step_id} 的调用是一个同步可调用对象，调用点必须传 invoke=True"
                "（同步实现要提交到收尾专用的 daemon 工作线程）：直接在这里调用它会把事件循环"
                "整段钉住，收尾期间 /api/state、SSE 与 POST /api/actions 会一起停摆"
            )
        budget = max(float(min_budget), self._remaining_budget(deadline))
        started = time.monotonic()
        try:
            # 两步分开写：先拿到「这一趟要 await 的东西」，再把它**无一例外**包进
            # `asyncio.wait_for(...)` —— 同步路（daemon 执行器的线程 Future）与协程路
            # （协程 / 任务）因此受同一个 `budget` 约束，没有哪条路能绕过去。
            if invoke:
                loop = asyncio.get_running_loop()
                pending: Any = asyncio.wrap_future(
                    loop.run_in_executor(self._drain_executor_or_create(), call)
                )
            else:
                pending = call()
                if asyncio.iscoroutine(pending):
                    pending = asyncio.ensure_future(pending)
            awaited = await asyncio.wait_for(pending, timeout=budget)
        except asyncio.TimeoutError as exc:
            elapsed = time.monotonic() - started
            raise DrainLimitExceeded(
                f"超出收尾预算（budget={round(budget, 3)}s，实测耗时 {round(elapsed, 3)}s）"
            ) from exc
        return awaited, max(0.0, time.monotonic() - started)

    async def _run_drain_steps(self) -> None:
        backlog, segment_timeout = self._drain_inputs()
        budget = compute_drain_budget(backlog, segment_timeout)
        deadline = time.monotonic() + budget
        # 这一趟的预算与 deadline 都留给运行时：「等收尾落定」的上限与本次收尾同源
        # （`_wait_drain_settled()` 经 `_drain_wait_timeout()` 沿用这笔预算，不再读第二次输入）
        self._drain_budget = budget
        self._drain_deadline = deadline
        _log.info(
            "收尾开始：backlog=%s segment_timeout=%ss -> budget=%ss（下限 %ss、封顶 %ss）；"
            "各步共享这一条 deadline（log 步的 flush 与摘除另保底 %ss）",
            backlog, segment_timeout, budget,
            DRAIN_BUDGET_MIN_SECONDS, DRAIN_BUDGET_MAX_SECONDS, DRAIN_LOG_MIN_BUDGET_SECONDS,
        )
        try:
            await self._drain_flush(deadline)
            await self._drain_log(deadline)
            await self._drain_post(deadline)
        finally:
            self._drain_deadline = None
        self._finish_drain()
        # 收尾走完之后的最后一趟落盘：盘上那一版与终态一致
        # （含 `state`、`drain.steps` 与结束时间）
        self._write_session(ended_at=now_iso())
        _log.info(
            "收尾终态已落盘：ended.phase=%s（这一趟实测耗时 %ss）",
            self.snapshot.ended.phase if self.snapshot.ended else "?",
            round(max(0.0, time.monotonic() - (deadline - budget)), 3),
        )

    async def _drain_flush(self, deadline: float) -> None:
        """第 1 步 `flush`：停采集、落盘（后端 `finalize()`）；采集替身记 `skipped`。

        **flush 落定那一刻，转码在后台自动起**（转码已摘出收尾序列：它是这一场的独立
        工序，`meeting/capture/transcode.py`；不占收尾预算、不挡 start 与会后动作）。
        采集替身（没有 transcode 能力）那一档不起 —— 没有真实 PCM 可转。
        """
        step_id = "flush"
        self._begin_step(step_id, "停采集并把这一场的归档落盘（后端 finalize()）")
        resolution = self.capture_resolution
        if resolution is None or resolution.skipped() or not self.capture_capabilities.get("finalize"):
            self._skip_step(
                step_id,
                "这一场没有真实归档要落（采集替身不写真实音频；finalize 归 dca4539b）",
                resolution,
            )
            return
        try:
            _value, elapsed = await self._drain_wait(
                step_id, self.capture_backend.finalize, deadline=deadline, invoke=True
            )
        except Exception as exc:
            self._fail_step(step_id, exc, resolution, "采集归档落盘失败")
            return
        self._done_step(step_id, "归档已落盘（finalize() 返回）", resolution, elapsed=elapsed)
        # 落盘一落定，转码就自动在后台接着做（起不等于挡路：它不占收尾预算，也不挡任何人）
        self._start_transcode_after_flush()

    def flush_settled(self) -> bool:
        """收尾的 `flush` 步是否已落定（`done` / `failed` / `skipped` 都算终态）。

        `start` 的收尾闸判据用它：收尾 task 还在飞而 flush 已落定时，新一场**放行**
        （盘上落盘已完成、音频已安全；剩下的 log / post 两步归收尾自己的尾巴）。
        """
        ended = self.snapshot.ended
        if ended is None:
            return False
        step = ended.drain_step("flush")
        return step.status in ("done", "skipped", "failed")

    async def _drain_log(self, deadline: float) -> None:
        """第 3 步 `log`：写 `session.json` 的结束标记，并 flush / 写结束标记 / 摘掉场次 log。

        这一格**在任何构建里都有真实实现**，所以它**永远不该出现 `skipped`**；写失败
        （例如磁盘满）是「有实现却没做成」，按 §3.0 的裁决记 **`failed`** 并如实上报
        （**不中断录音**：此刻本来就已经在收尾了，进程照常活着、页面照常看得见）。

        **预算上的例外（§3.1 第 3 条）**：`log` 步的 flush 与摘除至少拿到
        `DRAIN_LOG_MIN_BUDGET_SECONDS`，不受剩余预算约束 —— 否则前面几步把 deadline 用光时，
        本该「正常收尾」的一档会被拖成收尾失败档。
        """
        step_id = "log"
        self._begin_step(step_id, "写这一场的结束标记（会话文件 + 场次日志）")
        started = time.monotonic()
        try:
            # `ended_at` 不在这里写（§2.4 的只读契约清单）：它只在收尾走到终态时才落值，
            # 终态由 `finish_drain()` 一处判定，之后 `_run_drain_steps()` 末尾那次落盘才是
            # 唯一写入点。这里先写会出现「state=draining + ended_at 有值」的瞬态，
            # 与「强制打断不写 ended_at」也读不顺。
            self._write_session()
            _log.info("场次结束标记已写入 session.json（会话文件这一半）")
        except Exception as exc:
            self._fail_step(step_id, exc, None, "写这一场的结束标记失败")
            return

        state = self._session_state_on_disk()
        steps = [step.status for step in (self.snapshot.ended.steps if self.snapshot.ended else [])]
        try:
            # **`log` 步也经 `_drain_wait()`**：同步 IO 一律提交到工作线程再连同等待一起
            # 包进那一个等待上限里，`log` 步的实际耗时因此以它返回的实测值为准。
            problem, log_elapsed = await self._drain_wait(
                step_id,
                lambda: logging_setup.write_session_end_marker(state=state, steps=steps),
                deadline=deadline,
                min_budget=float(DRAIN_LOG_MIN_BUDGET_SECONDS),
                invoke=True,
            )
        except asyncio.CancelledError:
            # 取消是强制打断路径（§3.5）：登记交回取消路径，这里不吞
            raise
        except Exception as exc:
            self._fail_step(step_id, exc, None, "场次日志的 flush / 结束标记 / 摘除失败")
            return
        if problem:
            self._fail_step(
                step_id,
                OSError(problem),
                None,
                f"场次日志写失败（如实上报，不静默）[{problem}]",
            )
            return
        self._settle_step(
            step_id,
            "done",
            "场次日志已 flush、结束标记已写、handler 已摘掉（结束标记是这一场的最后一行）",
            None,
            None,
            elapsed=max(float(log_elapsed), max(0.0, time.monotonic() - started)),
        )

    async def _drain_post(self, deadline: float) -> None:
        """`post` 步：让会中模型退出显存，把会后留给用户按按钮。

        按「结束录音」之后什么都不发生（R26 / `docs/design-intent.md` 第 10 条）：一个会后
        动作都不启动，会后离线链与纪要层各有各的按钮。这一格如实停在 `waiting`（界面上
        「等你动手」的诚实读数）；释放显存照做（第 21 条），包在 try/except 里，失败只留
        log、不把收尾拖成 `failed`。
        """
        step_id = "post"
        self._begin_step(step_id, "释放会中模型；会后流程等你按按钮（这一格停在 waiting）")
        release = getattr(self.transcriber_backend, "release_gpu", None)
        if callable(release):
            try:
                # 与其余几步同一形态：同步实现提交到收尾专用的 daemon 工作线程，
                # 连同等待一起包进这一步的剩余预算里。
                await self._drain_wait(step_id, release, deadline=deadline, invoke=True)
            except asyncio.CancelledError:
                # 取消是强制打断路径（§3.5）：登记交回取消路径，这里不吞
                raise
            except Exception:
                _log.exception("会中模型退出显存失败（如实登记，不静默）")
        # 这一格**如实停在 `waiting`**（＝界面上那句「等你动手」）：会后那两趟活各有各的按钮，
        # 用户按了哪一个才跑哪一个。落定这一笔照走 `_settle_step()`（写盘 + 提交都在它里面），
        # 于是前端那一条步骤带当场读到「post：waiting」。
        self._settle_step(
            step_id,
            "waiting",
            "会后不自动跑：按「生成会后精细稿」或「直接出纪要」才起（这一格等你动手）",
            None,
            self.post_resolution,
        )
        _log.info(
            "收尾`post` 步：会后流程不自动拉起（这一格停在 waiting，两个按钮归用户按）：%s",
            self.snapshot.session.dir if self.snapshot.session else "?",
        )

    # 收尾登记（写盘 + 提交都收在这几个方法里）

    def _begin_step(self, step_id: str, detail: str) -> None:
        _log.info("收尾步骤开始：%s（%s）", step_id, detail)
        self._drain_step_started = time.monotonic()
        begin_drain_step(step_id, self.snapshot)
        try:
            self._write_session()
        except Exception:
            _log.exception("收尾写盘失败（步骤 %s 进入）：如实登记，不静默", step_id)
        self.commit()

    def _settle_step(
        self,
        step_id: str,
        status: str,
        detail: str,
        error: str | None,
        resolution: BackendResolution | None,
        *,
        cancelled: bool = False,
        elapsed: float | None = None,
    ) -> None:
        if elapsed is None and self._drain_step_started is not None:
            # 缺席步（`skipped`）也报耗时：它们各自也有 enter -> settle 的实测时长
            elapsed = max(0.0, time.monotonic() - self._drain_step_started)
        # 每步的「结束 + 耗时」逐条进 log（方案 §5.3 行 250、§6.3 行 304）
        spent = "" if elapsed is None else f"，耗时 {round(float(elapsed), 3)}s"
        _log.info("收尾步骤结束：%s -> %s（%s）%s", step_id, status, detail, spent)
        if elapsed is not None:
            # 单列一行「耗时」：`log` 步的结束那一行落在摘掉场次 handler 之后，
            # 这一行则一定写进场次 log（方案 §6.3 行 304 的「每一步的耗时」两处都查得到）。
            _log.info("收尾步骤耗时：%s 用时 %ss", step_id, round(float(elapsed), 3))
        finish_drain_step(
            step_id,
            status,
            detail,
            error,
            snapshot=self.snapshot,
            frozen_ended=cancelled,
            frozen_interrupted=cancelled,
        )
        try:
            # 取消路径的落盘把 `state` 钉在 `draining` 上（§3.5 的第二条不覆盖约束）：
            # 已有 `failed` 步时 `state_for()` 的 `has_failed_step → failed` 会算出
            # "failed"，把 `mark_interrupted()` 刚写下的 `draining` 覆盖掉——强制打断档
            # 盘上必须是 `draining` + `interrupted: true` + 无 `ended_at`。
            self._write_session(
                interrupted=True if cancelled else None,
                state="draining" if cancelled else None,
            )
        except Exception:
            _log.exception("收尾写盘失败（步骤 %s 落定）：如实登记，不静默", step_id)
        self.commit()
        if resolution is not None:
            resolution.mark_run(status != "failed")

    def _done_step(
        self,
        step_id: str,
        detail: str,
        resolution: BackendResolution | None,
        *,
        elapsed: float | None = None,
    ) -> None:
        self._settle_step(step_id, "done", detail, None, resolution, elapsed=elapsed)

    def _skip_step(
        self,
        step_id: str,
        detail: str,
        resolution: BackendResolution | None,
        *,
        elapsed: float | None = None,
    ) -> None:
        # 缺席（构建期没有真实实现）→ skipped，不阻塞 done，留一行 warning
        _log.warning("收尾步骤跳过：%s —— %s", step_id, detail)
        self._settle_step(step_id, "skipped", detail, None, resolution, elapsed=elapsed)

    def _fail_step(
        self,
        step_id: str,
        exc: Exception,
        resolution: BackendResolution | None,
        what: str,
        *,
        elapsed: float | None = None,
    ) -> None:
        # 有实现但抛错 / 超预算 → failed（error = 原因 + 可复制的命令 + log 路径）
        session_dir = self.snapshot.session.dir if self.snapshot.session else "<场次目录>"
        error = (
            f"{what}：{type(exc).__name__}: {exc}"
            f"；可复制命令：meeting post {session_dir}"
            f"；log 路径：{self.log_path}"
        )
        _log.exception("收尾步骤失败：%s（%s）", step_id, what)
        self._settle_step(step_id, "failed", what, error, resolution, elapsed=elapsed)

    def _finish_drain(self, *, cancelled: bool = False) -> str:
        """调**收尾的唯一出口** `finish_drain()` 判终态；失败档把两条出路写进 log。

        `cancelled=True`（取消那一趟）一路传到终态判定：被取消的收尾没有跑完，
        `ended.phase` 该保持 `draining`。
        """
        session_dir = self.snapshot.session.dir if self.snapshot.session else None
        final_state = finish_drain(self.snapshot, cancelled=cancelled)
        ended = self.snapshot.ended
        if final_state == "done":
            _log.info("收尾完成：各步全部落定且没有失败步（ended.phase=done，top_state 停在 ended）")
            # 成功档的收尾副作用把两路 `health` 从 `stopped` 带回 `unprobed`（phase2 §3.5.2），
            # 逐路落一条（失败档保留 `stopped` 现场，health 不动、因此这里不落）
            self._log_health_migrations(
                reason="收尾走到终态（ended.phase=done）",
                threshold="无阈值（收尾终态这一步不看阈值）",
            )
        else:
            if cancelled:
                _log.warning(
                    "收尾被取消且各步未全部落定：ended.phase 保持 draining"
                    "、top_state 停在 ended、session 保留（不判 done）"
                )
            else:
                failed = [step.id for step in (ended.steps if ended else []) if step.status == "failed"]
                _log.error(
                    "收尾有失败步 %s：会话停在 draining，服务不自杀；"
                    "出路一：`meeting post %s`（音频与 session.json 都在盘上，重跑不欠这次收尾什么）；"
                    "出路二：看清现场后重启服务",
                    failed, session_dir,
                )
        return final_state

    # 强制打断（§3.5）与关停接线

    def mark_interrupted(self) -> dict[str, Any] | None:
        """强制打断的先落盘：写 `state: "draining"` + `interrupted: true`。

        保留原 `steps`、不伪造终态、不写 `done`、不写 `ended_at`——重启与 `meeting post`
        都还接得上。走 `SessionStore.mark_interrupted()` 薄封装（同一入口、同一原子写，
        不开第二个写盘入口）。只在各步尚未全部落定这一档负责盘上最终那一版；已全部落定
        的按终态规则由 `finish_drain()` 那一趟落。
        """
        session = self.snapshot.session
        if session is None:  # pragma: no cover - 走到这里一定有场次
            return None
        steps = steps_from_ended(self.snapshot.ended) if self.snapshot.ended is not None else None
        # 这一版的 `state` / `interrupted` / `ended_at` 三个参数由那层薄封装组装
        # （`SessionStore.mark_interrupted()`），这里交出去的是**现场记录本身**
        record = self._session_record(
            session,
            state="",
            ended_at=None,
            interrupted=False,
            steps=steps,
        )
        path = self.store.mark_interrupted(session.dir, record)
        written = dict(record)
        written["state"] = "draining"
        written["interrupted"] = True
        written["ended_at"] = None
        _log.warning(
            "%s：已落盘 state=draining + interrupted=true"
            "、原 steps 原样保留、不写 ended_at；盘上那一版 -> %s",
            _interrupted_message(),
            path,
        )
        return written

    def drain_has_settled(self) -> bool:
        """各步是否全部落定（`running` 不算落定；`waiting` 是 `post` 那一格的终态，算）。

        `post` 步 `post` 按「结束录音」之后不自动拉起会后（R26），它做完该做的（释放会中模型）
        就停在 `waiting`（＝界面上那句"等你动手"）。因此这一格与 `finish_drain()` 同口径：
        `waiting` 是落定，`running` 还在跑。
        """
        ended = self.snapshot.ended
        if ended is None:
            return False
        return all(step.status in ("done", "skipped", "failed", "waiting") for step in ended.steps)

    def drain_finished(self) -> bool:
        """这一趟收尾是否**走完且终态已定**（`ended.phase == "done"`，收尾 task 句柄已置空）。

        第一次 `Ctrl+C` 的等待条件：收尾要**真跑完**（含 `log` 步的挂摘）才关停服务。
        """
        return self.snapshot.ended is not None and self.snapshot.ended.phase == "done"

    def drain_in_progress(self) -> bool:
        """收尾 task 是否还在跑 / 已经排上但还没开始跑。"""
        task = self._drain_task
        return task is not None and not task.done()

    async def _cancel_drain_task(self) -> None:
        """对 `_drain_task` 调 `cancel()` 并等它收场。

        **句柄已被读成「已结束」时跳过 `cancel()`、只做落盘**（§3.5 第 2 步）。
        """
        task = self._drain_task
        if task is None or task.done():
            _log.info("收尾 task 已结束（或没有收尾在跑）：跳过 cancel()，只做落盘")
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            _log.info("收尾 task 已按取消路径落盘（未开跑的步骤维持 waiting，不伪造终态）")

    async def trigger_shutdown(self, *, force: bool) -> str:
        """**信号路线唯一的线程安全触发入口**（由 `app.handle_shutdown_signal` 排进事件循环）。

        语义是「先跑收尾、再请求关停」，四类触发点在这里分支（§3.5 的表）：

          - `force=False` 且**当场没有收尾要管** → 直接退出（空闲退出档）：`idle`（进程刚起、
            没开过任何一场）、以及
            `ended` 且各步已全部落定**无 `failed`**（`ended.phase == "done"`，收尾干净走完）
            两格都落在这一支 —— 第二种是审查问题 C1 补上的那一格，它**不写盘**；
          - `force=False` 且当时 `recording` / `alert` → 走**同一条**
            `stop_session(reason="ctrl_c")` 路径起收尾，等它落定再请求关停；
          - `force=True`，或当时已经停在 `ended·draining`（`stop` 之后、收尾失败或还没跑完）
            → **等同「第二次 Ctrl+C」**：先落盘（`draining` + `interrupted: true`、不写
            `ended_at`）→ 再 `cancel()` 收尾 task → 请求关停。**绝不写 `done`**。这一支落的
            log 行是冻结常量 `DRAIN_INTERRUPTED_MESSAGE` 加上**真实按下次数**（见下面那行
            注释：这一支里用户可能只按过一次）。这一支开头仍挡着
            `_first_press_can_just_exit()` 那一格（收尾干净走完），避免误改已经落稳的 `done`。

        全部动作（收尾状态、`interrupted`、`session.json` 的写值、`cancel()`）都发生在事件循环
        这一侧 —— 信号处理函数本身只做「把这个回调排进循环」这一件事。
        """
        from meeting.service import app as service_app

        if not force and self._first_press_can_just_exit():
            # 这一支有三格（§3.5 行为表的二 / 四 / 收尾干净那一格），都不是「有收尾要做」：
            # 空闲态、重启提示态、以及「各步已全部落定且无 failed」的收尾干净档。
            # 三格都不写盘、不 `cancel()`、不写 `interrupted`（C1 的那一格就在第三种）。
            _log.info("这一次 Ctrl+C 落在「没有收尾要管」这一格：不写盘、直接退出（空闲退出档）")
            self._shutdown_interrupted = False
            self._request_server_shutdown()
            return "exit"

        if not force and self.snapshot.top_state in ("recording", "alert"):
            try:
                self.stop_session(reason="ctrl_c")
                _log.info("第一次 Ctrl+C：收尾已起（与 stop 同一条路径），等它落定再关停服务")
            except ActionError as exc:
                _log.warning("第一次 Ctrl+C 想收尾但动作被拒（%s）：不关停，等第二次 Ctrl+C", exc)
                return "idle"
            await self._wait_drain_settled()
            self._shutdown_interrupted = False
            self._request_server_shutdown()
            return "drained"

        # 第二次按下 / 已经停在 ended·draining 再按：立即中止，顺序写死（先落盘、再 cancel()）。
        # **先挡一格**：各步已全部落定且**无 `failed`** 时，这一场已经收尾干净（`ended.phase` 已是
        # `done`、盘上是 `state="done"` + `ended_at`），当场**没有收尾要管** —— 不落盘、不 `cancel()`、
        # 不写 `interrupted`，直接走空闲退出档。判据与 `_first_press_can_just_exit()` 同一句，
        # 两处都不许把「收尾干净走完」读成「已停在 `ended·draining` 再按」（审查问题 C1）。
        if self._first_press_can_just_exit():
            _log.info(
                "这一次关停请求落在「收尾干净走完」这一格：没有收尾要管，"
                "盘上那一版（state=done + ended_at）一个字都不改，直接退出"
            )
            self._shutdown_interrupted = False
            self._request_server_shutdown()
            return "exit"

        self._shutdown_interrupted = True
        # 冻结常量 `DRAIN_INTERRUPTED_MESSAGE` 原样保留（§3.5 的行为表把它划给「等同第二次
        # Ctrl+C」这一档），同一行里补上**真实按下次数**：`stop` 之后收尾中再按的那一档，
        # 用户只按了一次，常量末尾那句「用户第二次 Ctrl+C」与事实对不上，这一项把记录对齐。
        _log.warning("%s", _interrupted_message())
        if self.snapshot.ended is not None:
            self._settle_drain_host()
            self.mark_interrupted()
        else:  # pragma: no cover - 盘上有没收完的场次但快照没有它（收编归注册表节点）
            _log.warning("强制打断：手上没有收尾中的场次，盘上那一版不在本次收尾的责任范围")
        await self._cancel_drain_task()
        self._request_server_shutdown()
        return "interrupted"

    def _settle_drain_host(self) -> None:
        """强制打断只写 `draining` + `interrupted: true`：让 `_session_state_on_disk()` 的映射
        落在 `draining` 这一档上（`ended.phase` 保持 `draining`，不写 `done`）。"""
        if self.snapshot.ended is not None and not self.drain_has_settled():
            self.snapshot.ended.phase = "draining"

    def _drain_running(self) -> bool:
        """这一趟收尾**是不是还在跑 / 还没开跑**（句柄在手上且未落定）。

        §3.5 行为表把「收尾进行中」「收尾失败现场」两档与「无收尾可做」分得一清二楚，
        判据因此落在**句柄 `_drain_task` 的读法**上（它由 phase2 的 `drain()` 持有、走完含
        被取消那趟后置空），**不看盘上有没有未收尾场次**：盘上的旧场次归下一节点的
        注册表收编，但与本收尾句柄无关 —— 此刻**没有任何收尾在跑**。
        """
        return self.drain_in_progress()

    def _drain_failed_face(self) -> bool:
        """是不是「收尾已跑完、停在 `ended·draining` 的失败现场」（§3.5 行为表第四行）。

        两条件缺一不可：各步都落定、且各步里有 `failed`。只判「落定」会把「收尾干净走完」
        那格吞进来——第一次 `Ctrl+C` 会被当成强制打断、把盘上 `done` 无条件改写成
        `draining` + `interrupted`（审查问题 C1；负向实测退回 `return True` 时
        `tests/test_shutdown_signal.py` 的 `test_clean_drain_then_sigbreak_exits_zero_...`
        退出码 130）。
        """
        ended = self.snapshot.ended
        if ended is None:
            return False
        if not self.drain_has_settled() or self._drain_running():
            return False
        return any(step.status == "failed" for step in ended.steps)

    def _first_press_can_just_exit(self) -> bool:
        """第一次 `Ctrl+C` 能不能「直接退出」（§3.5 行为表）。

        `recording` / `alert` → 起收尾不算；`ended` + 收尾在跑 → 等同第二次按下；
        `ended` + 失败现场 → 等同第二次按下；`ended` + 收尾干净走完（`phase == "done"`）
        与其余情形（`idle` 等）→ 没有收尾要管，直接退出、盘上一个字节都不改（C1 的那格）。
        """
        top = self.snapshot.top_state
        if top in ("recording", "alert"):
            return False
        if self._drain_running():
            return False
        if self._drain_failed_face():
            return False
        # 剩下的两格都落在这一句上（收尾干净走完与「其余情形」）：当场都没有收尾要管，
        # 盘上那一版由上一趟自己落稳，这里不产生任何写盘动作。
        return True

    async def _wait_drain_settled(self, *, timeout: float | None = None) -> None:
        """等这一趟收尾落定（第一次 `Ctrl+C` 的「收尾真的跑完」）。

        退出条件只有两条：收尾走到终态，或各步全部落定且收尾 task 不在跑。上限与本次收尾
        同源（不传 `timeout` 时取 `_drain_wait_timeout()`，不把输入读第二遍——再读一次只会
        得到更短的上限）。等待只做睡觉与读快照，不打断收尾。
        """
        if timeout is None:
            timeout = self._drain_wait_timeout()
        deadline = time.monotonic() + float(timeout)
        while True:
            if self.drain_finished():
                return
            if not self.drain_in_progress() and self.drain_has_settled():
                return
            if time.monotonic() >= deadline:
                _log.warning("等收尾落定超时（%ss）：按当前落盘的那一版关停，不静默", timeout)
                return
            await asyncio.sleep(DRAIN_SETTLE_POLL_SECONDS)

    def _request_server_shutdown(self) -> None:
        from meeting.service import app as service_app

        service_app.request_server_shutdown()

    @property
    def shutdown_interrupted(self) -> bool:
        """本次关停是不是强制打断那一档（`cli.py` 据此挑退出码，§3.5）。"""
        return bool(self._shutdown_interrupted)
