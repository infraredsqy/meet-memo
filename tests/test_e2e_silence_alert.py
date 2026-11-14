"""一路采不到 → 致命级告警，另一路与录音继续。"""

from __future__ import annotations

import time

import pytest
from tests import _helpers as helpers
from tests._helpers import wait_until
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


ALERT_POLL_TIMEOUT_SECONDS = 20

#: 本用例把静默阈值**显式调小**到这个秒数（写进 config.json；代码默认值仍是 180）
TEST_SILENCE_SECONDS = 1

#: 注入静默之后的观察窗口（秒）：这一段里录音该继续走
OBSERVE_SECONDS = 1.5

CRITICAL_LEVEL = "critical"


def _source(snapshot: dict, source_id: str) -> dict:
    for item in snapshot["sources"]:
        if item["id"] == source_id:
            return item
    raise AssertionError(f"快照里没有 {source_id} 这一路：{snapshot['sources']}")


def test_silence_injection_raises_critical_alert(service_process, meeting_env):
    """一路静默 → 致命级告警；另一路与录音都继续，结束必须由我们触发。"""
    case_started = time.monotonic()
    meeting_env.write_config(
        {"capture.backend": "dev", "alert.silence_seconds": TEST_SILENCE_SECONDS}
    )
    proc = service_process(
        home=meeting_env.home,
        extra_env=meeting_env.env,
    )
    proc.wait_ready()

    workspace = helpers.allocate_start_dir(meeting_env.out_root)
    started = post_action(proc.base_url, "start", {"dir": workspace})
    assert started.status_code == 200, started.text
    # 先读一次 `/api/state`：把本用例自己那条 HTTP 连接建好，
    # 否则这笔构造代价会落进「等告警」那段轮询的时间预算里
    proc.snapshot()

    injected = post_action(
        proc.base_url,
        "dev_inject",
        {"source": "loopback", "fault": "silence", "after_seconds": 0},
    )
    assert injected.status_code == 200, injected.text

    alerting = wait_until(
        lambda: (lambda snap: snap if snap["top_state"] == "alert" else None)(proc.snapshot()),
        timeout=ALERT_POLL_TIMEOUT_SECONDS,
    )

    critical = alerting["alerts"]["critical"]
    assert critical is not None, alerting["alerts"]
    assert critical["source"] == "loopback", critical
    assert critical["level"] == CRITICAL_LEVEL, critical
    assert critical["duration_seconds"] >= TEST_SILENCE_SECONDS, critical

    # 另一路照常采着（采集侧的异常不牵连另一路）
    mic = _source(alerting, "mic")
    assert mic["health"] == "capturing", mic
    assert mic["level_dbfs"] is not None, mic

    # 进 `alert` 不等于停录：本场已录秒数继续走、时长没被冻结
    elapsed_at_alert = alerting["session"]["elapsed_seconds"]
    assert alerting["session"]["frozen"] is False

    def grew():
        snapshot = proc.snapshot()
        return snapshot if snapshot["session"]["elapsed_seconds"] >= (
            elapsed_at_alert + OBSERVE_SECONDS
        ) else None

    later = wait_until(grew, timeout=ALERT_POLL_TIMEOUT_SECONDS)
    assert later["session"]["frozen"] is False
    assert later["top_state"] in ("alert", "recording"), later["top_state"]
    assert later["alerts"]["critical"] is not None

    # 服务绝不因采集异常自动结束会话：结束必须由我们触发
    assert proc.poll() is None
    stopped = post_action(proc.base_url, "stop")
    assert stopped.status_code == 200, stopped.text
    ended = wait_until(
        lambda: (lambda snap: snap if snap["ended"] is not None else None)(proc.snapshot()),
        timeout=ALERT_POLL_TIMEOUT_SECONDS,
    )
    assert ended["top_state"] == "ended"
    assert (ended["ended"] or {}).get("phase") in ("draining", "done")
    _finish_case(case_started)
