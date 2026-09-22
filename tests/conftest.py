"""`tests/conftest.py` —— **夹具与 pytest hook**（phase2 §4 / §5.4 的五个夹具名在本 Phase 冻结）。

**跑测试的三条命令与快慢分档（哪些用例算慢、为什么按文件分档）见 `docs/service.md` 第 7 章。**
本文件里那道 5 秒看门是分档能落在**文件粒度**上的前提：没有任何一条用例能超过 5 秒，所以
「慢」来自文件层面的真子进程与真墙钟等待，标记因此加在文件头（`pytestmark = pytest.mark.slow`）。

本文件装两样东西：下面的**七个夹具**与**两个 autouse 安全兜底**（`_no_real_browser` /
`_no_real_audio_capture`），以及两条 pytest hook（`pytest_addoption` 注册
`--regen-contract`；耗时看门那两个 hook 在文末）。**hook 不属于夹具语义**，它们在这里的
唯一理由是 pytest 只认 conftest / 插件里的 `pytest_addoption`（写进测试模块会被静默忽略），
详见下文各自那一段。**本文件里没有一条用例**。

七个夹具由 phase2 定义并冻结、phase3 只复用，不得重定义同名 fixture：

  - `service_process`：**真服务子进程夹具**（内部包 `tests/_service.py` 的 `ServiceProcess`，
    起进程即传环境变量覆盖、家目录 `HOME` 与 `USERPROFILE` 两项都设）；
  - `app_client`：**应用客户端**（`TestClient(app, base_url="http://127.0.0.1")` 形态）；
  - `dev_runtime`：**开发替身运行时**（`MEETING_CAPTURE=dev` 的运行时实例）；
  - `tmp_out_root`：**临时输出根**；
  - `isolated_config_dir`：**隔离的临时配置目录**（连同隔离的临时日志目录）。

phase3 追加两个夹具（名字固定，与上面五个不重名）：

  - `meeting_env`：**本用例的一套隔离环境**（临时输出根 + 临时 log 目录 + **家目录重定向**
    + 本服务那四个环境变量：`MEETING_NO_BROWSER` / `MEETING_PORT` / `MEETING_OUT_ROOT` /
    `MEETING_LOG_DIR`），把子进程的环境变量一次给全。**采集后端不在这份 env 里**（第七轮审查
    m9、第八轮审查 m5 / m6）：它由各用例自己 `config.json` 的 `capture.backend` 表达，env 里
    留着 `MEETING_CAPTURE` 会把那条配置键静默压住 —— 本夹具因此**看门**（建 env 之前那条
    断言）要求宿主进程 env 里也没有这一项，四条 `tests/test_e2e_*.py` 全部同源；
  - `sse_client`：**在 `service_process` 上开一条 SSE 订阅并保证关闭**。

**夹具都在这里，hook 只有两条**：`parse_sse_frame()` / `read_sse_frames()` / `wait_until()` 这类
解析与等待助手、`ServiceProcess` / `SseClient` / `post_action()` 这几个驱动件的实现**都在
`tests/_service.py`**，这里只 import 并包装成 fixture；假件一律放 `tests/_drain_fakes.py`。
**本文件里没有一条用例**（审查问题 m4）：原先那条「`isolated_config_dir` 与 `app_client`
指向同一个家目录」的用例搬到了 `tests/test_config.py`
（`test_isolated_config_dir_and_app_client_share_one_home`），断言强度一字未改 ——
`python -m pytest tests/conftest.py --collect-only -q` 收集到 0 条。

## 浏览器兜底（安全要求，autouse）

pytest **父进程**里 `meeting.service.app.webbrowser` 就是**标准库那个模块对象**：谁让
`launch_browser()` 跑起来而没先把 `webbrowser.open` 换掉，那一下就会落到宿主机器的默认浏览器上
（本项目已经因此误触过一次用户的 Chrome）。因此这里装一个 **autouse** 夹具
`_no_real_browser`，默认把 `meeting.service.app.webbrowser.open`（`launch_browser()` 唯一
会走到的那个属性）换成一个**只记录、绝不开窗**的假替身，测试结束自动还原。
需要真行为的用例在**自己的用例里**显式覆盖它（`monkeypatch.setattr` 再用自己的假替身，
或用本夹具提供的 `browser_calls` 记录）。

## 声卡兜底（安全要求，autouse）

同一类保护的第二层，针对**音频输入设备**：谁在测试里真的打开麦克风或 loopback，就是在
**不打招呼地录机主的声音**。因此这里装另一个 **autouse** 夹具 `_no_real_audio_capture`，
把「一碰就采集声音」的那几个入口换成抛 `RealCaptureForbidden`（**继承 `BaseException`、
不继承 `Exception`**）的桩：

  - `sounddevice.InputStream` / `sounddevice.rec` / `sounddevice.check_input_settings`
    （`sounddevice` 里能**开流 / 读样本**的入口）；
  - `soundcard.get_microphone`（loopback 的唯一入口）。

**只读的枚举不打掉**：`sd.query_devices()` / `sd.query_hostapis()` / `sc.all_speakers()` /
`sc.default_speaker()` 不采集任何声音，用例拿真实端点名做参照时要用它们；`probe()` 的验
一律靠**注入的假端点表 + 假试开函数**（见 `tests/test_capture_probe.py`），真机那条路不在
自动化测试里验。

**夹具自己 import 这两个库，再按模块对象打桩**（`_import_audio_modules()`）：生产代码只在
函数体内惰性 import（`meeting/capture/sources.py` 的 `_import_sounddevice()` /
`_import_soundcard()`），所以「夹具跑的这一刻库还没进 `sys.modules`」是**常态**而不是例外
—— 一个干净 pytest 进程里 `sys.modules.get("sounddevice")` 就是 `None`。夹具因此不再靠
调用方先 import：它自己把库 import 进来（装不上就跳过这一条，无硬件的 CI 因此一行都不用
做），打桩落在**模块对象**上，之后 `sources.py` 在函数体里 `import sounddevice` 拿到的仍是
这个被打过桩的模块对象。**效果与 import 次序无关**：无论用例先 import 库、还是库到那一刻
才第一次被 import，撞上的都是桩。这条不依赖导入顺序的性质由
`tests/test_real_capture_guard.py` 在**一个全新解释器**里逐条验（子进程里先不 import 库、
直接调 `sd.InputStream`，必须被拦下）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from tests import _helpers as helpers
from tests._service import ServiceProcess, SseClient


# ---------------------------------------------------------------------------
# `--regen-contract`：重写 `contract/fixtures/`（c62dec56 phase1 的 golden 再生成开关）
#
# 这条 hook 只能注册在 conftest / 插件里（测试模块里写 `pytest_addoption` 会被 pytest
# 静默忽略），因此它落在本文件 —— 与夹具放在一起，但它不承载任何夹具语义：
# 默认模式下 fixtures 逐文件比对、不一致即红；给出本选项时改写 fixtures。
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--regen-contract",
        action="store_true",
        default=False,
        help="重写 contract/fixtures/（默认模式是逐文件比对，不一致即 fail）",
    )


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
# 安全兜底：绝不许碰真声卡（打开输入设备 = 不打招呼地录机主的声音）
# ---------------------------------------------------------------------------

#: 打桩时报错的那句话（用例看到它就是「这条用例在碰真硬件」）
REAL_CAPTURE_FORBIDDEN_MESSAGE = (
    "自动化测试不许碰真声卡（打开输入设备就是在录机主的声音）："
    "请注入假设备表 / 假采集源。`probe()` 的真实硬件这条路不在自动化测试里验。"
)


class RealCaptureForbidden(BaseException):
    """**看门抛的异常**：它**不继承 `Exception`**（`BaseException` 直系子类）。

    这条继承关系就是这道看门的意义所在：`meeting/capture/sources.py` 在试开那一步收
    「开流失败」（`except Exception`，真正的打开失败照旧如实留痕），而任何一处
    `except Exception` / `except BaseException` 都不该把「这条用例在碰真硬件」收成一条
    业务失败。不继承 `Exception` 之后：**收窄成 `except Exception` 的地方自然吞不掉它**
    （`sources.py` 两处试开），而别处那些宽的 `except BaseException`（例如
    `meeting/service/runtime.py` 里「这一步的任何意外都不许带崩启动」那一类兜底）也**必须
    额外点名它**才会吞掉它 —— 宽 `except` 顺手吞掉看门这件事因此不再可能。
    """


#: 要打掉的入口：`(模块名, 入口名)`。**只列能开流 / 读样本的**，枚举设备的一律留着。
_FORBIDDEN_CAPTURE_ENTRIES: tuple[tuple[str, str], ...] = (
    ("sounddevice", "InputStream"),
    ("sounddevice", "rec"),
    ("sounddevice", "check_input_settings"),
    ("soundcard", "get_microphone"),
)


def _forbid_real_capture(stub_call):
    """把某个入口换成「一碰就炸」的桩（`stub_call` 收下这次调用的全部参数只用于报错）。"""

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise RealCaptureForbidden(
            f"{REAL_CAPTURE_FORBIDDEN_MESSAGE}（被拦下的调用：{stub_call}，"
            f"args={args!r}，kwargs={kwargs!r}）"
        )

    return blocked


def _import_audio_modules() -> dict[str, Any]:
    """**夹具自己把两个声卡库 import 进来**（装不上就跳过那一条），再按模块对象打桩。

    为什么不能只查 `sys.modules`：生产代码只在函数体内惰性 import
    （`meeting/capture/sources.py` 的 `_import_sounddevice()` / `_import_soundcard()`），
    一个干净 pytest 进程里这两个库**根本还没进 `sys.modules`**；只查表就一个入口都打不上，
    看门成了空操作 —— 用例随后 `import sounddevice; sd.rec(1)` 会真的落到真设备上。
    这里主动 import 一次（`importlib.import_module`）把库请进来，桩因此**与导入次序无关**。

    库没装（`ModuleNotFoundError`）时**跳过**它：没有硬件的 CI 上一行都不用做；装了但
    import 过程中抛别的错（缺 DLL 之类的坏安装）同样跳过，理由一样 —— 夹具不许因为
    「这台机器上没有这个库」把用例判红。两个库各自独立判定，一个装了一个没装也照常工作。
    返回值是 `{模块名: 模块对象}`。
    """
    import importlib

    modules: dict[str, Any] = {}
    for module_name in ("sounddevice", "soundcard"):
        try:
            modules[module_name] = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - 装不上 / 装坏了都按「这台机器上没有它」处理
            continue
    return modules


@pytest.fixture(autouse=True)
def _no_real_audio_capture(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """**autouse 兜底**：把真采集入口打成「一碰就炸」，只读的枚举一律留着。

    打桩分两步，**顺序不能换**：先 `_import_audio_modules()` 把两个库 import 进来（此前
    它们通常还没进 `sys.modules`），再在**模块对象**上打桩。`meeting/capture/sources.py`
    的函数内 `import sounddevice` 拿到的就是同一个模块对象，于是「惰性 import」绕不过这
    道桩、**导入次序也影响不了它**：库是在本夹具里第一次进 `sys.modules` 的，桩装着；
    用例自己先 import 过，桩照样落在同一个模块对象上。

    抛的异常是 `RealCaptureForbidden`（**不继承 `Exception`**）：`sources.py` 试开那两步
    只收「开流失败」（`except Exception`），吞不掉它；别处的宽 `except BaseException` 也要
    额外点名它才吞得掉。

    需要验「真开设备」的用例已经不存在了：真机那条路的正确性靠假表单测 + 段末真机脚本。
    """
    blocked: list[str] = []
    modules = _import_audio_modules()
    for module_name, entry in _FORBIDDEN_CAPTURE_ENTRIES:
        module = modules.get(module_name)
        if module is None:
            continue  # 这台机器上没有它：没有可打桩的，也没有可误碰的
        if not hasattr(module, entry):
            continue
        monkeypatch.setattr(
            module, entry, _forbid_real_capture(f"{module_name}.{entry}"), raising=False
        )
        blocked.append(f"{module_name}.{entry}")
    yield blocked


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

    用法：``proc = service_process(home=meeting_env.home, extra_env=meeting_env.env)``
    （工厂自己注册收场，用例不必写 `finally`）。

    能力（phase2 冻结）：`base_url`（`MEETING_PORT=0` + 子进程启动输出解析）、
    向子进程发中断信号（`send_interrupt()` / `send_ctrl_c()`）、读退出码（`returncode` /
    `wait()`）、**传完整的环境变量覆盖**（`env=` 给全一份映射；`PYTHONPATH` 默认 = 仓库根，
    需要假件时由调用方把「仓库根 + 临时假件目录」拼好传进来）。

    **本夹具不往子进程 env 里塞 `MEETING_CAPTURE`**（审查问题 m5 / m6 的直接修复）：
    这条环境变量的优先级高于配置文件，静默塞一个缺省值就会把各用例自己写在 `config.json`
    里的 `capture.backend` **压住**（假件挂不上、采集替身选错档，用例「以为在验 A、其实在验
    B」）。采集后端一律由**用例自己的 `config.json`** 表达：`meeting_env.write_config({
    "capture.backend": "dev"})` —— 四条 `tests/test_e2e_*.py` 与本文件因此同源。需要走环境
    变量那一档的用例（`capture.backend` 在 env 里的取值域）显式传 `capture="dev"` /
    `capture="null"`，那是有意为之的一条覆盖，本夹具的缺省**不**做这件事。
    """
    started: list[ServiceProcess] = []
    counter = {"n": 0}

    def factory(**kwargs: Any) -> ServiceProcess:
        counter["n"] += 1
        home = kwargs.pop("home", None) or (tmp_path / f"home{counter['n']}")
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


# ---------------------------------------------------------------------------
# phase3 追加的两个夹具（名字固定：`meeting_env` / `sse_client`）
# ---------------------------------------------------------------------------


@dataclass
class MeetingEnv:
    """一个用例的一套隔离环境（`meeting_env` 夹具给出的那一个）。"""

    home: Path
    out_root: Path
    log_dir: Path
    config_dir: Path
    env: dict[str, str]

    @property
    def config_path(self) -> Path:
        return self.config_dir / "config.json"

    def write_config(self, values: dict[str, Any]) -> Path:
        """把「点号扁平键名 → 值」写进本用例的 `config.json`（**写合法 JSON**）。"""
        return helpers.write_config_json(self.home, values)


@pytest.fixture()
def meeting_env(tmp_path: Path) -> MeetingEnv:
    """**本用例的一套隔离环境**：临时输出根 + 临时 log 目录 + **家目录重定向** + 服务变量。

    逐项照 §3.8 的「子进程环境（每条测试的必备项）」：

      - `MEETING_NO_BROWSER=1`（**每一条子进程测试都必须设上**，否则真机会弹浏览器）；
      - `MEETING_PORT=0`、`MEETING_OUT_ROOT=<本用例临时目录>`、
        `MEETING_LOG_DIR=<本用例临时目录>`；
      - **家目录重定向**：`HOME` 与 `USERPROFILE` 都指向本用例的临时家目录 —— 这是硬条件：
        配置文件 `~/.config/meeting/config.json` 与服务级 `logs/` 都挂在它下面，只设输出根 /
        端口而漏掉它，子进程会读真实用户的配置与日志（测试全绿却验的不是临时配置）。

    **采集后端改由 `config.json` 的 `capture.backend` 表达，env 里不带 `MEETING_CAPTURE`**
    （第七轮审查 m9）：环境变量的优先级高于配置文件，env 里留着这一项就会把配置键**静默压住**
    —— 挂假件的那几条用例（`tests/test_drain.py` 那一支）因此一开就摘它，而原先本夹具又把
    `dev` 写在 env 里，两条子进程路径的采集后端于是各说一套。现在四条 `tests/test_e2e_*.py`
    与本夹具同源：都在自己的 `config.json` 里写 `capture.backend`，env 里那一项一个都不多设。

    **靠重定向既有环境变量达成隔离，不新增任何 `MEETING_*` 变量**；`PYTHONPATH` 与
    「额外环境变量」由用例经 `service_process(*, pythonpath=..., extra_env=...)` 给全。
    """
    # 看门：本用例的宿主进程 env 里不许留着 `MEETING_CAPTURE`。本夹具自己不设它（见上），
    # 但同一进程里别的夹具（`dev_runtime`）会临时设上；万一那一层没被还原，这一份 env
    # 就会带着它进子进程、把用例写在 `config.json` 里的采集后端静默压住。这里当场说话。
    assert "MEETING_CAPTURE" not in os.environ, (
        "宿主进程 env 里留着 MEETING_CAPTURE：它会压住子进程配置里的 capture.backend，"
        "本用例的隔离环境因此不该带着它建立（实测取值 %r）" % (os.environ.get("MEETING_CAPTURE"),)
    )
    home = case_home(tmp_path)
    out_root = tmp_path / "out"
    log_dir = tmp_path / "logs"
    out_root.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    config_dir = home / ".config" / "meeting"
    config_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "MEETING_NO_BROWSER": "1",
        "MEETING_PORT": "0",
        "MEETING_OUT_ROOT": str(out_root),
        "MEETING_LOG_DIR": str(log_dir),
    }
    return MeetingEnv(
        home=home, out_root=out_root, log_dir=log_dir, config_dir=config_dir, env=env
    )


@pytest.fixture()
def sse_client(service_process: Callable[..., Any]) -> Iterator[Callable[..., SseClient]]:
    """**在 `service_process` 上开一条 SSE 订阅并保证关闭**。

    用法：``client = sse_client(proc)``（夹具自己记下来，用例结束时统一 `close()`；
    用例也可以自己 `client.close()` 提前断开 —— 重复关闭是幂等的）。

    订阅者本身的读写助手在 `tests/_service.py` 的 `SseClient` 上：
    `read_frame()` / `read_frames()` / `read_event_frame()` / `read_until()`。
    """

    opened: list[SseClient] = []

    def factory(target: Any, *, path: str = "/api/events", timeout: float = 30.0) -> SseClient:
        client = SseClient(target, path=path, timeout=timeout)
        opened.append(client)
        return client

    try:
        yield factory
    finally:
        for client in opened:
            try:
                client.close()
            except Exception:  # pragma: no cover - 收场失败不掩盖用例结果
                pass


# ---------------------------------------------------------------------------
# 耗时看门（**用户硬要求**：每一条用例 setup + call + teardown 合计 ≤ 5.0 秒）
#
# 契约里写的是「`conftest.py` 只放 fixture」；这一条 hook 与 `pytest_addoption` 同属那条例外的
# 另一半（用户把「单条用例 ≤ 5 秒」定为与修审查问题同级的硬要求，并要求加一道**自动看门**，
# 看门要落在 pytest 里跑得起来的地方，光在报告里声明一句不够）。两条 hook 都不改任何夹具语义：
# `pytest_addoption` 只是登记一个开关，这一条只汇总 pytest 自己的三段耗时。
# ---------------------------------------------------------------------------

#: 单条用例（setup + call + teardown 三段合计）的墙钟上限（秒）
CASE_WALL_CLOCK_LIMIT_SECONDS = 5.0

#: 本用例三段耗时（`nodeid` -> `{段名: 秒}`）；`pytest_runtest_logreport` 逐段填，
#: `pytest_runtest_makereport` 在最后一段（`teardown`）之后汇总判红。
_CASE_DURATIONS: dict[str, dict[str, float]] = {}


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """把每一条用例的 `setup` / `call` / `teardown` 三段耗时记下来。"""
    if report.when not in ("setup", "call", "teardown"):
        return
    _CASE_DURATIONS.setdefault(report.nodeid, {})[report.when] = float(report.duration)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """最后一段（`teardown`）之后汇总三段耗时：**合计超过 5.0 秒就把这条用例判红**。

    判红方式是在 `teardown` 的 report 上打一条 `longrepr`（报出用例名与三段各自的耗时），
    于是这条超时用例会以「失败」出现在 `pytest -q` 的统计里、挂在自己的名字下，谁都能一眼
    看到是哪一条慢了。耗时数据只从 pytest 自己的 report 里取（不额外计时、不加 fixture），
    因此看门本身对被测用例零影响。
    """
    outcome = yield
    report = outcome.get_result()
    if report.when != "teardown":
        return
    durations = _CASE_DURATIONS.pop(item.nodeid, None)
    if not durations:
        return
    total = sum(durations.values())
    if total <= CASE_WALL_CLOCK_LIMIT_SECONDS:
        return
    report.outcome = "failed"
    report.longrepr = (
        f"{item.nodeid} 单条用例合计耗时 {total:.2f}s，超过上限 "
        f"{CASE_WALL_CLOCK_LIMIT_SECONDS:.1f}s"
        f"（setup {durations.get('setup', 0.0):.2f}s / "
        f"call {durations.get('call', 0.0):.2f}s / "
        f"teardown {durations.get('teardown', 0.0):.2f}s）"
    )
