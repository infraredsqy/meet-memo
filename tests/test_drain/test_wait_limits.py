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
    register_inprocess_runtime,
)

pytestmark = pytest.mark.slow


@pytest.mark.parametrize(
    "invoke, mode",
    [
        (True, "sync-callable-in-worker-thread"),
        (False, "coroutine-on-the-loop"),
    ],
)
def test_drain_wait_wraps_every_call_in_the_remaining_budget(dev_runtime, invoke, mode):
    runtime = dev_runtime
    elapsed = DRAIN_UNIT_STEP_SECONDS
    if invoke:
        def step():
            time.sleep(elapsed)

    else:
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
    return await _returns_after(DRAIN_UNIT_STEP_SECONDS)


def test_remaining_budget_and_drain_wait_reject_a_missing_deadline(dev_runtime):
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


#: 那个「不该被跑」的同步步的时长（秒）：足够长，长到它一旦真的跑起来就会
#: 把事件循环钉住、把这条用例的墙钟顶起来。
DRAIN_FORGOTTEN_INVOKE_STEP_SECONDS = 0.2

#: 那一步的窗口（秒）：比假步短，于是「假步真跑起来」会额外体现为「等满上限」。
DRAIN_FORGOTTEN_INVOKE_WINDOW_SECONDS = 0.05


def test_sync_callable_without_invoke_true_fails_fast_and_keeps_the_loop_running(dev_runtime):
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
    from meeting import logging_setup as logging_setup_module
    from meeting.state import begin_drain

    runtime = _inprocess_runtime_with_backends(tmp_path, monkeypatch)

    async def flush_with_the_forgotten_invoke(deadline: float) -> None:
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


DRAIN_LOG_PROBLEM_TEXT = "写结束标记失败：OSError: [Errno 28] No space left on device"


def _inprocess_runtime_with_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, post_hook_absent: bool = True
):
    """进程内那一档的 `ServiceRuntime`，**后端已解析**（可直接开一场、跑一趟收尾）。
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
    # 后端解析在真服务里由 `startup()` 触发（本档不起 lifespan），这里显式走一遍 ——
    # 于是 `start_session()` 有一个在位的采集替身，四条收尾也按 registry 的结论登记。
    runtime._resolve_backends()
    # 这一台起过的真转写流水线由包级 conftest 的 autouse 夹具在收场 join
    # （transcriber.backend=auto 在 `start_session()` 里起真线程，登记是收场的凭据）
    register_inprocess_runtime(runtime)
    return runtime


def _inprocess_config(
    tmp_path: Path, overrides: dict[str, Any], *, post_hook_absent: bool = True
):
    """进程内用例的一小份配置：**默认值表原样** + 本用例的输出根 + 点名要改的那几个键。

    默认值表直接取 `config.DEFAULT_VALUES`（不手抄一份：`settings` 视图、告警阈值与后端解析
    都要读全键集，抄一份迟早漏键）。

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
    assert runtime.snapshot.ended.phase == "draining", runtime.snapshot.ended.phase
    assert runtime.snapshot.top_state == "ended", runtime.snapshot.top_state
    assert statuses["post"] == "waiting", statuses
    post_step = next(step for step in runtime.snapshot.ended.steps if step.id == "post")
    assert "等你动手" in (post_step.detail or ""), post_step.detail
    assert set(statuses.values()) <= set(DRAIN_STEP_STATUS), statuses
    print(
        f"[m3 证据] 场次日志写失败：log 步 -> {log_step.status}"
        f"（error={log_step.error!r}）；ended.phase={runtime.snapshot.ended.phase}；"
        f"四步状态={statuses}"
    )


def test_post_step_waits_for_the_user_under_the_product_default_hook(tmp_path, monkeypatch):
    from meeting.backends import post as post_module
    from meeting.config import DEFAULT_VALUES
    from meeting.state import begin_drain

    assert DEFAULT_VALUES["post.hook"] == "auto", DEFAULT_VALUES["post.hook"]

    runtime = _inprocess_runtime_with_backends(
        tmp_path, monkeypatch, post_hook_absent=False
    )
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


#: 整趟收尾的预算（秒）：小到第 1 步一定会越过它
DRAIN_ABANDONED_TOTAL_BUDGET_SECONDS = 0.05

#: 第 1 步的窗口（秒）：整趟预算就是它的全部额度，越过即被放弃
DRAIN_ABANDONED_FLUSH_WINDOW_SECONDS = 0.05

#: 闸门的等待上限（秒）：闸门由用例在收尾跑完之后放开，这只是「万一没放开」的兜底
DRAIN_ABANDONED_GATE_SECONDS = 10.0

#: 整趟收尾墙钟的上界换算：`log` 那一步的额度（`DRAIN_LOG_MIN_BUDGET_SECONDS`）乘这个
#: 倍数。前一步被放弃之后，后续步该**立刻**拿到一条线程 —— 整趟墙钟因此落在「那一步自己的
#: 预算」附近，而线程池只有一条时它会顶到整份额度（5 秒）那一档。
DRAIN_ABANDONED_WALL_CLOCK_FACTOR = 0.8


def test_later_steps_keep_a_worker_after_an_earlier_step_is_abandoned(tmp_path, monkeypatch):
    import threading

    from meeting.backends.registry import BackendResolution
    from meeting.state import begin_drain

    runtime = _inprocess_runtime_with_backends(tmp_path, monkeypatch)
    gate = threading.Event()
    finalized = {"n": 0}

    class GatedCapture:
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
    disk = ws_path / SESSION_FILE_NAME
    assert disk.exists(), f"log 那一步该把这一场写下来：{disk} 不存在"
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


