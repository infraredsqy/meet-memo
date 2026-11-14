"""关掉全部页面（无头环境里由「断开全部 SSE 订阅者」**等价表达**）录音照跑；停录之后
重连首帧与紧接着一次 `GET /api/state` 的 JSON **逐字段原样相等**。

**比对时点为什么在停录之后**：录音期那几处「读的时刻天然不一样」的
活字段（本场秒数 / 30 秒包络数组 / 两路瞬时电平）在 `ended` 冻结之后全部静止 —— 秒数由
`session.frozen` 定住，包络与电平只在收到电平帧时变（`meeting/state.py` 的 `push_level()`），
采集一停就没有新帧。于是两份读数可以**逐字段**对上，不必先摘字段再比：这条断言因此与契约
原文一样强，没有任何归一化在后面弱化它。诊断用的逐字段差异由 `_diff()` 报出。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from tests import _helpers as helpers
from tests._helpers import wait_until
from tests._service import post_action

pytestmark = pytest.mark.slow

#: 单条用例的墙钟上限（秒），与 `tests/conftest.py` 的看门 hook 同值。
E2E_CASE_WALL_CLOCK_LIMIT_SECONDS = 5.0

#: 观察窗口的判据：断开之后 `elapsed_seconds` 还在往前走、且这一段里 `revision` 一次都没动。
E2E_ELAPSED_GROWTH_SECONDS = 1.0

E2E_POLL_TIMEOUT_SECONDS = 30

SSE_READ_TIMEOUT_SECONDS = 20


def _finish_case(started: float) -> None:
    """用例正文的**墙钟断言**：实测值不超过 `E2E_CASE_WALL_CLOCK_LIMIT_SECONDS`。

    与 `tests/conftest.py` 的看门 hook 是同一个上限、同一条红线（那条看门量的是
    setup + call + teardown 三段合计，这里量的是用例正文这一段）。
    """
    spent = time.monotonic() - started
    assert spent <= E2E_CASE_WALL_CLOCK_LIMIT_SECONDS, (
        f"本用例正文墙钟 {spent:.2f}s 超过上限 {E2E_CASE_WALL_CLOCK_LIMIT_SECONDS:.1f}s"
    )


def _start(proc) -> None:
    workspace = helpers.allocate_start_dir(proc.sessions_root)
    started = post_action(proc.base_url, "start", {"dir": workspace})
    assert started.status_code == 200, started.text


def test_recording_continues_after_all_clients_disconnect(service_process, meeting_env, sse_client):
    """关掉全部订阅者之后录音照跑，重连第一帧就是当前快照。"""
    started = time.monotonic()
    # **采集后端由本用例自己的 `config.json` 表达**：env 里那一项
    # （`MEETING_CAPTURE`）一个都不写 —— 它的优先级高于配置文件，留着就会把这条键静默压住。
    meeting_env.write_config({"capture.backend": "dev"})
    proc = service_process(
        home=meeting_env.home,
        log_dir=meeting_env.log_dir,
        extra_env=meeting_env.env,
    )
    proc.wait_ready()
    _start(proc)
    # 先读一次 `/api/state`：把本用例自己那条 HTTP 连接建好，
    # 否则这笔构造代价会落进下面那段观察窗口里
    proc.snapshot()

    first = sse_client(proc, timeout=SSE_READ_TIMEOUT_SECONDS)
    second = sse_client(proc, timeout=SSE_READ_TIMEOUT_SECONDS)
    # 两条订阅各自先拿到首帧 `snapshot`（连上就有状态，不用等下一次变更）
    for client in (first, second):
        frame = client.read_event_frame(timeout=SSE_READ_TIMEOUT_SECONDS)
        assert frame["event"] == "snapshot", frame
        assert frame["data"]["top_state"] == "recording"

    before = proc.snapshot()
    assert before["top_state"] == "recording"
    elapsed_at_disconnect = before["session"]["elapsed_seconds"]
    revision_at_disconnect = before["revision"]

    # 「关掉全部页面」= 断开全部 SSE 订阅者（无头环境里这两件事等价）
    first.close()
    second.close()

    # **重连那条订阅现在就建好**：建好之后它只是
    # 挂在流上等首帧，这笔构造代价因此落在观察窗口之外 —— 窗口里量到的是
    # 「断开之后录音还在往前走」这件事本身。
    reconnected = sse_client(proc, timeout=SSE_READ_TIMEOUT_SECONDS)

    def grew():
        snapshot = proc.snapshot()
        return snapshot if snapshot["session"]["elapsed_seconds"] >= (
            elapsed_at_disconnect + E2E_ELAPSED_GROWTH_SECONDS
        ) else None

    after = wait_until(grew, timeout=E2E_POLL_TIMEOUT_SECONDS)

    assert after["revision"] == revision_at_disconnect, (
        f"断开期间窗口里出现了状态变更：{revision_at_disconnect} -> {after['revision']}"
    )

    frame = reconnected.read_event_frame(timeout=SSE_READ_TIMEOUT_SECONDS)
    assert frame["event"] == "snapshot", frame
    assert frame["data"]["top_state"] == "recording"
    assert frame["data"]["session"]["elapsed_seconds"] > elapsed_at_disconnect

    assert post_action(proc.base_url, "stop").status_code == 200
    stopped = wait_until(
        lambda: (
            lambda snap: snap
            if (snap.get("ended") or {}).get("phase") == "done"
            else None
        )(proc.snapshot()),
        timeout=E2E_POLL_TIMEOUT_SECONDS,
    )
    assert stopped["top_state"] == "ended"
    assert stopped["session"]["frozen"] is True

    after_stop = sse_client(proc, timeout=SSE_READ_TIMEOUT_SECONDS)
    ended_frame = after_stop.read_event_frame(timeout=SSE_READ_TIMEOUT_SECONDS)
    assert ended_frame["event"] == "snapshot", ended_frame
    assert ended_frame["data"]["top_state"] == "ended"
    fresh = proc.snapshot()
    assert ended_frame["data"] == fresh, (
        "停录之后重连首帧与紧接着的 /api/state 该是同一份状态（逐字段）："
        f"\n帧：{_diff(ended_frame['data'], fresh)}\n状态：{_diff(fresh, ended_frame['data'])}"
    )
    _finish_case(started)


def _diff(left: Any, right: Any, *, path: str = "") -> dict[str, Any]:
    """逐字段找出两边的不同（**只用于断言失败时报出差异**，判据仍是上面的整份相等）。

    递归着走进字典与等长列表，把「哪条路径上、左边是什么、右边是什么」列出来。比对本身不
    摘任何字段：**字段一个都不少**，这也是本用例把比对时点挪到停录之后换来的东西。
    """
    out: dict[str, Any] = {}
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            out.update(_diff(left.get(key), right.get(key), path=f"{path}.{key}"))
        return out
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        for index, (one, other) in enumerate(zip(left, right)):
            out.update(_diff(one, other, path=f"{path}[{index}]"))
        return out
    if left != right:
        out[path or "<根>"] = (left, right)
    return out
