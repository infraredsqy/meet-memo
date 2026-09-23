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
    alert / ended·done / ended·draining 的快照、两个心跳样本、成功动作、七类失败动作、
    resync 帧（`hub.py` 的真实广播路径：真订阅者队列灌满 512 后的下一次广播走 `resync()`）；
  - **真服务子进程**：SSE 帧样本（dev 采集替身 + `module:Class` 假转写后端，见
    `tests/_contract.write_transcriber_stub()`）；idle+notice.restart_unfinished
    （recording 中 `kill()` 留现场、再重启的真实启动扫描路径）。

结构断言（在掩码前的原始载荷上做）：快照顶层十键、两路次序、录音态包络恰 15 根、
`bar == envelope[-1]`、levels 帧 `ts` 恒 null、resync 帧顶层额外 `resync` / `dropped` 两键。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from meeting import config as config_module
from meeting.service.hub import SUBSCRIBER_QUEUE_SIZE
from meeting.state import ENVELOPE_BAR_COUNT, REVISION_START
from tests import _contract
from tests import _drain_fakes as fakes
from tests import _helpers as helpers
from tests._contract import emit, normalize, normalize_frame
from tests._service import SseClient, parse_sse_frame, post_action, wait_until

#: 结构断言与心跳样本共用的键全集（与 `Snapshot.to_dict()` 的产出逐字对应）
TOP_LEVEL_KEYS = {
    "revision", "service", "top_state", "session", "sources",
    "alerts", "transcript", "ended", "settings", "notice",
}
SOURCE_KEYS = {
    "id", "label", "device_name", "health", "lost_since",
    "restored", "level_dbfs", "peak_dbfs", "envelope",
}
HEARTBEAT_KEYS = {
    "service", "server_time", "revision", "top_state",
    "recording_alive", "seconds_since_last_tick",
}
TRANSCRIPT_LINE_KEYS = {"ts", "source", "text", "kind"}

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
        values = dict(values)
        values.setdefault("web.dist_dir", str(helpers.missing_dist_dir(home)))
        values.setdefault("output.root", str(tmp_path / f"out{made}"))
        return stack.enter_context(helpers.app_client(home, values=values))

    try:
        yield make
    finally:
        stack.close()


def _act(client, action: str, args: dict | None = None):
    """进程内打一次动作（两个必需请求头由这里带全）。"""
    body: dict = {"action": action}
    if args is not None:
        body["args"] = args
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
    """idle（无 notice）快照 + 空闲心跳 + 空闲态的拒绝动作（dev 替身：两路 ready）。"""
    client = make_client({"capture.backend": "dev"})

    snapshot = client.get("/api/state").json()
    _assert_snapshot_shape(snapshot, top_state="idle", envelope_filled=False)
    assert snapshot["session"] is None
    assert snapshot["ended"] is None
    assert snapshot["notice"] is None
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
            "actions/fail-capture-unavailable.json": _action_record(capture_down, "start", {}),
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
            "actions/start-ok.json": _action_record(started, "start", {"mode": "both"}),
            "snapshot/recording.json": normalize(snapshot),
            "heartbeat/recording.json": normalize(heartbeat),
            "actions/fail-already-recording.json": _action_record(
                duplicate, "start", {"mode": "both"}
            ),
            "actions/mute-session-ok.json": _action_record(muted, "mute_session", {}),
            "sse/resync-snapshot.json": normalize_frame(resync),
        },
        regen=contract_regen,
    )


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
    """ended·done 快照 + stop 成功体（收尾四步走完的终态）。

    `post.hook` 显式取 `null`：会后钩子（`7c6952e5` phase2 的 `PostProcessHook`）落地之后，
    产品缺省 `auto` 会让第 4 步真的起一条后台线程跑会后链、往场次目录里多落四五份产物，
    于是 `ended-done` 这份 golden 样本里第 4 步的状态与采集到的目录内容都不再稳定。
    这一步的「有实现就 done」另有 `tests/test_post_hook.py` 与 `tests/test_drain.py` 盯着。
    """
    client = make_client({"capture.backend": "dev", "post.hook": "null"})

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
        "skipped", "skipped", "done", "skipped",
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
    """ended·draining 停留档快照 + draining 拒绝（进程内换抛错的假 post 钩子）。"""
    home = tmp_path / "home-drain"
    home.mkdir(parents=True, exist_ok=True)
    plugin = fakes.write_failing_post_hook(home, capture="dev")
    monkeypatch.syspath_prepend(str(plugin.directory))
    values = dict(plugin.config)
    values.setdefault("web.dist_dir", str(helpers.missing_dist_dir(home)))
    values.setdefault("output.root", str(tmp_path / "out-drain"))
    with helpers.app_client(home, values=values) as client:
        assert _act(client, "start", {"mode": "both"}).status_code == 200
        _wait_envelope_full(client)
        assert _act(client, "stop").status_code == 200

        def settled(snap):
            steps = (snap.get("ended") or {}).get("steps") or []
            if not steps or any(
                step["status"] in ("waiting", "running") for step in steps
            ):
                return False
            return snap["top_state"] == "ended"

        snapshot = _wait_snapshot(client, settled)
        _assert_snapshot_shape(snapshot, top_state="ended", envelope_filled=True)
        assert snapshot["ended"]["phase"] == "draining"
        assert snapshot["ended"]["error"]
        post_step = snapshot["ended"]["steps"][-1]
        assert post_step["id"] == "post"
        assert post_step["status"] == "failed"
        assert "假会后钩子" in post_step["error"]
        # 失败停留档保留 stopped 现场
        assert all(item["health"] == "stopped" for item in snapshot["sources"])

        refused = _act(client, "start", {"mode": "both"})
        assert refused.status_code == 409
        refused_body = refused.json()
        assert refused_body["code"] == "draining"
        detail = refused_body["detail"]
        assert set(detail) == {"session_id", "dir", "failed_steps", "log_path"}
        assert detail["failed_steps"] == [
            {"id": "post", "error": post_step["error"]}
        ]

    emit(
        {
            "snapshot/ended-draining.json": normalize(snapshot),
            "actions/fail-draining.json": _action_record(refused, "start", {"mode": "both"}),
        },
        regen=contract_regen,
    )


# ---------------------------------------------------------------------------
# idle + notice.restart_unfinished（真服务子进程：杀进程留现场再重启）
# ---------------------------------------------------------------------------


def test_capture_idle_notice_restart(service_process, meeting_env, contract_regen):
    """recording 中被杀 → 同一输出根重启 → 空闲态挂 notice.restart_unfinished。"""
    meeting_env.write_config({"capture.backend": "dev"})
    proc = service_process(home=meeting_env.home, extra_env=meeting_env.env)
    url = proc.wait_ready()
    assert post_action(url, "start", {"mode": "both"}).status_code == 200
    proc.kill()

    restarted = service_process(home=meeting_env.home, extra_env=meeting_env.env)
    restarted.wait_ready()
    snapshot = restarted.snapshot()
    assert snapshot["top_state"] == "idle"
    assert snapshot["revision"] == REVISION_START
    assert snapshot["session"] is None
    assert snapshot["ended"] is None
    notice = snapshot["notice"]
    assert notice is not None
    assert notice["kind"] == "restart_unfinished"
    assert notice["text"].startswith("服务重启过。")
    assert notice["session_dir"]
    assert notice["log_path"] == snapshot["service"]["log_path"]

    emit({"snapshot/idle-notice.json": normalize(snapshot)}, regen=contract_regen)


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
    assert post_action(url, "start", {"mode": "both"}).status_code == 200

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
