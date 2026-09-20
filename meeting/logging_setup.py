"""日志（phase1 §3.13，R16；D4 的三个毛病一起修掉）。

- **服务级**：`~/.config/meeting/logs/meeting.log`（`log.dir` / `MEETING_LOG_DIR` 可改）。
- **场次级**：`<场次目录>/meeting.log`，`start` 时挂上、收尾 `log` 那一步 flush 后摘掉 —— R16 的
  log 要能**按场查**（V1 的固定单文件做不到）。本 Phase 只落挂/摘机制
  （`attach_session_log()` / `detach_session_log()`），调用点归 phase2 / phase3。

D4 的三处必改：
  - ① V1「日志目录不可写时 `except OSError: pass` 静默降级」→ 这里**主 log 不可写即抛出**
    （`OSError`），由 `cli.main()` 映射成 `EXIT_STARTUP_UNMET`（拒绝启动）；bootstrap 阶段的
    默认日志目录不可写时不在此处抛，交给调用方按同一档处理。
  - ② V1「超过 5MB 保留后半、删掉前半」→ 轮转保留**整份**旧文件。
  - ③ V1「固定单文件」→ 服务级 + 场次级两层。

**轮转参数一律从配置取**：`RotatingFileHandler(max_bytes=<log.max_bytes 的生效值>,
backup_count=<log.backup_count 的生效值>)` —— 这两个数**不许**在本模块里写死；
服务级与场次级走同一个「按配置值构造轮转处理器」入口（`build_rotating_handler`）。

**记录器**：命名 `meeting` + `propagate = False`（不污染 torch / transformers 的日志），
**幂等初始化**（重复调用不重复加 handler）。

**格式**（逐字照抄）：

    %(asctime)s %(levelname)-7s [%(threadName)s] session=%(session)s %(name)s: %(message)s

无场次时 `session=-`。`session` 字段的注入定死用 `logging.Filter`：`SessionFilter` 在
`filter(record)` 里给每条 record 补 `record.session`；**filter 挂在 handler 上、每个 handler
自带它要写的那个 session 值**（服务级 handler 恒 `-`，场次级 handler 是场次目录名）。
**不采用 `LoggerAdapter`**：服务级与场次级两个 handler 挂在同一个 `meeting` logger 上时，
同一个 logger 给不出两份 session 值。
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from meeting import config as config_module

LOGGER_NAME = "meeting"
LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] session=%(session)s %(name)s: %(message)s"
SESSION_PLACEHOLDER = "-"

#: 本模块自己加的 handler 上的标记：清理由此只动自己的 handler，不动第三方挂上来的
_HANDLER_FLAG = "_meeting_logging_handler"

_SUB = "meeting.logging_setup"
_log = logging.getLogger(_SUB)

_state: dict = {
    "handler": None,
    "session_handler": None,
    "session_dir": None,
}

_logger = logging.getLogger(LOGGER_NAME)
_logger.propagate = False


# ---------------------------------------------------------------------------
# session 字段注入
# ---------------------------------------------------------------------------


class SessionFilter(logging.Filter):
    """给每条 record 补 `record.session`（挂到 handler 上，每个 handler 自带一个值）。"""

    def __init__(self, session: str = SESSION_PLACEHOLDER) -> None:
        super().__init__()
        self.session = session

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging 的固定接口名
        record.session = self.session
        return True


# ---------------------------------------------------------------------------
# 轮转处理器
# ---------------------------------------------------------------------------


def build_rotating_handler(
    log_dir: str,
    *,
    level: str = "INFO",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
    filename: str = config_module.SERVICE_LOG_FILE_NAME,
    session: str = SESSION_PLACEHOLDER,
    encoding: str = "utf-8",
) -> RotatingFileHandler:
    """按配置值构造一个轮转处理器（服务级与场次级共用的唯一入口）。

    `max_bytes` / `backup_count` 由调用方从配置取（默认 5 MiB / 3 份），
    本模块不写死这两个数。目录建立失败或文件不可写 → `OSError`。
    """
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, filename)
    handler = RotatingFileHandler(
        path,
        maxBytes=int(max_bytes),
        backupCount=int(backup_count),
        encoding=encoding,
        delay=False,
    )
    handler.setLevel(_level_number(level))
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(SessionFilter(session))
    setattr(handler, _HANDLER_FLAG, True)
    return handler


def _level_number(level: str | int) -> int:
    if isinstance(level, int):
        return level
    name = str(level).strip().upper()
    value = logging.getLevelName(name)
    if isinstance(value, int):
        return value
    _log.warning("未知日志级别 %r，按 INFO 处理", level)
    return logging.INFO


def _log_path_of(handler: logging.Handler) -> str:
    return str(getattr(handler, "baseFilename", ""))


# ---------------------------------------------------------------------------
# 初始化 / 重建
# ---------------------------------------------------------------------------


def setup_service_logging(
    log_dir: str,
    *,
    level: str = "INFO",
    max_bytes: int | None = None,
    backup_count: int | None = None,
    filename: str = config_module.SERVICE_LOG_FILE_NAME,
) -> RotatingFileHandler:
    """幂等初始化入口：按配置值构造轮转处理器，并换掉 bootstrap 阶段的 handler。

    §3.5 第 1 条的四步都走它：
      ① bootstrap 阶段用**默认参数**建一次；
      ③ 读配置后按生效参数**重建**一次（先摘掉并关闭旧 handler、再加新的）。
    **同参数重复调用时 handler 不重复叠加**（直接返回既有 handler）。

    主 log 不可写 → 抛 `OSError`（由 `cli.main()` 映射成 `EXIT_STARTUP_UNMET`）。
    """
    if max_bytes is None:
        max_bytes = config_module.default_for("log.max_bytes")
    if backup_count is None:
        backup_count = config_module.default_for("log.backup_count")
    log_dir = str(log_dir)

    current = _state["handler"]
    if isinstance(current, RotatingFileHandler):
        same = (
            os.path.normcase(os.path.dirname(_log_path_of(current))) == os.path.normcase(log_dir)
            and os.path.basename(_log_path_of(current)) == filename
            and current.maxBytes == int(max_bytes)
            and current.backupCount == int(backup_count)
            and current.level == _level_number(level)
        )
        if same:
            return current
        previous_path = _log_path_of(current)
        _remove_handler(current)
    else:
        previous_path = ""

    handler = build_rotating_handler(
        log_dir,
        level=level,
        max_bytes=int(max_bytes),
        backup_count=int(backup_count),
        filename=filename,
    )
    _logger.addHandler(handler)
    _logger.setLevel(logging.DEBUG)  # 交给 handler 上的级别过滤，logger 自身不拦
    _state["handler"] = handler

    if previous_path and os.path.normcase(previous_path) != os.path.normcase(_log_path_of(handler)):
        _log.info(
            "bootstrap 阶段的日志留在 %s；服务级日志已按生效 log.dir 重建为 %s（log.level=%s, "
            "log.max_bytes=%s, log.backup_count=%s）",
            previous_path,
            _log_path_of(handler),
            level,
            max_bytes,
            backup_count,
        )
    return handler


def _remove_handler(handler: logging.Handler) -> None:
    """先从 logger 上摘掉，再关闭（关闭本身失败不阻塞重建）。"""
    try:
        _logger.removeHandler(handler)
    except Exception:  # pragma: no cover - removeHandler 不抛
        pass
    try:
        handler.close()
    except OSError:  # pragma: no cover - 关闭失败时旧句柄已无引用
        pass


def handler_snapshot() -> dict:
    """把当前生效的 handler 抓一份快照（**查询**，不改动任何状态）。

    服务级 `handler` 与场次级 `session_handler` 两个槽位各一个（未挂时为 `None`），
    外加场次 handler 对应的目录。它是本模块对外的**小查询入口**：测试层要「先换掉日志
    落点、用完再放回原样」时走 `handler_snapshot()` / `restore_handlers()` 这一对，
    不必去读模块内部的 `_state`。**快照只读**：拿到之后本模块的接线照旧，
    `setup_service_logging()` 该换 handler 还是换。
    """
    return {
        "handler": _state["handler"],
        "session_handler": _state["session_handler"],
        "session_dir": _state["session_dir"],
    }


def restore_handlers(snapshot: dict) -> None:
    """把 `handler_snapshot()` 抓的快照放回接线里（**先清干净自己的 handler，再放回去**）。

    只动本模块自己加的 handler（带 `_HANDLER_FLAG` 标记的那些），第三方挂在 `meeting`
    logger 上的 handler 一律不碰。放回去的是**原来那两个 handler 对象本身**，
    因此恢复之后 `service_log_path()` / `session_log_dir()` 读到的与快照那一刻一致。
    """
    reset_for_tests()

    service_handler = snapshot.get("handler")
    if service_handler is not None:
        _logger.addHandler(service_handler)
        _logger.setLevel(logging.DEBUG)
        _state["handler"] = service_handler

    session_handler = snapshot.get("session_handler")
    if session_handler is not None:
        _logger.addHandler(session_handler)
        _state["session_handler"] = session_handler
        _state["session_dir"] = snapshot.get("session_dir")


def service_log_path() -> str:
    """当前生效的服务级主 log 完整路径（快照 `service.log_path` 取它）。"""
    handler = _state["handler"]
    if handler is None:
        return ""
    return _log_path_of(handler)


def get_logger(name: str | None = None) -> logging.Logger:
    """取 `meeting` 命名空间下的记录器。"""
    if not name:
        return _logger
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


# ---------------------------------------------------------------------------
# 场次级 log 的挂 / 摘
# ---------------------------------------------------------------------------


def attach_session_log(session_dir: str, *, level: str | None = None) -> RotatingFileHandler:
    """把 `<场次目录>/meeting.log` 挂到 `meeting` logger 上（幂等：先摘掉旧的）。

    轮转参数取与服务级相同的两个配置键（`log.max_bytes` / `log.backup_count`）；
    失败以 `OSError` 表达，不静默吞。
    """
    session_dir = str(session_dir)
    if _state["session_handler"] is not None:
        detach_session_log()

    if level is None:
        loaded = config_module.current()
        level = loaded.get("log.level") if loaded is not None else "INFO"
    max_bytes = config_module.get("log.max_bytes")
    backup_count = config_module.get("log.backup_count")
    if max_bytes is None:
        max_bytes = config_module.default_for("log.max_bytes")
    if backup_count is None:
        backup_count = config_module.default_for("log.backup_count")

    handler = build_rotating_handler(
        session_dir,
        level=level,
        max_bytes=int(max_bytes),
        backup_count=int(backup_count),
        session=os.path.basename(os.path.normpath(session_dir)),
    )
    _logger.addHandler(handler)
    _state["session_handler"] = handler
    _state["session_dir"] = session_dir
    return handler


def detach_session_log() -> None:
    """flush 后摘掉场次级 handler。

    写失败时**不许静默吞异常**：抛出 `OSError` 及其子类，**不返回错误对象、不返回布尔值**
    （phase2 的收尾 `log` 步靠 `except OSError` 记账）。无论成败，handler 都已从 logger 上摘掉。
    """
    handler = _state["session_handler"]
    _state["session_handler"] = None
    _state["session_dir"] = None
    if handler is None:
        return

    error: OSError | None = None
    try:
        handler.flush()
    except OSError as exc:
        error = exc
    if error is None:
        try:
            handler.close()
        except OSError as exc:  # pragma: no cover - close 一般只在 flush 失败时失败
            error = exc
    else:
        try:
            handler.close()
        except OSError:
            pass
    _logger.removeHandler(handler)
    if error is not None:
        raise error
    return None


def session_log_dir() -> str | None:
    return _state["session_dir"]


def reset_for_tests() -> None:
    """测试用：摘掉本模块加的全部 handler（不动 pytest 自己挂的）。"""
    if _state["session_handler"] is not None:
        try:
            detach_session_log()
        except OSError:
            pass
    handler = _state["handler"]
    if handler is not None:
        _remove_handler(handler)
    _state["handler"] = None


__all__ = [
    "LOGGER_NAME",
    "LOG_FORMAT",
    "SESSION_PLACEHOLDER",
    "SessionFilter",
    "attach_session_log",
    "build_rotating_handler",
    "detach_session_log",
    "get_logger",
    "handler_snapshot",
    "reset_for_tests",
    "restore_handlers",
    "service_log_path",
    "session_log_dir",
    "setup_service_logging",
]
