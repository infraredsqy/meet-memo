
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


