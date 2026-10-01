"""真服务子进程夹具的驱动件与全部解析 / 等待助手（phase2 §5.4）。

**定位**：本文件是测试支持模块 —— 文件名不以 `test_` 开头，`pytest` **不收集**它；
它**不是 `conftest.py`**，因此不提供 fixture（fixture 全在 `tests/conftest.py`）。

**导出全集（九个名字，逐字冻结；phase3 只引用、不自起别名）**：

    ServiceProcess / SseClient / post_action() / dev_inject() / read_session_json() /
    write_config_json() / parse_sse_frame() / read_sse_frames() / wait_until()

`__all__` **恰为这九个**；模块内另有几个**常量**（`WAIT_POLL_INTERVAL_SECONDS` /
`SERVICE_ID` 与三条 SSE 行正则）随本模块一起工作，它们是本模块的实现细节，**不进导出名单**
（名单一漂移，phase3 就会照着一个并不存在的导出名去 import）。`iter_session_dirs()` 这个
无人使用的第十个公开名已删掉。

等待助手的名字收成一个：**`wait_until()`**（§2.2 / §3.8 的冻结口径）。需要夹具能力
（真服务子进程 / SSE）的用例走本模块这一个（`timeout` 必传）；phase1 的组件级用例走
`tests/_helpers.py` 里同名的那个（签名与语义保持不变）。旧名在 `tests/` 下已经清干净。

与 phase1 的 `tests/_helpers.py` 的分工（**同一职责只留一处落点**）：子进程的底层构造
（`spawn_service()` / 家目录隔离 / 假 `webbrowser` 注入）与 ASGI 客户端仍在 `_helpers.py`
里；本模块**复用**它们并在之上补齐 phase2 契约要求的能力（`base_url` / 发中断信号 /
读退出码 / 完整的环境变量覆盖），**不另起第二套子进程管理**。
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from tests import _helpers as helpers
from tests._helpers import REPO_ROOT

#: `wait_until()` 的缺省轮询间隔（秒）；用例仍须显式传 `timeout`
WAIT_POLL_INTERVAL_SECONDS = 0.05

#: 服务标识（就绪判定与端口探针同一判据）
SERVICE_ID = "meeting"

#: 「这份快照不是 JSON 对象」那一次诊断的落点文件名（写在服务级 log 目录那一侧，
#: 随本用例的临时目录一起消失）。`ServiceProcess.snapshot()` 用它把应答原文留成现场。
SNAPSHOT_DUMP_NAME = "snapshot-shape-mismatch.txt"

#: SSE 帧里 `event:` / `data:` / `id:` 三行的解析
_EVENT_RE = re.compile(r"^event:\s*(.*)$")
_DATA_RE = re.compile(r"^data:\s?(.*)$")
_ID_RE = re.compile(r"^id:\s*(.*)$")

__all__ = [
    "ServiceProcess",
    "SseClient",
    "dev_inject",
    "parse_sse_frame",
    "post_action",
    "read_session_json",
    "read_sse_frames",
    "wait_until",
    "write_config_json",
]


# ---------------------------------------------------------------------------
# 等待助手
# ---------------------------------------------------------------------------


def wait_until(
    predicate: Callable[[], Any],
    *,
    timeout: float,
    interval: float = WAIT_POLL_INTERVAL_SECONDS,
) -> Any:
    """同步等待一个条件成立（`predicate` 返回真值即返回它）。

    `timeout` **必传**（用例里显式给具名秒数、不留裸数字）；到超时抛 `AssertionError`。
    谓词自身抛异常时继续等（探针还没就绪是常态）。

    **异常留在这里、只当诊断用**（这是本助手最容易走歪的一格）：谓词抛出的异常**一律不当成
    返回值**。它此前被摆进 `last` 里，而异常本身就是真值 —— 于是一次**瞬时**的读故障
    （本机实测过 `httpx.ReadError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。`
    与 `httpx.ReadTimeout: timed out`）会让 `wait_until()` 当场把它**当成「等到了」交回去**，
    调用点接着去取字段，炸成一句与现场无关的
    `TypeError: string indices must be integers, not 'str'`（`tests/test_drain.py` 的 ⑤f 条
    就是这么偶发的：同一趟子进程的 log 里收尾四步完全正常，红却红在这句话上）。

    现在改成就地重试：故障落在窗口里 → 下一次读把它绕过去、用例照常过；窗口内一直读不到 →
    那条报错作为**最后观察值**带进超时报告，红的时候手上有一句能读的现场。
    """
    deadline = time.monotonic() + float(timeout)
    last_failure: BaseException | None = None
    while True:
        try:
            observed = predicate()
        except Exception as exc:  # 探针自身报错时继续等（不把它当成「等到了」）
            last_failure = exc
            observed = None
        if observed:
            return observed
        if time.monotonic() >= deadline:
            detail = f"wait_until 超时（{timeout}s，间隔 {interval}s）：最后观察值 {observed!r}"
            if last_failure is not None:
                detail += (
                    f"；窗口内探针最后一次报错："
                    f"{type(last_failure).__name__}: {last_failure}"
                )
            raise AssertionError(detail)
        time.sleep(interval)


# ---------------------------------------------------------------------------
# 真服务子进程
# ---------------------------------------------------------------------------


def _generate_console_ctrl_event(event: int, process_group: int) -> bool:
    """Windows：把 `CTRL_C_EVENT` / `CTRL_BREAK_EVENT` 定向发给某个进程组。

    走 `GenerateConsoleCtrlEvent`（`os.kill` 的 `CTRL_C_EVENT` 在 Windows 上不可靠：
    子进程收不到，用例会挂到超时）。返回是否调用成功。
    """
    if os.name != "nt":  # pragma: no cover - 只在 Windows 上用
        return False
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GenerateConsoleCtrlEvent.argtypes = [ctypes.c_uint, ctypes.c_uint]
    kernel32.GenerateConsoleCtrlEvent.restype = ctypes.c_int
    return bool(kernel32.GenerateConsoleCtrlEvent(ctypes.c_uint(event), ctypes.c_uint(process_group)))


class ServiceProcess(helpers.ServiceProcess):
    """一个真服务子进程（`[sys.executable, "-m", "meeting", "web"]`，cwd = 仓库根）。

    在 phase1 的 `_helpers.ServiceProcess` 之上补齐 phase2 契约点名的三项能力与七项环境变量
    覆盖：**`base_url`**（`MEETING_PORT=0` + 子进程启动输出解析）、**向子进程发中断信号**、
    **读退出码**（`returncode`）；环境变量覆盖由调用方给全（`env=` 完整映射，或给
    `home` + 各项；`PYTHONPATH` = 仓库根 + 临时假件目录，不需要假件时至少保留仓库根）。

    **`capture=` 只在你显式传它时才生效**（审查问题 m5 / m6）：这一项写成 `MEETING_CAPTURE`
    进子进程 env，而它的优先级高于配置文件；缺省 `None` 表示采集后端**由用例的 `config.json`
    表达**（`capture.backend`）。四条 `tests/test_e2e_*.py`、`tests/test_shutdown_signal.py`
    与 `tests/test_drain.py` 都走这条路，两条子进程路径因此同源。

    **配置文件里没写 `capture.backend` 时，构造时补上测试层的共用缺省 `dev`**
    （`helpers.apply_backend_defaults()`，见 `helpers.DEFAULT_CAPTURE_BACKEND`）：
    产品缺省 `auto` 会解析到真实现，子进程 startup 里的 `probe()` 就去真枚举、真试开声卡
    设备了。用例显式写过的值（含 `null` / `auto` / 挂假件的 `module:Class`）一个字节都不动。

    `args=` 给的是**送给解释器的参数表**（`[sys.executable, *args]`），默认那条是
    `["-m", "meeting", action]`。用例需要「仍然跑真实服务、但外面包一层极小的启动脚本」时
    用它传 `["-c", "<脚本>"]` —— 例如 `tests/test_shutdown_signal.py` 的「两路信号同形」
    那一条：脚本里照常调 `meeting.cli.main(["web"])`，只是过一会儿由子进程自己给自己发
    `SIGINT`（本机把 `CTRL_C_EVENT` 送到子进程这条路走不通）。包装脚本因此只活在命令行里，
    仓库内与仓库外都不落探针文件。
    """

    def __init__(
        self,
        home: Path,
        *,
        action: str = "web",
        capture: str | None = None,
        port: int | None = 0,
        out_root: Path | str | None = None,
        log_dir: Path | str | None = None,
        no_browser: bool = True,
        pythonpath: str | None = None,
        env: dict[str, str] | None = None,
        extra_env: dict[str, str] | None = None,
        args: list[str] | None = None,
    ) -> None:
        home = Path(home)
        home.mkdir(parents=True, exist_ok=True)
        self.home = home

        # **两个后端的共用缺省**（与 `_helpers.spawn_service()` / `run_cli()` 同源）：本用例的
        # `config.json` 里没写 `capture.backend` 时补 `DEFAULT_CAPTURE_BACKEND`（`dev` 替身）、
        # 没写 `post.hook` 时补 `DEFAULT_POST_HOOK`（`null` 缺席档）。不补的话产品缺省 `auto`
        # 会让子进程 startup 里的 `probe()` 走到真实采集入口上、收尾第 4 步起一条后台线程跑
        # 整条会后链。用例自己写过的值一个字节都不动（只补缺，见 `apply_backend_defaults()`）。
        helpers.apply_backend_defaults(home)

        # 环境变量覆盖：调用方给全的 `env` 优先；否则按七项布置（家目录两项都设）
        overrides: dict[str, str] = {}
        overrides["HOME"] = str(home)
        overrides["USERPROFILE"] = str(home)
        if port is not None:
            overrides["MEETING_PORT"] = str(port)
        # `out_root=` 这一项沿用旧名，语义变了：它只是**测试层的场次放置根**
        # （给 `post_action()` 为 `start` 动作预建工作区目录用），不再写 env —— 产品
        # 已经没有 `MEETING_OUT_ROOT` 这条变量与「输出根」这层配置。
        self.sessions_root = None
        if out_root is not None:
            sessions_root = Path(out_root)
            sessions_root.mkdir(parents=True, exist_ok=True)
            self.sessions_root = sessions_root
        if log_dir is not None:
            overrides["MEETING_LOG_DIR"] = str(log_dir)
        if capture is not None:
            # **只有显式传 `capture=` 才写进子进程 env**（审查问题 m5 / m6）：`MEETING_CAPTURE`
            # 的优先级高于配置文件，一个静默的缺省值会把各用例自己写在 `config.json` 里的
            # `capture.backend` 压住（假件挂不上、替身选错档）。子进程的采集后端因此统一由
            # `config.json` 表达；这里的 `capture=` 留给「有意验环境变量覆盖」的用例，
            # 缺省 `None` 表示这一项不出现。
            overrides["MEETING_CAPTURE"] = str(capture)
        if no_browser:
            overrides["MEETING_NO_BROWSER"] = "1"
        else:
            overrides.pop("MEETING_NO_BROWSER", None)
        overrides["PYTHONPATH"] = (
            str(REPO_ROOT) if pythonpath is None else str(pythonpath)
        )
        if extra_env:
            overrides.update(extra_env)
        if env:
            # 调用方给全的一整份映射：**在算好的那一份之上覆盖**（不是顶掉整份）。
            # 顶掉的话，`capture=` / `port=` / `pythonpath=` 这几个关键字会**静默失效** ——
            # 例如 `extra_env={"MEETING_CAPTURE": ""}` 想压掉 `capture="dev"`，结果连
            # `HOME` 与 `PYTHONPATH` 一起被顶掉，子进程读到真实家目录（隔离静默落空）。
            overrides.update(env)

        # 看门断言（与 `_helpers.spawn_service()` 同一条）：走到「打开浏览器」这一步而不注入
        # 假 webbrowser，就会落到宿主的默认浏览器上 —— 直接把用例判红。
        if overrides.get("MEETING_NO_BROWSER", "") != "1" and not overrides.get(
            helpers.FAKE_BROWSER_PATH_ENV
        ):
            raise AssertionError(
                "子进程要走到「打开浏览器」这一步，就必须经 fake_browser_module() 注入假 "
                "webbrowser（env 里带 MEETING_TEST_FAKE_BROWSER_PATH）：否则这一步会落到"
                "宿主的默认浏览器上，测试会打开真窗口。"
            )

        self._proc = subprocess.Popen(
            [sys.executable, "-m", "meeting", action] if args is None else [sys.executable, *args],
            cwd=str(REPO_ROOT),
            env=helpers.service_env(home, extra=overrides),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        self._lines: queue.Queue[str] = queue.Queue()
        self._all_lines: list[str] = []
        self._reader = threading.Thread(target=self._pump, name="service-stdout", daemon=True)
        self._reader.start()
        self._base_url: str | None = None

    # -- 读数 -------------------------------------------------------------

    def _pump(self) -> None:
        stream = self._proc.stdout
        if stream is None:  # pragma: no cover - PIPE 一定在
            return
        try:
            for line in stream:
                self._all_lines.append(line)
                self._lines.put(line)
        except Exception:  # pragma: no cover - 收场时管道被关
            pass

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def returncode(self) -> int | None:
        """读退出码（进程还在跑时为 `None`）。"""
        return self._proc.poll()

    def poll(self) -> int | None:
        return self._proc.poll()

    def stdout(self) -> str:
        """子进程 stdout 到目前为止的全部内容。"""
        return "".join(self._all_lines)

    def stdout_text(self) -> str:
        return self.stdout()

    def stderr(self) -> str:
        """子进程 stderr（进程已收场时读管道；还在跑就返回空串，避免阻塞）。"""
        stream = self._proc.stderr
        if stream is None or self._proc.poll() is None:
            return ""
        try:
            return stream.read()
        except Exception:  # pragma: no cover - 管道已关
            return ""

    def log_path(self) -> Path:
        return helpers.service_log_path_of(self.home)

    def log_text(self) -> str:
        return helpers.read_text(self.log_path())

    @property
    def base_url(self) -> str:
        """`MEETING_PORT=0` 时从子进程启动输出里解析出的地址（**不猜端口**）。"""
        if self._base_url is None:
            self._base_url = self.wait_for_base_url()
        return self._base_url

    def wait_for_base_url(self, *, timeout: float = 20.0) -> str:
        """等子进程打印 `URL：http://127.0.0.1:<port>` 并解析成 `base_url`。"""

        def parsed() -> str | None:
            for line in list(self._all_lines):
                match = re.search(r"URL：(http://127\.0\.0\.1:\d+)", line)
                if match and not match.group(1).endswith(":0"):
                    return match.group(1)
            return None

        return str(wait_until(parsed, timeout=timeout))

    def port(self, *, timeout: float = 20.0) -> int:
        return int(self.base_url.rsplit(":", 1)[1])

    def wait_ready(self, *, timeout: float = 20.0) -> str:
        """就绪判定 = `/api/heartbeat` 回 200 且 body 里 `"service": "meeting"`。"""
        url = self.wait_for_base_url(timeout=timeout)

        def ready() -> bool:
            if self._proc.poll() is not None:
                raise AssertionError(
                    f"服务子进程提前退出（退出码 {self._proc.poll()}）；"
                    f"stdout={self.stdout()[-2000:]} stderr={self.stderr()[-2000:]}"
                )
            try:
                response = helpers.local_get(f"{url}/api/heartbeat", timeout=1.0)
            except Exception:
                return False
            if response.status_code != 200:
                return False
            return f'"{SERVICE_ID}"' in response.text or f"'{SERVICE_ID}'" in response.text

        wait_until(ready, timeout=timeout)
        return url

    def snapshot(self) -> dict[str, Any]:
        """读一次 `/api/state` 并返回那份快照。

        **状态码在这一层就断言**（审查问题 m7c）：`GET /api/state` 是「收尾期间照常可观测」
        这条判据的读入口（§3.1 第 6 条），拿到一份非 200 的应答说明服务答得不对 —— 那件事
        必须当场说话，不能悄悄回一份别的什么 JSON 让调用点接着断言字段。此前这里只做
        `response.json()`，而 `tests/test_drain.py` 的注释已经声称「`proc.snapshot()` 自己就
        断言状态码」，两处对不上；现在二者一致。

        **形状也在这里就断言**（同一条口径的下一格）：这份快照**必须是一个 JSON 对象**。
        少了这一格，一份不是对象的应答会一路走到调用点的 `snapshot["ended"]` 上，炸成一句
        `TypeError: string indices must be integers, not 'str'` —— 那句话既没说清是哪一次读、
        也没留下应答原文，读的人只能猜（`tests/test_drain.py` 的 ⑤f 条就是这样偶发过一次：
        同一趟子进程的 log 显示收尾完全正常，红却红在「读回来的东西不是 dict」上）。
        这一格挑明之后，那份快照只可能是对象、或者是一份说得清形状问题的报错，中间那一档没有了。

        报错里带齐**应答原文**、状态码与 `Content-Type`，并另存一份到 `SNAPSHOT_DUMP_NAME`
        指名的那个文件（落在服务级 log 目录那一侧，随本用例的临时目录一起消失）——
        复核时手上有东西可看。

        **重试交回给等待助手**：形状不对时这条断言在场，`wait_until()` 会把它当成探针报错、
        就地重试（理由与判红口径见那个助手）；窗口内一直是这个形状，它就作为**最后观察值**
        进超时报告，落盘那份诊断也留在原处供复核。
        """
        response = helpers.local_get(f"{self.base_url}/api/state", timeout=5.0)
        assert response.status_code == 200, (
            f"GET /api/state 该回 200，实测 {response.status_code}：{response.text[:500]}"
        )
        snapshot = response.json()
        assert isinstance(snapshot, dict), (
            f"GET /api/state 该回一个 JSON 对象，实测 {type(snapshot).__name__}"
            f"（{self._bad_snapshot_report(response, snapshot)}）"
        )
        return snapshot

    def _bad_snapshot_report(self, response: "httpx.Response", snapshot: Any) -> str:
        """把「这份快照不是对象」那一次的应答原文落盘，并拼出一句能直接读的诊断。

        **不吞、不兜底**：返回值只用于拼错误文案，判红由调用点那条断言负责。
        落盘失败（例如目录不可写）只让诊断少一条线索，不改判红这件事本身。
        """
        report = (
            f"这一趟 GET {self.base_url}/api/state 的状态码 {response.status_code}、"
            f"Content-Type {response.headers.get('content-type')!r}、"
            f"应答 {len(response.content)} 字节、解析出来的类型 {type(snapshot).__name__}、"
            f"解析出来的值 {snapshot!r}；应答原文（前 800 字符）：{response.text[:800]!r}"
        )
        try:
            dump = Path(self.log_path()).parent / SNAPSHOT_DUMP_NAME
            dump.write_text(report + "\n", encoding="utf-8")
            report += f"；本条诊断另存于 {dump}"
        except OSError as exc:  # 诊断落盘失败不改判红
            report += f"；本条诊断落盘失败：{type(exc).__name__}: {exc}"
        return report

    # -- 信号 / 收场 -------------------------------------------------------

    def send_interrupt(self) -> None:
        """向子进程发中断信号（Windows 上经 `CTRL_BREAK_EVENT`，对应服务侧的 `SIGBREAK`）。

        信号常量**取自 `signal` 模块**（`CTRL_BREAK_EVENT` / `CTRL_C_EVENT`）：
        手写数字会撞上别的信号（`6` 是 `SIGABRT`，子进程会以「被中止」收场，走不到本服务的关停路径）。
        """
        if os.name == "nt":
            os.kill(self._proc.pid, signal.CTRL_BREAK_EVENT)
        else:  # pragma: no cover - 本项目在 Windows 上跑
            os.kill(self._proc.pid, signal.SIGINT)

    def send_ctrl_c(self) -> None:
        """发 `CTRL_C_EVENT`（与 `CTRL_BREAK_EVENT` 走服务侧的同一个处理函数）。

        `os.kill(pid, signal.CTRL_C_EVENT)` 在 Windows 上**不可靠**（子进程收不到，
        用例会挂到超时），这里用 `GenerateConsoleCtrlEvent` 定向发给子进程所在的进程组 ——
        子进程由 `CREATE_NEW_PROCESS_GROUP` 起在同一组里，事件因此只落到它身上。
        """
        if os.name == "nt":
            if not _generate_console_ctrl_event(signal.CTRL_C_EVENT, self._proc.pid):
                raise AssertionError(
                    f"GenerateConsoleCtrlEvent(CTRL_C_EVENT, {self._proc.pid}) 调用失败"
                )
        else:  # pragma: no cover
            self._proc.send_signal(signal.SIGINT)

    def wait(self, *, timeout: float = 20.0) -> int:
        """等子进程收场并返回退出码。"""
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
            raise AssertionError(f"服务子进程没有在 {timeout}s 内退出")
        return int(self._proc.returncode or 0)

    def kill(self) -> None:
        """硬杀（模拟断电 / 被任务管理器杀掉）：发 `kill()` 之后按同一个 grace 等它收场。"""
        if self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait(timeout=helpers.PROCESS_TERMINATE_GRACE_SECONDS)
        self._close_pipes()

    def terminate(self) -> None:
        """收场：先 `terminate`、等 `PROCESS_TERMINATE_GRACE_SECONDS` 秒、仍在才 `kill`，
        随后**关掉 stdout / stderr 两个管道**。

        宽限秒数取自 `tests/_helpers.py` 的 `PROCESS_TERMINATE_GRACE_SECONDS`（契约 §3.8
        文末那张表里的具名常量）：本文件与 phase1 的 `ServiceProcess` 因此共用同一个口径，
        不再各自写一个裸秒数。

        关管道这一步是硬要求（M6）：`Popen(stdout=PIPE, stderr=PIPE)` 交回来的两个句柄
        由本对象持有，收场路径不收它们，`python -m pytest -q -W always` 每条起过子进程的
        用例都会报一串 `ResourceWarning: unclosed file`。关闭之前先把**还在用的输出**读出来
        （`stderr()` 的语义不变：进程已收场时返回它读到的全部内容），因此诊断能力不丢。
        重复收场是幂等的（`_close_pipes()` 里逐项 `contextlib.suppress`）。
        """
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=helpers.PROCESS_TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self._proc.kill()
                self._proc.wait()
        self._close_pipes()

    def _close_pipes(self) -> None:
        """把已用的输出先读出来，再关掉 stdout / stderr 两个管道（幂等）。

        - stdout 由 `_pump` 线程持续读走，这里只 `close()`（`_all_lines` 已完整）；
        - stderr 没有读线程，进程收场后 `stderr()` 会把它读空并缓存下来，再关句柄。
        """
        self.stderr()
        for stream in (self._proc.stdout, self._proc.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:  # pragma: no cover - 已关 / 管道已断
                pass

    def __enter__(self) -> "ServiceProcess":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.terminate()


# ---------------------------------------------------------------------------
# SSE 解析与读取
# ---------------------------------------------------------------------------


def parse_sse_frame(text: str) -> dict[str, Any]:
    """把一个 SSE 帧的原文解析成 `{"event", "data", "id", "comment", "raw"}`。

    `data` 是**解析后的 JSON**（解析不了时保留原文并标 `"parsed": False`）；
    注释帧（`: ping`）里 `comment` 有值、`event` 为 `None`。
    """
    frame: dict[str, Any] = {
        "event": None,
        "data": None,
        "id": None,
        "comment": None,
        "raw": text,
        "parsed": False,
        "retry": None,
    }
    data_lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith(":"):
            frame["comment"] = line[1:].strip()
            continue
        match = _EVENT_RE.match(line)
        if match:
            frame["event"] = match.group(1).strip()
            continue
        match = _DATA_RE.match(line)
        if match:
            data_lines.append(match.group(1))
            continue
        match = _ID_RE.match(line)
        if match:
            frame["id"] = match.group(1).strip()
            continue
        if line.startswith("retry:"):
            frame["retry"] = line.split(":", 1)[1].strip()
    if data_lines:
        raw = "\n".join(data_lines)
        try:
            frame["data"] = json.loads(raw)
            frame["parsed"] = True
        except ValueError:
            frame["data"] = raw
    return frame


def _split_frame(buffer: str) -> tuple[str, str, str]:
    """从缓冲区里切出一个 SSE 帧：返回 `(帧原文, 分隔符, 剩下的部分)`。

    SSE 的空行既可能是 `\\n\\n`，也可能是 `\\r\\n\\r\\n`（uvicorn 在某些实现下会规范成后者），
    两者都要认，缺一种就会「一帧都读不到」。
    """
    for separator in ("\r\n\r\n", "\n\n"):
        index = buffer.find(separator)
        if index != -1:
            return buffer[:index], separator, buffer[index + len(separator):]
    return buffer, "", ""


class SseClient:
    """一条 SSE 订阅（同步 `httpx.stream` + 后台读线程；`close()` 保证断开）。

    用法（测试一律同步写）：:

        with SseClient(base_url) as client:
            first = client.read_frame(timeout=10.0)
            frames = client.read_frames(3, timeout=10.0)
    """

    def __init__(self, base_url: Any, *, path: str = "/api/events", timeout: float = 30.0) -> None:
        self.base_url = _resolve_base_url(base_url)
        self.path = path
        self._frames: queue.Queue[dict[str, Any]] = queue.Queue()
        self._raw: list[str] = []
        self._buffer = ""
        self._stop = threading.Event()
        # **每条订阅自己持有一条连接**（`close()` 关掉的就是它，不与别人共用），但 TLS 上下文
        # 走 `tests/_helpers.py` 那份进程级共享的：本机新建 `httpx.Client` 的 0.83 秒几乎全花在
        # `ssl.create_default_context()` 上，实测传进共享上下文之后只要 0.03 秒。明文回环上
        # `verify` 对请求结果毫无影响，而本文件的好几条用例一条就要开两到四条订阅。
        self._client = httpx.Client(
            trust_env=False,
            timeout=httpx.Timeout(timeout, read=timeout),
            verify=helpers.local_ssl_context(),
        )
        # 注意：`Client.stream()` 返回的是**上下文管理器**，真正的响应要靠 `__enter__()` 取出来
        self._stream = self._client.stream("GET", f"{self.base_url}{path}")
        self._response = self._stream.__enter__()
        self._reader = threading.Thread(target=self._pump, name="sse-reader", daemon=True)
        self._reader.start()

    # -- 读 ---------------------------------------------------------------

    def _pump(self) -> None:
        try:
            for chunk in self._response.iter_text():
                if self._stop.is_set():
                    break
                self._buffer += chunk
                while True:
                    raw, separator, rest = _split_frame(self._buffer)
                    if not separator:
                        break
                    self._buffer = rest
                    if raw.strip():
                        self._raw.append(raw)
                        self._frames.put(parse_sse_frame(raw))
        except Exception:  # pragma: no cover - 连接被关时读线程退出即可
            pass

    @property
    def raw_text(self) -> str:
        return "\n\n".join(self._raw)

    def read_frame(self, *, timeout: float) -> dict[str, Any]:
        """读一帧（到超时抛 `AssertionError`，不留无上限等待）。"""
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(
                f"{timeout}s 内没有读到 SSE 帧（已读到 {len(self._raw)} 帧；"
                f"原文尾部：{self.raw_text[-500:]!r}）"
            ) from None

    def read_frames(self, count: int, *, timeout: float) -> list[dict[str, Any]]:
        """连读 `count` 帧（每一帧各自用同一个 `timeout` 兜住）。"""
        return [self.read_frame(timeout=timeout) for _ in range(int(count))]

    def read_until(self, name: str, *, timeout: float) -> dict[str, Any]:
        """读到某个 `event:` 名为止（读到的其它帧丢弃）。"""
        deadline = time.monotonic() + float(timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"{timeout}s 内没有读到 event: {name}")
            frame = self.read_frame(timeout=remaining)
            if frame.get("event") == name:
                return frame

    def read_event_frame(self, *, timeout: float) -> dict[str, Any]:
        """读**第一条带 `event:` 的帧**（`retry:` 与 `: ping` 这两类先跳过）。

        SSE 流开头那一条 `retry:` 是重连间隔的控制指令，它只带一个字段；契约说的
        「第一帧必须是 `snapshot`」指的是**第一条事件帧**。
        """
        deadline = time.monotonic() + float(timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"{timeout}s 内没有读到带 event: 的帧")
            frame = self.read_frame(timeout=remaining)
            if frame.get("event"):
                return frame

    def frames(self) -> list[dict[str, Any]]:
        """已经读到的全部帧（不等待）。"""
        out: list[dict[str, Any]] = []
        while True:
            try:
                out.append(self._frames.get_nowait())
            except queue.Empty:
                return out

    def close(self) -> None:
        self._stop.set()
        try:
            self._stream.__exit__(None, None, None)
        except Exception:  # pragma: no cover
            pass
        try:
            self._client.close()
        except Exception:  # pragma: no cover
            pass

    def __enter__(self) -> "SseClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _resolve_base_url(target: Any) -> str:
    """`base_url` 可以给：字符串 / `ServiceProcess` / Starlette `TestClient`。"""
    if isinstance(target, str):
        return target.rstrip("/")
    for attr in ("base_url",):
        value = getattr(target, attr, None)
        if isinstance(value, str):
            return value.rstrip("/")
        if value is not None and hasattr(value, "scheme"):
            return f"{value.scheme}://{value.netloc}".rstrip("/")
    raise TypeError(f"认不出 base_url：{target!r}")


def read_sse_frames(client: Any, count: int = 1, *, timeout: float) -> list[dict[str, Any]]:
    """按计数连读若干帧（`timeout` 必传）。"""
    if isinstance(client, SseClient):
        return client.read_frames(count, timeout=timeout)
    return [parse_sse_frame(item) for item in _read_via_httpx(client, count, timeout)]


def _read_via_httpx(base_url: str, count: int, timeout: float) -> list[str]:  # pragma: no cover
    """给「直接给一个 URL 字符串」的调用点用的兜底读法。"""
    out: list[str] = []
    buffer = ""
    with httpx.stream("GET", f"{_resolve_base_url(base_url)}/api/events",
                      timeout=httpx.Timeout(timeout, read=timeout), trust_env=False) as response:
        for chunk in response.iter_text():
            buffer += chunk.replace("\r\n", "\n")
            while "\n\n" in buffer:
                raw, buffer = buffer.split("\n\n", 1)
                if raw.strip():
                    out.append(raw)
                    if len(out) >= count:
                        return out
    return out


# ---------------------------------------------------------------------------
# 动作 / 会话文件 / 配置
# ---------------------------------------------------------------------------


def post_action(base_url: Any, action: str, args: dict[str, Any] | None = None, **kwargs: Any):
    """打一次 `POST /api/actions`（**两个必需请求头由这里带全**，测试不自己拼头）。

    走 `tests/_helpers.py` 的 `local_post()`：连接对象按 `(url, trust_env, timeout)` 复用 ——
    本机新建一个 `httpx.Client` 要 0.83 秒，动作请求与状态轮询都不该为它买单。
    """
    body: dict[str, Any] = {"action": action}
    merged_args = dict(args or {})
    # `start` 必须显式带目标工作区目录：调用方没给时由**测试层**的这个放置根预建一个
    # （产品侧没有默认落点，测试也不该悄悄兜在产品里 —— 缺省只为让既有用例少改几行）。
    if action == "start" and merged_args.get("dir") is None:
        sessions_root = getattr(base_url, "sessions_root", None)
        assert sessions_root is not None, (
            "打 start 却没给 args.dir，也没有可用的预建根："
            "请经 service_process 夹具起进程（它给 out_root=），或显式传 dir。"
        )
        merged_args["dir"] = helpers.allocate_start_dir(sessions_root)
    if merged_args:
        body["args"] = merged_args
    headers = {"Content-Type": "application/json", "X-Meeting-Service": "1"}
    headers.update(kwargs.pop("headers", {}) or {})
    url = f"{_resolve_base_url(base_url)}/api/actions"
    timeout = kwargs.pop("timeout", 10.0)
    return helpers.local_post(url, json=body, headers=headers, timeout=timeout, **kwargs)


def dev_inject(
    base_url: Any,
    *,
    source: str,
    fault: str,
    after_seconds: float = 0.0,
    duration_seconds: float | None = None,
    **kwargs: Any,
):
    """`dev_inject` 动作：**透传 `after_seconds` 与 `duration_seconds`**（「注入 → 恢复」可写）。"""
    args: dict[str, Any] = {"source": source, "fault": fault, "after_seconds": after_seconds}
    if duration_seconds is not None:
        args["duration_seconds"] = duration_seconds
    return post_action(base_url, "dev_inject", args, **kwargs)


def read_session_json(session_dir: Path | str) -> dict[str, Any] | None:
    """读一个场次目录里的 `session.json`（读不出来返回 `None`）。"""
    path = Path(session_dir) / "session.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_config_json(home: Path | str, values: dict[str, Any]) -> Path:
    """把「点号扁平键名 → 值」的映射写进 `<家目录>/.config/meeting/config.json`。

    键形与 `settings_update` 的 `args` **逐字同形**（只有一层点号扁平键名，不写嵌套）。
    """
    return helpers.write_config_json(Path(home), values)
