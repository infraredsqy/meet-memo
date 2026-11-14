from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest


from meeting import config as config_module
from meeting.backends.base import ReadOnlyConfig, TranscriptLine
from meeting.service import runtime as runtime_module
from meeting.service.hub import SUBSCRIBER_QUEUE_SIZE
from meeting.state import ENVELOPE_BAR_COUNT
from tests import _contract
from tests import _drain_fakes as fakes
from tests import _helpers as helpers
from tests._contract import emit, normalize, normalize_frame
from tests._helpers import wait_until
from tests._service import SseClient, parse_sse_frame, post_action

from tests.test_contract_fixtures._common import (
    CAPTURE_POLL_TIMEOUT_SECONDS,
    TOP_LEVEL_KEYS,
    _act,
    _action_record,
    _assert_heartbeat_shape,
    _assert_snapshot_shape,
    _wait_envelope_full,
    _wait_snapshot,
    contract_regen,
    make_client,
)

pytestmark = pytest.mark.slow


def test_capture_idle_baseline(tmp_path, make_client, contract_regen):
    """idle 快照 + 空闲心跳 + 空闲态的拒绝动作（dev 替身：两路 ready）。"""
    client = make_client({"capture.backend": "dev"})

    snapshot = client.get("/api/state").json()
    _assert_snapshot_shape(snapshot, top_state="idle", envelope_filled=False)
    assert snapshot["session"] is None
    assert snapshot["ended"] is None
    # 空闲态**不发提示级**。这一格与下面的 `transcript.health == "stopped"`
    # 说的是两件事：那一格报的是「真转写后端还没起过场次」，提示级说的是「录音中转写
    # 跟不上了」。
    assert snapshot["alerts"] == {"critical": None, "hints": []}
    assert snapshot["transcript"]["last_line_at"] is None
    assert all(item["health"] == "ready" for item in snapshot["sources"])

    heartbeat = client.get("/api/heartbeat").json()
    _assert_heartbeat_shape(heartbeat)
    assert heartbeat["top_state"] == "idle"
    assert heartbeat["recording_alive"] is None
    assert heartbeat["seconds_since_last_tick"] is None

    refused = _act(client, "stop")
    assert refused.status_code == 409
    refused_body = refused.json()
    assert refused_body["ok"] is False
    assert refused_body["code"] == "not_recording"
    assert refused_body["detail"] == {}

    emit(
        {
            "snapshot/idle.json": normalize(snapshot),
            "heartbeat/idle.json": normalize(heartbeat),
            "actions/fail-not-recording.json": _action_record(refused, "stop", {}),
        },
        regen=contract_regen,
    )


def test_capture_action_failures_on_idle(tmp_path, make_client, monkeypatch, contract_regen):
    """七类失败动作里的五类（idle 即可触发）+ `settings_update` 成功体。

    **采集后端显式取 `null` 替身**：`auto` 是真采集后端，`start` 会去真枚举、真试开
    设备 —— golden 样本既会带上这台机器的设备名，又会在有声卡的机器上真的打开一次麦克风。
    `null` 替身的 `start()` 照样抛 `CaptureUnavailable`，`detail` 的三键形状与 `message`
    文案逐字不变，golden 样本因此不漂移。
    """
    client = make_client({"capture.backend": "null"})

    unknown = _act(client, "definitely_not_an_action")
    assert unknown.status_code == 400
    unknown_body = unknown.json()
    assert unknown_body["code"] == "invalid_setting"
    assert set(unknown_body["detail"]) == {"action", "known"}

    bad_mode = _act(client, "start", {"mode": "burst"})
    assert bad_mode.status_code == 400
    bad_mode_body = bad_mode.json()
    assert bad_mode_body["code"] == "invalid_setting"
    assert set(bad_mode_body["detail"]) == {"key", "value", "legal"}
    assert bad_mode_body["detail"]["legal"] == ["both", "mic", "loopback"]

    unavailable = _act(client, "dev_inject", {"source": "mic", "fault": "stall"})
    assert unavailable.status_code == 404
    unavailable_body = unavailable.json()
    assert unavailable_body["code"] == "action_unavailable"
    assert unavailable_body["detail"] == {}

    capture_down = _act(client, "start")
    assert capture_down.status_code == 503
    capture_body = capture_down.json()
    assert capture_body["code"] == "capture_unavailable"
    assert set(capture_body["detail"]) == {"session_id", "dir", "reason"}

    # 成功体先采（写盘注入要在它之后才挂上）
    settings = _act(client, "settings_update", {"asr.timeout_seconds": 30})
    assert settings.status_code == 200, settings.text
    settings_body = settings.json()
    assert set(settings_body) == {"ok", "revision", "applied", "pending_restart"}
    assert "message" not in settings_body
    assert settings_body["applied"] == ["asr.timeout_seconds"]
    assert settings_body["pending_restart"] == []

    def _boom(*args, **kwargs):
        raise OSError("注入的写盘失败")

    monkeypatch.setattr(config_module, "write_config_values", _boom)
    persist = _act(client, "settings_update", {"asr.timeout_seconds": 30})
    assert persist.status_code == 500
    persist_body = persist.json()
    assert persist_body["code"] == "persist_failed"
    assert set(persist_body["detail"]) == {"error"}
    assert "注入的写盘失败" in persist_body["detail"]["error"]

    emit(        {
            "actions/fail-invalid-setting-unknown-action.json": _action_record(
                unknown, "definitely_not_an_action", {}
            ),
            "actions/fail-invalid-setting.json": _action_record(
                bad_mode, "start", {"mode": "burst"}
            ),
            "actions/fail-action-unavailable.json": _action_record(
                unavailable, "dev_inject", {"source": "mic", "fault": "stall"}
            ),
            "actions/fail-capture-unavailable.json": _action_record(
                capture_down, "start", {"dir": "<path>"}
            ),
            "actions/settings-update-ok.json": _action_record(
                settings, "settings_update", {"asr.timeout_seconds": 30}
            ),
            "actions/fail-persist-failed.json": _action_record(
                persist, "settings_update", {"asr.timeout_seconds": 30}
            ),
        },
        regen=contract_regen,
    )


def test_capture_recording_group(tmp_path, make_client, contract_regen):
    """录音态快照 + 录音中心跳 + start/mute 成功体 + already_recording + resync 帧。"""
    client = make_client({"capture.backend": "dev"})

    started = _act(client, "start", {"mode": "both"})
    assert started.status_code == 200, started.text
    assert started.json()["ok"] is True

    _wait_envelope_full(client)
    snapshot = _wait_snapshot(client, lambda snap: snap["top_state"] == "recording")
    _assert_snapshot_shape(snapshot, top_state="recording", envelope_filled=True)
    assert snapshot["session"]["mode"] == "both"
    assert snapshot["session"]["frozen"] is False
    assert all(item["health"] == "capturing" for item in snapshot["sources"])

    heartbeat = wait_until(
        lambda: (
            lambda hb: hb if hb["recording_alive"] is True else None
        )(client.get("/api/heartbeat").json()),
        timeout=CAPTURE_POLL_TIMEOUT_SECONDS,
    )
    _assert_heartbeat_shape(heartbeat)
    assert heartbeat["top_state"] == "recording"

    duplicate = _act(client, "start", {"mode": "both"})
    assert duplicate.status_code == 409
    duplicate_body = duplicate.json()
    assert duplicate_body["code"] == "already_recording"
    assert set(duplicate_body["detail"]) == {"session_id", "dir", "elapsed_seconds"}

    muted = _act(client, "mute_session")
    assert muted.status_code == 200, muted.text
    assert muted.json()["ok"] is True

    # resync 帧：`hub.py` 的真实广播路径 —— 注册一个真订阅者，灌满 512 队列之后，
    # 下一次 `broadcast_state()` 的 offer 失败即走 `resync()`（清队列 + 补一份带
    # resync/dropped 的快照帧）。dropped 恰为 1（溢出一次），采集因此可复现。
    runtime = client.app.state.runtime
    snapshot_json = runtime.to_dict()
    subscriber = runtime.hub.register()
    try:
        for _ in range(SUBSCRIBER_QUEUE_SIZE):
            runtime.hub.broadcast_state(snapshot_json)
        runtime.hub.broadcast_state(snapshot_json)
        item = subscriber.take()
    finally:
        runtime.hub.unregister(subscriber)
    assert item is not None and item[0] == "snapshot"
    resync = parse_sse_frame(item[1])
    assert resync["event"] == "snapshot"
    assert resync["id"] is not None
    assert int(resync["id"]) == resync["data"]["revision"] == snapshot_json["revision"]
    assert resync["data"]["resync"] is True
    assert resync["data"]["dropped"] == 1
    assert set(resync["data"]) == TOP_LEVEL_KEYS | {"resync", "dropped"}

    emit(
        {
            "actions/start-ok.json": _action_record(
                started, "start", {"mode": "both", "dir": "<path>"}
            ),
            "snapshot/recording.json": normalize(snapshot),
            "heartbeat/recording.json": normalize(heartbeat),
            "actions/fail-already-recording.json": _action_record(
                duplicate, "start", {"mode": "both", "dir": "<path>"}
            ),
            "actions/mute-session-ok.json": _action_record(muted, "mute_session", {}),
            "sse/resync-snapshot.json": normalize_frame(resync),
        },
        regen=contract_regen,
    )


def test_capture_heartbeat_no_first_frame(tmp_path, make_client, contract_regen):
    """「还没有第一帧」的心跳样本：录音态而 `recording_alive` 为 `null`。

    采样办法：进程内直调 dev 替身的 `inject_fault()` 把 `stall` 挂上**再** start
    （`dev_inject` 动作要有在录场次才挂得上，开场前的那段只能进程内直挂）——合成线程
    第一拍就看到停发，本场从 start 起一帧都没见过。于是开场窗口（2.0s）内的心跳读到的
    正是「还没有第一帧」那一档：`recording_alive` 与 `seconds_since_last_tick` 同为
    `null`，而 `top_state` 已经是 `recording`。这一档从此有自己的样本，界面不再把
    它等同于「停止」。

    样本落盘之后顺手把窗口走完的那一档验掉（幂等样本要的确定性由 stall 全程挡帧保证）：
    超窗仍无首帧，`recording_alive` 落 `false` —— 「别把未知做成永不报」，真饿死报得出。
    """
    client = make_client({"capture.backend": "dev"})
    runtime = client.app.state.runtime
    runtime.capture_backend.inject_fault(
        source="mic", fault="stall", after_seconds=0.0, duration_seconds=30.0
    )

    started = _act(client, "start", {"mode": "both"})
    assert started.status_code == 200, started.text

    heartbeat = wait_until(
        lambda: (
            lambda hb: hb
            if hb["top_state"] == "recording" and hb["recording_alive"] is None
            else None
        )(client.get("/api/heartbeat").json()),
        timeout=CAPTURE_POLL_TIMEOUT_SECONDS,
    )
    _assert_heartbeat_shape(heartbeat)
    assert heartbeat["seconds_since_last_tick"] is None, "一帧都没收到过，走秒量如实 null"

    starved = wait_until(
        lambda: (
            lambda hb: hb if hb["recording_alive"] is False else None
        )(client.get("/api/heartbeat").json()),
        timeout=CAPTURE_POLL_TIMEOUT_SECONDS,
    )
    assert starved["top_state"] == "recording"
    assert starved["seconds_since_last_tick"] is None

    emit({"heartbeat/no-first-frame.json": normalize(heartbeat)}, regen=contract_regen)


def test_capture_heartbeat_capture_gaps(tmp_path, make_client, contract_regen):
    """「帧空洞留痕」的心跳样本（gil-stall-hole）：`capture_gaps` 带一条合并后的记录。

    采样办法：dev 替身正常开一场（等 `recording_alive is True`，跟 `no-first-frame`
    那份正好互补 —— 那份是「一帧都没见过」的未知档，这份要帧已经流过、空洞才有记录可落），
    然后在进程内直调 `runtime.apply_fact()` 打进两条**区间重叠**的 `FrameGap` 事实
    （同一次冻结、两路刻度线各报一条的真实形状），心跳里应合并为一条。

    确定性：`at` 走 `<iso>` 掩码、`seconds` / `total_seconds` 走走秒掩码落 0、
    `total_count` 是确定值 —— 这一格样本记的是**形状**（一键三格 + recent 每条两键）。
    """
    client = make_client({"capture.backend": "dev"})
    runtime = client.app.state.runtime

    assert _act(client, "start", {"mode": "both"}).status_code == 200
    heartbeat = wait_until(
        lambda: (
            lambda hb: hb if hb["recording_alive"] is True else None
        )(client.get("/api/heartbeat").json()),
        timeout=CAPTURE_POLL_TIMEOUT_SECONDS,
    )
    assert heartbeat["capture_gaps"] == {"total_count": 0, "total_seconds": 0.0, "recent": []}

    import time as _time

    from meeting.backends.base import FrameGap

    now = _time.monotonic()
    runtime.apply_fact(FrameGap(source="mic", seconds=2.4, detected=now))
    runtime.apply_fact(FrameGap(source="loopback", seconds=2.4, detected=now + 0.01))

    gapped = client.get("/api/heartbeat").json()
    _assert_heartbeat_shape(gapped)
    assert gapped["recording_alive"] is True, "空洞是历史事实，recording_alive 只答当下"
    gaps = gapped["capture_gaps"]
    assert gaps["total_count"] == 1, "同一回合的两条事实合并为一条"
    assert len(gaps["recent"]) == 1
    assert set(gaps["recent"][0]) == {"at", "seconds"}

    emit({"heartbeat/capture-gap.json": normalize(gapped)}, regen=contract_regen)


def test_capture_alert_snapshot(tmp_path, make_client, contract_regen):
    """告警态快照（小静默阈值 + dev_inject(loopback, silence) 的真实动作路径）。"""
    client = make_client({"capture.backend": "dev", "alert.silence_seconds": 1})

    started = _act(client, "start", {"mode": "both"})
    assert started.status_code == 200, started.text
    injected = _act(
        client,
        "dev_inject",
        {"source": "loopback", "fault": "silence", "after_seconds": 0},
    )
    assert injected.status_code == 200, injected.text

    snapshot = _wait_snapshot(client, lambda snap: snap["top_state"] == "alert")
    _assert_snapshot_shape(snapshot, top_state="alert", envelope_filled=True)
    critical = snapshot["alerts"]["critical"]
    assert critical is not None
    assert critical["level"] == "critical"
    assert critical["source"] == "loopback"
    assert critical["duration_seconds"] >= 1.0
    # **首发那一帧该出声**：`muted` 说的是「这一条现在会不会出声」，一条新起的告警首发是
    # `false`（界面据此响一声），此后同一条才是 `true`。这条 golden 样本抓的就是首发那一帧。
    assert critical["muted"] is False, (
        f"致命级首发帧该是 muted=false（读到 {critical['muted']}）—— "
        "提交时那次重算把它压住的话，界面按契约看这个布尔值，一声都不会响"
    )
    loopback = next(item for item in snapshot["sources"] if item["id"] == "loopback")
    assert loopback["health"] == "lost"
    assert loopback["lost_since"]
    mic = next(item for item in snapshot["sources"] if item["id"] == "mic")
    assert mic["health"] == "capturing"

    emit({"snapshot/alert.json": normalize(snapshot)}, regen=contract_regen)


def test_capture_ended_done(tmp_path, make_client, contract_regen):
    """ended·done 快照 + stop 成功体（收尾前三步走完、第 4 步停在「等你动手」的终态）。

    按「结束录音」之后会后一个动作都不自动跑：第 4 步 `post` 做完它该做的
    （释放会中模型）就如实停在 `waiting`，这一格与 `post.hook` 取什么值无关 ——
    会后那两趟活归工作台上那两个按钮（`post_offline` / `post_minutes`）。
    """
    client = make_client({"capture.backend": "dev"})

    assert _act(client, "start", {"mode": "both"}).status_code == 200
    _wait_envelope_full(client)
    stopped = _act(client, "stop")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["ok"] is True

    snapshot = _wait_snapshot(
        client, lambda snap: bool(snap["ended"]) and snap["ended"]["phase"] == "done"
    )
    _assert_snapshot_shape(snapshot, top_state="ended", envelope_filled=True)
    assert snapshot["ended"]["error"] is None
    assert [step["status"] for step in snapshot["ended"]["steps"]] == [
        "skipped", "done", "waiting",
    ]  # 三步（转码已摘出收尾序列）：flush skipped / log done / post waiting
    assert all(step["error"] is None for step in snapshot["ended"]["steps"])
    assert snapshot["session"]["frozen"] is True
    # 成功档的收尾副作用：两路从 stopped 回 unprobed
    assert all(item["health"] == "unprobed" for item in snapshot["sources"])

    emit(
        {
            "actions/stop-ok.json": _action_record(stopped, "stop", {}),
            "snapshot/ended-done.json": normalize(snapshot),
        },
        regen=contract_regen,
    )


def test_capture_ended_draining(tmp_path, make_client, monkeypatch, contract_regen):
    """ended·draining 停留档快照 + draining 拒绝（进程内把 `log` 那一步的落点换成抛错的替身）。

    失败步的工具箱：第 4 步 `post` 现在**不再自动拉起会后**，它每次都停在 `waiting`
    —— 拿它演不出失败档。这一条改把 `log` 那一步写**场次日志结束标记**的落点换成抛错的替身：
    「有实现却抛错 → `failed`」这一档照旧在收尾里看得见，`post` 那一格仍是
    `waiting`。快照样本记的是**形状**（哪一步 failed、`detail` 有哪几键），
    那句话的内容不逐字进 golden。
    """
    home = tmp_path / "home-drain"
    home.mkdir(parents=True, exist_ok=True)
    plugin = fakes.write_failing_post_hook(home, capture="dev")
    monkeypatch.syspath_prepend(str(plugin.directory))
    values = dict(plugin.config)
    values.setdefault("web.dist_dir", str(helpers.missing_dist_dir(home)))
    with helpers.app_client(home, values=values) as client:
        client.place_root = tmp_path / "out-drain"
        client.place_root.mkdir(parents=True, exist_ok=True)
        runtime = client.app.state.runtime

        def boom_end_marker(*args, **kwargs):
            raise RuntimeError("假场次日志接缝：按样本要求抛错")

        monkeypatch.setattr(
            runtime_module.logging_setup, "write_session_end_marker", boom_end_marker
        )
        assert _act(client, "start", {"mode": "both"}).status_code == 200
        _wait_envelope_full(client)
        assert _act(client, "stop").status_code == 200

        def settled(snap):
            """收尾前三步都落定、第 4 步如实停在「等你动手」那一格。

            判据把 `waiting` 算作落定（那一格就是 `post` 的终态），`running` 不算。
            """
            steps = (snap.get("ended") or {}).get("steps") or []
            if not steps or any(step["status"] == "running" for step in steps):
                return False
            return snap["top_state"] == "ended"

        snapshot = _wait_snapshot(client, settled)
        _assert_snapshot_shape(snapshot, top_state="ended", envelope_filled=True)
        assert snapshot["ended"]["phase"] == "draining"
        assert snapshot["ended"]["error"]
        post_step = snapshot["ended"]["steps"][-1]
        assert post_step["id"] == "post"
        assert post_step["status"] == "waiting"
        failed = [step for step in snapshot["ended"]["steps"] if step["status"] == "failed"]
        assert [step["id"] for step in failed] == ["log"]
        assert "假场次日志接缝" in failed[0]["error"]
        # 失败停留档保留 stopped 现场
        assert all(item["health"] == "stopped" for item in snapshot["sources"])

        refused = _act(client, "start", {"mode": "both"})
        assert refused.status_code == 409
        refused_body = refused.json()
        assert refused_body["code"] == "draining"
        detail = refused_body["detail"]
        assert set(detail) == {"session_id", "dir", "failed_steps", "log_path"}
        assert detail["failed_steps"] == [
            {"id": "log", "error": failed[0]["error"]}
        ]

    emit(
        {
            "snapshot/ended-draining.json": normalize(snapshot),
            "actions/fail-draining.json": _action_record(
                refused, "start", {"mode": "both", "dir": "<path>"}
            ),
        },
        regen=contract_regen,
    )


