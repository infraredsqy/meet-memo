"""`tests/test_sse.py` —— `GET /api/events` 与三通道（phase2 §5.1 第七行；§3.8）。

覆盖（逐条对 §5.1 的 ①–⑥）：

  ① **首帧是 `snapshot`**：连上后第一个 `event:` 名必须是 `snapshot`、`data` 带完整状态字段；
     录音中建连时首帧 `top_state == "recording"`（**不经过 `idle`**）；同一时刻
     `GET /api/state` 的关键字段与首帧一致（`test_api_state_matches_first_frame`，
     函数名与契约逐字一致）。
     首帧时序的另一半（取快照与注册订阅之间没有 `await`）按源码文本自查（辅助证据）。
  ② `state` 是**全量**快照、`id:` == `data.revision`。
  ③ `levels` 4Hz 量级且**不占 revision**，`data` 形状 `{revision, frames: {mic, loopback}}`，
     每个 source 带 `ts` / `dbfs` / `peak` / `bar` / `envelope`。
  ④ `transcript` 形状 `{revision, line: {...}}`（骨架阶段恒 0 行；无转写后端时不报错、不断流）。
  ⑤ 开头出现 `retry: 2000`；保活帧是仅一行注释 `: ping`（不伪造 `event` / `data`）。
  ⑥ **慢消费者重同步**：一个只连不读的订阅者把队列灌到 512，再触发若干次状态变更 →
     它最终收到一份带 `resync: true` 与 `dropped: N ≥ 1` 的 `snapshot`；
     **另一个正常订阅者不受影响**、继续依次收到每次 `state`、revision **无跳号**。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx
import pytest

from tests import _helpers as helpers
from meeting.service.hub import (
    PING_INTERVAL_SECONDS,
    SSE_RETRY_MILLISECONDS,
    SUBSCRIBER_QUEUE_SIZE,
    EventHub,
    encode_ping,
)
from tests._service import SseClient, parse_sse_frame

#: 用例的显式等待与读帧上限（秒）
SSE_TIMEOUT_SECONDS = 15.0
#: 4Hz 抽帧的观测窗口与期望帧数
LEVELS_OBSERVE_SECONDS = 1.0
LEVELS_MIN_FRAMES = 2


# ---------------------------------------------------------------------------
# 真服务：SSE 的基本行为
# ---------------------------------------------------------------------------


@pytest.fixture()
def dev_service(tmp_path, service_process):
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {})
    proc = service_process(home=home, capture="dev", out_root=tmp_path / "out")
    proc.wait_ready()
    return proc


def post(proc, action: str, args: dict | None = None):
    body = {"action": action}
    if args is not None:
        body["args"] = args
    return helpers.local_post(
        f"{proc.base_url}/api/actions",
        json=body,
        headers={"Content-Type": "application/json", "X-Meeting-Service": "1"},
        timeout=10.0,
    )


def test_first_frame_is_snapshot(dev_service):
    """① 首帧必须是 `snapshot`；录音中建连的首帧 `top_state` 就是 `recording`。"""
    proc = dev_service
    assert post(proc, "start").status_code == 200
    before = helpers.local_get(f"{proc.base_url}/api/state", timeout=5.0).json()

    with SseClient(proc.base_url) as client:
        first = client.read_event_frame(timeout=SSE_TIMEOUT_SECONDS)

    assert first["event"] == "snapshot", f"第一条事件帧必须是 snapshot：{first['raw']!r}"
    assert first["parsed"] is True
    assert first["data"]["revision"] >= before["revision"]
    # 录音中建连：首帧就是真实态，**不经过 idle**
    assert first["data"]["top_state"] == "recording"
    assert set(first["data"]) == {
        "revision", "service", "top_state", "session", "sources",
        "alerts", "transcript", "ended", "settings", "notice",
    }
    assert [item["id"] for item in first["data"]["sources"]] == ["mic", "loopback"]


def test_first_frame_snapshot_takes_the_snapshot_before_registering():
    """①（另一半，辅助证据）`stream()` 里取快照与注册订阅之间**不出现 `await`**。"""
    import ast
    import inspect

    from meeting.service.hub import EventHub as Hub

    source = inspect.getsource(Hub.stream)
    tree = ast.parse(_dedent(source))
    func = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef))
    # 丢掉 docstring，只在真正的语句里找「取快照」与「注册订阅」这两句
    statements = [node for node in func.body if not isinstance(node, ast.Expr)]
    texts = [_dedent(ast.unparse(node)) for node in statements]
    snapshot_at = next(index for index, text in enumerate(texts) if "provider()" in text)
    register_at = next(index for index, text in enumerate(texts) if "self.register()" in text)
    assert snapshot_at < register_at, "先取快照、再注册订阅"
    between = texts[snapshot_at:register_at]
    assert all("await" not in text for text in between), f"两句之间不许出现 await：{between}"
    for node in statements[snapshot_at:register_at]:
        assert not any(isinstance(item, ast.Await) for item in ast.walk(node))


def _dedent(source: str) -> str:
    import textwrap

    return textwrap.dedent(source)


def test_api_state_matches_first_frame(dev_service):
    """① 同一时刻 `GET /api/state` 的关键字段与 SSE 首帧一致（§9-3，方案行 373）。"""
    proc = dev_service
    assert post(proc, "start").status_code == 200

    with SseClient(proc.base_url) as client:
        first = client.read_event_frame(timeout=SSE_TIMEOUT_SECONDS)
        # 两次读取之间不发任何动作请求
        again = helpers.local_get(f"{proc.base_url}/api/state", timeout=5.0).json()

    frame = first["data"]
    for key in ("revision", "top_state", "ended"):
        assert frame[key] == again[key], f"这一项两边必须一致：{key}"
    # `session` 的 `elapsed_seconds` 每秒都在走，可比的是它的身份与冻结档
    for key in ("id", "dir", "mode", "started_at", "frozen"):
        assert frame["session"][key] == again["session"][key], f"session.{key} 两边必须一致"
    # `sources` 里电平与包络每 100ms 都在动，可比的是**形状与健康度**这几项
    assert [item["id"] for item in frame["sources"]] == [
        item["id"] for item in again["sources"]
    ]
    assert [item["health"] for item in frame["sources"]] == [
        item["health"] for item in again["sources"]
    ]
    assert frame["service"]["port"] == again["service"]["port"]


def test_state_events_are_full_snapshots_with_matching_ids(dev_service):
    """② `state` 是全量快照，且帧级 `id:` == `data.revision`。"""
    proc = dev_service
    with SseClient(proc.base_url) as client:
        client.read_frame(timeout=SSE_TIMEOUT_SECONDS)  # snapshot
        assert post(proc, "start").status_code == 200
        frame = client.read_until("state", timeout=SSE_TIMEOUT_SECONDS)

    assert frame["data"]["top_state"] == "recording"
    assert set(frame["data"]) >= {"revision", "service", "top_state", "session", "sources"}
    assert frame["id"] is not None and int(frame["id"]) == frame["data"]["revision"]


def test_levels_channel_shape_and_rate(dev_service):
    """③ `levels` 4Hz 量级、不占 revision，`data` 形状与每 source 的五个字段都对。"""
    proc = dev_service
    assert post(proc, "start").status_code == 200

    with SseClient(proc.base_url) as client:
        assert client.read_event_frame(timeout=SSE_TIMEOUT_SECONDS)["event"] == "snapshot"
        level_frames = []
        deadline = time.monotonic() + LEVELS_OBSERVE_SECONDS
        while time.monotonic() < deadline:
            frame = client.read_frame(timeout=SSE_TIMEOUT_SECONDS)
            if frame["event"] == "levels":
                level_frames.append(frame)

    assert len(level_frames) >= LEVELS_MIN_FRAMES, f"1 秒里该有 4Hz 量级的 levels：{len(level_frames)}"
    payload = level_frames[-1]["data"]
    assert set(payload) == {"revision", "frames"}
    assert set(payload["frames"]) == {"mic", "loopback"}
    for source, item in payload["frames"].items():
        assert set(item) == {"ts", "dbfs", "peak", "bar", "envelope"}
        assert isinstance(item["envelope"], list)
        assert len(item["envelope"]) == 15
        # `bar` 与 `envelope` 的末项同值（同一算法、同一口径）
        assert item["bar"] == item["envelope"][-1]
    # `levels` 不带帧级 `id:`（只带当前 revision 作数据字段）
    assert level_frames[-1]["id"] is None

    # 不占 revision：录音期间 revision 只在状态变更时涨（levels 一条都不涨它）
    state = helpers.local_get(f"{proc.base_url}/api/state", timeout=5.0).json()
    assert payload["revision"] <= state["revision"]
    # 一帧 levels 不改变任何状态（顶态、场次都照旧）
    assert state["top_state"] == "recording"


def test_transcript_channel_shape_without_a_transcriber(dev_service):
    """④ `transcript` 形状 `{revision, line}`；没有转写后端时不报错、不断流。

    骨架阶段没有真转写后端，所以这一条直接对 hub 的广播出口断言形状；
    同时确认真服务在这条通道上**不断流**（连接照旧活着、后续还能读帧）。
    """
    from meeting.service.hub import encode_event

    payload = {"revision": 7, "line": {"ts": 1.5, "source": "mic", "text": "一行", "kind": "line"}}
    raw = encode_event("transcript", payload)
    frame = parse_sse_frame(raw.strip())
    assert frame["event"] == "transcript"
    assert frame["id"] is None, "`transcript` 不带帧级 id"
    assert frame["data"] == payload

    proc = dev_service
    assert post(proc, "start").status_code == 200
    with SseClient(proc.base_url) as client:
        first = client.read_event_frame(timeout=SSE_TIMEOUT_SECONDS)
        assert first["event"] == "snapshot"
        assert first["data"]["transcript"] == {
            "health": "normal", "last_line_at": None, "buffered_lines": 0, "failed_segments": 0,
        }
        # 没有转写后端也不断流：还读得到 levels
        assert client.read_until("levels", timeout=SSE_TIMEOUT_SECONDS)["parsed"] is True


def test_retry_and_ping_frames(dev_service):
    """⑤ 开头 `retry: 2000`；保活帧是仅一行注释 `: ping`（不伪造 `event` / `data`）。"""
    proc = dev_service
    with SseClient(proc.base_url) as client:
        first = client.read_event_frame(timeout=SSE_TIMEOUT_SECONDS)

    assert SSE_RETRY_MILLISECONDS == 2000
    assert "retry: 2000" in client.raw_text, client.raw_text[:400]
    assert PING_INTERVAL_SECONDS == 15.0
    assert encode_ping() == ": ping\n\n"
    ping = parse_sse_frame(": ping")
    assert ping["comment"] == "ping"
    assert ping["event"] is None and ping["data"] is None
    assert first["event"] == "snapshot"


def test_hub_resyncs_the_slow_subscriber_and_leaves_the_fast_one_alone():
    """⑥ 慢消费者重同步（进程内直测 hub）：灌满 512 条队列后 + 一次状态变更 →
    该订阅者收到带 `resync: true` / `dropped: N ≥ 1` 的 `snapshot`；
    另一个正常订阅者照旧依次收到每次 `state`、**revision 无跳号**。
    """
    revisions = {"value": 1}
    hub = EventHub(snapshot_provider=lambda: {"revision": revisions["value"], "top_state": "recording"})
    slow = hub.register()          # 只连不读的那一个
    fast = hub.register()          # 正常订阅者

    # 把慢订阅者的队列灌满（512 条），正常订阅者的队列每轮都取空
    for index in range(SUBSCRIBER_QUEUE_SIZE):
        revisions["value"] = index + 2
        hub.broadcast_state({"revision": revisions["value"], "top_state": "recording"})
        fast.take()
    assert slow.queue.qsize() == SUBSCRIBER_QUEUE_SIZE

    # 再触发一次状态变更：慢订阅者的队列已满 → 清空 + 补一份带 resync 的快照
    revisions["value"] += 1
    hub.broadcast_state({"revision": revisions["value"], "top_state": "recording"})
    fast_item = fast.take()
    assert fast_item is not None
    fast_frame = parse_sse_frame(fast_item[1])
    assert fast_frame["event"] == "state"
    assert fast_frame["data"]["revision"] == revisions["value"]

    slow_item = slow.take()
    assert slow_item is not None
    slow_frame = parse_sse_frame(slow_item[1])
    assert slow_frame["event"] == "snapshot"
    assert slow_frame["data"]["resync"] is True
    assert slow_frame["data"]["dropped"] >= 1

    # 正常订阅者不受影响：继续依次收到每次 state，revision 逐次 +1（无跳号）
    seen = []
    for step in range(5):
        revisions["value"] += 1
        hub.broadcast_state({"revision": revisions["value"], "top_state": "recording"})
        item = fast.take()
        assert item is not None, "正常订阅者不该被慢消费者牵连"
        seen.append(parse_sse_frame(item[1])["data"]["revision"])
    assert seen == list(range(revisions["value"] - 4, revisions["value"] + 1))
    assert len(slow.queue.queue) <= SUBSCRIBER_QUEUE_SIZE


def test_sse_connection_can_be_dropped_without_touching_the_session(dev_service):
    """断开一个订阅者**只做这一件事**：会话照跑、`elapsed_seconds` 继续走、状态不变。

    （慢消费者重同步那条口径由 `test_hub_resyncs_the_slow_subscriber_and_leaves_the_fast_one_alone`
    在进程内直测 hub；真服务这一侧只验「断开没有副作用」。）
    """
    proc = dev_service
    assert post(proc, "start").status_code == 200
    with SseClient(proc.base_url) as client:
        assert client.read_event_frame(timeout=SSE_TIMEOUT_SECONDS)["event"] == "snapshot"
        before = helpers.local_get(f"{proc.base_url}/api/state", timeout=5.0).json()

    after = helpers.local_get(f"{proc.base_url}/api/state", timeout=5.0).json()
    assert after["top_state"] == "recording"
    assert after["revision"] == before["revision"], "断开订阅者不该产生任何状态变更"
    assert after["session"]["frozen"] is False
    assert after["session"]["elapsed_seconds"] >= before["session"]["elapsed_seconds"]


def test_event_names_are_the_frozen_four():
    """通道名固定四个；`broadcast_snapshot()` 这个名字不存在（首帧与重同步收在 hub 内部）。"""
    from meeting.service import hub as hub_module

    assert hub_module.SSE_CHANNELS == ("snapshot", "state", "levels", "transcript")
    assert not hasattr(hub_module.EventHub, "broadcast_snapshot")
    for name in ("broadcast_state", "broadcast_levels", "broadcast_transcript", "resync"):
        assert hasattr(hub_module.EventHub, name), name


def test_unknown_event_paths_are_not_invented(dev_service):
    """`/api/events` 的应答头是 SSE 类型（不是 HTML、不是 JSON）。"""
    proc = dev_service
    # 复用探针的 client（本机新建一个 `httpx.Client` 要 0.83 秒，见 `helpers.local_get()`）；
    # 不设 client 级超时，这一次流式读只受下面 `get()` 自带的超时约束
    with helpers.local_client_for(f"{proc.base_url}/api/events").stream(
        "GET",
        f"{proc.base_url}/api/events",
        timeout=httpx.Timeout(10.0, read=10.0),
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
