from __future__ import annotations

import asyncio
import contextlib
import re
import time
from pathlib import Path
from typing import Any

import pytest

from meeting.service.runtime import (
    DRAIN_BUDGET_MAX_SECONDS,
    DRAIN_BUDGET_MIN_SECONDS,
    DRAIN_BUDGET_SEGMENT_OVERHEAD_SECONDS,
    DRAIN_DEFAULT_BACKLOG,
    DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS,
    DRAIN_LOG_MIN_BUDGET_SECONDS,
    DrainLimitExceeded,
    compute_drain_budget,
)
from meeting.session import SESSION_FILE_NAME
from meeting.state import DRAIN_STEP_IDS, DRAIN_STEP_STATUS
from tests import _drain_fakes as fakes
from tests import _helpers as helpers
from tests._helpers import wait_until
from tests._service import post_action, read_session_json

from tests.test_drain._common import (
    DRAIN_UNIT_BUDGET_SECONDS,
    DRAIN_UNIT_STEP_SECONDS,
    _inprocess_runtime,
    _new_workspace,
)

#: 慢档标记：`pytest.mark.slow` 的归位判据见 `docs/service.md`。
pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# ⑤b 等待上限对同步与协程两条路一视同仁（**进程内**，极小 deadline）
#
# 这一条是审查问题 C1 的直接判据：`_drain_wait()` 曾经在「同步 lambda」这条路上直接调用、
# 不经 `asyncio.wait_for`，于是同步步完整跑完、`budget` 与 `DRAIN_LOG_MIN_BUDGET_SECONDS`
# 成了死值。这里用**可注入的小 deadline**（0.02 秒）把墙钟代价压到毫秒级，
# 而断言的语义与「真等 30 秒预算」完全一致：超预算 → `DrainLimitExceeded`。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "invoke, mode",
    [
        (True, "sync-callable-in-worker-thread"),
        (False, "coroutine-on-the-loop"),
    ],
)
def test_drain_wait_wraps_every_call_in_the_remaining_budget(dev_runtime, invoke, mode):
    """同步路（工作线程）与协程路**都**受同一个剩余预算约束（超出即 `DrainLimitExceeded`）。

    判据取 `DrainLimitExceeded` 自报的**实测耗时**（`_drain_wait()` 里那把秒表量出来的那一段）：
    它必须贴近 0.02 秒那条上限，**远小于**那一步自己声明的 0.6 秒。C1 那个 bug 的签名正是
    「同步 lambda 完整跑完、连异常都不抛」——两条断言各自都咬得住它。

    外层墙钟只做一个宽松的上界（< 5 秒）：这一条跑在 pytest 进程里，收尾执行器的线程**首次
    使用要现起线程**（Windows 上数百毫秒），把它算进「这一步花了多久」会冤枉被测的那段逻辑；
    真服务子进程里线程池早已就位（⑩ 条在那边断的是同一件事的进程级形态）。
    """
    runtime = dev_runtime
    elapsed = DRAIN_UNIT_STEP_SECONDS
    if invoke:
        # 真服务里四步调用点用的形态：同步 callable + `invoke=True`（提交到工作线程）
        def step():
            time.sleep(elapsed)

    else:
        # 协程形态：走 `call()` 的返回值（`ensure_future` 包成任务）再等
        async def step():
            await asyncio.sleep(elapsed)

    # deadline 落在「已经过去」（剩余预算为 0）→ 实际生效的上限就是 `min_budget`
    started = time.monotonic()
    with pytest.raises(DrainLimitExceeded) as caught:
        asyncio.run(
            runtime._drain_wait(
                "flush",
                step,
                deadline=started - 1.0,
                min_budget=DRAIN_UNIT_BUDGET_SECONDS,
                invoke=invoke,
            )
        )
    spent = time.monotonic() - started
    message = str(caught.value)
    assert "超出收尾预算" in message, message
    match = re.search(r"实测耗时 ([\d.]+)s", message)
    assert match, message
    inside = float(match.group(1))
    assert inside < elapsed / 2, (
        f"{mode} 这条路绕过了等待上限：{elapsed}s 的假步照跑完（{message}）"
    )
    assert inside <= DRAIN_UNIT_BUDGET_SECONDS * 20, message
    assert spent < 5.0, f"{mode} 这条路的墙钟 {spent:.3f}s 该在量级上就收住（{message}）"

    # 反向对照：预算够宽时同一条路正常返回，且实测耗时以 `_drain_wait()` 的返回值为准。
    # 协程那一档给的是货真价实的 `async def`（`inspect.iscoroutinefunction()` 为真）：
    # 写成本文件从前那种 `lambda: _returns_after(...)` 会被下面那道「同步 callable 必须传
    # `invoke=True`」的看门（审查问题 m1）拦下来 —— 那正是它想拦住的那类调用点。
    healthy = (lambda: "落定") if invoke else _coroutine_step
    value, measured = asyncio.run(
        runtime._drain_wait(
            "flush",
            healthy,
            deadline=time.monotonic() + elapsed + 5.0,
            invoke=invoke,
        )
    )
    assert value == "落定"
    assert 0.0 <= measured < elapsed + 5.0

    # 同步 callable 忘了传 `invoke=True` 这一档：这条错法在**调用之前**就被认出来
    # （`TypeError`，文案点名 `invoke=True`），不会退化成「先把同步实现跑在事件循环上」。
    # 完整的判据（含「同步实现一次都没被调到」与「墙钟远小于假步时长」）在下面 ⑤b1 那一条。
    if invoke:
        with pytest.raises(TypeError, match="invoke=True"):
            asyncio.run(
                runtime._drain_wait(
                    "flush",
                    lambda: time.sleep(0.0) or "普通值",
                    deadline=time.monotonic() + 5.0,
                )
            )


async def _coroutine_step() -> str:
    """`_drain_wait()` 协程路的健康对照：`async def` 形态的「一步调用」。"""
    return await _returns_after(DRAIN_UNIT_STEP_SECONDS)


def test_remaining_budget_and_drain_wait_reject_a_missing_deadline(dev_runtime):
    """`deadline=None` 显式报错，不给任何上限（**进程内**，审查问题 m2）。

    契约 §3.3 与 §8 的坑表把这条上限的来源钉在一处：**只能来自 `compute_drain_budget()`**。
    `None` 换不出一个说得清的数 —— 退成 `float("inf")` 就是「没有上限」、退成预算下限是另一条
    凭空的保守上界。两条入口因此都当场说话，并且**在等待之前**说话（下面的阻塞步一次都不该
    被跑到：真去等的话这一趟要挂 10 秒）。

    断的是三件事：抛的是 `ValueError`、文案里点名 `compute_drain_budget()`、以及这一步的
    调用**没有被跑到**。
    """
    runtime = dev_runtime
    ran = {"n": 0}

    def blocking_step():
        ran["n"] += 1
        time.sleep(DRAIN_UNIT_STEP_SECONDS * 10)
        return "跑完了"

    with pytest.raises(ValueError) as caught:
        runtime._remaining_budget(None)
    message = str(caught.value)
    assert "compute_drain_budget()" in message, message

    started = time.monotonic()
    with pytest.raises(ValueError) as caught_wait:
        asyncio.run(
            runtime._drain_wait(
                "flush",
                blocking_step,
                deadline=None,
                invoke=True,
            )
        )
    spent = time.monotonic() - started
    wait_message = str(caught_wait.value)
    assert "compute_drain_budget()" in wait_message, wait_message
    assert ran["n"] == 0, (
        f"报错之前先把这一步跑到了 {ran['n']} 次 —— 缺 deadline 该在等待之前就说话"
    )
    assert spent < DRAIN_UNIT_STEP_SECONDS, (
        f"缺 deadline 的那一趟等了 {spent:.3f}s：它该在开跑之前就抛出来"
    )
    print(
        f"[m2 证据] _remaining_budget(None) -> {type(caught.value).__name__}：{message}\n"
        f"[m2 证据] _drain_wait(..., deadline=None) -> {type(caught_wait.value).__name__}："
        f"{wait_message}（该步被跑到的次数={ran['n']}，实测墙钟={spent:.4f}s）"
    )


async def _returns_after(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return "落定"


# ---------------------------------------------------------------------------
# ⑤b1 同步 callable 忘传 `invoke=True`：在开跑之前就刺耳（**进程内**，审查问题 m1）
#
# 从前这条错法是「先跑完、再抛错」：`call()` 在事件循环上把那个同步实现整段跑掉（审查实测
# 3.00 秒的 `time.sleep` 把循环钉满 3 秒，`min_budget` 拦不住），之后 `wait_for` 才认不出
# 普通值、抛 `TypeError`。收尾期间 `/api/state`、SSE 与 `POST /api/actions` 在这 3 秒里一起
# 停摆 —— 与「收尾必须可观测」相抵（契约 §3.1 第 6 条）。
#
# 现在这个错法在**调用之前**被认出来：`invoke` 为假 + 同步可调用对象 → 当场抛 `TypeError`，
# 同步实现一行都不跑（下面的计数器是这条的直接判据），那一步立刻落 `failed`，收尾的其余
# 步骤照常推进、事件循环一直在转。
# ---------------------------------------------------------------------------

#: ⑤b1 里那个「不该被跑」的同步步的时长（秒）：足够长，长到它一旦真的跑起来就会
#: 把事件循环钉住、把这条用例的墙钟顶起来。
DRAIN_FORGOTTEN_INVOKE_STEP_SECONDS = 0.2

#: ⑤b1 里那一步的窗口（秒）：比假步短，于是「假步真跑起来」会额外体现为「等满上限」。
DRAIN_FORGOTTEN_INVOKE_WINDOW_SECONDS = 0.05


def test_sync_callable_without_invoke_true_fails_fast_and_keeps_the_loop_running(dev_runtime):
    """忘传 `invoke=True` → 同步实现一行都不跑、事件循环照常转、那一步落 `failed`（m1）。

    三件事一起断，缺一件这条错法就还是「先跑完再抛错」：

      1. 那个同步实现**一次都没被调到**（它自己的计数器停在 0），于是「事件循环被它钉住」
         这件事在成因上就不存在；
      2. 抛的是 `TypeError`，文案里点名 `invoke=True`（说得清是哪一处调用点的问题）；
      3. **报错发生在开跑之前**：这一趟的实测墙钟远小于那个假步自己的时长（假步若真跑起来，
         墙钟至少要 0.2 秒）。
    """
    runtime = dev_runtime
    ran = {"n": 0}

    def step():
        ran["n"] += 1
        time.sleep(DRAIN_FORGOTTEN_INVOKE_STEP_SECONDS)
        return "跑完了"

    started = time.monotonic()
    with pytest.raises(TypeError) as caught:
        asyncio.run(
            runtime._drain_wait(
                "flush",
                step,
                deadline=time.monotonic() + DRAIN_FORGOTTEN_INVOKE_WINDOW_SECONDS,
            )
        )
    spent = time.monotonic() - started
    message = str(caught.value)
    assert "invoke=True" in message, message
    assert ran["n"] == 0, (
        f"同步实现被跑到了 {ran['n']} 次 —— 这条错法该在调用之前就被认出来"
        f"（实测墙钟 {spent:.3f}s）"
    )
    assert spent < DRAIN_FORGOTTEN_INVOKE_STEP_SECONDS, (
        f"报错前先跑了那个同步实现：墙钟 {spent:.3f}s 不小于假步自己的 "
        f"{DRAIN_FORGOTTEN_INVOKE_STEP_SECONDS}s"
    )
    print(
        f"[m1 证据] 同步 callable 未传 invoke=True：{type(caught.value).__name__}"
        f"（{message}）；同步实现被调到的次数={ran['n']}；实测墙钟={spent:.4f}s"
    )


def test_step_over_budget_still_leaves_later_steps_room(tmp_path, monkeypatch):
    """某一步的调用出事之后，**后续步仍能推进并落定**（m1 的收尾编排形态）。

    这一条走的是真正的收尾编排（`drain()` 四步），只在过程内把第一步的调用换成「同步
    可调用对象 + 漏掉 `invoke=True`」这个错法（`_drain_flush()` 的登记照原样照抄：出错
    经 `except` 落 `failed`，**整段 `drain` 不许因此抛出去**）。丢掉了 `invoke=True` 的
    同步步从前会先跑在事件循环上、`_drain_flush()` 于是在整段睡眠里卡住，下面这个 ticker
    一次都跑不到 —— 收尾期间 `/api/state` 与 SSE 也就一起停摆。

    断言因此分三处：事件循环一直在转（ticker 计数）、那一步落 `failed` 且文案点名
    `invoke=True`、**后续步照常推进到终态**（`transcode` 按采集替身没有 transcode 能力、
    `post` 按钩子缺席记 `skipped`，`log` 记 `done`）。

    **观察窗口从第一步挪到整趟收尾**：修好之后第一步在开跑之前就落定（零点几毫秒），单看它
    那一段量不到「循环一直在转」。于是本用例给 `log` 那一步挂一个睡觉的假替身
    （`DRAIN_FORGOTTEN_INVOKE_STEP_SECONDS`，走真正的工作线程路径），收尾因此有一段真实的
    时长可观测，而「后续步在一个已经出错的收尾里照常推进」这件事也走得更实。
    """
    from meeting import logging_setup as logging_setup_module
    from meeting.state import begin_drain

    runtime = _inprocess_runtime_with_backends(tmp_path, monkeypatch)

    async def flush_with_the_forgotten_invoke(deadline: float) -> None:
        """`_drain_flush()` 的等价体：调用点丢掉 `invoke=True`，出错照原样落 `failed`。"""
        runtime._begin_step("flush", "停采集并把这一场的归档落盘（后端 finalize()）")
        try:
            await runtime._drain_wait(
                "flush",
                lambda: time.sleep(DRAIN_FORGOTTEN_INVOKE_STEP_SECONDS) or "同步实现跑完了",
                deadline=deadline,
            )
        except Exception as exc:
            runtime._fail_step("flush", exc, None, "采集归档落盘失败")
            return
        runtime._done_step("flush", "归档已落盘（finalize() 返回）", None)

    monkeypatch.setattr(runtime, "_drain_flush", flush_with_the_forgotten_invoke)

    def slow_end_marker(*, state: str, steps) -> None:
        """`log` 那一步的假替身：在**工作线程**里睡一小段（真路径走的就是那条线程）。"""
        time.sleep(DRAIN_FORGOTTEN_INVOKE_STEP_SECONDS)
        return None

    monkeypatch.setattr(
        logging_setup_module, "write_session_end_marker", slow_end_marker
    )

    async def scenario() -> int:
        ticks = {"n": 0}

        async def ticker() -> None:
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0.005)

        beat = asyncio.ensure_future(ticker())
        try:
            runtime.start_session(dir=_new_workspace(tmp_path, "loop-pinned"))
            begin_drain(runtime.snapshot, reason="user")
            await runtime.drain()
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        return ticks["n"]

    ticks = asyncio.run(scenario())
    statuses = {step.id: step.status for step in runtime.snapshot.ended.steps}
    assert ticks >= 10, (
        f"收尾期间事件循环只转了 {ticks} 次 —— 同步步被留在事件循环上了（收尾不可观测）"
    )
    assert statuses["flush"] == "failed", statuses
    flush_step = next(step for step in runtime.snapshot.ended.steps if step.id == "flush")
    assert "invoke=True" in (flush_step.error or ""), flush_step.error
    assert statuses["log"] == "done", (
        f"第 1 步超出窗口之后，后续步该照常推进到终态：{statuses}"
    )
    print(
        f"[m1 证据] 第一步丢掉 invoke=True：flush -> {statuses['flush']}"
        f"（error={flush_step.error!r}）；收尾期间事件循环转了 {ticks} 次；"
        f"后续四步状态={statuses}"
    )


# ---------------------------------------------------------------------------
# ⑤b2 `log` 步的写失败要如实落 `failed`（**进程内**，审查问题 m3）
#
# `meeting/service/runtime.py` 的 `log` 步有一条 `if problem:` 分支：场次日志的 flush /
# 结束标记 / 摘除返回一条非空的问题说明时，这一步记 `failed`（契约 §3.7：写失败例如磁盘满 →
# 不中断录音，`drain.steps.log` 记 `failed`、状态里出告警、主 log 留一条，**不许静默**）。
# 在这一条之前，那一格没有任何用例走到过 —— 把它整段删掉、或者让 `problem` 被吞掉，
# 全量回归照旧全绿。
#
# 这一条把 `write_session_end_marker()` 换成一个「返回非空 problem」的实现（进程内，
# 不起服务、不碰磁盘），然后走真正的 `drain()` 四步：那一步落 `failed`、`error` 里带如实
# 文案与问题原文、进程照常活着、其余步照常推进。
# ---------------------------------------------------------------------------

#: ⑤b2 里那个假替身返回的问题说明（与磁盘满同形的一条如实文案）
DRAIN_LOG_PROBLEM_TEXT = "写结束标记失败：OSError: [Errno 28] No space left on device"


def _inprocess_runtime_with_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, post_hook_absent: bool = True
):
    """进程内那一档的 `ServiceRuntime`，**后端已解析**（可直接开一场、跑一趟收尾）。

    与 `_inprocess_runtime()` 同一档，差别只在两处：

      - 配置由本用例现给（`capture.backend = "dev"`：采集替身在场、`finalize` / `transcode`
        两项能力如实为假），于是 `start_session()` 有一个可用的采集后端（缺它会回 503）；
      - 走真正的后端解析（`_resolve_backends()`），四条收尾编排因此按 registry 的结论登记 ——
        「采集替身 → `flush` / `transcode` 记 `skipped`」这条骨架构建下的面孔是真的走出来的，
        用例自己的假件只要往 `post` 那一格或步骤方法上注入就够了。

    `post_hook_absent=False` 时不写 `post.hook` 这一格（取值直接来自 `config.DEFAULT_VALUES`
    里的产品缺省 `auto`）：⑤b3 那一条量的是产品缺省那一档。
    """
    from meeting import config as config_module
    from meeting.service.app import build_runtime

    monkeypatch.setenv("HOME", str(tmp_path / "home-inprocess-backends"))
    monkeypatch.delenv("MEETING_CAPTURE", raising=False)
    # `build_runtime()` 会把这份配置设成当前配置（`config._LOADED`）；用例结束时放回原样
    # —— 进程内用例共用同一份模块级当前配置，留着自己那一份会渗到别的用例里。
    monkeypatch.setattr(config_module, "_LOADED", config_module.current())
    runtime = build_runtime(
        _inprocess_config(
            tmp_path, {"capture.backend": "dev"}, post_hook_absent=post_hook_absent
        )
    )
    # 后端解析在真服务里由 `startup()` 的 ④ 步触发（本档不起 lifespan），这里显式走一遍 ——
    # 于是 `start_session()` 有一个在位的采集替身，四条收尾也按 registry 的结论登记。
    runtime._resolve_backends()
    return runtime


def _inprocess_config(
    tmp_path: Path, overrides: dict[str, Any], *, post_hook_absent: bool = True
):
    """进程内用例的一小份配置：**默认值表原样** + 本用例的输出根 + 点名要改的那几个键。

    默认值表直接取 `config.DEFAULT_VALUES`（不手抄一份：`settings` 视图、告警阈值与后端解析
    都要读全键集，抄一份迟早漏键）。

    `post_hook_absent=True`（缺省）时，`post.hook` 在这里显式取 `null`（缺席档）：缺席档下
    收尾第 4 步按「无实现」记 `skipped`。R26 之后产品缺省 `auto` 也不再拉起任何会后动作
    （那一格做完释放会中模型就停在 `waiting`），但本文件里数收尾编排那几步记录的用例
    （⑤b2 那一条逐字断言 `"post": "skipped"`）要的正是「缺席」这一档的面孔。

    `post_hook_absent=False` 时这一格整个不写，`post.hook` 于是取到 `config.DEFAULT_VALUES`
    里的产品缺省 `auto` —— ⑤b3 那一条量的就是产品缺省那一档（第 4 步解析到真实现，
    按 R26 停在 `waiting`）。
    """
    from meeting.config import DEFAULT_VALUES, Config

    applied = {
        **DEFAULT_VALUES,
        "log.dir": str(tmp_path / "out-inprocess-backends" / "logs"),
        "transcriber.backend": "auto",
        **({"post.hook": "null"} if post_hook_absent else {}),
        **overrides,
    }
    return Config(applied=applied, sources={key: "默认" for key in applied}, path="")


def test_log_step_lands_failed_when_the_session_log_write_reports_a_problem(
    tmp_path, monkeypatch
):
    """场次日志写失败 → `log` 步 `failed`、`error` 如实带上那一段、服务不自杀（审查问题 m3）。

    **契约依据**：§3.7（写失败不中断录音，`drain.steps.log` 记 `failed`、状态里出告警、
    主 log 留一条，**不许静默**）。`log` 是四步里唯一在任何构建里都有真实实现的步骤，
    「有实现却没做成」按 §3.0 的裁决就是 `failed`。

    断的是四件事：那一步 `failed`；`error` 里有「场次日志写失败」这类如实文案与问题原文；
    收尾就此停在 `draining`（`ended.phase` 保持这一档 —— 失败现场留在快照与盘上）；
    **其余步照常推进**（第 4 步 `post` 按「会后不自动跑」落 `waiting`，
    收尾编排没有整段抛出去）。最后一条同时是本文件里「这一步做完它该做的就空着等人」的落点：
    它走的是真 `drain()` 四步里的真 `_drain_post()`。
    """
    from meeting import logging_setup as logging_setup_module
    from meeting.state import begin_drain

    runtime = _inprocess_runtime_with_backends(tmp_path, monkeypatch)
    calls: list[dict[str, Any]] = []

    def fake_write_session_end_marker(*, state: str, steps) -> str:
        calls.append({"state": state, "steps": list(steps)})
        return DRAIN_LOG_PROBLEM_TEXT

    monkeypatch.setattr(
        logging_setup_module, "write_session_end_marker", fake_write_session_end_marker
    )

    async def one_pass() -> None:
        runtime.start_session(dir=_new_workspace(tmp_path, "log-write-problem"))
        begin_drain(runtime.snapshot, reason="user")
        await runtime.drain()

    asyncio.run(one_pass())

    assert len(calls) == 1, f"这一步该被调到恰好一次：{calls}"
    statuses = {step.id: step.status for step in runtime.snapshot.ended.steps}
    log_step = next(step for step in runtime.snapshot.ended.steps if step.id == "log")
    assert log_step.status == "failed", (log_step.status, log_step.detail, log_step.error)
    assert "场次日志写失败" in (log_step.error or ""), log_step.error
    assert DRAIN_LOG_PROBLEM_TEXT in (log_step.error or ""), log_step.error
    # 失败那一档：收尾停在 draining（不判 done、也不自杀）
    assert runtime.snapshot.ended.phase == "draining", runtime.snapshot.ended.phase
    assert runtime.snapshot.top_state == "ended", runtime.snapshot.top_state
    # 其余步照常推进（第 4 步停在「等你动手」，收尾编排没有整段抛出去）
    assert statuses["post"] == "waiting", statuses
    post_step = next(step for step in runtime.snapshot.ended.steps if step.id == "post")
    assert "等你动手" in (post_step.detail or ""), post_step.detail
    assert set(statuses.values()) <= set(DRAIN_STEP_STATUS), statuses
    print(
        f"[m3 证据] 场次日志写失败：log 步 -> {log_step.status}"
        f"（error={log_step.error!r}）；ended.phase={runtime.snapshot.ended.phase}；"
        f"四步状态={statuses}"
    )


# ---------------------------------------------------------------------------
# ⑤b3 收尾第 4 步在**产品缺省**那一档停 `waiting`（**进程内**，审查问题 C2）
#
# `_inprocess_config()` 把 `post.hook` 显式钉成 `null`（缺席档），好让 ⑤b2 那条逐字断言
# `"post": "skipped"` 继续为真 —— 那是「这一格不写实现时记 skipped」的覆盖，值钱的正是
# 「缺席」这半边。代价是本文件从此不再观察**产品缺省**：`post.hook = auto` 配上
# `meeting/backends/post.py` 这条真实现，第 4 步解析到真实现、做完释放会中模型就停在
# `waiting`（R26：会后一个动作都不自动跑）。
# 这一条把那半边补回来：`post.hook` 整个不写（`post_hook_absent=False`，取值直接来自
# `config.DEFAULT_VALUES`）、走真正的 `_resolve_backends()` 与真正的 `drain()` 四步。
# 本实现不再带任何钩子方法（`launch()` 已随收尾第 4 步的退场一并删除），第 4 步因此
# 起不了任何后台线程，`tests/conftest.py` 不许任何一个用例碰真模型。
# ---------------------------------------------------------------------------


def test_post_step_waits_for_the_user_under_the_product_default_hook(tmp_path, monkeypatch):
    """产品缺省 `post.hook = auto`：第 4 步解析到真实现，但**一个会后动作都不启动**（R26）。

    与 ⑤b2 同一条进程内路子，差别只在 `post.hook` 这一格：这里**不写它**，
    配置里那一格的取值就是 `config.DEFAULT_VALUES` 里的产品缺省（先断言它确实是 `auto`，
    免得缺省值哪天改了这条用例悄悄换成另一档）。断言这几件事：解析落到本单元的钩子类且
    `skipped()` 为假、第 4 步停在 `waiting`（＝等你动手）、收尾终态
    `ended.phase == "done"`（按「结束录音」之后什么都不发生，工作区回到没有进行中的会议）。
    """
    from meeting.backends import post as post_module
    from meeting.config import DEFAULT_VALUES
    from meeting.state import begin_drain

    assert DEFAULT_VALUES["post.hook"] == "auto", DEFAULT_VALUES["post.hook"]

    runtime = _inprocess_runtime_with_backends(
        tmp_path, monkeypatch, post_hook_absent=False
    )
    # 接线第一段：产品缺省那一档真的解析到本实现（`null` 那一档才落缺席）
    assert runtime.config.get("post.hook") == "auto", runtime.config.get("post.hook")
    assert runtime.post_resolution.target == "meeting.backends.post:PostProcessHook", (
        runtime.post_resolution
    )
    assert runtime.post_resolution.skipped() is False
    assert isinstance(runtime.post_hook, post_module.PostProcessHook)

    async def one_pass() -> None:
        runtime.start_session(dir=_new_workspace(tmp_path, "post-default-hook"))
        begin_drain(runtime.snapshot, reason="user")
        await runtime.drain()

    asyncio.run(one_pass())

    statuses = {step.id: step.status for step in runtime.snapshot.ended.steps}
    assert statuses["post"] == "waiting", (statuses, runtime.snapshot.ended)
    assert runtime.snapshot.ended.phase == "done", runtime.snapshot.ended.phase


# ---------------------------------------------------------------------------
# ⑤k 一步被放弃之后，后续步的线程仍可用（**进程内**，审查问题 m7）
#
# 收尾的工作池从前只有一条线程：某一步被按剩余预算判超时之后，它的**等待**被放弃，已经提交
# 给那条线程的同步调用却照跑到底（§3.5 明写这一条），后续步的调用于是只能排队等 ——
# 「一步超时」因此把后续步的**可用性**一起堵住。这一条把那件事量出来：让第 1 步 `flush` 的
# 调用卡在一条闸门上，超时之后 `log` 那一步仍要在**它自己的最小预算**里落定。
#
# **闸门为什么挂在第 1 步而不是最后一步**（探针量过才定的）：四步里只有 `flush` 与 `log`
# 真的走工作线程（缺席的步骤根本不进等待层）。挂到 `post` 上时，被放弃的那次调用与 `log`
# 的调用**并列在池里**，一条线程与四条线程都能让 `log` 立刻开跑，这一条就分不出两种池
# （实测：改回 1 条线程它照样全绿）。挂在第 1 步上时，被放弃的调用**先占住**线程、`log` 在
# 它之后才要线程 —— 那时池里还有没有空线程，正是这一条要量的东西。
# ---------------------------------------------------------------------------

#: ⑤k 里整趟收尾的预算（秒）：小到第 1 步一定会越过它
DRAIN_ABANDONED_TOTAL_BUDGET_SECONDS = 0.05

#: ⑤k 里第 1 步的窗口（秒）：整趟预算就是它的全部额度，越过即被放弃
DRAIN_ABANDONED_FLUSH_WINDOW_SECONDS = 0.05

#: ⑤k 里闸门的等待上限（秒）：闸门由用例在收尾跑完之后放开，这只是「万一没放开」的兜底
DRAIN_ABANDONED_GATE_SECONDS = 10.0

#: ⑤k 里整趟收尾墙钟的上界换算：`log` 那一步的额度（`DRAIN_LOG_MIN_BUDGET_SECONDS`）乘这个
#: 倍数。前一步被放弃之后，后续步该**立刻**拿到一条线程 —— 整趟墙钟因此落在「那一步自己的
#: 预算」附近，而线程池只有一条时它会顶到整份额度（5 秒）那一档。
DRAIN_ABANDONED_WALL_CLOCK_FACTOR = 0.8


def test_later_steps_keep_a_worker_after_an_earlier_step_is_abandoned(tmp_path, monkeypatch):
    """前一步被放弃之后，**后续步仍在自己的预算里推进并落定**（审查问题 m7）。

    现场：第 1 步 `flush` 的 `finalize()` 卡在一条闸门上（不出声地占着一条工作线程），整趟
    收尾的预算被压到 0.05 秒 —— 那一步因此真的越过窗口、被 `_drain_wait()` 放弃（落
    `failed`），而那次调用**还在线程里跑**。此时 `log` 那一步**尚有一条线程可用**：它的
    flush、结束标记与摘除在 `DRAIN_LOG_MIN_BUDGET_SECONDS` 之内跑完、落 `done`，并真的把
    `session.json` 写下来（下面读盘核对）。

    **线程池只有一条线程时这一条必红**：`log` 的那次调用排在闸门后面，整趟收尾的墙钟要等
    到闸门被放开那一下才收场；`log` 的调用则要等光它那 5 秒的最小预算才轮到、那一步落
    `failed`，盘上那份 `session.json` 也不会出现。用例结束前把闸门放开，那条被放弃的调用
    随即收场，不留悬挂线程。
    """
    import threading

    from meeting.backends.registry import BackendResolution
    from meeting.state import begin_drain

    runtime = _inprocess_runtime_with_backends(tmp_path, monkeypatch)
    gate = threading.Event()
    finalized = {"n": 0}

    class GatedCapture:
        """第 1 步卡在闸门上、其余调用立刻返回的采集后端（一次 `finalize()` 占住一条线程）。"""

        def start(self, mode, session_dir) -> None:
            return None

        def stop(self) -> None:
            return None

        def finalize(self) -> None:
            finalized["n"] += 1
            gate.wait(timeout=DRAIN_ABANDONED_GATE_SECONDS)
            return None

        def transcode(self) -> None:
            return None

    capture = GatedCapture()
    monkeypatch.setattr(runtime, "capture_backend", capture)
    monkeypatch.setattr(
        runtime,
        "capture_capabilities",
        {"finalize": True, "transcode": True, "dev_inject": False},
    )
    monkeypatch.setattr(
        runtime,
        "capture_resolution",
        BackendResolution(
            key="capture.backend",
            selector="gated",
            target=f"{__name__}:GatedCapture",
            implementation=capture,
            has_implementation=True,
            detail="本用例造的现场（一条闸门占着工作线程）",
        ),
    )
    monkeypatch.setattr(runtime, "_resolve_backends", lambda: None)
    monkeypatch.setattr(
        "meeting.service.runtime_drain.compute_drain_budget",
        lambda backlog, segment_timeout: DRAIN_ABANDONED_TOTAL_BUDGET_SECONDS,
    )

    still_flying = {"value": None}
    ws_path = Path(_new_workspace(tmp_path, "abandoned-flush"))
    try:

        async def one_pass() -> None:
            runtime.start_session(dir=str(ws_path))
            begin_drain(runtime.snapshot, reason="user")
            await runtime.drain()

        started = time.monotonic()
        asyncio.run(one_pass())
        spent = time.monotonic() - started
        # 收尾收场那一刻的现场：被放弃的那次 `finalize()` 还卡在闸门上（下面才放开）
        still_flying["value"] = not gate.is_set()
    finally:
        gate.set()

    statuses = {step.id: step.status for step in runtime.snapshot.ended.steps}
    flush_step = next(step for step in runtime.snapshot.ended.steps if step.id == "flush")
    log_step = next(step for step in runtime.snapshot.ended.steps if step.id == "log")
    assert finalized["n"] == 1, f"第 1 步该被调到恰好一次：{finalized}"
    assert flush_step.status == "failed", (flush_step.status, flush_step.error)
    assert "超出收尾预算" in (flush_step.error or ""), flush_step.error
    assert log_step.status == "done", (
        f"前一步被放弃之后 log 那一步仍该落定：{statuses}"
        f"（log 的 detail={log_step.detail!r}、error={log_step.error!r}）"
    )
    # 这一步做的是真 IO，判据因此落在盘上：本场的场次目录与 `session.json` 都该在
    disk = ws_path / SESSION_FILE_NAME
    assert disk.exists(), f"log 那一步该把这一场写下来：{disk} 不存在"
    # 被放弃的那次调用在收尾收场那一刻还卡在闸门上，而整趟收尾已经结束 —— 墙钟因此只落在
    # `log` 那一步自己的预算之内；池里只有一条线程时它会顶到整份额度那一档。
    assert still_flying["value"] is True, (
        "收尾收场那一刻，被放弃的那次调用该还在飞（闸门还没放开）"
    )
    assert spent < float(DRAIN_LOG_MIN_BUDGET_SECONDS) * DRAIN_ABANDONED_WALL_CLOCK_FACTOR, (
        f"这一趟收尾花了 {spent:.3f}s：前一步被放弃之后，后续步该立刻拿到一条线程"
    )
    print(
        f"[m7 证据] flush 卡在闸门上（窗口 {DRAIN_ABANDONED_FLUSH_WINDOW_SECONDS}s）-> "
        f"flush {flush_step.status}（error={flush_step.error!r}）；"
        f"log {log_step.status}（detail={log_step.detail!r}）；"
        f"盘上 {disk.name} 已写出；被放弃的 finalize 收场时仍在飞={still_flying['value']}；"
        f"整趟实测墙钟={spent:.3f}s"
    )


