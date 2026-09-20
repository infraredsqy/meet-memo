"""Starlette 应用组装、端口策略、三道防线（phase1 §3.5–§3.8；phase2 §3.12 的搬迁与接线）。

本模块在 phase2 之后的落点（phase2 §3.5.1 的搬迁边界，写死）：

  - `ServiceRuntime` **已经搬到 `meeting/service/runtime.py`**（类名、构造语义、
    `startup()` / `shutdown()` / `to_dict()` 的名字与语义一字不改）；本模块只从那里导入。
  - 工厂入口 `build_runtime()` **仍在本模块**（构造运行时对象 + 解析 `output.root`），
    形态与名字保持不变，`cli.py` 的调用点一个字不动。
  - **常驻本模块的四样**（不搬走、不在别处复制一份）：
    服务器子类 `ServiceServer`（重写 `capture_signals()`）、信号处理函数
    `handle_shutdown_signal`、安装点 `install_shutdown_signal_handlers()`、
    模块级 `_shutdown_target`（`ServiceRuntime.startup()` 在框架 startup 钩子里写它）。
    phase3 只会在这个已有的处理函数**内部**加分支，并把写进 `_shutdown_target` 的对象换成
    `trigger_shutdown`（安装点与处理函数体不变；换协程时必须自行包一层调度）。
  - 端口策略（`bind_http_socket()` / `probe_meeting_service()`，**标准库 socket + 单调时钟
    deadline**，运行时代码不许 import httpx）。
  - 三道本地最低防线（Host 校验 / 动作自定义头 / 不开 CORS）与 `/api/*` 的 `no-store`。
  - **五条路由全部只在 `meeting/service/routes.py` 注册一次**：本模块把路由模块挂上应用，
    自己不留 `/api/state` 与 `/api/heartbeat` 的最小版注册（phase1 的那两条已随本次搬迁摘除）。
"""

from __future__ import annotations

import asyncio
import contextlib
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
from starlette.routing import Route

from meeting import config as config_module, logging_setup
from meeting.service.runtime import ServiceRuntime

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
# 应用组装（注册次序是硬约束：五条路由先注册，静态 / 说明页最后挂）
# ---------------------------------------------------------------------------


def build_runtime(config: Any = None) -> ServiceRuntime:
    """公共工厂：构造 `ServiceRuntime` 对象并把 `output.root` 解析成路径字符串。

    `cli.py` **只经这个函数拿对象与解析后的输出根**，不直接 import `ServiceRuntime` 类。
    这一步**不启动任何异步物**（取事件循环、写关停目标、信号接管自检、解析后端、探测设备、
    扫未收尾场次、打开浏览器全部由框架 startup 钩子触发）。
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


async def _not_found(request: Any) -> Response:
    """末端 404（未匹配到任何路由的请求）。"""
    return Response("Not Found", status_code=404, media_type="text/plain")


def build_app(runtime: ServiceRuntime, config: Any = None) -> Starlette:
    """组装 Starlette 应用（五条路由 + 三道防线 + `/` 的二选一分支）。

    **五条路由（四条 `/api/*` + `GET /debug`）只在 `meeting/service/routes.py` 注册一次**：
    phase1 在装配模块里注册的 `GET /api/state` / `GET /api/heartbeat` 两条最小版已随本次
    搬迁摘除（处理体与状态 JSON 产出函数随 `ServiceRuntime` 进了 `runtime.py`），
    `static.py` 里那份 `/debug` 注册同样已摘除 —— 否则完整版会永远不生效。
    """
    if config is None:
        config = runtime.config
    config_module.set_current(config)

    from meeting.service import routes as routes_module
    from meeting.service import static as static_module

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        # ASGI 层没有 `cli.py` 的 bind 步骤：用回环地址补齐绑定（真实服务由 `cli.py` 先绑好，
        # 那里的 `set_binding()` 因此不会被这里改掉已经绑定的真实端口）。
        runtime.bind_from_scope({"server": ("127.0.0.1", 8765)})
        await runtime.startup()
        try:
            yield
        finally:
            await runtime.shutdown()

    routes: list[Any] = [
        # 五条路由（四条 /api/* + GET /debug）**先注册**：唯一注册点是 service/routes.py
        *routes_module.all_routes(),
    ]
    # 静态 / 说明页的挂载函数**必须最后调用**（否则会把 API 与自检页一起吃走）
    routes.extend(static_module.static_routes(dist_dir_of(config)))
    # 末端 404（保证未知路径拿到 404，而不是 Starlette 的 405 兜底）
    routes.append(
        Route(
            "/{path:path}",
            _not_found,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        )
    )

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.runtime = runtime
    app.add_middleware(_LocalGuardsMiddleware)
    return app


class _LocalGuardsMiddleware:
    """三道本地最低防线 + `/api/*` 的 `Cache-Control: no-store`（纯 ASGI 中间件）。

    1. **Host 校验**：非 `127.0.0.1[:port]` / `localhost[:port]` 的 `Host` 一律 **403**
       （挡 DNS rebinding）。
    2. **动作要自定义头**：`POST /api/actions` 必须带 `Content-Type: application/json`
       **且**带 `X-Meeting-Service: 1`，否则 **403**（挡表单式简单请求）。
       **校验先于路由匹配生效**；phase2 新加的 `GET /api/events` 与 `POST /api/actions`
       都落在这道中间件的覆盖范围内。
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
    "set_server",
    "set_shutdown_target",
]
