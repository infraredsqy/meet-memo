"""`tests/test_port_policy.py` —— 端口策略、命令面与退出码（phase1 §5 第 1 / 2 / 6 / 7 条）。

覆盖：
  - 默认 8765；`web.port` / `MEETING_PORT` 能改；`0` 让系统挑并打印 / 落 log 选中端口；
  - **只监听 127.0.0.1**（两条独立断言：主 log 绑定行的 `getsockname` host、枚举出的非回环地址被拒）；
  - 两种占用情形的文案与**退出码 `EXIT_REJECTED`**；探针 **1 秒总超时**（另断一条 ≤ 5 秒墙钟）；
  - **用法错误 → `EXIT_REJECTED`**（无参数 / 未定义动作词）；
  - **空闲态 `CTRL_BREAK_EVENT` → `EXIT_OK`** + `收到 SIGBREAK，进入关停` + 信号自检通过两行 log；
  - `install_shutdown_signal_handlers()` 后 `getsignal(SIGINT) is getsignal(SIGBREAK)`（用后复原）；
  - **`meeting` 命令面的最小验证**（`[project.scripts]` + 可调用对象 `is` 同一个入口）；
  - **`post` extra 的依赖声明**（`[project.optional-dependencies].post` 在场且含 `av`，
    模型依赖不在这里重复列）；
  - 启动序列第 5 步「打开浏览器」在真子进程里的两种情形（`MEETING_NO_BROWSER=1` 跳过 /
    打开失败只警告且服务照常起来）。**子进程里一律不许出现真浏览器**：要走到「打开浏览器」
    这一步的用例（拿掉 `MEETING_NO_BROWSER` 的那一条）经 `fake_browser_module()` 注入一份假的
    `webbrowser` 模块，`BROWSER=no-such-browser` 那套在 Windows 上会退到宿主默认浏览器，
    已经废掉（见 `tests/_helpers.py` 的文件头说明）。

`EXIT_STARTUP_UNMET` 这一档在本文件里**不出现**（§4.4 的落点分工）。
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

#: 慢档标记：`pytest.mark.slow` 的归位判据与实测数据见 `docs/service.md` 第 7 章。
pytestmark = pytest.mark.slow

DEFAULT_PORT = 8765
SELF_CHECK_LINE = "信号接管自检通过：SIGINT 与 SIGBREAK 指向本服务的处理函数"
SIGBREAK_LINE = "收到 SIGBREAK，进入关停"
BIND_PREFIX = "绑定成功："


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 起得来、选中的端口说得出
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 只监听 127.0.0.1（两条独立断言）
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 端口被占的两种情形（都以 EXIT_REJECTED 结束）
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 启动序列第 5 步「打开浏览器」（真子进程；§1.1 / §3.5 第 5 条）
# ---------------------------------------------------------------------------


def test_browser_step_skipped_in_subprocess_when_disabled(tmp_path):
    """`MEETING_NO_BROWSER=1` → 真子进程里这一步被跳过，主 log 落一行说明跳过。"""
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {"web.port": 0})

    proc = helpers.spawn_service(home, env_extra={"MEETING_NO_BROWSER": "1"})
    try:
        port = proc.wait_ready_by_heartbeat()
        assert port > 0
        trace = helpers.wait_until(
            lambda: helpers.open_browser_step_trace(home) or None,
            timeout=20,
            what="主 log 里出现打开浏览器那一步的行",
        )
    finally:
        proc.terminate()

    assert "MEETING_NO_BROWSER=1，跳过打开浏览器" in trace
    assert "打开浏览器失败" not in trace, f"关掉时不该尝试打开：{trace}"


def test_browser_failure_only_warns_in_subprocess(tmp_path):
    """子进程里打开浏览器失败（假 `webbrowser` 按测试要求抛错）→ 只警告，服务照常起来。

    **子进程里不许出现真浏览器**：这里经 `fake_browser_module()` 把一份假的 `webbrowser`
    模块前置注入子进程，子进程的 `webbrowser.open()` 因此是假替身 —— 它只记录、只按测试
    要求返回或抛错。这一支同时把「这一步真的被调到了」与「失败不阻塞」两件事钉在一起：
    `calls` 文件里有一行 = 子进程确实走到了这一步（这几行由假替身写，真浏览器碰不到）。
    """
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {"web.port": 0})

    with helpers.fake_browser_module(tmp_path, home=home, outcome="raise") as fake:
        # 拿掉 MEETING_NO_BROWSER：这一步必须真的被调到，验的才是「失败只警告」那一支
        env = {"MEETING_NO_BROWSER": ""}
        env.update(helpers.fake_browser_env(fake))
        proc = helpers.spawn_service(home, env_extra=env)
        try:
            port = proc.wait_ready_by_heartbeat()
            assert port > 0, "这一步失败也不许把服务拖住：它必须照常起来"
            response = helpers.local_get(f"http://127.0.0.1:{port}/api/state")
            assert response.status_code == 200
            trace = helpers.wait_until(
                lambda: helpers.open_browser_step_trace(home) or None,
                timeout=20,
                what="主 log 里出现打开浏览器失败那一行",
            )
            assert proc.poll() is None, "打开浏览器失败不许让服务退出"
        finally:
            proc.terminate()

    assert "打开浏览器失败（只警告、不阻塞）" in trace
    assert fake.calls(), "子进程里的 webbrowser.open() 应当被调到（假替身会把它记下来）"
    assert fake.loaded(), "子进程命中的应当是注入的那份假 webbrowser（marker 会写一行）"
    assert "模拟没有可用的浏览器" in trace


def test_fake_browser_module_shadows_the_stdlib_in_subprocess(tmp_path):
    """注入设施本身要先证得动：子进程里 `import webbrowser` 命中的是注入的那一份。

    这条与「打开浏览器」那一步无关，它只钉住隔离设施：假模块被导入时会写 marker，
    标准库那份不会。设施一旦失灵（例如 `PYTHONPATH` 排到了标准库后面），这里先红，
    而不是等到某条用例真把窗口弹出来才发现。
    """
    with helpers.fake_browser_module(tmp_path, home=tmp_path / "home") as fake:
        result = subprocess.run(
            [sys.executable, "-c", "import webbrowser; print(webbrowser.__file__)"],
            cwd=str(helpers.REPO_ROOT),
            env=helpers.service_env(tmp_path / "home", extra=helpers.fake_browser_env(fake)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )

    assert result.returncode == 0, result.stderr
    assert str(fake.module_dir) in result.stdout, (
        f"子进程里的 webbrowser 应当是注入的那一份：{result.stdout!r}"
    )
    assert fake.loaded(), "假模块被载入时会写 marker，这里没写说明命中的不是它"


# ---------------------------------------------------------------------------
# 用法错误 / post 桩
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 信号接管
# ---------------------------------------------------------------------------


def test_sigint_and_sigbreak_share_handle_shutdown_signal():
    """进程内断言：装上之后 SIGINT 与 SIGBREAK 都 `is handle_shutdown_signal`（用后复原）。

    两个信号各自断言一次身份，两者的同一性由这两条传递可得。
    """
    prev_int = signal.getsignal(signal.SIGINT)
    prev_break = signal.getsignal(getattr(signal, "SIGBREAK", signal.SIGINT))
    try:
        service_app.install_shutdown_signal_handlers()
        assert signal.getsignal(signal.SIGINT) is service_app.handle_shutdown_signal
        if hasattr(signal, "SIGBREAK"):
            assert signal.getsignal(signal.SIGBREAK) is service_app.handle_shutdown_signal
    finally:
        signal.signal(signal.SIGINT, prev_int)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, prev_break)


def test_idle_ctrl_break_event_exits_ok(tmp_path):
    """空闲态发 `CTRL_BREAK_EVENT` → `EXIT_OK` + 两行 log（信号处理函数真的生效的证据）。

    **与 phase3 的信号语义同源**（§4 落点表 `tests/test_port_policy.py` 行点名的同源替换，
    审查问题 M6）：子进程与信号辅助改用 `tests/_service.py` 的 `ServiceProcess`
    （`send_interrupt()` 发的同样是 `CTRL_BREAK_EVENT`，退出码走 `wait()` /
    `poll()`）—— 此前这里走的是 `tests/_helpers.py` 的 `spawn_service()` / `send_break()`，
    两者机制本来相同，却让「空闲态 `Ctrl+C`」这一档在仓库里有两套写法。**只换辅助来源**：
    下面的断言与语义一个字没动（同一条退出码常量、同一行 log、同一条「输出根不凭空多目录」
    的自检）。

    **授权来源（第七轮审查 m4）**：phase3 §4 落点表把这一处动作明写成「同源替换：只同步
    断言、不改 phase1 的契约取值」；端口策略的两种占用文案、探针 1 秒超时、其余退出码档位
    取值与 `getsignal(SIGINT) is getsignal(SIGBREAK)` 那一条都不动。
    """
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

    # `python -m meeting` 也指向同一个入口（解析 `模块:属性` 得到的对象与 `meeting.cli.main` 同一）
    entry_module = importlib.import_module("meeting.__main__")
    assert entry_module.main is cli_module.main
    assert getattr(entry_module, "main") is resolved


def _requirement_name(requirement: str) -> str:
    """一条依赖声明里的项目名（`"av>=14"` / `"av==14.0.0"` → `"av"`）。"""
    for separator in ("==", ">=", "<=", "~=", "!=", ">", "<", "[", ";"):
        requirement = requirement.split(separator, 1)[0]
    return requirement.strip()


def test_post_extra_declares_the_av_decoder():
    """`pyproject.toml` 的 `[project.optional-dependencies]` 里有 `post`，且含 `av`。

    这条断言只读那一条包装声明：`meeting/offline/audio.py` 在函数体内
    `import av`，`engine.py` 的 `transcribe()` 每次都会走那个解码器。少了这一格，
    `pip install "meeting[post]"` 直接失败、装了也每条音频路径都落
    `engine_error: ModuleNotFoundError` —— 用例全绿证明不了这件事（「范围」表与自检
    第 9 条因此要求本文件把 `post` extra 钉住）。

    **只放必要的那一条**：模型依赖已经在 `asr` 那一格里（会后离线路径与会中转写共用），
    在这里重复列一遍会让人以为两格各装各的；`librosa` 是有意避开的（`numba` 与钉死的
    numpy 冲突），它的缺席出自设计上的取舍。
    """
    pyproject = Path(helpers.REPO_ROOT) / "pyproject.toml"
    with open(pyproject, "rb") as handle:
        data = tomllib.load(handle)

    extras = data["project"]["optional-dependencies"]
    assert "post" in extras, f"`post` extra 不在场：{sorted(extras)}"
    names = {_requirement_name(item) for item in extras["post"]}
    assert "av" in names, f"`post` extra 里没有 av：{extras['post']}"
    assert names == {"av"}, f"`post` extra 该只有 av 一条：{sorted(names)}"
    for duplicated in ("torch", "transformers", "webrtcvad-wheels", "librosa"):
        assert duplicated not in names, f"{duplicated} 不该出现在 `post` extra 里：{extras['post']}"


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
