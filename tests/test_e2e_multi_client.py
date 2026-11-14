"""多开看到同一份状态的进程级用例。"""

from __future__ import annotations

import time

import pytest

from tests import _helpers as helpers
from tests._helpers import read_text, wait_until
from tests._service import post_action

pytestmark = pytest.mark.slow

#: 单条用例的墙钟上限（秒）：与 `tests/conftest.py` 的看门 hook 同一条红线；
#: 本常量在用例正文里**真的被断言**（`_finish_case()`）。
E2E_CASE_WALL_CLOCK_LIMIT_SECONDS = 5.0


def _finish_case(started: float) -> None:
    """用例正文的**墙钟断言**：实测值不超过 `E2E_CASE_WALL_CLOCK_LIMIT_SECONDS`。"""
    spent = time.monotonic() - started
    assert spent <= E2E_CASE_WALL_CLOCK_LIMIT_SECONDS, (
        f"本用例正文墙钟 {spent:.2f}s 超过上限 {E2E_CASE_WALL_CLOCK_LIMIT_SECONDS:.1f}s"
    )


E2E_POLL_TIMEOUT_SECONDS = 30

SSE_READ_TIMEOUT_SECONDS = 20

#: 本用例里静默的告警（把 `alert.silence_seconds` **显式调小**之后才等得到；代码默认值仍是 180）。
#:
#: 取 1 秒：这一条要**连着**等两次告警（loopback 一次、静默期新起的 mic 一次）。实测把阈值压到
#: 0.5 秒时，dev 替身 0.1 秒一帧的累计会卡在门限下方一档（帧时间戳的间隔取值所致），两条用例
#: 双双等超时。
TEST_SILENCE_SECONDS = 1

#: 注入多久的静默（秒）：要长过 `TEST_SILENCE_SECONDS`，短到不拖长用例
TEST_SILENCE_FAULT_SECONDS = 3


def _warm_up_client(proc) -> None:
    """先读一次 `/api/state`，把用例自己那条 HTTP 连接建好再进入被测窗口。

    这笔构造代价若落在「等告警」那段轮询里，就会被算进告警阈值的时间预算。
    """
    proc.snapshot()


def _start(proc) -> None:
    workspace = helpers.allocate_start_dir(proc.sessions_root)
    started = post_action(proc.base_url, "start", {"dir": workspace})
    assert started.status_code == 200, started.text


def _mute(proc) -> None:
    muted = post_action(proc.base_url, "mute_session")
    assert muted.status_code == 200, muted.text


def _snapshot(proc) -> dict:
    return proc.snapshot()


def _read_state_event(client, *, timeout: float = SSE_READ_TIMEOUT_SECONDS) -> dict:
    """读到一条 `state` 事件（`snapshot` 那一类先跳过）。"""
    frame = client.read_until("state", timeout=timeout)
    return frame["data"]


def test_two_clients_share_one_revision(service_process, meeting_env, sse_client):
    """两条订阅者收到同一个 `revision` 的 `state`，静音对两边同样生效。"""
    case_started = time.monotonic()
    meeting_env.write_config(
        {"capture.backend": "dev", "alert.silence_seconds": TEST_SILENCE_SECONDS}
    )
    proc = service_process(
        home=meeting_env.home,
        log_dir=meeting_env.log_dir,
        extra_env=meeting_env.env,
    )
    proc.wait_ready()
    _start(proc)
    _warm_up_client(proc)

    first = sse_client(proc, timeout=SSE_READ_TIMEOUT_SECONDS)
    second = sse_client(proc, timeout=SSE_READ_TIMEOUT_SECONDS)
    for client in (first, second):
        frame = client.read_event_frame(timeout=SSE_READ_TIMEOUT_SECONDS)
        assert frame["event"] == "snapshot"

    # 造一条告警（一路静默），好让 `muted` 有对象可谈
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
    wait_until(
        lambda: _snapshot(proc)["top_state"] == "alert",
        timeout=E2E_POLL_TIMEOUT_SECONDS,
    )
    alert_frames = [_read_state_event(client) for client in (first, second)]
    assert alert_frames[0]["top_state"] == "alert"
    assert alert_frames[0]["revision"] == alert_frames[1]["revision"], alert_frames
    assert alert_frames[0]["alerts"]["critical"] is not None
    assert alert_frames[0]["alerts"]["critical"]["source"] == "loopback"

    _mute(proc)
    muted_frames = [_read_state_event(client) for client in (first, second)]
    assert muted_frames[0]["revision"] == muted_frames[1]["revision"], muted_frames
    for index, frame in enumerate(muted_frames):
        critical = frame["alerts"]["critical"]
        assert critical is not None, f"第 {index} 条订阅者收到的 critical 是空的：{frame}"
        assert critical["muted"] is True, f"第 {index} 条订阅者的 critical 没被静音：{critical}"
        for hint in frame["alerts"]["hints"]:
            assert hint["muted"] is True, f"第 {index} 条订阅者的提示级没被静音：{hint}"

    # 静音期间**新起的**告警同样 `muted == true`（静音是会话级的）
    again = post_action(
        proc.base_url,
        "dev_inject",
        {
            "source": "mic",
            "fault": "silence",
            "after_seconds": 0,
            "duration_seconds": TEST_SILENCE_FAULT_SECONDS,
        },
    )
    assert again.status_code == 200, again.text
    fresh = wait_until(
        lambda: (
            lambda snap: snap
            if snap["alerts"]["critical"] is not None
            and snap["alerts"]["critical"]["muted"] is True
            and snap["alerts"]["critical"]["source"] == "mic"
            else None
        )(_snapshot(proc)),
        timeout=E2E_POLL_TIMEOUT_SECONDS,
    )
    assert fresh["alerts"]["critical"]["muted"] is True
    _finish_case(case_started)


def test_session_mute_resets_on_next_session(service_process, meeting_env, sse_client):
    """下一场 `start` 会把会话级静音复位，而 `alarm.sound` 的持久化值原样保留。"""
    case_started = time.monotonic()
    meeting_env.write_config(
        {"capture.backend": "dev", "alert.silence_seconds": TEST_SILENCE_SECONDS}
    )
    proc = service_process(
        home=meeting_env.home,
        log_dir=meeting_env.log_dir,
        extra_env=meeting_env.env,
    )
    proc.wait_ready()
    _start(proc)
    _warm_up_client(proc)

    client = sse_client(proc, timeout=SSE_READ_TIMEOUT_SECONDS)
    client.read_event_frame(timeout=SSE_READ_TIMEOUT_SECONDS)

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
    wait_until(
        lambda: _snapshot(proc)["top_state"] == "alert", timeout=E2E_POLL_TIMEOUT_SECONDS
    )
    _mute(proc)
    muted = wait_until(
        lambda: (
            lambda snap: snap if snap["alerts"]["critical"]["muted"] is True else None
        )(_snapshot(proc)),
        timeout=E2E_POLL_TIMEOUT_SECONDS,
    )
    assert muted["alerts"]["critical"]["muted"] is True

    assert post_action(proc.base_url, "stop").status_code == 200
    wait_until(
        lambda: (_snapshot(proc)["ended"] or {}).get("phase") == "done",
        timeout=E2E_POLL_TIMEOUT_SECONDS,
    )
    _start(proc)

    fresh = wait_until(
        lambda: (
            lambda snap: snap
            if snap["top_state"] == "recording" and snap["session"] is not None
            and snap["session"]["id"] != muted["session"]["id"]
            else None
        )(_snapshot(proc)),
        timeout=E2E_POLL_TIMEOUT_SECONDS,
    )
    # 新一场起来之后转写报 `normal`：告警区里那几条提示级（空闲态本来就不发，
    # 这一场起来之后也不该有）是空数组。
    assert fresh["alerts"]["hints"] == []
    assert fresh["alerts"]["critical"] is None
    # `alarm.sound` 的持久化值未变（会话级静音不改它）
    assert fresh["settings"]["alarm_sound"] is True
    log_text = read_text(meeting_env.log_dir / "meeting.log")
    assert "本场已静音（会话级；下一场 start 自动复位" in log_text, (
        f"服务级 log 里该有那行「本场已静音」：{log_text[-800:]}"
    )
    _finish_case(case_started)
