"""`tests/conftest.py` —— **夹具与 pytest hook**（设计口径 / §5.4 的五个夹具名在本项目冻结）。

**跑测试的三条命令与快慢分档（哪些用例算慢、为什么按文件分档）见 `docs/service.md`。**
本文件里那道 5 秒看门是分档能落在**文件粒度**上的前提：没有任何一条用例能超过 5 秒，所以
「慢」来自文件层面的真子进程与真墙钟等待，标记因此加在文件头（`pytestmark = pytest.mark.slow`）。

本文件装两样东西：下面的**九个夹具**与**八道 autouse 安全兜底**（`_no_real_browser` /
`_no_real_audio_capture` / `_no_real_minutes_session` / `_no_real_model_call`，第五道
**PATH 守卫** —— 会话级的 `_no_real_dsh_on_path` 与用例级的 `_no_real_dsh_launch` 两条夹具
合起来构成它，见下文那一节；第六道 **真家目录守卫** —— 会话级的
`_no_writes_to_the_real_home` 与用例级的 `_no_writes_to_the_real_home_case` 合起来构成它，
见下文那一节；第七道 **真实 DSH web 服务端守卫** —— `_no_real_dsh_web_call`；第八道
**DSH 触达原语守卫** —— `_no_real_dsh_delivery`，把探针 / 交付 / headless 拉起三个入口
桩成「记录调用即判红」，见下文那一节），以及 pytest hook（`pytest_addoption` 注册
`--regen-contract`；耗时看门那两个 hook 在文末；`pytest_sessionfinish` 是 PATH 守卫与
真家目录守卫的收场复查）。**hook 不属于夹具语义**，它们在这里的唯一理由是 pytest 只认
conftest / 插件里的 `pytest_addoption`（写进测试模块会被静默忽略），详见下文各自那一段。
**本文件里没有一条用例**。

七个夹具由设计定义并冻结、设计只复用，不得重定义同名 fixture：

  - `service_process`：**真服务子进程夹具**（内部包 `tests/_service.py` 的 `ServiceProcess`，
    起进程即传环境变量覆盖、家目录 `HOME` 与 `USERPROFILE` 两项都设）；
  - `app_client`：**应用客户端**（`TestClient(app, base_url="http://127.0.0.1")` 形态）；
  - `dev_runtime`：**开发替身运行时**（`MEETING_CAPTURE=dev` 的运行时实例）；
  - `tmp_out_root`：**临时场次放置根**（给 `start` 的 `args.dir` 预建目录用）；
  - `isolated_config_dir`：**隔离的临时配置目录**（连同隔离的临时日志目录）。

设计追加两个夹具（名字固定，与上面五个不重名）：

  - `meeting_env`：**本用例的一套隔离环境**（临时场次放置根 + 临时 log 目录 + **家目录重定向**
    + 本服务的环境变量：`MEETING_NO_BROWSER` / `MEETING_PORT` / `MEETING_LOG_DIR`）——
    （`MEETING_OUT_ROOT` 随 `output.root` 配置键一并退场，不再出现）。把子进程的环境变量一次给
    全。**采集后端不在这份 env 里**（第七轮审查
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

## 模型出口兜底（安全要求，autouse）

第三类保护的落点是**会中总结那条 HTTP 出口**：`meeting/summary/client.py` 的
`_http_post()` 是这套代码里唯一一次真的往外发模型请求的地方（标准库 `urllib.request`）。
谁让它跑起来而没先注入假客户端，那一下就是**真的一次模型调用**：`meeting/config.py` 里
`summary.base_url` 的默认值就是真地址（`https://api.deepseek.com/v1`），配置里补上
`summary.api_key` 之后「忘了注入」与「真发出去」之间一件事都不差 —— 代价是机主的 token。

因此这里装 **autouse** 夹具 `_no_real_model_call`：把 `urllib.request.urlopen` 换成一个
**按目标地址判**的包装 —— 本机回环（`localhost` / `127.0.0.0/8` / `::1`）**原样放行**给真
传输层（将来接本地假端点不受影响），其余一律在出口处当场抛 `RealModelCallForbidden`
（一个 `RuntimeError` 子类：它在请求里被收成 `summary_failed`(502) 那条既定失败档、消息里
带着兜底的名字与被拦下的地址；为什么不用 `BaseException` 直系子类见那个类的说明 —— 实测
那样会让 ASGI 门户的收场抛一个空消息的 `CancelledError`，把真正有用的信号盖住）。

自证用 `tests/test_summary_action.py::test_a_missing_injection_is_blocked_at_the_egress_guard`：
那一条**故意不注入** `runtime.summary_client`（配置里就是真地址），断言夹具的登记清单里
恰好一笔、就是那个地址，而这一趟总结回的是带着兜底名字的 502 —— 它**没有出门**。

## DSH 子会话兜底（安全要求，autouse，第五道）

第四类保护的落点是**真会起一个 headless DSH 子会话**的那一步：
`meeting/minutes/launch.py::launch_headless()` 按 `shutil.which("dsh")` 解析可执行文件并
`subprocess.run` 它 —— 本机 PATH 上那件 `dsh` 是 npm 全局装的真件
（`C:\\Users\\<user>\\AppData\\Roaming\\npm\\dsh.cmd`），起来就是一次真模型回合、真花钱。

`_no_real_minutes_session` 那道再入闸挡的是**产品代码里那条**路（`runner.run()` 最前面的
再入闸）；它挡不住「用例把再入闸摘掉、又忘了注入假启动器」这一档，而这一档**已经真实发生过**
（`tests/test_cli_post_config.py` 早期版本把假启动器挂在一个不存在的名字上，
`raising=False` 现造死属性 → 那层「一碰就抛」的网落空 → 65 个真子会话、243 个真模型回合）。
**一道不依赖任何单条用例是否记得装替身的网必须落在夹具这一层**，这就是本道守卫：

  - `_no_real_dsh_on_path`（会话级）：在临时目录里写一件假的 `dsh.cmd`（只往记录追加一行
    「时刻 + 全部 argv + 当前工作目录」，然后**以非 0 退出、什么都不执行**），把那个目录
    **前置**到测试进程 `os.environ["PATH"]` 最前面 —— `shutil.which("dsh")` 从此命中的是它，
    漏网的用例再也起不来真会话；收场时还原 `PATH` 并删掉临时目录。这道守卫只写文件 + 改
    `os.environ`（不起任何进程、不用 `Start-Process`）；
  - `_no_real_dsh_launch`（用例级）：每条用例开始时记下记录行数，结束时**新增了就当场判红**，
    话术里带着被叫起的 argv 与 cwd；用例收场之后才落进记录的那一笔由下一条用例的开工复查
    与 `pytest_sessionfinish` 兜住，**不存在「安静地跑过去」这一档**。

要走到「找可执行文件」那一步**并故意让 PATH 上那件假 `dsh` 被叫起**的用例，显式请
`allow_real_dsh_resolution` 那道 opt-out 夹具（名字写清「这条用例故意允许走到可执行文件
解析」）；**没有别的关掉记录断言的办法**。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from tests import _helpers as helpers
from tests._service import ServiceProcess, SseClient


# ---------------------------------------------------------------------------
# `--regen-contract`：重写 `contract/fixtures/`（设计的 golden 再生成开关）
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


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch: pytest.MonkeyPatch) -> Iterator[helpers.ServiceBrowser]:
    """**autouse 兜底**：把 `meeting.service.app.webbrowser.open` 换成只记录的假替身。

    假替身类就是 `tests/_helpers.py` 的 `ServiceBrowser`（本仓「只记录、绝不开窗」的假
    webbrowser 只有这一份；缺省 `outcome = "safe"`：返回 `False`，只把 URL 记进 `calls`）。
    这条保护与 `tests/_helpers.py` 里 `app_client()` 自己的替换是**两层**（那一层在上下文
    退出时会把真 `open` 放回来，之后仍然由本夹具这一层兜着），因此 pytest 进程里任何一条
    路径都碰不到真浏览器。用例需要验「打开浏览器」这一步的行为时，显式再覆盖一次即可。
    """
    from meeting.service import app as service_app

    fake = helpers.ServiceBrowser()
    monkeypatch.setattr(service_app.webbrowser, "open", fake.fake_open, raising=False)
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


#: 会后纪要那一层的再入闸标记：测试进程里缺省立着它（见下面的 autouse 夹具）
MINUTES_CHILD_FLAG = "MEETING_MINUTES_CHILD"


@pytest.fixture(autouse=True)
def _no_real_minutes_session(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """**autouse 兜底**：把会后纪要那一层的再入闸标记立起来，测试进程绝不拉起真 DSH。

    理由与上面两个兜底同一条：本机 PATH 上真的有 `dsh`
    （`C:\\Users\\<user>\\AppData\\Roaming\\npm\\dsh.cmd`）。生产路径上离线的尾巴会调
    `minutes.hook`，而 `minutes.hook = auto` 那一档会**真的**起一个 headless DSH 子会话去跑
    纪要提示词 —— 那是一次真模型调用、几十分钟的墙钟、还占着显卡，而绝大多数用例只想知道
    离线那三份产物长什么样。

    立起这一格之后 `meeting.minutes.runner.run()` **最前面**的再入闸当场返回 `skipped`，
    一个子进程都不会起（这道闸就落在产品代码里，测试进程借它把真会话挡住）。
    要验钩子本身的用例显式去掉它（`monkeypatch.delenv("MEETING_MINUTES_CHILD")`）并注入假启动器，
    `tests/test_minutes_runner.py` 走的就是那一档。
    """
    monkeypatch.setenv(MINUTES_CHILD_FLAG, "1")
    yield MINUTES_CHILD_FLAG


# ---------------------------------------------------------------------------
# 安全兜底：PATH 上那件 `dsh` 被换成假件（**第五道**：绝不真起 headless 子会话）
#
# 这一道与上面四道同一族，但要挡住的事故更贵：真的起一个 headless DSH 子会话 = 一次真模型
# 回合（本节点实测过 65 个子会话 / 243 个模型回合）。**它不依赖任何单条用例是否记得装替身**：
# 会话级把假 `dsh` 前置到 PATH 最前面，用例级拿记录把漏网当场判红。
# ---------------------------------------------------------------------------

#: 假 `dsh` 的落点与记录名（都在会话级的临时目录里；那个目录被前置到 `PATH` 最前面）
DSH_GUARD_SHIM_NAME = "dsh.cmd"
DSH_GUARD_LEDGER_NAME = "dsh-calls.log"

#: 假 `dsh` 的退出码（非 0：产品代码照 `FAILURE_EXIT_CODE` 那一档如实记失败）
DSH_GUARD_EXIT_CODE = 97

#: 收场复查之前最多等产品那条「纪要生成」daemon 线程多久（秒）：**上界只是保险**
#: （假 `dsh` 什么都不做、当场退非 0，那条线程正常在百毫秒级收工）
DSH_GUARD_THREAD_WAIT_SECONDS = 2.0

#: 假 `dsh` 的全文：**只用 cmd 内建**，把「时刻 + 全部 argv + 当前工作目录」追加进记录，
#: 然后以非 0 退出 —— **什么都不执行**（不 import、不起进程、不碰网络、不写产物）。
_FAKE_DSH_SOURCE = (
    "@echo off\r\n"
    '>> "{ledger}" echo %DATE% %TIME% argv=%* cwd=%CD%\r\n'
    "exit /b {exit_code}\r\n"
)

#: 记录断言的话术（用例看到它就是「这一趟在叫 PATH 上那件假 `dsh`」）
DSH_GUARD_VIOLATION_MESSAGE = (
    "假 `dsh` 被调用了：测试进程去叫了 `PATH` 上那件 `dsh`（`tests/conftest.py` 装的假件，"
    "什么都没执行、当场退非 0）。真子会话因此起不来、一分钱都花不出去，但这条用例要么该注入"
    "假启动器（`which=` / `executable=` / `launcher=`，或者把真拉起点整个换掉），"
    "要么就是泄漏了真纪要链。"
)


@dataclass
class DshGuard:
    """这道 PATH 守卫的读数：假 `dsh` 的落点、记录、以及被换下来的那一段 `PATH`。"""

    root: Path
    ledger: Path
    shim: Path
    previous_path: str
    #: 已经被某条用例「认领」过的记录行数（迟到的那些靠它认出来）
    claimed: int = 0
    #: 上一次认领的用例（迟到的那一笔属于它）
    claimed_by: str = ""

    def lines(self) -> list[str]:
        """记录里现有的行（一行 = 假 `dsh` 被叫起一次）。"""
        if not self.ledger.is_file():
            return []
        text = _decode_ledger(self.ledger.read_bytes())
        return [line for line in text.splitlines() if line.strip()]

    def unreported(self) -> list[str]:
        """已经落进记录、但还没有任何一条用例认领过的那些行。"""
        return self.lines()[self.claimed :]


#: 会话级那道守卫的读数（`pytest_sessionfinish` 收场复查时读它）
_ACTIVE_DSH_GUARD: DshGuard | None = None


def _decode_ledger(raw: bytes) -> str:
    """记录的字节 → 文本（`cmd` 的 `echo` 按当前代码页写：UTF-8 与本地代码页都认）。"""
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:  # pragma: no cover - 换下一个编码再来
            continue
    return raw.decode("utf-8", errors="replace")  # pragma: no cover - latin-1 之后到不了


def _dsh_guard_violation(guard: DshGuard, lines: list[str], *, when: str) -> str:
    """记录断言的话术：谁被叫起、argv 与 cwd 是什么、这条用例该做什么。"""
    return (
        f"{DSH_GUARD_VIOLATION_MESSAGE}\n"
        f"记录：{guard.ledger}（新增 {len(lines)} 笔；归属用例 {when}）\n"
        + "\n".join(f"  {line}" for line in lines)
    )


def _allows_real_dsh_resolution(request: pytest.FixtureRequest) -> bool:
    """这条用例是不是显式请了那道 opt-out 夹具（`allow_real_dsh_resolution`）。"""
    return "allow_real_dsh_resolution" in request.fixturenames


def _wait_for_the_minutes_thread() -> None:
    """等产品那条「起线程就返回」的 daemon 线程收工（**join，不轮询、不空转**）。

    为什么必须有这一步：真拉起点住在一条 daemon 线程上（`meeting/backends/minutes.py` 的
    `MinutesHook.launch()` 起完线程就返回），子进程因此可能**晚于**用例收场才落记录 —— 不等的
    话，一次真漏网会写在记录上却没人读（用例级的复查已经读完了）。线程名取产品那个常量
    （`meeting.backends.minutes.THREAD_NAME`）；**那条线程不存在时这一步零成本**（只扫一遍
    当前线程表）。上界见 `DSH_GUARD_THREAD_WAIT_SECONDS`。
    """
    import threading

    try:
        from meeting.backends import minutes as minutes_backend

        thread_name = str(minutes_backend.THREAD_NAME)
    except Exception:  # pragma: no cover - 产品那边改名/导入不了时退回既有名字
        thread_name = "minutes-final"
    for thread in list(threading.enumerate()):
        if thread.name == thread_name and thread.is_alive():
            thread.join(DSH_GUARD_THREAD_WAIT_SECONDS)


@pytest.fixture(scope="session", autouse=True)
def _no_real_dsh_on_path() -> Iterator[DshGuard]:
    """**autouse 兜底（会话级）**：把一件假的 `dsh` 前置到测试进程 `PATH` 的最前面。

    为什么：`meeting/minutes/launch.py::launch_headless()` 是按「找可执行文件」这条路去起
    headless DSH 的（`shutil.which("dsh")`），而本机 PATH 上真的有 npm 装的那件真 `dsh`。
    `_no_real_minutes_session` 那道再入闸只挡产品代码里那条路，**挡不住「用例摘掉再入闸、
    又忘了注入假启动器」**这一档 —— 那一档本节点已经真实发生过（65 个真子会话 / 243 个真
    模型回合）。因此这里造一件假的 `dsh.cmd`（内容见 `_FAKE_DSH_SOURCE`：往记录追加一行
    「时刻 + 全部 argv + 当前工作目录」，然后以非 0 退出、**什么都不执行**），把它所在目录
    前置到 `os.environ["PATH"]` 最前面：漏网的用例命中的是这件假件，**花钱这件事变成不可能**。

    只写文件 + 改 `os.environ`（不起任何进程、不用 `Start-Process`）；收场时把 `PATH` 原样
    放回并删掉那个临时目录。
    """
    global _ACTIVE_DSH_GUARD

    root = Path(tempfile.mkdtemp(prefix="pytest-dsh-guard-"))
    ledger = root / DSH_GUARD_LEDGER_NAME
    shim = root / DSH_GUARD_SHIM_NAME
    shim.write_text(
        _FAKE_DSH_SOURCE.format(ledger=ledger, exit_code=DSH_GUARD_EXIT_CODE),
        encoding="ascii",
    )
    previous_path = os.environ.get("PATH", "")
    os.environ["PATH"] = (
        os.pathsep.join([str(root), previous_path]) if previous_path else str(root)
    )
    guard = DshGuard(
        root=root, ledger=ledger, shim=shim, previous_path=previous_path
    )
    _ACTIVE_DSH_GUARD = guard
    try:
        yield guard
    finally:
        _ACTIVE_DSH_GUARD = None
        os.environ["PATH"] = previous_path
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_real_dsh_launch(
    request: pytest.FixtureRequest, _no_real_dsh_on_path: DshGuard
) -> Iterator[DshGuard]:
    """**autouse 兜底（用例级）**：这条用例里假 `dsh` 一次都不许被叫起，叫了就当场判红。

    两个读数一起看：

      - **开工复查**：记录上还有没有被认领的笔数？有 —— 说明上一轮**用例收场之后**才落进来
        （真拉起点住在一条 daemon 线程上：`MinutesHook.launch()` 起线程就返回，子进程可能晚
        于那条用例的收场落记录）。这一笔也当场判红，话术里点名它属于哪一条用例；
      - **收场复查**：先把产品那条「纪要生成」daemon 线程 join 掉（`_wait_for_the_minutes_thread()`：
        真拉起点起完线程就返回，不 join 就会读到一个还没落定的记录），再比这条用例开工到收场
        之间新增的行 = 这一趟漏到 PATH 上那件假 `dsh` 上了 —— `pytest.fail` 把这条用例判红，
        消息里逐笔列出 argv 与 cwd。

    `pytest_sessionfinish` 另有一道收场复查（最后一条用例收场之后落进来的那一笔也跑不掉）。
    要**故意**走到「可执行文件解析」并让那件假 `dsh` 被叫起的用例，显式请
    `allow_real_dsh_resolution` 那道 opt-out 夹具。
    """
    guard = _no_real_dsh_on_path
    allowed = _allows_real_dsh_resolution(request)
    late = guard.unreported()
    before = len(guard.lines())
    guard.claimed = before
    if late and not allowed:
        pytest.fail(_dsh_guard_violation(guard, late, when=guard.claimed_by))
    guard.claimed_by = request.node.nodeid

    yield guard

    _wait_for_the_minutes_thread()
    lines = guard.lines()
    guard.claimed = len(lines)
    added = lines[before:]
    if added and not allowed:
        pytest.fail(_dsh_guard_violation(guard, added, when=request.node.nodeid))


@pytest.fixture()
def allow_real_dsh_resolution() -> Iterator[None]:
    """**显式 opt-out**：这一条用例**故意允许**走到「找可执行文件」那一步。

    请了它，记录断言就不把「PATH 上那件假 `dsh` 被叫起」算在这条用例头上 —— 那道**结构性**
    守卫一个字都不动（`PATH` 上的假件照旧在岗，真会话照旧起不来），放开的只是「这一条用例
    的记录判红」。只有**真的**要验「可执行文件解析 / 拉起动作」这一层、且没法靠自己注入
    `which=` / `executable=` / `launcher=` 的用例才该请它。

    **当前全仓 0 处引用（如实记一笔，别读成「有人靠它放行过」）**：现有的用例都能靠注入
    `which=` / `executable=` / `launcher=` 或把真拉起点整个换掉来验自己那一格，因此这道
    opt-out 至今没有主。它是**备着**的：那道守卫的判红口径要留一个显式的、写明理由的出口
    （否则将来真需要验解析那一层的用例只能去改守卫本身）；留着它不构成放行 —— 谁请了它，
    谁的名字就出现在那条用例的源码里，审计一眼可见。
    """
    yield None


# ---------------------------------------------------------------------------
# 安全兜底：测试不许写机主真实家目录（**第六道**：`~/.config/meeting/**`）
#
# 与上面五道同一族的**结构性**守卫：会话开工记录机主真实家目录下 `~/.config/meeting/` 的
# 文件集合与逐个 sha256，用例级与 `pytest_sessionfinish` 两级复查 —— 被创建 / 被改动就把
# 结果判红，并打印是哪条路径、从什么变成了什么。它挡的是「用例没重定向家目录，于是把
# 配置或日志写进了机主家目录」这一档（设计的 `post()` 接配置层之后就真实发生过：
# 一条不做隔离的用例读了机主那份 `config.json`，把 `minutes.hook = "null"` 设成模块级生效
# 配置且不还，24 条用例因此被串档打红）。
# ---------------------------------------------------------------------------

#: 真家目录守卫的话术（用例看到它就是「这一趟在动机主家目录」）
REAL_HOME_GUARD_VIOLATION_MESSAGE = (
    "测试写进了机主真实家目录（`~/.config/meeting/**`）：测试一律靠**重定向家目录**隔离"
    "（`HOME` / `USERPROFILE` 两项都指向本用例的临时家目录 —— `_isolated_home` 夹具已经做了"
    "这一步），不许读也不许写机主的那一份。下面是被创建 / 被改动的路径与前后读数。"
)


def _real_home_config_dir() -> Path:
    """会话开工那一刻的「机主真实家目录下的配置目录」（按当时的环境变量算，只算一次）。"""
    from meeting import config as config_module

    return Path(config_module.config_path()).parent


def _real_home_snapshot(config_dir: Path) -> dict[str, str]:
    """`config_dir` 的文件集合与逐个内容 sha256（相对路径 -> 读数）。"""
    if not config_dir.is_dir():
        return {"<配置目录>": "不存在"}
    snapshot: dict[str, str] = {}
    for path in sorted(config_dir.rglob("*")):
        relative = path.relative_to(config_dir).as_posix()
        if path.is_dir():
            snapshot[relative + "/"] = "目录"
            continue
        try:
            snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:  # pragma: no cover - 读不动时如实记一笔，不掩盖
            snapshot[relative] = "<读不动>"
    return snapshot


def _real_home_diff(
    before: dict[str, str], after: dict[str, str], config_dir: Path | None = None
) -> list[str]:
    """两份快照的差异（一条路径一行）。

    行里除了「开工读数 -> 收工读数」，还给得出**收工时那份文件的内容**（`config_dir` 给定时；
    截到 `REAL_HOME_GUARD_CONTENT_CHARS` 个字符）—— 只有两个哈希的话，谁都得再去机主家目录
    翻一遍才知道被改成了什么，而这一眼就该看得出。
    """
    lines: list[str] = []
    for key in sorted(set(before) | set(after)):
        was = before.get(key, "<不存在>")
        now = after.get(key, "<不存在>")
        if was == now:
            continue
        detail = ""
        if config_dir is not None and not key.endswith("/") and now not in ("<不存在>", "<读不动>"):
            content = _real_home_content(config_dir, key)
            if content:
                detail = f"，收工内容={content!r}"
        lines.append(f"  {key}: {was} -> {now}{detail}")
    return lines


#: 判红时最多把多少字符的文件内容带进消息里（够看清「改成了什么」，又不撑爆终端）
REAL_HOME_GUARD_CONTENT_CHARS = 200


def _real_home_content(config_dir: Path, relative: str) -> str:
    """收工时那份文件的内容（读不动就交回空串：消息里不出现这一格）。"""
    try:
        raw = (config_dir / relative).read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - 读不动时只留哈希
        return ""
    return raw.strip().replace("\r\n", " ").replace("\n", " ")[:REAL_HOME_GUARD_CONTENT_CHARS]


@dataclass
class RealHomeGuard:
    """这道守卫的读数：机主真实家目录下那个配置目录的落点与开工快照。"""

    config_dir: Path
    before: dict[str, str]

    def changed(self) -> list[str]:
        """开工到此刻的差异（空列表 = 一个字节都没动）。"""
        snapshot = _real_home_snapshot(self.config_dir)
        return _real_home_diff(self.before, snapshot, self.config_dir)


def _real_home_violation(guard: RealHomeGuard, lines: list[str], *, when: str) -> str:
    """判红的话术：机主那一份在哪、改成了什么、归属哪条用例。"""
    return (
        f"{REAL_HOME_GUARD_VIOLATION_MESSAGE}\n"
        f"机主家目录下的配置目录：{guard.config_dir}（归属用例 {when}）\n"
        + "\n".join(lines)
    )


#: 会话级那道守卫的读数（`pytest_sessionfinish` 收场复查时读它）
_ACTIVE_REAL_HOME_GUARD: RealHomeGuard | None = None


@pytest.fixture(scope="session", autouse=True)
def _no_writes_to_the_real_home() -> Iterator[RealHomeGuard]:
    """**autouse 兜底（会话级）**：开工记下机主真实家目录下配置目录的快照，收工复查。

    只读不写（`rglob` + `sha256`），不起任何进程、不改任何环境变量。快照在**第一条用例
    开工之前**取，因此本会话里任何一处写进去的字节都会被认出来。收场复查在
    `pytest_sessionfinish` 里（那时所有用例都已收场），另有用例级的
    `_no_writes_to_the_real_home_case` 把责任落到具体那条用例上。
    """
    global _ACTIVE_REAL_HOME_GUARD

    config_dir = _real_home_config_dir()
    guard = RealHomeGuard(config_dir=config_dir, before=_real_home_snapshot(config_dir))
    _ACTIVE_REAL_HOME_GUARD = guard
    try:
        yield guard
    finally:
        _ACTIVE_REAL_HOME_GUARD = None


@pytest.fixture(autouse=True)
def _no_writes_to_the_real_home_case(
    request: pytest.FixtureRequest, _no_writes_to_the_real_home: RealHomeGuard
) -> Iterator[RealHomeGuard]:
    """**autouse 兜底（用例级）**：这一条用例收工时，机主那一份动过就当场判红。

    与 `_no_real_dsh_launch` 同一形状：读数落在「开工快照 vs 此刻快照」上，判红的消息里
    逐条列出被创建 / 被改动的路径与前后内容哈希，因此下一眼就知道该去哪条用例里加隔离。
    """
    guard = _no_writes_to_the_real_home
    yield guard
    changed = guard.changed()
    if changed:
        pytest.fail(_real_home_violation(guard, changed, when=request.node.nodeid))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """收场复查两处：假 `dsh` 的记录，以及机主真实家目录下那份配置。

    PATH 守卫这一半兜的是「漏网那一笔落在最后一条用例收场之后」那一档：用例级的复查已经
    没有下一条用例可挂了，只有会话收场这一步还在。真家目录守卫这一半兜的是同一件事
    （最后一条用例收场之后才写进去的那一笔也算）。并行档（`pytest-xdist`）下这两道守卫只在
    各 worker 进程里成立，本函数在 controller 上对它们空转 —— 那一半由
    `_xdist_sessionfinish_bridge()` 搬运：worker 的违例记录写成 run 级共享文件、controller 汇总
    打红线并整趟判红（红线必须到主终端）。
    """
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")

    def report(message: str) -> None:
        if reporter is not None:
            reporter.write_line(message, red=True)
        else:  # pragma: no cover - 没有终端上报器时至少留一行
            print(message)

    guard = _ACTIVE_DSH_GUARD
    if guard is not None:
        leftovers = guard.unreported()
        if leftovers:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            report(_dsh_guard_violation(guard, leftovers, when="（用例收场之后才落进记录）"))

    home_guard = _ACTIVE_REAL_HOME_GUARD
    if home_guard is not None:
        changed = home_guard.changed()
        if changed:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            report(_real_home_violation(home_guard, changed, when="（会话收场复查）"))

    _xdist_sessionfinish_bridge(session)

    _drop_pending_isolated_homes()


# ---------------------------------------------------------------------------
# `pytest-xdist` 并行档的收场复查搬运：worker 写 run 级共享文件，controller 汇总判红
#
# xdist 下两道会话级守卫只在各 worker 进程里开工（controller 里它们恒为 `None`），上面
# `pytest_sessionfinish` 的收场复查因此在 controller 上空转；worker 里查出的红只到得了
# worker 自己的终端、改的也是 worker 自己的 `session.exitstatus`（定不了整趟退出码）——
# 正是「红而不可见」。搬运的办法：controller 开趟时建一个 run 级共享目录（临时目录，
# 环境变量传给各 worker），worker 的收场复查查出违例就往里写一份上报文件；controller 的
# 收场复查把上报文件逐份打红线并把整趟判红。**串行趟不建目录、环境变量不存在，这一整块
# 零动作**，行为与从前逐字节一致。目录名带 `MEETING_` 前缀只是习惯：`service_env()` 按名单
# 剔除本服务变量，这份它不认——但它只是个临时目录路径，子进程拿到也无语义。
# ---------------------------------------------------------------------------

#: controller 建的 run 级共享目录经这个环境变量传给各 worker（worker 进程从控制器继承）。
XDIST_GUARD_DIR_ENV = "MEETING_XDIST_GUARD_DIR"

#: worker 上报文件的文件名模式（`<共享目录>/worker-<worker 名>.txt`，按文件名排序汇总）。
XDIST_GUARD_REPORT_GLOB = "worker-*.txt"


def _xdist_dist_active(config: pytest.Config) -> bool:
    """这一趟是不是带着并行参数起的（controller 侧判据；没装 `pytest-xdist` 时恒 `False`）。"""
    if not config.pluginmanager.hasplugin("xdist"):
        return False
    try:
        numprocesses = config.getoption("numprocesses", None)
        dist_mode = config.getoption("dist", None)
    except (ValueError, KeyError):  # pragma: no cover - 选项没注册的布局
        return False
    return bool(numprocesses) or dist_mode not in (None, "no")


def pytest_configure(config: pytest.Config) -> None:
    """并行趟的 controller 建 run 级共享目录（worker 从控制器进程继承环境变量，不自己建）。"""
    if hasattr(config, "workerinput"):
        return
    if not _xdist_dist_active(config):
        return
    shared = Path(tempfile.mkdtemp(prefix="meeting-xdist-guards-"))
    config._meeting_xdist_guard_dir = shared  # type: ignore[attr-defined]
    os.environ[XDIST_GUARD_DIR_ENV] = str(shared)


def pytest_unconfigure(config: pytest.Config) -> None:
    """并行趟收摊：摘掉环境变量、删掉 run 级共享目录（worker 上报文件已经汇总进主终端）。"""
    shared = getattr(config, "_meeting_xdist_guard_dir", None)
    if shared is not None:
        os.environ.pop(XDIST_GUARD_DIR_ENV, None)
        shutil.rmtree(shared, ignore_errors=True)


def _xdist_sessionfinish_bridge(session: pytest.Session) -> None:
    """并行档收场复查的搬运口：worker 写上报文件，controller 汇总判红；串行趟零动作。"""
    shared = os.environ.get(XDIST_GUARD_DIR_ENV)
    if shared is None:
        return
    if hasattr(session.config, "workerinput"):
        _xdist_report_worker_violations(Path(shared))
    else:
        _xdist_collect_worker_reports(session, Path(shared))


def _xdist_worker_violation_messages() -> list[str]:
    """本进程两道会话级守卫的违例记录（各一段完整话术；没红就空列表）。"""
    messages: list[str] = []
    guard = _ACTIVE_DSH_GUARD
    if guard is not None:
        leftovers = guard.unreported()
        if leftovers:
            messages.append(
                _dsh_guard_violation(guard, leftovers, when="（用例收场之后才落进记录）")
            )
    home_guard = _ACTIVE_REAL_HOME_GUARD
    if home_guard is not None:
        changed = home_guard.changed()
        if changed:
            messages.append(_real_home_violation(home_guard, changed, when="（会话收场复查）"))
    return messages


def _xdist_report_worker_violations(shared: Path) -> bool:
    """worker 侧：违例记录写成上报文件（红到不了主终端，落盘给 controller 汇总）。

    文件名带 worker 名（`PYTEST_XDIST_WORKER`，缺席时退 `worker`），一个 worker 一份、
    互不覆盖。交回 `True` = 写了违例记录（给探针与单测一个明确的读数）。
    """
    messages = _xdist_worker_violation_messages()
    if not messages:
        return False
    shared.mkdir(parents=True, exist_ok=True)
    worker = os.environ.get("PYTEST_XDIST_WORKER", "worker")
    (shared / f"worker-{worker}.txt").write_text(
        "\n\n".join(messages) + "\n", encoding="utf-8"
    )
    return True


def _xdist_collect_worker_reports(session: pytest.Session, shared: Path) -> None:
    """controller 侧：worker 上报文件逐份打红线并整趟判红 —— 红必须到主终端。"""
    reports = sorted(shared.glob(XDIST_GUARD_REPORT_GLOB))
    if not reports:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")

    def report(message: str) -> None:
        if reporter is not None:
            reporter.write_line(message, red=True)
        else:  # pragma: no cover - 没有终端上报器时至少留一行
            print(message)

    report(
        f"xdist 收场复查：{len(reports)} 个 worker 上报了漏网"
        f"（run 级共享目录 {shared}）"
    )
    for path in reports:
        text = path.read_text(encoding="utf-8", errors="replace").rstrip()
        if text:
            report(text)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


# ---------------------------------------------------------------------------
# 安全兜底：绝不许向真实模型端点发请求（会中总结那条 HTTP 出口）
# ---------------------------------------------------------------------------

#: 打桩时报错的那句话（用例看到它就是「这条用例在往外发模型请求」）
REAL_MODEL_CALL_FORBIDDEN_MESSAGE = (
    "自动化测试不许向真实模型端点发请求（那会花机主的 token）："
    "会中总结的假客户端请从 `runtime.summary_client` 注入"
    "（`ModelClient` 只在生产路径上用）。"
)

#: 唯一放行的目标：本机回环（`localhost` / `127.0.0.0/8` / `::1`）。
#:
#: 判据按**目标地址**来，与调用点无关：将来给会中总结接一个本地假端点（回环上的 HTTP
#: 服务）时，这一档照常放行；只有真往本机之外发才拦。
LOCAL_MODEL_HOSTNAMES: tuple[str, ...] = ("localhost",)


class RealModelCallForbidden(RuntimeError):
    """**看门抛的异常**：它在出口处当场抛出，名字与消息都点名「这是测试兜底拦的」。

    **这一档收下 `RuntimeError` 子类、不照抄 `RealCaptureForbidden` 那份 `BaseException` 形状**
    （来路是本机的两条实测，写在下面）：会中总结那一次调用跑在**工作线程**上（`runtime._summary_wait()` 的
    `loop.run_in_executor` → `asyncio.wrap_future()`），请求由 ASGI 门户（anyio）驱动。
    本机实测两条路：

      - 抛 `BaseException` 直系子类：请求处确实抛出，但 ASGI 门户那次任务的收场被带走，
        紧接着 `TestClient.__exit__` 抛 `concurrent.futures.CancelledError` —— 真正有用的那句
        「这是测试兜底拦的」被一个空消息的 `CancelledError` 盖住，谁看了都不知道发生了什么；
      - 抛 `RuntimeError` 子类（就是本类）：请求仍**没有出门**，而是照既有口径回
        `summary_failed`(502)，`message` 与 `detail.error` 里带着本类的名字与被拦下的地址
        （`meeting/service/runtime.py` 的 `except Exception` 把它收成那条既定失败档），
        `TestClient` 收场干净。

    于是这一档选后者：**拦下外发**这件事两条路都做到了，而「报出来」这件事只有后者干净。
    替身的登记清单（夹具的返回值）是「拦下了哪几笔」的权威读法，用例读它就不必依赖异常
    怎么冒泡。
    """


def is_local_endpoint(url: Any) -> bool:
    """目标地址是不是本机回环（是 → 放行给真传输层）。

    认三种写法：`localhost`、IPv4 回环（`127.0.0.0/8`，含常见的 `127.0.0.1`）、IPv6 回环
    `::1`。**读不出来的一律判「不是本机」**（拦下来比漏出去安全）。
    """
    import urllib.parse

    try:
        host = (urllib.parse.urlsplit(str(url)).hostname or "").lower()
    except ValueError:  # pragma: no cover - 畸形 URL 一律按「不是本机」处理
        return False
    if not host:
        return False
    if host in LOCAL_MODEL_HOSTNAMES:
        return True
    import ipaddress

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _no_real_model_call(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """**autouse 兜底**：会中总结那条 HTTP 出口只许打本机回环，其余当场抛错。

    打桩落在 `meeting/summary/client.py` 的 `_http_post()` 真正会走到的那个名字上
    （`urllib.request.urlopen`，`_http_post()` 只在函数体里按模块属性取它）：包装函数先按
    目标地址判 —— 本机回环**原样交给真 `urlopen`**，其余记一笔进返回值并抛
    `RealModelCallForbidden`（`RuntimeError` 子类，见那个类）。

    为什么必须有这一道：`runtime.summarize()` 在 `summary_client` 为空时**静默现造一个真
    `ModelClient`**（那是生产路径该有的行为），而 `summary.base_url` 的配置默认值就是真地址
    —— 一份「端点配齐了、忘了注入假件」的用例会真的把窗口文本 POST 出去。用例一律从
    `runtime.summary_client` 注入假件（`tests/_drain_fakes.py` 那一档同源），这一道是**网**：
    谁忘了，红在出口处，机主的付费记录一个字都不动。

    返回值是被拦下的目标地址清单（`blocked`），用例要断言「拦的是哪一个」时读它。
    """
    from meeting.summary import client as summary_client

    blocked: list[str] = []
    real_urlopen = summary_client.urllib.request.urlopen

    def guarded(url: Any, *args: Any, **kwargs: Any) -> Any:
        target = str(getattr(url, "full_url", url))
        if is_local_endpoint(target):
            return real_urlopen(url, *args, **kwargs)
        blocked.append(target)
        raise RealModelCallForbidden(
            f"{REAL_MODEL_CALL_FORBIDDEN_MESSAGE}（被拦下的请求：{target}）"
        )

    monkeypatch.setattr(summary_client.urllib.request, "urlopen", guarded)
    yield blocked


# ---------------------------------------------------------------------------
# 安全兜底：会后纪要的 web 分支只许打假端点（**第七道**：绝不真往机主那台 DSH 上交接）
# ---------------------------------------------------------------------------


#: 真实 DSH web 服务端的缺省端口（`meeting/minutes/dsh_web.py::DEFAULT_DSH_URL` 的那一格）。
#:
#: **回环在这一次不能当放行判据**：真实那台服务就跑在本机回环上（`127.0.0.1:3080`），而用例的
#: 假端点起在回环的**临时端口**上 —— 判据因此是「回环 **且** 端口不是这一个」。
REAL_DSH_WEB_PORT = 3080

#: 被拦下时那句话（点名「这是测试兜底拦的」，与真实模型调用那道同一个口径）
REAL_DSH_WEB_CALL_FORBIDDEN_MESSAGE = "这是测试兜底拦下的真实 DSH web 调用"


class RealDshWebCallForbidden(RuntimeError):
    """**看门抛的异常**：会后纪要的 web 分支要往真实的 DSH web 服务端发请求时当场抛出。

    **拦下之后这一趟照常往下走**：`runner._probe_web()` 把探针抛出的异常按「DSH web 没在跑」
    处理，那些忘了注入 `probe` 的用例于是落回现成的 headless 分支（它们本来就是那么写的形态）。
    """


def _url_port(url: str) -> int | None:
    """URL 里的端口（畸形 URL 或没写端口时交回 `None`）。"""
    import urllib.parse

    try:
        return urllib.parse.urlsplit(str(url)).port
    except ValueError:  # pragma: no cover - 畸形 URL 一律按「不是假端点」处理
        return None


@pytest.fixture(autouse=True)
def _no_real_dsh_web_call(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """**autouse 兜底**：会后纪要的 web 分支只许打「回环上的假端点」，真实那台当场拦下。

    打桩落在 `meeting/minutes/dsh_web.py` **唯一**的 HTTP 出口 `_post_json()` 上：包装函数先按
    目标地址判 —— 回环**且**端口不是 `REAL_DSH_WEB_PORT` 的原样交给真实现（用例起在临时端口上的
    假端点因此照常工作）；其余记一笔进返回值并抛 `RealDshWebCallForbidden`。

    为什么必须有这一道：`minutes.dsh_url` 的缺省值就是**真实地址**（`http://127.0.0.1:3080`），
    而 `runner.run()` 在拉起之前**一定会探一次**、探到在跑就会**真的交接**。少了这道网，
    一条忘了注入假件的用例会在机主开着 DSH GUI 时往那台服务上建真工作区、真会话、发真提示词
    —— 那是真花钱、也真在用户的 GUI 里落东西。返回值是被拦下的地址清单，用例要断言
    「拦的是哪一个」时读它。

    **拦下之后按「DSH web 没在跑」算**（探针捕获异常 → 交回 False → 走 headless 分支）：
    这正是那些没打算验 web 分支的用例要的形态，它们一行都不用改。
    """
    from meeting.minutes import dsh_web

    blocked: list[str] = []
    real_post = dsh_web._post_json

    def guarded(url: str, method: str, payload: bytes, cookie: str, timeout: float) -> Any:
        target = str(url)
        if is_local_endpoint(target) and _url_port(target) != REAL_DSH_WEB_PORT:
            return real_post(url, method, payload, cookie, timeout)
        blocked.append(target)
        raise RealDshWebCallForbidden(
            f"{REAL_DSH_WEB_CALL_FORBIDDEN_MESSAGE}（被拦下的请求：{target}）"
        )

    monkeypatch.setattr(dsh_web, "_post_json", guarded)
    yield blocked


# ---------------------------------------------------------------------------
# 安全兜底：DSH 触达原语一碰就红（**第八道**：探针 / 交付 / headless 拉起，测试里零触达）
#
# 事故背景（2026-09 夜）：一个复现脚本调了真的 `run_session()` 顶层入口，收尾按设计通知了
# 纪要层，纪要层探到机主正在跑的 DSH web 服务端、走了 web 分支真交付 —— DSH 侧多出工作区、
# 按纪要流程拉起了多个 subagent。产品侧的隔离闸（`announce_minutes` / `deliver` /
# `allow_dsh` 三层默认拒绝）已把这条路封死，这一道是**测试基建**那一层：测试进程里谁碰到
# 探针 / 交付 / headless 拉起这三个原语，就当场把那条用例判红 —— 「用例里不该有 DSH 触达」
# 因此不靠单条用例自觉，而靠夹具这一张网。
# ---------------------------------------------------------------------------


#: 被拦下时那句话（点名「这是测试兜底拦的」，与前面几道同一口径）
REAL_DSH_DELIVERY_FORBIDDEN_MESSAGE = (
    "测试碰到了 DSH 触达原语（探针 / 交付 / headless 拉起）：测试进程里这三件事零触达。"
    "专门测交付协议或拉起机制的用例请显式请 `allow_dsh_protocol_stubs` 夹具"
    "（它们用假服务器 / 假桩，不碰真端口）。"
)


class RealDshDeliveryForbidden(RuntimeError):
    """**看门抛的异常**：DSH 触达原语在测试进程里被调到时当场抛出（记录之外，截断流程）。"""


@pytest.fixture(autouse=True)
def _no_real_dsh_delivery(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[list[str]]:
    """**autouse 兜底**：探针 / 交付 / headless 拉起三个原语桩成「记录调用即判红」。

    打桩落在三个**模块属性**上（`meeting.minutes.dsh_web.probe` / `.handoff` 与
    `meeting.minutes.launch.launch_headless`）：`runner.run()` 在调用那一刻才按模块属性取
    它们，桩因此与导入次序无关。桩做两件事 —— 把这一次调用（原语名 + 参数）记进记录并抛
    `RealDshDeliveryForbidden` 截断流程；用例收场时记录上有新笔迹、而这条用例没有显式请
    `allow_dsh_protocol_stubs` 夹具，就 `pytest.fail` 判红（异常被 `except Exception` 兜掉
    的那几条漏网路径由这一步兜住）。收场之前先把产品那条「纪要生成」daemon 线程 join 掉
    （与第五道守卫同一个理由：真拉起点起完线程就返回）。

    **豁免是显式的**：专门测交付协议（`tests/test_minutes_dsh_web.py`）与拉起机制
    （`tests/test_minutes_launch.py`）的用例在自己模块的 `pytestmark` 上请
    `allow_dsh_protocol_stubs` —— 它们用假服务器 / 假桩，不碰真实端口；产品侧还有
    `DeliveryForbidden` 那道默认拒绝闸兜底。
    """
    from meeting.minutes import dsh_web
    from meeting.minutes import launch as launch_module

    calls: list[str] = []

    def _blocked(primitive: str) -> Any:
        def stub(*args: Any, **kwargs: Any) -> Any:
            calls.append(f"{primitive}(args={args!r}, kwargs={kwargs!r})")
            raise RealDshDeliveryForbidden(
                f"{REAL_DSH_DELIVERY_FORBIDDEN_MESSAGE}（被拦下的调用：{primitive}）"
            )

        return stub

    exempt = "allow_dsh_protocol_stubs" in request.fixturenames
    if not exempt:
        monkeypatch.setattr(dsh_web, "probe", _blocked("dsh_web.probe"), raising=False)
        monkeypatch.setattr(dsh_web, "handoff", _blocked("dsh_web.handoff"), raising=False)
        monkeypatch.setattr(
            launch_module, "launch_headless", _blocked("launch.launch_headless"), raising=False
        )
    yield calls
    if exempt:
        return
    _wait_for_the_minutes_thread()
    if calls:
        pytest.fail(
            f"{REAL_DSH_DELIVERY_FORBIDDEN_MESSAGE}\n"
            "被拦下的调用（归属用例 "
            f"{request.node.nodeid}）：\n  " + "\n  ".join(calls)
        )


@pytest.fixture()
def allow_dsh_protocol_stubs() -> Iterator[None]:
    """**显式 opt-out**：这一条用例专门测交付协议 / 拉起机制（假服务器 / 假桩，不碰真端口）。

    请了它，第八道守卫就不打桩、收场也不判红 —— 放开的只是「这一条用例的触达登记」；
    产品侧那道 `DeliveryForbidden` 默认拒绝闸一个字不动（这些用例按新契约显式传
    `allow_dsh=True`），真端口那一侧由第七道守卫兜着。谁请了它，谁的名字就出现在那条
    用例的源码里，审计一眼可见。
    """
    yield None


# ---------------------------------------------------------------------------
# 五个夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_out_root(tmp_path: Path) -> Path:
    """**临时场次放置根**（测试层给 `start` 的 `args.dir` 预建目录用；产品没有输出根）。"""
    root = tmp_path / "out"
    root.mkdir(parents=True, exist_ok=True)
    return root


#: 本用例的临时家目录名（`isolated_config_dir` 与 `app_client` **共用这一个**）
CASE_HOME_DIR_NAME = "home"


def case_home(tmp_path: Path) -> Path:
    """本用例的临时家目录（两个夹具都从这里取，谁先来谁建）。

    家目录隔离的硬约束（设计口径）：`HOME` 与 `USERPROFILE` 两个变量都指向它。
    两个夹具同时使用时必须指向**同一个**目录，否则「隔离」这件事在其中一个夹具的视角下就
    落到另一个目录上了（配置与日志读写的落点会与断言的落点错开）。
    """
    home = Path(tmp_path) / CASE_HOME_DIR_NAME
    home.mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture()
def isolated_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """**隔离的临时配置目录**：家目录重定向 + 隔离的临时日志目录。

    隔离手法照设计口径的硬约束：**重定向家目录**（`HOME` 与 `USERPROFILE` 两项都设），
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


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """**autouse**：每一条用例的 `HOME` / `USERPROFILE` 都指到本用例自己的临时家目录。

    为什么必须**每一条用例**都做（而不是只给「自己写了隔离」的用例做）： 设计
    给 `meeting post` 的入口加了一步配置前奏（`post()` → `config_module.load()` +
    `set_current()`），于是**任何**在进程内走真入口的用例都会去读 `~/.config/meeting/config.json`
    —— 没做隔离的那些读的是**机主真实那一份**，机主的取值（例如 `minutes.hook = "null"`）
    因此进了测试进程的生效配置。这一步是结构性的：不管哪条用例（现在写的、以后加的）走真
    入口，读到的都是它自己的临时家目录，机主那一份连读都读不到。

    **落点不放在 `tmp_path` 下面**（`tempfile.mkdtemp()` 单开一个，收场删掉）：`tmp_path` 下
    那个 `home` 名字是各用例自己用的（`(tmp_path / "home").mkdir()` 这种写法在本仓有 20 多处，
    它们都假定它还不存在），预建它会把那些用例撞成 `FileExistsError`。用例自己把 `HOME` 指到
    别的临时目录（`isolated_config_dir` / `meeting_env` / `app_client` 与
    `test_state_machine.py` 那一族）照旧以它自己的为准：`monkeypatch` 后设的覆盖先设的。

    **故意不往这个家目录里写 `config.json`**：产品缺省（`minutes.hook = "auto"` 等）与
    「没读过配置」在进程内是同一件事（`config.get()` 无生效配置时退回 `DEFAULT_VALUES`），
    写一份测试层缺省进去反而会把「入口有没有真的读配置」这一类断言搅浑
    （`tests/test_cli_post_config.py` 的用例自己写它要的那一份）。

    **`MEETING_CONFIG_DIR` 在这里一并摘掉**：它整组改写 `config_dir()`（注册表、
    `config.json`、日志默认落点全跟着走），机主环境里设了它，家目录重定向就被整组
    拐去那个目录——隔离与真家目录守卫盯的落点都会落空。
    """
    home = Path(tempfile.mkdtemp(prefix="pytest-isolated-home-"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    from meeting import config as config_module

    monkeypatch.delenv(config_module.ENV_CONFIG_DIR, raising=False)
    try:
        yield home
    finally:
        _drop_isolated_home(home)


#: 收场时再删一次的隔离家目录（Windows 上服务级日志的句柄还开着时**当场删不掉**：
#: `post()` 那一趟会把 `<家目录>/.config/meeting/logs/meeting.log` 打开，用例收场时
#: 文件仍被占用，`shutil.rmtree` 会以 `PermissionError` 收场）。于是这里先记下来，
#: 会话收场（`pytest_sessionfinish`）关掉那些句柄之后再删一遍。
_PENDING_ISOLATED_HOMES: list[Path] = []


def _drop_isolated_home(home: Path) -> None:
    """删掉一个隔离家目录；删不掉（句柄还占着）就记进待删清单，会话收场再删一次。"""
    shutil.rmtree(home, ignore_errors=True)
    if home.exists() and home not in _PENDING_ISOLATED_HOMES:
        _PENDING_ISOLATED_HOMES.append(home)


def _drop_pending_isolated_homes() -> None:
    """会话收场：先关掉还开着的服务级日志句柄（文件锁随之释放），再把待删的删干净。

    只在本会话真的留下过删不掉的家目录时才动手，且吞掉一切异常 —— 收场清理失败不该
    改变这一趟的测试结论（真正要看的是上面两道守卫）。
    """
    if not _PENDING_ISOLATED_HOMES:
        return
    try:
        from meeting import logging_setup

        logging_setup.reset_for_tests()
    except Exception:  # pragma: no cover - 日志层抽风时照旧尽力删
        pass
    for home in list(_PENDING_ISOLATED_HOMES):
        shutil.rmtree(home, ignore_errors=True)
        if not home.exists():
            _PENDING_ISOLATED_HOMES.remove(home)


@pytest.fixture(autouse=True)
def _restore_effective_config() -> Iterator[None]:
    """**autouse**：用例收场时把模块级生效配置（`config_module.set_current()`）原样放回。

    与 `_isolated_home` 配对：家目录隔离挡住「读到机主那一份」，这一条挡住「读到之后不还」。
    `post()` 的配置前奏会把 `config.set_current(loaded)` 设成模块级的，而**它是进程级全局**：
    用例不还原，它的取值就跟着后面每一条用例走（实测后果是 `minutes.hook = "null"` 串档，
    24 条用例倒在 `configured_branch()` 上）。这里在**每一条用例**开工时记下当时的生效配置、
    收场时放回，任何一条用例的现场因此都渗不出去。

    `app_client()` / `dev_runtime` 这些夹具自己也会还原（那一层不动），本夹具是**兜底**那
    一层：谁忘了还原都跑不掉。
    """
    from meeting import config as config_module

    previous = config_module.current()
    yield
    config_module.set_current(previous)


@pytest.fixture()
def service_process(tmp_path: Path) -> Iterator[Any]:
    """**真服务子进程夹具**：给出一个「起一个 `meeting web` 子进程」的工厂。

    用法：proc = service_process(home=meeting_env.home, extra_env=meeting_env.env)
    （工厂自己注册收场，用例不必写 `finally`）。

    能力（设计冻结）：`base_url`（`MEETING_PORT=0` + 子进程启动输出解析）、
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
        home, {"web.port": 8765}
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
# 设计追加的两个夹具（名字固定：`meeting_env` / `sse_client`）
# ---------------------------------------------------------------------------


@dataclass
class MeetingEnv:
    """一个用例的一套隔离环境（`meeting_env` 夹具给出的那一个）。

    `out_root` 沿用旧名，语义变了：它只是**测试层的场次放置根**（给 `start` 的
    `args.dir` 预建工作区目录用），不再是任何配置键的值。
    """

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
    """**本用例的一套隔离环境**：临时场次放置根 + 临时 log 目录 + **家目录重定向** + 服务变量。

    逐项照 §3.8 的「子进程环境（每条测试的必备项）」：

      - `MEETING_NO_BROWSER=1`（**每一条子进程测试都必须设上**，否则真机会弹浏览器）；
      - `MEETING_PORT=0`、`MEETING_LOG_DIR=<本用例临时目录>`；
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
        "MEETING_LOG_DIR": str(log_dir),
    }
    return MeetingEnv(
        home=home, out_root=out_root, log_dir=log_dir, config_dir=config_dir, env=env
    )


@pytest.fixture()
def sse_client(service_process: Callable[..., Any]) -> Iterator[Callable[..., SseClient]]:
    """**在 `service_process` 上开一条 SSE 订阅并保证关闭**。

    用法：client = sse_client(proc)（夹具自己记下来，用例结束时统一 `close()`；
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

#: 单条用例（setup + call + teardown 三段合计）的墙钟上限（秒）。
#: 这是**串行趟的缺省值，逐字节不动**；并行档（`pytest-xdist`）的放宽口径走下面的环境变量。
CASE_WALL_CLOCK_LIMIT_SECONDS = 5.0

#: 看门阈值的环境变量覆盖口径（并行档在命令行上设它，如
#: `$env:MEETING_CASE_WALL_CLOCK_LIMIT_SECONDS = "8"`）：不设 = 缺省 5.0 秒，串行趟因此与
#: 从前完全一致；设了必须是正数，读不出来（名字拼错 / 非数字 / ≤ 0 / 空串）一律退回
#: 缺省 5.0 —— 宁可从严，不让看门悄悄失明。
CASE_WALL_CLOCK_LIMIT_SECONDS_ENV = "MEETING_CASE_WALL_CLOCK_LIMIT_SECONDS"


def case_wall_clock_limit() -> float:
    """本趟生效的看门阈值（秒）：环境变量覆盖口径，缺省 `CASE_WALL_CLOCK_LIMIT_SECONDS`。"""
    raw = os.environ.get(CASE_WALL_CLOCK_LIMIT_SECONDS_ENV)
    if raw:
        try:
            limit = float(raw)
        except ValueError:
            return CASE_WALL_CLOCK_LIMIT_SECONDS
        if limit > 0:
            return limit
    return CASE_WALL_CLOCK_LIMIT_SECONDS

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
    """最后一段（`teardown`）之后汇总三段耗时：**合计超过本趟阈值就把这条用例判红**。

    阈值取 `case_wall_clock_limit()`（缺省 5.0 秒，并行档可经环境变量放宽）。判红方式是在
    `teardown` 的 report 上打一条 `longrepr`（报出用例名与三段各自的耗时），于是这条超时
    用例会以「失败」出现在 `pytest -q` 的统计里、挂在自己的名字下，谁都能一眼看到是哪一条
    慢了。耗时数据只从 pytest 自己的 report 里取（不额外计时、不加 fixture），因此看门本身
    对被测用例零影响。
    """
    outcome = yield
    report = outcome.get_result()
    if report.when != "teardown":
        return
    durations = _CASE_DURATIONS.pop(item.nodeid, None)
    if not durations:
        return
    limit = case_wall_clock_limit()
    total = sum(durations.values())
    if total <= limit:
        return
    report.outcome = "failed"
    report.longrepr = (
        f"{item.nodeid} 单条用例合计耗时 {total:.2f}s，超过上限 "
        f"{limit:.1f}s"
        f"（setup {durations.get('setup', 0.0):.2f}s / "
        f"call {durations.get('call', 0.0):.2f}s / "
        f"teardown {durations.get('teardown', 0.0):.2f}s）"
    )
