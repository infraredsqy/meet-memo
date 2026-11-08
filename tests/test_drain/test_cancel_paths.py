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
    DRAIN_CANCEL_POLL_SECONDS,
    DRAIN_CANCEL_STEP_SECONDS,
    DRAIN_DEADLINE_STEP_SECONDS,
    DRAIN_OVERRUN_BACKLOG_LARGE,
    DRAIN_OVERRUN_BACKLOG_SMALL,
    DRAIN_OVERRUN_POLL_SECONDS,
    DRAIN_OVERRUN_STEP_SECONDS,
    DRAIN_POLL_TIMEOUT_SECONDS,
    DRAIN_SETTLE_WAIT_TOLERANCE_SECONDS,
    DRAIN_TIGHT_DEADLINE_SECONDS,
    DRAIN_WIDE_DEADLINE_SECONDS,
    _inprocess_runtime,
    _new_workspace,
    _record_steps,
    _step_statuses,
)

#: 慢档标记：`pytest.mark.slow` 的归位判据见 `docs/service.md`。
pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# ⑤c 上限跟着**传进来的那条预算**走（**进程内**，两组小 deadline）
#
# 审查问题 M3 / M4 要的那一半就在这一条：上限来自预算函数、且它随预算变化。两组毫秒级的
# deadline 把「随预算变化」量成可判定的事实，而不必真等满一条 30 秒的预算。
# ---------------------------------------------------------------------------


def test_drain_wait_upper_bound_tracks_the_passed_deadline(dev_runtime):
    """一趟收尾的**等待上限就是传进来的那条预算**，不会是别的什么秒数（审查问题 M3 / M4 的判据）。

    「不许再出现写死的 10 秒收尾等待」这条判据要能咬住两件事：上限**来自预算**、且它**随预算
    变化**。同一条 0.2 秒的假步配两条 deadline：

      - **紧**的那条（0.02 秒）在假步跑完之前就到期 → 这一步落 `DrainLimitExceeded`，
        自报的实测耗时贴着 0.02 秒（**远小于**假步那 0.2 秒）；
      - **宽**的那条（0.5 秒）把同一段假步放行 → 调用正常返回，`_drain_wait()` 报出的实测耗时
        落在这一段假步自己的时长上（0.2 秒上下），**不会**被迫等在一条更长的预算上。

    两条合起来说明：上限跟着传进来的 deadline 走。若它是个写死的常量（V1 那个 `timeout=10`），
    紧的那一趟会**照跑完 0.2 秒**、第一条断言当场红；宽的那一趟也不会刚好在 0.2 秒上下收住。
    这个现场跑在进程内、量级在毫秒到零点几秒之间，「真等满一条预算」因此不必去等 30 秒。
    """
    runtime = dev_runtime

    def step() -> str:
        time.sleep(DRAIN_DEADLINE_STEP_SECONDS)
        return "跑完了"

    # 紧的那条 deadline：上限明显短于这一步自己的时长 → 超出即 `DrainLimitExceeded`
    started = time.monotonic()
    with pytest.raises(DrainLimitExceeded) as caught:
        asyncio.run(
            runtime._drain_wait(
                "flush",
                step,
                deadline=started + DRAIN_TIGHT_DEADLINE_SECONDS,
                invoke=True,
            )
        )
    message = str(caught.value)
    match = re.search(r"实测耗时 ([\d.]+)s", message)
    assert match, message
    measured = float(match.group(1))
    assert measured < DRAIN_DEADLINE_STEP_SECONDS / 2, (
        f"{DRAIN_TIGHT_DEADLINE_SECONDS}s 的上限没能拦住这一步：它跑了 {measured}s"
        f"（自己的时长是 {DRAIN_DEADLINE_STEP_SECONDS}s）—— 上限没跟着传进来的 deadline 走"
        f"（{message}）"
    )

    # 宽的那条 deadline：同一段假步被放行，实测耗时就是这一步自己的时长（比紧的那趟宽得多）
    value, wide_measured = asyncio.run(
        runtime._drain_wait(
            "flush",
            step,
            deadline=time.monotonic() + DRAIN_WIDE_DEADLINE_SECONDS,
            invoke=True,
        )
    )
    assert value == "跑完了"
    assert DRAIN_DEADLINE_STEP_SECONDS <= wide_measured < DRAIN_DEADLINE_STEP_SECONDS * 3, (
        f"宽 deadline 那一趟的实测耗时 {wide_measured}s 该落在"
        f" [{DRAIN_DEADLINE_STEP_SECONDS}, {DRAIN_DEADLINE_STEP_SECONDS * 3}) 之内 —— "
        "上限跟着预算走，这一步才走得出这个区间"
    )


def test_drain_whole_pass_upper_bound_grows_with_the_backlog(tmp_path, monkeypatch):
    """同一个拖长假步配两组 `backlog`：可等待的上限随 `compute_drain_budget()` 的输出变大（M3）。

    两组现场只差 `backlog_depth()` 报的数（`DRAIN_OVERRUN_BACKLOG_SMALL` / `..._LARGE`），
    `segment_timeout()` 都是 `DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS`：

      - **小积压档**：`compute_drain_budget()` 的返回值就是下限那一格
        （`DRAIN_BUDGET_MIN_SECONDS`）；
      - **大积压档**：同一函数产出公式那一档（明显离开下限），运行时那一趟的可等待上限
        因此比小积压档**大出预算函数算出的那一整笔差额**。

    每档三件事一起断：这一趟的收尾**真的在跑**（在飞的那一步停在 `running`，测量点因此落在
    「本次预算」那一格）、「等落定」的上限**贴着预算函数对同一组输入算出来的值加上 `log` 步
    最小预算**（只差测量那一刻收尾用掉的时间，容差 `DRAIN_SETTLE_WAIT_TOLERANCE_SECONDS`）、
    两档的上限之差**就是两组预算之差**（同一条容差）。收尾真的还在跑、两档的上限真的差出
    一整笔预算 —— 这两件事都量到了。
    """
    backlog_small = DRAIN_OVERRUN_BACKLOG_SMALL
    backlog_large = DRAIN_OVERRUN_BACKLOG_LARGE
    segment_timeout = DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS
    budget_small = compute_drain_budget(backlog_small, segment_timeout)
    budget_large = compute_drain_budget(backlog_large, segment_timeout)
    assert budget_small == DRAIN_BUDGET_MIN_SECONDS, budget_small
    assert budget_large > budget_small, (budget_small, budget_large)

    from meeting.backends.registry import BackendResolution
    from meeting.state import begin_drain

    runtime = _inprocess_runtime(tmp_path, monkeypatch)

    # 采集假件：`finalize()` 拖长 `DRAIN_OVERRUN_STEP_SECONDS`，于是收尾有一小段「确实还在跑」
    # 的窗口可以把上限读出来。**三样一起给**（与 ⑤e 条同一处口径）：`_flush` 那一步的判定读的
    # 是**解析结论**（`capture_resolution` 缺席就记 `skipped`，拖长的假件根本不会被调到）。
    # `transcode` 这一项如实开成 `False`（本用例不需要第 2 步跑起来），收尾第 2 步因此照常记
    # `skipped`，这一条量的仍然是「同一趟收尾的上限」
    backend = fakes.FakeCaptureBackend(finalize_delay_seconds=DRAIN_OVERRUN_STEP_SECONDS)
    runtime.capture_backend = backend
    runtime.capture_capabilities = {"finalize": True, "transcode": False, "dev_inject": False}
    runtime.capture_resolution = BackendResolution(
        key="capture.backend",
        selector="fake",
        target="meeting_drain_fakes:FakeCaptureBackend",
        implementation=backend,
        has_implementation=True,
        detail="本用例造的现场（进程内直接注入运行时成员）",
    )
    monkeypatch.setattr(runtime, "_resolve_backends", lambda: None)

    transcriber = fakes.FakeTranscriberBackend(
        backlog_depth=backlog_small, segment_timeout=segment_timeout
    )
    monkeypatch.setattr(runtime, "transcriber_backend", transcriber)
    # 预算输入读的是本用例那两档（与 `ServiceRuntime._drain_inputs()` 同形）
    monkeypatch.setattr(
        runtime,
        "_drain_inputs",
        lambda: (int(transcriber.backlog_depth()), float(transcriber.segment_timeout())),
    )

    samples: dict[int, dict[str, Any]] = {}

    async def one_pass(backlog: int) -> dict[str, Any]:
        transcriber._backlog = int(backlog)
        session = runtime.start_session(dir=_new_workspace(tmp_path, f"budget-{int(backlog)}"))
        begin_drain(runtime.snapshot, reason="user")
        task = asyncio.get_running_loop().create_task(runtime.drain())
        drain_started = time.monotonic()
        # **确定的同步点**：轮询到在飞的那一步进入 `running`（收尾确实在跑），才把上限读出来。
        # `drain()` 把这一趟的预算交给运行时的那一刻起算，量到这里花掉的时间：上限就是
        # 「本次预算 − 已用时间 + log 步最小预算」（§3.3 的「剩余预算」那一句）
        deadline = time.monotonic() + DRAIN_POLL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if runtime.snapshot.drain_step("flush").status == "running":
                break
            await asyncio.sleep(DRAIN_OVERRUN_POLL_SECONDS)
        else:  # pragma: no cover - 收尾一定先进 running
            raise AssertionError(
                f"等不到 flush 那一步进入 running（实测 "
                f"{runtime.snapshot.drain_step('flush').status!r}）"
            )
        sampled_at = time.monotonic()
        wait_timeout = float(runtime._drain_wait_timeout())
        # 收尾确实还在跑：读数与「这一趟还没落定」同时成立
        running = not runtime.drain_has_settled()
        await task
        return {
            "backlog": int(backlog),
            "wait_timeout": wait_timeout,
            "spent": sampled_at - drain_started,
            "running_when_sampled": running,
            "session_dir": session.dir,
            "phase": runtime.snapshot.ended.phase,
        }

    for backlog in (backlog_small, backlog_large):
        samples[backlog] = asyncio.run(one_pass(backlog))

    for backlog, sample in samples.items():
        budget = compute_drain_budget(backlog, segment_timeout)
        # 这一档的收尾**确实还在跑**（在飞的那一步停在 `running`）：读数因此落在
        # 「本次那笔预算」那一格（§3.3 的「等待上限 = 这条 deadline 的剩余时间 加 log 步最小预算」）
        assert sample["running_when_sampled"], (
            f"backlog={backlog} 那一档的读数落在「收尾还在跑」之外：{sample}"
        )
        # 上限**取自这一档的预算**：读到的值 = 那笔预算 − 收尾已经用掉的时间 + `log` 步保底。
        # 减掉实测的 `spent` 之后，它与预算函数对同一组输入算出来的值逐值相等（差额只是一次
        # 轮询往返的调度开销，容差 `DRAIN_SETTLE_WAIT_TOLERANCE_SECONDS`）
        predicted = budget - sample["spent"] + float(DRAIN_LOG_MIN_BUDGET_SECONDS)
        assert abs(sample["wait_timeout"] - predicted) <= DRAIN_SETTLE_WAIT_TOLERANCE_SECONDS, (
            f"backlog={backlog} 那一档的可等待上限该贴着 "
            f"compute_drain_budget({backlog}, {segment_timeout}) − 已用 "
            f"{sample['spent']:.3f}s + DRAIN_LOG_MIN_BUDGET_SECONDS = {predicted:.3f}s"
            f"（容差 {DRAIN_SETTLE_WAIT_TOLERANCE_SECONDS}s），实测 {sample['wait_timeout']:.3f}s"
        )
    # **这一条要量的关系**：积压量调大 → 可等待的上限随之变大，差额就是两组预算之差
    grew = samples[backlog_large]["wait_timeout"] - samples[backlog_small]["wait_timeout"]
    budget_gap = budget_large - budget_small
    spend_gap = samples[backlog_large]["spent"] - samples[backlog_small]["spent"]
    expected_gap = budget_gap - spend_gap
    assert abs(grew - expected_gap) <= DRAIN_SETTLE_WAIT_TOLERANCE_SECONDS, (
        f"两档可等待的上限之差该是两组预算之差 {budget_gap}s 扣掉两趟测量点之差 {spend_gap:.3f}s "
        f"= {expected_gap:.3f}s（容差 {DRAIN_SETTLE_WAIT_TOLERANCE_SECONDS}s），实测 {grew:.3f}s "
        "—— 一趟收尾的总时长没有随积压量变大"
    )
    assert grew > DRAIN_OVERRUN_STEP_SECONDS, grew
    # 反过来读一遍这条关系：小档的可等待上限落在**大档那条预算之下**（两组分得开）
    assert samples[backlog_small]["wait_timeout"] < budget_large, (
        samples[backlog_small],
        budget_large,
    )

    print(
        f"[M3 证据] 同一个拖长假步（finalize() 睡 {DRAIN_OVERRUN_STEP_SECONDS}s）配两组积压量：\n"
        f"  小档 backlog={backlog_small} → compute_drain_budget={budget_small}s → "
        f"可等待上限实测 {samples[backlog_small]['wait_timeout']:.3f}s"
        f"（收尾在跑：{samples[backlog_small]['running_when_sampled']}、终态 "
        f"{samples[backlog_small]['phase']!r}）\n"
        f"  大档 backlog={backlog_large} → compute_drain_budget={budget_large}s → "
        f"可等待上限实测 {samples[backlog_large]['wait_timeout']:.3f}s"
        f"（收尾在跑：{samples[backlog_large]['running_when_sampled']}、终态 "
        f"{samples[backlog_large]['phase']!r}）\n"
        f"  两档之差实测 {grew:.3f}s ≈ 预算函数两档之差 {budget_gap}s"
    )


# ---------------------------------------------------------------------------
# ⑤e 取消路径真的经 `_settle_step(..., cancelled=True)`（**进程内**，审查问题 M2）
#
# 契约 §3.5 第 3 条与 §2.4 要求「取消时按收尾步骤表落盘」，并给出三条**不覆盖约束**：
# 不许把第 1 步写下的 `interrupted: true` 抹回 `false`、不许改 `state`、不许补 `ended_at`。
# `ServiceRuntime._settle_step(..., cancelled=True)`（它把 `frozen_ended` / `frozen_interrupted`
# 双双置真）就是这条机制 —— 但在修本条之前**一个调用点都没有**，`frozen_*` 收到的永远是
# 默认的 `False`，那三条约束没有一条被实际走到（相2 的
# `tests/test_state_machine.py::test_cancelled_drain_keeps_the_same_face_without_done`
# 只经 `finish_drain()`，走不到 `_settle_step` 这一层）。这一条把「真的接线了」钉住。
# ---------------------------------------------------------------------------


def test_cancelled_drain_routes_through_the_frozen_accounting(tmp_path, monkeypatch):
    """收尾被 `cancel()` 时，**取消路径真的经 `cancelled=True` 那条登记机制**（审查问题 M2）。

    现场（进程内、可控、毫秒级）：把在飞的那一步钉住（假采集后端的 `finalize()` 睡
    `DRAIN_CANCEL_STEP_SECONDS`），等它进入 `running` 之后 `cancel()` 这一趟收尾。三条都断：

      1. **机制真的被走到**：`finish_drain_step` 收到的 `frozen_ended` 与 `frozen_interrupted`
         都是 `True`（`_settle_step(..., cancelled=True)` 的两处实参），`_finish_drain` 收到的
         `cancelled` 也是 `True`；
      2. **在飞那一步不伪造终态**：它照实记 `running`（取消打断的这一步既没跑完也没抛错），
         已落定的与未开跑的一个都不改 —— 契约 §3.5 第 3 条的「已落定的步骤保持原样、
         未开跑的维持 `waiting`」；
      3. **三条不覆盖约束在盘上成立**：先按强制打断的顺序写一版
         `state="draining"` + `interrupted=true` + `ended_at=None`（就是 `mark_interrupted()`
         那一版），取消之后**逐字段不变**：`interrupted` 还是 `true`、`state` 还是 `draining`、
         `ended_at` 还是 `None`。

    整条用例不起服务、只跑一趟被取消的 `drain()`，墙钟在零点几秒的量级。
    """
    from meeting.backends.registry import BackendResolution
    from meeting.state import DRAIN_STEP_STATUS, begin_drain

    runtime = _inprocess_runtime(tmp_path, monkeypatch)

    # **先把后端的解析结论钉住，再建场次**（顺序是硬要求）：`start_session()` 内部会经
    # `_resolve_backends()` 重算 `capture_backend` / `capture_capabilities` / `capture_resolution`
    # 三样，先注入假件就会被它整个盖掉 —— 那样第 1 步会因为「采集替身没有 `finalize` 能力」
    # 记 `skipped`，「在飞的那一步被取消」这个同步点根本造不出来。
    backend = fakes.FakeCaptureBackend(finalize_delay_seconds=DRAIN_CANCEL_STEP_SECONDS)
    runtime.capture_backend = backend
    runtime.capture_capabilities = {"finalize": True, "transcode": False, "dev_inject": False}
    runtime.capture_resolution = BackendResolution(
        key="capture.backend",
        selector="fake",
        target="meeting_drain_fakes:FakeCaptureBackend",
        implementation=backend,
        has_implementation=True,
        detail="本用例造的现场（进程内直接注入运行时成员）",
    )
    monkeypatch.setattr(runtime, "_resolve_backends", lambda: None)

    session = runtime.start_session(dir=_new_workspace(tmp_path, "resolve-pinned"))
    assert runtime.capture_backend is backend, "解析结论该是本用例注入的那一份（没有被重算盖掉）"
    begin_drain(runtime.snapshot, reason="user")
    runtime._write_session(interrupted=True)  # 强制打断的**先落盘**那一版
    interrupted_before = read_session_json(session.dir)
    assert interrupted_before["state"] == "draining", interrupted_before
    assert interrupted_before["interrupted"] is True, interrupted_before
    assert interrupted_before["ended_at"] is None, interrupted_before

    # **记下 `finish_drain_step` / `_finish_drain` 收到的那些关键字**（相2 的登记出口）。
    # 补丁打在 **`_settle_step()` 所在模块 `meeting.service.runtime_drain` 的模块级名字**上：
    # 拆分后 `runtime_drain.py` 是 `from meeting.state import finish_drain_step` 这样取
    # 进来的，`_settle_step()` 读的是**它自己模块的那个全局名** —— 改 `meeting.state` 上的
    # 同名属性、或只改聚合壳 `meeting.service.runtime` 的 re-export 绑定，都不会被它看见
    # （这一步的功夫全下在「补丁打在哪一个命名空间」：全局名挂对了，登记出口才会被记到）。
    from meeting.service import runtime_drain as runtime_drain_module

    seen: dict[str, Any] = {"finish_drain_step": [], "finish_drain": []}
    real_finish_step = runtime_drain_module.finish_drain_step

    def spy_finish_step(step_id, status, detail=None, error=None, **kwargs):
        seen["finish_drain_step"].append(
            {
                "step_id": step_id,
                "status": status,
                "frozen_ended": kwargs.get("frozen_ended"),
                "frozen_interrupted": kwargs.get("frozen_interrupted"),
            }
        )
        return real_finish_step(step_id, status, detail, error, **kwargs)

    monkeypatch.setattr(runtime_drain_module, "finish_drain_step", spy_finish_step)
    real_finish_drain = runtime._finish_drain

    def spy_finish_drain(*, cancelled: bool = False):
        seen["finish_drain"].append(cancelled)
        return real_finish_drain(cancelled=cancelled)

    monkeypatch.setattr(runtime, "_finish_drain", spy_finish_drain)

    async def cancel_during_the_blocked_step() -> None:
        task = asyncio.get_running_loop().create_task(runtime.drain())
        # 确定同步点：轮询到 `flush` 那一步进了 `running`（收尾确实在跑）再取消
        deadline = time.monotonic() + DRAIN_POLL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if runtime.snapshot.drain_step("flush").status == "running":
                break
            await asyncio.sleep(DRAIN_CANCEL_POLL_SECONDS)
        else:  # pragma: no cover - 收尾一定先进 running
            raise AssertionError(
                f"等不到 flush 那一步进入 running（实测 {runtime.snapshot.drain_step('flush').status!r}）"
            )
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(cancel_during_the_blocked_step())

    # ① 机制真的被走到：两处 `frozen_*` 都是 True，终态提交也带 cancelled=True
    frozen_calls = [item for item in seen["finish_drain_step"] if item["frozen_ended"] is True]
    assert frozen_calls, f"取消路径没有把 frozen_ended 置真：{seen['finish_drain_step']}"
    assert all(item["frozen_interrupted"] is True for item in frozen_calls), seen[
        "finish_drain_step"
    ]
    assert seen["finish_drain"] == [True], seen["finish_drain"]
    print(f"[M2 证据] 取消路径上的登记调用：{seen['finish_drain_step']}；终态提交：{seen['finish_drain']}")

    # ② 在飞那一步照实记 running（不伪造终态）；其余步骤维持原样
    statuses = {step.id: step.status for step in runtime.snapshot.ended.steps}
    assert statuses["flush"] == "running", statuses
    assert set(statuses.values()) <= set(DRAIN_STEP_STATUS), statuses
    assert "failed" not in statuses.values(), statuses
    for step_id in DRAIN_STEP_IDS[1:]:
        assert statuses[step_id] == "waiting", statuses
    assert runtime.snapshot.ended.phase == "draining", runtime.snapshot.ended
    assert runtime._drain_task is None, "走完（含被取消那趟）后句柄该置空"

    # ③ 三条不覆盖约束在盘上成立：先落盘那一版逐字段不变
    after = read_session_json(session.dir)
    assert after["interrupted"] is True, after
    assert after["state"] == "draining", after
    assert after["ended_at"] is None, after
    print(
        f"[M2 证据] 取消之后盘上那一版：state={after['state']!r} / "
        f"interrupted={after['interrupted']!r} / ended_at={after['ended_at']!r}"
        f"（与先落盘那一版逐字段相同）"
    )


# ---------------------------------------------------------------------------
# ⑤i 强制打断档：现场**前面已有 `failed` 步**时，取消路径的落盘仍把 `state` 钉在 `draining`
#     （**进程内**，审查问题 C2）
#
# §3.5 表第三 / 第四行给这一档写死的盘上三件套是「`state: "draining"`、`interrupted: true`、
# 无 `ended_at`」，第 3 条的第二条不覆盖约束又补了一句「**不许把 `state` 从 `draining` 改成
# 别的值**」。但取消路径的落盘走的是 `state_for()` 的四档映射：现场里前面已经有一步 `failed`
# 时，那份映射按 `has_failed_step → failed` 算出 `"failed"`，把 `mark_interrupted()` 刚写下的
# `draining` **覆盖掉**——于是「强制打断 + 前面有失败步」这一格盘上留的是 `state: "failed"`，
# 与契约要求的那三件套对不上。
#
# **收口的位置在 phase3 这条路径上**：`state_for()` 的四档映射是 phase2 冻结的跨 Phase 契约
# （取值域与优先级一个字都不动），改的是**取消路径的写值** —— `_settle_step(..., cancelled=True)`
# 落盘时显式把 `state="draining"` 传下去（见 `ServiceRuntime._write_session()` 的 `state=` 参数），
# 不让映射在这一格上接管。这一条把该行为钉住。
# ---------------------------------------------------------------------------


def test_cancelled_drain_keeps_draining_even_with_a_failed_step_before_it(tmp_path, monkeypatch):
    """强制打断档：前面已有 `failed` 步 + 收尾在飞 → 盘上仍是 `draining` + `interrupted`（C2）。

    现场（进程内、毫秒级）：第 1 步 `flush` 的假件**抛错**（有实现却没做成 → 落 `failed`），
    第 2 步 `transcode` 的同一个假件把这一刻钉住（它的 `transcode()` 睡
    `DRAIN_CANCEL_STEP_SECONDS`，收尾因此停在「`flush` 已落定、`transcode` 正在跑」这一格）；
    随后按 §3.5 的顺序走**强制打断**那一趟：先 `mark_interrupted()` 落盘
    （`draining` + `interrupted: true` + 无 `ended_at`），再 `cancel()` 收尾 task。

    **钉住的为什么换成了第 2 步**：第 4 步 `post` 按「结束录音」之后不自动拉起会后（R26），
    它做完该做的（释放会中模型）就落 `waiting` —— 拿它已经钉不住「收尾还在跑」这一刻了。

    四件事一起断：

      1. **盘上 `state` 仍是 `draining`**（前面那一步 `failed` 还在，`ended.phase` 也仍是
         `draining`）—— 取消路径的落盘不许把它改成 `failed`；
      2. `interrupted is True`（先落盘那一版没被抹回否）；
      3. **无 `ended_at`**（强制打断路径不写它）；
      4. **原 `steps` 保留**：`flush` 仍是 `failed`、在飞那一步照实记 `running`
         （不伪造终态）；退出码那一档由 `meeting/cli.py` 的 `_drain_exit_code()` 给出
         `EXIT_INTERRUPTED`。
    """
    from meeting.backends.registry import BackendResolution
    from meeting.cli import EXIT_INTERRUPTED, _drain_exit_code
    from meeting.state import begin_drain

    runtime = _inprocess_runtime(tmp_path, monkeypatch)

    # 第 1 步「有实现却抛错」（采集假件的 finalize() 抛错）；在飞那一步改钉在 `log` 上 ——
    # 转码已摘出收尾序列，`log` 步的结束标记由本用例换成会睡一小段的假件（真实实现照常
    # 在它身后，睡完照常落）
    backend = fakes.FakeCaptureBackend(
        finalize_error="C2 现场的采集归档落盘失败",
    )
    runtime.capture_backend = backend
    runtime.capture_capabilities = {"finalize": True, "transcode": False, "dev_inject": False}
    runtime.capture_resolution = BackendResolution(
        key="capture.backend",
        selector="fake",
        target="meeting_drain_fakes:FakeCaptureBackend",
        implementation=backend,
        has_implementation=True,
        detail="本用例造的现场（进程内直接注入运行时成员）",
    )
    monkeypatch.setattr(runtime, "_resolve_backends", lambda: None)

    from meeting import logging_setup as logging_setup_module

    real_end_marker = logging_setup_module.write_session_end_marker

    def slow_end_marker(*args, **kwargs):
        time.sleep(DRAIN_CANCEL_STEP_SECONDS)
        return real_end_marker(*args, **kwargs)

    monkeypatch.setattr(logging_setup_module, "write_session_end_marker", slow_end_marker)

    session = runtime.start_session(dir=_new_workspace(tmp_path, "resolve-pinned"))

    async def interrupt_while_the_last_step_runs() -> None:
        # `begin_drain()` 与收尾协程都必须在事件循环这一侧（要在同一条循环上跑得起来）
        begin_drain(runtime.snapshot, reason="user")
        task = asyncio.get_running_loop().create_task(runtime.drain())
        runtime._drain_task = task
        # 确定同步点：`log` 那一步真的进了 `running`（收尾确实还在跑）再打断
        deadline = time.monotonic() + DRAIN_POLL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if runtime.snapshot.drain_step("log").status == "running":
                break
            await asyncio.sleep(DRAIN_CANCEL_POLL_SECONDS)
        else:  # pragma: no cover - 收尾一定走到 log 那一步
            raise AssertionError(
                f"等不到 log 那一步进入 running（实测 "
                f"{runtime.snapshot.drain_step('log').status!r}）"
            )
        # 强制打断那一趟的顺序（§3.5）：**先落盘**、**再 cancel()**
        runtime._shutdown_interrupted = True
        runtime.mark_interrupted()
        await runtime._cancel_drain_task()

    asyncio.run(interrupt_while_the_last_step_runs())

    # 现场确实是我们说的那一格：前面有 `failed` 步，且收尾这一趟已经被打断
    snapshot = runtime.snapshot
    assert snapshot.ended.phase == "draining", snapshot.ended
    statuses = {step.id: step.status for step in snapshot.ended.steps}
    assert statuses["flush"] == "failed", statuses
    assert statuses["log"] == "running", statuses
    assert statuses["post"] == "waiting", statuses

    # 换实现之前的那一版会在这里拿到 `state == "failed"`（映射按「有 failed 步」算出来的）
    after = read_session_json(session.dir)
    assert after is not None, "盘上读不出 session.json"
    assert after["state"] == "draining", (
        f"强制打断档盘上该留在 draining（前面那一步 failed 不该把它顶成 failed）：{after}"
    )
    assert after["interrupted"] is True, after
    assert after["ended_at"] is None, after
    # 原 `steps` 保留：已落定的保持原样、在飞那一步照实记 running、不伪造终态
    disk_steps = _step_statuses(_record_steps(after))
    assert disk_steps == statuses, (disk_steps, statuses)

    # 退出码那一档（`cli.py` 的辅助按「本次关停是不是强制打断」挑）
    assert runtime.shutdown_interrupted is True
    assert _drain_exit_code(runtime, fallback=0) == EXIT_INTERRUPTED
    print(
        f"[C2 证据] 强制打断 + 前面已有 failed 步这一格：\n"
        f"  盘上 state={after['state']!r} / interrupted={after['interrupted']!r} / "
        f"ended_at={after['ended_at']!r}\n"
        f"  steps（盘上与快照逐项相同）：{disk_steps}\n"
        f"  ended.phase={snapshot.ended.phase!r}；退出码 = EXIT_INTERRUPTED"
        f"（{_drain_exit_code(runtime, fallback=0)}）"
    )


# ---------------------------------------------------------------------------
# ⑤j 收尾进行中盘上没有 `ended_at`，终态那一版才有（**进程内**，审查问题 m8）
#
# §2.4 的只读契约那一行写死了：**`ended_at` 只在收尾走到终态（`done` / `failed`）时写**
# （这两处都由 `finish_drain()` 落值），强制打断路径不写。此前 `log` 那一步也在写它 ——
# 于是盘上会出现「`state=draining` + `ended_at` 有值」的瞬态（那一刻收尾还没走完），
# 重启扫描与 `meeting post` 读到的现场就多了一个自相矛盾的字段。
#
# 这一条把「盘上每一版都不许出现那种组合」变成可判定的：代理 `write_session_json()`，
# 把这一场**每一次落盘的内容**都抄一份，再逐版检查。收尾进行中（`log` 步刚落下那一刻）
# 的每一版都该是 `ended_at is None`；终态那一版才该有值。
# ---------------------------------------------------------------------------


def test_disk_has_no_ended_at_while_draining_only_at_the_terminal_version(tmp_path, monkeypatch):
    """收尾进行中盘上没有 `ended_at`，收尾走到终态那一版才有（m8）。

    现场：第 3 步 `log` 写场次日志结束标记时抛错（那一步 `failed`）——收尾因此会走到**失败终态**
    那一版（`state == "failed"` + `ended_at` 有值，§3.4 第 2 条），于是「进行中」与「终态」两版
    在同一条用例里都出现得了。（原先由第 4 步的会后钩子抛错来演，而按 R26 会后已经不再自动跑。）

    判据分两半：

      - **逐版扫一遍**：这一场写下去的每一版里，**没有哪一版**是「`state == "draining"`
        却有 `ended_at`」的组合（那正是 `log` 步提前写它造出来的瞬态）；
      - **两个端点各自成立**：收尾还在跑的那几版 `ended_at` 全是 `None`，而最后一版
        （终态）`ended_at` 有值、`state == "failed"`。
    """
    from meeting.backends.registry import BackendResolution
    from meeting.state import begin_drain

    runtime = _inprocess_runtime(tmp_path, monkeypatch)

    backend = fakes.FakeCaptureBackend()
    runtime.capture_backend = backend
    runtime.capture_capabilities = {"finalize": True, "transcode": False, "dev_inject": False}
    runtime.capture_resolution = BackendResolution(
        key="capture.backend",
        selector="fake",
        target="meeting_drain_fakes:FakeCaptureBackend",
        implementation=backend,
        has_implementation=True,
        detail="本用例造的现场（进程内直接注入运行时成员）",
    )
    # 第 3 步写场次日志结束标记时抛错 → 收尾走到**失败终态**（`ended_at` 有值的那一版）
    from meeting import logging_setup as logging_setup_module

    def boom_end_marker(*args, **kwargs):
        raise RuntimeError("假场次日志接缝：按测试要求抛错")

    monkeypatch.setattr(
        logging_setup_module, "write_session_end_marker", boom_end_marker
    )
    monkeypatch.setattr(runtime, "_resolve_backends", lambda: None)

    # 把这一场**每一次落盘**的内容抄下来（代理写在 store 的方法上，运行时的写盘入口不变）
    versions: list[dict[str, Any]] = []
    real_write = runtime.store.write_session_json

    def recording_write(session_dir, record):
        versions.append({"state": record.get("state"), "ended_at": record.get("ended_at")})
        return real_write(session_dir, record)

    monkeypatch.setattr(runtime.store, "write_session_json", recording_write)

    async def one_pass() -> None:
        runtime.start_session(dir=_new_workspace(tmp_path, "end-marker-versions"))
        begin_drain(runtime.snapshot, reason="user")
        await runtime.drain()

    asyncio.run(one_pass())

    assert versions, "这一趟该至少落过一版盘"
    # ① 逐版扫：没有哪一版是「state=draining + ended_at 有值」这种自相矛盾的组合
    bad = [item for item in versions if item["state"] == "draining" and item["ended_at"]]
    assert not bad, (
        f"盘上出现了「收尾中却写着 ended_at」的版本（`log` 步提前写它就是这么来的）：{bad}\n"
        f"这一趟全部版本：{versions}"
    )
    # ② 两个端点：收尾还在跑的那几版 `ended_at` 都是 None
    draining_versions = [item for item in versions if item["state"] == "draining"]
    assert draining_versions, f"这一趟该有过「收尾中」那一版：{versions}"
    assert all(item["ended_at"] is None for item in draining_versions), draining_versions
    # ③ 终态那一版才有 `ended_at`，且盘上与快照都对得上
    final = versions[-1]
    assert final["state"] == "failed", versions
    assert final["ended_at"], f"终态那一版该写 ended_at：{versions}"
    record = read_session_json(runtime.snapshot.session.dir)
    assert record["state"] == "failed" and record["ended_at"], record
    print(
        f"[m8 证据] 这一趟盘上共落了 {len(versions)} 版：\n"
        f"  收尾进行中的每一版 ended_at 都是 None（共 "
        f"{len(draining_versions)} 版）；「state=draining 且有 ended_at」的版本：{len(bad)}\n"
        f"  终态那一版：state={final['state']!r} / ended_at={final['ended_at']!r}"
    )


