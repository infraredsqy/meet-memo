"""端口策略、命令面与退出码。

**子进程里一律不许出现真浏览器**：要走到「打开浏览器」这一步的用例（拿掉
`MEETING_NO_BROWSER` 的那一条）经 `fake_browser_module()` 注入一份假的 `webbrowser`
模块，`BROWSER=no-such-browser` 那套在 Windows 上会退到宿主默认浏览器，已经废掉。
"""

from __future__ import annotations

import importlib
import signal
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import httpx
import pytest

from tests import _helpers as helpers
from tests._service import ServiceProcess
from meeting import cli as cli_module
from meeting.cli import EXIT_OK, EXIT_REJECTED
from meeting.service import app as service_app

pytestmark = pytest.mark.slow

DEFAULT_PORT = 8765
SELF_CHECK_LINE = "信号接管自检通过：SIGINT 与 SIGBREAK 指向本服务的处理函数"
SIGBREAK_LINE = "收到 SIGBREAK，进入关停"
BIND_PREFIX = "绑定成功："


def _wait_heartbeat(port: int, timeout: float = 20.0) -> httpx.Response:
    def ok():
        try:
            response = helpers.local_get(f"http://127.0.0.1:{port}/api/heartbeat", timeout=1.0)
        except Exception:
            return None
        return response if response.status_code == 200 else None

    return helpers.wait_until(ok, timeout=timeout, what="GET /api/heartbeat 200")


def _bind_line(log_text: str) -> str:
    lines = helpers.lines_with(log_text, BIND_PREFIX)
    assert lines, f"服务级主 log 里没有绑定那一行：\n{log_text}"
    return lines[-1]


def _free_ephemeral_port() -> int:
    """向系统要一个当前空闲的临时端口（用完就放掉）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.slow
def test_service_starts_and_reports_selected_port(tmp_path):
    """起得来：终端有 URL / log 路径两样；选中端口可问 heartbeat。"""
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {"web.port": 0})

    proc = helpers.spawn_service(home)
    try:
        proc.wait_ready()
        stdout = proc.stdout()
        assert "URL：" in stdout
        assert "log 路径：" in stdout

        port = proc.port()
        response = _wait_heartbeat(port)
        assert response.status_code == 200
        assert response.json()["service"] == "meeting"
        assert response.headers["cache-control"] == "no-store"
    finally:
        proc.terminate()


@pytest.mark.slow
def test_default_port_is_8765(tmp_path):
    """默认端口 8765（什么都不配时）。"""
    if not helpers.can_bind(DEFAULT_PORT):
        pytest.skip(f"端口 {DEFAULT_PORT} 在本机被别的进程占着，这一条没法验证默认值")

    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {})

    proc = helpers.spawn_service(home)
    try:
        proc.wait_ready()
        assert f"127.0.0.1:{DEFAULT_PORT}" in _bind_line(proc.log_text())
        assert _wait_heartbeat(DEFAULT_PORT).status_code == 200
    finally:
        proc.terminate()


@pytest.mark.slow
def test_web_port_from_config_json(tmp_path):
    """`web.port` 能改。"""
    port = _free_ephemeral_port()
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {"web.port": port})

    proc = helpers.spawn_service(home)
    try:
        proc.wait_ready()
        assert f"127.0.0.1:{port}" in _bind_line(proc.log_text())
        assert _wait_heartbeat(port).status_code == 200
    finally:
        proc.terminate()


@pytest.mark.slow
def test_meeting_port_env_overrides_config_json(tmp_path):
    """`MEETING_PORT` 压过 `web.port`（环境变量 > config.json）。"""
    env_port = _free_ephemeral_port()
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {"web.port": 8765})

    proc = helpers.spawn_service(home, env_extra={"MEETING_PORT": str(env_port)})
    try:
        proc.wait_ready()
        log_text = proc.log_text()
        assert f"127.0.0.1:{env_port}" in _bind_line(log_text)
        assert "环境变量 MEETING_PORT" in log_text
        assert _wait_heartbeat(env_port).status_code == 200
    finally:
        proc.terminate()


@pytest.mark.slow
def test_port_zero_lets_os_choose_and_prints_it(tmp_path):
    """`0` 让系统挑空闲端口，**选中的端口照常打印并落 log**（不许把 `0` 写进快照）。"""
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {"web.port": 0})

    proc = helpers.spawn_service(home)
    try:
        proc.wait_ready()
        port = proc.port()
        assert port > 0
        assert f"127.0.0.1:{port}" in _bind_line(proc.log_text())

        snapshot = helpers.local_get(f"http://127.0.0.1:{port}/api/state").json()
        assert snapshot["service"]["port"] == port
        assert snapshot["service"]["url"] == f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()


@pytest.mark.slow
def test_bind_address_from_log_is_loopback(tmp_path):
    """① 从服务级主 log 的绑定那一行读回真实 host：必须是 `127.0.0.1`。"""
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {"web.port": 0})

    proc = helpers.spawn_service(home)
    try:
        proc.wait_ready()
        line = _bind_line(proc.log_text())
        endpoint = line.split(BIND_PREFIX, 1)[1].split("（", 1)[0].strip()
        host, _, port_text = endpoint.rpartition(":")
        assert host == "127.0.0.1"
        assert int(port_text) == proc.port()
    finally:
        proc.terminate()


@pytest.mark.slow
def test_non_loopback_address_refuses_connection(tmp_path):
    """② 枚举出的非回环地址连不上（用 `ConnectionRefusedError` 判，不接受「超时」）。"""
    candidates = helpers.non_loopback_ipv4_addresses()
    if not candidates:
        pytest.skip("本机只有回环地址，没有可用的非回环候选（绑定地址那一条仍然必须通过）")

    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {"web.port": 0})

    proc = helpers.spawn_service(home)
    try:
        proc.wait_ready()
        port = proc.port()
        with pytest.raises(ConnectionRefusedError):
            helpers.tcp_connect(candidates[0], port, timeout=3.0)
    finally:
        proc.terminate()


@pytest.mark.slow
def test_host_header_is_checked(tmp_path):
    """带 `Host: evil.example` 的请求得 403（挡 DNS rebinding）。"""
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {"web.port": 0})

    proc = helpers.spawn_service(home)
    try:
        port = proc.wait_ready_by_heartbeat()
        response = helpers.local_get(
            f"http://127.0.0.1:{port}/api/heartbeat",
            headers={"Host": "evil.example"},
        )
        assert response.status_code == 403
    finally:
        proc.terminate()


@pytest.mark.slow
def test_port_taken_by_meeting_service_is_rejected(tmp_path):
    """占用者应答出 meeting 形状 → 拒绝启动、文案是「已经有一个 meeting 服务在跑…（当前空闲）」。"""
    try:
        fake = helpers.FakeMeetingServer(DEFAULT_PORT).start()
    except OSError as exc:
        pytest.skip(f"meeting 形状占用者没法占住端口 {DEFAULT_PORT}（{exc}）")
    try:
        home = tmp_path / "home"
        home.mkdir()
        helpers.write_config_json(home, {})

        result = helpers.run_web_expecting_failure(home)

        assert result.returncode == EXIT_REJECTED
        combined = result.stdout + result.stderr
        assert f"已经有一个 meeting 服务在跑：http://127.0.0.1:{DEFAULT_PORT}" in combined
        assert "（当前空闲）" in combined
    finally:
        fake.stop()


@pytest.mark.slow
def test_port_taken_by_other_program_is_rejected(tmp_path):
    """占用者是别的程序 → 拒绝启动、写明端口被谁占着与怎么改（示例命令）。"""
    try:
        holder = helpers.OccupiedPort(DEFAULT_PORT)
    except OSError as exc:
        pytest.skip(f"普通 socket 没法占住端口 {DEFAULT_PORT}（{exc}）")
    try:
        home = tmp_path / "home"
        home.mkdir()
        helpers.write_config_json(home, {})

        result = helpers.run_web_expecting_failure(home)

        assert result.returncode == EXIT_REJECTED
        combined = result.stdout + result.stderr
        assert f"端口 {DEFAULT_PORT} 被别的程序占着" in combined
        assert "MEETING_PORT=8766 meeting web" in combined
    finally:
        holder.stop()


@pytest.mark.slow
def test_probe_total_timeout_is_one_second(tmp_path):
    """接受连接却不应答 → 按「别的程序占着」处理；**墙钟断言**：起子进程到退出 ≤ 5 秒。"""
    try:
        silent = helpers.SilentListener(DEFAULT_PORT).start()
    except OSError as exc:
        pytest.skip(f"静默监听者没法占住端口 {DEFAULT_PORT}（{exc}）")
    try:
        home = tmp_path / "home"
        home.mkdir()
        helpers.write_config_json(home, {})

        started = time.monotonic()
        result = helpers.run_web_expecting_failure(home)
        elapsed = time.monotonic() - started

        assert result.returncode == EXIT_REJECTED
        assert f"端口 {DEFAULT_PORT} 被别的程序占着" in (result.stdout + result.stderr)
        assert elapsed <= 5.0, f"探针的 1 秒总超时没兜住：起子进程到退出用了 {elapsed:.2f}s"
    finally:
        silent.stop()


def test_no_action_word_prints_usage_and_exits_rejected(tmp_path):
    """`meeting` 不带参数 → 用法说明 + `EXIT_REJECTED`。"""
    home = tmp_path / "home"
    home.mkdir()

    result = helpers.run_cli(home, action=None)

    assert result.returncode == EXIT_REJECTED
    combined = result.stdout + result.stderr
    assert "用法" in combined
    assert "web" in combined
    assert "post" in combined


def test_undefined_action_word_exits_rejected(tmp_path):
    """`meeting serve`（未定义动作词）→ 用法说明 + `EXIT_REJECTED`。"""
    home = tmp_path / "home"
    home.mkdir()

    result = helpers.run_cli(home, action="serve")

    assert result.returncode == EXIT_REJECTED
    combined = result.stdout + result.stderr
    assert "用法" in combined
    assert "serve" in combined


def test_exit_code_constants_match_the_frozen_table():
    """五档常量的取值就是冻结表里的 0 / 1 / 2 / 3 / 130（本文件只引用常量名）。"""
    assert cli_module.EXIT_OK == 0
    assert cli_module.EXIT_FAILURE == 1
    assert cli_module.EXIT_REJECTED == 2
    assert cli_module.EXIT_STARTUP_UNMET == 3
    assert cli_module.EXIT_INTERRUPTED == 130


@pytest.mark.slow
def test_idle_ctrl_break_event_exits_ok(tmp_path):
    """空闲态发 `CTRL_BREAK_EVENT` → `EXIT_OK` + 两行 log（信号处理函数真的生效的证据）。"""
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {"web.port": 0})

    proc = ServiceProcess(home)
    try:
        port = proc.wait_ready_by_heartbeat()
        assert port > 0
        proc.wait_ready()
        proc.send_interrupt()
        proc.wait(timeout=20)
    finally:
        proc.terminate()

    assert proc.poll() == EXIT_OK
    log_text = proc.log_text()
    assert SIGBREAK_LINE in log_text
    assert SELF_CHECK_LINE in log_text
    assert "退出原因与退出码：" in log_text


# ---------------------------------------------------------------------------
# `meeting` 命令面的最小验证（R15 的对外命令面，且不要求安装）
# ---------------------------------------------------------------------------


def test_console_script_entry_is_callable_and_shared():
    """`pyproject.toml` 的 `[project.scripts] meeting` 与 `meeting.cli.main` 是同一个入口。"""
    pyproject = Path(helpers.REPO_ROOT) / "pyproject.toml"
    with open(pyproject, "rb") as handle:
        data = tomllib.load(handle)

    target = data["project"]["scripts"]["meeting"]
    assert target == "meeting.cli:main"

    module_name, _, attr = target.partition(":")
    resolved = getattr(importlib.import_module(module_name), attr)

    entry_module = importlib.import_module("meeting.__main__")
    assert entry_module.main is cli_module.main
    assert getattr(entry_module, "main") is resolved


def _requirement_name(requirement: str) -> str:
    """一条依赖声明里的项目名（`"av>=14"` / `"av==14.0.0"` → `"av"`）。"""
    for separator in ("==", ">=", "<=", "~=", "!=", ">", "<", "[", ";"):
        requirement = requirement.split(separator, 1)[0]
    return requirement.strip()


def test_probe_helper_only_accepts_the_meeting_shape():
    """探针只认「状态码 200 + body 带 `"service": "meeting"`」，别的一律按别的程序算。"""
    other_app = helpers.FakeMeetingServer(_free_ephemeral_port(), service_name="something-else")
    other_app.start()
    try:
        assert service_app.probe_meeting_service("127.0.0.1", other_app.port) is False
    finally:
        other_app.stop()

    fake = helpers.FakeMeetingServer(_free_ephemeral_port())
    fake.start()
    try:
        assert service_app.probe_meeting_service("127.0.0.1", fake.port) is True
    finally:
        fake.stop()
