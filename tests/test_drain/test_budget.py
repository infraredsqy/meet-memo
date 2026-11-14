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
    DRAIN_DRAG_TOTAL_SECONDS,
    DRAIN_LARGE_BACKLOG,
    DRAIN_LARGE_SEGMENT_TIMEOUT_SECONDS,
    DRAIN_OVERRUN_CLOCK_OFFSET_SECONDS,
    DRAIN_OVERRUN_MARKER_NAME,
    DRAIN_OVERRUN_STEP_SECONDS,
    DRAIN_SLOW_POST_STEP_SECONDS,
    DRAIN_SLOW_STEP_SECONDS,
    E2E_CASE_WALL_CLOCK_LIMIT_SECONDS,
    _drain_evidence,
    _inprocess_runtime,
    _new_workspace,
    _pin_fake,
    _record_steps,
    _service_log_text,
    _start_and_stop,
    _start_service,
    _step_statuses,
    _wait_any_failed,
    _wait_done,
    _warm_up_client,
)

pytestmark = pytest.mark.slow


def test_drain_budget_formula_and_bounds():
    assert DRAIN_BUDGET_MIN_SECONDS == 30
    assert DRAIN_BUDGET_MAX_SECONDS == 300
    assert compute_drain_budget(0, 20) == 30
    assert compute_drain_budget(1, 20) == 55
    assert compute_drain_budget(10, 20) == 280
    assert compute_drain_budget(100, 20) == 300
    assert compute_drain_budget(0, 0) == 30
    assert compute_drain_budget(2, 20) < compute_drain_budget(3, 20)


def test_drain_wait_limit_follows_backlog_budget(service_process, meeting_env):
    plugin = fakes.write_slow_drain_plugin(
        meeting_env.home,
        capture_delay_seconds=DRAIN_SLOW_STEP_SECONDS + DRAIN_SLOW_POST_STEP_SECONDS,
        post_delay_seconds=DRAIN_SLOW_POST_STEP_SECONDS,
    )
    fakes.write_plugin_config(meeting_env.home, plugin)

    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _pin_fake(plugin)

    backlog, segment_timeout = 0, 20.0  # `FakeTranscriberBackend` 的缺省（没给旋钮就是它）
    budget = compute_drain_budget(backlog, segment_timeout)
    assert budget == DRAIN_BUDGET_MIN_SECONDS

    began = time.monotonic()
    _start_and_stop(proc)
    snapshot = _wait_done(proc)
    elapsed = time.monotonic() - began

    statuses = _step_statuses(snapshot["ended"]["steps"])
    # 拖长的是 `flush` 一步（转码已摘出收尾序列，三步：flush / log / post）
    assert statuses["flush"] == "done", snapshot["ended"]
    assert statuses["post"] == "waiting", snapshot["ended"]
    assert statuses["log"] == "done", snapshot["ended"]
    assert elapsed >= DRAIN_DRAG_TOTAL_SECONDS, f"这两步该真的等满（实测 {elapsed:.2f}s）"
    upper = budget + DRAIN_LOG_MIN_BUDGET_SECONDS
    assert elapsed <= upper, f"一趟收尾 {elapsed:.2f}s 超出预算上限 {upper}s（deadline 没生效？）"
    assert elapsed <= E2E_CASE_WALL_CLOCK_LIMIT_SECONDS

    log_text = _service_log_text(meeting_env)
    assert f"backlog={backlog}" in log_text
    assert f"segment_timeout={segment_timeout}" in log_text
    assert "budget=" in log_text


def test_drain_budget_follows_a_large_backlog(service_process, meeting_env):
    plugin = fakes.write_slow_drain_plugin(
        meeting_env.home,
        capture_delay_seconds=DRAIN_SLOW_STEP_SECONDS,
        post_delay_seconds=DRAIN_SLOW_POST_STEP_SECONDS,
        backlog_depth=DRAIN_LARGE_BACKLOG,
        segment_timeout=DRAIN_LARGE_SEGMENT_TIMEOUT_SECONDS,
    )
    fakes.write_plugin_config(meeting_env.home, plugin)

    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _pin_fake(plugin)

    backlog = DRAIN_LARGE_BACKLOG
    segment_timeout = DRAIN_LARGE_SEGMENT_TIMEOUT_SECONDS
    budget = compute_drain_budget(backlog, segment_timeout)
    assert budget > DRAIN_BUDGET_MIN_SECONDS
    assert budget == (
        DRAIN_BUDGET_MIN_SECONDS
        + backlog * (segment_timeout + DRAIN_BUDGET_SEGMENT_OVERHEAD_SECONDS)
    )

    began = time.monotonic()
    _start_and_stop(proc)
    snapshot = _wait_done(proc)
    elapsed = time.monotonic() - began

    statuses = _step_statuses(snapshot["ended"]["steps"])
    assert statuses["log"] == "done", snapshot["ended"]
    assert statuses["post"] == "waiting", snapshot["ended"]
    assert set(statuses.values()) <= {"done", "skipped", "waiting"}

    log_text = _service_log_text(meeting_env)
    assert f"backlog={backlog}" in log_text, log_text[-1500:]
    assert f"segment_timeout={segment_timeout}s" in log_text, log_text[-1500:]
    logged = re.search(r"budget=([\d.]+)s", log_text)
    assert logged, f"服务级 log 里没有落预算结果：{log_text[-1500:]}"
    assert float(logged.group(1)) == float(budget), (
        f"预算函数算出 {budget}s，log 里落下的却是 {logged.group(1)}s —— "
        "运行时用的那条 deadline 与预算函数的输出对不上"
    )

    upper = budget + DRAIN_LOG_MIN_BUDGET_SECONDS
    assert elapsed <= upper, f"一趟收尾 {elapsed:.2f}s 超出预算上限 {upper}s（deadline 没生效？）"
    assert elapsed <= E2E_CASE_WALL_CLOCK_LIMIT_SECONDS


def test_step_over_budget_lands_failed_and_service_survives(service_process, meeting_env):
    plugin = fakes.write_clock_offset_plugin(
        meeting_env.home,
        finalize_delay_seconds=DRAIN_OVERRUN_STEP_SECONDS,
        clock_offset_seconds=DRAIN_OVERRUN_CLOCK_OFFSET_SECONDS,
        marker_path=meeting_env.home / DRAIN_OVERRUN_MARKER_NAME,
        post_delay_seconds=DRAIN_OVERRUN_STEP_SECONDS,
    )
    fakes.write_plugin_config(meeting_env.home, plugin)

    budget = compute_drain_budget(
        DRAIN_DEFAULT_BACKLOG, DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS
    )
    assert budget == DRAIN_BUDGET_MIN_SECONDS, budget
    assert DRAIN_OVERRUN_CLOCK_OFFSET_SECONDS > budget, (
        f"时钟偏移 {DRAIN_OVERRUN_CLOCK_OFFSET_SECONDS}s 必须大于这一档的预算 {budget}s，"
        "否则到期时剩余预算还大于 0、这一步不会被判越界"
    )

    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _pin_fake(plugin)
    _warm_up_client(proc)
    began = time.monotonic()
    _start_and_stop(proc)

    snapshot = _wait_any_failed(proc)
    elapsed = time.monotonic() - began
    statuses = _step_statuses(snapshot["ended"]["steps"])
    evidence = _drain_evidence(snapshot, _service_log_text(meeting_env))

    # 时钟偏移由 `flush` 那一步的 `finalize()` 写下，越界因此落在**它之后第一个被
    # `_drain_wait()` 包住的调用**上 —— 具体哪一格不写死
    failed = [step for step in snapshot["ended"]["steps"] if step["status"] == "failed"]
    assert failed, evidence
    overran = failed[-1]
    error = overran["error"] or ""
    assert "超出收尾预算" in error, f"越界那一步的 error 该写明超出收尾预算：{error!r}"
    assert re.search(r"budget=[\d.]+s", error), f"error 里该带那一次的 budget：{error!r}"
    assert re.search(r"实测耗时 [\d.]+s", error), f"error 里该带实测耗时：{error!r}"
    assert f"meeting post {snapshot['session']['dir']}" in error, error
    overran_budget = float(re.search(r"budget=([\d.]+)s", error).group(1))
    measured_spent = float(re.search(r"实测耗时 ([\d.]+)s", error).group(1))
    assert measured_spent > overran_budget, (
        f"error 里自报的实测耗时 {measured_spent}s 该大于那一次的 budget {overran_budget}s：{error!r}"
    )
    # 越界落在有真实调用的步骤里（`post` 那一格停在 `waiting`，越界因此不会落在它身上）
    assert overran["id"] in ("flush", "log"), evidence
    record = read_session_json(snapshot["session"]["dir"])
    assert record is not None, "盘上读不出 session.json"
    assert record["state"] == "failed", record
    assert record["ended_at"], record
    disk_steps = _step_statuses(_record_steps(record))
    assert disk_steps[overran["id"]] == "failed", record
    assert proc.poll() is None, "收尾有步骤 failed 时服务不该自杀"
    assert proc.snapshot()["top_state"] == "ended"
    assert snapshot["ended"]["phase"] == "draining", evidence

    print(
        f"[M5 证据] 这一趟的预算 = compute_drain_budget({DRAIN_DEFAULT_BACKLOG}, "
        f"{DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS}) = {budget}s"
        f"（DRAIN_BUDGET_MIN_SECONDS={DRAIN_BUDGET_MIN_SECONDS}）；"
        f"假件 finalize() 拖长 {DRAIN_OVERRUN_STEP_SECONDS}s，post 那一步的 launch() 写下 "
        f"{DRAIN_OVERRUN_CLOCK_OFFSET_SECONDS}s 的时钟前移标记（> 预算 → 那条 deadline 过期）\n"
        f"  越界那一步：post -> {statuses['post']}；error 自报 budget={overran_budget}s / "
        f"实测耗时 {measured_spent}s（后者大于前者 = 真的越界）\n"
        f"  error 原文：{error}\n"
        f"  盘上那一版：state={record['state']!r} / ended_at={record['ended_at']!r} / "
        f"steps={disk_steps}\n"
        f"  服务不自杀：poll()={proc.poll()!r}；top_state={proc.snapshot()['top_state']!r}；"
        f"这一趟**真实**墙钟 {elapsed:.2f}s（上限 {E2E_CASE_WALL_CLOCK_LIMIT_SECONDS}s）"
    )
    assert elapsed <= E2E_CASE_WALL_CLOCK_LIMIT_SECONDS


#: 那一趟收尾的总预算（秒）：小到后面某一步自己要用的时长就能把它用光 ——
#: 「预算耗尽」与「剩余预算逐级变小」这两件事因此都在零点几秒里量得出来。
DRAIN_SHARED_BUDGET_SECONDS = 0.05

#: 那一步的假件睡多久（秒）：**明显长于那时剩下的预算** ——
#: 它开跑时刻已经把 deadline 用掉了一大截（剩余预算比 `flush` 那一格小一档）。
DRAIN_SHARED_TRANSCODE_SECONDS = 0.06


def test_drain_steps_share_one_deadline_with_shrinking_remaining_budget(tmp_path, monkeypatch):
    from meeting.backends.registry import BackendResolution
    from meeting.service import runtime as runtime_module
    from meeting.state import begin_drain

    runtime = _inprocess_runtime(tmp_path, monkeypatch)

    backend = fakes.FakeCaptureBackend(
        finalize_delay_seconds=DRAIN_SHARED_TRANSCODE_SECONDS,
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
    # `post` 步只做一件事 —— 让会中模型退出显存，那一次调用同样经 `_drain_wait()`，
    # 因此这里给一台**带 `release_gpu()` 的假转写后端**（共享假件 `FakeTranscriberBackend`）。
    # **transcode 能力不给**：flush 越界落 `failed`，flush 落定自动起转码那一档不触发。
    transcriber = fakes.FakeTranscriberBackend()
    runtime.transcriber_backend = transcriber
    monkeypatch.setattr(runtime, "_resolve_backends", lambda: None)

    calls = {"budget": 0}

    def tiny_budget(backlog, segment_timeout):
        del backlog, segment_timeout
        calls["budget"] += 1
        return DRAIN_SHARED_BUDGET_SECONDS

    # 补丁打在 **`_run_drain_steps()` 所在模块**的那个全局名上：改 `meeting.state` 上的名字
    # 或只改聚合壳 `meeting.service.runtime` 的 re-export 绑定，`_run_drain_steps()` 都看不见。
    from meeting.service import runtime_drain as runtime_drain_module

    monkeypatch.setattr(runtime_drain_module, "compute_drain_budget", tiny_budget)

    # 记下每一步**实际经 `_drain_wait()` 开跑**那一刻的剩余预算与截止时刻，以及这一步
    # 真跑完之后的剩余预算。
    seen: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    collecting = True
    real_wait = runtime._drain_wait

    async def recording_wait(step_id, call, **kwargs):
        deadline = kwargs.get("deadline")
        began = time.monotonic()
        record = {
            "step": step_id,
            "remaining": None if deadline is None else max(0.0, float(deadline) - began),
            "deadline": None if deadline is None else float(deadline),
            "after": None,
        }
        (seen if collecting else manual).append(record)
        try:
            return await real_wait(step_id, call, **kwargs)
        finally:
            if deadline is not None:
                record["after"] = max(0.0, float(deadline) - time.monotonic())

    monkeypatch.setattr(runtime, "_drain_wait", recording_wait)

    async def one_pass() -> None:
        nonlocal collecting
        runtime.start_session(dir=_new_workspace(tmp_path, "deadline-share"))
        begin_drain(runtime.snapshot, reason="user")
        await runtime.drain()
        # 收尾编排里那一次 `_drain_wait()` 已在开跑之前就判越界（剩余预算 0），
        # 这里给 `post` 步一条新的 deadline，量的是「它也经同一层等待」。
        collecting = False
        await runtime._drain_post(deadline=time.monotonic() + DRAIN_SHARED_BUDGET_SECONDS)

    asyncio.run(one_pass())

    assert [item["step"] for item in seen] == list(DRAIN_STEP_IDS), seen
    statuses = {step.id: step.status for step in runtime.snapshot.ended.steps}
    # 变异（各处调用点改成 `deadline=None`）会让这一格读到 `None` —— 下面这句如实报出来，
    # 不靠格式化把信号盖掉
    assert seen[0]["remaining"] is not None, (
        f"第 1 步开跑时手上没有 deadline（各处调用点没把这一趟那条传下来）：{seen}"
    )
    assert statuses["flush"] == "failed", (
        f"{statuses}（这一步的假件睡了 {DRAIN_SHARED_TRANSCODE_SECONDS}s，"
        f"而它开跑时只剩 {seen[0]['remaining']:.4f}s）"
    )
    flush_step = next(step for step in runtime.snapshot.ended.steps if step.id == "flush")
    assert "超出收尾预算" in (flush_step.error or ""), flush_step.error
    # 收尾编排那一趟只算一次预算（单独再跑的 `post` 步不经过 `_run_drain_steps()`，不另算）
    assert calls["budget"] == 1, f"这一趟该只算一次预算，实测喊了 {calls['budget']} 次"
    # `post` 步做完它该做的（释放会中模型）就停在「等你动手」：三步里没有一步被漏掉
    assert statuses["post"] == "waiting", statuses
    # 释放被喊到（收尾编排那一趟 + 上面单独再跑的那一趟；两次都是真的经等待层交到工作线程）
    assert transcriber.released >= 1, transcriber.released
    # 那一次调用同样经 `_drain_wait()` 的等待层（给它的是新的一条 deadline）
    assert [item["step"] for item in manual] == ["post"], manual

    # ② 共享 deadline 的直接证据
    for item in seen:
        assert item["deadline"] is not None, (
            f"{item['step']} 那一步拿到的 deadline 是 None —— 每步各拿一份下限预算，"
            f"各步没有共享同一条 deadline：{seen}"
        )
    deadlines = {item["deadline"] for item in seen}
    assert len(deadlines) == 1, (
        f"各步该读到**同一条**截止时刻，实测有 {len(deadlines)} 条："
        f"{[(item['step'], item['deadline']) for item in seen]}"
    )
    remainings = [item["remaining"] for item in seen]
    assert all(
        later <= earlier for earlier, later in zip(remainings, remainings[1:])
    ), f"各步开跑时的剩余预算该逐级单调不增：{[(i['step'], i['remaining']) for i in seen]}"
    assert seen[0]["after"] == 0.0, (
        f"flush 跨过预算之后，这条 deadline 的剩余预算该是 0，实测 {seen[0]['after']!r}：{seen}"
    )
    print(
        f"[C1 证据] 预算固定为 compute_drain_budget(){DRAIN_SHARED_BUDGET_SECONDS}s，"
        f"flush 的假件睡 {DRAIN_SHARED_TRANSCODE_SECONDS}s（明显长于预算）：\n"
        f"  每一步开跑时的剩余预算 / 截止时刻："
        f"{[(item['step'], round(item['remaining'], 4), round(item['deadline'], 4)) for item in seen]}\n"
        f"  超预算那一步：flush -> {statuses['flush']}"
        f"（error={flush_step.error!r}）\n"
        f"  各步读到同一条截止时刻：{len(deadlines) == 1}"
        f"（{list(deadlines)[0]:.4f}）；"
        f"剩余预算 {remainings[0]:.4f}s -> {remainings[-1]:.4f}s"
    )


def test_drain_default_budget_inputs_when_transcriber_is_absent(tmp_path, monkeypatch):
    runtime = _inprocess_runtime(tmp_path, monkeypatch)
    runtime.transcriber_backend = None

    backlog, segment_timeout = runtime._drain_inputs()
    assert backlog == DRAIN_DEFAULT_BACKLOG, (
        f"转写后端缺席时 `backlog` 该取点名常量 DRAIN_DEFAULT_BACKLOG="
        f"{DRAIN_DEFAULT_BACKLOG}，实测 {backlog!r}"
    )
    assert segment_timeout == DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS, (
        f"转写后端缺席时 `segment_timeout` 该取点名常量 DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS="
        f"{DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS}，实测 {segment_timeout!r}"
    )

    budget = compute_drain_budget(backlog, segment_timeout)
    assert budget == DRAIN_BUDGET_MIN_SECONDS, (
        f"({backlog}, {segment_timeout}) 这一组缺省输入算出来的预算该是下限那一档 "
        f"{DRAIN_BUDGET_MIN_SECONDS}s，实测 {budget}s"
    )

    # 收尾没在跑、也没有 leftover deadline 的那一格
    runtime._drain_budget = budget
    runtime._drain_deadline = None
    assert runtime._drain_wait_timeout() == float(DRAIN_BUDGET_MIN_SECONDS) + float(
        DRAIN_LOG_MIN_BUDGET_SECONDS
    )

    print(
        f"[M3 证据] 转写后端缺席 -> _drain_inputs()=({backlog}, {segment_timeout})"
        f"（DRAIN_DEFAULT_BACKLOG={DRAIN_DEFAULT_BACKLOG} / "
        f"DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS={DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS}）"
        f" -> budget={budget}（DRAIN_BUDGET_MIN_SECONDS={DRAIN_BUDGET_MIN_SECONDS}）"
        f" -> _drain_wait_timeout()={runtime._drain_wait_timeout()}"
    )


