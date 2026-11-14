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
    ALERT_POLL_TIMEOUT_SECONDS,
    DRAIN_LOG_CLOSED_RECHECK_SECONDS,
    DRAIN_POLL_TIMEOUT_SECONDS,
    END_MARKER_TEXT,
    SESSION_STATE_DOMAIN,
    TEST_RECOVER_SECONDS,
    TEST_SILENCE_FAULT_SECONDS,
    TEST_SILENCE_SECONDS,
    _record_steps,
    _service_log_text,
    _session_dir_of_log,
    _start,
    _start_service,
    _step_statuses,
    _wait_done,
    _wait_end_marker,
    _wait_state,
    _warm_up_client,
)

pytestmark = pytest.mark.slow


def test_session_log_attached_then_detached_with_end_marker(service_process, meeting_env):
    """录音期场次 log 在长；收尾后该文件存在、**最后一行是结束标记**、且此后不再增长。"""
    proc = _start_service(service_process, meeting_env)
    proc.wait_ready()

    _start(proc)
    session_dir = _session_dir_of_log(_service_log_text(meeting_env))
    session_log_path = Path(session_dir) / "meeting.log"

    # 录音期本身没有 INFO 级事件，文件先空着
    wait_until(session_log_path.exists, timeout=DRAIN_POLL_TIMEOUT_SECONDS)
    assert session_log_path.exists(), f"`start` 之后场次 log 该已经挂上：{session_log_path}"

    assert post_action(proc.base_url, "stop").status_code == 200
    snapshot = _wait_done(proc)

    session_log = _wait_end_marker(session_dir)
    lines = [line for line in session_log.splitlines() if line.strip()]
    assert lines, "场次 log 不该为空"
    marker_index = next(
        (index for index, line in enumerate(lines) if END_MARKER_TEXT in line), None
    )
    assert marker_index is not None, f"场次 log 里没有结束标记：\n{session_log}"
    assert marker_index == len(lines) - 1, (
        f"结束标记必须是最后一行：它落在第 {marker_index + 1} 行，"
        f"而最后一个非空行是第 {len(lines)} 行\n{lines[marker_index:]}"
    )

    # 标记是 `log` 步**在它自己那一刻**写下的：那一刻 `state` 还是 `draining`（收尾没走完），
    # 四步概要是 `log=running`（正在写它）、`post=waiting`（还没开跑）。这两件事因此按
    # 「**在 `log` 步那一刻成立的取值**」逐项核 —— 而不是拿收尾走完之后盘上那一版去套，
    # 那样会把「标记**照实**写下了它那一刻的收尾进度」这条事实读成一句对不上。
    record = read_session_json(session_dir)
    assert record is not None, f"盘上读不出 {session_dir}/session.json"
    marker = lines[marker_index]
    assert f"session={Path(session_dir).name} " in marker, (
        f"标记该带场次 id（= 场次目录名）：{marker!r}"
    )
    state = marker.split("场次结束：state=", 1)[1].split("；", 1)[0]
    assert state in SESSION_STATE_DOMAIN, f"标记里的 state 落在取值域之外：{state!r}（{marker!r}）"
    # 收尾概要：**各档按 `DRAIN_STEP_IDS` 的原顺序**逐项报出
    summary = marker.split("收尾步骤：", 1)[1].split("；", 1)[0]
    statuses = [item.strip() for item in summary.split(",")]
    assert len(statuses) == len(DRAIN_STEP_IDS), (
        f"收尾概该正好 {len(DRAIN_STEP_IDS)} 项（按 {list(DRAIN_STEP_IDS)} 的原顺序）：{marker!r}"
    )
    assert set(statuses) <= set(DRAIN_STEP_STATUS), marker
    by_id = dict(zip(DRAIN_STEP_IDS, statuses))
    # 写这一行的是 `log` 步自己：它那一刻是 `running`，它后面的 `post` 还没开跑
    assert by_id["log"] == "running", marker
    assert by_id["post"] == "waiting", marker
    # 它前面那一步在有真实实现的构建下记 `done`（骨架构建是 `skipped`；两者都算落定）
    assert by_id["flush"] in ("done", "skipped"), marker
    # 收尾走完之后盘上那一版：`done` + 四步都有终态（标记之后收尾照常把剩下的走完）
    assert record["state"] == "done", record
    assert set(step["status"] for step in _record_steps(record)) <= {
        "done", "skipped", "waiting",
    }, record
    assert _step_statuses(_record_steps(record))["post"] == "waiting", record
    assert marker.rstrip().endswith(f"服务级 log：{snapshot['service']['log_path']}"), (
        f"标记末尾该是服务级 log 路径（{snapshot['service']['log_path']!r}）：{marker!r}"
    )

    size_after = session_log_path.stat().st_size
    time.sleep(DRAIN_LOG_CLOSED_RECHECK_SECONDS)
    assert session_log_path.stat().st_size == size_after


def test_all_log_event_categories_findable_before_end_marker(service_process, meeting_env):
    """结束标记**之前**这一场的事件逐类可查。

    事件由 dev 采集替身与采集后端的收尾调用造出来。阈值只**显式调小**（写进临时 home 的
    `config.json`，**不改代码默认值**）。

    **故障档取 `silence`**：它一路把 loopback 从 `capturing` 推到 `lost`（阈值
    `alert.silence_seconds`）、恢复判定再推到 `restored`，于是「每一路健康迁移」这一类在一趟里
    出现**三段真实迁移**。`lost` 那一档同样落这一类事件，但它一跳就到、看不出 `restored` 那
    一段，因此这里用 `silence`。

    **「场次开始与目录」这一类取服务级 log**：场次 log 是 `start` 成功之后才挂上的 handler，
    而那一行按时间顺序落在挂上之前 —— 它进不了场次 log。这一条因此按**两类 log 联合**读：
    场次 log 里取本场的 session id / 目录 / 「场次结束」，服务级 log 里取「场次开始」那一行
    （带同一个 id 与目录）。别为了迎合字面去场次 log 里伪造一行。
    """
    plugin = fakes.write_fake_module(meeting_env.home)
    plugin.config.update(
        {
            "capture.backend": "dev",
            "alert.silence_seconds": TEST_SILENCE_SECONDS,
            "alert.recover_seconds": TEST_RECOVER_SECONDS,
        }
    )
    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _warm_up_client(proc)

    _start(proc)
    session_dir = _session_dir_of_log(_service_log_text(meeting_env))

    injected = post_action(
        proc.base_url,
        "dev_inject",
        {
            "source": "loopback",
            "fault": "silence",
            "after_seconds": 0,
            "duration_seconds": TEST_SILENCE_FAULT_SECONDS,
        },
    )
    assert injected.status_code == 200, injected.text

    _wait_state(
        proc,
        lambda snap: snap["top_state"] == "alert",
        what="一路静默把 top_state 推到 alert",
        timeout=ALERT_POLL_TIMEOUT_SECONDS,
    )
    # 等它恢复（`alert.recover_seconds` 已调小、静默时长到这里就到点），好让「告警起止」
    # 这一类事件在一趟里齐活（观察窗口 `TEST_SILENCE_FAULT_SECONDS` 因此要长过静默阈值）
    _wait_state(
        proc,
        lambda snap: snap["top_state"] == "recording",
        what="告警恢复回 recording",
        timeout=ALERT_POLL_TIMEOUT_SECONDS,
    )

    assert post_action(proc.base_url, "stop").status_code == 200
    _wait_done(proc)

    session_log = _wait_end_marker(session_dir)
    marker_index = session_log.find(END_MARKER_TEXT)
    assert marker_index > 0, f"场次 log 里没有结束标记：\n{session_log}"
    before = session_log[:marker_index]

    def check(needle: str, label: str) -> None:
        assert needle in before, f"结束标记之前查不到「{label}」（{needle}）：\n{before}"

    session_id = session_dir.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    check("场次结束：", "场次结束与目录（本场那一份里的收尾痕迹）")
    check(session_id, "场次 id")
    check(session_dir, "场次目录路径")
    check("告警起：", "告警起")
    check("状态迁移：alert -> recording", "告警止（恢复）")
    check("alarm.sound", "出声开关（抑制口径）")
    # 断言按路写：**mic 那一行**在场（它整场都在采，这一档不掺别的路的话就查不到），
    # 以及 loopback 那一路的三段真实迁移 —— 采集替身注入的静默把它从 `capturing` 推到
    # `lost`（阈值 `alert.silence_seconds` 已被显式调小），恢复判定再把它推到 `restored`，
    # `stop` 进收尾时推到 `stopped`。
    check("每一路健康迁移：mic", "每一路健康迁移（mic 那一路）")
    check("每一路健康迁移：loopback", "每一路健康迁移（loopback 那一路）")
    check(
        f"每一路健康迁移：loopback capturing -> lost（原因：这一路起致命级告警",
        "一路从 capturing 迁到 lost（含原因）",
    )
    check(
        "每一路健康迁移：loopback lost -> restored（原因：连续有信号达恢复阈值",
        "一路从 lost 迁回 restored（含恢复原因）",
    )
    check("阈值：静默阈值 alert.silence_seconds=", "迁到 lost 那一条的阈值")
    check("阈值：恢复阈值 alert.recover_seconds=", "迁回 restored 那一条的阈值")
    check("收尾开始：", "收尾起点")
    check("backlog=", "预算输入 backlog")
    check("budget=", "预算结果")
    check("耗时", "每一步的耗时")
    # 场次 log 在 `log` 步落定那一刻被摘掉：它里面只有 `flush` 那一步的完整记录
    # （`log` 步自己的开始 / 结束与 `post` 步的一切都只进服务级 log）
    for step_id in ("flush",):
        check(f"收尾步骤开始：{step_id}", f"{step_id} 的开始")
        check(f"收尾步骤结束：{step_id}", f"{step_id} 的结束")

    # 服务级 log 那半边：**「场次开始与目录」落在这里**，加上两路设备探测与各步齐备的收尾记录。
    service_log = _service_log_text(meeting_env)
    assert "场次开始：" in service_log, "服务级 log 缺「场次开始」"
    assert session_id in service_log, "服务级 log 的「场次开始」该带本场的 id"
    assert session_dir in service_log, "服务级 log 的「场次开始」该带本场的目录"
    assert "设备探测：mic" in service_log, "服务级 log 缺 mic 那一路的探测记录"
    assert "设备探测：loopback" in service_log, "服务级 log 缺 loopback 那一路的探测记录"
    for step_id in DRAIN_STEP_IDS:
        assert f"收尾步骤开始：{step_id}" in service_log
        assert f"收尾步骤结束：{step_id}" in service_log

    # 结束标记之后不再有这一场的行（摘掉之后的消息只走服务级 log；标记自己那行里的
    # 「收尾步骤：」概要不算 —— 判据点的是步骤的开始 / 结束行）
    after = session_log[marker_index:]
    assert "收尾步骤开始" not in after and "收尾步骤结束" not in after, (
        f"结束标记之后还有这一场的行：\n{after}"
    )
