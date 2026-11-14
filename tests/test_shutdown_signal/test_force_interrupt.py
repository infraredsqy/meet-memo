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
    INTERRUPTED_LINE,
    INTERRUPT_EXIT_LATENCY_LIMIT_SECONDS,
    PROCESS_EXIT_TIMEOUT_SECONDS,
    SHUTDOWN_BLOCKING_STEP_SECONDS,
    SIGBREAK_LINE,
    SIGBREAK_STATE_POLL_TIMEOUT_SECONDS,
    _assert_interrupted_face,
    _finish_case,
    _latest_session_dir,
    _log_text,
    _spawn,
    _start,
    _step_statuses,
    _wait_state,
    case_env,
)

pytestmark = pytest.mark.slow


@pytest.mark.slow
def test_second_sigbreak_during_drain_exits_130(service_process, case_env):
    """收尾进行中再按一次 → `interrupted: true`、`state == "draining"`、强制打断档退出码。

    **同步点不靠抢时间窗口**：假采集件 `finalize()`（收尾第 1 步 `flush`）在工作线程里睡
    `SHUTDOWN_BLOCKING_STEP_SECONDS`，轮询 `/api/state` 一定能在阻塞结束之前查到
    `flush` 那一步 `status == "running"`，那时收尾确实还在跑。

    **「立即中止」的时延**：第二次按下之后进程必须在
    `INTERRUPT_EXIT_LATENCY_LIMIT_SECONDS` 之内退出，而**在飞的同步步还剩
    `SHUTDOWN_BLOCKING_STEP_SECONDS` 那么久** —— 收尾各步的工作线程是 daemon，所以这段
    剩余时长不参与进程退出。
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

    assert post_action(proc.base_url, "stop").status_code == 200
    during = _wait_state(
        proc,
        lambda snap: _step_statuses(snap).get("flush") == "running",
        what="收尾的 flush 那一步停在 running",
    )
    assert during["ended"]["phase"] == "draining"

    pressed = time.monotonic()
    proc.send_interrupt()
    code = proc.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
    exit_latency = time.monotonic() - pressed
    assert code == EXIT_INTERRUPTED

    _assert_interrupted_face(proc)
    log_text = _log_text(proc)
    assert INTERRUPTED_LINE in log_text
    # 这一档用户只按了**一次**（`stop` 之后收尾中再按）；同一行里补上真实按下次数，
    # 记录因此不失真。子进程的计数器从 0 起，这一按就是第 1 次。
    assert f"{INTERRUPTED_LINE}（本次是第 1 次按下）" in log_text, (
        f"强制打断那一行该带上真实按下次数：\n{log_text[-1200:]}"
    )

    # 「立即中止」：这一路的退出时延与在飞那一步还剩多久无关（daemon 工作线程不等它），
    # 因此「退出没等在飞步」这件事由这条上限单独咬着
    assert exit_latency <= INTERRUPT_EXIT_LATENCY_LIMIT_SECONDS, (
        f"第二次 Ctrl+C 之后进程用了 {exit_latency:.2f}s 才退出（上限 "
        f"{INTERRUPT_EXIT_LATENCY_LIMIT_SECONDS}s）—— 这个量级说明解释器在等收尾步骤的"
        f"工作线程（现场里那一步阻塞 {SHUTDOWN_BLOCKING_STEP_SECONDS}s）"
    )
    _finish_case(case_started, "③ 收尾中途第二次按")


@pytest.mark.slow
def test_second_press_in_the_same_process_carries_force_true(service_process, case_env):
    """**同一个进程连按两次** → 第二次按下带 `force=True`，强制打断档收场。

    现场（一个子进程、两次 `CTRL_BREAK_EVENT`）：

      1. `start` 进 `recording`；
      2. **第一次按下** —— 这一刻 `top_state == "recording"`，走 stop_session 那条路；
         断言它**确实走的是这一行**（服务级 log 里有「第一次 Ctrl+C：收尾已起」），于是
         第二次按下的 `force=True` 真的来自「同一次进程里的第二次信号」，与状态判据无关；
      3. 收尾**还在跑**（假采集件 `finalize()` 在工作线程里睡
         `SHUTDOWN_BLOCKING_STEP_SECONDS`，轮询到 `flush` 那一步停在 `running`——同步点确定，
         不靠抢时间窗口）；
      4. **第二次按下** —— 这时收尾仍在进行，处理函数内部计数走到 2、`force=True`。

    「本次是第 2 次按下」这一行里的次数就是 `force` 的实测形式（`force = 次数 > 1`）。
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

    first_pressed = time.monotonic()
    proc.send_interrupt()
    _wait_state(
        proc,
        lambda snap: (snap.get("ended") or {}).get("phase") == "draining",
        what="第一次按下之后收尾已起（ended.phase == draining）",
    )
    started_line = "第一次 Ctrl+C：收尾已起（与 stop 同一条路径），等它落定再关停服务"
    assert started_line in _log_text(proc), (
        f"第一次按下该走「录音中按一次」那一条路（日志里没有那句话）：\n{_log_text(proc)[-1500:]}"
    )

    # **确定的同步点**：这时才按第二次
    during = _wait_state(
        proc,
        lambda snap: _step_statuses(snap).get("flush") == "running",
        what="两次按下之间收尾的 flush 那一步停在 running",
    )
    assert during["ended"]["phase"] == "draining", during.get("ended")

    second_pressed = time.monotonic()
    proc.send_interrupt()
    code = proc.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
    exit_latency = time.monotonic() - second_pressed
    assert code == EXIT_INTERRUPTED

    disk = _assert_interrupted_face(proc)
    log_text = _log_text(proc)
    # 这一行的次数就是处理函数内部数出来的 `force`（2 > 1）
    assert f"{INTERRUPTED_LINE}（本次是第 2 次按下）" in log_text, (
        f"第二次按下该落「本次是第 2 次按下」那一行（`force=True` 的实测形式）：\n"
        f"{log_text[-1500:]}"
    )
    assert log_text.count(SIGBREAK_LINE) == 2, (
        f"同一个进程里该落下两行「{SIGBREAK_LINE}」：\n{log_text[-1500:]}"
    )
    # 「立即中止」：退出时延与在飞那一步还剩多久无关（收尾的工作线程是 daemon）
    assert exit_latency <= INTERRUPT_EXIT_LATENCY_LIMIT_SECONDS, (
        f"第二次按下之后进程用了 {exit_latency:.2f}s 才退出（上限 "
        f"{INTERRUPT_EXIT_LATENCY_LIMIT_SECONDS}s；现场里那一步阻塞 "
        f"{SHUTDOWN_BLOCKING_STEP_SECONDS}s）"
    )

    steps = ((disk.get("drain") or {}).get("steps")) or []
    print(
        f"[C1 证据] 同一个进程连按两次：第一次 @ {first_pressed - case_started:.3f}s"
        f"（force=False，走 stop_session 那条路，log 里查到「{started_line}」）；"
        f"第二次 @ {second_pressed - case_started:.3f}s（force=True，收尾仍在跑：flush="
        f"{_step_statuses(during)['flush']!r}）\n"
        f"  退出码 {code}（EXIT_INTERRUPTED={EXIT_INTERRUPTED}），第二次按下到退出 "
        f"{exit_latency:.3f}s\n"
        f"  盘上 {session_dir.name}：state={disk['state']!r} / interrupted={disk['interrupted']!r}"
        f" / ended_at={disk['ended_at']!r} / steps={[step['status'] for step in steps]}\n"
        f"  服务级 log 那一行：{INTERRUPTED_LINE}（本次是第 2 次按下）；"
        f"「{SIGBREAK_LINE}」共 {log_text.count(SIGBREAK_LINE)} 行"
    )
    _finish_case(case_started, "③b 同一进程连按两次")


@pytest.mark.slow
def test_sigbreak_while_already_draining_exits_130(service_process, case_env):
    """已经停在收尾失败现场再按一次 → 与 ③ 同结果（**不为它单开第三条路径**）。

    失败现场由假采集件的 `finalize()` 抛错造出（收尾第 1 步 `flush` 因此落 `failed`）：
    `POST stop` 之后轮询到 `ended.phase == "draining"`
    且 `flush` 那一步 `status == "failed"`，这一档下服务不自杀，正好等第二次信号。
    """
    case_started = time.monotonic()
    plugin = fakes.write_failing_capture_plugin(case_env)
    fakes.write_plugin_config(case_env, plugin)

    proc = _spawn(service_process, case_env, plugin)
    proc.wait_ready()
    wait_until(plugin.module_loaded, timeout=SIGBREAK_STATE_POLL_TIMEOUT_SECONDS)
    _start(proc)

    assert post_action(proc.base_url, "stop").status_code == 200
    _wait_state(
        proc,
        lambda snap: (snap.get("ended") or {}).get("phase") == "draining"
        and _step_statuses(snap).get("flush") == "failed",
        what="收尾落在失败现场（phase 保持 draining、flush 是 failed）",
    )
    # 服务不自杀：这一档下进程照常活着
    assert proc.poll() is None

    proc.send_interrupt()
    code = proc.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
    assert code == EXIT_INTERRUPTED

    _assert_interrupted_face(proc)
    _finish_case(case_started, "④ 已停在 draining 再按")


