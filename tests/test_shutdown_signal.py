"""`Ctrl+C` 两阶段语义的进程级用例（phase3 §5.1 / §3.8；契约点名的六个函数逐字在位）。

**用例计数（照实登记，数目可核对）**

  - **契约点名的六个函数**（§4 落点表那行逐条列出）：`test_idle_sigbreak_exits_zero` /
    `test_recording_sigbreak_drains_and_exits_zero` / `test_second_sigbreak_during_drain_exits_130` /
    `test_sigbreak_while_already_draining_exits_130` / `test_failed_drain_exits_one_and_prints_log_path` /
    `test_sigint_and_sigbreak_share_one_handler` —— 六个一个不少、各自独立；
  - **审查修复追加的 3 个函数**（名字与上面六个不相撞、也不顶替其中任何一条；出处写在各自的
    docstring 里）：`test_restart_notice_first_sigbreak_exits_zero_without_rewriting_disk`
    （C1 / M7：重启提示态下第一次按下走空闲退出档、盘上那一版一个字不改写）、
    `test_clean_drain_then_sigbreak_exits_zero_without_rewriting_disk`（**C1 的另一格**：
    `stop` 已收尾干净走完之后按下 → 同样走空闲退出档、盘上逐字节不变、重启后不再挂
    `restart_unfinished`）、`test_web_startup_failure_exits_failure_and_says_so`
    （M11：`server.run()` 之外那一路的意外同样以 `EXIT_FAILURE` 说话）、
    `test_drain_exit_code_matrix_including_the_server_crash_fallback`（M4：退出码逐档写死，
    含「收尾干净 + `server.run()` 抛异常」这个组合；它不起服务、直接调 `meeting/cli.py` 的
    `_drain_exit_code()`，因此不占墙钟）；
  - **第六轮审查再追加的 2 个函数**（同样不与上面任何一条相撞）：原文那一句「追加的 3 个函数」
    的实际条数是 4（上面已逐条列全），这里只登记本轮新增的两条 ——
    `test_second_press_in_the_same_process_carries_force_true`（**C1**：同一个真服务子进程
    连按两次信号，第二次带 `force=True`，落进 log 的是「本次是第 2 次按下」）、
    `test_drain_report_speaks_for_the_real_face_not_the_exit_code`（**M1**：终端那套「收尾有
    步骤失败」的文案只在现场真有 `failed` 步时出现；进程内纯函数级、不占墙钟）。
    **合计 6 + 4 + 2 = 12 个测试函数**（本文件里没有参数化，pytest 收集到 12 条）。
  - **第七轮审查再追加的 2 个函数**（同样不与上面任何一条相撞、各自独立）：
    `test_degraded_state_read_still_raises_when_the_service_answers_badly`
    （**m7b**：`_state_while_alive()` 只把「进程已收场」与「连接被断」这两种真时序读成
    `None`，服务答了个非 200 时照样抛出去 —— 于是「退出前读到的是运行期快照」这件事有活的
    判据，不会被一句 `except Exception: return None` 静默吞掉；进程内纯函数级、不占墙钟）、
    `test_interrupted_report_speaks_for_the_forced_interrupt_not_the_failed_face`
    （**M11**：强制打断档 + 现场前面有 `failed` 步这一格的终端文案；进程内纯函数级、不占墙钟）。
    **本文件当前实测收集数**：`python -m pytest tests/test_shutdown_signal.py --collect-only -q`
    = **14 条**（契约点名的六个一个不少、各自独立；其余为历次审查修复追加的条目，均不带参数化）。

**本文件补上的那一格（审查问题 C1 / M7）**：`top_state == "idle"` 且启动扫描命中了未收尾场次
（挂了 `notice.restart_unfinished`）时，**第一次** `Ctrl+C` 落在 §3.5 行为表的**第二行**
（`idle` → 直接退出 → 空闲退出档），盘上那一版由启动扫描写下、此后**一个字都不再改写**。
判据是「当场有没有收尾在跑 / 有没有失败现场」，盘上留没留未收尾场次只拦新场次的 `start`
（§3.6）。这一格由 `test_restart_notice_first_sigbreak_exits_zero_without_rewriting_disk`
盯住：它重起一个真服务、造出重启提示态、只发一次信号，断言退出码是 `EXIT_OK` 且
`session.json` 逐字节未变。

**C1 的第二格（同一处判据的另一面）**：`stop` 已经把四步全部收尾干净（`ended.phase == "done"`、
盘上 `state="done"` + `ended_at`）之后按一次 `Ctrl+C`，同样落在「当场没有收尾要管」这一格 ——
退出码 `EXIT_OK`、`session.json` **逐字节未变**（不写 `interrupted`、不删 `ended_at`）、
盘上那份场次不会被重启扫描认领（`start` 因此能直接开新场）。这一格由
`test_clean_drain_then_sigbreak_exits_zero_without_rewriting_disk` 钉住（进程级）。

**顶部注释（契约要求写在这里）**：`SIGINT` 与 `SIGBREAK` **共用同一个处理函数**
（`meeting/service/app.py` 的 `handle_shutdown_signal`，安装器 `install_shutdown_signal_handlers()`
在服务器运行入口之前把它挂到两个信号上）—— 两个信号走的是**同一条代码路径**，
因此下面每一条用例都可用 `CTRL_BREAK_EVENT`（Windows 的子进程只能这样发信号）触发，
第 ⑥ 条再用 `CTRL_C_EVENT` 复证两者结果一致。

**真服务子进程**：起停、发中断信号、读退出码、取 `base_url` 四项能力都在
`tests/_service.py` 的 `ServiceProcess` 上（phase2 的夹具能力），**本文件不另起一套子进程管理**。
子进程的环境变量与 `service_process` 夹具那一支**同一套**：家目录重定向（`HOME` +
`USERPROFILE`）、`MEETING_PORT` / `MEETING_OUT_ROOT` / `MEETING_LOG_DIR` / `MEETING_NO_BROWSER`
四项、`PYTHONPATH`；假件经临时目录 + `PYTHONPATH` + 既有配置键 `post.hook` 挂上（**本文件只
import `tests/_drain_fakes.py` 里的假件，不自行定义、不各写一份**）。**采集后端一律由本用例
`config.json` 的 `capture.backend` 表达，子进程 env 里不写 `MEETING_CAPTURE`** —— 这正是
`tests/test_drain.py` 那一支的同一口径（两条子进程路径的采集后端因此同源、也看得见），
理由见 `_spawn()` 的 docstring：环境变量会静默压住那条配置键。

**③ 与 ④ 的确定同步点（不许靠抢时间窗口）**：骨架构建下四步几乎瞬间跑完，靠抢时间窗口去
「收尾中途再按一次」会 flaky。本文件用可注入的假会后钩子把同步点钉死，两条各用一个子进程、
各自一份临时 home 与临时假钩子目录：

  - ③ 用 **`BlockingThreadPostHook`**（`launch()` 里睡 `SHUTDOWN_BLOCKING_HOOK_SECONDS`：
    它经运行时的**收尾专用 daemon 执行器**跑在工作线程里，事件循环空着，`/api/state`
    在整个阻塞期间照常应答）→ `POST stop` 之后轮询 `/api/state` 直到 `post` 那一步
    `status == "running"`，再发 `CTRL_BREAK_EVENT`；
  - ④ 用 **`FailingPostHook`**（`launch()` 抛错）先把收尾失败现场落定 → 轮询到
    `ended.phase == "draining"` 且 `post` 那一步 `status == "failed"`，再发一次
    `CTRL_BREAK_EVENT`。

**⑥ 里 `SIGINT` 那一路怎么真的送到（写清手法与实测结论）**：本机（Windows 11 + Python 3.13.7）
实测 `GenerateConsoleCtrlEvent(CTRL_C_EVENT, ...)` 与 `os.kill(pid, CTRL_C_EVENT)` 都**送不到**
子进程（两条都返回成功、子进程一行 log 都不落），`CTRL_BREAK_EVENT` 则照常送达 —— 所以
「①`SIGBREAK` 按一次、②`SIGINT` 按一次，比对两边的结果字段与退出码」这件事没法靠控制台事件
收口。本文件改用**子进程自己给自己发**：包装脚本用 `python -c` 起（**包装脚本只存在于命令行
里，仓库里与仓库外都不落任何探针文件**），仍然跑真实的 `meeting.cli.main(["web"])`，另起一条
线程等 `SELF_SIGNAL_DELAY_SECONDS` 后调 `signal.raise_signal(signal.SIGINT)` —— 收到的是服务
自己装的 `handle_shutdown_signal`，走的是 `Ctrl+C` 那条真实路径（不是模拟、不是进程内调用），
两路于是可以逐字段比对。`raise_signal` 由主线程执行（CPython 的信号处理只认主线程），
所以它排在 `main()` 跑起来之后。
"""

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
from tests._service import post_action, read_session_json, wait_until

#: 慢档标记：`pytest.mark.slow` 的归位判据与实测数据见 `docs/service.md` 第 7 章。
pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# 本文件的具名常量（§3.8 末「测试侧常量」那张表；用例里不留裸秒数）
#
# **取值收口的授权说明（第七轮审查 m3，措辞在下游文件里一字不改地重复）**：
# 「以上取值由指挥者授权收口（原契约表的值见 phase3.md §3.8 末表；红线：单条用例 ≤5s）」
# —— 本文件里被这一条覆盖的常量是 `SHUTDOWN_BLOCKING_HOOK_SECONDS`、
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

#: ③ 条里假钩子在工作线程里的阻塞秒数：**大于轮询间隔、小于骨架期的总上限 30 秒**
#: （§3.8 末「测试侧常量」那张表把这一档命名为 `SHUTDOWN_BLOCKING_HOOK_SECONDS`）。
#:
#: **用户把「单条用例 ≤ 5 秒」定为硬要求，取值因此贴着下界走**（契约表写 3.0 秒，这里取 2.0
#: 是**这一条授权下的缩短**）：2.0s / 0.05s = 40 次机会，「轮询一定能在阻塞结束前查到
#: `running`」这条论证余量充足，用例的墙钟也留得住余量。
#:
#: **它与下面那条退出时延上限的关系**（审查问题 M10 改掉的说法）：这两笔钱**互不相干** ——
#: 收尾四步的同步调用由**收尾专用的 daemon 执行器**推进工作线程，进程退出时**不等**在飞的
#: 那一步，因此「第二次按下 → 进程退出」的时延**与剩余阻塞时长无关**（阻塞 2.0 秒的现场实测
#: 0.172 秒；同一个现场切回非 daemon 的默认执行器才是等满的 2.007 秒）。下面那条 1.0 秒的
#: 上限，咬的是「**退出不等在飞步**」这件事 —— 它量的是关停这一路自己的时延，
#: 与在飞那一步还剩多久无关。
#:
#: **这个常量真的被用上了**（修审查问题 M2）：它经 `fakes.write_blocking_post_hook(
#: block_seconds=...)` 落进假件的环境变量，子进程里的 `BlockingThreadPostHook` 从那里读到它，
#: 于是「阻塞 `SHUTDOWN_BLOCKING_HOOK_SECONDS`」这句话与实测一致；② 与 ③ 两条用例的墙钟
#: 也随之明显下降（② 从 3.56 秒降到 2.51 秒，③ 从 3.28 秒降到 0.44 秒）。
SHUTDOWN_BLOCKING_HOOK_SECONDS = 2.0

#: ③ 条「第二次 `Ctrl+C` → 进程退出」的时延上限（秒）。
#:
#: 契约（§3.5 行为表第二行）要求第二次按下是「**立即中止**」：`cancel()` 中止的是收尾
#: task 的等待，已经交给工作线程的那次同步调用（这里就是假钩子的 `time.sleep`）**照跑到底**
#: —— 若解释器收尾要 join 它，「立即中止」就被在飞的同步步拖着走（**修 M5 之前实测：
#: 第二次按下 → 进程退出要等满那次阻塞**）。收尾四步的工作线程因此是 daemon
#: （`meeting/service/runtime.py` 的 `_DaemonThreadPoolExecutor`），本上限就是这条行为的判据。
#:
#: **这条上限盯的是什么（审查问题 M10 改掉的说法）**：daemon 执行器下，进程退出的时延与
#: **剩余阻塞时长无关**（那一步留在工作线程里自己跑完，解释器不 join 它），因此这里比的是
#: 「退出**没等**在飞步」这一件事，而不是「离阻塞结束还剩多少秒」。1.0 秒相对一次正常的
#: 关停（几十毫秒）足够宽，而**任何一条把在飞步等满的路径都远远越过它**。
#:
#: **取值论证**：同一个现场（在飞的那一步阻塞 2.0 秒）上量两次 —— 收尾四步走 daemon 执行器时
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

#: 服务级 log 里两行必断的字面（phase1 冻结，一处都不许改字面）
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

    环境变量那一份由 `_spawn_env()` 单独给出：①b 条要在**同一个输出根**上重起第二个服务，
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
    out_root = home.parent / "out"
    env: dict[str, str] = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "MEETING_NO_BROWSER": "1",
        "MEETING_PORT": "0",
        "MEETING_OUT_ROOT": str(out_root),
        "MEETING_LOG_DIR": str(helpers.log_dir_of(home)),
        "PYTHONPATH": (
            str(helpers.REPO_ROOT)
            if plugin is None
            else plugin.pythonpath(helpers.REPO_ROOT)
        ),
    }
    out_root.mkdir(parents=True, exist_ok=True)
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


def _start(proc: Any) -> None:
    started = post_action(proc.base_url, "start")
    assert started.status_code == 200, started.text


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
    """收尾终态那一刻的 `/api/state`（进程正在退出时退到盘上那份同源记录）。

    `_service.py` 的 `ServiceProcess.snapshot()` 只问 `/api/state` 这一条路；进程退出过程中
    监听 socket 会先关掉，那一下读不到。这里因此在读不到时退到**盘上那份 `session.json`**
    （`state` 字段与快照的 `ended.phase` 同源，都是 `finish_drain()` 判出来的那一格），
    并按同一个谓词判它 —— 于是「退出前读到的是 `ended.phase == "done"`」这件事在
    「服务还答得上」与「服务刚好在退出」两种时序下都判得出来。全程不猜、不睡死。

    **返回值的 `source` 是判据的一部分**（审查问题 m7b）：`"/api/state"` 表示这一次真的是
    从运行期快照读到的，`"session.json"` 表示退化到了盘上那一份 —— 后者**不构成**「退出前
    读到 `/api/state`」这件事的证据（契约 §3.8 第 ② 条要的正是前者）。调用点因此必须显式
    断言 `source == "/api/state"`（② 条就是这么写的）；只判谓词成立就放行，等于把「盘上那份
    也对得上」当成「运行期快照读到了」。

    **轮询间隔在这里单独收紧到 `SIGBREAK_TERMINAL_POLL_INTERVAL_SECONDS`**（那一段常量说明
    写了实测窗口有多窄、以及为什么 0.05 一整格只能取到一次样）：这一格要抓的是几十毫秒宽的
    窗口，用 0.05 的间隔等于拿一次采样去赌，赌输了就把「服务答不上」读成契约违约。收紧的只是
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
      - **已落定步骤保持原样**：`flush` / `transcode` / `log` 三步在 ③④ 两个现场里都已落定
        （骨架构建下 `flush` / `transcode` 记 `skipped`、`log` 记 `done`），打断之后它们仍是
        各自的终态 —— 取消时的记账不许把它们改回去；
      - **未跑完的那一步不伪造终态**：`post` 在 ③ 的现场里停在 `running`（假钩子把它钉住，
        那一趟收尾确实还在跑），在 ④ 的现场里已经是 `failed`（失败现场本来就是这样）——
        两档都只报真事，`waiting`（还没开跑）同样算如实。

    ④ 复用同一份断言，于是「不为它单开第三条路径」这件事有了逐字段的可判定形式。
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
    # 已落定步骤保持原样（`log` 在任何构建里都有实现，前三步在这一档都已是终态）
    assert settled["log"] == "done", record
    for step_id in ("flush", "transcode"):
        assert settled[step_id] in ("done", "skipped"), record
    # 未跑完的那一步只报真事：③ 里它正被假钩子钉在 running（或还没开跑），④ 里它是那个失败现场
    assert settled["post"] in ("running", "waiting", "failed"), record
    return record


# ---------------------------------------------------------------------------
# 读 `/api/state` 这一层的退化边界（**进程内纯函数级**，审查问题 m7b）
#
# `_state_while_alive()` 是「退出前读到运行期快照」这条判据的读入口。它原先写的是
# `except Exception: return None`：服务答了个非 200、或应答体不是 JSON 时，那一下会被读成
# 「进程刚好在退出」，用例于是静默退化到盘上那一份（`_state_at_terminal()` 的第二条路），
# 而「退出前真的读到过 `/api/state`」这件事一次都没有被验到。这一条把那道边界钉住。
# ---------------------------------------------------------------------------


class _StubProcess:
    """只回放 `_state_while_alive()` 会用到的两处读法（`poll()` 与 `snapshot()`）。"""

    def __init__(self, *, returncode: int | None, error: Exception | None = None) -> None:
        self._returncode = returncode
        self._error = error

    def poll(self) -> int | None:
        return self._returncode

    def snapshot(self) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return {"top_state": "ended", "ended": {"phase": "done"}}


def test_degraded_state_read_tolerates_only_teardown_and_still_raises_on_bad_answers():
    """**传输层没拿到应答**整类让位；服务答得不好时照样抛出去（审查问题 m7b 的那道边界）。

    四格逐格写死：

      1. **子进程已经收场**（`poll()` 非空）→ 返回 `None`（这一路是真的没有运行期可读，
         `_state_at_terminal()` 随后退到盘上那份）；
      2. **传输层没拿到应答** → 返回 `None`。这一格此前只接 `ConnectError`，实测漏掉了
         `RemoteProtocolError`（服务退出时对端在应答之前把连接断掉，MRO 走
         `ProtocolError → TransportError`，不在 `ConnectError` 那一支下），于是那一次偶发
         表现为**异常冒出去、把用例判红**，而它本该落进「这一次读不算数」。现在这一整类都
         收下：`ConnectError` / `ConnectTimeout` / `RemoteProtocolError` / `ReadError` /
         `ReadTimeout` / `PoolTimeout` 六种逐条断言，一条都不许再漏；
      3. **服务答了个非 200**（`ServiceProcess.snapshot()` 自己就断言 200，不合格的应答在那一层
         变成 `httpx.HTTPStatusError`）→ **原样抛出去**。这一格是「服务答得不好」与「连接被断」
         的分水岭：吞掉它，用例就会把「服务答了个 500」读成「刚好在退出」，静默退化到盘上那份；
      4. **其余任何异常**（这里用 `RuntimeError` 代表「应答体不是 JSON / 形状不对」那一类）
         → 同样**原样抛出去**。

    正面那一格照旧：进程还活着、应答正常 → 带上 `source` 标记交出去。
    """
    def predicate(snap: dict[str, Any]) -> bool:
        return (snap.get("ended") or {}).get("phase") == "done"

    # ① 子进程已收场
    assert (
        _state_while_alive(_StubProcess(returncode=0), predicate, what="已收场") is None
    )

    # ② 传输层没拿到应答：整类让位（六种逐条钉住，别只钉实测撞上的那一种）
    for name, error in (
        ("连接都建不起来", httpx.ConnectError("连接被断")),
        ("连上了但对端不发应答", httpx.ConnectTimeout("连接超时")),
        ("应答之前对端断了连接", httpx.RemoteProtocolError("Server disconnected")),
        ("应答读到一半断了", httpx.ReadError("读到一半断了")),
        ("应答读取超时", httpx.ReadTimeout("读取超时")),
        ("连接池在关停瞬间收走连接", httpx.PoolTimeout("池超时")),
    ):
        assert (
            _state_while_alive(
                _StubProcess(returncode=None, error=error), predicate, what=name
            )
            is None
        ), name

    # ③ 服务答了个非 200：异常原样冒出去（不许被吞成 None）
    with pytest.raises(httpx.HTTPStatusError):
        _state_while_alive(
            _StubProcess(
                returncode=None,
                error=httpx.HTTPStatusError(
                    "GET /api/state 该回 200，实测 500",
                    request=httpx.Request("GET", "http://127.0.0.1/api/state"),
                    response=httpx.Response(500),
                ),
            ),
            predicate,
            what="服务答了个 500",
        )

    # ④ 其余任何异常（应答体不是 JSON / 形状不对那一类）：同样原样冒出去
    with pytest.raises(RuntimeError):
        _state_while_alive(
            _StubProcess(returncode=None, error=RuntimeError("读到一份形状不对的应答")),
            predicate,
            what="服务答得不好",
        )

    # 正面那一格：进程还活着、应答正常 → 带上 `source` 标记交出去
    alive = _state_while_alive(_StubProcess(returncode=None), predicate, what="正常应答")
    assert alive is not None and alive["source"] == "/api/state", alive
    print(
        "[m7b 证据] 已收场与「传输层没拿到应答」六种都返回 None；"
        "非 200 那一格抛 HTTPStatusError、形状不对那一格抛 RuntimeError；"
        f"正常应答那一格交出的快照 source={alive['source']!r}"
    )


# ---------------------------------------------------------------------------
# ① 空闲态按一次 → 空闲退出档
# ---------------------------------------------------------------------------


def test_idle_sigbreak_exits_zero(service_process, case_env):
    """空闲态按一次 `CTRL_BREAK_EVENT` → 空闲退出档退出码。

    这一条与 phase1 在 `tests/test_port_policy.py` 里的那条空闲态 `Ctrl+C` 断言**同源**：
    同一个退出码常量（`EXIT_OK`）、同一套子进程与信号辅助。
    """
    case_started = time.monotonic()
    proc = _spawn(service_process, case_env)
    proc.wait_ready()
    assert proc.poll() is None

    proc.send_interrupt()
    code = proc.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
    assert code == EXIT_OK

    log_text = _log_text(proc)
    assert SIGBREAK_LINE in log_text
    assert SELF_CHECK_LINE in log_text
    assert "退出原因与退出码：" in log_text
    # 空闲态没有场次可收尾：输出根下不该凭空多出目录
    assert _session_dirs(proc) == []
    _finish_case(case_started, "① 空闲态按一次")


# ---------------------------------------------------------------------------
# ①b 重启提示态（`idle` + `restart_unfinished`）第一次按 → 空闲退出档，盘上那一版一个字不改写
#     —— 审查问题 C1 / M7
# ---------------------------------------------------------------------------


def test_restart_notice_first_sigbreak_exits_zero_without_rewriting_disk(
    service_process, case_env
):
    """重启提示态下**第一次** `Ctrl+C` → 空闲退出档，且盘上那一版逐字节未变（C1）。

    现场：起一个真服务、开一场、硬杀（模拟断电）→ **在同一输出根**上重起一个 → 新实例的启动
    扫描把那一场改写成 `state == "draining"` + `interrupted: true` 并在空闲态挂上
    `notice.restart_unfinished`（§3.6）。这时**只发一次** `CTRL_BREAK_EVENT`。

    判据是「当场有没有收尾在跑 / 有没有失败现场」：这一刻 `_drain_task is None`、手上没有任何
    要收的尾，因此落在 §3.5 行为表的**第二行**（`idle` → 直接退出 → 空闲退出档）。修 C1 之前
    这里会把盘上那份未收尾场次当成「已停在 `ended·draining` 再按」那一档，落成强制打断档
    （退出码 130）并往盘上补写 `interrupted` / 抹掉 `ended_at`。

    「盘上未被改写」的读法：**发信号前**先把 `session.json` 逐字节读下来（启动扫描那一版），
    进程退出之后再读一次，两次必须逐字节相同 —— 启动扫描写的那一版与这次关停的落盘是两件事，
    这一条同时咬住「不写 `interrupted`」「不写 `ended_at`」「不动 `steps`」三件事。
    """
    case_started = time.monotonic()
    proc = _spawn(service_process, case_env)
    proc.wait_ready()
    _start(proc)
    session_dir = _latest_session_dir(proc)
    assert read_session_json(session_dir)["state"] == "recording", "被杀之前的现场是「录音中」"

    proc.kill()  # 被杀 / 断电：不发信号、不等收尾

    # **同一输出根**上重起第二个服务：`_spawn_env()` 按 `<家目录>/../out` 算输出根，
    # 因此把第二个家目录取成第一个的兄弟目录，两趟指向的输出根就是同一个。
    restarted_home = case_env.parent / "home-restart-notice"
    restarted = service_process(
        home=restarted_home, capture=None, env=_spawn_env(restarted_home)
    )
    try:
        restarted.wait_ready()
        snapshot = restarted.snapshot()
        # 现场就位（否则下面那条退出码断言验的并不是这一格）
        assert snapshot["top_state"] == "idle", snapshot
        assert (snapshot["notice"] or {}).get("kind") == "restart_unfinished", snapshot
        assert (snapshot["notice"] or {}).get("session_dir") == str(session_dir), snapshot
        assert snapshot["session"] is None, "重启后不自动续录"

        session_json = session_dir / "session.json"
        before = session_json.read_bytes()
        assert read_session_json(session_dir)["interrupted"] is True, "启动扫描那一版已落稳"

        # **只发这一次**信号
        restarted.send_interrupt()
        code = restarted.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)

        assert code == EXIT_OK, f"重启提示态下第一次 Ctrl+C 该走空闲退出档：实测 {code}"
        after = session_json.read_bytes()
        assert after == before, (
            "这一次关停不该改写盘上那份 session.json：\n"
            f"发信号前 {before.decode('utf-8', 'replace')}\n"
            f"退出之后 {after.decode('utf-8', 'replace')}"
        )
        record = read_session_json(session_dir)
        assert record["state"] == "draining", record
        assert record["interrupted"] is True, record
        assert record["ended_at"] is None, record
        assert "强制打断" not in _log_text(restarted), "这一格不该落强制打断那一行"
        assert SIGBREAK_LINE in _log_text(restarted)
        assert "退出原因与退出码：" in _log_text(restarted)
        assert "EXIT_OK" in _log_text(restarted)
        print(
            f"[C1 证据] 重启提示态下第一次 Ctrl+C -> 退出码 {code}（EXIT_OK={EXIT_OK}）；"
            f"盘上 session.json 逐字节未改写；"
            f"notice.kind={(snapshot['notice'] or {}).get('kind')!r}；"
            f"record.state={record['state']!r} / interrupted={record['interrupted']!r}"
            f" / ended_at={record['ended_at']!r}"
        )
    finally:
        restarted.terminate()
    _finish_case(case_started, "①b 重启提示态第一次按")


# ---------------------------------------------------------------------------
# ①c 收尾干净走完之后按一次 → 空闲退出档，盘上那一版逐字节不改写（**C1 的第二格**）
# ---------------------------------------------------------------------------


def test_clean_drain_then_sigbreak_exits_zero_without_rewriting_disk(
    service_process, case_env
):
    """`stop` 已收尾干净（`ended·done`）之后按一次 `Ctrl+C` → 空闲退出档，盘上逐字节未变（C1）。

    现场：起一个真服务、开一场、`POST stop`、**轮询到收尾走完**（`ended.phase == "done"`，
    此刻盘上是 `state="done"` + `ended_at` 有值 + `interrupted == false`）—— 这是「正常 stop
    之后关服务」这条最自然的操作。这时**只发一次** `CTRL_BREAK_EVENT`。

    判据（§3.5 行为表：第一次按下的「直接退出」档要按「**此时此刻有没有收尾要管**」读）：
    四步已全部落定且**无 `failed`**，收尾终态已经由 `finish_drain()` 那一趟落稳，
    当场没有任何要收的尾 —— 因此落在**空闲退出档**，且**盘上一个字节都不改写**。

    修 C1 之前这一格被 `_drain_failed_and_settled()` 的漏判吞进强制打断档：盘上那一版被无条件
    改写成 `state="draining"` + `interrupted=true` + `ended_at=None`、退出码是
    `EXIT_INTERRUPTED`，紧接着同一输出根上的下一次 `start` 会撞上 `draining`(409)。

    三件事一起断（缺一件这一格就还是坏的）：

      1. **退出码** `EXIT_OK`（空闲退出档），且 log 里查得到「退出原因与退出码：… EXIT_OK」，
         同时**没有**强制打断那一行；
      2. **盘上逐字节未变**：发信号前读一次 `session.json` 的原始字节，进程退出之后再读一次，
         两次必须**逐字节相同**（这一条同时咬住「不写 `interrupted`」「不删 `ended_at`」
         「不动 `steps`」三件事）；
      3. **下一场能开起来**：在**同一输出根**上重起一个服务 —— 不再挂
         `notice.restart_unfinished`（未收尾判据是 `state ∈ {recording, draining}`
         **或** `interrupted: true`，这一版三样都不占），`POST start` 放行（200）。
    """
    case_started = time.monotonic()
    proc = _spawn(service_process, case_env)
    proc.wait_ready()
    _start(proc)
    session_dir = _latest_session_dir(proc)

    assert post_action(proc.base_url, "stop").status_code == 200
    # **等收尾真的走完**（不是靠抢时间窗口）：`ended.phase` 落到 `done`、盘上那一版也落稳
    _wait_state(
        proc,
        lambda snap: (snap.get("ended") or {}).get("phase") == "done",
        what="收尾走完（ended.phase == done）",
    )
    record_before = _disk_record(proc)
    assert record_before["state"] == "done", record_before
    assert record_before["ended_at"], record_before
    assert record_before["interrupted"] is False, record_before

    session_json = session_dir / "session.json"
    before = session_json.read_bytes()

    # **只发一次**信号
    proc.send_interrupt()
    code = proc.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)

    log_text = _log_text(proc)
    assert code == EXIT_OK, (
        f"收尾干净走完之后按一次 Ctrl+C 该走空闲退出档：实测 {code}"
        f"（EXIT_OK={EXIT_OK} / EXIT_INTERRUPTED={EXIT_INTERRUPTED}）"
    )
    assert "强制打断" not in log_text, "这一格不该落强制打断那一行"
    assert SIGBREAK_LINE in log_text
    assert "退出原因与退出码：" in log_text
    assert "EXIT_OK" in log_text, f"log 里该查得到 EXIT_OK 这一档：\n{log_text[-1200:]}"

    after = session_json.read_bytes()
    assert after == before, (
        "收尾干净走完之后的这一次关停不该改写盘上那份 session.json：\n"
        f"发信号前 {before.decode('utf-8', 'replace')}\n"
        f"退出之后 {after.decode('utf-8', 'replace')}"
    )
    record_after = _disk_record(proc)
    assert record_after["state"] == "done", record_after
    assert record_after["interrupted"] is False, record_after
    assert record_after["ended_at"], record_after

    # **下一场能开起来**：同一输出根上重起（`_spawn_env()` 按 `<家目录>/../out` 算输出根，
    # 因此把第二个家目录取成第一个的兄弟目录，两趟指向的输出根就是同一个）
    restarted_home = case_env.parent / "home-clean-drain-restart"
    restarted = service_process(
        home=restarted_home, capture=None, env=_spawn_env(restarted_home)
    )
    try:
        restarted.wait_ready()
        snapshot = restarted.snapshot()
        notice = snapshot["notice"] or {}
        assert snapshot["top_state"] == "idle", snapshot
        assert notice.get("kind") != "restart_unfinished", (
            f"收尾干净走完的场次不该被重启扫描认领：{notice}"
        )
        started = post_action(restarted.base_url, "start")
        assert started.status_code == 200, (
            f"同一输出根上该能直接开新场：{started.status_code} {started.text}"
        )
        print(
            f"[C1 证据] stop 收尾干净（record.state={record_before['state']!r} / "
            f"ended_at={record_before['ended_at']!r} / interrupted={record_before['interrupted']!r}）"
            f"之后按一次 Ctrl+C -> 退出码 {code}（EXIT_OK={EXIT_OK}）；"
            f"session.json 逐字节未变；重启后 notice.kind={notice.get('kind')!r}"
            f"（不再挂 restart_unfinished）；POST start -> {started.status_code}"
        )
    finally:
        restarted.terminate()
    _finish_case(case_started, "①c 收尾干净之后按一次")


# ---------------------------------------------------------------------------
# ② 录音中按一次 → 收尾干净档
# ---------------------------------------------------------------------------


def test_recording_sigbreak_drains_and_exits_zero(service_process, case_env):
    """录音中按一次 `CTRL_BREAK_EVENT` → 收尾真跑完，收尾干净档退出码。

    **收尾期间服务照常应答**（收尾协程不把事件循环钉住）：假会后钩子在工作线程里睡
    `SHUTDOWN_BLOCKING_HOOK_SECONDS`，收尾进度因此在进度条上真的看得见 —— 断言在**进程还活着**
    的时候读到一次“收尾步骤结束”的快照，且 `ended.phase == "draining"`（此刻确实还在收尾）。

    **契约点名的「退出前读到的那份 `/api/state`」（§3.8 ② 条、审查问题 M1 / m7b）**：收尾终态
    （`top_state == "ended"` 且 `ended.phase == "done"`）**在进程退出之前真的从 `/api/state`
    读一次** —— 这里显式断言读回来的那一份 `source == "/api/state"`（**不是**退化到盘上那份
    `session.json` 的替代品）；退到盘上只在「服务刚好在退出」那一瞬才发生，真发生时这条断言
    当场红，而不是把「盘上也对得上」读成「运行期快照读到了」。终态读到手之后再看**盘上**
    那一版（`state == "done"`、四步都有终态、`interrupted == false`、`ended_at` 有值），
    退出码 `EXIT_OK` 单独断言。
    """
    case_started = time.monotonic()
    plugin = fakes.write_blocking_post_hook(
        case_env, block_seconds=SHUTDOWN_BLOCKING_HOOK_SECONDS
    )
    plugin.config["capture.backend"] = "dev"
    plugin.config["post.hook"] = "meeting_drain_fakes:BlockingThreadPostHook"
    fakes.write_plugin_config(case_env, plugin)

    proc = _spawn(service_process, case_env, plugin)
    proc.wait_ready()
    wait_until(plugin.module_loaded, timeout=SIGBREAK_STATE_POLL_TIMEOUT_SECONDS)
    _start(proc)
    session_dir = _latest_session_dir(proc)
    assert read_session_json(session_dir)["state"] == "recording"

    proc.send_interrupt()

    # 收尾期间服务照常应答：读得到「收尾中 + 某一步已落定」
    progress = _wait_state(
        proc,
        lambda snap: (snap.get("ended") or {}).get("phase") == "draining"
        and any(
            step["status"] in ("done", "skipped")
            for step in (snap.get("ended") or {}).get("steps") or []
        ),
        what="收尾进行中（且至少一步已落定）",
    )
    assert progress["top_state"] == "ended", progress.get("ended")

    # **退出前读到的那份 `/api/state`**：收尾终态停在 `ended` 且 `ended.phase == "done"`
    # （收尾终态不回 `idle`；这一步在进程退出之前读，读法见 `_state_at_terminal()`）
    terminal = _state_at_terminal(
        proc,
        lambda snap: snap.get("top_state", "ended") == "ended"
        and (snap.get("ended") or {}).get("phase") == "done",
        what="退出前读到 ended.phase == done 的 /api/state",
        timeout=SIGBREAK_STATE_POLL_TIMEOUT_SECONDS,
    )
    assert terminal is not None, "退出前该读到一次 ended.phase == done 的 /api/state"
    # **这一份必须是从 `/api/state` 读到的**（审查问题 m7b）：`source == "session.json"` 是
    # `_state_at_terminal()` 在「读不到运行期快照」时的退化替身，它证明不了契约 §3.8 ② 条
    # 要的那件事（退出前真的读到过运行期快照）。撞上退化说明那一刻服务已经答不上了 ——
    # 这条断言因此把偶发判红，而不是静默放行。
    assert terminal["source"] == "/api/state", (
        f"这一步要求真的从运行期快照读到收尾终态，实测来源是 {terminal['source']!r}"
        f"（退化到盘上那份）：{terminal}"
    )
    # 真读到服务那一刻的应答：顶层状态同样必须是 `ended`（收尾终态停在 `ended`）
    assert terminal["top_state"] == "ended", terminal
    assert (terminal.get("ended") or {}).get("phase") == "done", terminal

    code = proc.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
    assert code == EXIT_OK

    record = read_session_json(session_dir)
    assert record is not None
    assert record["state"] == "done"
    assert record["interrupted"] is False
    assert record["ended_at"]
    statuses = [step["status"] for step in (record.get("drain") or {}).get("steps") or []]
    assert [step["id"] for step in (record.get("drain") or {}).get("steps") or []] == list(
        DRAIN_STEP_IDS
    )
    assert set(statuses) <= set(DRAIN_STEP_STATUS)
    assert all(status in ("done", "skipped") for status in statuses)
    assert _log_text(proc).count("退出原因与退出码：") >= 1
    # 两路读到的收尾终态一致：退出前那份 `/api/state` 的相位与盘上那一版逐字对上
    assert (terminal.get("ended") or {}).get("phase") == record["state"]
    _finish_case(case_started, "② 录音中按一次")


# ---------------------------------------------------------------------------
# ③ 收尾中途第二次按 → 强制打断档
# ---------------------------------------------------------------------------


def test_second_sigbreak_during_drain_exits_130(service_process, case_env):
    """收尾进行中再按一次 → `interrupted: true`、`state == "draining"`、强制打断档退出码。

    **同步点不靠抢时间窗口**：假会后钩子在工作线程里睡
    `SHUTDOWN_BLOCKING_HOOK_SECONDS`，轮询 `/api/state` 一定能在阻塞结束之前查到
    `post` 那一步 `status == "running"`，那时收尾确实还在跑。

    **「立即中止」的时延**（§3.5 行为表第二行、修审查问题 M5）：第二次按下之后进程必须在
    `INTERRUPT_EXIT_LATENCY_LIMIT_SECONDS` 之内退出，而**在飞的同步步还剩
    `SHUTDOWN_BLOCKING_HOOK_SECONDS` 那么久** —— 收尾四步的工作线程是 daemon，所以这段
    剩余时长不参与进程退出（判据与实测见那个常量的说明）。
    """
    case_started = time.monotonic()
    plugin = fakes.write_blocking_post_hook(
        case_env, block_seconds=SHUTDOWN_BLOCKING_HOOK_SECONDS
    )
    plugin.config["capture.backend"] = "dev"
    fakes.write_plugin_config(case_env, plugin)

    proc = _spawn(service_process, case_env, plugin)
    proc.wait_ready()
    wait_until(plugin.module_loaded, timeout=SIGBREAK_STATE_POLL_TIMEOUT_SECONDS)
    _start(proc)

    assert post_action(proc.base_url, "stop").status_code == 200
    during = _wait_state(
        proc,
        lambda snap: _step_statuses(snap).get("post") == "running",
        what="收尾的 post 那一步停在 running",
    )
    assert during["ended"]["phase"] == "draining"

    pressed = time.monotonic()
    proc.send_interrupt()
    code = proc.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
    exit_latency = time.monotonic() - pressed
    assert code == EXIT_INTERRUPTED

    _assert_interrupted_face(proc)
    log_text = _log_text(proc)
    assert INTERRUPTED_LINE in log_text
    # **M5 的那一格**：这一档（`stop` 之后收尾中再按）用户只按了**一次**，而冻结常量的文案
    # 写的是「用户第二次 Ctrl+C」—— 常量逐字冻结不动，同一行里补上真实按下次数，记录因此
    # 不失真。子进程的计数器从 0 起，这一按就是第 1 次。
    assert f"{INTERRUPTED_LINE}（本次是第 1 次按下）" in log_text, (
        f"强制打断那一行该带上真实按下次数：\n{log_text[-1200:]}"
    )

    # 「立即中止」：这一路的退出时延与在飞那一步还剩多久无关（daemon 工作线程不等它），
    # 因此「退出没等在飞步」这件事由这条上限单独咬着
    assert exit_latency <= INTERRUPT_EXIT_LATENCY_LIMIT_SECONDS, (
        f"第二次 Ctrl+C 之后进程用了 {exit_latency:.2f}s 才退出（上限 "
        f"{INTERRUPT_EXIT_LATENCY_LIMIT_SECONDS}s）—— 这个量级说明解释器在等收尾步骤的"
        f"工作线程（现场里那一步阻塞 {SHUTDOWN_BLOCKING_HOOK_SECONDS}s）"
    )
    _finish_case(case_started, "③ 收尾中途第二次按")


# ---------------------------------------------------------------------------
# ③b 同一个进程**连按两次**：第二次的 `force=True` 那一格（**C1**）
#
# 契约 §3.5 的行为表把四类触发点列成四格并要求**逐条落地、不许合并成一条路径**。其中
# 「第二次按下 / 收尾进行中」这一格的判据落在**处理函数内部数出来的按下次数**上：处理函数
# 每进来一次就把 `_shutdown_press_count` 加一、算出 `force = 次数 > 1`，随回调一起排进事件
# 循环；`ServiceRuntime.trigger_shutdown(force=...)` 据此走强制打断那一支。
#
# ③（`test_second_sigbreak_during_drain_exits_130`）验的是**另一格**：那一刻 `stop` 已经让
# 盘上停在 `ended·draining`，用户**只按了一次**，判据落在状态上（`force=False`），落进 log 的
# 是「本次是第 1 次按下」。**「同一个进程连按两次、第二次带 `force=True`」这一格因此在
# 修本条之前没有任何进程级用例走到过。** 下面这一条把它走到底：连按两次、断言落进 log 的
# 是「本次是第 2 次按下」（那行文案里的次数就是 `force` 的实测形式）。
# ---------------------------------------------------------------------------


def test_second_press_in_the_same_process_carries_force_true(service_process, case_env):
    """**同一个进程连按两次** → 第二次按下带 `force=True`，强制打断档收场（C1）。

    现场（一个子进程、两次 `CTRL_BREAK_EVENT`）：

      1. `start` 进 `recording`；
      2. **第一次按下** —— 这一刻 `top_state == "recording"`，走 §3.5 第一行那条路
         （`stop_session(reason="ctrl_c")` 起收尾、等它落定）；断言它**确实走的是这一行**
         （服务级 log 里有「第一次 Ctrl+C：收尾已起」），于是第二次按下的 `force=True`
         真的来自「同一次进程里的第二次信号」，与状态判据无关；
      3. 收尾**还在跑**（假会后钩子 `BlockingThreadPostHook` 在工作线程里睡
         `SHUTDOWN_BLOCKING_HOOK_SECONDS`，轮询到 `post` 那一步停在 `running`——同步点确定，
         不靠抢时间窗口）；
      4. **第二次按下** —— 这时收尾仍在进行，处理函数内部计数走到 2、`force=True`。

    断言（与 ③ 同一套结果字段，另加 `force` 那一格的可判定形式）：

      - 退出码 `EXIT_INTERRUPTED`（强制打断档）；
      - 盘上 `state == "draining"`、`interrupted is True`、无 `ended_at`、原 `steps` 保留
        （`_assert_interrupted_face()` 逐项断言）；
      - 服务级 log 里有强制打断那行，且**「本次是第 2 次按下」**——这一行里的次数就是
        `force` 的实测形式（`force = 次数 > 1`）；
      - 两行「收到 SIGBREAK，进入关停」都在（两个信号真的都送达了同一个处理函数）；
      - 「立即中止」：退出时延在 `INTERRUPT_EXIT_LATENCY_LIMIT_SECONDS` 之内（在飞那一步
        还剩 `SHUTDOWN_BLOCKING_HOOK_SECONDS`，收尾用的工作线程是 daemon、进程不等它）。
    """
    case_started = time.monotonic()
    plugin = fakes.write_blocking_post_hook(
        case_env, block_seconds=SHUTDOWN_BLOCKING_HOOK_SECONDS
    )
    plugin.config["capture.backend"] = "dev"
    plugin.config["post.hook"] = "meeting_drain_fakes:BlockingThreadPostHook"
    fakes.write_plugin_config(case_env, plugin)

    proc = _spawn(service_process, case_env, plugin)
    proc.wait_ready()
    wait_until(plugin.module_loaded, timeout=SIGBREAK_STATE_POLL_TIMEOUT_SECONDS)
    _start(proc)
    session_dir = _latest_session_dir(proc)
    assert read_session_json(session_dir)["state"] == "recording"

    # **第一次按下**：落在 `recording` 这一格（`force=False`，走 stop_session 那条路）
    first_pressed = time.monotonic()
    proc.send_interrupt()
    _wait_state(
        proc,
        lambda snap: (snap.get("ended") or {}).get("phase") == "draining",
        what="第一次按下之后收尾已起（ended.phase == draining）",
    )
    started_line = "第一次 Ctrl+C：收尾已起（与 stop 同一条路径），等它落定再关停服务"
    assert started_line in _log_text(proc), (
        f"第一次按下该走「录音中按一次」那一条路（日志里没有那句话）：\n{_log_text(proc)[-1500:]}"
    )

    # **确定的同步点**：收尾还在跑（`post` 那一步停在 `running`），这时才按第二次
    during = _wait_state(
        proc,
        lambda snap: _step_statuses(snap).get("post") == "running",
        what="两次按下之间收尾的 post 那一步停在 running",
    )
    assert during["ended"]["phase"] == "draining", during.get("ended")

    second_pressed = time.monotonic()
    proc.send_interrupt()
    code = proc.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
    exit_latency = time.monotonic() - second_pressed
    assert code == EXIT_INTERRUPTED

    disk = _assert_interrupted_face(proc)
    log_text = _log_text(proc)
    # **C1 的那一格**：这一行里的次数就是处理函数内部数出来的 `force`（2 > 1）
    assert f"{INTERRUPTED_LINE}（本次是第 2 次按下）" in log_text, (
        f"第二次按下该落「本次是第 2 次按下」那一行（`force=True` 的实测形式）：\n"
        f"{log_text[-1500:]}"
    )
    # 两次按下都真的送到了同一个处理函数
    assert log_text.count(SIGBREAK_LINE) == 2, (
        f"同一个进程里该落下两行「{SIGBREAK_LINE}」：\n{log_text[-1500:]}"
    )
    # 「立即中止」：退出时延与在飞那一步还剩多久无关（收尾的工作线程是 daemon）
    assert exit_latency <= INTERRUPT_EXIT_LATENCY_LIMIT_SECONDS, (
        f"第二次按下之后进程用了 {exit_latency:.2f}s 才退出（上限 "
        f"{INTERRUPT_EXIT_LATENCY_LIMIT_SECONDS}s；现场里那一步阻塞 "
        f"{SHUTDOWN_BLOCKING_HOOK_SECONDS}s）"
    )

    steps = ((disk.get("drain") or {}).get("steps")) or []
    print(
        f"[C1 证据] 同一个进程连按两次：第一次 @ {first_pressed - case_started:.3f}s"
        f"（force=False，走 stop_session 那条路，log 里查到「{started_line}」）；"
        f"第二次 @ {second_pressed - case_started:.3f}s（force=True，收尾仍在跑：post="
        f"{_step_statuses(during)['post']!r}）\n"
        f"  退出码 {code}（EXIT_INTERRUPTED={EXIT_INTERRUPTED}），第二次按下到退出 "
        f"{exit_latency:.3f}s\n"
        f"  盘上 {session_dir.name}：state={disk['state']!r} / interrupted={disk['interrupted']!r}"
        f" / ended_at={disk['ended_at']!r} / steps={[step['status'] for step in steps]}\n"
        f"  服务级 log 那一行：{INTERRUPTED_LINE}（本次是第 2 次按下）；"
        f"「{SIGBREAK_LINE}」共 {log_text.count(SIGBREAK_LINE)} 行"
    )
    _finish_case(case_started, "③b 同一进程连按两次")


# ---------------------------------------------------------------------------
# ④ 已经停在 `ended·draining` 再按 → 与 ③ 同结果
# ---------------------------------------------------------------------------


def test_sigbreak_while_already_draining_exits_130(service_process, case_env):
    """已经停在收尾失败现场再按一次 → 与 ③ 同结果（**不为它单开第三条路径**）。

    失败现场由 `FailingPostHook` 造出：`POST stop` 之后轮询到 `ended.phase == "draining"`
    且 `post` 那一步 `status == "failed"`，这一档下服务不自杀，正好等第二次信号。
    """
    case_started = time.monotonic()
    plugin = fakes.write_failing_post_hook(case_env)
    fakes.write_plugin_config(case_env, plugin)

    proc = _spawn(service_process, case_env, plugin)
    proc.wait_ready()
    wait_until(plugin.module_loaded, timeout=SIGBREAK_STATE_POLL_TIMEOUT_SECONDS)
    _start(proc)

    assert post_action(proc.base_url, "stop").status_code == 200
    _wait_state(
        proc,
        lambda snap: (snap.get("ended") or {}).get("phase") == "draining"
        and _step_statuses(snap).get("post") == "failed",
        what="收尾落在失败现场（phase 保持 draining、post 是 failed）",
    )
    # 服务不自杀：这一档下进程照常活着
    assert proc.poll() is None

    proc.send_interrupt()
    code = proc.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
    assert code == EXIT_INTERRUPTED

    _assert_interrupted_face(proc)
    _finish_case(case_started, "④ 已停在 draining 再按")


# ---------------------------------------------------------------------------
# ⑤ 收尾有步骤 `failed` → 收尾失败档
# ---------------------------------------------------------------------------


def test_failed_drain_exits_one_and_prints_log_path(service_process, case_env):
    """收尾有步骤 `failed` → 收尾失败档退出码，且终端上出现 log 路径与 `meeting post`。

    同一趟顺带断言**服务级 log** 里可查到启动与端口 / URL / 输出根、以及退出原因与退出码
    （方案 §6.3 行 304 清单的服务级那半边）。失败手段是共用支持模块里的 `FailingPostHook`
    （`launch()` 抛错，经 `PYTHONPATH` + `post.hook` 配进子进程），`dev_inject` 那三档故障
    只改事实流、造不出收尾失败。
    """
    case_started = time.monotonic()
    plugin = fakes.write_failing_post_hook(case_env)
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

    assert "绑定成功：127.0.0.1:" in log_text
    assert "http://127.0.0.1:" in log_text, "服务级 log 该写清选中的 URL"
    assert "输出根" in log_text, "服务级 log 该写清输出根"
    assert "退出原因与退出码：" in log_text
    assert "EXIT_FAILURE" in log_text
    _finish_case(case_started, "⑤ 收尾失败档")


# ---------------------------------------------------------------------------
# ⑥ 两路信号同形
# ---------------------------------------------------------------------------


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
    assert all(status in ("done", "skipped") for status in results["sigbreak"]["steps"]), results
    _finish_case(case_started, "⑥ 两路信号同形")


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
    """只记账的 logger（`cli.web()` 会往它写启动几行与出错那一行）。"""

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

    out_root = "<临时输出根>"
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
            _exit_code_runtime(statuses=("done", "skipped", "done", "skipped")),
            fallback=EXIT_OK,
        )
        == EXIT_OK
    )
    # ③ 收尾失败档：有步骤 failed → EXIT_FAILURE（这一档与兜底值无关）
    assert (
        cli_module._drain_exit_code(
            _exit_code_runtime(statuses=("done", "done", "done", "failed")),
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
            _exit_code_runtime(statuses=("done", "skipped", "done", "skipped")),
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
# `exit_code != EXIT_FAILURE` 就早退，于是「收尾四步全绿、只是服务器自己炸了」的现场也会在
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
    clean = _exit_code_runtime(statuses=("done", "skipped", "done", "skipped"))
    failed = _exit_code_runtime(statuses=("done", "skipped", "done", "failed"))
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
        `ended.steps` 里 `post` 是 `failed`（前面已经失败过一步，`flush` / `transcode` / `log`
        都已有终态）—— `_drain_reported_failed()` 因此是 `True`，正是会误报「服务不自杀」的那一格；
      - `_drain_exit_code()` 对这一组报 `EXIT_INTERRUPTED`（档位压过现场：强制打断那一支先判）。

    四件事一起断：退出码是强制打断档；终端说「收尾被强制打断」；**不出现**「服务不自杀」那句；
    终端带上 log 路径与两条出路（`meeting post` / 重启服务），并落一行 log 记下这一次打了什么。
    """
    logger = _FakeLogger()
    runtime = _exit_code_runtime(
        interrupted=True, statuses=("done", "skipped", "done", "failed")
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
