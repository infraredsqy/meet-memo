"""`tests/test_contract_fixtures.py` —— `contract/fixtures/` 的采集与漂移比对（c62dec56 phase1）。

两个模式（开关注册在 `tests/conftest.py` 的 `pytest_addoption`）：

  - **默认**：重新走一遍真实服务路径采集样本，与 `contract/fixtures/` 逐文件比对，
    缺文件或内容不一致即红 —— 服务侧的形状漂移在这一步被拦下；
  - **`--regen-contract`**：同样的采集，改写 fixtures。掩码（`tests/_contract.py` 的
    `normalize()`）把墙钟、路径、端口、电平这些每跑必变的量收成自描述占位符，
    于是连跑两次 `--regen-contract` 落盘的内容逐字相同；`revision` 保留真实值 ——
    它由固定的迁移与提交次数决定，同样确定。

样本出处（更细的出处表在 `contract/README.md`）：

  - **进程内 TestClient**（真实 lifespan + startup 钩子 + 动作端点）：idle / recording /
    alert / ended·done / ended·draining 的快照、四个心跳样本（idle / recording /
    还没有第一帧 / 帧空洞留痕）、成功动作、七类失败动作、
    resync 帧（`hub.py` 的真实广播路径：真订阅者队列灌满 512 后的下一次广播走 `resync()`）；
  - **真服务子进程**：SSE 帧样本（dev 采集替身 + `module:Class` 假转写后端，见
    `tests/_contract.write_transcriber_stub()`）。

结构断言（在掩码前的原始载荷上做）：快照顶层九键、两路次序、录音态包络恰 15 根、
`bar == envelope[-1]`、levels 帧 `ts` 恒 null、resync 帧顶层额外 `resync` / `dropped` 两键。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

#: 慢档标记（68927c27 收尾轮）：真服务/真样本采集的收尾验收，不进每次迭代圈。
pytestmark = pytest.mark.slow

from meeting import config as config_module
from meeting.backends.base import ReadOnlyConfig, TranscriptLine
from meeting.service import runtime as runtime_module
from meeting.service.hub import SUBSCRIBER_QUEUE_SIZE
from meeting.state import ENVELOPE_BAR_COUNT
from tests import _contract
from tests import _drain_fakes as fakes
from tests import _helpers as helpers
from tests._contract import emit, normalize, normalize_frame
from tests._service import SseClient, parse_sse_frame, post_action, wait_until

#: 结构断言与心跳样本共用的键全集（与 `Snapshot.to_dict()` 的产出逐字对应）
TOP_LEVEL_KEYS = {
    "revision", "service", "top_state", "session", "sources",
    "alerts", "transcript", "ended", "settings",
}
SOURCE_KEYS = {
    "id", "label", "device_name", "health", "lost_since",
    "restored", "level_dbfs", "peak_dbfs", "envelope",
}
HEARTBEAT_KEYS = {
    "service", "server_time", "revision", "top_state",
    "recording_alive", "seconds_since_last_tick", "capture_gaps",
}
TRANSCRIPT_LINE_KEYS = {"ts", "source", "text", "kind"}
#: 快照 `settings` 的键全集（`311f84f5` 加了 `summary_default_window`；
#: 会中切模型节点加了 `summary_preset_names` / `summary_active_preset`，共七键）
SETTINGS_KEYS = {
    "alarm_sound", "heartbeat_interval_seconds", "summary_default_window",
    "summary_preset_names", "summary_active_preset", "asr", "devices",
}

#: 各态等待的显式超时（秒；单条用例 ≤ 5 秒看门之内的具名预算）
CAPTURE_POLL_TIMEOUT_SECONDS = 4.0
#: SSE 读帧的显式超时（秒）
SSE_READ_TIMEOUT_SECONDS = 10.0


@pytest.fixture()
def contract_regen(request: pytest.FixtureRequest) -> bool:
    """`--regen-contract` 开关的读法（比对模式与再生成模式共用同一套采集）。"""
    return bool(request.config.getoption("--regen-contract"))


@pytest.fixture()
def make_client(tmp_path: Path):
    """进程内起真实应用的多实例工厂（`helpers.app_client` 的上下文栈包装）。"""
    import contextlib

    stack = contextlib.ExitStack()
    made = 0

    def make(values: dict) -> Any:
        nonlocal made
        made += 1
        home = tmp_path / f"home{made}"
        home.mkdir(parents=True, exist_ok=True)
        place_root = tmp_path / f"out{made}"
        place_root.mkdir(parents=True, exist_ok=True)
        values = dict(values)
        values.setdefault("web.dist_dir", str(helpers.missing_dist_dir(home)))
        client = stack.enter_context(helpers.app_client(home, values=values))
        client.place_root = place_root
        return client

    try:
        yield make
    finally:
        stack.close()


def _act(client, action: str, args: dict | None = None):
    """进程内打一次动作（两个必需请求头由这里带全；`start` 由测试层补缺省落点）。"""
    body: dict = {"action": action}
    if args is not None or action == "start":
        body["args"] = helpers.start_args(action, args, client.place_root)
    return client.post("/api/actions", json=body, headers={"X-Meeting-Service": "1"})


def _action_record(response, action: str, args: dict) -> dict:
    """动作样本的 fixtures 形态：请求三件套 + 应答状态码 + 掩码后的应答体。"""
    return {
        "action": action,
        "args": args,
        "status": response.status_code,
        "body": normalize(response.json()),
    }


def _wait_snapshot(client, predicate):
    """轮询 `/api/state` 直到谓词命中（掩码前拿到的都是实时快照）。"""
    return wait_until(
        lambda: (lambda snap: snap if predicate(snap) else None)(
            client.get("/api/state").json()
        ),
        timeout=CAPTURE_POLL_TIMEOUT_SECONDS,
    )


def _wait_envelope_full(client):
    """等到两路包络都长满 15 根（首帧电平已到），消除「连上太早」的形状分叉。"""
    return _wait_snapshot(
        client,
        lambda snap: all(
            len(item["envelope"]) == ENVELOPE_BAR_COUNT for item in snap["sources"]
        ),
    )


def _assert_snapshot_shape(snapshot: dict, *, top_state: str, envelope_filled: bool) -> None:
    assert set(snapshot) == TOP_LEVEL_KEYS
    assert snapshot["top_state"] == top_state
    assert set(snapshot["settings"]) == SETTINGS_KEYS
    assert [item["id"] for item in snapshot["sources"]] == ["mic", "loopback"]
    for item in snapshot["sources"]:
        assert set(item) == SOURCE_KEYS
        if envelope_filled:
            assert len(item["envelope"]) == ENVELOPE_BAR_COUNT
        else:
            assert item["envelope"] == []
        # bar 的同值口径在快照里经 `envelope[-1]` 体现；电平值本身进掩码
        if envelope_filled and item["id"] == "loopback":
            assert item["envelope"]  # 已长满，占位前的原始值非空


def _assert_heartbeat_shape(heartbeat: dict) -> None:
    assert set(heartbeat) == HEARTBEAT_KEYS
    assert heartbeat["service"] == "meeting"
    assert isinstance(heartbeat["revision"], int)


# ---------------------------------------------------------------------------
# idle：快照、心跳、拒绝动作
# ---------------------------------------------------------------------------


def test_capture_idle_baseline(tmp_path, make_client, contract_regen):
    """idle 快照 + 空闲心跳 + 空闲态的拒绝动作（dev 替身：两路 ready）。"""
    client = make_client({"capture.backend": "dev"})

    snapshot = client.get("/api/state").json()
    _assert_snapshot_shape(snapshot, top_state="idle", envelope_filled=False)
    assert snapshot["session"] is None
    assert snapshot["ended"] is None
    # 空闲态**不发提示级**（`docs/ui-design.md` 章 2.5 那张表：空闲态告警区不出现）。
    # 这一格与下面的 `transcript.health == "stopped"` 说的是两件事：那一格报的是「真转写
    # 后端还没起过场次」，提示级说的是「录音中转写跟不上了」。
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

    **采集后端显式取 `null` 替身**（原先写的是 `auto`）：`auto` 在本节点落地之后就是**真采集
    后端**，`start` 会去真枚举、真试开设备 —— golden 样本既会带上这台机器的设备名，又会在有
    声卡的机器上真的打开一次麦克风。`null` 替身的 `start()` 照样抛 `CaptureUnavailable`，
    `detail` 的三键形状与 `message` 文案逐字不变，golden 样本因此不漂移。
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

    emit(
        {
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


# ---------------------------------------------------------------------------
# recording：成功动作、心跳、already_recording、resync 帧
# ---------------------------------------------------------------------------


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
    """「还没有第一帧」的心跳样本（de899128）：录音态而 `recording_alive` 为 `null`。

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


# ---------------------------------------------------------------------------
# alert / ended·done / ended·draining
# ---------------------------------------------------------------------------


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

    按「结束录音」之后会后一个动作都不自动跑（R26）：第 4 步 `post` 做完它该做的
    （释放会中模型）就如实停在 `waiting`，这一格与 `post.hook` 取什么值无关 ——
    大会后那两趟活归工作台上那两个按钮（`post_offline` / `post_minutes`）。
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
        "skipped", "skipped", "done", "waiting",
    ]
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

    失败步的工具箱：第 4 步 `post` 现在**不再自动拉起会后**（R26），它每次都停在 `waiting`
    —— 拿它演不出失败档。这一条改把 `log` 那一步写**场次日志结束标记**的落点换成抛错的替身：
    「有实现却抛错 → `failed`」这一档（§3.0 的边界）照旧在收尾里看得见，`post` 那一格仍是
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

            判据把 `waiting` 算作落定（那一格就是 `post` 的终态，R26），`running` 不算。
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


# ---------------------------------------------------------------------------
# 会中总结：动作样本 + 本场记录端点（`1987cb20` phase2）
# ---------------------------------------------------------------------------


class _ContractTranscriber:
    """样本采集用的假转写后端：`bind()` 记下出口、`health()` 恒 `normal`。

    与 `tests/_contract.py` 写出到子进程的那个假后端同形，区别只在它活在**进程内**
    （这一档走 TestClient，不另起服务子进程）。`start()` 什么都不起：喂行由用例自己经
    `bind_transcriber()` 那个 sink 做，样本的形状因此完全确定。
    """

    def __init__(self) -> None:
        self.line_sink = None

    def bind(self, line_sink, *, config=None) -> None:
        del config
        self.line_sink = line_sink

    def start(self, mode: str, session_dir: str) -> None:
        del mode, session_dir

    def stop(self) -> None:
        pass

    def health(self) -> str:
        return "normal"

    def backlog_depth(self) -> int:
        return 0

    def segment_timeout(self) -> float:
        return 20.0


def test_capture_summarize_group(tmp_path, make_client, contract_regen):
    """`summarize` 成功体 + `GET /api/summaries` 两份样本（假总结客户端注入，不发真请求）。

    窗口里铺 12 行（`ts` 取固定值：`render_window()` 会把它印成 `[HH:MM:SS]`，固定值让两次
    采集逐字一致），三小段由 `_contract.SummaryStub` 的固定输出给。**总结是只读的**：
    这一档前后都不提交，`revision` 因此停在 `start` 那一版上。

    `summary.api_key` 交一个**显眼的假值**：这一格就是 `SummarySettings.missing` 点名的那一格，
    空着的话动作会在出门前停下，而这一档采的是成功体。假客户端从注入点进
    （`runtime.summary_client`，把传输层整个替掉），这个假值不上任何线路。
    """
    client = make_client(
        {"capture.backend": "dev", "summary.api_key": "contract-fixture-fake-key"}
    )
    runtime = client.app.state.runtime

    transcriber = _ContractTranscriber()
    runtime.transcriber_backend = transcriber
    transcriber.bind(runtime.bind_transcriber(transcriber), config=runtime._config_view())
    stub = _contract.SummaryStub()
    runtime.summary_client = stub

    assert _act(client, "start", {"mode": "both"}).status_code == 200
    for index in range(12):
        transcriber.line_sink(
            TranscriptLine(ts=1.5, source="mic", text=f"样本第 {index} 行", kind="line")
        )

    summarized = _act(client, "summarize", {"window": "15m"})
    assert summarized.status_code == 200, summarized.text
    body = summarized.json()
    assert set(body) == {"ok", "revision", "summary"}
    summary = body["summary"]
    assert set(summary) == {
        "window", "window_label", "at", "elapsed_seconds", "lines",
        "what", "where", "lost",
    }
    assert summary["window"] == "15m"
    assert summary["window_label"] == "15 分钟"
    assert summary["lines"] == 12
    assert summary["what"] and summary["where"] and summary["lost"]
    assert stub.calls == 1

    # 记录端点：读的是**内存里**那一份（`session_id` 与 `session.id` 同值）
    listing = client.get("/api/summaries")
    assert listing.status_code == 200
    listed = listing.json()
    assert set(listed) == {"session_id", "entries"}
    assert listed["session_id"] == runtime.snapshot.session.id
    assert len(listed["entries"]) == 1
    entry = listed["entries"][0]
    assert set(entry) == {
        "window", "window_label", "at", "elapsed_seconds", "what", "where", "lost",
    }
    assert entry["at"] == summary["at"]

    # 两个新 `code` 各采一份失败体：`detail` 的形状与 HTTP 码都在样本里看得见。
    # ① `transcript_unavailable`(503)：把转写后端拿掉（健康度读不出 = 没在跑那一档）
    runtime.transcriber_backend = None
    transcript_down = _act(client, "summarize", {"window": "15m"})
    assert transcript_down.status_code == 503, transcript_down.text
    assert transcript_down.json()["code"] == "transcript_unavailable"
    assert transcript_down.json()["detail"] == {"health": "stopped"}
    runtime.transcriber_backend = transcriber

    # ② `summary_failed`(502) 的缺键那一档：把只读配置视图换成一份缺端点的（真配置文件不动）
    real_view = runtime._config_view
    runtime._config_view = lambda: ReadOnlyConfig(
        {"summary.base_url": "   ", "summary.api_key": "   ", "summary.model": "test-model"}
    )
    try:
        endpoint_missing = _act(client, "summarize", {"window": "15m"})
    finally:
        runtime._config_view = real_view
    assert endpoint_missing.status_code == 502, endpoint_missing.text
    assert endpoint_missing.json()["code"] == "summary_failed"
    assert endpoint_missing.json()["detail"] == {
        "missing": ["summary.base_url", "summary.api_key"],
    }

    emit(
        {
            "actions/summarize-ok.json": _action_record(
                summarized, "summarize", {"window": "15m"}
            ),
            "actions/fail-transcript-unavailable.json": _action_record(
                transcript_down, "summarize", {"window": "15m"}
            ),
            "actions/fail-summary-failed-missing.json": _action_record(
                endpoint_missing, "summarize", {"window": "15m"}
            ),
            "summaries/after-one.json": normalize(listed),
        },
        regen=contract_regen,
    )


# ---------------------------------------------------------------------------
# 工作区注册表与目录选择器（工作台收编节点 e2f6166b）
# ---------------------------------------------------------------------------


def test_capture_workspace_group(tmp_path, make_client, contract_regen):
    """`GET /api/sessions` 两份 + `workspace_open` / `workspace_remove` 两份成功体。

    注册表在 `~/.config/meeting/workspaces.json`（按路径去重）；每行的现场读数
    （`dir_exists` / `kind` / `suspected` / 盘上 `session.json` 的 `state`）由服务判，
    样本记的是**形状**：三个目的目录在采集用的临时根下造好（空目录 / 带 `session.json`
    的会议目录 / 有音频归档名没有 `session.json` 的疑似会议目录），路径与目录名一律
    掩成 `<path>` / `<session-id>`。
    """
    client = make_client({"capture.backend": "dev"})

    # 空注册表的那一份（顶层第三格 `post_job`：此刻没有会后那一趟在飞 → `None`）
    listed = client.get("/api/sessions")
    assert listed.status_code == 200
    empty_listing = listed.json()
    assert empty_listing == {"sessions": [], "recent": None, "post_job": None}

    # 造三个目的目录并逐个走「新会议」：空的 / 有会议材料的 / 疑似会议目录
    place = tmp_path / "contract-workspaces"
    place.mkdir()
    empty_dir = place / "empty_ws"
    empty_dir.mkdir()
    meeting_dir = place / "meeting_ws"
    meeting_dir.mkdir()
    # 段记录给两段（续接过的形状）：时间戳按 ISO 形状写（掩成 <iso>），
    # 秒数走掩码（`duration_seconds` 在 ZERO_KEYS 里，落 0）
    (meeting_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": "meeting_ws",
                "state": "done",
                "date": "2026-09-21",
                "mode": "both",
                "segments": [
                    {
                        "index": 1,
                        "started_at": "2026-09-21T10:00:00+08:00",
                        "ended_at": "2026-09-21T10:19:00+08:00",
                        "duration_seconds": 61.5,
                    },
                    {
                        "index": 2,
                        "started_at": "2026-09-21T11:30:00+08:00",
                        "ended_at": "2026-09-21T11:32:00+08:00",
                        "duration_seconds": 122.3,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    suspected_dir = place / "suspected_ws"
    suspected_dir.mkdir()
    (suspected_dir / "meeting_audio_mic.pcm").write_bytes(b"")
    # 一个非空又没有材料的目录：被确认之前拒（`invalid_setting` + confirm 提示）
    junk_dir = place / "junk_ws"
    junk_dir.mkdir()
    (junk_dir / "wj.pdf").write_bytes(b"%PDF")

    opened_empty = _act(client, "workspace_open", {"dir": str(empty_dir)})
    assert opened_empty.status_code == 200, opened_empty.text
    assert opened_empty.json()["workspace"]["kind"] == "empty"

    opened_meeting = _act(client, "workspace_open", {"dir": str(meeting_dir)})
    assert opened_meeting.status_code == 200, opened_meeting.text

    suspected = _act(client, "workspace_open", {"dir": str(suspected_dir)})
    assert suspected.status_code == 200, suspected.text
    assert suspected.json()["workspace"]["suspected"] is True

    confirm_required = _act(client, "workspace_open", {"dir": str(junk_dir)})
    assert confirm_required.status_code == 400
    assert confirm_required.json()["code"] == "invalid_setting"

    after = client.get("/api/sessions").json()
    assert len(after["sessions"]) == 3
    assert after["recent"] is not None
    # 删除的一档：从列表摘走（返回 `removed`），文件无人碰 —— 文件面归单测，这里只采形状
    removed = _act(client, "workspace_remove", {"dir": str(empty_dir)})
    assert removed.status_code == 200
    assert removed.json()["workspace"]["removed"] is True

    emit(
        {
            "sessions/empty.json": normalize(empty_listing),
            "sessions/after-open.json": normalize(after),
            "actions/workspace-open-ok.json": _action_record(
                opened_meeting, "workspace_open", {"dir": "<path>"}
            ),
            "actions/workspace-remove-ok.json": _action_record(
                removed, "workspace_remove", {"dir": "<path>"}
            ),
        },
        regen=contract_regen,
    )


def test_capture_devices_group(tmp_path, make_client, monkeypatch, contract_regen) -> None:
    """`GET /api/devices` 那一份（设置抽屉的两个设备下拉，节点 `311f84f5`）。

    设备枚举在样本采集里**一律走假件**（`tests/conftest.py` 的看门只挡「真开设备」，
    清单端点的确定性靠这里把 `sources.py` 的两个枚举函数换成固定端点表）：名字与
    `is_default` 是确定值，原样进样本；**不开任何设备**这一点由端点实现本身保证
    （`devices_listing()` 只枚举，`probe()` 的试开不在它的路上）。
    """
    from meeting.capture import sources as sources_module
    from meeting.capture.devices import DeviceEndpoint

    fake_inputs = [
        DeviceEndpoint("假-Focusrite Analogue 1 + 2", "MME", "input", index=0, channels=4,
                       is_default=True),
        DeviceEndpoint("假-CABLE-Output (VB-Audio Virtual Cable)", "MME", "input", index=2,
                       channels=16),
    ]
    fake_renders = [
        DeviceEndpoint("假-扬声器 (Focusrite USB Audio)", "", "render", index=0),
        DeviceEndpoint("假-CABLE Input (VB-Audio Virtual Cable)", "", "render", index=1),
    ]
    monkeypatch.setattr(sources_module, "list_input_endpoints", lambda: list(fake_inputs))
    monkeypatch.setattr(sources_module, "list_render_endpoints", lambda: list(fake_renders))

    client = make_client({"capture.backend": "dev"})
    before = client.get("/api/state").json()

    listed = client.get("/api/devices")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert set(body) == {"mic", "loopback"}
    assert body["mic"][0]["name"] == "假-Focusrite Analogue 1 + 2"
    assert body["mic"][0]["is_default"] is True
    assert body["loopback"][1]["name"] == "假-CABLE Input (VB-Audio Virtual Cable)"
    # 纯读：不涨 revision、不动快照
    assert int(client.get("/api/state").json()["revision"]) == int(before["revision"])

    emit({"devices/all.json": normalize(body)}, regen=contract_regen)


def test_capture_session_status_group(tmp_path, make_client, contract_regen) -> None:
    """`GET /api/sessions/{id}` 那一份（会后视图的进度区，节点 `a9affaa8`）。

    这一份的**来源是真实服务路径上的只读端点**：场次目录里先按真实形状落下那份逐块
    进度记录（`meeting_offline_progress.jsonl`，两路各两块，含一块静音、一块失败）与
    状态文件，再把这一场登记进工作台、打一次那只端点。

    样本记的是**形状**：路径与场次目录名掩成 `<path>` / `<session-id>`，两个墙钟戳
    掩成 `<iso>`，走秒的量（`elapsed_seconds` / `wall_seconds`）掩成 `0`。块号、
    块数、四档计数、四段文字这些是**确定值**（造出来的记录是固定的），原样留着 ——
    它们正是这一份样本要给人看的形状。
    """
    from meeting.offline import artifacts as offline_artifacts
    from meeting.offline import progress as offline_progress

    client = make_client({"capture.backend": "dev"})

    place = tmp_path / "contract-session-status"
    place.mkdir()
    session_dir = place / "250906"
    session_dir.mkdir()
    # 段记录给一段（形状样本；时间戳按 ISO 形状写、秒数走掩码落 0）
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": "250906",
                "state": "done",
                "date": "2025-09-06",
                "mode": "both",
                "segments": [
                    {
                        "index": 1,
                        "started_at": "2025-09-06T20:00:00+08:00",
                        "ended_at": "2025-09-06T20:03:00+08:00",
                        "duration_seconds": 3.0,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # 这一场有一份会后精细稿（`has_offline` 那一格因此为真）
    (session_dir / "meeting_offline_transcript_mic.md").write_text(
        "[00:00] **S01**: 会后稿的一行\n", encoding="utf-8"
    )
    # 那一份逐块记录：两路各两块，mic 的第 2 块没声跳过、loopback 的第 2 块没跑成
    offline_progress.write_progress(
        offline_progress.ledger_path(session_dir),
        session_id="250906",
        sources=["mic", "loopback"],
        rows=[
            offline_progress.ChunkProgress(
                "mic", 1, 0.0, 450.0, "transcribed",
                "[00:00] **c01:S01**: 第一块的一句话", 3, 45.247,
            ).to_row(chunks_total=2),
            offline_progress.ChunkProgress(
                "mic", 2, 450.0, 450.0, "silent",
            ).to_row(chunks_total=2),
            offline_progress.ChunkProgress(
                "loopback", 1, 0.0, 450.0, "transcribed",
                "[00:00] **c01:S01**: 这一路第一块的一句话", 2, 30.5,
            ).to_row(chunks_total=2),
            offline_progress.ChunkProgress(
                "loopback", 2, 450.0, 450.0, "failed",
                error="engine_error：RuntimeError: 这一块没过",
            ).to_row(chunks_total=2),
        ],
    )
    offline_artifacts.write_status(
        session_dir / offline_artifacts.STATUS_NAME,
        session_id="250906",
        state="done",
        started_at="2025-09-06T20:00:00",
        sources=["mic", "loopback"],
        artifacts={},
        finished_at="2025-09-06T20:02:30",
        partial=True,
    )

    opened = _act(client, "workspace_open", {"dir": str(session_dir)})
    assert opened.status_code == 200, opened.text

    queried = client.get(f"/api/sessions/{quote(str(session_dir), safe='')}")
    assert queried.status_code == 200, queried.text
    body = queried.json()
    assert body["offline"]["sources"][0]["total"] == 2

    emit(
        {"sessions/status-after-run.json": normalize(body)},
        regen=contract_regen,
    )


def test_capture_session_status_interrupted_group(tmp_path, make_client, contract_regen) -> None:
    """`GET /api/sessions/{id}` 的**被打断档**（b1c6a58e）：`offline.state` 照盘上答
    `interrupted`。

    采集形状与 `status-after-run` 同一条路：场次目录里落一份逐块记录（mic 两块里跑了
    一块、loopback 没跑）与一版 `interrupted` 状态文件，登记进工作台之后打那只端点。
    打断这一档要回答的是「停了、可以重按」：`offline.state` 是盘上自己的终态
    `interrupted`（折档行把它照盘透传，不许折回 `running`）、`in_process` 是 `false`。
    """
    from meeting.offline import artifacts as offline_artifacts
    from meeting.offline import progress as offline_progress

    client = make_client({"capture.backend": "dev"})

    place = tmp_path / "contract-session-interrupted"
    place.mkdir()
    session_dir = place / "260907"
    session_dir.mkdir()
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": "260907",
                "state": "done",
                "date": "2026-09-07",
                "mode": "both",
                "segments": [
                    {
                        "index": 1,
                        "started_at": "2026-09-07T20:00:00+08:00",
                        "ended_at": "2026-09-07T20:03:00+08:00",
                        "duration_seconds": 3.0,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    offline_progress.write_progress(
        offline_progress.ledger_path(session_dir),
        session_id="260907",
        sources=["mic", "loopback"],
        rows=[
            offline_progress.ChunkProgress(
                "mic", 1, 0.0, 450.0, "transcribed",
                "[00:00] **c01:S01**: 被打断前跑完的那一块", 3, 41.7,
            ).to_row(chunks_total=2),
        ],
    )
    offline_artifacts.write_status(
        session_dir / offline_artifacts.STATUS_NAME,
        session_id="260907",
        state="interrupted",
        started_at="2026-09-07T20:00:00",
        sources=["mic", "loopback"],
        artifacts={},
        finished_at="2026-09-07T20:01:30",
        error="interrupted：跑到第 1/2 块被停下；没开跑：loopback",
    )

    opened = _act(client, "workspace_open", {"dir": str(session_dir)})
    assert opened.status_code == 200, opened.text

    queried = client.get(f"/api/sessions/{quote(str(session_dir), safe='')}")
    assert queried.status_code == 200, queried.text
    body = queried.json()
    assert body["offline"]["state"] == "interrupted"
    assert body["offline"]["in_process"] is False

    emit(
        {"sessions/status-interrupted.json": normalize(body)},
        regen=contract_regen,
    )


def test_capture_materials_group(tmp_path, make_client, contract_regen) -> None:
    """会议资料四条端点的样本（资料节点 285bf58c）：上传 / 清单 / 删除 / 取不到。

    采样路径是**真实服务路径**：往临时根下造一个目的目录、经 `workspace_open` 登记，
    然后打 `/api/sessions/{id}/materials` 那四条端点。样本记的是**形状**：

    - 上传三连：干净名字原样存、重名自动加序号后缀（`报告.pdf` → `报告 (2).pdf`）、
      穿越加非法字符的脏名字清洗后落盘 —— 名字是**确定值**，原样留在样本里；
    - 清单一条三格（`name` / `size_bytes` / `added_at`），贴上时刻走 `<iso>` 掩码、
      路径走 `<path>` 掩码、字节数是确定值；
    - 删除成功体与「取不到」的 404 失败体（`material_missing`）各一份。

    资料层独立于快照：这一档不提交、不涨 `revision`，样本里的 `revision` 是登记那一刻
    的现值（确定）。
    """
    client = make_client({"capture.backend": "dev"})

    place = tmp_path / "contract-materials"
    place.mkdir()
    session = place / "250906"
    session.mkdir()
    opened = _act(client, "workspace_open", {"dir": str(session)})
    assert opened.status_code == 200, opened.text

    base = f"/api/sessions/{quote(str(session), safe='')}/materials"

    first = client.post(
        f"{base}?name={quote('报告.pdf', safe='')}",
        content=b"%PDF-1.4\ncontract fixture bytes\n",
        headers={"X-Meeting-Service": "1"},
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert set(first_body) == {"ok", "revision", "material"}
    assert set(first_body["material"]) == {"name", "size_bytes", "added_at"}
    assert first_body["material"]["name"] == "报告.pdf"
    assert first_body["material"]["size_bytes"] == len(b"%PDF-1.4\ncontract fixture bytes\n")

    duplicate = client.post(
        f"{base}?name={quote('报告.pdf', safe='')}",
        content=b"duplicate bytes",
        headers={"X-Meeting-Service": "1"},
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["material"]["name"] == "报告 (2).pdf"

    hostile = client.post(
        f"{base}?name={quote('..\\假报告<v>?.pdf ', safe='')}",
        content=b"cleaned bytes",
        headers={"X-Meeting-Service": "1"},
    )
    assert hostile.status_code == 200, hostile.text
    assert hostile.json()["material"]["name"] == "假报告_v__.pdf"

    listed = client.get(base)
    assert listed.status_code == 200
    listing = listed.json()
    assert set(listing) == {"dir", "materials_dir", "items"}
    assert [row["name"] for row in listing["items"]] == [
        "假报告_v__.pdf", "报告 (2).pdf", "报告.pdf",
    ]
    for row in listing["items"]:
        assert set(row) == {"name", "size_bytes", "added_at"}

    fetched = client.get(f"{base}/{quote('报告.pdf', safe='')}")
    assert fetched.status_code == 200
    assert fetched.content == b"%PDF-1.4\ncontract fixture bytes\n"
    assert fetched.headers["content-type"].startswith("application/pdf")

    deleted = client.delete(
        f"{base}/{quote('报告 (2).pdf', safe='')}", headers={"X-Meeting-Service": "1"}
    )
    assert deleted.status_code == 200, deleted.text
    deleted_body = deleted.json()
    assert deleted_body["material"] == {"name": "报告 (2).pdf"}

    missing = client.get(f"{base}/{quote('报告 (2).pdf', safe='')}")
    assert missing.status_code == 404
    missing_body = missing.json()
    assert missing_body["ok"] is False
    assert missing_body["code"] == "material_missing"
    assert missing_body["detail"] == {"name": "报告 (2).pdf"}

    emit(
        {
            "materials/upload-ok.json": {
                "method": "POST",
                "name": "报告.pdf",
                "status": first.status_code,
                "body": normalize(first_body),
            },
            "materials/after-upload.json": normalize(listing),
            "materials/delete-ok.json": {
                "method": "DELETE",
                "name": "报告 (2).pdf",
                "status": deleted.status_code,
                "body": normalize(deleted_body),
            },
            "materials/missing.json": {
                "method": "GET",
                "name": "报告 (2).pdf",
                "status": missing.status_code,
                "body": normalize(missing_body),
            },
        },
        regen=contract_regen,
    )


# ---------------------------------------------------------------------------
# SSE 帧样本（真服务子进程：dev 替身 + module:Class 假转写后端）
# ---------------------------------------------------------------------------


def _read_frames_until(client, names: set[str], *, timeout: float) -> dict[str, dict]:
    """读到 `names` 里每一个 event 名都到手为止，返回 `{event 名: 帧}`。

    **不依赖帧的到达次序**（审查问题：读帧顺序对 `revision` 有隐式时序依赖）。`levels` 是
    高频通道，它在 `start` 之后随时会来，谁先谁后由服务侧的广播节奏决定；按次序逐个
    `read_until()` 会把「哪一帧先到」写进断言，读起来像在验契约、实际在验时序。
    `revision` 的确定性因此改由另一条**与次序无关**的判据钉住：每一帧都自报它落在哪一版，
    这里逐帧比对「帧自报的 revision」与「帧内容里的 revision」必须一致（见调用点）。
    """
    import time as _time

    wanted = set(names)
    found: dict[str, dict] = {}
    deadline = _time.monotonic() + float(timeout)
    while wanted:
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"{timeout}s 内没读齐这些 event：{sorted(wanted)}（已到手 {sorted(found)}）"
            )
        frame = client.read_frame(timeout=remaining)
        name = frame.get("event")
        if name in wanted:
            wanted.discard(name)
            found[name] = frame
    return found


def test_capture_sse_frames(tmp_path, service_process, meeting_env, contract_regen):
    """snapshot 首帧、state、levels、transcript 四个帧样本，全部出自真服务的 `/api/events`。

    读帧**不依赖到达次序**（`_read_frames_until()`）：四类帧都在同一次 `start` 之后的
    广播节奏里先后到达，断言只看内容。`revision` 的确定性由帧自己的两处自报钉住 ——
    `snapshot` / `state` 的 `id:` 等于它 `data.revision`，`levels` / `transcript` 不带
    `id:` 但带当前 `revision` 作参照，四帧因此落在同一版上，与它们谁先到无关。
    """
    stub_dir = tmp_path / "contract-transcriber-stub"
    selector = _contract.write_transcriber_stub(stub_dir)
    meeting_env.write_config({"capture.backend": "dev", "transcriber.backend": selector})
    proc = service_process(
        home=meeting_env.home,
        extra_env=meeting_env.env,
        pythonpath=os.pathsep.join([str(helpers.REPO_ROOT), str(stub_dir)]),
    )
    url = proc.wait_ready()
    assert post_action(
        url, "start", {"mode": "both", "dir": helpers.allocate_start_dir(meeting_env.out_root)}
    ).status_code == 200

    # 转写行与包络都落稳之后才连 SSE：首帧的 transcript.last_line_at 与包络长度因此确定
    wait_until(
        lambda: (lambda snap: snap if snap["transcript"]["last_line_at"] else None)(
            proc.snapshot()
        ),
        timeout=CAPTURE_POLL_TIMEOUT_SECONDS,
    )
    wait_until(
        lambda: (
            lambda snap: snap
            if all(
                len(item["envelope"]) == ENVELOPE_BAR_COUNT for item in snap["sources"]
            )
            else None
        )(proc.snapshot()),
        timeout=CAPTURE_POLL_TIMEOUT_SECONDS,
    )

    with SseClient(proc.base_url, timeout=SSE_READ_TIMEOUT_SECONDS) as client:
        first = client.read_event_frame(timeout=SSE_READ_TIMEOUT_SECONDS)
        assert first["event"] == "snapshot"
        assert first["id"] is not None
        assert int(first["id"]) == first["data"]["revision"]
        assert set(first["data"]) == TOP_LEVEL_KEYS
        assert first["data"]["top_state"] == "recording"
        assert [item["id"] for item in first["data"]["sources"]] == ["mic", "loopback"]

        # 三帧里 `levels` 与 `transcript` 的到达先后由服务侧的广播节奏决定，读齐即可；
        # `state` 帧单独在下面用 mute 触发（它是一次真动作的产物，不该混进这条等待里）。
        others = _read_frames_until(
            client, {"levels", "transcript"}, timeout=SSE_READ_TIMEOUT_SECONDS
        )

        levels = others["levels"]
        assert levels["id"] is None
        levels_payload = levels["data"]
        assert set(levels_payload) == {"revision", "frames"}
        assert set(levels_payload["frames"]) == {"mic", "loopback"}
        for item in levels_payload["frames"].values():
            assert set(item) == {"ts", "dbfs", "peak", "bar", "envelope"}
            assert item["ts"] is None  # levels 帧的 ts 恒为 null
            assert len(item["envelope"]) == ENVELOPE_BAR_COUNT
            assert item["bar"] == item["envelope"][-1]

        transcript = others["transcript"]
        assert transcript["id"] is None
        transcript_payload = transcript["data"]
        assert set(transcript_payload) == {"revision", "line"}
        assert set(transcript_payload["line"]) == TRANSCRIPT_LINE_KEYS
        assert transcript_payload["line"]["source"] == "mic"
        assert transcript_payload["line"]["kind"] == "line"
        assert transcript_payload["line"]["text"]

        # 两个增量通道不带 `id:`，`data.revision` 就是它们自报的落点：与首帧同一版
        # （start 之后没再提交过），这条判据与到达次序无关。
        assert levels_payload["revision"] == first["data"]["revision"]
        assert transcript_payload["revision"] == first["data"]["revision"]

        assert post_action(proc.base_url, "mute_session").status_code == 200
        state = client.read_until("state", timeout=SSE_READ_TIMEOUT_SECONDS)
        assert state["event"] == "state"
        assert state["id"] is not None
        assert int(state["id"]) == state["data"]["revision"]
        # mute 提交一次：state 帧落在首帧之后的那一版上。
        assert state["data"]["revision"] == first["data"]["revision"] + 1
        assert set(state["data"]) == TOP_LEVEL_KEYS

    emit(
        {
            "sse/first-snapshot.json": normalize_frame(first),
            "sse/levels.json": normalize_frame(levels),
            "sse/transcript.json": normalize_frame(transcript),
            "sse/state.json": normalize_frame(state),
        },
        regen=contract_regen,
    )
