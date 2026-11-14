
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


#: 同步点的轮询间隔（秒）——「阻塞期间一定查得到 `running`」这条论证的余量更大。
SIGBREAK_STATE_POLL_INTERVAL_SECONDS = 0.05

#: 「退出前读到那份 `/api/state`」的轮询间隔（秒）——**只给收尾终态那一格用**。
#: **压的是采样频率、不是判据**。
SIGBREAK_TERMINAL_POLL_INTERVAL_SECONDS = 0.005

#: 轮询 `/api/state` 的超时（秒）
SIGBREAK_STATE_POLL_TIMEOUT_SECONDS = 20

#: 假采集件 `finalize()`（收尾第 1 步 `flush`）在工作线程里的阻塞秒数：
#: **大于轮询间隔、小于骨架期的总上限**。
#: 它经 `fakes.write_knob_plugin(capture_delay_seconds=...)` 落进假件的环境变量，
#: 子进程里的 `FakeCaptureBackend.finalize()` 从那里读到它。
SHUTDOWN_BLOCKING_STEP_SECONDS = 2.0

#: 「第二次 `Ctrl+C` → 进程退出」的时延上限（秒）。
#: 第二次按下是「**立即中止**」：`cancel()` 中止的是收尾 task 的等待，已经交给
#: 工作线程的那次同步调用（假采集件 `finalize()` 的 `time.sleep`）**照跑到底**，
#: 本上限就是这条行为的判据。
INTERRUPT_EXIT_LATENCY_LIMIT_SECONDS = 1.0

#: 包装脚本等「父进程说可以按了」的最长等待（秒）：到点还没等到就直接按 ——
#: 这一支只在小概率的父进程侧异常里出现，**保证信号一定送得出去**（不留无上限挂死）
SELF_SIGNAL_MAX_WAIT_SECONDS = 5.0

#: 「可以按了」标记文件的落点：本用例临时家目录的**兄弟目录**
#: （子进程把 `HOME` 重定向到了临时家目录，用它的 `..` 就不必再传一个绝对路径）
READY_MARKER_DIR_NAME = "self-signal-ready"

#: 那两个子进程的收场超时（秒）
PROCESS_EXIT_TIMEOUT_SECONDS = 30

#: 单条用例的墙钟上限（秒）。
E2E_CASE_WALL_CLOCK_LIMIT_SECONDS = 5.0

#: 服务级 log 里两行必断的字面
SELF_CHECK_LINE = "信号接管自检通过：SIGINT 与 SIGBREAK 指向本服务的处理函数"
SIGBREAK_LINE = "收到 SIGBREAK，进入关停"
SIGINT_LINE = "收到 SIGINT，进入关停"

#: 强制打断那一行的文案（`meeting/service/app.py` 的 `DRAIN_INTERRUPTED_MESSAGE`）
INTERRUPTED_LINE = service_app.DRAIN_INTERRUPTED_MESSAGE


@pytest.fixture()
def case_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """本用例的家目录重定向。

    子进程那一侧由 `_spawn()` 给全一整份环境变量；这一份是本进程的（插件工厂写配置时要用
    `HOME` / `USERPROFILE` 解析 `~/.config/meeting/config.json`）。
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


#: 包装脚本：**原样跑真实的 `meeting.cli.main(["web"])`**，
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

    `self_signal=True` 时改起**包装脚本**（`python -c`，见 `_SELF_SIGNAL_WRAPPER`）：它仍然跑
    真实的 `meeting.cli.main(["web"])`，只是等用例写下「可以按了」的标记之后由子进程自己给自己
    发一次 `SIGINT`（把 `Ctrl+C` 那一路真的走一遍：本机的控制台事件送不到子进程）。
    标记的落点由 `self_signal_ready_marker(home)` 给出，用例在 `start`
    之后写它 —— 于是这一按落在「录音中」，与 `CTRL_BREAK_EVENT` 那一路同一格。

    环境变量那一份由 `_spawn_env()` 单独给出（另一条子进程路径复用同一份构造，
    不会两边各长一套）。
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
    """一条子进程路径的环境变量与假件布置（与 `_spawn()` 共用同一份构造）。

    **`MEETING_CAPTURE` 不写进子进程 env，这一步是必要的、不是漂移**：
    采集后端一律由本用例 `config.json` 里的 **`capture.backend`** 说了算 ——
    环境变量的优先级高于配置文件，留着 `MEETING_CAPTURE` 就会把这条配置键**静默压住**
    （假件挂不上、采到的东西与断言对不上，用例却照常绿）。这一项因此写成配置键
    `capture.backend`（缺省 `"dev"`：采集替身，`start` 照常能开起来）。
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
    合计，这里量用例正文这一段）。
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
    return {
        step["id"]: step["status"]
        for step in (snapshot.get("ended") or {}).get("steps") or []
    }


def _state_while_alive(proc: Any, predicate, *, what: str) -> dict[str, Any] | None:
    """**进程还活着**的时候读一次 `/api/state`（读到就让位、进程已收场就返回 `None`）。

    与 `_wait_state()` 的分工：那条等到条件成立为止、到超时抛错；这一条只把「现在这一刻读到
    的那份快照」带回来，因此可以放在「等进程退出」之后 —— 进程还没收场时它是真读数，进程已经
    收场时它返回 `None`，两种情形都由调用点如实记下来。

    **「返回 `None`」的两种来路分开认**：子进程已经收场（`poll()` 非空）或这次读撞上
    「服务正在退出、连接被断」—— 这两种是真时序，返回 `None`；其余任何异常（例如读到
    一份**非 200 的应答**、或 JSON 解析炸了）一律**原样抛出去**，不再被这里吞成 `None`
    —— 吞掉的话「服务答了个 500」会被读成「刚好在退出」，用例会以「以为在读运行期快照、
    其实在读盘」的形式静默退化。
    """
    if proc.poll() is not None:
        return None
    try:
        snapshot = proc.snapshot()
    except (
        # 连接都建不起来：服务正在退出、监听 socket 已关
        httpx.ConnectError,
        httpx.ConnectTimeout,
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

    **返回值的 `source` 是判据的一部分**：`"/api/state"` 表示这一次真的是
    从运行期快照读到的，`"session.json"` 表示退化到了盘上那一份 —— 后者**不构成**「退出前
    读到 `/api/state`」这件事的证据。调用点因此必须显式
    断言 `source == "/api/state"`；只判谓词成立就放行，等于把「盘上那份
    也对得上」当成「运行期快照读到了」。
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
            # 收尾终态那一格停在 `ended`，盘上那份同源记录因此与 `/api/state` 说同一件事
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
    """两条强制打断用例共用的「同一个结果」断言（两者只有触发时收尾处于哪一格不同）。

      - **`interrupted == true`**（`is True` 的真断言）；
      - `state == "draining"`，且落在 `draining` 这一格（`!= "done"`，绝不写 `done`）；
      - `ended_at` 无值（强制打断路径不写它，也不补写任何别的终态字段）；
      - 四步 `id` 齐全（`DRAIN_STEP_IDS` 逐项相等）、四步 `status` 落在 `DRAIN_STEP_STATUS`
        的成员域里（成员关系，不重抄取值域）；
      - **每一步只报真事、落定过的保持原样**：取消时的登记不许伪造终态，也不许把已经落定的
        步骤改回去 —— **正在飞的那一步照实记 `running`**（现场是第 1 步 `flush`：假采集件的
        `finalize()` 把它钉在工作线程里，取消只中止了等待，它既没跑完也没抛错），
        还没轮到的维持 `waiting`，已经落定的各自维持 `done` / `skipped` / `failed`。

    两个触发用例复用同一份断言，「不为它单开第三条路径」因此有了逐字段的可判定形式。

    **「哪几步已落定」在两个现场之间不同，判据因此不写死某一格的取值**：
    一个现场是第 1 步 `flush` 停在 `running`，它后面三步还没轮到（`waiting`）；
    另一个现场里 `flush` 已经落 `failed`，后面三步照常跑完（`done` / `skipped`）。
    共同的那一条 ——「落定过的步骤保持原样、在飞的那一步照实记 `running`」—— 仍然逐字段可判。
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
    # 被取消打断时照实记 `running`（会后那两个按钮归用户，它不会自动跑起来）
    assert settled["post"] in ("waiting", "running"), record
    # 在飞的那一步只报真事：假采集件的 `finalize()` 把 `flush` 钉住，取消记的就是 `running`
    assert settled["flush"] in ("running", "failed", "done", "skipped"), record
    # `log` 在任何构建里都有真实实现：它要么还没轮到、要么已经落定（不许被改回 `waiting`）
    for step_id in ("log",):
        assert settled[step_id] in ("waiting", "running", "done", "failed"), record
        assert settled[step_id] in DRAIN_STEP_STATUS, record
    return record


