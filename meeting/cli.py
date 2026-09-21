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

#: 收尾失败档退出时往终端倒的服务 log 行数（§5.4 行 256 的「最后几行」）
LOG_TAIL_LINES_ON_FAILURE = 20


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
    """`meeting web` —— 走 §3.5 的启动序列，前台跑到 `Ctrl+C`。

    **退出码有五条出口，全都显式 `sys.exit(<常量>)`**（§3.5「不许让异常冒到 uvicorn /
    解释器那里变成解释器默认码」）：启动前提不成立 → `EXIT_STARTUP_UNMET`；配置值非法 /
    端口被占 / 用法错误 → `EXIT_REJECTED`；**起步这一路（组装应用、构造服务器对象）的
    意外异常 → `EXIT_FAILURE`**（同一档另落一行 error 与终端说明，不静默）；进到服务器
    主循环之后的退出码由 `_drain_exit_code()` 按本次关停落在哪一档挑。
    """
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
    # 终端上这三样同样要落 log（§6.3 行 304 的「启动与选中的端口 / URL / 输出根」：
    # 没在终端前面守着的人靠 log 复核时，这三行就是他要找的东西）
    logger.info(
        "启动就绪：URL：%s；输出根：%s；log 路径：%s", url, runtime.out_root, runtime.log_path
    )

    # ⑥ 装信号处理函数，再把服务器跑起来，前台接收 Ctrl+C
    service_app.install_shutdown_signal_handlers()

    exit_code = EXIT_OK
    try:
        app = service_app.build_app(runtime)
        server = service_app.ServiceServer(service_app.build_server_config(app))
        # 只在这一处把服务器对象交给模块级关停目标（`trigger` 用它置 `should_exit`）；
        # 运行时对象自己不持有服务器引用，于是没有第二处重复写入。
        service_app.set_server(server)
        try:
            server.run(sockets=[sock])
        except KeyboardInterrupt:  # pragma: no cover - 处理函数已接管，这里只是兜底
            exit_code = EXIT_OK
        except Exception:
            logger.exception("服务异常退出")
            exit_code = EXIT_FAILURE
    except Exception:
        # **`server.run()` 这一路之外的意外**（组装应用、构造服务器对象这些起步步骤）：
        # 一律按收尾失败档的同一个常量收场（§3.5「不许让异常冒到 uvicorn / 解释器那里变成
        # 解释器默认码」+ §8「不许静默」）。这些起步步骤还没进服务器主循环，因此没有
        # 「等收尾落定」可做，但它们**同样是一条退出路径**：落一行 error、给出可复制的命令
        # 与 log 路径，然后以 `EXIT_FAILURE` 结束，绝不静默退回 `EXIT_OK`。
        logger.exception("服务起步失败（组装应用 / 构造服务器对象的这一路）")
        exit_code = EXIT_FAILURE
        print("服务起步失败：这一趟没能进到服务器主循环。", file=sys.stderr, flush=True)
        print(f"log 路径：{runtime.log_path}", file=sys.stderr, flush=True)
        print(
            "出路：看清上面那条异常与 log 后再起一次；"
            "收尾现场若留在盘上，可用 `meeting post <场次目录>` 收干净。",
            file=sys.stderr,
            flush=True,
        )
    finally:
        try:
            sock.close()
        except OSError:  # pragma: no cover - 已由服务器接管时重复关闭
            pass
        # 退出码按「本次关停落在哪一档」挑（强制打断档 / 收尾失败档 / 空闲退出档取前两者
        # 之外的一切）；挑完之后立刻把收尾失败档的现场打到终端 —— 这一步必须留在
        # `finally` 里：`server.run()` 内部任何「关停途中又抛异常」的情形都要经由这里说话。
        exit_code = _drain_exit_code(runtime, fallback=exit_code)
        _report_drain_outcome(runtime, logger, exit_code)

    logger.info("退出原因与退出码：服务已关停 -> %s", _exit_name(exit_code))
    sys.exit(exit_code)


def _drain_exit_code(runtime, *, fallback: int = EXIT_OK) -> int:
    """按本次关停落在哪一档挑退出码（**五档都是 `cli.py` 模块级的常量标识符**）。

    - **强制打断档**（第二次 `Ctrl+C`，含「已停在 `draining` 再按」）：本次关停被标成
      「强制打断」时一律 `EXIT_INTERRUPTED`；
    - **收尾失败档**：收尾有步骤 `failed` → `EXIT_FAILURE`（阶段 §3.2 的失败终态）；
    - **空闲退出档 / 收尾干净档**：其余情形 → `EXIT_OK`（两档同为 `EXIT_OK`）。

    **`fallback` 的语义（写死、与实现一致）**：它带的是**另一路**信息 ——「服务自己有没有
    出事」（`server.run()` 抛异常时 `cli` 的 `finally` 把 `EXIT_FAILURE` 传进来）。这一路与
    上面三档**正交**：收尾干净只说明「关停落在哪一档」，它判不出「服务是不是异常退出」。
    因此 `fallback` **原样沿用**：`server.run()` 抛异常 + 收尾干净这个组合的退出码是
    `EXIT_FAILURE`，不被收尾干净那一格抹成 `EXIT_OK`；服务正常收场时 `fallback` 就是
    `EXIT_OK`，这一档因此也不改变上面三档的取值。

    这条语义在 `tests/test_shutdown_signal.py` 的
    `test_drain_exit_code_matrix_including_the_server_crash_fallback` 里逐档写死。
    **退出码这一档的合并到此为止**：终端文案不跟着这条合并走 —— `_report_drain_outcome()`
    按**现场有没有 `failed` 步**挑话（见 `_drain_reported_failed()`），退出码只决定「这一档
    该不该看现场」。
    """
    if getattr(runtime, "shutdown_interrupted", False):
        return EXIT_INTERRUPTED
    ended = getattr(runtime.snapshot, "ended", None)
    if ended is not None and any(step.status == "failed" for step in ended.steps):
        return EXIT_FAILURE
    # 收尾干净 / 空闲态：沿用 `server.run()` 那一侧的兜底（正常收场时它就是 `EXIT_OK`，
    # 异常退出时它是 `EXIT_FAILURE`）。上面两步已经把三档判完，这一行就是它们的落脚点；
    # 兜底值在这里原样交出，收尾干净这一格因此抹不掉服务异常退出那一档。
    return fallback


def _drain_reported_failed(runtime) -> bool:
    """**现场档位**的判据：收尾里有没有步骤 `failed`（§3.4 第 1 条 / §3.5）。

    只看现场，不看退出码：`EXIT_FAILURE` 这一档里压着**两件正交的事** ——「收尾有步骤
    `failed`」与「`server.run()` 自己抛了异常」（后者由 `web()` 的 `finally` 经
    `fallback` 交出来，见 `_drain_exit_code()` 的说明）。合并看退出码会让「收尾四步全绿、
    只是服务器自己炸了」的现场被说成「收尾有步骤失败，服务不自杀（现场仍在）」，与盘上
    那一版（`state == "done"`）对不上 —— 终端文案因此一律按**现场**说话：
    `ended.steps` 里找得到 `failed` 才算收尾失败档；手上没有收尾现场（`ended is None`，
    空闲态那一格）同样不算。
    """
    ended = getattr(getattr(runtime, "snapshot", None), "ended", None)
    if ended is None:
        return False
    return any(step.status == "failed" for step in ended.steps)


def _report_drain_outcome(runtime, logger: logging.Logger, exit_code: int) -> None:
    """退出时把现场打到终端 —— **按真实档位说话，两档文案分开**（§5.4 行 256、§3.5）。

    两档的判据各看各的（`exit_code` 只用来把强制打断那一档挑出来）：

      - **强制打断档**（`exit_code == EXIT_INTERRUPTED`，第二次 `Ctrl+C` 含「已停在
        `draining` 再按」）：这一趟是**被强制打断**的，盘上留在 `draining` + `interrupted`，
        说的话就该是「收尾被强制打断」那一套 —— **不是**「服务不自杀（现场仍在）」。
        此前这一档不看档位、只按现场挑话，于是「强制打断 + 现场前面有 `failed` 步」这一格
        打出了「服务不自杀（现场仍在）」（审查问题 M11 点名的误报）；
      - **收尾失败档**（现场 `ended.steps` 里找得到 `failed`，见 `_drain_reported_failed()`）：
        照 §5.4 行 256 把 log 路径与服务 log 的最后几行打到终端。这一档的判据看现场、
        不看退出码：`EXIT_FAILURE` 里还压着「`server.run()` 抛异常」这另一件事 —— 那件事由
        `web()` 的 `try` / `except` 自己说清（`logger.exception("服务异常退出")`、带上异常与
        log 路径），这里因此只在收尾**真的有步骤 `failed`** 时倒那套文案；其余情形
        （收尾干净 + 服务异常退出 / 空闲态退出）一个字都不打。
    """
    if exit_code == EXIT_INTERRUPTED:
        # 强制打断档：先说档位，再说盘上留在哪一格与出路（盘上那一版的写法见 §3.5 行为表
        # 第三 / 第四行：`state: "draining"` + `interrupted: true`、无 `ended_at`）
        print(
            "收尾被强制打断（盘上留在 draining + interrupted，未写 ended_at）；"
            f"log 路径：{runtime.log_path}",
            file=sys.stderr,
            flush=True,
        )
        print(
            "出路：看清现场后用 `meeting post <场次目录>` 收干净，或重启服务"
            "（重启走 restart_unfinished 那一档）",
            file=sys.stderr,
            flush=True,
        )
        logger.error("强制打断档退出：已把档位、盘上那一格与 log 路径打到终端")
        return
    if not _drain_reported_failed(runtime):
        return
    log_path = runtime.log_path
    print(f"收尾有步骤失败，服务不自杀（现场仍在）；log 路径：{log_path}", file=sys.stderr, flush=True)
    print(
        "出路一：meeting post <场次目录>；出路二：看清现场后重启服务",
        file=sys.stderr,
        flush=True,
    )
    tail = logging_setup.read_log_tail(log_path, lines=LOG_TAIL_LINES_ON_FAILURE)
    if tail:
        print(f"服务 log 最后 {LOG_TAIL_LINES_ON_FAILURE} 行：", file=sys.stderr, flush=True)
        for line in tail:
            print(f"  {line}", file=sys.stderr, flush=True)
    logger.error("收尾失败档退出：已把 log 路径与最后 %s 行打到终端", LOG_TAIL_LINES_ON_FAILURE)


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
