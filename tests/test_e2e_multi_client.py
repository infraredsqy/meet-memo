"""§9-2（多开看到同一份状态）的进程级用例（phase3 §3.8）。

两条用例：

  - `test_two_clients_share_one_revision`：起 dev 真服务 → `start` → 两条独立 SSE 订阅者 →
    一次 `POST mute_session` → 两边收到的 `state` 事件**同一 `revision`** 且 `alerts` 里每条的
    `muted == true`（静音期间新起的告警**同样** `muted == true`）；
  - `test_session_mute_resets_on_next_session`：`stop`（走完收尾）后再 `start` 一场 →
    新一场 `snapshot.alerts.hints == []`、不存在 `muted == true` 的条目（**会话级静音自动复位**），
    且 `alarm.sound` 的持久化值**未变**。
"""

from __future__ import annotations

import time

from tests._helpers import read_text
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


#: 进程级用例的轮询超时（秒）
E2E_POLL_TIMEOUT_SECONDS = 30

#: SSE 读取的单帧超时（秒）
SSE_READ_TIMEOUT_SECONDS = 20

#: 本用例里静默的告警（把 `alert.silence_seconds` **显式调小**之后才等得到；代码默认值仍是 180）。
#:
#: **这是指挥者授权的收口（契约表 3 → 实现 1，判据形状不变、代码默认值未动）**：契约 §3.8 末
#: 「测试侧常量」那张表给 `TEST_SILENCE_SECONDS` 写的取值是 `3`，用户的硬要求「单条用例
#: ≤ 5 秒」优先于方案文字里的等待时长 —— 把这一处恢复成 3 会让等告警的这两条用例越过红线。
#: 本节点因此保持 `1`，**判据的形状一字未改**（仍然把阈值经 `config.json` 显式写小、仍然按
#: 调小的阈值断言告警），`alerts.py` 的代码默认值（180 秒）也一个字没动。三处同值同注释的
#: 文件：`tests/test_drain.py` / `tests/test_e2e_silence_alert.py` / `tests/test_e2e_multi_client.py`。
#:
#: 取 1 秒：这一条要**连着**等两次告警（loopback 一次、静默期新起的 mic 一次）。实测把阈值压到
#: 0.5 秒时，dev 替身 0.1 秒一帧的累计会卡在门限下方一档（帧时间戳的间隔取值所致），两条用例
#: 双双等超时 —— 1 秒是本机替身下稳的那一档，也与 `tests/test_e2e_silence_alert.py`、
#: `tests/test_drain.py` 第 ⑫ 条取同一个数。
TEST_SILENCE_SECONDS = 1

#: 注入多久的静默（秒）：要长过 `TEST_SILENCE_SECONDS`，短到不拖长用例
TEST_SILENCE_FAULT_SECONDS = 3


def _warm_up_client(proc) -> None:
    """先读一次 `/api/state`，把用例自己那条 HTTP 连接建好再进入被测窗口。

    本机新建一个 `httpx.Client` 要 0.83 秒（Windows 上 SSL 上下文的初始化，
    `tests/_helpers.py` 的 `local_get()` 上有详细说明）：这笔构造代价若落在「等告警」
    那段轮询里，就会被算进告警阈值的时间预算。
    """
    proc.snapshot()


def _start(proc) -> None:
    started = post_action(proc.base_url, "start")
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
    # 两条订阅者都读到那条告警（同一个 revision）
    alert_frames = [_read_state_event(client) for client in (first, second)]
    assert alert_frames[0]["top_state"] == "alert"
    assert alert_frames[0]["revision"] == alert_frames[1]["revision"], alert_frames
    assert alert_frames[0]["alerts"]["critical"] is not None
    assert alert_frames[0]["alerts"]["critical"]["source"] == "loopback"

    # 一次 `mute_session`：两条订阅者收到的 `state` 是**同一 revision**
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

    # 收尾走完，再开新的一场
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
    assert fresh["alerts"]["hints"] == []
    assert fresh["alerts"]["critical"] is None
    # `alarm.sound` 的持久化值未变（会话级静音不改它）
    assert fresh["settings"]["alarm_sound"] is True
    assert fresh["alerts"]["critical"] is None and fresh["alerts"]["hints"] == []
    log_text = read_text(meeting_env.log_dir / "meeting.log")
    assert "本场已静音（会话级；下一场 start 自动复位" in log_text, (
        f"服务级 log 里该有那行「本场已静音」：{log_text[-800:]}"
    )
    _finish_case(case_started)
