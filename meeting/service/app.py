"""Starlette 应用组装、最小 `ServiceRuntime`、端口策略、三道防线、最小版 API（phase1 §3.5–§3.8 / §3.14）。

本模块的落点（phase1 定死，phase2 搬迁运行时类时不许把这些搬走、不许在别处再定义一份）：

  - 最小 `ServiceRuntime`（本 Phase 先住在这里，phase2 搬到 `service/runtime.py`；
    类名、构造语义、`startup()` / `shutdown()` / `to_dict()` 的名字与 `build_runtime()`
    的入口形态一起保持不变）；
  - 服务器子类 `ServiceServer`（`uvicorn.Server` 的子类，重写 `capture_signals()` 返回
    `contextlib.nullcontext()` —— 服务器在整个运行期内**一个信号处理函数都不装**）；
  - 信号处理函数 `handle_shutdown_signal`、安装点 `install_shutdown_signal_handlers()`、
    模块级 `_shutdown_target`（phase3 只替换 `_shutdown_target` 里那个对象，安装点与处理
    函数体不变，不许另起第二个处理函数）；
  - 端口策略（`bind_http_socket()` / `probe_meeting_service()`，**标准库 socket + 单调时钟
    deadline**，运行时代码不许 import httpx）；
  - 三道本地最低防线（Host 校验 / 动作自定义头 / 不开 CORS）与 `/api/*` 的 `no-store`。

`to_dict()` 是**全应用唯一的状态 JSON 产出点**：`GET /api/state` 用它，phase2 的 SSE 首帧
也用它（形状只有一处定义）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from meeting import __version__, config as config_module, logging_setup
from meeting.backends.base import SOURCE_IDS, ReadOnlyConfig
from meeting.backends.null import NullCaptureBackend, NullTranscriberBackend
from meeting.config import ConfigValueError

_log = logging.getLogger("meeting.service")

#: 服务标识字面量（端口探针靠它认亲：`"service": "meeting"`）
SERVICE_ID = "meeting"

#: 三道防线里动作请求必须带的头 / Content-Type
ACTION_HEADER_NAME = "x-meeting-service"
ACTION_HEADER_VALUE = "1"
ACTION_CONTENT_TYPE = "application/json"

#: 端口探针的总超时（秒）：`GET /api/heartbeat` 总超时 1 秒
PROBE_TIMEOUT_SECONDS = 1.0

#: 允许的 Host（回环 + localhost；挡 DNS rebinding）
ALLOWED_HOSTS = ("127.0.0.1", "localhost")

#: 产物缺失说明页文案（冻结项 17）
DIST_MISSING_MESSAGE = "前端产物还没构建。跑 `npm run build`，或先用 `/debug` 自检页看服务状态"

#: 进程启动那一刻（快照 `service.started_at` 的来源）。
#: 取值窗口：这里在**导入时刻**定一次（导入即进程启动那一刻的同义词，供尚未走到
#: `startup()` 的早期日志用它）；`ServiceRuntime.startup()` 里会把它**覆盖成真正的启动
#: 时刻**（框架 startup 钩子跑起来的那一刻）。对外可见的最终取值只有一个 ——
#: `startup()` 覆盖后的那个；本模块的这个模块级常量只是导入期的初值。
PROCESS_STARTED_AT = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 运行时错误
# ---------------------------------------------------------------------------


class PortUnavailableError(RuntimeError):
    """端口被占（两种情形都算：已有 meeting 服务 / 别的程序占着）。

    `message` 是**给终端看的那句话**，由 `cli.py` 打印并以 `EXIT_REJECTED` 结束。
    """

    def __init__(self, message: str, *, occupied_by_meeting: bool) -> None:
        self.message = message
        self.occupied_by_meeting = occupied_by_meeting
        super().__init__(message)


# ---------------------------------------------------------------------------
# 信号接管（常驻本模块：`handle_shutdown_signal` / `install_shutdown_signal_handlers()`
# / `_shutdown_target` / `ServiceServer`）
# ---------------------------------------------------------------------------

#: 关停目标：`(loop, trigger)`；本 Phase 写进去的对象语义 = 「立即请求关停」。
#: phase3 把它替换成 `trigger_shutdown`（先跑收尾、再请求关停），安装点与调用形态不变。
#:
#: **`trigger` 是可被 `call_soon_threadsafe` 直接调用的同步 callable（本 Phase 定死的一条）**：
#: 处理函数体里那一句 `loop.call_soon_threadsafe(trigger)` 把 `trigger` 当**同步可调用对象**
#: 排进循环，循环排到它就调一次、不 await 它的返回值（契约 §3.5 第 6 条的文字把它写成
#: 「普通协程」，但本 Phase 的落地形态与 `call_soon_threadsafe` 的调用形态一致地取同步
#: callable —— 唯一动作是同步地把 `should_exit` 置 `True`，没有需要 await 的步骤）。
#:
#: **phase3 的接缝（写死，免得踩坑）**：phase3 换成 `trigger_shutdown`（普通协程）时，
#: **必须自行包一层调度**，例如 `lambda: loop.create_task(trigger_shutdown())`；
#: 直接把协程对象交给 `call_soon_threadsafe` 的话，它只会被创建、**永远不被 await**，
#: 收尾步骤于是静默失效（不报错、不落 log）。安装点（`ServiceRuntime.startup()` 里写
#: `_shutdown_target`）与处理函数体一律不变。
_shutdown_target: tuple[asyncio.AbstractEventLoop, Callable[[], Any]] | None = None

#: 服务器引用（由 `cli.py` 在运行入口之前写入；`trigger` 用它置 `should_exit`）
_runtime_server: Any = None


def set_shutdown_target(loop: asyncio.AbstractEventLoop, trigger: Callable[[], Any]) -> None:
    """由 `ServiceRuntime.startup()` 在框架生命周期钩子里写入 `(loop, trigger)`。

    `trigger` 必须是**同步 callable**（见 `_shutdown_target` 上的说明：phase3 的
    `trigger_shutdown` 是协程，写进来之前要先包一层 `loop.create_task(...)`）。
    """
    global _shutdown_target
    _shutdown_target = (loop, trigger)


def clear_shutdown_target() -> None:
    global _shutdown_target
    _shutdown_target = None


def make_shutdown_trigger(loop: asyncio.AbstractEventLoop) -> Callable[[], None]:
    """构造本 Phase 的关停目标：唯一动作是把服务器的 `should_exit` 置 `True`。

    返回的是**同步 callable**（可被 `loop.call_soon_threadsafe` 直接调用，见
    `_shutdown_target` 上的说明）；它不返回协程、也不 await 任何东西。

    phase3 会把写进 `_shutdown_target` 的这个对象换成 `trigger_shutdown`
    （先跑收尾、再请求关停）—— 安装点与处理函数体都不变，但**换成协程时必须自行包一层
    调度**（`lambda: loop.create_task(trigger_shutdown())`），否则协程永不被 await。
    """

    def trigger() -> None:
        server = _runtime_server
        if server is not None:
            server.should_exit = True
        else:  # pragma: no cover - 服务器尚未建立
            _log.warning("收到关停请求，但服务器对象还没建立")

    return trigger


def set_server(server: Any) -> None:
    """由 `cli.py` 写入服务器对象（`trigger` 的目标）。"""
    global _runtime_server
    _runtime_server = server


def handle_shutdown_signal(signum: int, frame: Any = None) -> None:
    """SIGINT / SIGBREAK 共用的处理函数：只做两件事。

    ① 用 `meeting` logger 往服务级主 log 落一行（模板 `收到 %s，进入关停`，信号名取
       `signal.Signals(signum).name`，级别 `INFO`）；
    ② `loop.call_soon_threadsafe(trigger)`。

    `_shutdown_target` 为 `None`（服务尚未就绪）时只落上面那行 log 然后返回。
    这一行 log 就是端到端断言所绑的可观察副作用 —— 服务器的处理函数不往 `meeting`
    logger 写任何东西。
    """
    try:
        signame = signal.Signals(signum).name
    except ValueError:  # pragma: no cover - 未知信号号
        signame = str(signum)
    _log.info("收到 %s，进入关停", signame)

    target = _shutdown_target
    if target is None:
        return
    loop, trigger = target
    try:
        loop.call_soon_threadsafe(trigger)
    except RuntimeError:  # pragma: no cover - 事件循环已关（正在关停）
        pass


def install_shutdown_signal_handlers() -> None:
    """在服务器运行入口**之前**装上 SIGINT / SIGBREAK（两个信号挂**同一个**处理函数）。

    Windows 的 ProactorEventLoop 不支持 `add_signal_handler`，所以走 `signal.signal`。
    """
    signal.signal(signal.SIGINT, handle_shutdown_signal)
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signal.signal(sigbreak, handle_shutdown_signal)


def check_signal_handlers() -> bool:
    """运行期自检：SIGINT 与 SIGBREAK 必须指向本服务的同一个处理函数。

    通过落 `信号接管自检通过：SIGINT 与 SIGBREAK 指向本服务的处理函数`；
    不通过落 `信号接管自检失败：<实际读到的对象>`。自检**不新增退出码档位**。
    """
    sigbreak = getattr(signal, "SIGBREAK", None)
    sigint_handler = signal.getsignal(signal.SIGINT)
    sigbreak_handler = signal.getsignal(sigbreak) if sigbreak is not None else None
    if (
        sigbreak is not None
        and sigint_handler is handle_shutdown_signal
        and sigbreak_handler is handle_shutdown_signal
    ):
        _log.info("信号接管自检通过：SIGINT 与 SIGBREAK 指向本服务的处理函数")
        return True
    _log.error(
        "信号接管自检失败：SIGINT -> %r，SIGBREAK -> %r",
        sigint_handler,
        sigbreak_handler,
    )
    return False


class ServiceServer(uvicorn.Server):
    """服务器子类：**屏蔽服务器自己的信号捕获**。

    否则运行期内生效的会是服务器那个处理函数，本服务的处理函数与它那行 log 都不出现，
    退出码也会落到服务器还原信号后的默认处置上。

    phase3 只往 `handle_shutdown_signal` 里加语义，**不许恢复这里的服务器信号捕获**。
    """

    def capture_signals(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()


# ---------------------------------------------------------------------------
# 端口策略（§3.6）
# ---------------------------------------------------------------------------


def probe_meeting_service(
    host: str, port: int, *, timeout: float = PROBE_TIMEOUT_SECONDS
) -> bool:
    """探测 `host:port` 上是不是**本服务的另一个实例**。

    实现定死一种：标准库 `socket` + 单调时钟 deadline（**不用 `urllib.request`**，
    它的 timeout 只覆盖单次 socket 操作，给不出「总超时」的保证）。

    进探针时记 `deadline = monotonic() + timeout`，**先按 deadline 的剩余值把 socket
    超时设好、再发起连接**（首次连接也必须落在 deadline 之内）；此后每次收发前都把
    socket 超时设成剩余值，剩余值 ≤ 0 立即返回 `False`。

    超时、连接被拒、状态码非 200、body 里没有 `"service": "meeting"`，**一律按「别的程序
    占着」处理**（返回 `False`）。不猜、不重试、不换端口。
    """
    deadline = time.monotonic() + float(timeout)
    request = (
        f"GET /api/heartbeat HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("ascii")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        sock.settimeout(remaining)
        try:
            sock.connect((host, port))
        except OSError:
            return False

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        sock.settimeout(remaining)
        try:
            sock.sendall(request)
        except OSError:
            return False

        chunks: list[bytes] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(4096)
            except OSError:
                return False
            if not chunk:
                break
            chunks.append(chunk)
            body = b"".join(chunks)
            if b'"service": "meeting"' in body or b'"service":"meeting"' in body:
                break

        raw = b"".join(chunks)
        if not raw:
            return False
        text = raw.decode("utf-8", errors="replace")
        status_line = text.split("\r\n", 1)[0]
        if " 200" not in status_line:
            return False
        return '"service": "meeting"' in text or '"service":"meeting"' in text
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def _listen_on_loopback(port: int, backlog: int) -> socket.socket:
    """建 `AF_INET` / `SOCK_STREAM` 监听 socket → `bind(("127.0.0.1", port))` → `listen()`。

    **只监听 `127.0.0.1`，这一条不可配**（`web.host` 这类键不许出现）。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(backlog)
    except BaseException:
        with contextlib.suppress(OSError):
            sock.close()
        raise
    return sock


def bind_http_socket(
    port: int | None = None,
    *,
    backlog: int = 2048,
    probe: Callable[[str, int], bool] | None = None,
) -> socket.socket:
    """绑 `127.0.0.1:<port>` 并返回已 `listen()` 的监听 socket。

    探测顺序定死：**先尝试绑**；绑定抛 `OSError` 才去探测占用者形状。
    `port = 0` 时不做占用探测（系统挑端口）。
    端口被占 → 抛 `PortUnavailableError`（两处文案不同，见 §3.6）。
    """
    if port is None:
        port = int(config_module.get("web.port"))
    port = int(port)

    try:
        return _listen_on_loopback(port, backlog)
    except OSError as exc:
        if port == 0:
            raise
        _log.info("端口 %s 绑不上（%s），转去探测占用者形状", port, exc)

    is_meeting = False
    probe_fn = probe_meeting_service if probe is None else probe
    is_meeting = bool(probe_fn("127.0.0.1", port))

    if is_meeting:
        raise PortUnavailableError(
            f"已经有一个 meeting 服务在跑：http://127.0.0.1:{port}（当前空闲）",
            occupied_by_meeting=True,
        )
    raise PortUnavailableError(
        f"端口 {port} 被别的程序占着，没法起服务。"
        f"换一个端口再来，例如：MEETING_PORT=8766 meeting web",
        occupied_by_meeting=False,
    )


# ---------------------------------------------------------------------------
# 最小 `ServiceRuntime`（§3.14；phase2 搬到 `service/runtime.py`）
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_session_record(entry: Path) -> tuple[str | None, Any, str | None]:
    """读一个场次目录的 `session.json`，返回 `(state, interrupted, 出错说明)`。

    **所有读不出来 / 解不出来的情形一律按「这一场未命中」处理**（契约 §3.5 第 2 条：
    扫描不许拖垮启动；冻结项 22 / 28）：异常**绝不许冒到启动序列**。两类情形的**条数口径
    不同**（契约 §3.5 第 2 条 / 冻结项 22、28，这一处区别是定死的）：
      - **`session.json` 根本不存在**（`FileNotFoundError`）→ 与「输出根下没有场次目录」
        同口径：**算未命中、且不落任何 WARNING**，返回的出错说明因此是 `None`；
      - **文件存在但读不出来 / 解码失败 / 顶层不是对象**（`OSError` 里除
        `FileNotFoundError` 之外的那些、`UnicodeDecodeError`、`json.JSONDecodeError`、
        顶层类型不符）→ 同样按未命中处理，但**必须留下一行「读不出来…按未命中处理」的
        WARNING**（M4 的要求，不许回退成静默），出错说明因此非 `None`。
    覆盖面（别只加 `json.JSONDecodeError` 就完）：`UnicodeDecodeError` 属于 `ValueError`
    这一族，只盯着 `JSONDecodeError` 会漏掉解码失败那一路。
    """
    session_file = entry / "session.json"
    try:
        with open(session_file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        # 场次目录里没有 session.json：与「输出根下没有场次目录」同口径的未命中，不落任何 log
        return None, None, None
    except (OSError, ValueError) as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return None, None, "session.json 顶层不是 JSON 对象"
    return data.get("state"), data.get("interrupted"), None


def scan_unfinished_sessions(out_root: str) -> str | None:
    """按方案 §5.5 的「**只取最新的一场**」扫输出根；命中即返回一行 `WARNING` 的内容。

    口径定死：枚举输出根下的**场次目录**，按目录名的**字典序取最大者**为最新一场
    （方案 §5.1 的 `YYMMDD` 与 `YYMMDD-N` 命名下字典序与时间序一致），**只看这一场**的
    `session.json`；命中 `state ∈ {recording, draining}` 或 `interrupted: true` 时返回
    warning 文案，否则返回 `None`。

    **不自动开始录音、不改写那份记录、不建目录**；输出根不存在时结果就是「没有未收尾
    场次」，也不趁机建目录；更早的未收尾场次不产生第二行。

    **条数口径（契约 §3.5 第 2 条 / 冻结项 22、28）**：这里返回的那行「发现未收尾场次」
    `WARNING` 每次启动最多一行、未命中时不出现；最新一场的 `session.json` **根本不存在**
    时同样是「未命中」，与「输出根下没有场次目录」同口径 —— **一行 log 都不落**。
    文件存在却读不出来 / 解码失败 / 顶层不是对象时，`_read_session_record` 给出的出错说明
    非 `None`，这里照旧落一行「读不出来…按未命中处理」的 `WARNING` 并返回 `None`
    （服务照常启动，这是 M4 的要求）。
    """
    root = Path(out_root)
    if not root.is_dir():
        return None
    try:
        entries = [item for item in root.iterdir() if item.is_dir()]
    except OSError:  # pragma: no cover - 输出根不可读时视为没有未收尾场次
        return None
    if not entries:
        return None

    latest = max(entries, key=lambda item: item.name)
    state, interrupted, error = _read_session_record(latest)
    if error is not None:
        _log.warning(
            "启动扫描：最新一场 %s 的 session.json 读不出来（%s），按未命中处理",
            latest,
            error,
        )
        return None
    if state in ("recording", "draining") or interrupted is True:
        return (
            f"启动扫描：发现未收尾的场次 {latest}"
            f"（扫到的 state={state!r}，interrupted={interrupted!r}）"
            "；本 Phase 不自动录音、不改写记录、不建目录"
        )
    return None


class ServiceRuntime:
    """最小运行时：持有空闲态快照的唯一一份状态，只在事件循环里改，不加锁。

    本 Phase 的语义：四态恒 `idle`、`session = null`、无场次目录、无 SSE、无动作端点。
    **不启线程、不用 `threading.Event` 记状态、不起后台轮询循环。**
    """

    def __init__(self, out_root: str, config: Any) -> None:
        self.output_root = str(out_root)
        self.config = config

        # `service` 六个字段（重建生效日志、取到实际监听地址之后才齐）
        # `started_at` 先取导入期初值，`startup()` 里覆盖成真正的启动时刻（对外只见后一个）
        self.started_at: str = PROCESS_STARTED_AT
        self.bound_host: str | None = None
        self.bound_port: int | None = None

        # 空闲态：四态恒 idle
        self.top_state = "idle"
        self.revision = 1

        # 设备探测结果（只进 log 与 /debug，**不改变快照 sources 的空形态语义**）
        self.probes: list[Any] = []
        self.capture_backend: Any = None
        self.transcriber_backend: Any = None

        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._signal_ok = False

    # -- 绑定地址 ---------------------------------------------------------

    def set_binding(self, host: str, port: int) -> None:
        """从监听 socket 的 `getsockname()` 读回值写入（`port` 不许另外猜）。"""
        self.bound_host = str(host)
        self.bound_port = int(port)

    @property
    def url(self) -> str:
        """由**监听 socket 的 `getsockname()` 读回值**拼出的 URL（绑定前取不到）。"""
        if self.bound_port is None:
            raise ConfigValueError(
                "service.url",
                None,
                "先绑监听 socket 并调 set_binding()，再取 URL"
                "（绑定前这个值取不到，不许拿配置值或字面量顶替）",
                "service.port / service.url 只能由监听 socket 的 getsockname() 产生",
            )
        return f"http://{self.bound_host}:{self.bound_port}"

    @property
    def log_path(self) -> str:
        return logging_setup.service_log_path()

    @property
    def out_root(self) -> str:
        return self.output_root

    def _value(self, key: str) -> Any:
        if self.config is None:
            return config_module.default_for(key)
        return self.config.get(key)

    # -- 生命周期（由框架 lifespan 的 startup 钩子触发） -------------------

    async def startup(self) -> None:
        """框架生命周期钩子里的启动动作（**同步序列里取不到事件循环**）。

        依次：取事件循环 → 写关停目标（`_shutdown_target`，语义 = 立即请求关停）→
        信号接管自检 → 探测设备 → 扫未收尾场次（只取最新的一场，命中落一行 `WARNING`）→
        **打开浏览器**（契约 §1.1 / §3.5 第 5 条：这一步的落点在框架 startup 钩子的**尾部**，
        此刻运行时对象已就绪、`service.url` 已有值、监听 socket 已在 `listen()` 上，
        因此它排在服务器运行入口之后、不进 `cli.py` 的同步序列）。

        **打开浏览器这一步失败只警告、不阻塞启动**：`launch_browser()` 自己已把
        `webbrowser.open` 的失败收成警告；这里再兜一层，任何意外都不许把启动钩子带崩
        （钩子抛异常的后果是应用起不来，与「只警告」相反）。
        """
        if self._started:
            return
        self._started = True
        self.started_at = _now_iso()

        # ① 取事件循环（`asyncio.get_running_loop()` 只有在这里才成立）
        self._loop = asyncio.get_running_loop()

        # ② 写关停目标：语义 = 「立即请求关停」
        set_shutdown_target(self._loop, make_shutdown_trigger(self._loop))

        # ③ 信号接管自检（服务器的信号捕获已被 ServiceServer.capture_signals() 屏蔽）
        self._signal_ok = check_signal_handlers()

        # ④ 探测设备（默认 null 后端如实报「未探测」）
        self._probe_devices()

        # ⑤ 扫未收尾场次：最新一场命中时往服务级主 log 落一行 WARNING（每次启动最多一行）
        warning = scan_unfinished_sessions(self.output_root)
        if warning:
            _log.warning(warning)

        # ⑥ 打开浏览器（尾部；`MEETING_NO_BROWSER=1` 时跳过并落一行日志）
        self._open_browser()

    def _open_browser(self) -> None:
        """打开浏览器这一步：故障一律收成警告，**不阻塞启动**。"""
        try:
            launch_browser(self.url)
        except BaseException as exc:  # 兜底：这一步的任何意外都不许带崩启动钩子
            _log.warning("打开浏览器这一步出意外（只警告、不阻塞）：%s: %s", type(exc).__name__, exc)

    async def shutdown(self) -> None:
        """框架生命周期钩子里的关停动作（本 Phase 没有异步物要收，只清关停目标）。"""
        clear_shutdown_target()

    # -- 设备探测 ---------------------------------------------------------

    def _probe_devices(self) -> None:
        """调 capture 后端的 `probe()`；结果只写 log 与 `/debug`，不改快照语义。"""
        backend = resolve_backend("capture.backend", NullCaptureBackend)
        self.capture_backend = backend
        self.transcriber_backend = resolve_backend(
            "transcriber.backend", NullTranscriberBackend
        )

        values = self.config.as_dict() if self.config is not None else config_module.as_dict()
        view = ReadOnlyConfig(values)
        loop = self._loop

        def emit(fact: Any) -> None:
            # 事实出口可从任何线程调：这里包一层线程安全投递
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(_log.debug, "采集事实：%r", fact)
            else:
                _log.debug("采集事实：%r", fact)

        try:
            backend.bind(emit, config=view)
        except Exception:
            _log.exception("采集后端 bind() 失败（如实记账，不静默）")

        try:
            probes = backend.probe()
        except Exception:
            _log.exception("采集后端 probe() 失败（如实记账，不静默）")
            probes = []
        self.probes = list(probes or [])

        if not self.probes:
            _log.info("设备探测：没有拿到任何一路的探测结果")
        for probe in self.probes:
            _log.info(
                "设备探测：%s -> status=%s device=%s（%s）",
                getattr(probe, "source", "?"),
                getattr(probe, "status", "?"),
                getattr(probe, "device_name", None),
                getattr(probe, "detail", ""),
            )

    # -- 状态 JSON 产出（唯一产出者） -------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """产出 `GET /api/state` 与（phase2 的）SSE 首帧共用的那份快照。

        顶层键集与字段名按方案 §3.2 冻结；本 Phase 的取值是空形态。

        **未装配即取快照这件事在实现上不可静默发生**（契约 §3.5 第 4 条）：`service.port` /
        `service.url` 只能由监听 socket 的 `getsockname()` 读回值产生，所以这里在拿到绑定
        端口之前直接抛 `ConfigValueError`（`RuntimeConfigError` 曾是这个位置的专用异常类，
        它没有契约依据与调用点，已删除；这里改抛配置层的值级非法错误 —— 语义同属
        「运行时对象还没装配好就取快照」这一类实现不合格）。
        """
        if self.bound_port is None:
            raise ConfigValueError(
                "service.port",
                None,
                "先绑监听 socket 并调 set_binding()，再取快照"
                "（框架 startup 钩子跑起来之前取快照一律视为实现不合格）",
                "service.port / service.url 不落配置值、不落 0、不落字面量拼接",
            )
        return {
            "revision": int(self.revision),
            "service": {
                "started_at": self.started_at,
                "version": self._version(),
                "port": self.bound_port,
                # 上面的守卫已保证绑定端口在位，所以 `url` 取得到真实值（不是配置值 / 字面量）
                "url": self.url,
                "log_path": self.log_path,
                "out_root": self.out_root,
            },
            "top_state": self.top_state,
            "session": None,
            "sources": [self._empty_source(source) for source in SOURCE_IDS],
            "alerts": {"critical": None, "hints": []},
            "transcript": {
                "health": "normal",
                "last_line_at": None,
                "buffered_lines": 0,
                "failed_segments": 0,
            },
            "ended": None,
            "settings": {
                "alarm_sound": bool(self._value("alarm.sound")),
                "heartbeat_interval_seconds": int(self._value("heartbeat.interval_seconds")),
                "asr": {},
                "devices": {},
            },
            "notice": None,
        }

    def heartbeat_dict(self) -> dict[str, Any]:
        """`GET /api/heartbeat` 的应答（两个消费方：端口探测 + 界面判迟到 / 失联）。"""
        return {
            "service": SERVICE_ID,
            "server_time": _now_iso(),
            "revision": int(self.revision),
            "top_state": self.top_state,
            "recording_alive": None,
            "seconds_since_last_tick": None,
        }

    @staticmethod
    def _version() -> str:
        return __version__ if isinstance(__version__, str) and __version__ else "0.0.0"

    @staticmethod
    def _empty_source(source: str) -> dict[str, Any]:
        return {
            "id": source,
            "label": "本地" if source == "mic" else "远端",
            "device_name": None,
            "health": "unprobed",
            "lost_since": None,
            "restored": False,
            "level_dbfs": None,
            "peak_dbfs": None,
            "envelope": [],
        }


# ---------------------------------------------------------------------------
# 后端选取的最小回退路径（§3.9；`registry.py` 本体归 phase2）
# ---------------------------------------------------------------------------

_BACKEND_DEFAULT_PATHS = {
    "capture.backend": "meeting.backends.capture:CaptureBackend",
    "transcriber.backend": "meeting.backends.transcribe:TranscriberBackend",
    "post.hook": "meeting.backends.post:PostProcessHook",
}


def _resolve_backend_class(target: str) -> Any:
    """按约定路径 / `module:Class` 形状解析后端类。"""
    if ":" not in target:
        raise LookupError(f"取值不是 module:Class 形状：{target!r}")
    module_name, _, attr = target.partition(":")
    if not module_name or not attr:
        raise LookupError(f"取值不是 module:Class 形状：{target!r}")
    import importlib

    module = importlib.import_module(module_name)
    if not hasattr(module, attr):
        raise LookupError(f"{module_name} 里没有 {attr}")
    return getattr(module, attr)


def resolve_backend(key: str, null_class: type) -> Any:
    """按 `capture.backend` / `transcriber.backend` 解析后端；失败回退 null + warning。

    本 Phase 的最小路径：`null` 与 `auto` 两条要走通；`dev` 在本 Phase 会落到
    「找不到 → 回退 null + warning」（`dev.py` 归 phase2）。
    """
    selector = config_module.get(key)
    if not isinstance(selector, str) or not selector.strip():
        selector = "auto"
    selector = selector.strip()

    if selector == "null":
        return null_class()

    target = _BACKEND_DEFAULT_PATHS[key] if selector == "auto" else selector
    try:
        cls = _resolve_backend_class(target)
    except Exception as exc:
        _log.warning(
            "%s = %r 解析失败（%s: %s），回退 null 替身",
            key, selector, type(exc).__name__, exc,
        )
        return null_class()

    if not callable(cls):
        _log.warning("%s = %r 解析出的对象不可调用，回退 null 替身", key, selector)
        return null_class()
    try:
        return cls()  # 类无参实例化：后端构造函数不许有必填参数
    except Exception as exc:
        _log.warning(
            "%s = %r 无参实例化失败（%s: %s），回退 null 替身",
            key, selector, type(exc).__name__, exc,
        )
        return null_class()


# ---------------------------------------------------------------------------
# 应用组装（注册次序是硬约束：API 与 /debug 先注册，静态 / 说明页最后挂）
# ---------------------------------------------------------------------------

#: 当前应用的运行时对象（路由处理器从这里读状态）
_app_runtime: ServiceRuntime | None = None


def build_runtime(config: Any = None) -> ServiceRuntime:
    """公共工厂：构造 `ServiceRuntime` 对象并把 `output.root` 解析成路径字符串。

    `cli.py` **只经这个函数拿对象与解析后的输出根**，不直接 import `ServiceRuntime` 类。
    这一步**不启动任何异步物**（取事件循环、写关停目标、信号接管自检、扫未收尾场次
    全部由框架 startup 钩子触发）。
    """
    if config is None:
        config = config_module.current() or config_module.load()
    config_module.set_current(config)
    out_root = str(Path(str(config.get("output.root"))))
    return ServiceRuntime(out_root, config)


def dist_dir_of(config: Any) -> str:
    """`web.dist_dir`（默认 `web/dist`）；相对路径按当前工作目录解析。"""
    value = str(config.get("web.dist_dir"))
    return value if os.path.isabs(value) else os.path.abspath(value)


async def _api_state(request: Request) -> Response:
    """`GET /api/state`：body 就是 `to_dict()` 的产出。"""
    runtime = _app_runtime
    if runtime is None:  # pragma: no cover - 应用未装配
        return JSONResponse({"error": "runtime 未装配"}, status_code=500)
    return JSONResponse(runtime.to_dict())


async def _api_heartbeat(request: Request) -> Response:
    """`GET /api/heartbeat` 最小版：`service` 字面量必须有（端口探针靠它认亲）。

    **未装配时的兜底要自洽**（契约 §3.14.1 的 `service` 字段必须存在且为字面量 `"meeting"`，
    §3.6 的探针判据是「状态码 200 + body 含 `"service": "meeting"`」）：这里回 **200 + 完整
    心跳字段**（`recording_alive` / `seconds_since_last_tick` 取 `null`，`top_state` 取空闲态
    `idle`），字段集合与 `ServiceRuntime.heartbeat_dict()` 一致。

    落地选型的理由：`build_app()` 必然先拿到 `runtime` 才会注册路由，所以这一支在正常路径上
    走不到；真走到时，回 200 的完整字段让探针读到的是「这里有一个 meeting 形状的服务」，
    与「路由已注册、服务只是还没进 startup 钩子」这件事一致 —— 回 500 则会让探针把这台
    自己的服务判成「别的程序占着」，与「探针只看状态码 200 + `service` 字面量」的判据冲突。
    """
    runtime = _app_runtime
    if runtime is None:  # pragma: no cover - 应用未装配
        return JSONResponse(
            {
                "service": SERVICE_ID,
                "server_time": _now_iso(),
                "revision": 0,
                "top_state": "idle",
                "recording_alive": None,
                "seconds_since_last_tick": None,
            }
        )
    return JSONResponse(runtime.heartbeat_dict())


async def _not_found(request: Request) -> Response:
    """末端 404：`POST /api/actions` 的端点本体归 phase2，本 Phase 对「路由还没有」定死 404。"""
    return Response("Not Found", status_code=404, media_type="text/plain")


def build_app(runtime: ServiceRuntime, config: Any = None) -> Starlette:
    """组装 Starlette 应用（三道防线 + 最小版 API + `/debug` + `/` 的二选一分支）。"""
    global _app_runtime
    _app_runtime = runtime
    if config is None:
        config = runtime.config
    config_module.set_current(config)

    from meeting.service import static as static_module

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        await runtime.startup()
        try:
            yield
        finally:
            await runtime.shutdown()

    routes: list[Any] = [
        # API 与自检页**先注册**
        Route("/api/state", _api_state, methods=["GET"]),
        Route("/api/heartbeat", _api_heartbeat, methods=["GET"]),
    ]
    routes.extend(static_module.debug_routes())
    # 静态 / 说明页的挂载函数**必须最后调用**（否则会把 API 与自检页一起吃走）
    routes.extend(static_module.static_routes(dist_dir_of(config)))
    # 末端 404（保证「头齐的动作请求」拿到 404，而不是 Starlette 的 405 兜底）
    routes.append(
        Route(
            "/{path:path}",
            _not_found,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        )
    )

    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(_LocalGuardsMiddleware)
    return app


class _LocalGuardsMiddleware:
    """三道本地最低防线 + `/api/*` 的 `Cache-Control: no-store`（纯 ASGI 中间件）。

    1. **Host 校验**：非 `127.0.0.1[:port]` / `localhost[:port]` 的 `Host` 一律 **403**
       （挡 DNS rebinding）。
    2. **动作要自定义头**：`POST /api/actions` 必须带 `Content-Type: application/json`
       **且**带 `X-Meeting-Service: 1`，否则 **403**（挡表单式简单请求）。
       **校验先于路由匹配生效**；头齐的完整请求在本 Phase 一律回 `404`（端点本体归 phase2）。
    3. **不开 CORS**：**不发** `Access-Control-Allow-Origin`。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }

        # ① Host 校验
        if not self._host_allowed(headers.get("host", "")):
            await self._plain(send, 403, "Forbidden: Host 校验不通过（只接受回环地址）")
            return

        # ② 动作自定义头（校验先于路由匹配）
        if path == "/api/actions" and scope.get("method", "").upper() == "POST":
            content_type = headers.get("content-type", "")
            if (
                headers.get(ACTION_HEADER_NAME, "").strip() != ACTION_HEADER_VALUE
                or not content_type.lower().startswith(ACTION_CONTENT_TYPE)
            ):
                await self._plain(
                    send,
                    403,
                    "Forbidden: /api/actions 需要 Content-Type: application/json "
                    "与 X-Meeting-Service: 1",
                )
                return

        # ③ `/api/*` 一律 Cache-Control: no-store（不发 CORS 头）
        if path.startswith("/api/"):
            async def send_wrapper(message: dict) -> None:
                if message.get("type") == "http.response.start":
                    raw = [
                        (key, value)
                        for key, value in message.get("headers", [])
                        if key.decode("latin-1").lower() != "cache-control"
                    ]
                    raw.append((b"cache-control", b"no-store"))
                    message = dict(message, headers=raw)
                await send(message)

            await self.app(scope, receive, send_wrapper)
            return

        await self.app(scope, receive, send)

    @staticmethod
    def _host_allowed(host_header: str) -> bool:
        hostname = host_header.strip()
        if hostname.startswith("["):  # IPv6 字面量
            hostname = hostname.split("]", 1)[0].lstrip("[")
        elif ":" in hostname:
            hostname = hostname.split(":", 1)[0]
        return hostname.lower() in ALLOWED_HOSTS

    @staticmethod
    async def _plain(send: Any, status: int, text: str) -> None:
        body = text.encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def build_server_config(app: Any = None) -> uvicorn.Config:
    """服务器的配置：不自己绑端口（socket 由本服务绑好交给 `run(sockets=[...])`）。

    `app` 必须给：uvicorn 的 `Config(app=None)` 会让运行入口拿到一个 `None` ASGI 应用。
    """
    return uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=0,
        log_config=None,
        access_log=False,
    )


def launch_browser(url: str) -> bool:
    """打开浏览器；失败**只警告、不阻塞**。`MEETING_NO_BROWSER=1` 关掉。

    **调用点在 `ServiceRuntime.startup()` 的尾部**（契约 §1.1 / §3.5 第 5 条）：此刻运行时
    对象已就绪、`service.url` 已有值、监听 socket 已在 `listen()` 上，第 4 步那个 socket 的
    backlog 会接住这个连接。这一步排在服务器运行入口之后，**不进 `cli.py` 的同步序列**。
    `url` 由 `ServiceRuntime.url` 从监听 socket 的 `getsockname()` 读回值产生。

    返回值只作观测用（`True` = 真的开上了）；**调用方一律不看返回值决定是否继续**。
    """
    if os.environ.get(config_module.ENV_NO_BROWSER, "").strip() == "1":
        _log.info("MEETING_NO_BROWSER=1，跳过打开浏览器")
        return False
    try:
        opened = webbrowser.open(url)
    except BaseException as exc:  # 这一步的任何意外都只警告（含 webbrowser 内部的怪异常）
        _log.warning("打开浏览器失败（只警告、不阻塞）：%s: %s", type(exc).__name__, exc)
        return False
    if not opened:
        _log.warning("打开浏览器没成功（只警告、不阻塞）：%s", url)
        return False
    # 成功这一支也必须留痕（契约 §3.13「必须落 log 的事件」含启动与选中的 URL；
    # §1.1 / §6 的「不许静默」）：三支各自一行 —— 跳过、失败、成功。
    _log.info("打开浏览器成功：%s", url)
    return True


__all__ = [
    "ALLOWED_HOSTS",
    "DIST_MISSING_MESSAGE",
    "PROBE_TIMEOUT_SECONDS",
    "SERVICE_ID",
    "PortUnavailableError",
    "ServiceRuntime",
    "ServiceServer",
    "bind_http_socket",
    "build_app",
    "build_runtime",
    "build_server_config",
    "check_signal_handlers",
    "clear_shutdown_target",
    "dist_dir_of",
    "handle_shutdown_signal",
    "install_shutdown_signal_handlers",
    "launch_browser",
    "make_shutdown_trigger",
    "scan_unfinished_sessions",
    "set_server",
    "set_shutdown_target",
]
