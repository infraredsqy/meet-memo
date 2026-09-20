"""`tests/conftest.py` —— **只放 fixture**（phase2 §4 / §5.4 的五个夹具名在本 Phase 冻结）。

五个夹具（名字与语义由 phase2 定义并冻结，phase3 只复用、不得重定义同名 fixture）：

  - `service_process`：**真服务子进程夹具**（内部包 `tests/_service.py` 的 `ServiceProcess`，
    起进程即传环境变量覆盖、家目录 `HOME` 与 `USERPROFILE` 两项都设）；
  - `app_client`：**应用客户端**（`TestClient(app, base_url="http://127.0.0.1")` 形态）；
  - `dev_runtime`：**开发替身运行时**（`MEETING_CAPTURE=dev` 的运行时实例）；
  - `tmp_out_root`：**临时输出根**；
  - `isolated_config_dir`：**隔离的临时配置目录**（连同隔离的临时日志目录）。

**本文件只放 fixture**：`parse_sse_frame()` / `read_sse_frames()` / `wait_until()` 这类解析与
等待助手、`ServiceProcess` / `SseClient` / `post_action()` 这几个驱动件的实现**都在
`tests/_service.py`**，这里只 import 并包装成 fixture。

## 浏览器兜底（安全要求，autouse）

pytest **父进程**里 `meeting.service.app.webbrowser` 就是**标准库那个模块对象**：谁让
`launch_browser()` 跑起来而没先把 `webbrowser.open` 换掉，那一下就会落到宿主机器的默认浏览器上
（本项目已经因此误触过一次用户的 Chrome）。因此这里装一个 **autouse** 夹具
`_no_real_browser`，默认把 `meeting.service.app.webbrowser.open`（`launch_browser()` 唯一
会走到的那个属性）换成一个**只记录、绝不开窗**的假替身，测试结束自动还原。
需要真行为的用例在**自己的用例里**显式覆盖它（`monkeypatch.setattr` 再用自己的假替身，
或用本夹具提供的 `browser_calls` 记录）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

import pytest

import _helpers as helpers
from _service import ServiceProcess


# ---------------------------------------------------------------------------
# 安全兜底：绝不许触达真实浏览器
# ---------------------------------------------------------------------------


class _RecordingBrowser:
    """进程内的假 `webbrowser`：`open()` 只把 URL 记进 `calls`，**永不打开任何东西**。"""

    #: 与 `tests/_helpers.py` 的 `ServiceBrowser("safe")` 同一口径：返回 `False`
    outcome = "safe"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def open(self, url: Any, *args: Any, **kwargs: Any) -> bool:
        self.calls.append(str(url))
        if self.outcome == "raise":
            raise RuntimeError("假 webbrowser（conftest 兜底）：按测试要求模拟没有可用的浏览器")
        return self.outcome == "open"


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch: pytest.MonkeyPatch) -> Iterator[_RecordingBrowser]:
    """**autouse 兜底**：把 `meeting.service.app.webbrowser.open` 换成只记录的假替身。

    这条保护与 `tests/_helpers.py` 里 `app_client()` 自己的替换是**两层**（那一层在上下文
    退出时会把真 `open` 放回来，之后仍然由本夹具这一层兜着），因此 pytest 进程里任何一条
    路径都碰不到真浏览器。用例需要验「打开浏览器」这一步的行为时，显式再覆盖一次即可。
    """
    from meeting.service import app as service_app

    fake = _RecordingBrowser()
    monkeypatch.setattr(service_app.webbrowser, "open", fake.open, raising=False)
    yield fake


# ---------------------------------------------------------------------------
# 五个夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_out_root(tmp_path: Path) -> Path:
    """**临时输出根**（场次目录只在 `start` 那一刻建在它下面）。"""
    root = tmp_path / "out"
    root.mkdir(parents=True, exist_ok=True)
    return root


#: 本用例的临时家目录名（`isolated_config_dir` 与 `app_client` **共用这一个**）
CASE_HOME_DIR_NAME = "home"


def case_home(tmp_path: Path) -> Path:
    """本用例的临时家目录（两个夹具都从这里取，谁先来谁建）。

    家目录隔离的硬约束（phase2 §5.4）：`HOME` 与 `USERPROFILE` 两个变量都指向它。
    两个夹具同时使用时必须指向**同一个**目录，否则「隔离」这件事在其中一个夹具的视角下就
    落到另一个目录上了（配置与日志读写的落点会与断言的落点错开）。
    """
    home = Path(tmp_path) / CASE_HOME_DIR_NAME
    home.mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture()
def isolated_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """**隔离的临时配置目录**：家目录重定向 + 隔离的临时日志目录。

    隔离手法照 phase2 §5.4 的硬约束：**重定向家目录**（`HOME` 与 `USERPROFILE` 两项都设），
    再叠加临时 log 目录；**不新增任何 `MEETING_*` 变量**。家目录取 `case_home()`，与
    `app_client` 用的是同一个。
    """
    home = case_home(tmp_path)
    config_dir = home / ".config" / "meeting"
    config_dir.mkdir(parents=True, exist_ok=True)
    log_dir = config_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("MEETING_LOG_DIR", str(log_dir))
    return config_dir


@pytest.fixture()
def service_process(tmp_path: Path) -> Iterator[Any]:
    """**真服务子进程夹具**：给出一个「起一个 `meeting web` 子进程」的工厂。

    用法：``with service_process(capture="dev", out_root=...) as proc: proc.wait_ready()``
    或 ``proc = service_process(...)``（工厂自己注册收场，用例不必写 `finally`）。

    能力（phase2 冻结）：`base_url`（`MEETING_PORT=0` + 子进程启动输出解析）、
    向子进程发中断信号（`send_interrupt()` / `send_ctrl_c()`）、读退出码（`returncode` /
    `wait()`）、**传完整的环境变量覆盖**（`env=` 给全一份映射；`PYTHONPATH` 默认 = 仓库根，
    需要假件时由调用方把「仓库根 + 临时假件目录」拼好传进来）。
    """
    started: list[ServiceProcess] = []
    counter = {"n": 0}

    def factory(**kwargs: Any) -> ServiceProcess:
        counter["n"] += 1
        home = kwargs.pop("home", None) or (tmp_path / f"home{counter['n']}")
        kwargs.setdefault("capture", "dev")
        kwargs.setdefault("out_root", tmp_path / f"out{counter['n']}")
        proc = ServiceProcess(Path(home), **kwargs)
        started.append(proc)
        return proc

    try:
        yield factory
    finally:
        for proc in started:
            try:
                proc.terminate()
            except Exception:  # pragma: no cover - 收场失败不掩盖用例结果
                pass


@pytest.fixture()
def app_client(tmp_path: Path, _no_real_browser) -> Iterator[Any]:
    """**应用客户端**：`TestClient(app, base_url="http://127.0.0.1")` 形态的应用实例。

    家目录重定向到本用例的临时家目录（`case_home()`：与 `isolated_config_dir` **同一个**；
    配置文件与日志都落在它下面）；`startup` 钩子里那一步「打开浏览器」由 `_no_real_browser`
    兜住（**绝不开真窗口**）。用例可以用 `client.browser_calls.calls` 观测这一步有没有被调到。
    """
    with helpers.app_client(case_home(tmp_path), values={}) as client:
        yield client


def test_isolated_config_dir_and_app_client_share_one_home(
    tmp_path: Path, isolated_config_dir: Path, app_client: Any
):
    """两个夹具同时使用时指向**同一个**本用例临时家目录（phase2 §5.4 的家目录硬约束）。

    同时请求两个夹具：`isolated_config_dir` 归属的那个家目录（它的上一级目录），
    与 `HOME` / `USERPROFILE` 两个环境变量、以及 `app_client` 实际落配置的那条路径，
    必须逐字对上 —— 否则「隔离」这件事在两个夹具的视角里各落一处。
    """
    home = isolated_config_dir.parent.parent
    assert home == case_home(tmp_path)
    assert os.environ["HOME"] == str(home)
    assert os.environ["USERPROFILE"] == str(home)
    assert app_client is not None
    assert helpers.config_path_of(home).exists(), "app_client 的配置文件落在同一个家目录下"


@pytest.fixture()
def dev_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """**开发替身运行时**：`MEETING_CAPTURE=dev` 的 `ServiceRuntime` 实例（进程内，未起服务）。

    只做装配与绑定（`set_binding("127.0.0.1", 8765)`），**不触发 lifespan**，因此既没有后台
    线程也没有真实连接；下游要起真服务请走 `service_process` 夹具。
    """
    from meeting import config as config_module
    from meeting.service import app as service_app

    home = tmp_path / "home-dev-runtime"
    home.mkdir(parents=True, exist_ok=True)
    helpers.write_config_json(
        home, {"web.port": 8765, "output.root": str(tmp_path / "out-dev-runtime")}
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("MEETING_CAPTURE", "dev")
    previous = config_module.current()
    try:
        loaded = config_module.load()
        config_module.set_current(loaded)
        runtime = service_app.build_runtime(loaded)
        runtime.set_binding("127.0.0.1", 8765)
        yield runtime
    finally:
        config_module.set_current(previous)
