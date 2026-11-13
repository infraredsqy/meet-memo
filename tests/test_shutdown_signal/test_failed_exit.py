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
    SIGBREAK_LINE,
    SIGBREAK_STATE_POLL_TIMEOUT_SECONDS,
    SIGINT_LINE,
    _disk_record,
    _finish_case,
    _log_text,
    _press_self_signal,
    _spawn,
    _start,
    case_env,
)

#: 慢档标记：`pytest.mark.slow` 的归位判据见 `docs/service.md`。
pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# ⑤ 收尾有步骤 `failed` → 收尾失败档
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_failed_drain_exits_one_and_prints_log_path(service_process, case_env):
    """收尾有步骤 `failed` → 收尾失败档退出码，且终端上出现 log 路径与 `meeting post`。

    同一趟顺带断言**服务级 log** 里可查到启动与端口 / URL、以及退出原因与退出码
    （设计口径 §6.3 行 304 清单的服务级那半边）。失败手段是共用支持模块里的
    `FakeCaptureBackend`（`finalize()` 抛错 → 第 1 步 `flush` 落 `failed`，
    经 `PYTHONPATH` + `capture.backend` 配进子进程），`dev_inject` 那三档故障
    只改事实流、造不出收尾失败。
    """
    case_started = time.monotonic()
    plugin = fakes.write_failing_capture_plugin(case_env)
    fakes.write_plugin_config(case_env, plugin)

    proc = _spawn(service_process, case_env, plugin)
    proc.wait_ready()
    wait_until(plugin.module_loaded, timeout=SIGBREAK_STATE_POLL_TIMEOUT_SECONDS)
    _start(proc)

    proc.send_interrupt()
    code = proc.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
    assert code == EXIT_FAILURE

    record = _disk_record(proc)
    assert record["state"] == "failed"
    assert record["state"] != "done"
    assert record["ended_at"]
    assert record["interrupted"] is False

    stdout = proc.stdout()
    stderr = proc.stderr()
    combined = stdout + stderr
    log_path = str(helpers.service_log_path_of(proc.home))
    assert log_path in combined, f"终端上要出现 log 路径：\n{combined[-2000:]}"
    assert "log 路径：" in combined, f"终端上那一行该写清是 log 路径：\n{combined[-2000:]}"
    assert "meeting post" in combined, f"终端上要出现那条可复制的命令：\n{combined[-2000:]}"

    # **正向断言：终端确实把「服务 log 最后 20 行」那一节打出来了**（审查问题 m7a）。
    # 此前这一档只有片段式的判据（「终端上出现 log 路径」这类），`read_log_tail()` 就算回空
    # 列表、那一节整段不打，上面几条也照绿。这里按 `cli.py` 的两处常量把这一节钉住：
    # 小节标题（含 `LOG_TAIL_LINES_ON_FAILURE`）在场，标题之下**紧跟着那 20 行 log 的正文**
    # —— 逐行剥掉 `cli.py` 加的两个空格缩进之后，**每行都逐字出现在这份服务 log 里**。
    # 这样比「与文件尾部逐行相等」松一档但足够严：`cli.py` 倒完这一节之后还会再写两行 log
    # （「收尾失败档退出…」与退出码那一行），按「滚动窗口」去对齐反而会假红；而 20 行正文
    # 一行不少、且确实来自这份 log，正是「这一节真的落了内容」要的那件事。
    from meeting.cli import LOG_TAIL_LINES_ON_FAILURE

    tail_header = f"服务 log 最后 {LOG_TAIL_LINES_ON_FAILURE} 行："
    assert tail_header in combined, (
        f"收尾失败档该把「{tail_header}」这一节打到终端上：\n{combined[-2000:]}"
    )
    log_text = _log_text(proc)
    printed_section = combined.split(tail_header, 1)[1].splitlines()
    printed_tail: list[str] = []
    for line in printed_section:
        if line.startswith("  "):
            printed_tail.append(line[2:])
            continue
        # 小节标题自己那一行的行尾**紧跟一个空白行**（标题是 `print` 单独一行落的），
        # 它还不属于正文 —— 收正文之前先把这种空白跳掉
        if line.strip() == "" and not printed_tail:
            continue
        if line.strip() == "" and len(printed_tail) < LOG_TAIL_LINES_ON_FAILURE:
            printed_tail.append("")
            continue
        if not line.startswith((" ", "\t")) and not line[:4].isdigit():
            break  # 「退出原因与退出码：…」这一类收尾输出：这一节到此为止
        printed_tail.append(line)
    assert len(printed_tail) == LOG_TAIL_LINES_ON_FAILURE, (
        f"那一节该正好 {LOG_TAIL_LINES_ON_FAILURE} 行正文，实测 {len(printed_tail)} 行："
        f"{printed_tail}\n终端原文：{combined[-2000:]}"
    )
    missing = [line for line in printed_tail if line.rstrip() not in log_text]
    assert not missing, (
        "终端那一节的每一行都该来自这份服务 log（`read_log_tail()` 读的就是它）："
        f"以下行没在 log 里找到：{missing}"
    )

    log_text = _log_text(proc)
    assert "绑定成功：127.0.0.1:" in log_text
    assert "http://127.0.0.1:" in log_text, "服务级 log 该写清选中的 URL"
    assert "退出原因与退出码：" in log_text
    assert "EXIT_FAILURE" in log_text
    _finish_case(case_started, "⑤ 收尾失败档")


# ---------------------------------------------------------------------------
# ⑥ 两路信号同形
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_sigint_and_sigbreak_share_one_handler(service_process, case_env):
    """`SIGBREAK` 与 `SIGINT` 走**同一个处理函数**，两边的结果字段与退出码逐项一致。

    三段合起来看：

      ① **进程内**：`install_shutdown_signal_handlers()` 之后两个信号号**映射到同一个函数
         对象**（`handle_shutdown_signal`）—— 逐个信号各断言一次身份，两者的同一性由这两条
         传递可得，用完复原；
      ② **进程内**：直接调 `handle_shutdown_signal(SIGINT, None)` 与
         `handle_shutdown_signal(SIGBREAK, None)`，两次都落在同一个处理函数上、同一行
         服务级 log（`收到 SIGINT，进入关停` / `收到 SIGBREAK，进入关停`），且
         **按下次数计数器在函数内部真的在数**（`shutdown_press_count()` 从 0 走到 2，
         第二次因此被判成强制打断那一档）；
      ③ **进程级**：两个真服务子进程各收一次信号 —— 一个走 `CTRL_BREAK_EVENT`
         （`os.kill` 在 Windows 上可靠送达的那一路），一个在子进程里由主线程
         `signal.raise_signal(signal.SIGINT)` **自己给自己发**（走的是真实的 `Ctrl+C`
         路径，理由见文件顶部说明）—— 两者的退出码、盘上 `state` / `interrupted` /
         `ended_at` / 四步 `status`、以及各自那行服务级 log 逐字段一致。
    """
    case_started = time.monotonic()
    previous_int = signal.getsignal(signal.SIGINT)
    previous_break = signal.getsignal(getattr(signal, "SIGBREAK", signal.SIGINT))
    target = service_app._shutdown_target
    try:
        service_app.install_shutdown_signal_handlers()
        # ① 两个信号号 → 同一个函数对象
        assert signal.getsignal(signal.SIGINT) is service_app.handle_shutdown_signal
        assert hasattr(signal, "SIGBREAK"), "SIGBREAK 是本服务要接管的那第二个信号"
        assert signal.getsignal(signal.SIGBREAK) is service_app.handle_shutdown_signal

        # ② 同一个处理函数被两个信号各调一次：计数器在它内部自增、第二次是强制打断那一档
        service_app.reset_shutdown_press_count()
        service_app.clear_shutdown_target()
        assert service_app.shutdown_press_count() == 0
        service_app.handle_shutdown_signal(signal.SIGINT, None)
        assert service_app.shutdown_press_count() == 1, "第一次按下该被数进来"
        service_app.handle_shutdown_signal(signal.SIGBREAK, None)
        assert service_app.shutdown_press_count() == 2, "第二次按下该被数进来（强制打断那一档）"
    finally:
        if target is None:
            service_app.clear_shutdown_target()
        else:
            service_app.set_shutdown_target(*target)
        service_app.reset_shutdown_press_count()
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(getattr(signal, "SIGBREAK", signal.SIGINT), previous_break)

    # ③ 两个真服务子进程：一路 CTRL_BREAK_EVENT、一路子进程自己发 SIGINT
    results: dict[str, dict[str, Any]] = {}
    for label, self_signal in (("sigbreak", False), ("sigint", True)):
        home = case_env.parent / f"home-{label}"
        home.mkdir(parents=True, exist_ok=True)
        proc = _spawn(service_process, home, self_signal=self_signal)
        proc.wait_ready()
        _start(proc)
        if self_signal:
            # 用例在这里做「人手按下」这件事：标记一落，包装脚本就替我们按下 Ctrl+C
            # （落在「录音中」这一格，与 CTRL_BREAK_EVENT 那一路同一格）
            _press_self_signal(home)
        else:
            proc.send_interrupt()
        code = proc.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)

        record = _disk_record(proc)
        results[label] = {
            "code": code,
            "state": record["state"],
            # `ended_at` 是两次关停各自的时刻，**天然不会逐字相同**：这里比对的是
            # 「这一档该不该写它」（两路都该写），时刻本身不比
            "ended_at_written": bool(record["ended_at"]),
            "interrupted": record["interrupted"],
            "steps": [step["status"] for step in (record.get("drain") or {}).get("steps") or []],
            "step_ids": [step["id"] for step in (record.get("drain") or {}).get("steps") or []],
            "has_line": (SIGBREAK_LINE if label == "sigbreak" else SIGINT_LINE) in _log_text(proc),
        }

    assert set(results) == {"sigbreak", "sigint"}, results
    assert results["sigbreak"]["code"] == results["sigint"]["code"] == EXIT_OK, results
    assert results["sigbreak"]["has_line"], f"SIGBREAK 那一行该落 log：{results}"
    assert results["sigint"]["has_line"], f"SIGINT 那一行该落 log：{results}"
    for field in (
        "code",
        "state",
        "ended_at_written",
        "interrupted",
        "steps",
        "step_ids",
        "has_line",
    ):
        assert results["sigbreak"][field] == results["sigint"][field], (
            f"两路信号的结果字段在 {field} 上不一致：{results}"
        )
    # 两路都真的收尾干净（收尾干净档那一副面孔）
    assert results["sigbreak"]["state"] == "done", results
    assert results["sigbreak"]["interrupted"] is False, results
    assert results["sigbreak"]["ended_at_written"], results
    assert results["sigbreak"]["step_ids"] == list(DRAIN_STEP_IDS), results
    # 四步都落定：前三步各自的终态，第 4 步如实停在「等你按按钮」那一格（R26）
    assert all(
        status in ("done", "skipped", "waiting") for status in results["sigbreak"]["steps"]
    ), results
    assert results["sigbreak"]["steps"][-1] == "waiting", results
    _finish_case(case_started, "⑥ 两路信号同形")


