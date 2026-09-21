"""§9-7（一路采不到 → 致命级告警，另一路与录音继续）的进程级用例（phase3 §3.8）。

一条用例：起 dev 真服务，**显式**把 `alert.silence_seconds` 调小（写进临时 home 的
`~/.config/meeting/config.json`，**不改代码默认值**）→ `start` →
`POST /api/actions {"action":"dev_inject","args":{"source":"loopback","fault":"silence",
"after_seconds":0}}` → 用 `wait_until()` 轮询 `/api/state`（超时 `ALERT_POLL_TIMEOUT_SECONDS`）
直到 `top_state == "alert"` → 断言 `alerts.critical.source == "loopback"`、级别为**致命级**、
`duration_seconds` 已达调小的阈值（秒数由 `alerts.py` 拿 `LevelFrame` 帧时间戳累计）→
**另一路** `sources` 里 `id == "mic"` 的 `health == "capturing"` 且 `level_dbfs` 有值 →
继续轮询：`session.elapsed_seconds` 继续增长、`session.frozen` 为假（**进 `alert` 不等于停录**）→
`POST stop` 能正常进 `ended`（**服务绝不因采集异常自动结束会话**，结束必须由我们触发）。
"""

from __future__ import annotations

import time

from tests._service import post_action, wait_until

# ---------------------------------------------------------------------------
# 本文件的具名常量（§3.8 末「测试侧常量」那张表；用例里不留裸秒数）
#
# **取值收口的授权说明（第七轮审查 m3，措辞在下游文件里一字不改地重复）**：
# 「以上取值由指挥者授权收口（原契约表的值见 phase3.md §3.8 末表；红线：单条用例 ≤5s）」
# —— 本文件里被这一条覆盖的常量是 `E2E_CASE_WALL_CLOCK_LIMIT_SECONDS` 与
# `TEST_SILENCE_SECONDS`；下面各自那一段说明写清了判据的形状与这两笔钱的来路。
# ---------------------------------------------------------------------------

#: 单条用例的墙钟上限（秒）：与 `tests/conftest.py` 的看门 hook
#: （`CASE_WALL_CLOCK_LIMIT_SECONDS`）同一条红线 —— 用户把「每条用例 ≤ 5 秒」定为硬要求，
#: 全局 5 秒看门取代了契约 §3.8 里原先那条 60 秒口径；本常量在用例正文里**真的被断言**
#: （`_finish_case()`），不再是一行只写在文件头部的声明。
E2E_CASE_WALL_CLOCK_LIMIT_SECONDS = 5.0


def _finish_case(started: float) -> None:
    """用例正文的**墙钟断言**：实测值不超过 `E2E_CASE_WALL_CLOCK_LIMIT_SECONDS`。"""
    spent = time.monotonic() - started
    assert spent <= E2E_CASE_WALL_CLOCK_LIMIT_SECONDS, (
        f"本用例正文墙钟 {spent:.2f}s 超过上限 {E2E_CASE_WALL_CLOCK_LIMIT_SECONDS:.1f}s"
    )


#: 阈值调小之后，告警出现的轮询超时（秒）
ALERT_POLL_TIMEOUT_SECONDS = 20

#: 本用例把静默阈值**显式调小**到这个秒数（写进 config.json；代码默认值仍是 180）
#:
#: **这是指挥者授权的收口（契约表 3 → 实现 1，判据形状不变、代码默认值未动）**：契约 §3.8 末
#: 「测试侧常量」那张表给 `TEST_SILENCE_SECONDS` 写的取值是 `3`，用户的硬要求「单条用例
#: ≤ 5 秒」优先于方案文字里的等待时长 —— 把这一处恢复成 3 会让等告警的这条用例（它要等到
#: `duration_seconds` 达阈值）越过红线。本节点因此保持 `1`，**判据的形状一字未改**（仍然把
#: 阈值经 `config.json` 显式写小、仍然断 `duration_seconds >= TEST_SILENCE_SECONDS`），
#: `alerts.py` 的代码默认值（180 秒）也一个字没动。三处同值同注释的文件：
#: `tests/test_drain.py` / `tests/test_e2e_silence_alert.py` / `tests/test_e2e_multi_client.py`。
TEST_SILENCE_SECONDS = 1

#: 注入静默之后的观察窗口（秒）：这一段里录音该继续走
OBSERVE_SECONDS = 1.5

#: 致命级的取值（`alerts.py` 的分级口径）
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

    started = post_action(proc.base_url, "start")
    assert started.status_code == 200, started.text
    # 先读一次 `/api/state`：把本用例自己那条 HTTP 连接建好（本机新建一个 `httpx.Client`
    # 要 0.83 秒），否则这笔构造代价会落进「等告警」那段轮询的时间预算里
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
