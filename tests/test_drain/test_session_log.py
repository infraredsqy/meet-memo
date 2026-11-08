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

#: 慢档标记：`pytest.mark.slow` 的归位判据见 `docs/service.md`。
pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# ⑪ 场次 log 挂摘
# ---------------------------------------------------------------------------


def test_session_log_attached_then_detached_with_end_marker(service_process, meeting_env):
    """录音期场次 log 在长；收尾后该文件存在、**最后一行是结束标记**、且此后不再增长。

    **「最后一行」照 §3.7 的严格口径断**（审查问题 M3）：标记所在行的**下标**必须等于最后
    一个非空行的下标 —— 也就是**标记之后没有任何非空行**。⑫ 条用的 `marker_index` 与这里
    同一个口径（那条看的是标记**之前**的每一类事件）。

    **标记的内容逐字段核一遍**（审查问题 M8，§3.7「一行带**场次 id** 与收尾结果（含 `state`
    与四步 `status` 概要）的记录」）：场次 id = 场次目录名、`state` = 盘上那一版、
    四步概要按 `id=status` 逐项与盘上 `drain.steps` 对齐、服务级 log 路径与快照里的
    `service.log_path` 一致。
    """
    proc = _start_service(service_process, meeting_env)
    proc.wait_ready()

    _start(proc)
    session_dir = _session_dir_of_log(_service_log_text(meeting_env))
    session_log_path = Path(session_dir) / "meeting.log"

    # **挂上**：`start` 成功之后场次 log 就在位（录音期本身没有 INFO 级事件，文件先空着）
    wait_until(session_log_path.exists, timeout=DRAIN_POLL_TIMEOUT_SECONDS)
    assert session_log_path.exists(), f"`start` 之后场次 log 该已经挂上：{session_log_path}"

    assert post_action(proc.base_url, "stop").status_code == 200
    snapshot = _wait_done(proc)

    session_log = _wait_end_marker(session_dir)
    lines = [line for line in session_log.splitlines() if line.strip()]
    assert lines, "场次 log 不该为空"
    # 严格口径：标记所在行就是最后那个非空行（标记之后不再有任何非空行）
    marker_index = next(
        (index for index, line in enumerate(lines) if END_MARKER_TEXT in line), None
    )
    assert marker_index is not None, f"场次 log 里没有结束标记：\n{session_log}"
    assert marker_index == len(lines) - 1, (
        f"结束标记必须是最后一行：它落在第 {marker_index + 1} 行，"
        f"而最后一个非空行是第 {len(lines)} 行\n{lines[marker_index:]}"
    )

    # **标记逐字段核**（审查问题 M8、§3.7）：场次 id、`state`、收尾概要、服务级 log 路径。
    # 标记是 `log` 步**在它自己那一刻**写下的：那一刻 `state` 还是 `draining`（收尾没走完），
    # 四步概要是 `log=running`（正在写它）、`post=waiting`（还没开跑）。这两件事因此按
    # 「**在 `log` 步那一刻成立的取值**」逐项核 —— 而不是拿收尾走完之后盘上那一版去套，
    # 那样会把「标记**照实**写下了它那一刻的收尾进度」这条事实读成一句对不上。
    record = read_session_json(session_dir)
    assert record is not None, f"盘上读不出 {session_dir}/session.json"
    marker = lines[marker_index]
    # 场次 id：格式串里的 `session=%(session)s`（场次级 handler 的 `SessionFilter` 写的就是它）
    assert f"session={Path(session_dir).name} " in marker, (
        f"标记该带场次 id（= 场次目录名）：{marker!r}"
    )
    # `state`：取值落在 `session.json` 的取值域里
    state = marker.split("场次结束：state=", 1)[1].split("；", 1)[0]
    assert state in SESSION_STATE_DOMAIN, f"标记里的 state 落在取值域之外：{state!r}（{marker!r}）"
    # 收尾概要：**各档按 `DRAIN_STEP_IDS` 的原顺序**逐项报出（格式串把 `status` 列表按顺序
    # 用 `, ` 连起来），每一项的 `status` 落在 `DRAIN_STEP_STATUS` 的取值域里
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
    # 服务级 log 路径：与本次启动的服务级日志路径逐字相同
    assert marker.rstrip().endswith(f"服务级 log：{snapshot['service']['log_path']}"), (
        f"标记末尾该是服务级 log 路径（{snapshot['service']['log_path']!r}）：{marker!r}"
    )

    size_after = session_log_path.stat().st_size
    time.sleep(DRAIN_LOG_CLOSED_RECHECK_SECONDS)  # 摘掉之后这一场不再有新行
    assert session_log_path.stat().st_size == size_after


# ---------------------------------------------------------------------------
# ⑫ 结束标记前逐类事件可查（G6 的自动化对应物）
# ---------------------------------------------------------------------------


def test_all_log_event_categories_findable_before_end_marker(service_process, meeting_env):
    """结束标记**之前**这一场的事件逐类可查（方案 §6.3 行 304 的清单）。

    事件由 dev 采集替身与采集后端的收尾调用造出来：**每一路健康迁移**与告警起止由
    `MEETING_CAPTURE=dev` 的两路合成电平与 `dev_inject` 在**真服务子进程**里驱动，
    收尾每一步的开始 / 结束 / 耗时与 `budget` / `backlog` 由 phase3 的编排落。
    阈值只**显式调小**（写进临时 home 的 `config.json`，**不改代码默认值**）。

    **故障档取 `silence`**（审查问题 C1 的证据口径）：它一路把 loopback 从 `capturing`
    推到 `lost`（阈值 `alert.silence_seconds`）、恢复判定再推到 `restored`，于是「每一路健康
    迁移」这一类在一趟里出现**三段真实迁移**，且每段都带着是哪一路、从什么值到什么值、
    原因与阈值。`lost` 那一档同样落这一类事件（`麦克风/回环 → lost`），但它一跳就到、看不出
    `restored` 那一段，因此这里用 `silence` 把两段都留在场次 log 里（结束标记之前）。

    **「场次开始与目录」这一类取服务级 log（审查问题 M3 的落地读法）**：场次 log 是 `start`
    成功之后才挂上的 handler，而那一行按时间顺序落在挂上之前 —— 它进不了场次 log。契约点名的
    这一条因此按**两类 log 联合**读：场次 log 里取本场的 session id / 目录 / 「场次结束」，
    服务级 log 里取「场次开始」那一行（带同一个 id 与目录）。别为了迎合字面去场次 log 里
    伪造一行。

    **「挂载顺序」这件事与 phase2 的核对结论（审查问题 m10，照实写清）**：`start` 里那一行
    `场次开始：<id>（目录 <dir>，mode=…）` 落在 `logging_setup.attach_session_log(session_dir)`
    **之前**，因此它只可能出现在服务级 log 里。这个顺序是 **phase2 的既成事实**：拿 HEAD 的
    上一个提交核过 —— `git show 56fcf33:meeting/service/runtime.py` 的 `start_session()` 里
    `_log.info("场次开始：…")` 同样排在 `attach_session_log(session_dir)` 之前；
    `git diff --ignore-cr-at-eol 56fcf33 -- meeting/service/runtime.py` 在这个位置只多了一行
    「每一路健康迁移」的登记（phase3 补的），**挂载顺序一处没动**（phase3 §1.3 也没把
    「重排 start 的挂载顺序」列进本 Phase 的动作）。本 Phase 因此照现行的两类 log 联合断言写，
    没有把这一行搬进场次 log。
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
    # **「场次开始与目录」这一类事件按时间顺序只可能落在服务级 log**（审查问题 M3 的口径，
    # 写在这里是为了不让人对着契约那半句去找一条不存在的场次 log 行）：场次 log 是 `start`
    # 成功之后才挂上的 handler，而「场次开始：<id>（目录 <dir>，mode=…）」这一行落在挂上之前
    # —— 场次 log 里能找到的是**同一场的**「场次结束：<id>（目录 …）」与 session id / 目录本身。
    # 两类 log 因此**联合断言**：本场身份（id 与目录）两边各取一半，合起来覆盖契约点名的这一条。
    #
    # **这个顺序的出处（审查问题 m10）**：它是 **phase2 的挂载顺序**，
    # `git show 56fcf33:meeting/service/runtime.py` 的 `start_session()` 里
    # `_log.info("场次开始：…")` 就已排在 `attach_session_log(session_dir)` 之前；
    # `git diff --ignore-cr-at-eol 56fcf33 -- meeting/service/runtime.py` 在这个位置只多了一行
    # 「每一路健康迁移」的登记，**挂载顺序一处没改**（phase3 未动）。下面这条联合断言因此
    # 照现行顺序写，没有把这一行搬进场次 log（搬了反而会与 phase2 的既成事实对不上）。
    check("场次结束：", "场次结束与目录（本场那一份里的收尾痕迹）")
    check(session_id, "场次 id")
    check(session_dir, "场次目录路径")
    check("告警起：", "告警起")
    check("状态迁移：alert -> recording", "告警止（恢复）")
    check("alarm.sound", "出声开关（抑制口径）")
    # **每一路健康迁移**（契约点名的必落 log 事件；审查问题 C1 补上的那一类）。
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

    # 服务级 log 那半边：**「场次开始与目录」落在这里**（理由见上面 session_id 那一段的注释
    # —— 场次 log 是 `start` 成功之后才挂上的，这一行按时间顺序在它之前），加上两路设备探测
    # 与各步齐备的收尾记录。字段级断言：这一行要带**本场的 id 与目录**，与场次 log 里那一半对上。
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
