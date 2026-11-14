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
    DRAIN_BLOCKING_STEP_SECONDS,
    DRAIN_POLL_TIMEOUT_SECONDS,
    DRAIN_WINDOW_ENTRY_DELAY_SECONDS,
    DRAIN_WINDOW_POLLS,
    DRAIN_WINDOW_POLL_SECONDS,
    _drain_evidence,
    _failed_steps_of,
    _log_with_all,
    _pin_fake,
    _service_log_text,
    _start,
    _start_and_stop,
    _start_service,
    _step_statuses,
    _wait_any_failed,
    _wait_done,
    _wait_state,
    _warm_up_client,
)

pytestmark = pytest.mark.slow


def test_post_step_completes_on_launch_return(service_process, meeting_env):
    """第 4 步 `post` 做完它该做的就落定，收尾不等任何会后链。

    按「结束录音」之后会后一个动作都不自动跑：那一步只剩「让会中模型退出显存」
    这一件事，做完就停在 `waiting`（＝等你动手），收尾仍在预算内正常落 `done`，
    盘上 `state == "done"`。
    """
    plugin = fakes.write_immediately_returning_post_plugin(meeting_env.home)
    fakes.write_plugin_config(meeting_env.home, plugin)

    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _pin_fake(plugin)

    _start_and_stop(proc)
    snapshot = _wait_done(proc)
    statuses = _step_statuses(snapshot["ended"]["steps"])
    assert statuses["post"] == "waiting"
    assert statuses["log"] == "done"

    record = read_session_json(snapshot["session"]["dir"])
    assert record is not None and record["state"] == "done"


def test_drain_logs_budget_and_elapsed(service_process, meeting_env):
    """收尾每一步的**开始 / 结束 / 耗时**与预算的**输入 / 结果**在日志文件里查得到。

    读的是**两个日志文件**（本文件不使用 `caplog`）：`MEETING_LOG_DIR` 下的**服务级 log**
    与场次目录下的**场次 log**。两者的分工照实情写：

      - **服务级 log** 是各步齐备的那一份 —— 场次 log 在 `log` 步落定那一刻就被摘掉，
        `log` 步自己的「结束」与「耗时」写在摘掉之后，只进得了服务级 log；
      - **场次 log** 里核摘掉之前的记录（收尾开始 + `flush` 的开始 / 结束 / 耗时 +
        `log` 步的开始），它同时是「收尾开始 + 预算输入 + 预算结果」落在本场那一份里的证据。

    这条用例只做自己那一遍必需的现场操作（一次 `start` + `stop`），读 log 一律走
    `wait_until()` 等两个文件都写到位再断言 —— 跨进程读文件撞上「还差最后一行」是偶发红的
    常见来源。
    """
    proc = _start_service(service_process, meeting_env)
    proc.wait_ready()
    _start_and_stop(proc)
    snapshot = _wait_done(proc)
    session_dir = Path(snapshot["session"]["dir"])

    # 场次 log：收尾起点、预算的输入与结果，加上摘掉之前那几步的记录
    session_log = wait_until(
        lambda: _log_with_all(
            session_dir / "meeting.log",
            ["收尾开始：", "budget=", "backlog=", "segment_timeout="]
            + [f"收尾步骤开始：{step_id}" for step_id in ("flush", "log")]
            + [f"收尾步骤结束：{step_id}" for step_id in ("flush",)]
            + [f"收尾步骤耗时：{step_id}" for step_id in ("flush",)],
        ),
        timeout=DRAIN_POLL_TIMEOUT_SECONDS,
    )
    assert session_log, "场次 log 里该有收尾开始与摘掉之前那几步的记录"
    assert "segment_timeout=" in session_log, "预算的另一个输入也该落在本场那一份里"

    service_log_path = meeting_env.log_dir / "meeting.log"
    service_log = wait_until(
        lambda: _log_with_all(
            service_log_path,
            ["收尾开始：", "budget=", "backlog="]
            + [
                f"收尾步骤开始：{step_id}"
                for step_id in DRAIN_STEP_IDS
            ]
            + [
                f"收尾步骤结束：{step_id}"
                for step_id in DRAIN_STEP_IDS
            ]
            + [
                f"收尾步骤耗时：{step_id}"
                for step_id in DRAIN_STEP_IDS
            ],
        ),
        timeout=DRAIN_POLL_TIMEOUT_SECONDS,
    )
    assert service_log, "服务级 log 里该有各步齐备的记录"

    for step_id in DRAIN_STEP_IDS:
        for needle, label in (
            (f"收尾步骤开始：{step_id}", "开始"),
            (f"收尾步骤结束：{step_id}", "结束"),
            (f"收尾步骤耗时：{step_id}", "耗时"),
        ):
            assert needle in service_log, (
                f"服务级 log 里查不到 {step_id} 那一步的{label}（{needle}）：\n"
                f"{service_log[-1500:]}"
            )
    # 预算的输入与结果：积压量、段长与算出来的那条上限都写着
    assert "budget=" in service_log
    assert "backlog=" in service_log
    assert "segment_timeout=" in service_log


def test_failed_step_keeps_service_alive_and_start_conflicts(service_process, meeting_env):
    """某步 `failed` 后服务照常活着；此时 `POST start` 回 409 `draining`。

    响应里带**失败那一步**与 **log 路径**；`ended.phase` 停在 `draining`、盘上 `state != "done"`。
    """
    plugin = fakes.write_failing_capture_plugin(meeting_env.home)
    fakes.write_plugin_config(meeting_env.home, plugin)

    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _pin_fake(plugin)
    _start_and_stop(proc)

    snapshot = _wait_any_failed(proc)
    ended = snapshot["ended"]
    assert ended["phase"] == "draining"
    assert ended["error"]

    # 服务不自杀：`GET /api/state` 仍 200（`proc.snapshot()` 自己就断言状态码）
    assert proc.snapshot()["top_state"] == "ended"
    assert proc.poll() is None

    record = read_session_json(snapshot["session"]["dir"])
    assert record is not None
    assert record["state"] == "failed"
    assert record["ended_at"]

    conflict = post_action(
        proc.base_url,
        "start",
        args={"dir": helpers.allocate_start_dir(proc.sessions_root)},
    )
    assert conflict.status_code == 409
    payload = conflict.json()
    assert payload["code"] == "draining"
    detail = payload["detail"]
    failed_ids = [step["id"] for step in _failed_steps_of(snapshot)]
    assert detail["failed_steps"], f"拒绝路径要带上失败那一步：{detail}"
    assert [item["id"] for item in detail["failed_steps"]] == failed_ids
    assert detail["log_path"], f"拒绝路径要带上 log 路径：{detail}"


def test_failed_step_records_both_ways_out(service_process, meeting_env):
    """收尾失败时，log 里能找到 `meeting post` 与「重启」两条出路。"""
    plugin = fakes.write_failing_capture_plugin(meeting_env.home)
    fakes.write_plugin_config(meeting_env.home, plugin)

    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _pin_fake(plugin)
    _start_and_stop(proc)

    snapshot = _wait_any_failed(proc)
    failed = _failed_steps_of(snapshot)[0]
    session_dir = snapshot["session"]["dir"]
    assert f"meeting post {session_dir}" in failed["error"]
    assert snapshot["ended"]["error"] and "meeting post" in snapshot["ended"]["error"]

    service_log = _service_log_text(meeting_env)
    assert "meeting post" in service_log
    assert "重启" in service_log


def test_state_stays_readable_while_draining(service_process, meeting_env):
    """收尾窗口里 `/api/state` 一直 200 且 `ended.phase == "draining"`（协程没把事件循环钉住）。

    用**钉在某一步 `running` 上的假件**造出这个窗口：假采集件的 `finalize()` 在工作线程里
    睡 `DRAIN_BLOCKING_STEP_SECONDS`（第 1 步 `flush` 因此停在 `running`）；同步阻塞若留在
    被调用的那一步里会把事件循环占住，这个窗口根本等不到。
    """
    plugin = fakes.write_fake_module(meeting_env.home)
    plugin.config["capture.backend"] = plugin.module("FakeCaptureBackend")
    plugin.extra_env[fakes.FINALIZE_DELAY_SECONDS_ENV] = str(float(DRAIN_BLOCKING_STEP_SECONDS))
    fakes.write_plugin_config(meeting_env.home, plugin)

    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _pin_fake(plugin)
    _warm_up_client(proc)
    _start(proc)

    assert post_action(proc.base_url, "stop").status_code == 200
    # 先把观察点挪进窗口，再轮询
    time.sleep(DRAIN_WINDOW_ENTRY_DELAY_SECONDS)
    during = _wait_state(
        proc,
        lambda snap: _step_statuses((snap.get("ended") or {}).get("steps") or []).get("flush")
        == "running",
        what="flush 那一步停在 running",
    )
    assert during["ended"]["phase"] == "draining", _drain_evidence(during, _service_log_text(meeting_env))

    # 窗口里连着读几次 `/api/state`：每一次都 200，且「那一步还在 running」与「phase 仍是
    # draining」两件事一起成立 —— 窗口之内的读才算数（现场证据只在断言真要报错时才去收集，
    # 免得读日志把窗口本身吃掉）
    for index in range(DRAIN_WINDOW_POLLS):
        snapshot = proc.snapshot()
        steps = _step_statuses(snapshot["ended"]["steps"])
        if snapshot["ended"]["phase"] != "draining" or steps["flush"] != "running":
            raise AssertionError(
                f"第 {index} 次读落在窗口之外：{_drain_evidence(snapshot, _service_log_text(meeting_env))}"
            )
        time.sleep(DRAIN_WINDOW_POLL_SECONDS)

    _wait_done(proc)


