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


pytestmark = pytest.mark.slow


class _FakeLogger:
    """只登记的 logger（`cli.web()` 会往它写启动几行与出错那一行）。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def info(self, message: str, *args: object) -> None:
        self.records.append(("info", message % args if args else message))

    def error(self, message: str, *args: object) -> None:
        self.records.append(("error", message % args if args else message))

    def exception(self, message: str, *args: object) -> None:
        self.records.append(("exception", message % args if args else message))

    def warning(self, message: str, *args: object) -> None:
        self.records.append(("warning", message % args if args else message))


class _FakeRuntime:
    """`cli.web()` 起步几步读的那几个运行时字段（快照里还没有收尾现场）。"""

    log_path = "<临时 log 路径>"
    url = "http://127.0.0.1:0"

    def __init__(self) -> None:
        self.snapshot = SimpleNamespace(ended=None, top_state="idle")
        self.shutdown_interrupted = False

    def set_binding(self, host: str, port: int) -> None:
        return None


def test_web_startup_failure_exits_failure_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """起步这一路（`build_app`）抛意外 → 退出码 `EXIT_FAILURE`，终端与 log 各留一行。

    真跑 `cli.web()`：家目录重定向到本用例的临时目录（配置那一趟因此走**真** `config.load()`），
    只把套接字绑定、应用组装、日志与运行时对象换成假件，让 `build_app()` 抛出 `RuntimeError`。
    """
    from meeting import logging_setup as logging_setup_module
    from meeting.service import app as service_app

    fake_logger = _FakeLogger()
    fake_runtime = _FakeRuntime()
    run_calls = {"n": 0}

    class _FakeServer:
        def run(self, sockets=None) -> None:  # pragma: no cover - 这一档走不到它
            run_calls["n"] += 1

    class _FakeSocket:
        def __init__(self) -> None:
            self.closed = 0

        def getsockname(self):
            return ("127.0.0.1", 51234)

        def close(self) -> None:
            self.closed += 1

    def fake_bind(port=None):
        return _FakeSocket()

    factory_calls = {"n": 0}

    def boom(runtime, config=None):
        factory_calls["n"] += 1
        raise RuntimeError("组装应用这一步按用例要求炸掉")

    monkeypatch.setenv("HOME", str(tmp_path / "home-startup-failure"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home-startup-failure"))
    monkeypatch.setattr(
        logging_setup_module, "setup_service_logging", lambda *a, **k: None
    )
    monkeypatch.setattr(cli_module, "_log", lambda: fake_logger)
    monkeypatch.setattr(service_app, "build_runtime", lambda loaded: fake_runtime)
    monkeypatch.setattr(service_app, "bind_http_socket", fake_bind)
    monkeypatch.setattr(service_app, "build_app", boom)
    monkeypatch.setattr(service_app, "ServiceServer", _FakeServer)

    with pytest.raises(SystemExit) as raised:
        cli_module.web()

    assert raised.value.code == EXIT_FAILURE, (
        f"起步这一步的意外必须以 EXIT_FAILURE 收场：实测 {raised.value.code!r}"
    )
    terminal = capsys.readouterr().err
    assert "服务起步失败" in terminal, terminal
    assert str(fake_runtime.log_path) in terminal, terminal
    assert "meeting post" in terminal, terminal
    assert factory_calls["n"] == 1, "组装应用那一步确实被走到了（这条用例的现场就位）"
    assert run_calls["n"] == 0, "起步就炸了，服务器主循环一次都不该走到"
    assert any(level == "exception" and "服务起步失败" in text for level, text in fake_logger.records), (
        f"服务级 log 上该落一行 error：{fake_logger.records}"
    )
    print(
        f"[M11 证据] build_app 抛 RuntimeError -> 退出码 {raised.value.code}（EXIT_FAILURE="
        f"{EXIT_FAILURE}）；终端：{terminal.strip().splitlines()[:2]}；log 里的 error 行："
        f"{[text for level, text in fake_logger.records if level == 'exception']}"
    )


def _exit_code_runtime(*, interrupted: bool = False, statuses: tuple[str, ...] | None = ()):
    ended = (
        None
        if statuses is None
        else SimpleNamespace(steps=[SimpleNamespace(status=status) for status in statuses])
    )
    return SimpleNamespace(
        shutdown_interrupted=interrupted,
        snapshot=SimpleNamespace(ended=ended),
        log_path="<临时 log 路径>",
    )


def test_drain_exit_code_matrix_including_the_server_crash_fallback():
    """`_drain_exit_code()` 的四组输入逐组写死（**收尾干净 + 服务异常退出 → `EXIT_FAILURE`**）。

    `fallback` 带的是**另一路**信息（`server.run()` 抛异常时 `cli` 的 `finally` 传进来的
    `EXIT_FAILURE`），它原样沿用。
    """
    assert cli_module._drain_exit_code(_exit_code_runtime(statuses=None)) == EXIT_OK
    assert (
        cli_module._drain_exit_code(
            _exit_code_runtime(statuses=("done", "done", "skipped")),
            fallback=EXIT_OK,
        )
        == EXIT_OK
    )
    assert (
        cli_module._drain_exit_code(
            _exit_code_runtime(statuses=("done", "done", "failed")),
            fallback=EXIT_OK,
        )
        == EXIT_FAILURE
    )
    assert (
        cli_module._drain_exit_code(
            _exit_code_runtime(interrupted=True, statuses=("done", "waiting")),
            fallback=EXIT_FAILURE,
        )
        == EXIT_INTERRUPTED
    )
    # 收尾干净这一格抹不掉它（若把兜底值丢掉，这一行会拿到 EXIT_OK）
    assert (
        cli_module._drain_exit_code(
            _exit_code_runtime(statuses=("done", "done", "skipped")),
            fallback=EXIT_FAILURE,
        )
        == EXIT_FAILURE
    )
    assert (
        cli_module._drain_exit_code(
            _exit_code_runtime(statuses=None), fallback=EXIT_FAILURE
        )
        == EXIT_FAILURE
    )

def test_drain_report_speaks_for_the_real_face_not_the_exit_code(capsys):
    """终端那套「收尾失败」文案只在**现场真有 `failed` 步**时出现。"""
    logger = _FakeLogger()
    clean = _exit_code_runtime(statuses=("done", "done", "skipped"))
    failed = _exit_code_runtime(statuses=("done", "done", "failed"))
    idle = _exit_code_runtime(statuses=None)

    assert cli_module._drain_reported_failed(clean) is False
    assert cli_module._drain_reported_failed(idle) is False
    assert cli_module._drain_reported_failed(failed) is True

    assert cli_module._drain_exit_code(clean, fallback=EXIT_FAILURE) == EXIT_FAILURE
    cli_module._report_drain_outcome(clean, logger, EXIT_FAILURE)
    terminal_clean = capsys.readouterr().err
    assert terminal_clean == "", (
        "收尾干净、只是服务器自己炸了：这套「收尾有步骤失败」的文案不该出现 —— "
        f"终端上打出了：{terminal_clean!r}"
    )
    assert not [item for item in logger.records if item[0] == "error"], logger.records

    cli_module._report_drain_outcome(failed, logger, EXIT_FAILURE)
    terminal_failed = capsys.readouterr().err
    assert "收尾有步骤失败，服务不自杀（现场仍在）" in terminal_failed, terminal_failed
    assert f"log 路径：{failed.log_path}" in terminal_failed, terminal_failed
    assert "出路一：meeting post <场次目录>" in terminal_failed, terminal_failed
    assert [item for item in logger.records if item[0] == "error"], logger.records

    cli_module._report_drain_outcome(idle, logger, EXIT_FAILURE)
    terminal_idle = capsys.readouterr().err
    assert terminal_idle == "", terminal_idle

    print(
        "[M1 证据] 三组输入都带 exit_code=EXIT_FAILURE，终端文案按现场挑：\n"
        f"  ① 收尾干净（steps=done/skipped/done/skipped）+ 服务异常退出 -> 终端 {terminal_clean!r}"
        f"（_drain_reported_failed={cli_module._drain_reported_failed(clean)}）\n"
        f"  ② 收尾有步骤 failed -> 终端打出：{terminal_failed.strip().splitlines()[:2]}\n"
        f"  ③ ended 为空（空闲态退出）-> 终端 {terminal_idle!r}"
    )


def test_interrupted_report_speaks_for_the_forced_interrupt_not_the_failed_face(capsys):
    """**强制打断档 + 现场有 `failed` 步** → 终端照着真实档位说话。

    档位压过现场：`_drain_exit_code()` 对强制打断那一支先判。
    """
    logger = _FakeLogger()
    runtime = _exit_code_runtime(
        interrupted=True, statuses=("done", "done", "failed")
    )
    assert cli_module._drain_reported_failed(runtime) is True, "这一格现场确实有 failed 步"
    assert cli_module._drain_exit_code(runtime, fallback=EXIT_OK) == EXIT_INTERRUPTED

    cli_module._report_drain_outcome(runtime, logger, EXIT_INTERRUPTED)
    terminal = capsys.readouterr().err

    assert "收尾被强制打断" in terminal, terminal
    assert "draining + interrupted" in terminal, terminal
    assert "服务不自杀（现场仍在）" not in terminal, (
        "强制打断档不该说「服务不自杀（现场仍在）」—— 这一趟是被打断的、"
        f"进程正在关停：{terminal!r}"
    )
    assert f"log 路径：{runtime.log_path}" in terminal, terminal
    assert "meeting post" in terminal, terminal
    assert "重启服务" in terminal, terminal
    assert [item for item in logger.records if item[0] == "error"], logger.records
    print(
        "[M11 证据] 强制打断 + 现场有 failed 步（post）这一格：\n"
        f"  退出码 = {cli_module._exit_name(EXIT_INTERRUPTED)}（_drain_reported_failed="
        f"{cli_module._drain_reported_failed(runtime)}）\n"
        f"  终端打出：{terminal.strip().splitlines()}"
    )
