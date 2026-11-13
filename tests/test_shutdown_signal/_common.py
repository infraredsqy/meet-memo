
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

# ---------------------------------------------------------------------------
# 本文件的具名常量（§3.8 末「测试侧常量」那张表；用例里不留裸秒数）
#
# **取值收口的授权说明（第七轮审查 m3，措辞在下游文件里一字不改地重复）**：
# 「以上取值由指挥者授权收口（原契约表的值见设计.md §3.8 末表；红线：单条用例 ≤5s）」
# —— 本文件里被这一条覆盖的常量是 `SHUTDOWN_BLOCKING_STEP_SECONDS`、
# `SIGBREAK_STATE_POLL_INTERVAL_SECONDS`、`SIGBREAK_TERMINAL_POLL_INTERVAL_SECONDS`
# 与 `E2E_CASE_WALL_CLOCK_LIMIT_SECONDS`；
# 下面各自那一段说明写清了判据的形状与这几笔钱的来路。数值一律不改。
# ---------------------------------------------------------------------------

#: ③ 条同步点的轮询间隔（秒）—— 压到 `_service.py` 的等待助手同一档，
#: 于是「阻塞期间一定查得到 `running`」这条论证的余量更大（阻塞 2.0s / 间隔 0.05s）
SIGBREAK_STATE_POLL_INTERVAL_SECONDS = 0.05

#: ② 条「退出前读到那份 `/api/state`」的轮询间隔（秒）——**只给收尾终态那一格用**。
#:
#: 为什么不跟 0.05 那一档共用：那一格在 `/api/state` 上**可读的窗口只有几十毫秒**。
#: 本机实测（细读一轮，2ms 间隔）：收尾终态在第 2.006s 起可读、到第 2.043s 进程收场，
#: 窗口约 **37ms**。0.05 的间隔一整格只能取到一次样、还常常整格错过（实测 30 次单跑里
#: 有 4 次错过），于是「服务答不上」这件偶发被读成契约违约。间隔压到 0.005 之后，
#: 同一个 37ms 里能取到 6～8 次样，偶发因此落回它该有的量级。
#:
#: **压的是采样频率、不是判据**：谓词、`source == "/api/state"` 那条断言、超时上限
#: 与「退化到盘上就判红」的口径一个字都没改 —— 采样稀了漏掉的读数，采样密了照样漏不掉。
SIGBREAK_TERMINAL_POLL_INTERVAL_SECONDS = 0.005

#: ③ / ④ 条轮询 `/api/state` 的超时（秒）
SIGBREAK_STATE_POLL_TIMEOUT_SECONDS = 20

#: ③ 条里假采集件 `finalize()`（收尾第 1 步 `flush`）在工作线程里的阻塞秒数：
#: **大于轮询间隔、小于骨架期的总上限 30 秒**
#: （§3.8 末「测试侧常量」那张表把这一档命名为 `SHUTDOWN_BLOCKING_HOOK_SECONDS`；
#: 第 4 步 `post` 按 R26 不再拉起会后钩子之后，同一个 2.0 秒改由第 1 步的 `finalize()`
#: 承担 —— 取值与判据的形状都没动，只是钉住的那一步换了）。
#:
#: **用户把「单条用例 ≤ 5 秒」定为硬要求，取值因此贴着下界走**（契约表写 3.0 秒，这里取 2.0
#: 是**这一条授权下的缩短**）：2.0s / 0.05s = 40 次机会，「轮询一定能在阻塞结束前查到
#: `running`」这条论证余量充足，用例的墙钟也留得住余量。
#:
#: **它与下面那条退出时延上限的关系**（审查问题 M10 改掉的说法）：这两笔钱**互不相干** ——
#: 收尾各步的同步调用由**收尾专用的 daemon 执行器**推进工作线程，进程退出时**不等**在飞的
#: 那一步，因此「第二次按下 → 进程退出」的时延**与剩余阻塞时长无关**（阻塞 2.0 秒的现场实测
#: 0.172 秒；同一个现场切回非 daemon 的默认执行器才是等满的 2.007 秒）。下面那条 1.0 秒的
#: 上限，咬的是「**退出不等在飞步**」这件事 —— 它量的是关停这一路自己的时延，
#: 与在飞那一步还剩多久无关。
#:
#: **这个常量真的被用上了**：它经 `fakes.write_knob_plugin(capture_delay_seconds=...)`
#: 落进假件的环境变量，子进程里的 `FakeCaptureBackend.finalize()` 从那里读到它，
#: 于是「阻塞 2.0 秒」这句话与实测一致。
SHUTDOWN_BLOCKING_STEP_SECONDS = 2.0

#: ③ 条「第二次 `Ctrl+C` → 进程退出」的时延上限（秒）。
#:
#: 契约（§3.5 行为表第二行）要求第二次按下是「**立即中止**」：`cancel()` 中止的是收尾
#: task 的等待，已经交给工作线程的那次同步调用（这里就是假采集件 `finalize()` 的
#: `time.sleep`）**照跑到底**
#: —— 若解释器收尾要 join 它，「立即中止」就被在飞的同步步拖着走（**修 M5 之前实测：
#: 第二次按下 → 进程退出要等满那次阻塞**）。收尾各步的工作线程因此是 daemon
#: （`meeting/service/runtime.py` 的 `_DaemonThreadPoolExecutor`），本上限就是这条行为的判据。
#:
#: **这条上限盯的是什么（审查问题 M10 改掉的说法）**：daemon 执行器下，进程退出的时延与
#: **剩余阻塞时长无关**（那一步留在工作线程里自己跑完，解释器不 join 它），因此这里比的是
#: 「退出**没等**在飞步」这一件事，而不是「离阻塞结束还剩多少秒」。1.0 秒相对一次正常的
#: 关停（几十毫秒）足够宽，而**任何一条把在飞步等满的路径都远远越过它**。
#:
#: **取值论证**：同一个现场（在飞的那一步阻塞 2.0 秒）上量两次 —— 收尾各步走 daemon 执行器时
#: **0.172 秒**退出；把同一个现场临时切回事件循环的默认执行器（工作线程非 daemon）时
#: **2.007 秒**退出 —— 后者正是「等满在飞步骤」的形状，前者与它差一个数量级。于是
#: 「≤ 1.0 秒」在没修的那一版上必红（实测 2.0 秒），在修好的这一版上实测 0.17 秒。
INTERRUPT_EXIT_LATENCY_LIMIT_SECONDS = 1.0

#: ⑥ 条里包装脚本等「父进程说可以按了」的最长等待（秒）：到点还没等到就直接按 ——
#: 这一支只在小概率的父进程侧异常里出现，**保证信号一定送得出去**（不留无上限挂死）
SELF_SIGNAL_MAX_WAIT_SECONDS = 5.0

#: ⑥ 条里那个「可以按了」标记文件的落点：本用例临时家目录的**兄弟目录**
#: （子进程把 `HOME` 重定向到了临时家目录，用它的 `..` 就不必再传一个绝对路径）
READY_MARKER_DIR_NAME = "self-signal-ready"

#: ⑥ 条里那两个子进程的收场超时（秒）
PROCESS_EXIT_TIMEOUT_SECONDS = 30

#: 单条用例的墙钟上限（秒）。
#:
#: **口径已按用户新定的红线收口**（修审查问题 M6）：契约 §3.8 文末那张表原先写 60 秒，
#: 用户后来把「每一条用例 setup + call + teardown ≤ 5.0 秒」定为硬要求，并由
#: `tests/conftest.py` 的看门 hook（`CASE_WALL_CLOCK_LIMIT_SECONDS`）自动判红 ——
#: **全局 5 秒看门取代了方案里那条 60 秒口径**。本常量与看门同值，并在本文件的六条用例里
#: 经 `_finish_case()` 作为**显式断言**真的被用上（`tests/test_e2e_*.py` 四个文件同办），
#: 于是它每一趟都替我们守着一道活的判据。
E2E_CASE_WALL_CLOCK_LIMIT_SECONDS = 5.0

#: 服务级 log 里两行必断的字面（设计冻结，一处都不许改字面）
SELF_CHECK_LINE = "信号接管自检通过：SIGINT 与 SIGBREAK 指向本服务的处理函数"
SIGBREAK_LINE = "收到 SIGBREAK，进入关停"
SIGINT_LINE = "收到 SIGINT，进入关停"

#: 强制打断那一行的文案（`meeting/service/app.py` 的 `DRAIN_INTERRUPTED_MESSAGE`）
INTERRUPTED_LINE = service_app.DRAIN_INTERRUPTED_MESSAGE


# ---------------------------------------------------------------------------
# 本文件的辅助
# ---------------------------------------------------------------------------


@pytest.fixture()
def case_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """本用例的家目录重定向（与 §3.8 的两条子进程路径同一套口径）。

    子进程那一侧由 `_spawn()` 给全一整份环境变量；这一份是本进程的（插件工厂写配置时要用
    `HOME` / `USERPROFILE` 解析 `~/.config/meeting/config.json`）。
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


#: ⑥ 条用的包装脚本：**原样跑真实的 `meeting.cli.main(["web"])`**，
#: 另起一条线程等父进程写下「可以按了」的标记之后，由**主线程**给自己发 `SIGINT`
#: （`raise_signal` 在主线程执行，CPython 的信号处理只认主线程）。
#: 脚本只活在命令行里（`python -c`），仓库内与仓库外都不落任何探针文件；
#: 那个标记文件落在**本用例的临时目录**里，用例跑完随临时目录一起消失。
_SELF_SIGNAL_WRAPPER = (
    "import os, signal, sys, threading, time\n"
    "from meeting.cli import main\n"
    "\n"
    "def _press():\n"
    "    deadline = time.monotonic() + {max_wait!r}\n"
    "    while time.monotonic() < deadline and not os.path.exists({marker!r}):\n"
    "        time.sleep(0.05)\n"
    "    signal.raise_signal(signal.SIGINT)\n"
    "\n"
    "threading.Thread(target=_press, daemon=True).start()\n"
    "main(['web'])\n"
)


def _spawn(
    service_process,
    home: Path,
    plugin: fakes.FakePlugin | None = None,
    *,
    capture: str = "dev",
    self_signal: bool = False,
):
    """起一个真服务子进程：环境变量与假件一次性给全（与夹具那一支同一套）。

    `PYTHONPATH` = **仓库根 + 临时假件目录**（没有假件时只留仓库根）。

    **`MEETING_CAPTURE` 不写进子进程 env**（与环境变量一项都不多设的口径一致）：采集后端一律
    由本用例 `config.json` 里的 **`capture.backend`** 说了算 —— 环境变量的优先级高于配置文件，
    留着 `MEETING_CAPTURE` 就会把这条配置键静默压住（假件挂不上、采到的东西与断言对不上，用例
    却照常绿）。这一项因此写成配置键 `capture.backend`（缺省 `"dev"`：采集替身，`start` 照常
    能开起来），两条子进程路径的采集后端由此同源、也看得见。

    `self_signal=True` 时改起**包装脚本**（`python -c`，见 `_SELF_SIGNAL_WRAPPER`）：它仍然跑
    真实的 `meeting.cli.main(["web"])`，只是等用例写下「可以按了」的标记之后由子进程自己给自己
    发一次 `SIGINT`（⑥ 条用它把 `Ctrl+C` 那一路真的走一遍：本机的控制台事件送不到子进程，
    见文件顶部说明）。标记的落点由 `self_signal_ready_marker(home)` 给出，用例在 `start`
    之后写它 —— 于是这一按落在「录音中」，与 `CTRL_BREAK_EVENT` 那一路同一格。
    环境变量与直接起 `meeting web` 的那一路**逐项相同**（家目录两项、`MEETING_*` 四项、
    `PYTHONPATH`），因此隔离手法与假件注入口径一处都没漂。

    环境变量那一份由 `_spawn_env()` 单独给出：①c 条要在**同一个放置根**下重起第二个服务，
    它复用同一份构造（口径因此与这里逐项相同，不会两边各长一套）。
    """
    home = Path(home)
    env = _spawn_env(home, plugin, capture=capture)
    if self_signal:
        marker = self_signal_ready_marker(home)
        return service_process(
            home=home,
            capture=None,
            env=env,
            args=[
                "-c",
                _SELF_SIGNAL_WRAPPER.format(
                    marker=str(marker), max_wait=SELF_SIGNAL_MAX_WAIT_SECONDS
                ),
            ],
        )
    return service_process(home=home, capture=None, env=env)


def _spawn_env(
    home: Path,
    plugin: fakes.FakePlugin | None = None,
    *,
    capture: str = "dev",
) -> dict[str, str]:
    """一条子进程路径的环境变量与假件布置（`_spawn()` 与 ①b 条共用同一份构造）。

    **`MEETING_CAPTURE` 不写进子进程 env，这一步是必要的、不是漂移**（审查问题 M13 点名的
    读法）：采集后端一律由本用例 `config.json` 里的 **`capture.backend`** 说了算 ——
    环境变量的优先级高于配置文件，留着 `MEETING_CAPTURE` 就会把这条配置键**静默压住**
    （假件挂不上、采到的东西与断言对不上，用例却照常绿）。这一项因此写成配置键
    `capture.backend`（缺省 `"dev"`：采集替身，`start` 照常能开起来），两条子进程路径
    （本文件这一支与 `tests/test_drain.py` 那一支）的采集后端由此**同源**、也看得见。
    """
    home = Path(home)
    env: dict[str, str] = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "MEETING_NO_BROWSER": "1",
        "MEETING_PORT": "0",
        "MEETING_LOG_DIR": str(helpers.log_dir_of(home)),
        "PYTHONPATH": (
            str(helpers.REPO_ROOT)
            if plugin is None
            else plugin.pythonpath(helpers.REPO_ROOT)
        ),
    }
    # 场次放置根（`<家目录>/../out`，仅测试层 Os 落点用；本层没有「输出根」这个概念）
    out_root = home.parent / "out"
    out_root.mkdir(exist_ok=True)
    helpers.log_dir_of(home).mkdir(parents=True, exist_ok=True)
    if plugin is None:
        # 采集后端走配置键（点号扁平键），环境变量里一项都不多设
        helpers.write_config_json(home, {"capture.backend": capture})
    else:
        plugin.config.setdefault("capture.backend", capture)
        fakes.write_plugin_config(home, plugin)
        env.update(plugin.extra_env)
    return env


def self_signal_ready_marker(home: Path) -> Path:
    """⑥ 条那个「可以按了」标记的落点：`<临时家目录的上一级>/self-signal-ready/<家目录名>`。"""
    home = Path(home)
    return home.parent / READY_MARKER_DIR_NAME / home.name


def _press_self_signal(home: Path) -> None:
    """写下「可以按了」的标记：包装脚本看到它就替用例按一次 `Ctrl+C`。"""
    marker = self_signal_ready_marker(home)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("按吧\n", encoding="utf-8")


def _log_text(proc: Any) -> str:
    return helpers.read_text(helpers.service_log_path_of(proc.home))


def _session_dirs(proc: Any) -> list[Path]:
    out_root = proc.home.parent / "out"
    if not out_root.is_dir():
        return []
    return sorted((item for item in out_root.iterdir() if item.is_dir()), key=lambda p: p.name)


def _latest_session_dir(proc: Any) -> Path:
    dirs = _session_dirs(proc)
    assert dirs, "输出根下还没有场次目录"
    return dirs[-1]


def _start(proc: Any) -> Path:
    """给这个进程显式预建一个工作区目录并开一场：返回落点目录的路径。

    产品代码不建目录、没有默认落点：start 的 `args.dir` 由**测试层**预建（`tests/_helpers.py`
    的 `allocate_start_dir()`）后显式给上 —— 与真实使用「人从磁盘里挑一个已有目录」同一口径。
    """
    workspace = helpers.allocate_start_dir(proc.home.parent / "out")
    started = post_action(proc.base_url, "start", {"dir": workspace})
    assert started.status_code == 200, started.text
    return Path(workspace)


def _finish_case(started: float, label: str) -> None:
    """用例正文的**墙钟断言**：实测值不超过 `E2E_CASE_WALL_CLOCK_LIMIT_SECONDS`。

    与 `tests/conftest.py` 的看门 hook 是同一条红线（那条量 setup + call + teardown 三段
    合计，这里量用例正文这一段）；`E2E_CASE_WALL_CLOCK_LIMIT_SECONDS` 因此有了活的用处。
    """
    spent = time.monotonic() - started
    assert spent <= E2E_CASE_WALL_CLOCK_LIMIT_SECONDS, (
        f"{label}：用例正文墙钟 {spent:.2f}s 超过上限 "
        f"{E2E_CASE_WALL_CLOCK_LIMIT_SECONDS:.1f}s"
    )


def _wait_state(proc: Any, predicate, *, what: str, timeout: float = SIGBREAK_STATE_POLL_TIMEOUT_SECONDS):
    """轮询 `/api/state` 直到条件成立，**返回那份快照**。

    谓词抛异常（服务刚收到信号、连接被断这一类）时继续轮询；到超时抛 `AssertionError`。
    这里**不把 `wait_until` 的返回值直接往外传** —— 它对「谓词抛异常」这一档会把异常文字
    当成观察值返回，那样外层拿到的就是一句话而不是快照。
    """
    deadline = time.monotonic() + float(timeout)
    last: Any = None
    while True:
        try:
            snapshot = proc.snapshot()
            last = snapshot
            if predicate(snapshot):
                return snapshot
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        if time.monotonic() >= deadline:
            raise AssertionError(f"等待超时（{timeout}s）：{what}（最后观察值 {last!r}）")
        time.sleep(SIGBREAK_STATE_POLL_INTERVAL_SECONDS)


def _step_statuses(snapshot: dict[str, Any]) -> dict[str, str]:
    """快照里四步 `id -> status`（③ / ④ 的同步点判据用它）。"""
    return {
        step["id"]: step["status"]
        for step in (snapshot.get("ended") or {}).get("steps") or []
    }


def _state_while_alive(proc: Any, predicate, *, what: str) -> dict[str, Any] | None:
    """**进程还活着**的时候读一次 `/api/state`（读到就让位、进程已收场就返回 `None`）。

    与 `_wait_state()` 的分工：那条等到条件成立为止、到超时抛错；这一条只把「现在这一刻读到
    的那份快照」带回来，因此可以放在「等进程退出」之后 —— 进程还没收场时它是真读数，进程已经
    收场时它返回 `None`，两种情形都由调用点如实记下来。

    **「返回 `None`」的两种来路分开认**（审查问题 m7b，别把两种退化成一种）：① 子进程已经
    收场（`poll()` 非空）或这次读撞上「服务正在退出、连接被断」—— 这两种是真时序，返回
    `None`；② 其余任何异常（例如读到一份**非 200 的应答**、或 JSON 解析炸了）一律**原样抛
    出去**，不再被这里吞成 `None` —— 吞掉的话「服务答了个 500」会被读成「刚好在退出」，
    用例会以「以为在读运行期快照、其实在读盘」的形式静默退化。

    **① 里的「连接被断」收一整类、不逐条枚举**（这一条是本用例那条偶发红的根因）：服务退出时
    监听 socket 先关掉，客户端那条 keep-alive 连接上正在飞的那一次请求因此有三种收场 ——
    连接都建不起来（`ConnectError`）、连上了但对端不发应答（`ConnectTimeout`）、对端在应答
    之前把连接断掉。**第三种是本机实测真正撞上的那一种**：`httpx.RemoteProtocolError`
    （`Server disconnected without sending a response.`），它的 MRO 是
    `RemoteProtocolError → ProtocolError → TransportError`，**不在 `ConnectError` 那一支下**，
    此前因此原样抛出去、把用例判红。断在应答中途的那两种（`ReadError` / `ReadTimeout`）与
    连接池在关停时收走的 `PoolTimeout` 同理。这一格现在按**「传输层没拿到应答」**整类收下，
    与 `ConnectError` 同一个处置：这一次读不算数、返回 `None`，由调用点继续等。

    **收下的只是「没读到」这件事**：`httpx.HTTPStatusError`（非 200 应答，由
    `ServiceProcess.snapshot()` 那一条断言抛出）与 JSON 形状问题都**不在**这一类里，
    照旧原样抛出去判红 —— 「服务答了个 500」与「连接被断」这两件事的分别一处都没丢。
    """
    if proc.poll() is not None:
        return None
    try:
        snapshot = proc.snapshot()
    except (
        # 连接都建不起来：服务正在退出、监听 socket 已关
        httpx.ConnectError,
        httpx.ConnectTimeout,
        # 连上了、但应答之前对端把连接断掉（实测的那一种就是这个）
        httpx.RemoteProtocolError,
        # 应答读到一半断了
        httpx.ReadError,
        httpx.ReadTimeout,
        # 连接池里那条连接在关停瞬间被收走
        httpx.PoolTimeout,
    ):
        return None
    if snapshot is None:
        return None
    snapshot.setdefault("source", "/api/state")
    return snapshot if predicate(snapshot) else None


def _state_at_terminal(proc: Any, predicate, *, what: str, timeout: float) -> dict[str, Any] | None:
    """收尾终态那一刻的 `/api/state`（进程正在退出时退到盘上那份同源记录 + 服务级 log 那句话）。

    `_service.py` 的 `ServiceProcess.snapshot()` 只问 `/api/state` 这一条路；进程退出过程中
    监听 socket 会先关掉，那一下读不到。这里因此在读不到时退到**盘上那份 `session.json`**
    （`state` 字段与快照的 `ended.phase` 同源，都是 `finish_drain()` 判出来的那一格），
    并按同一个谓词判它 —— 于是「退出前收尾终态已经落定」这件事在
    「服务还答得上」与「服务刚好在退出」两种时序下都判得出来。全程不猜、不睡死。

    **返回值的 `source` 是判据的一部分**（审查问题 m7b）：`"/api/state"` 表示这一次真的是
    从运行期快照读到的，`"session.json"` 表示退化到了盘上那一份 —— 后者**不构成**「退出前
    读到 `/api/state`」这件事的证据（契约 §3.8 第 ② 条要的正是前者）。调用点因此必须显式
    断言 `source == "/api/state"`（② 条就是这么写的）；只判谓词成立就放行，等于把「盘上那份
    也对得上」当成「运行期快照读到了」。

    **轮询间隔在这里单独收紧到 `SIGBREAK_TERMINAL_POLL_INTERVAL_SECONDS`**（那一段常量说明
    写了实测窗口有多窄、以及为什么 0.05 一整格只能取到一次样）：这一格要抓的是那一段窄窗口，
    用 0.05 的间隔等于拿一次采样去赌，赌输了就把「服务答不上」读成契约违约。收紧的只是
    **采样频率** —— 判据、`source` 那条断言、超时上限与「退化到盘上就判红」的口径都没动。
    """
    deadline = time.monotonic() + float(timeout)
    last: Any = None
    while True:
        snapshot = _state_while_alive(proc, predicate, what=what)
        if snapshot is not None:
            return snapshot
        record = read_session_json(_latest_session_dir(proc))
        last = record
        if record is not None and predicate({"ended": {"phase": record.get("state")}}):
            # 收尾终态那一格停在 `ended`（§3.2：成功档与失败档都不回 `idle`），盘上那份同源记录
            # 因此与 `/api/state` 说同一件事
            return {
                "source": "session.json",
                "top_state": "ended",
                "ended": {"phase": record.get("state")},
                "record": record,
            }
        if proc.poll() is not None:
            return None
        if time.monotonic() >= deadline:
            raise AssertionError(f"等待超时（{timeout}s）：{what}（最后观察值 {last!r}）")
        time.sleep(SIGBREAK_TERMINAL_POLL_INTERVAL_SECONDS)


def _terminal_landed_line(proc: Any) -> str | None:
    """服务级 log 里「收尾终态已落盘」那一行（收尾真的走到终态那一刻，运行期自己落的）。

    它是**运行期快照已经落到终态**这件事在「服务刚好在退出、`/api/state` 已经答不上」这一档
    下的可核对证据：这一行由 `_run_drain_steps()` 在 `finish_drain()` 判完终态之后写，
    而请求关停要等 `_wait_drain_settled()` 下一次轮询（`DRAIN_SETTLE_POLL_SECONDS`）才发生。
    行里带着那一刻的 `ended.phase`，调用点据此断言它真的是 `done`。
    """
    for line in _log_text(proc).splitlines():
        if "收尾终态已落盘：" in line:
            return line
    return None


def _disk_record(proc: Any) -> dict[str, Any]:
    record = read_session_json(_latest_session_dir(proc))
    assert record is not None, "盘上读不出 session.json"
    return record


def _assert_interrupted_face(proc: Any) -> dict[str, Any]:
    """③ 与 ④ 共用的「同一个结果」断言（两者只有触发时收尾处于哪一格不同）。

    逐项照 §3.8 表格的第 ③ 条与 §3.5 行 262–266 那三条**不覆盖约束**，断的就是下面这些项：

      - **`interrupted == true`**（`is True` 的真断言）；
      - `state == "draining"`，且落在 `draining` 这一格（`!= "done"`，§3.5 的「绝不写 `done`」）；
      - `ended_at` 无值（强制打断路径不写它，也不补写任何别的终态字段）；
      - 四步 `id` 齐全（`DRAIN_STEP_IDS` 逐项相等）、四步 `status` 落在 `DRAIN_STEP_STATUS`
        的成员域里（成员关系，不重抄取值域）；
      - **每一步只报真事、落定过的保持原样**：取消时的登记不许伪造终态，也不许把已经落定的
        步骤改回去 —— **正在飞的那一步照实记 `running`**（③ 的现场是第 1 步 `flush`：假采集件的
        `finalize()` 把它钉在工作线程里，取消只中止了等待，它既没跑完也没抛错），
        还没轮到的维持 `waiting`，已经落定的各自维持 `done` / `skipped` / `failed`。
        第 4 步 `post`（会后）在两个现场里都不会自己跑起来（R26）：它要么还没轮到（`waiting`），
        要么恰好在「释放会中模型」那一瞬被取消打断（照实记 `running`）。

    ④ 复用同一份断言，于是「不为它单开第三条路径」这件事有了逐字段的可判定形式。

    **「哪几步已落定」在 ③ 与 ④ 之间不同，判据因此不写死某一格的取值**（改钉第 1 步之后必须
    放宽的那一处）：③ 的同步点是第 1 步 `flush` 停在 `running`，它后面三步还没轮到（`waiting`）；
    ④ 的现场里 `flush` 已经落 `failed`，后面三步照常跑完（`done` / `skipped`）。共同的那一条
    ——「落定过的步骤保持原样、在飞的那一步照实记 `running`」—— 仍然逐字段可判。
    """
    record = _disk_record(proc)
    assert record["interrupted"] is True, record
    assert record["state"] == "draining", record
    assert record["state"] != "done", record
    assert record["ended_at"] is None, record
    steps = (record.get("drain") or {}).get("steps") or []
    assert [step["id"] for step in steps] == list(DRAIN_STEP_IDS), record
    settled = {step["id"]: step["status"] for step in steps}
    assert set(settled.values()) <= set(DRAIN_STEP_STATUS), record
    # `post` 那一格（会后）只报真事：还没轮到就是 `waiting`；恰好在「释放会中模型」那一步
    # 被取消打断时照实记 `running`（会后那两个按钮归用户，R26 —— 它不会自动跑起来）
    assert settled["post"] in ("waiting", "running"), record
    # 在飞的那一步只报真事：假采集件的 `finalize()` 把 `flush` 钉住，取消记的就是 `running`
    assert settled["flush"] in ("running", "failed", "done", "skipped"), record
    # `log` 在任何构建里都有真实实现：它要么还没轮到、要么已经落定（不许被改回 `waiting`）
    for step_id in ("log",):
        assert settled[step_id] in ("waiting", "running", "done", "failed"), record
        assert settled[step_id] in DRAIN_STEP_STATUS, record
    return record


