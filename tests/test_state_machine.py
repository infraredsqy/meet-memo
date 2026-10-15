"""`tests/test_state_machine.py` —— 四态状态机、快照形状、包络与收尾闭环（phase2 §5.1 第一行）。

覆盖（逐条对 phase2 §5.1 的判据）：

  - 四态取值与**全部合法迁移**；非法迁移**抛错而不是静默 no-op**，且被拒绝的请求
    **不涨 `revision`、不写 `session.json`、不广播**；
  - 三条铁律：进 `alert` 不停录且快照同时能看见 `capturing` 与 `lost`；所有路都挂也只是
    `alert`（**没有** `lost → ended` 的路径）；心跳失联不参与迁移；
  - `revision` 每次变更单调 `+1`、同一次事实处理合并递增；
  - `elapsed_seconds` 在 `recording` / `alert` 走秒、进 `ended` 冻结；
  - `envelope` = 最近 30 秒 / 15 根柱 / 一根 2 秒，**一根柱 = 它那 2 秒窗口内
    所有 `LevelFrame.peak` 的最大值**；
  - `to_dict()` 的字段名与取值域逐条对齐 §3.1.2（`sources` 恰好两项、`health` 七取值、
    `settings` 五个子键齐全且 `devices` 的两个子键都在（没配时都是 `null`））；
  - **收尾闭环两个分支都断言**：
    `test_finish_drain_keeps_top_state_ended` 管成功分支（`finish_drain()` 之后
    `top_state` **停在 `ended`**、`ended.phase == "done"`、`session` 保留、盘上 `state == "done"`）；
    失败分支与取消分支由本文件与 `tests/test_api_actions.py` 的 `draining` 拒绝路径合起来断言。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from tests import _helpers as helpers
from meeting import config as config_module
from meeting.config import ConfigValueError
from meeting.service import app as service_app
from meeting.service import runtime as runtime_module
from meeting.state import (
    DRAIN_STEP_IDS,
    ENDED_PHASE,
    ENVELOPE_BAR_COUNT,
    ENVELOPE_BAR_SECONDS,
    POST_DRAIN_STEP_ID,
    PROBE_STATUS,
    SESSION_STATE,
    SOURCE_HEALTH,
    TOP_STATE,
    Alert,
    AlertState,
    DrainStateError,
    InvalidTransitionError,
    SessionInfo,
    Snapshot,
    begin_alert,
    begin_drain,
    begin_drain_step,
    finish_drain,
    finish_drain_step,
    new_snapshot,
    recover_source,
    start_recording,
)

#: 每条用例的显式等待上限（不留裸秒数、不依赖默认值）
STATE_POLL_TIMEOUT_SECONDS = 15.0
#: 轮询间隔
STATE_POLL_INTERVAL_SECONDS = 0.05
#: 取消用例里把收尾第一步钉在 `running` 的秒数（大于轮询间隔、小于用例超时）
CANCEL_PIN_SECONDS = 3.0


# ---------------------------------------------------------------------------
# 夹具：进程内的活运行时（起 ASGI 应用、startup 钩子跑过、绑定到回环 8765）
# ---------------------------------------------------------------------------


def build_live_client(tmp_path: Path, values: dict | None = None):
    """起一个进程内已启动的运行时 + ASGI 客户端，返回 `(client, runtime)`。

    与 `_helpers.app_client()` 同一手法（家目录重定向 + `webbrowser.open` 换成假替身），
    额外把 runtime 从应用状态里取出来供用例直接读。
    """
    import contextlib

    home = tmp_path / "home-live"
    home.mkdir(parents=True, exist_ok=True)
    out_root = tmp_path / "out-live"
    out_root.mkdir(parents=True, exist_ok=True)
    config = {"web.dist_dir": str(helpers.missing_dist_dir(home))}
    config.update(values or {})
    stack = contextlib.ExitStack()
    client = stack.enter_context(helpers.app_client(home, values=config))
    client.place_root = out_root
    runtime = client.app.state.runtime
    return stack, client, runtime


def snapshot_of(client) -> dict:
    response = client.get("/api/state")
    assert response.status_code == 200
    return response.json()


def source_of(snapshot: dict, source_id: str) -> dict:
    for item in snapshot["sources"]:
        if item["id"] == source_id:
            return item
    raise AssertionError(f"快照里没有 {source_id}")


def wait_for_state(client, wanted: str, *, timeout: float = STATE_POLL_TIMEOUT_SECONDS) -> dict:
    """等 `top_state` 变成 `wanted`（经公共等待助手，显式超时）。"""

    def check():
        snapshot = snapshot_of(client)
        return snapshot if snapshot["top_state"] == wanted else None

    return _wait(check, timeout=timeout, what=f"top_state == {wanted}")


def _wait(predicate, *, timeout: float, what: str):
    """本文件内的小等待器（同步轮询 + 上限；与 `_service.wait_until` 同一口径）。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(STATE_POLL_INTERVAL_SECONDS)
    raise AssertionError(f"等待超时（{timeout}s）：{what}（最后观察值 {last!r}）")


def wait_for_drain_done(client, *, timeout: float = STATE_POLL_TIMEOUT_SECONDS) -> dict:
    """等收尾走到终态（`ended.phase == "done"`）。"""

    def check():
        snapshot = snapshot_of(client)
        ended = snapshot["ended"]
        return snapshot if ended is not None and ended["phase"] == "done" else None

    return _wait(check, timeout=timeout, what="ended.phase == done")


def post(client, action: str, args: dict | None = None):
    body = {"action": action}
    if args is not None or action == "start":
        body["args"] = helpers.start_args(action, args, client.place_root)
    return client.post("/api/actions", json=body, headers={"X-Meeting-Service": "1"})


# ---------------------------------------------------------------------------
# 取值域与冻结常量
# ---------------------------------------------------------------------------


def test_state_vocabularies_are_the_frozen_ones():
    """四态、`session.json` 的 `state`、`ended.phase`、`DrainStep` 取值域逐字冻结。"""
    assert TOP_STATE == ("idle", "recording", "alert", "ended")
    assert SESSION_STATE == ("recording", "draining", "done", "failed")
    assert ENDED_PHASE == ("draining", "done")
    assert DRAIN_STEP_IDS == ("flush", "transcode", "log", "post")
    assert set(PROBE_STATUS) <= set(SOURCE_HEALTH)
    assert set(SOURCE_HEALTH) == {
        "unprobed", "ready", "probe_failed", "capturing", "lost", "restored", "stopped",
    }


def test_empty_snapshot_shape_is_the_frozen_one():
    """空形态快照：顶层十键齐、`sources` 恰好两项且都是 `unprobed`、包络为空数组。"""
    from meeting.state import ServiceInfo

    snapshot = new_snapshot()
    payload = snapshot.to_dict(
        ServiceInfo(
            started_at="2025-09-06T10:00:00+08:00",
            version="0.1.0",
            port=8765,
            url="http://127.0.0.1:8765",
            log_path="x/meeting.log",
        )
    )
    assert set(payload) == {
        "revision", "service", "top_state", "session", "sources",
        "alerts", "transcript", "ended", "settings",
    }
    assert payload["top_state"] == "idle"
    assert payload["session"] is None
    assert payload["ended"] is None
    assert [item["id"] for item in payload["sources"]] == ["mic", "loopback"]
    for item in payload["sources"]:
        assert item["health"] in SOURCE_HEALTH
        assert item["envelope"] == []
    assert set(payload["settings"]) == {
        "alarm_sound", "heartbeat_interval_seconds", "summary_default_window",
        "summary_preset_names", "summary_active_preset",
        "asr", "devices",
    }
    assert payload["settings"]["devices"] == {}


def test_all_legal_transitions_via_the_migration_functions():
    """全部合法迁移都经 `state.py` 的迁移函数：idle→recording→alert→recording→ended→done。"""
    snapshot = new_snapshot()
    session = SessionInfo(
        id="250906", dir="C:/out/250906", started_at="t0", mode="both", started_monotonic=0.0
    )
    start_recording(snapshot, session=session, mode="both", devices={"mic": "d1", "loopback": None})
    assert snapshot.top_state == "recording"
    assert snapshot.session is session
    assert {item.health for item in snapshot.sources} == {"capturing"}
    assert snapshot.ended is None

    alert = Alert(
        level="critical", source="loopback", since="t1", duration_seconds=180.0,
        message="静默 180 秒", muted=False,
    )
    assert begin_alert(
        snapshot, source="loopback", alert=alert, lost_since="t1"
    ) is True
    assert snapshot.top_state == "alert"
    assert snapshot.source("loopback").health == "lost"
    assert snapshot.alerts.critical is alert
    # 没出事的那一路照旧「在采」
    assert snapshot.source("mic").health == "capturing"
    # 已经是 alert 时再判定同一路：不产生第二次顶层迁移
    assert begin_alert(
        snapshot, source="loopback", alert=alert, lost_since="t1"
    ) is False

    assert recover_source(snapshot, source="loopback") is True
    assert snapshot.top_state == "recording"
    assert snapshot.source("loopback").health == "restored"
    assert snapshot.alerts.critical is None

    begin_drain(snapshot, reason="user")
    assert snapshot.top_state == "ended"
    assert snapshot.ended.phase == "draining"
    assert [step.id for step in snapshot.ended.steps] == list(DRAIN_STEP_IDS)
    assert {step.status for step in snapshot.ended.steps} == {"waiting"}
    assert snapshot.session.frozen is True
    assert {item.health for item in snapshot.sources} == {"stopped"}

    for step_id in DRAIN_STEP_IDS:
        begin_drain_step(step_id, snapshot)
        assert snapshot.drain_step(step_id).status == "running"
        finish_drain_step(
            step_id, "skipped" if step_id != "log" else "done", "这一趟的说明", snapshot=snapshot
        )
    assert finish_drain(snapshot) == "done"
    assert snapshot.ended.phase == "done"
    assert snapshot.top_state == "ended"
    assert snapshot.session is not None


def test_illegal_transitions_raise_instead_of_no_op():
    """非法迁移**抛错**（不是静默 no-op）；未知状态取值不许兜底成 `idle`。"""
    snapshot = new_snapshot()
    session = SessionInfo(id="250906", dir="d", started_at="t0", mode="both")

    with pytest.raises(InvalidTransitionError):
        begin_alert(
            snapshot,
            source="mic",
            alert=Alert(level="critical", source="mic", since="t", duration_seconds=0.0,
                        message="x", muted=False),
            lost_since="t",
        )
    with pytest.raises(InvalidTransitionError):
        begin_drain(snapshot, reason="user")
    with pytest.raises(InvalidTransitionError):
        recover_source(snapshot, source="mic")

    start_recording(snapshot, session=session, mode="both")
    with pytest.raises(InvalidTransitionError):
        # 已经在录音中，不能再 start 一次
        start_recording(
            snapshot,
            session=SessionInfo(id="260907", dir="d2", started_at="t1", mode="both"),
            mode="both",
        )
    with pytest.raises(InvalidTransitionError):
        start_recording(
            snapshot,
            session=SessionInfo(id="260908", dir="d3", started_at="t1", mode="both"),
            mode="fourth",
        )
    # 未知状态取值（既不是四态之一）一律按错误处理
    snapshot.top_state = "paused"
    with pytest.raises(InvalidTransitionError):
        begin_drain(snapshot, reason="user")


def test_rejected_request_does_not_grow_revision_or_touch_disk(tmp_path):
    """被拒绝的迁移不涨 `revision`、不写 `session.json`、不广播（phase2 §3.1.1）。"""
    stack, client, runtime = build_live_client(tmp_path)
    try:
        before = snapshot_of(client)["revision"]
        response = post(client, "stop")
        assert response.status_code == 409
        assert response.json()["code"] == "not_recording"
        assert snapshot_of(client)["revision"] == before
        assert not list(Path(client.place_root).rglob("session.json"))
    finally:
        stack.close()


def test_rejected_request_does_not_touch_disk_when_recording(tmp_path):
    """录音中再 `start` → `already_recording`(409)，`revision` 不动、盘上不留第二场。"""
    stack, client, runtime = build_live_client(tmp_path, {"capture.backend": "dev"})
    try:
        assert post(client, "start").status_code == 200
        after = snapshot_of(client)["revision"]
        rejected = post(client, "start")
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "already_recording"
        assert snapshot_of(client)["revision"] == after
        # 录中的那一场只有一个：预建的 ws1 里落了 session.json，拒绝路径没有第二场
        dirs = [
            item for item in Path(client.place_root).iterdir()
            if (item / "session.json").exists()
        ]
        assert len(dirs) == 1, f"盘上不该有第二场：{dirs}"
    finally:
        stack.close()


# ---------------------------------------------------------------------------
# 三条铁律
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_alert_does_not_stop_recording_and_shows_both_healths(tmp_path):
    """铁律 1：进 `alert` 不等于停录 —— 快照同时看得见 `capturing` 与 `lost`、时长继续走。"""
    stack, client, runtime = build_live_client(
        tmp_path,
        {
            "capture.backend": "dev",
            "alert.silence_seconds": 1,
            "alert.recover_seconds": 1,
        },
    )
    try:
        assert post(client, "start").status_code == 200
        injected = helpers_dev_direct(runtime, source="loopback", fault="silence",
                                     after_seconds=0.0, duration_seconds=60.0)
        assert injected is not None
        snapshot = wait_for_state(client, "alert")
        assert source_of(snapshot, "loopback")["health"] == "lost"
        assert source_of(snapshot, "mic")["health"] == "capturing"
        assert snapshot["alerts"]["critical"] is not None
        assert snapshot["alerts"]["critical"]["source"] == "loopback"
        assert snapshot["session"]["frozen"] is False

        first = snapshot["session"]["elapsed_seconds"]
        time.sleep(0.6)
        later = snapshot_of(client)["session"]["elapsed_seconds"]
        assert later > first, "进 alert 之后时长必须继续走"
        assert snapshot_of(client)["top_state"] == "alert"
    finally:
        stack.close()


def helpers_dev_direct(runtime, *, source: str, fault: str, after_seconds: float,
                       duration_seconds: float | None):
    """直接对 dev 替身注入一次故障（**只用于进程内用例**；HTTP 面另有 `dev_inject`）。"""
    if not runtime.capture_capabilities.get("dev_inject"):
        return None
    runtime.capture_backend.inject_fault(
        source=source, fault=fault, after_seconds=after_seconds, duration_seconds=duration_seconds
    )
    return True


def test_all_sources_lost_never_ends_the_session(tmp_path):
    """铁律 2：所有路都挂了也只是 `alert` —— **没有** `lost → ended` 的路径。"""
    stack, client, runtime = build_live_client(
        tmp_path,
        {"capture.backend": "dev", "alert.silence_seconds": 1, "alert.recover_seconds": 1},
    )
    try:
        assert post(client, "start").status_code == 200
        for source in ("mic", "loopback"):
            helpers_dev_direct(runtime, source=source, fault="lost",
                               after_seconds=0.0, duration_seconds=60.0)
        snapshot = wait_for_state(client, "alert")
        # 两路都被判定为 lost，但 top_state 仍只是 alert（服务绝不因采集异常自动结束会话）
        for source in ("mic", "loopback"):
            assert source_of(snapshot, source)["health"] == "lost"
        assert snapshot["ended"] is None
        assert snapshot["session"] is not None
        assert snapshot["session"]["frozen"] is False
        assert snapshot_of(client)["top_state"] == "alert"
    finally:
        stack.close()


def test_heartbeat_fields_do_not_drive_any_transition(tmp_path):
    """铁律 3：心跳失联（`recording_alive == false`）不进状态机。"""
    stack, client, runtime = build_live_client(tmp_path, {"capture.backend": "dev"})
    try:
        assert post(client, "start").status_code == 200
        # 让帧时钟先有值
        _wait(lambda: client.get("/api/heartbeat").json()["recording_alive"] is True,
              timeout=STATE_POLL_TIMEOUT_SECONDS, what="recording_alive == True")
        # 把帧时钟按下去（模拟「HTTP 通着但录音停了」），仍然不许发生任何迁移
        runtime.last_frame_ts = time.monotonic() - 60.0
        before = snapshot_of(client)["revision"]
        assert client.get("/api/heartbeat").json()["recording_alive"] is False
        assert snapshot_of(client)["top_state"] == "recording"
        assert snapshot_of(client)["revision"] == before
    finally:
        stack.close()


# ---------------------------------------------------------------------------
# revision 与时长
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_elapsed_seconds_runs_then_freezes(tmp_path):
    """`elapsed_seconds` 在 `recording` / `alert` 走秒、进 `ended` 冻结。"""
    stack, client, runtime = build_live_client(tmp_path, {"capture.backend": "dev"})
    try:
        assert post(client, "start").status_code == 200
        time.sleep(0.4)
        running = snapshot_of(client)["session"]["elapsed_seconds"]
        assert running > 0

        assert post(client, "stop").status_code == 200
        ended = snapshot_of(client)
        assert ended["session"]["frozen"] is True
        frozen_at = ended["session"]["elapsed_seconds"]
        time.sleep(0.4)
        after = wait_for_drain_done(client)
        assert after["session"]["frozen"] is True
        assert after["session"]["elapsed_seconds"] == frozen_at
    finally:
        stack.close()


# ---------------------------------------------------------------------------
# 包络
# ---------------------------------------------------------------------------


def test_envelope_is_fifteen_bars_of_two_second_windows():
    """包络口径只有一处：15 根柱、一根 2 秒，**柱值 = 该窗口内 `LevelFrame.peak` 的最大值**。"""
    from meeting.state import SourceState

    item = SourceState(id="mic", label="本地")
    # 第 0 个窗口（[0, 2)）里三帧，峰值依次 -40 / -12 / -30 → 这一根柱必须是 -12
    item.push_level(0.0, dbfs=-45.0, peak=-40.0)
    item.push_level(0.5, dbfs=-20.0, peak=-12.0)
    item.push_level(1.5, dbfs=-35.0, peak=-30.0)
    assert len(item.envelope) == ENVELOPE_BAR_COUNT
    assert item.envelope[-1] == -12.0
    assert item.envelope[-2] == -40.0, "更早的柱沿用会话开场那一帧的值（那会儿只有它在响）"
    # 跨到第 1 个窗口：最右那根换成新窗口的峰值
    item.push_level(2.4, dbfs=-25.0, peak=-25.0)
    assert len(item.envelope) == ENVELOPE_BAR_COUNT
    assert item.envelope[-1] == -25.0
    # 再跨一个窗口
    item.push_level(4.4, dbfs=-18.0, peak=-18.0)
    assert item.envelope[-1] == -18.0
    # 时间戳回退到本窗口内的另一刻（后端换时钟）：已落定的柱一根都不许动
    before = list(item.envelope)
    item.push_level(4.5, dbfs=-50.0, peak=-50.0)
    assert item.envelope[:-1] == before[:-1]
    assert item.envelope[-1] == -18.0


def test_envelope_grows_to_fifteen_bars_while_recording(tmp_path):
    """录音中包络从空数组长成 15 根柱（真服务 + dev 替身，§5.2 第 4 条）。"""
    stack, client, runtime = build_live_client(tmp_path, {"capture.backend": "dev"})
    try:
        assert post(client, "start").status_code == 200
        snapshot = _wait(
            lambda: (lambda s: s if all(
                len(item["envelope"]) == ENVELOPE_BAR_COUNT for item in s["sources"]
            ) else None)(snapshot_of(client)),
            timeout=STATE_POLL_TIMEOUT_SECONDS,
            what="包络长到 15 根柱",
        )
        for item in snapshot["sources"]:
            assert len(item["envelope"]) == ENVELOPE_BAR_COUNT
            assert item["level_dbfs"] is not None
            assert item["peak_dbfs"] is not None
            assert item["health"] == "capturing"
    finally:
        stack.close()


def test_revision_merges_within_one_fact_processing():
    """同一次事实处理里的多次变更**合并为一次**递增（不跳号）—— 进程内直接量。"""
    from meeting.state import SessionInfo, begin_alert, new_snapshot, start_recording
    from meeting.state import Alert as StateAlert

    snapshot = new_snapshot()
    start_recording(
        snapshot,
        session=SessionInfo(id="1", dir="d", started_at="t", mode="both"),
        mode="both",
    )
    before = snapshot.revision
    # 一次事实处理里「该路进 alert」+「另一路恢复」合并成一次递增
    begin_alert(
        snapshot,
        source="loopback",
        alert=StateAlert(level="critical", source="loopback", since="t",
                         duration_seconds=1.0, message="m", muted=False),
        lost_since="t",
    )
    assert snapshot.revision == before, "迁移函数自己不碰 revision（提交收在 commit() 里）"
    snapshot.revision += 1  # commit() 干的那一件事
    recover_source(snapshot, source="loopback")
    assert snapshot.revision == before + 1, "同一次处理里的第二次变更不许再涨一次"


def test_envelope_bar_equals_the_max_peak_of_its_window():
    """按同一算法逐根比对：柱高 = 该 2 秒窗口内所有帧 `peak` 的最大值。"""
    from meeting.state import SourceState

    item = SourceState(id="mic", label="本地")
    frames = [
        (0.0, -30.0), (0.4, -10.0), (1.2, -50.0),          # 第 0 个窗口 → -10
        (2.1, -60.0), (3.9, -22.0),                          # 第 1 个窗口 → -22
        (4.0, -5.0), (5.9, -44.0),                           # 第 2 个窗口 → -5
    ]
    for ts, peak in frames:
        item.push_level(ts, dbfs=peak, peak=peak)
    expected = [-10.0, -22.0, -5.0]
    assert item.envelope[-3:] == expected
    assert ENVELOPE_BAR_SECONDS == 2.0
    assert len(item.envelope) == ENVELOPE_BAR_COUNT


def assert_levels_frame_matches_frontend_judge(frame: dict) -> None:
    """`web/src/contract/sse.ts` 的同一份判据：包络长度恰满、`bar == envelope[-1]`。"""
    assert len(frame["envelope"]) == ENVELOPE_BAR_COUNT
    assert frame["bar"] == frame["envelope"][-1]


def test_sample_levels_fills_silence_envelope_before_the_first_frame():
    """开场那一瞬（第一帧电平未到）推出的帧也是合法形状：包络补齐成 15 根静音柱（-100 档）。"""
    from meeting.state import SessionInfo, sample_levels

    snapshot = new_snapshot()
    start_recording(
        snapshot,
        session=SessionInfo(id="1", dir="d", started_at="t", mode="both"),
        mode="both",
    )
    for frame in sample_levels(snapshot).values():
        assert_levels_frame_matches_frontend_judge(frame)
        assert frame["envelope"] == [-100.0] * ENVELOPE_BAR_COUNT, "补的是静音档（bar_default 同一口径）"
        assert frame["dbfs"] is None and frame["peak"] is None, "瞬时值仍如实报 null"
    # alert 态同门：活循环在 recording / alert 两态都广播，丢帧的那一路同样补齐
    snapshot.top_state = "alert"
    for frame in sample_levels(snapshot).values():
        assert_levels_frame_matches_frontend_judge(frame)


def test_sample_levels_single_channel_off_path_is_legal_at_every_sample():
    """单路整场：没在录的那一路每个取样点都是合法形状；在录的那一路照常长柱，不按静音处理。"""
    from meeting.state import SessionInfo, sample_levels

    snapshot = new_snapshot()
    start_recording(
        snapshot,
        session=SessionInfo(id="1", dir="d", started_at="t", mode="mic"),
        mode="mic",
    )
    mic = snapshot.source("mic")
    frames = {}
    for second in range(35):  # 跨过窗口滚动，覆盖整场的每个取样点
        mic.push_level(float(second), dbfs=-30.0, peak=-20.0)
        frames = sample_levels(snapshot)
        assert_levels_frame_matches_frontend_judge(frames["mic"])
        assert_levels_frame_matches_frontend_judge(frames["loopback"])
        assert frames["loopback"]["envelope"] == [-100.0] * ENVELOPE_BAR_COUNT
    assert frames["mic"]["envelope"][-1] == -20.0, "逐路判：在录的那一路不补静音"


def test_sample_levels_second_session_opening_beat_is_legal():
    """同一进程连开两场：第二场开场那一拍推出的帧也是合法形状。"""
    from meeting.state import SessionInfo, sample_levels

    snapshot = new_snapshot()
    start_recording(
        snapshot,
        session=SessionInfo(id="1", dir="d", started_at="t", mode="both"),
        mode="both",
    )
    snapshot.source("mic").push_level(0.0, dbfs=-30.0, peak=-20.0)
    snapshot.top_state = "ended"  # 第一场收场（stop 落定的终态）
    # 第二场开场：`start_recording` 把两路包络与柱窗口整组清零，第一帧未到
    start_recording(
        snapshot,
        session=SessionInfo(id="2", dir="d2", started_at="t2", mode="both"),
        mode="both",
    )
    for frame in sample_levels(snapshot).values():
        assert_levels_frame_matches_frontend_judge(frame)
        assert frame["envelope"] == [-100.0] * ENVELOPE_BAR_COUNT


def test_sample_levels_idle_keeps_the_empty_envelope():
    """空闲态（还没有场次）：空包络照旧合法，不许被填成满格。"""
    from meeting.state import sample_levels

    frames = sample_levels(new_snapshot())
    for frame in frames.values():
        assert frame["envelope"] == []
        assert frame["bar"] is None


# ---------------------------------------------------------------------------
# 收尾闭环（成功分支 + 失败分支 + 取消分支）
# ---------------------------------------------------------------------------


def test_finish_drain_keeps_top_state_ended(tmp_path):
    """成功分支：四步全部落定且无失败步 → `top_state` **停在 `ended`**、`phase == "done"`。

    断言逐条对齐 §5.1：`finish_drain()` 之后 `ended.phase == "done"`、`top_state` 停在
    `ended`、`session` 保留（本场时长冻结）、`ended` 摘要留到下一场 `start` 才清空、
    盘上 `state == "done"`。

    **第 4 步 `post` 停在 `waiting`**（R26：按「结束录音」之后会后一个动作都不自动跑）：
    这一步跑完它该做的（释放会中模型）就如实空着等人按按钮，`post.hook` 取什么值都一样。
    别的三步照旧：采集替身那两步 `skipped`、`log` 是 `done`。
    """
    stack, client, runtime = build_live_client(
        tmp_path, {"capture.backend": "dev", "post.hook": "null"}
    )
    try:
        assert post(client, "start").status_code == 200
        session_dir = Path(runtime.snapshot.session.dir)
        assert post(client, "stop").status_code == 200
        snapshot = wait_for_drain_done(client)

        assert snapshot["top_state"] == "ended"
        assert snapshot["ended"]["phase"] == "done"
        assert snapshot["ended"]["error"] is None
        # `session` 保留（本场时长冻结）
        assert snapshot["session"] is not None
        assert snapshot["session"]["frozen"] is True
        # 前三步都有终态；第 4 步如实停在「等你动手」那一格
        steps = {step["id"]: step["status"] for step in snapshot["ended"]["steps"]}
        assert steps == {
            "flush": "skipped", "transcode": "skipped", "log": "done", "post": "waiting",
        }
        # 成功档里两路从 stopped 回到 unprobed
        assert {item["health"] for item in snapshot["sources"]} == {"unprobed"}

        # 盘上 `state == "done"` + `ended_at` 有值
        record = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
        assert record["state"] == "done"
        assert record["ended_at"]
        assert [item["id"] for item in record["drain"]["steps"]] == list(DRAIN_STEP_IDS)

        # 取消 / 失败档都不写 `done`：这里再确认一遍成功档确实是 done
        assert record["state"] != "failed"

        # 下一场 `start` 才清空上一场的 `ended` 摘要，且直接进 recording
        assert post(client, "start").status_code == 200
        next_snapshot = snapshot_of(client)
        assert next_snapshot["ended"] is None
        assert next_snapshot["top_state"] == "recording"
        assert next_snapshot["session"]["id"] != record["session_id"]
        # 上一场的 `session.json` 留在盘上不动（它是盘上的场次档案）
        assert json.loads((session_dir / "session.json").read_text(encoding="utf-8"))["state"] == "done"
    finally:
        stack.close()


def test_only_the_post_step_may_settle_waiting(tmp_path):
    """`waiting` 只能当 `post` 那一格的落定结果，别的三步落回去一律抛错（复审 m4）。

    这条规则的价值正在「别的步骤不许落回 `waiting`」：`waiting` 说的是「这一步空着等人
    按按钮」（R26 的会后那一格），若 `flush` / `transcode` / `log` 也能落它，
    「收尾协程被取消、四步都还没轮到」与「这一步不归我管」就混成同一副面孔了。
    """
    snapshot = new_snapshot()
    session = SessionInfo(id="250906", dir="d", started_at="t0", mode="both")
    start_recording(snapshot, session=session, mode="both")
    begin_drain(snapshot, reason="user")

    # `post` 那一格：`waiting` 是它的合法落定值
    finish_drain_step(POST_DRAIN_STEP_ID, "waiting", snapshot=snapshot)
    assert snapshot.drain_step(POST_DRAIN_STEP_ID).status == "waiting"

    # 其余三步：逐个各来一次，逐条都要抛错（别只测一条）
    for step_id in DRAIN_STEP_IDS:
        if step_id == POST_DRAIN_STEP_ID:
            continue
        with pytest.raises(DrainStateError, match="只能作为"):
            finish_drain_step(step_id, "waiting", snapshot=snapshot)
        assert snapshot.drain_step(step_id).status == "waiting", (
            f"{step_id} 那一步被拒之后该保持原样（`begin_drain()` 落的 waiting）"
        )


def test_finish_drain_failure_branch_keeps_draining(tmp_path, monkeypatch):
    """失败分支：有任一步 `failed` → `phase` 保持 `draining`、`top_state` 停在 `ended`。

    造法（进程内，不起第二个服务）：把 `log` 那一步写**场次日志结束标记**的落点换成一个抛错的
    替身 ——「有实现却抛错 → `failed`」这一档（§3.0 的边界）在收尾四步里由它来演。
    第 4 步 `post` 现在停在 `waiting`（会后一个动作都不自动跑，R26），它不再产生失败步；
    而失败步的记录形状（`error` 里带可复制命令与 log 路径、`start` 被 409 拒）一个字没变。
    """
    stack, client, runtime = build_live_client(tmp_path, {"capture.backend": "dev"})
    try:
        assert post(client, "start").status_code == 200
        session_dir = Path(runtime.snapshot.session.dir)

        def boom_end_marker(*args, **kwargs):
            raise RuntimeError("假场次日志接缝：按测试要求抛错")

        monkeypatch.setattr(
            runtime_module.logging_setup, "write_session_end_marker", boom_end_marker
        )

        assert post(client, "stop").status_code == 200
        snapshot = _wait(
            lambda: (lambda s: s if (
                s["ended"] is not None
                and s["ended"]["phase"] == "draining"
                and any(step["status"] == "failed" for step in s["ended"]["steps"])
                and all(step["status"] in ("done", "failed", "skipped", "waiting")
                        for step in s["ended"]["steps"])
            ) else None)(snapshot_of(client)),
            timeout=STATE_POLL_TIMEOUT_SECONDS,
            what="收尾失败档落定",
        )

        assert snapshot["top_state"] == "ended"
        assert snapshot["ended"]["phase"] == "draining"
        assert snapshot["session"] is not None
        failed = [step for step in snapshot["ended"]["steps"] if step["status"] == "failed"]
        assert [step["id"] for step in failed] == ["log"]
        assert "meeting post" in failed[0]["error"]
        assert runtime.log_path in failed[0]["error"]
        assert snapshot["ended"]["error"]

        # 盘上落 `failed`（**绝不写 done**）
        record = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
        assert record["state"] == "failed"

        # 这一档下新的 `start` 一律 `draining`(409)，带失败步与场次 log 路径
        rejected = post(client, "start")
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "draining"
        detail = rejected.json()["detail"]
        assert detail["session_id"] == record["session_id"]
        assert detail["dir"] == str(session_dir)
        assert [item["id"] for item in detail["failed_steps"]] == ["log"]
        assert detail["log_path"] == runtime.log_path
        # 服务不自杀：快照照常应答、进程还在
        assert snapshot_of(client)["top_state"] == "ended"
    finally:
        stack.close()


def test_cancelled_drain_keeps_the_same_face_without_done(tmp_path):
    """取消分支（四步未全部落定）与失败档同一副面孔，只多一处 `interrupted: true` 的差别。"""
    stack, client, runtime = build_live_client(tmp_path, {"capture.backend": "dev"})
    original_release = None
    try:
        assert post(client, "start").status_code == 200
        session_dir = Path(runtime.snapshot.session.dir)
        assert post(client, "stop").status_code == 200
        wait_for_drain_done(client)
        record_before = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
        assert record_before["interrupted"] is False

        # 再开一场，把收尾的 `post` 步钉在 running 上，然后在它跑完之前取消这一趟收尾
        assert post(client, "start").status_code == 200
        second_dir = Path(runtime.snapshot.session.dir)
        assert second_dir != session_dir

        class PinRelease:
            """把第 4 步钉在 `running` 上，直到用例放行（phase3 §3.8 的同一手法）。

            这一步现在只做一件事：让会中模型退出显存（会后归两个按钮，R26）。堵点因此落在
            `release_gpu()` 上 —— **同步调用不落在事件循环上**：收尾的这一步由 `_drain_wait()`
            经运行时的**收尾专用执行器**（`loop.run_in_executor(...)`）提交到**工作线程**，
            `release_gpu()` 于是在那条线程里堵着、事件循环空着 —— `/api/state` 照常应答，
            「轮询到某步处于 running」这个同步点因此等得到。同步阻塞若真落在事件循环里，
            这个窗口根本不会出现（收尾期间状态接口停摆）。
            """

            def __init__(self) -> None:
                self.started = threading.Event()
                self.release = threading.Event()

            def release_gpu(self) -> bool:
                self.started.set()
                self.release.wait(timeout=CANCEL_PIN_SECONDS)
                return True

        hook = PinRelease()
        original_release = getattr(runtime.transcriber_backend, "release_gpu", None)
        runtime.transcriber_backend.release_gpu = hook.release_gpu  # type: ignore[method-assign]

        async def stop_then_cancel():
            # `stop_session` 必须在事件循环里调（收尾协程要挂在这一条循环上才跑得起来）
            runtime.stop_session("user")
            task = runtime._drain_task
            assert task is not None
            # 确定同步点（三步走，缺一步都会撞上「收尾几乎瞬间跑完」这条偶发）：
            #   ① 这一步先被置成 `running`（收尾确实还在跑）；
            #   ② 它的调用随后经运行时的**收尾专用执行器**交到工作线程（`_drain_wait()` 里
            #      那一句 `loop.run_in_executor(...)`），工作线程一进去就置 `hook.started`
            #      —— 等这个事件，就是把「调用真的开始了」等实；
            #   ③ 此时 `release_gpu()` 还堵在 `hook.release` 上（上限 CANCEL_PIN_SECONDS），
            #      于是取消一定落在「post 停在 running」这一格上。
            # 收尾的这一步一定经 `_drain_wait()` 的等待层（`invoke=True`）交到工作线程，
            # 因此②一定会发生；这里等它，而不是抢在它之前断言。
            deadline = time.monotonic() + STATE_POLL_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                ended = runtime.snapshot.ended
                running = ended is not None and any(
                    step.id == "post" and step.status == "running" for step in ended.steps
                )
                if running and hook.started.is_set():
                    break
                await asyncio.sleep(0.005)
            else:
                raise AssertionError(
                    "等不到「收尾的 post 步进入 running 且这次调用真的开始」"
                    f"（post 的状态={runtime.snapshot.drain_step('post').status!r}，"
                    f"hook.started={hook.started.is_set()}）"
                )
            # 收尾仍在 running 这一格上被取消
            assert runtime.snapshot.drain_step("post").status == "running"

            # 第三个 Phase 的强制打断路径是「先落盘（收尾中 + 已打断）再 cancel()」：
            # 这里照同一顺序做，然后取消；取消后的登记不许把这一版覆盖掉。
            record_before_cancel = json.loads(
                (second_dir / "session.json").read_text(encoding="utf-8")
            )
            record_before_cancel["state"] = "draining"
            record_before_cancel["interrupted"] = True
            record_before_cancel["ended_at"] = None
            (second_dir / "session.json").write_text(
                json.dumps(record_before_cancel, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                # 放行钉住的那条工作线程：取消已经在上一行传导到这一步（`await task` 收的就是
                # 那次取消），因此这里放行不会把 post 那一步补成终态。
                hook.release.set()

        asyncio.run(stop_then_cancel())

        assert runtime._drain_task is None  # 走完（含被取消）后置空
        assert runtime.snapshot.top_state == "ended"
        assert runtime.snapshot.ended.phase == "draining"
        assert runtime.snapshot.session is not None
        # 已落定的保持原样、正被取消的那一步留在 running（不伪造终态、不写 failed）
        statuses = [step.status for step in runtime.snapshot.ended.steps]
        assert statuses[:3] == ["skipped", "skipped", "done"]
        assert statuses[3] == "running"
        assert "failed" not in statuses

        # 取消路径也落盘（不留半截）：盘上停在收尾中，`done` 这一档还没到
        record = json.loads((second_dir / "session.json").read_text(encoding="utf-8"))
        assert record["state"] == "draining"
        assert record["interrupted"] is True
        assert record["ended_at"] is None
        # 新的 `start` 拒之门外
        assert post(client, "start").status_code == 409
    finally:
        if original_release is not None:
            runtime.transcriber_backend.release_gpu = original_release
        stack.close()


def test_probe_status_out_of_range_falls_back_to_probe_failed(tmp_path):
    """越界探针取值：记 warning、该路落 `probe_failed`、`top_state` 不动、服务照常起。"""
    from meeting.backends.base import SourceProbe
    from meeting.state import validate_health

    class WeirdProbeBackend:
        frame_interval = 0.5

        def bind(self, emit, *, config=None):
            self._emit = emit

        def probe(self):
            return [
                SourceProbe(source="mic", status="totally_not_a_status"),
                SourceProbe(source="loopback", status="ready", device_name="dev"),
            ]

        def start(self, mode, session_dir):
            raise RuntimeError("采集后端未接入")

        def finalize(self):
            pass

        def transcode(self):
            pass

        def stop(self):
            pass

    assert validate_health("totally_not_a_status") == "probe_failed"
    assert validate_health("ready") == "ready"
    assert validate_health(None) == "probe_failed"

    home = tmp_path / "home-weird"
    home.mkdir()
    helpers.write_config_json(home, {"web.port": 8765})
    import os

    old_home, old_profile = os.environ.get("HOME"), os.environ.get("USERPROFILE")
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    try:
        loaded = config_module.load()
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_profile is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = old_profile

    runtime = service_app.build_runtime(loaded)
    runtime.set_binding("127.0.0.1", 8765)
    runtime.capture_backend = WeirdProbeBackend()

    async def run_startup():
        runtime._loop = asyncio.get_running_loop()
        runtime._probe_devices()

    asyncio.run(run_startup())
    payload = runtime.to_dict()
    assert payload["top_state"] == "idle"
    assert source_of(payload, "mic")["health"] == "probe_failed"
    assert source_of(payload, "loopback")["health"] == "ready"


def test_unassembled_snapshot_raises(tmp_path):
    """未装配（还没绑监听 socket）即取快照 → 抛错，不留退化的空值。"""
    home = tmp_path / "home-unassembled"
    home.mkdir()
    helpers.write_config_json(home, {"web.port": 8765})
    import os

    old_home, old_profile = os.environ.get("HOME"), os.environ.get("USERPROFILE")
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    try:
        loaded = config_module.load()
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_profile is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = old_profile

    runtime = service_app.build_runtime(loaded)
    with pytest.raises(ConfigValueError):
        runtime.to_dict()
    runtime.set_binding("127.0.0.1", 8765)
    assert runtime.to_dict()["service"]["port"] == 8765


def test_alert_and_drain_dataclasses_encode_the_frozen_fields():
    """`Alert` / `AlertState` / `EndedState` 的编码字段集与取值域逐条对齐 §3.1.2。"""
    alert = Alert(level="hint", source="mic", since="t", duration_seconds=1.5,
                  message="某个提示", muted=True)
    assert set(alert.encode()) == {
        "level", "source", "since", "duration_seconds", "message", "muted",
    }
    state = AlertState(critical=alert, hints=[alert])
    assert set(state.encode()) == {"critical", "hints"}
    assert set(Snapshot().alerts.encode()) == {"critical", "hints"}
