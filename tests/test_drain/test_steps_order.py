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
    DRAIN_FROZEN_RECHECK_SECONDS,
    ENDED_PHASE_DOMAIN,
    _all_steps_settled,
    _drain_evidence,
    _ended_phase,
    _pin_fake,
    _record_steps,
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


def test_drain_step_order_and_terminal_states(service_process, meeting_env):
    """`stop` 后 `ended.phase` 经 `draining` 到 `done`；四步顺序与终态照契约。

    **`draining` 这一格在盘上与状态接口上都真的被观察到、也真的被断言**：
    骨架构建下四步几乎瞬间跑完，靠抢时间窗口去读 `draining` 会 flaky —— 这里用**在工作线程里
    睡 `DRAIN_BLOCKING_STEP_SECONDS`** 的假采集件把第 1 步 `flush` 钉在 `running` 上
    （`finalize()` 那一格就是收尾第 1 步的调用），收尾因此在这 `DRAIN_BLOCKING_STEP_SECONDS`
    秒里稳稳停在 `draining`：轮询到 `ended.phase == "draining"`、至少一步还没落定、
    盘上那一版同时写着 `state == "draining"`，**再**等它落定成 `done`。
    本用例末尾把那两次读到的相位与 `ENDED_PHASE_DOMAIN` 逐项对上 —— `draining` 与 `done`
    两格都由这一趟收尾自己走过。

    **第 4 步 `post` 不再被任何钩子接住**：按「结束录音」之后会后一个动作都不自动跑，
    那一步做完它该做的（释放会中模型）就停在 `waiting`（＝界面上那句"等你动手"）。
    采集侧是 dev 替身（`config.json` 里 `capture.backend = "dev"`，**非产品路径**）：
    它不写真实音频，`finalize` / `transcode` 两项能力如实为假，因此 `flush` / `transcode`
    两步记 `skipped`；`log` 这一格在任何构建里都有真实实现 → `done`。
    四步 `status` 全部收在 `DRAIN_STEP_STATUS` 的成员上。
    """
    plugin = fakes.write_fake_module(meeting_env.home)
    # 第 1 步 `flush` 由假采集件的 `finalize()` 拖住
    plugin.config["capture.backend"] = plugin.module("FakeCaptureBackend")
    plugin.extra_env[fakes.FINALIZE_DELAY_SECONDS_ENV] = str(float(DRAIN_BLOCKING_STEP_SECONDS))
    fakes.write_plugin_config(meeting_env.home, plugin)

    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _pin_fake(plugin)
    # 把用例自己那条 HTTP 连接先建好：观察点才落得进收尾那扇窗口
    _warm_up_client(proc)
    _start(proc)

    assert post_action(proc.base_url, "stop").status_code == 200

    def draining_with_unsettled_steps(snap):
        """收尾停在 `draining`：至少一步还没落定，且盘上那一版也写着 `draining`。"""
        if _ended_phase(snap) != "draining" or _all_steps_settled(snap):
            return None
        record = read_session_json(snap["session"]["dir"])
        return snap if record is not None and record["state"] == "draining" else None

    during = _wait_state(
        proc,
        draining_with_unsettled_steps,
        what="收尾停在 draining 且至少一步还没落定",
    )
    assert _ended_phase(during) == "draining"
    assert during["top_state"] == "ended"
    assert not _all_steps_settled(during), _drain_evidence(
        during, _service_log_text(meeting_env)
    )
    # 窗口里第 1 步正被假采集件钉住（这条断言只是把「还没落定的那一步是谁」说清楚）
    assert _step_statuses(during["ended"]["steps"])["flush"] in ("waiting", "running")
    assert read_session_json(during["session"]["dir"])["state"] == "draining"

    snapshot = _wait_done(proc)
    ended = snapshot["ended"]
    assert ended["phase"] == "done"
    assert [step["id"] for step in ended["steps"]] == list(DRAIN_STEP_IDS)
    statuses = [step["status"] for step in ended["steps"]]
    assert "failed" not in statuses
    assert _step_statuses(ended["steps"])["log"] == "done"
    assert _step_statuses(ended["steps"])["post"] == "waiting"

    # 「`draining` → `done`」这半句的实测依据：这一趟收尾先后读到的两个相位**正好是取值域的
    # 那两项**（少一项或冒出第三项，这一行当场红）
    assert {_ended_phase(during), _ended_phase(snapshot)} == set(ENDED_PHASE_DOMAIN)

    # 收尾终态不回 `idle`：`top_state` 停在 `ended`、`session` 保留（本场时长冻结）
    assert snapshot["top_state"] == "ended"
    assert snapshot["session"] is not None
    assert snapshot["session"]["frozen"] is True


def test_drain_reaches_done_when_no_step_failed(service_process, meeting_env):
    """四步全落在 `done` / `skipped` → `ended.phase == "done"`；`top_state` 停在 `ended`。

    同一趟顺带核盘上那一版（`state` / `ended_at` / `interrupted` / `drain.steps`）与「本场时长
    冻结」（收尾之后 `elapsed_seconds` 与 `frozen` 都不再变）。
    """
    proc = _start_service(service_process, meeting_env)
    proc.wait_ready()
    _start_and_stop(proc)

    snapshot = _wait_done(proc)
    ended = snapshot["ended"]
    assert all(step["status"] in ("done", "skipped", "waiting") for step in ended["steps"])
    assert _step_statuses(ended["steps"])["post"] == "waiting"
    assert ended["phase"] == "done"

    assert snapshot["top_state"] == "ended"
    assert snapshot["session"] is not None
    frozen_elapsed = snapshot["session"]["elapsed_seconds"]
    frozen_frozen = snapshot["session"]["frozen"]
    time.sleep(DRAIN_FROZEN_RECHECK_SECONDS)
    reread = proc.snapshot()["session"]
    assert reread["elapsed_seconds"] == frozen_elapsed
    assert reread["frozen"] is frozen_frozen is True

    record = read_session_json(snapshot["session"]["dir"])
    assert record is not None
    assert record["state"] == "done"
    assert record["ended_at"]
    assert record["interrupted"] is False
    assert [step["id"] for step in _record_steps(record)] == list(DRAIN_STEP_IDS)


def test_skipped_and_failed_follow_registry_resolution_without_exception_swallowing(
    service_process, meeting_env
):
    """沿用 registry 的解析结论：**缺席记 `skipped`、有实现却抛错记 `failed`**。

    行为断言证明「异常吞成 `skipped`」这条路不在实现里：给在场实现注入一个非预期异常
    （`finalize()` 抛 `OSError`），那一步必须落到 `failed`（转码已摘出收尾序列：它成了
    这一场的独立后台工序，`flush` 失败不挡 `log` / `post` 两步照常登记 ——「某一步
    `failed` 之后，后续步骤照常按顺序登记」这条铁律由本条钉住）。
    """
    plugin = fakes.write_failing_capture_plugin(meeting_env.home)
    fakes.write_plugin_config(meeting_env.home, plugin)

    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _pin_fake(plugin)
    _start_and_stop(proc)

    snapshot = _wait_any_failed(proc)
    statuses = _step_statuses(snapshot["ended"]["steps"])
    detail = _service_log_text(meeting_env)
    assert statuses["flush"] == "failed", _drain_evidence(snapshot, detail)
    assert set(statuses) == {"flush", "log", "post"}, sorted(statuses)
    assert statuses["log"] == "done"
    flush = next(step for step in snapshot["ended"]["steps"] if step["id"] == "flush")
    assert flush["error"] and "OSError" in flush["error"]
    assert snapshot["ended"]["phase"] == "draining"


