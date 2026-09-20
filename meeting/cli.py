"""命令行入口（phase1 §3.3 / §3.4 / R15）。

只认两个词：`meeting web` / `meeting post`（**不加第三个词**）。

五个退出码常量在**本模块**模块级定义（§3.4 冻结表），`main()` 的每个出口一律
`sys.exit(<常量>)`，log 里「退出原因与退出码」那一行也写常量名 —— **本模块里不出现裸数字**
（异常里的 HTTP 状态码之类不算退出码，`probe_meeting_service` 的 200 也不在本文件里）。

`config.py` / `logging_setup.py` **不定义退出码常量**：它们只抛本 Phase 已定的异常
（配置损坏、值级非法、主 log 不可写），由 `main()` 映射成 §3.4 的常量后 `sys.exit`。

配置与日志的时序（§3.5 第 1 条，四步定死、不得对调）：
  ① 先用默认日志目录起 bootstrap 日志 → ② 再依优先级读配置并做值级校验 →
  ③ 再按生效配置重建服务级日志 → ④ 生效 `log.dir` 写不进去即「主 log 不可写」。

服务器信号接管：`install_shutdown_signal_handlers()` 必须在服务器运行入口**之前**调用；
服务器自己的信号捕获由 `meeting.service.app.ServiceServer` 屏蔽。
"""

from __future__ import annotations

import logging
import sys

from meeting import config as config_module
from meeting import logging_setup
from meeting.service import app as service_app

# ---------------------------------------------------------------------------
# 退出码表（§3.4，**本 Phase 冻结**：标识符与取值都是冻结项）
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_REJECTED = 2
EXIT_STARTUP_UNMET = 3
EXIT_INTERRUPTED = 130

ACTIONS = ("web", "post")

USAGE = (
    "用法：meeting <动作词>\n"
    "  可用动作词：web / post\n"
    "    web   起本地服务（只监听 127.0.0.1），前台跑到 Ctrl+C\n"
    "    post  会后流程（本节点还没接入）\n"
)

POST_STUB_MESSAGE = "会后流程还没接入（65dd3e4c）"


def _log() -> logging.Logger:
    return logging_setup.get_logger("cli")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """唯一入口：`web` / `post` 两个词。"""
    args = list(sys.argv[1:] if argv is None else argv)

    if not args:
        print(USAGE, end="", file=sys.stderr)
        print("退出原因与退出码：用法错误（缺少动作词） -> EXIT_REJECTED", file=sys.stderr)
        sys.exit(EXIT_REJECTED)

    action = args[0]
    if action == "web":
        web()
        return
    if action == "post":
        post()
        return

    print(USAGE, end="", file=sys.stderr)
    print(f"未定义的动作词：{action!r}", file=sys.stderr)
    print("退出原因与退出码：用法错误（未定义的动作词） -> EXIT_REJECTED", file=sys.stderr)
    sys.exit(EXIT_REJECTED)


def post() -> None:
    """`meeting post` —— **登记在册的桩**（实现归 `65dd3e4c`，命令面不变）。"""
    print(POST_STUB_MESSAGE)
    try:
        _log().info("%s -> EXIT_FAILURE", POST_STUB_MESSAGE)
    except Exception:  # pragma: no cover - 日志尚未初始化时只打印终端
        pass
    sys.exit(EXIT_FAILURE)


def web() -> None:
    """`meeting web` —— 走 §3.5 的启动序列，前台跑到 `Ctrl+C`。"""
    # ① bootstrap 日志：默认日志目录 + 默认轮转参数
    try:
        logging_setup.setup_service_logging(config_module.default_log_dir())
    except OSError as exc:
        # 默认日志目录不可写时 bootstrap 建不起来：这一刻只有终端可写。
        # 是否拒绝启动由 ④（生效 log.dir）决定。
        print(
            f"注意：默认日志目录不可写（{exc}），bootstrap 阶段只有终端可写",
            file=sys.stderr,
            flush=True,
        )

    # ② 读配置并做值级校验
    try:
        loaded = config_module.load()
    except config_module.ConfigCorruptError as exc:
        print(f"启动前提不成立：{exc}", file=sys.stderr, flush=True)
        try:
            _log().error("启动前提不成立：%s -> EXIT_STARTUP_UNMET", exc)
        except Exception:
            pass
        sys.exit(EXIT_STARTUP_UNMET)
    except config_module.ConfigValueError as exc:
        print(f"配置值非法：{exc}", file=sys.stderr, flush=True)
        try:
            _log().error("配置值非法：%s -> EXIT_REJECTED", exc)
        except Exception:
            pass
        sys.exit(EXIT_REJECTED)

    config_module.set_current(loaded)

    # ③ 按生效配置重建服务级日志（写不进去即「主 log 不可写」）
    log_dir = loaded.get("log.dir")
    try:
        logging_setup.setup_service_logging(
            log_dir,
            level=loaded.get("log.level"),
            max_bytes=loaded.get("log.max_bytes"),
            backup_count=loaded.get("log.backup_count"),
        )
    except OSError as exc:
        # ④ 主 log 不可写 → 拒绝启动（不再照 V1 那样静默降级）
        print(
            f"启动前提不成立：主 log 不可写：{log_dir}（{type(exc).__name__}: {exc}）",
            file=sys.stderr,
            flush=True,
        )
        try:
            _log().error(
                "启动前提不成立：主 log 不可写：%s（%s: %s） -> EXIT_STARTUP_UNMET",
                log_dir, type(exc).__name__, exc,
            )
        except Exception:
            pass
        sys.exit(EXIT_STARTUP_UNMET)

    logger = _log()
    logger.info(
        "配置来源：%s；生效值清单：\n%s",
        loaded.config_path(),
        config_module.format_applied(loaded),
    )
    logger.info(
        "热读键（每段重新读）：%s；其余键改了要重启；环境变量一律启动时求值",
        " / ".join(loaded.hot_keys()),
    )

    runtime = service_app.build_runtime(loaded)

    # ④ 绑 127.0.0.1:<port>（绑定由本服务自己做；被占按两种情形说话）
    try:
        sock = service_app.bind_http_socket(loaded.get("web.port"))
    except service_app.PortUnavailableError as exc:
        print(exc.message, file=sys.stderr, flush=True)
        logger.error("%s -> EXIT_REJECTED", exc.message)
        sys.exit(EXIT_REJECTED)

    actual_host, actual_port = sock.getsockname()[:2]
    runtime.set_binding(actual_host, actual_port)
    url = runtime.url
    logger.info("绑定成功：%s:%s（getsockname 实测）", actual_host, actual_port)

    print(f"URL：{url}", flush=True)
    print(f"输出根：{runtime.out_root}", flush=True)
    print(f"log 路径：{runtime.log_path}", flush=True)

    # ⑥ 装信号处理函数，再把服务器跑起来，前台接收 Ctrl+C
    service_app.install_shutdown_signal_handlers()
    app = service_app.build_app(runtime)
    server = service_app.ServiceServer(service_app.build_server_config(app))
    # 只在这一处把服务器对象交给模块级关停目标（`trigger` 用它置 `should_exit`）；
    # 运行时对象自己不持有服务器引用，于是没有第二处重复写入。
    service_app.set_server(server)

    exit_code = EXIT_OK
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:  # pragma: no cover - 处理函数已接管，这里只是兜底
        exit_code = EXIT_OK
    except Exception:
        logger.exception("服务异常退出")
        exit_code = EXIT_FAILURE
    finally:
        try:
            sock.close()
        except OSError:  # pragma: no cover - 已由服务器接管时重复关闭
            pass

    logger.info("退出原因与退出码：服务已关停 -> %s", _exit_name(exit_code))
    sys.exit(exit_code)


def _exit_name(code: int) -> str:
    """把退出码常量翻回常量名（log 里写常量名，不写裸数字）。"""
    for name in (
        "EXIT_OK",
        "EXIT_FAILURE",
        "EXIT_REJECTED",
        "EXIT_STARTUP_UNMET",
        "EXIT_INTERRUPTED",
    ):
        if globals()[name] == code:
            return name
    return f"未知退出码 {code}"  # pragma: no cover - 五档之外不自造
