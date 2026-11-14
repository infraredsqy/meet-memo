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

pytestmark = pytest.mark.slow


def test_drain_wait_upper_bound_tracks_the_passed_deadline(dev_runtime):
    runtime = dev_runtime

    def step() -> str:
        time.sleep(DRAIN_DEADLINE_STEP_SECONDS)
        return "跑完了"

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
    # 的窗口可以把上限读出来。**三样一起给**：`_flush` 那一步的判定读的是**解析结论**
    # （`capture_resolution` 缺席就记 `skipped`，拖长的假件根本不会被调到）。
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
        assert sample["running_when_sampled"], (
            f"backlog={backlog} 那一档的读数落在「收尾还在跑」之外：{sample}"
        )
        predicted = budget - sample["spent"] + float(DRAIN_LOG_MIN_BUDGET_SECONDS)
        assert abs(sample["wait_timeout"] - predicted) <= DRAIN_SETTLE_WAIT_TOLERANCE_SECONDS, (
            f"backlog={backlog} 那一档的可等待上限该贴着 "
            f"compute_drain_budget({backlog}, {segment_timeout}) − 已用 "
            f"{sample['spent']:.3f}s + DRAIN_LOG_MIN_BUDGET_SECONDS = {predicted:.3f}s"
            f"（容差 {DRAIN_SETTLE_WAIT_TOLERANCE_SECONDS}s），实测 {sample['wait_timeout']:.3f}s"
        )
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


def test_cancelled_drain_routes_through_the_frozen_accounting(tmp_path, monkeypatch):
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

    # **记下 `finish_drain_step` / `_finish_drain` 收到的那些关键字**。
    # 补丁打在 **`_settle_step()` 所在模块 `meeting.service.runtime_drain` 的模块级名字**上：
    # `_settle_step()` 读的是**它自己模块的那个全局名** —— 改 `meeting.state` 上的同名属性、
    # 或只改聚合壳 `meeting.service.runtime` 的 re-export 绑定，都不会被它看见。
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

    frozen_calls = [item for item in seen["finish_drain_step"] if item["frozen_ended"] is True]
    assert frozen_calls, f"取消路径没有把 frozen_ended 置真：{seen['finish_drain_step']}"
    assert all(item["frozen_interrupted"] is True for item in frozen_calls), seen[
        "finish_drain_step"
    ]
    assert seen["finish_drain"] == [True], seen["finish_drain"]
    print(f"[M2 证据] 取消路径上的登记调用：{seen['finish_drain_step']}；终态提交：{seen['finish_drain']}")

    statuses = {step.id: step.status for step in runtime.snapshot.ended.steps}
    assert statuses["flush"] == "running", statuses
    assert set(statuses.values()) <= set(DRAIN_STEP_STATUS), statuses
    assert "failed" not in statuses.values(), statuses
    for step_id in DRAIN_STEP_IDS[1:]:
        assert statuses[step_id] == "waiting", statuses
    assert runtime.snapshot.ended.phase == "draining", runtime.snapshot.ended
    assert runtime._drain_task is None, "走完（含被取消那趟）后句柄该置空"

    after = read_session_json(session.dir)
    assert after["interrupted"] is True, after
    assert after["state"] == "draining", after
    assert after["ended_at"] is None, after
    print(
        f"[M2 证据] 取消之后盘上那一版：state={after['state']!r} / "
        f"interrupted={after['interrupted']!r} / ended_at={after['ended_at']!r}"
        f"（与先落盘那一版逐字段相同）"
    )


def test_cancelled_drain_keeps_draining_even_with_a_failed_step_before_it(tmp_path, monkeypatch):
    from meeting.backends.registry import BackendResolution
    from meeting.cli import EXIT_INTERRUPTED, _drain_exit_code
    from meeting.state import begin_drain

    runtime = _inprocess_runtime(tmp_path, monkeypatch)

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
        runtime._shutdown_interrupted = True
        runtime.mark_interrupted()
        await runtime._cancel_drain_task()

    asyncio.run(interrupt_while_the_last_step_runs())

    snapshot = runtime.snapshot
    assert snapshot.ended.phase == "draining", snapshot.ended
    statuses = {step.id: step.status for step in snapshot.ended.steps}
    assert statuses["flush"] == "failed", statuses
    assert statuses["log"] == "running", statuses
    assert statuses["post"] == "waiting", statuses

    after = read_session_json(session.dir)
    assert after is not None, "盘上读不出 session.json"
    assert after["state"] == "draining", (
        f"强制打断档盘上该留在 draining（前面那一步 failed 不该把它顶成 failed）：{after}"
    )
    assert after["interrupted"] is True, after
    assert after["ended_at"] is None, after
    disk_steps = _step_statuses(_record_steps(after))
    assert disk_steps == statuses, (disk_steps, statuses)

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


def test_disk_has_no_ended_at_while_draining_only_at_the_terminal_version(tmp_path, monkeypatch):
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
    from meeting import logging_setup as logging_setup_module

    def boom_end_marker(*args, **kwargs):
        raise RuntimeError("假场次日志接缝：按测试要求抛错")

    monkeypatch.setattr(
        logging_setup_module, "write_session_end_marker", boom_end_marker
    )
    monkeypatch.setattr(runtime, "_resolve_backends", lambda: None)

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
    bad = [item for item in versions if item["state"] == "draining" and item["ended_at"]]
    assert not bad, (
        f"盘上出现了「收尾中却写着 ended_at」的版本（`log` 步提前写它就是这么来的）：{bad}\n"
        f"这一趟全部版本：{versions}"
    )
    draining_versions = [item for item in versions if item["state"] == "draining"]
    assert draining_versions, f"这一趟该有过「收尾中」那一版：{versions}"
    assert all(item["ended_at"] is None for item in draining_versions), draining_versions
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


