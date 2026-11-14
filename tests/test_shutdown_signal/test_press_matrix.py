from __future__ import annotations

import signal
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import httpx

from meeting import cli as cli_module
from meeting.cli import EXIT_FAILURE, EXIT_INTERRUPTED, EXIT_OK
from meeting.service import app as service_app
from meeting.state import DRAIN_STEP_IDS, DRAIN_STEP_STATUS
from tests import _drain_fakes as fakes
from tests import _helpers as helpers
from tests._helpers import wait_until
from tests._service import post_action, read_session_json

from tests.test_shutdown_signal._common import (
    PROCESS_EXIT_TIMEOUT_SECONDS,
    SELF_CHECK_LINE,
    SHUTDOWN_BLOCKING_STEP_SECONDS,
    SIGBREAK_LINE,
    SIGBREAK_STATE_POLL_TIMEOUT_SECONDS,
    _disk_record,
    _finish_case,
    _latest_session_dir,
    _log_text,
    _session_dirs,
    _spawn,
    _spawn_env,
    _start,
    _state_at_terminal,
    _state_while_alive,
    _step_statuses,
    _terminal_landed_line,
    _wait_state,
    case_env,
)

pytestmark = pytest.mark.slow


class _StubProcess:
    def __init__(self, *, returncode: int | None, error: Exception | None = None) -> None:
        self._returncode = returncode
        self._error = error

    def poll(self) -> int | None:
        return self._returncode

    def snapshot(self) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return {"top_state": "ended", "ended": {"phase": "done"}}


def test_degraded_state_read_tolerates_only_teardown_and_still_raises_on_bad_answers():
    """**传输层没拿到应答**整类让位；服务答得不好时照样抛出去。

    四格逐格写死：

      1. **子进程已经收场**（`poll()` 非空）→ 返回 `None`（这一路是真的没有运行期可读，
         `_state_at_terminal()` 随后退到盘上那份）；
      2. **传输层没拿到应答** → 返回 `None`。这一整类都收下：
         `ConnectError` / `ConnectTimeout` / `RemoteProtocolError` / `ReadError` /
         `ReadTimeout` / `PoolTimeout` 六种逐条断言，一条都不许再漏
         （`RemoteProtocolError`：服务退出时对端在应答之前把连接断掉，MRO 走
         `ProtocolError → TransportError`，不在 `ConnectError` 那一支下）；
      3. **服务答了个非 200**（`ServiceProcess.snapshot()` 自己就断言 200，不合格的应答在那一层
         变成 `httpx.HTTPStatusError`）→ **原样抛出去**。这一格是「服务答得不好」与「连接被断」
         的分水岭：吞掉它，用例就会把「服务答了个 500」读成「刚好在退出」，静默退化到盘上那份；
      4. **其余任何异常**（这里用 `RuntimeError` 代表「应答体不是 JSON / 形状不对」那一类）
         → 同样**原样抛出去**。
    """
    def predicate(snap: dict[str, Any]) -> bool:
        return (snap.get("ended") or {}).get("phase") == "done"

    assert (
        _state_while_alive(_StubProcess(returncode=0), predicate, what="已收场") is None
    )

    for name, error in (
        ("连接都建不起来", httpx.ConnectError("连接被断")),
        ("连上了但对端不发应答", httpx.ConnectTimeout("连接超时")),
        ("应答之前对端断了连接", httpx.RemoteProtocolError("Server disconnected")),
        ("应答读到一半断了", httpx.ReadError("读到一半断了")),
        ("应答读取超时", httpx.ReadTimeout("读取超时")),
        ("连接池在关停瞬间收走连接", httpx.PoolTimeout("池超时")),
    ):
        assert (
            _state_while_alive(
                _StubProcess(returncode=None, error=error), predicate, what=name
            )
            is None
        ), name

    with pytest.raises(httpx.HTTPStatusError):
        _state_while_alive(
            _StubProcess(
                returncode=None,
                error=httpx.HTTPStatusError(
                    "GET /api/state 该回 200，实测 500",
                    request=httpx.Request("GET", "http://127.0.0.1/api/state"),
                    response=httpx.Response(500),
                ),
            ),
            predicate,
            what="服务答了个 500",
        )

    with pytest.raises(RuntimeError):
        _state_while_alive(
            _StubProcess(returncode=None, error=RuntimeError("读到一份形状不对的应答")),
            predicate,
            what="服务答得不好",
        )

    alive = _state_while_alive(_StubProcess(returncode=None), predicate, what="正常应答")
    assert alive is not None and alive["source"] == "/api/state", alive
    print(
        "[m7b 证据] 已收场与「传输层没拿到应答」六种都返回 None；"
        "非 200 那一格抛 HTTPStatusError、形状不对那一格抛 RuntimeError；"
        f"正常应答那一格交出的快照 source={alive['source']!r}"
    )


def test_idle_sigbreak_exits_zero(service_process, case_env):
    """空闲态按一次 `CTRL_BREAK_EVENT` → 空闲退出档退出码。"""
    case_started = time.monotonic()
    proc = _spawn(service_process, case_env)
    proc.wait_ready()
    assert proc.poll() is None

    proc.send_interrupt()
    code = proc.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
    assert code == EXIT_OK

    log_text = _log_text(proc)
    assert SIGBREAK_LINE in log_text
    assert SELF_CHECK_LINE in log_text
    assert "退出原因与退出码：" in log_text
    assert _session_dirs(proc) == []
    _finish_case(case_started, "① 空闲态按一次")


@pytest.mark.slow
def test_clean_drain_then_sigbreak_exits_zero_without_rewriting_disk(
    service_process, case_env
):
    """`stop` 已收尾干净（`ended·done`）之后按一次 `Ctrl+C` → 空闲退出档，盘上逐字节未变。

    判据：四步已全部落定且**无 `failed`**，当场没有任何要收的尾 —— 因此落在
    **空闲退出档**，且**盘上一个字节都不改写**；同一输出根上的下一次 `start` 不得
    撞上 `draining`(409)。
    """
    case_started = time.monotonic()
    proc = _spawn(service_process, case_env)
    proc.wait_ready()
    _start(proc)
    session_dir = _latest_session_dir(proc)

    assert post_action(proc.base_url, "stop").status_code == 200
    # **等收尾真的走完**（不是靠抢时间窗口）
    _wait_state(
        proc,
        lambda snap: (snap.get("ended") or {}).get("phase") == "done",
        what="收尾走完（ended.phase == done）",
    )
    record_before = _disk_record(proc)
    assert record_before["state"] == "done", record_before
    assert record_before["ended_at"], record_before
    assert record_before["interrupted"] is False, record_before

    session_json = session_dir / "session.json"
    before = session_json.read_bytes()

    # **只发一次**信号
    proc.send_interrupt()
    code = proc.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)

    log_text = _log_text(proc)
    assert code == EXIT_OK, (
        f"收尾干净走完之后按一次 Ctrl+C 该走空闲退出档：实测 {code}"
        f"（EXIT_OK={EXIT_OK} / EXIT_INTERRUPTED={EXIT_INTERRUPTED}）"
    )
    assert "强制打断" not in log_text, "这一格不该落强制打断那一行"
    assert SIGBREAK_LINE in log_text
    assert "退出原因与退出码：" in log_text
    assert "EXIT_OK" in log_text, f"log 里该查得到 EXIT_OK 这一档：\n{log_text[-1200:]}"

    after = session_json.read_bytes()
    assert after == before, (
        "收尾干净走完之后的这一次关停不该改写盘上那份 session.json：\n"
        f"发信号前 {before.decode('utf-8', 'replace')}\n"
        f"退出之后 {after.decode('utf-8', 'replace')}"
    )
    record_after = _disk_record(proc)
    assert record_after["state"] == "done", record_after
    assert record_after["interrupted"] is False, record_after
    assert record_after["ended_at"], record_after

    # **下一场能开起来**：同一个放置根上重起（`_spawn_env()` 按 `<家目录>/../out` 算放置根，
    # 因此把第二个家目录取成第一个的兄弟目录，两趟指向的是同一个根）
    restarted_home = case_env.parent / "home-clean-drain-restart"
    restarted = service_process(
        home=restarted_home, capture=None, env=_spawn_env(restarted_home)
    )
    try:
        restarted.wait_ready()
        snapshot = restarted.snapshot()
        assert snapshot["top_state"] == "idle", snapshot
        started = post_action(
            restarted.base_url,
            "start",
            {"dir": helpers.allocate_start_dir(restarted_home.parent / "out")},
        )
        assert started.status_code == 200, (
            f"收尾干净的场次之外该能直接开新场：{started.status_code} {started.text}"
        )
        print(
            f"[C1 证据] stop 收尾干净（record.state={record_before['state']!r} / "
            f"ended_at={record_before['ended_at']!r} / interrupted={record_before['interrupted']!r}）"
            f"之后按一次 Ctrl+C -> 退出码 {code}（EXIT_OK={EXIT_OK}）；"
            f"session.json 逐字节未变；重启后无需任何临终改写、POST start -> {started.status_code}"
        )
    finally:
        restarted.terminate()
    _finish_case(case_started, "①c 收尾干净之后按一次")


@pytest.mark.slow
def test_recording_sigbreak_drains_and_exits_zero(service_process, case_env):
    """录音中按一次 `CTRL_BREAK_EVENT` → 收尾真跑完，收尾干净档退出码。

    **收尾期间服务照常应答**（收尾协程不把事件循环钉住）：假采集件在工作线程里睡
    `SHUTDOWN_BLOCKING_STEP_SECONDS`（收尾第 1 步 `flush` 因此在盘上看得见地停在
    `running`），收尾进度因此在进度条上真的看得见。
    """
    case_started = time.monotonic()
    plugin = fakes.write_knob_plugin(
        case_env, capture_class="FakeCaptureBackend",
        capture_delay_seconds=SHUTDOWN_BLOCKING_STEP_SECONDS,
    )
    fakes.write_plugin_config(case_env, plugin)

    proc = _spawn(service_process, case_env, plugin)
    proc.wait_ready()
    wait_until(plugin.module_loaded, timeout=SIGBREAK_STATE_POLL_TIMEOUT_SECONDS)
    _start(proc)
    session_dir = _latest_session_dir(proc)
    assert read_session_json(session_dir)["state"] == "recording"

    proc.send_interrupt()

    progress = _wait_state(
        proc,
        lambda snap: (snap.get("ended") or {}).get("phase") == "draining"
        and _step_statuses(snap).get("flush") == "running",
        what="收尾进行中（第 1 步 flush 正在跑）",
    )
    assert progress["top_state"] == "ended", progress.get("ended")

    # **退出前读到的那份 `/api/state`**：收尾终态不回 `idle`，在进程退出之前读
    terminal = _state_at_terminal(
        proc,
        lambda snap: snap.get("top_state", "ended") == "ended"
        and (snap.get("ended") or {}).get("phase") == "done",
        what="退出前读到 ended.phase == done 的 /api/state",
        timeout=SIGBREAK_STATE_POLL_TIMEOUT_SECONDS,
    )
    assert terminal is not None, "退出前该读到一次 ended.phase == done 的 /api/state"
    # **这一份必须真的是终态那一刻的运行期读数**：两条路都算数、都不许只判谓词：
    #   1. `source == "/api/state"` —— 直接读到了运行期快照（首选，抓得到就是它）；
    #   2. `source == "session.json"` —— 服务已经答不上（监听 socket 先关、进程还在收尾），
    # 这时要求服务级 log 里**已经有**「收尾终态已落盘：ended.phase=done」那一行：
    # 它由运行期在 `finish_drain()` 判完终态之后写，而请求关停要等 `_wait_drain_settled()`
    # 下一次轮询才发生（`DRAIN_SETTLE_POLL_SECONDS`）—— 于是「退出前运行期快照已经是
    # 终态」这件事在任何时序下都有可核对的读数。
    if terminal["source"] != "/api/state":
        landed = _terminal_landed_line(proc)
        assert landed is not None and "ended.phase=done" in landed, (
            f"没从运行期快照读到收尾终态时，服务级 log 里该有「收尾终态已落盘：ended.phase=done」"
            f"那一行（实测来源 {terminal['source']!r}）：{landed!r}"
        )
    assert terminal["top_state"] == "ended", terminal
    assert (terminal.get("ended") or {}).get("phase") == "done", terminal

    code = proc.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
    assert code == EXIT_OK

    record = read_session_json(session_dir)
    assert record is not None
    assert record["state"] == "done"
    assert record["interrupted"] is False
    assert record["ended_at"]
    statuses = [step["status"] for step in (record.get("drain") or {}).get("steps") or []]
    assert [step["id"] for step in (record.get("drain") or {}).get("steps") or []] == list(
        DRAIN_STEP_IDS
    )
    assert set(statuses) <= set(DRAIN_STEP_STATUS)
    assert all(status in ("done", "skipped", "waiting") for status in statuses)
    settled = {
        step["id"]: step["status"]
        for step in (record.get("drain") or {}).get("steps") or []
    }
    assert settled["post"] == "waiting", settled
    assert _log_text(proc).count("退出原因与退出码：") >= 1
    assert (terminal.get("ended") or {}).get("phase") == record["state"]
    _finish_case(case_started, "② 录音中按一次")


