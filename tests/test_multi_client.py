"""多开 / 关闭 / 刷新。"""

from __future__ import annotations

import time
import pytest


from tests import _helpers as helpers
from tests._helpers import wait_until
from tests._service import SseClient

pytestmark = pytest.mark.slow

MULTI_TIMEOUT_SECONDS = 15.0


@pytest.fixture()
def dev_service(tmp_path, service_process):
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {"web.dist_dir": str(helpers.missing_dist_dir(home))})
    proc = service_process(home=home, capture="dev", out_root=tmp_path / "out")
    proc.wait_ready()
    return proc


def post(proc, action: str, args: dict | None = None):
    body = {"action": action}
    if action == "start" and (args or {}).get("dir") is None:
        # `start` 必须显式带目标工作区目录：没给时由测试层预建一个（产品没有默认落点）
        args = {"dir": helpers.allocate_start_dir(proc.sessions_root), **(args or {})}
    if args is not None:
        body["args"] = args
    return helpers.local_post(
        f"{proc.base_url}/api/actions",
        json=body,
        headers={"Content-Type": "application/json", "X-Meeting-Service": "1"},
        timeout=10.0,
    )


def snapshot_of(proc) -> dict:
    return helpers.local_get(f"{proc.base_url}/api/state", timeout=5.0).json()


def open_pair(proc) -> tuple[SseClient, SseClient]:
    first = SseClient(proc.base_url)
    second = SseClient(proc.base_url)
    for client in (first, second):
        frame = client.read_event_frame(timeout=MULTI_TIMEOUT_SECONDS)
        assert frame["event"] == "snapshot", frame["raw"]
    return first, second


def next_state(client: SseClient, *, after: int | None = None) -> dict:
    """读到一条 `state` 事件（`after` 给定时读到 revision 大于它的那一条）。"""
    deadline = time.monotonic() + MULTI_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("等不到下一条 state 事件")
        frame = client.read_event_frame(timeout=remaining)
        if frame["event"] != "state":
            continue
        if after is None or frame["data"]["revision"] > after:
            return frame


def test_mute_session_reaches_both_clients_with_one_revision(dev_service_alert):
    """① 静音期间两个订阅者拿到**同一个 `revision`**，且 `alerts.*.muted` 一致。"""
    proc = dev_service_alert
    first, second = open_pair(proc)
    try:
        assert post(proc, "mute_session").status_code == 200
        frame_a = next_state(first)
        frame_b = next_state(second)

        assert frame_a["data"]["revision"] == frame_b["data"]["revision"]
        assert frame_a["data"]["alerts"] == frame_b["data"]["alerts"]
        critical = frame_a["data"]["alerts"]["critical"]
        assert critical is not None and critical["muted"] is True
        for hint in frame_a["data"]["alerts"]["hints"]:
            assert hint["muted"] is True
    finally:
        first.close()
        second.close()


@pytest.fixture()
def dev_service_alert(tmp_path, service_process):
    """一个正在告警的会话（阈值写小、把 loopback 弄成静默）。"""
    home = tmp_path / "home-alert"
    home.mkdir()
    helpers.write_config_json(
        home,
        {"alert.silence_seconds": 1, "alert.recover_seconds": 30,
         "web.dist_dir": str(helpers.missing_dist_dir(home))},
    )
    proc = service_process(home=home, capture="dev", out_root=tmp_path / "out-alert")
    proc.wait_ready()
    assert post(proc, "start").status_code == 200
    assert post(
        proc,
        "dev_inject",
        {"source": "loopback", "fault": "silence",
         "after_seconds": 0.0, "duration_seconds": 120.0},
    ).status_code == 200
    wait_until(lambda: snapshot_of(proc)["top_state"] == "alert", what="进 alert")
    return proc


def test_revision_sequences_match_across_clients(dev_service):
    """② 连续几次状态变更后两个客户端的 revision 序列**逐项相同**。"""
    proc = dev_service
    assert post(proc, "start").status_code == 200
    first, second = open_pair(proc)
    try:
        sequence_a: list[int] = []
        sequence_b: list[int] = []
        for action in ("mute_session", "mute_session", "mute_session"):
            assert post(proc, action).status_code == 200
            sequence_a.append(next_state(first)["data"]["revision"])
            sequence_b.append(next_state(second)["data"]["revision"])
        assert sequence_a == sequence_b
        assert sequence_a == sorted(sequence_a) and len(set(sequence_a)) == len(sequence_a)
    finally:
        first.close()
        second.close()


def test_disconnecting_one_subscriber_leaves_the_other_and_the_session_running(dev_service):
    """③ 断开 A 不影响 B 与会话：B 继续收帧、时长继续走、**没有发生任何状态变更**。"""
    proc = dev_service
    assert post(proc, "start").status_code == 200
    first, second = open_pair(proc)
    try:
        before = snapshot_of(proc)
        first.close()

        # B 照常收帧（levels 是 4Hz 的高频通道）
        frame = second.read_until("levels", timeout=MULTI_TIMEOUT_SECONDS)
        assert frame["parsed"] is True

        grown = wait_until(            lambda: (lambda s: s if (
                s["session"]["elapsed_seconds"] > before["session"]["elapsed_seconds"] + 0.5
            ) else None)(snapshot_of(proc)),
            what="时长继续增长",
        )
        assert grown["revision"] == before["revision"]
        assert grown["top_state"] == "recording"
        assert grown["session"]["frozen"] is False
    finally:
        second.close()

    assert snapshot_of(proc)["top_state"] == "recording"
    with SseClient(proc.base_url) as late:
        frame = late.read_event_frame(timeout=MULTI_TIMEOUT_SECONDS)
    assert frame["event"] == "snapshot"
    assert frame["data"]["top_state"] == "recording"
    assert frame["data"]["session"] is not None


def test_reconnect_takes_a_fresh_snapshot_without_replaying(dev_service):
    """④ 断开再连：第一帧是 `snapshot`、内容是当前状态、**不重放**断开期间的事件。"""
    proc = dev_service
    assert post(proc, "start").status_code == 200
    with SseClient(proc.base_url) as first:
        assert first.read_event_frame(timeout=MULTI_TIMEOUT_SECONDS)["event"] == "snapshot"
    assert post(proc, "mute_session").status_code == 200
    muted_revision = snapshot_of(proc)["revision"]

    with SseClient(proc.base_url) as again:
        frame = again.read_event_frame(timeout=MULTI_TIMEOUT_SECONDS)
    assert frame["event"] == "snapshot", "重连后仍然先发 snapshot"
    assert frame["data"]["revision"] >= muted_revision
    # 重连之后的内容就是当前状态（静音已经在里面），断开期间那一条 `state` 不被重放
    assert frame["data"]["alerts"]["critical"] is None
    assert frame["data"]["top_state"] == "recording"


def test_debug_page_is_static_and_reachable_without_a_build(dev_service):
    """⑤ `/debug` 200 + HTML + body 含 `EventSource` 与 `/api/events`（产物缺失时也照旧）。"""
    proc = dev_service
    response = helpers.local_get(f"{proc.base_url}/debug", timeout=5.0)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "EventSource" in body
    assert "/api/events" in body
    assert "/api/actions" in body
    assert "/api/heartbeat" in body
    assert "X-Meeting-Service" in body
    assert "骨架自检页" in body

    # 产物目录指向不存在的地方：`/` 是说明页，`/debug` 与 `/api/*` 照常
    root = helpers.local_get(f"{proc.base_url}/", timeout=5.0)
    assert root.status_code == 200
    assert "npm run build" in root.text
    assert helpers.local_get(f"{proc.base_url}/api/state", timeout=5.0).status_code == 200
