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


#: 慢档标记：`pytest.mark.slow` 的归位判据见 `docs/service.md`。
pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# `server.run()` **之外**那一路的意外：退出码同样得说话（审查问题 M11）
#
# `cli.web()` 的 `finally` 曾经只围住 `server.run()`：组装应用（`build_app`）、构造服务器
# 对象这些**起步步骤**抛出的异常会直接冒到解释器，`_drain_exit_code()` 那一趟根本没跑，
# 终端上也一个字都没有。现在这些起步步骤与 `server.run()` 同在一个 `try` 里，退出码一律按
# `EXIT_FAILURE` 收场，并落一行 error 与终端说明（§3.5「不许让异常冒到 uvicorn / 解释器那里
# 变成解释器默认码」+ §8「不许静默」）。
#
# 下面这条**真跑 `cli.web()`**（只把外部依赖换掉：配置、绑定、应用组装、日志），因此它验的是
# 那条退出路径本身，而不是某个辅助函数。它在不需要起服务的情况下几毫秒就走完。
# ---------------------------------------------------------------------------


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
    """起步这一路（`build_app`）抛意外 → 退出码 `EXIT_FAILURE`，终端与 log 各留一行（M11）。

    真跑 `cli.web()`：家目录重定向到本用例的临时目录（配置那一趟因此走**真** `config.load()`），
    只把套接字绑定、应用组装、日志与运行时对象换成假件，让 `build_app()` 抛出 `RuntimeError`。
    断言四件事：

      - 退出码是 `EXIT_FAILURE`（起步失败与收尾失败同档）—— 这一条同时咬住「`finally` 把它
        静默吞成 `EXIT_OK`」那条老毛病；
      - 终端上写明「服务起步失败」并给出 log 路径与出路（§8 的「不许静默」）；
      - 服务级 log 上落一行 error（这一行由 `cli` 的 `logger.exception` 落）；
      - 同一档不再往下走：`server.run()` 一次都没被调到（`ServiceServer` 没被构造）。
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


# ---------------------------------------------------------------------------
# 退出码逐档写死（含「收尾干净 + `server.run()` 抛异常」这个组合）—— 审查问题 M4
#
# 契约 §3.5 的「退出码落地要求」把 `web` 分支的三档写死：空闲退出档与收尾干净档 `EXIT_OK`、
# 收尾失败档 `EXIT_FAILURE`、强制打断档 `EXIT_INTERRUPTED`，一律显式 `sys.exit(<常量标识符>)`。
# `meeting/cli.py` 的 `_drain_exit_code(runtime, fallback=...)` 就是挑这一档的那一处：
# 下面把三档逐档写死，再把 `fallback`（`server.run()` 抛异常那一支）叠上去看谁说了算。
# ---------------------------------------------------------------------------


def _exit_code_runtime(*, interrupted: bool = False, statuses: tuple[str, ...] | None = ()):
    """`_drain_exit_code()` 要的那两处读数（`shutdown_interrupted` 与 `snapshot.ended.steps`）。

    `statuses=None` 表示手上还没有收尾现场（`ended` 为空）；其余情形按给定的步骤状态造一版。
    """
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

    这一条盯的是审查问题 M4：`fallback` 原先只写在一句「三档都判不出来时沿用」的 docstring 里，
    实现那一行却是「收尾干净就把兜底值丢掉」的形状 —— 于是「服务异常退出」这件事在收尾干净时
    被静默抹掉。现在语义与实现一致：三档照契约挑，`fallback` 带的是**另一路**信息
    （`server.run()` 抛异常时 `cli` 的 `finally` 传进来的 `EXIT_FAILURE`），它原样沿用。

    三档照契约逐档写死（空闲态 / 收尾干净 / 收尾有步骤 `failed` / 强制打断），再把
    「`server.run()` 抛异常」这一支叠在「收尾干净」与「空闲态」两组上，看谁说了算。
    """
    # ① 空闲退出档：手上没有场次，无收尾可做 → EXIT_OK
    assert cli_module._drain_exit_code(_exit_code_runtime(statuses=None)) == EXIT_OK
    # ② 收尾干净档：四步都落在 done / skipped → EXIT_OK
    assert (
        cli_module._drain_exit_code(
            _exit_code_runtime(statuses=("done", "done", "skipped")),
            fallback=EXIT_OK,
        )
        == EXIT_OK
    )
    # ③ 收尾失败档：有步骤 failed → EXIT_FAILURE（这一档与兜底值无关）
    assert (
        cli_module._drain_exit_code(
            _exit_code_runtime(statuses=("done", "done", "failed")),
            fallback=EXIT_OK,
        )
        == EXIT_FAILURE
    )
    # ④ 强制打断档：本次关停被标成「强制打断」→ EXIT_INTERRUPTED，兜底值不参与
    assert (
        cli_module._drain_exit_code(
            _exit_code_runtime(interrupted=True, statuses=("done", "waiting")),
            fallback=EXIT_FAILURE,
        )
        == EXIT_INTERRUPTED
    )
    # ⑤ **审查问题 M4 的那一组**：收尾干净 + `server.run()` 抛异常 → EXIT_FAILURE 原样交出，
    # 收尾干净这一格抹不掉它（若把兜底值丢掉，这一行会拿到 EXIT_OK）
    assert (
        cli_module._drain_exit_code(
            _exit_code_runtime(statuses=("done", "done", "skipped")),
            fallback=EXIT_FAILURE,
        )
        == EXIT_FAILURE
    )
    # ⑥ 同一个组合落在空闲退出档上：空闲态 + `server.run()` 抛异常，沿用同一个兜底常量
    assert (
        cli_module._drain_exit_code(
            _exit_code_runtime(statuses=None), fallback=EXIT_FAILURE
        )
        == EXIT_FAILURE
    )
    # 兜底常量的取值由 ③ 那一档背书（`cli_module` 与裸名同源，自比一遍没有新信息）


# ---------------------------------------------------------------------------
# 终端文案按**现场档位**说话（**进程内纯函数级**）—— 审查问题 M1
#
# `_drain_exit_code()` 这一档里压着**两件正交的事**：「收尾有步骤 `failed`」与
# 「`server.run()` 自己抛了异常」。修 M1 之前 `_report_drain_outcome()` 只看
# `exit_code != EXIT_FAILURE` 就早退，于是「收尾各步全绿、只是服务器自己炸了」的现场也会在
# 终端打出「收尾有步骤失败，服务不自杀（现场仍在）」与「出路一：`meeting post <场次目录>`」
# —— 那两句话与现场（盘上是 `done`）对不上。
#
# 现在判据落在**现场**上（`ended.steps` 里找得到 `failed` 才算收尾失败档，见
# `meeting/cli.py` 的 `_drain_reported_failed()`）；「服务异常退出」那一路由 `web()` 的
# `except` 自己说清（`logger.exception("服务异常退出")` + log 路径），两条路各说各的事。
# ---------------------------------------------------------------------------


def test_drain_report_speaks_for_the_real_face_not_the_exit_code(capsys):
    """终端那套「收尾失败」文案只在**现场真有 `failed` 步**时出现（M1）。

    三组输入逐组写死（都是 `exit_code = EXIT_FAILURE` 的现场，只有现场档位不同）：

      1. **收尾干净 + `server.run()` 抛异常**（盘上是 `done`；这一组就是 M1 的那一格）：
         终端上**一个字都不打** —— 不出现「收尾有步骤失败，服务不自杀（现场仍在）」、
         不出现「出路一：`meeting post <场次目录>`」、不出现「服务 log 最后 N 行」，
         服务级 log 上也不落那行「收尾失败档退出」（那一档的 log 归属 `web()` 的异常分支）；
      2. **收尾有步骤 `failed`**：同一套文案照旧打全（判据按现场读，没有把该说的话一起收掉）；
      3. **`ended` 为空**（空闲态退出这一格）：同样一个字都不打。
    """
    logger = _FakeLogger()
    clean = _exit_code_runtime(statuses=("done", "done", "skipped"))
    failed = _exit_code_runtime(statuses=("done", "done", "failed"))
    idle = _exit_code_runtime(statuses=None)

    # 现场档位的读法（`cli.py` 的判据）：干净档与空闲档都不是收尾失败档
    assert cli_module._drain_reported_failed(clean) is False
    assert cli_module._drain_reported_failed(idle) is False
    assert cli_module._drain_reported_failed(failed) is True

    # ① 收尾干净 + `server.run()` 抛异常（`fallback` 把 EXIT_FAILURE 交出来）
    assert cli_module._drain_exit_code(clean, fallback=EXIT_FAILURE) == EXIT_FAILURE
    cli_module._report_drain_outcome(clean, logger, EXIT_FAILURE)
    terminal_clean = capsys.readouterr().err
    assert terminal_clean == "", (
        "收尾干净、只是服务器自己炸了：这套「收尾有步骤失败」的文案不该出现 —— "
        f"终端上打出了：{terminal_clean!r}"
    )
    assert not [item for item in logger.records if item[0] == "error"], logger.records

    # ② 收尾有步骤 `failed`：同一套文案照旧打全（判据看现场，没有误伤这一档）
    cli_module._report_drain_outcome(failed, logger, EXIT_FAILURE)
    terminal_failed = capsys.readouterr().err
    assert "收尾有步骤失败，服务不自杀（现场仍在）" in terminal_failed, terminal_failed
    assert f"log 路径：{failed.log_path}" in terminal_failed, terminal_failed
    assert "出路一：meeting post <场次目录>" in terminal_failed, terminal_failed
    assert [item for item in logger.records if item[0] == "error"], logger.records

    # ③ `ended` 为空（空闲态退出这一格）：同样一个字都不打
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


# ---------------------------------------------------------------------------
# 强制打断档 + 现场前面有 `failed` 步 → 终端说**真实档位**，不说「服务不自杀」（审查问题 M11）
#
# `_report_drain_outcome()` 原先只按**现场**挑话（`ended.steps` 里有没有 `failed`），完全不看
# 档位：强制打断档（`EXIT_INTERRUPTED`）里若前面已经有一步 `failed`（本例就是 `post`），终端
# 上打出的却是「收尾有步骤失败，服务不自杀（现场仍在）」—— 而这一趟是**被强制打断**的、盘上
# 留在 `draining` + `interrupted`、进程正在关停，那句话与事实对不上。这一条把这一格逐句写死。
# ---------------------------------------------------------------------------


def test_interrupted_report_speaks_for_the_forced_interrupt_not_the_failed_face(capsys):
    """**强制打断档 + 现场有 `failed` 步** → 终端照着真实档位说话（审查问题 M11）。

    现场逐项照实造（进程内纯函数级，不占墙钟）：

      - `shutdown_interrupted=True`（第二次 `Ctrl+C` / 已停在 `draining` 再按那一档），
        `ended.steps` 里 `post` 是 `failed`（前面已经失败过一步，`flush` / `log`
        都已有终态）—— `_drain_reported_failed()` 因此是 `True`，正是会误报「服务不自杀」的那一格；
      - `_drain_exit_code()` 对这一组报 `EXIT_INTERRUPTED`（档位压过现场：强制打断那一支先判）。

    四件事一起断：退出码是强制打断档；终端说「收尾被强制打断」；**不出现**「服务不自杀」那句；
    终端带上 log 路径与两条出路（`meeting post` / 重启服务），并落一行 log 记下这一次打了什么。
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
