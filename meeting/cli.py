"""命令行入口（phase1 §3.3 / §3.4 / R15）。

只认两个词：`meeting web` / `meeting post`（**不加第三个词**）。

五个退出码常量在**本模块**模块级定义（§3.4 冻结表），`main()` 的每个出口一律
`sys.exit(<常量>)`，log 里「退出原因与退出码」那一行也写常量名 —— **本模块里不出现裸数字**
（异常里的 HTTP 状态码之类不算退出码，`probe_meeting_service` 的 200 也不在本文件里；
`_parse_post_args()` 内部那个 `argv[0]` 的下标是取位置参数、不是退出码）。

`meeting post`（`65dd3e4c` phase1）的编排收在本模块的 `run_post()` 里：音频归档补齐
（PCM → MP3，复用 `meeting/capture/archive.py` 那一份转码）→ 跑离线那一趟 → 等纪要那一层的
终态 → 把每层的结论与产物**绝对路径**汇总成一份可读摘要。`post()` 只做三件事：解析参数 →
调 `run_post()` → 按返回的 `exit_code` 挑 `sys.exit(<常量>)` 并打终端输出。
**两条入口共用同一段编排**：开会结束时自动跑的那一遍（`meeting/backends/post.py` 的
`PostProcessHook._run()`）也调 `run_post()`，两处因此不会分叉。

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
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from meeting import config as config_module
from meeting import logging_setup
from meeting.capture.archive import EMPTY_PCM_BYTES
from meeting.offline import runner as offline_runner
from meeting.service import app as service_app

# ---------------------------------------------------------------------------
# 退出码表（§3.4，**本 Phase 冻结**：标识符与取值都是冻结项）
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_REJECTED = 2
EXIT_STARTUP_UNMET = 3
EXIT_INTERRUPTED = 130

#: 五档常量按序排好（`_exit_name()` 的翻名表、终端文案与用例都读这一份）
EXIT_NAMES: tuple[tuple[str, int], ...] = (
    ("EXIT_OK", EXIT_OK),
    ("EXIT_FAILURE", EXIT_FAILURE),
    ("EXIT_REJECTED", EXIT_REJECTED),
    ("EXIT_STARTUP_UNMET", EXIT_STARTUP_UNMET),
    ("EXIT_INTERRUPTED", EXIT_INTERRUPTED),
)

ACTIONS = ("web", "post")

USAGE = (
    "用法：meeting <动作词>\n"
    "  可用动作词：web / post\n"
    "    web   起本地服务（只监听 127.0.0.1），前台跑到 Ctrl+C\n"
    "    post  只跑会后那一套：meeting post <场次目录> [--force]\n"
)

#: `post` 那一行在用法说明里的样子（字符串常量名进文案，**不写裸数字**）
POST_USAGE = (
    "用法：meeting post <场次目录> [--force]\n"
    "  <场次目录>  要收干净的哪一场（不猜最新一场、不列目录内容、没有交互菜单）\n"
    "  --force     无视两层的终态，重跑离线段与纪要段（离线段与纪要段都会再花一次钱）\n"
)

#: `post` 认的开关（本轮**只有** `--force` 一个）
POST_FLAGS: tuple[str, ...] = ("--force",)

# ---------------------------------------------------------------------------
# 会后编排（`meeting post` 与「开会结束时自动跑的那一遍」共用这一段）
# ---------------------------------------------------------------------------

#: 归档那一层的三种结论文档名
ARCHIVE_DONE = "done"
ARCHIVE_SKIPPED = "skipped"
ARCHIVE_FAILED = "failed"

#: 归档那一层每个源的三档结论文档名（「转出了」/「没什么要转的」/「转砸了」）
OUTCOME_CONVERTED = "converted"
OUTCOME_NO_WORK = "no_work"
OUTCOME_FAILED = "failed"

#: 离线那一层的结论词（按层的终态分档）
LAYER_DONE = "done"
LAYER_ABSENT = "absent"
LAYER_FAILED = "failed"
LAYER_UNKNOWN = "unknown"

#: 两层共用的「上一趟还在跑 / 崩在半路」那一档（`meeting post` 默认拒跑的那一档）
LAYER_RUNNING = "running"

#: 纪要那一层独有的**第四档**：已交付 DSH web 服务端生成、产物还没落。
#:
#: 它既不是 `running`（本进程没有线程在跑）也不是 `done`（产物还没经四条判据），因此
#: 单独给一个词：读方按它判「这一场已经交付过、别再拉起第二个会话」，按产物判「完成没有」。
LAYER_DELIVERED = "delivered"

#: 纪要那一层的产物名（场次目录根）：终态状态文件与纪要本体
MINUTES_FINAL_NAME = "meeting_final.md"

#: 等纪要那一层终态的上界（秒）的兜底档：**只在连配置表都读不到时**用它，
#: 正常路径读的是现成的纪要墙钟键 `minutes.timeout_seconds`。
MINUTES_TIMEOUT_FALLBACK_SECONDS = 14400.0

#: 等纪要时每一次读盘之间那一小会儿。
#:
#: 为什么这里只能读盘、没有可挂的事件：纪要那一层由启动器起一条 daemon 线程（它自己的
#: `launch()` 返回即算启动成功），终态由那条线程或一个 headless 子进程写进
#: `meeting_minutes_status.json` —— 编排函数手里没有可 join 的句柄、也没有跨进程的回调，
#: 盘上那份状态文件就是这一层唯一的完成通知。因此这里的等待是**有界的**：上界取
#: `minutes.timeout_seconds`，每次读盘之间只让出这一小会儿，落定就立刻返回、不空转。
MINUTES_POLL_SECONDS = 0.05

#: 编排函数在摘要里报的三步（终端按这个顺序打「每步做了什么」）
POST_STEPS: tuple[tuple[str, str], ...] = (
    ("archive", "音频归档补齐（PCM -> MP3）"),
    ("offline", "会后离线（离线转写与说话人聚类）"),
    ("minutes", "会后纪要（纪要生成）"),
)


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
        post(args[1:])
        return

    print(USAGE, end="", file=sys.stderr)
    print(f"未定义的动作词：{action!r}", file=sys.stderr)
    print("退出原因与退出码：用法错误（未定义的动作词） -> EXIT_REJECTED", file=sys.stderr)
    sys.exit(EXIT_REJECTED)


# ---------------------------------------------------------------------------
# `meeting post` —— 参数面（校验落在可被用例直接调用的函数上，不埋在 main() 里）
# ---------------------------------------------------------------------------


def _parse_post_args(argv: Sequence[str]) -> dict[str, Any]:
    """解析 `meeting post` 后面那一串：交回 `{"dir": <path>, "force": <bool>}`；用法错就抛。

    **不猜目录、不挂菜单、不列目录内容**（R17 / 不做 TUI）：位置参数缺了就报用法，
    给了就用它；指到不存在的路径就报那一条路径。`--force` 是本轮**唯一**的开关，
    重复给也照收（幂等的布尔值）。

    抛出来的 `ValueError` 里那句话就是终端要打的原因，调用方（`post()`）接住它，
    打用法说明 + `EXIT_REJECTED`。
    """
    args = list(argv)
    force = False
    positionals: list[str] = []
    for item in args:
        if item in POST_FLAGS:
            force = True
            continue
        if item.startswith("-") and item != "-":
            raise ValueError(f"未定义的开关：{item!r}（本轮只认 {' / '.join(POST_FLAGS)}）")
        positionals.append(item)

    if not positionals:
        raise ValueError("缺少位置参数 <场次目录>")
    if len(positionals) > 1:
        raise ValueError(
            "位置参数只收一个 <场次目录>，收到 "
            + "、".join(repr(item) for item in positionals)
        )
    directory = Path(positionals[0])
    if not directory.is_dir():
        raise ValueError(f"场次目录不存在（或者不是目录）：{directory}")

    return {"dir": directory.resolve(), "force": force}


# ---------------------------------------------------------------------------
# `meeting post` —— 真入口
# ---------------------------------------------------------------------------


def _load_config_or_exit() -> config_module.Config:
    """**`meeting post` 这一条入口的配置前奏**：读配置 → 值级校验 → 生效，交回生效配置。

    与 `web()` 第 ② 步同一套口径（`config_module.load()` + 同一个出口的档位与同一套终端
    说法）：配置损坏 → `EXIT_STARTUP_UNMET`；取值非法 → `EXIT_REJECTED`。两个出口都
    `sys.exit(<常量>)`，**异常一律在本函数里接住**，不冒到解释器那里变成解释器默认码。

    **为什么这条路必须有这一段**：`run_post()` 这一趟里的每一个取值都经
    `config_module.get(...)` 现读 —— 离线段按 `post.engine` / `post.model` / `post.dtype` /
    `post.chunk_seconds` 造引擎与分块，纪要段按 `minutes.hook` 决定**要不要真拉起
    headless 子会话**、按 `minutes.timeout_seconds` 定等待的上界。这一层没读进来的话
    它们全部退回 `DEFAULT_VALUES`：用户显式写的 `minutes.hook = null`（明确不要会后纪要）
    会被当成缺省的 `auto` 照旧拉起子会话（**那是真花钱的一步**），显式写的
    `post.engine = <替身>` 也会被当成 `auto` 造出真离线引擎。
    `web()` 有这一段，`post()` 此前没有 —— 这就是这条入口不生效的那一格。

    **读取时机**：在幂等闸与花钱之前。配置本身就决定「这一趟要不要花钱」，
    判早了会按默认值误判（该跳过的层被拉起来、或该跑的层被当成已关掉）。

    失败档与 `web()` 同源：`ConfigCorruptError` →「启动前提不成立」（`EXIT_STARTUP_UNMET`）、
    `ConfigValueError` →「配置值非法」（`EXIT_REJECTED`），终端一律打 `f"{说法}：{exc}"`。

    **交回生效配置**（`config_module.Config`）供调用方走 `web()` 第 ③ 步那一格
    （按生效 `log.*` 重建服务级日志，见 `post()`）。
    """
    try:
        loaded = config_module.load()
    except config_module.ConfigCorruptError as exc:
        print(f"启动前提不成立：{exc}", file=sys.stderr, flush=True)
        try:
            _log().error("启动前提不成立：%s -> EXIT_STARTUP_UNMET", exc)
        except Exception:  # pragma: no cover - 日志尚未初始化时只打印终端
            pass
        sys.exit(EXIT_STARTUP_UNMET)
    except config_module.ConfigValueError as exc:
        print(f"配置值非法：{exc}", file=sys.stderr, flush=True)
        try:
            _log().error("配置值非法：%s -> EXIT_REJECTED", exc)
        except Exception:  # pragma: no cover - 同上
            pass
        sys.exit(EXIT_REJECTED)

    config_module.set_current(loaded)
    try:
        _log().info(
            "配置来源：%s；生效值清单：\n%s",
            loaded.config_path(),
            config_module.format_applied(loaded),
        )
    except Exception:  # pragma: no cover - 同上
        pass
    return loaded


def post(argv: Sequence[str] | None = None) -> None:
    """`meeting post <场次目录> [--force]` —— 只跑会后那一套，跑完就退。

    五件事，按顺序：解析参数（用法错 → 用法说明 + `EXIT_REJECTED`）→ **读配置并生效**
    （与 `web()` 同源那一套：损坏 `EXIT_STARTUP_UNMET`、取值非法 `EXIT_REJECTED`，
    见 `_load_config_or_exit()`）→ **按生效配置重建服务级日志**（`log.dir` / `log.level` /
    `log.max_bytes` / `log.backup_count` 四格，`web()` 第 ③ 步那一格；主 log 不可写 →
    `EXIT_STARTUP_UNMET`）→ 调 `run_post()` → 打终端输出并按摘要里的 `exit_code`
    挑 `sys.exit(<常量>)`。

    **每个出口都 `sys.exit(<常量>)`，`sys.exit()` 之前一律 `sys.stdout.flush()`**：
    退出码走 §3.4 那五档现成常量，输出不 flush 的话管道里读不到（用例也没法断言）。
    `Ctrl+C` 落在离线段（同步跑）或等纪要那一段（后台线程在跑、这里在轮询）都由
    本函数接住，按「强制打断档」的既有常量收场。
    """
    try:
        options = _parse_post_args(sys.argv[2:] if argv is None else argv)
    except ValueError as exc:
        print(POST_USAGE, end="", file=sys.stderr, flush=True)
        print(f"用法错误：{exc}", file=sys.stderr, flush=True)
        print("退出原因与退出码：用法错误（post 的参数） -> EXIT_REJECTED", file=sys.stderr, flush=True)
        sys.stdout.flush()
        sys.exit(EXIT_REJECTED)

    # **配置层在幂等闸与花钱之前读进来**（§3.5 第 1 条那一段的 ① ② 两步，与 `web()` 同源）：
    # 这一趟里每一个取值都从它现读，没有它 `minutes.hook` / `post.engine` 这些用户配置
    # 一个字都不生效。bootstrap 日志照 `web()` 第 ① 步那一套（默认日志目录 + 默认轮转参数）。
    try:
        logging_setup.setup_service_logging(config_module.default_log_dir())
    except OSError as exc:
        print(
            f"注意：默认日志目录不可写（{exc}），bootstrap 阶段只有终端可写",
            file=sys.stderr,
            flush=True,
        )
    loaded = _load_config_or_exit()

    # **③ 按生效配置重建服务级日志**（`web()` 第 ③ 步那一格，同一个口径）：`log.dir` 是这一
    # 条路上被读进来却曾经一次都没被用的那一个键 —— 不重建的话用户配的落点不生效、日志照旧
    # 落在默认目录。`log.level` / `log.max_bytes` / `log.backup_count` 三格同一处一起生效。
    log_dir = loaded.get("log.dir")
    try:
        logging_setup.setup_service_logging(
            log_dir,
            level=loaded.get("log.level"),
            max_bytes=loaded.get("log.max_bytes"),
            backup_count=loaded.get("log.backup_count"),
        )
    except OSError as exc:
        # 主 log 不可写 → 拒绝启动（与 `web()` 第 ④ 步同一句话、同一个退出码）
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
        except Exception:  # pragma: no cover - 同上
            pass
        sys.exit(EXIT_STARTUP_UNMET)

    try:
        _log().info(
            "会后流程开始：%s（force=%s）", options["dir"], bool(options["force"])
        )
    except Exception:  # pragma: no cover - 日志尚未初始化时只打印终端
        pass

    try:
        outcome = run_post(options["dir"], force=bool(options["force"]))
    except KeyboardInterrupt:
        print("会后流程被强制打断（Ctrl+C）。", file=sys.stderr, flush=True)
        try:
            _log().error("会后流程被强制打断 -> EXIT_INTERRUPTED")
        except Exception:  # pragma: no cover - 同上
            pass
        sys.stdout.flush()
        sys.exit(EXIT_INTERRUPTED)
    except BaseException as exc:
        # 编排函数整个抛出来（它自己也接不住的那一档）：这一趟按失败档收场，
        # **现场照实打到终端**，不让调用方只看到一个栈。
        error = f"{type(exc).__name__}: {exc}"
        try:
            _log().exception("会后流程抛出来了：%s", options["dir"])
        except Exception:  # pragma: no cover - 同上
            pass
        outcome = _failure_summary(Path(str(options["dir"])), {}, error)
        _report_post_outcome(outcome, EXIT_FAILURE)
        print(error, file=sys.stderr, flush=True)
        sys.stdout.flush()
        sys.exit(EXIT_FAILURE)

    exit_code = int(outcome.get("exit_code", EXIT_FAILURE))
    _report_post_outcome(outcome, exit_code)
    try:
        _log().info(
            "退出原因与退出码：会后流程结束（%s） -> %s",
            outcome.get("state"),
            _exit_name(exit_code),
        )
    except Exception:  # pragma: no cover - 同上
        pass
    sys.stdout.flush()
    sys.exit(exit_code)


def _report_post_outcome(outcome: dict[str, Any], exit_code: int) -> None:
    """把「选了哪一场 / 每步做了什么 / 产物落在哪 / 结论是什么」打到终端。

    产物一律打**绝对路径**（`Path.resolve()`），判据在用例里逐条断言到具体子串。
    幂等闸那三档在这里各有一句话：**无待办**（两层都已经收干净）、**`running` 拒跑**
    （点名那一个状态文件 + 给出 `--force`）、以及只补缺的那一层时每一层的结论。
    """
    directory = Path(str(outcome.get("session_dir") or ""))
    print(f"选了这一场：{outcome.get('session_id')}", flush=True)
    print(f"场次目录：{directory}", flush=True)

    steps = dict(POST_STEPS)
    archive = outcome.get("archive") or {}
    print(
        f"步骤 1/{len(POST_STEPS)} {steps['archive']}：{archive.get('state')}",
        flush=True,
    )
    for row in archive.get("sources") or []:
        mp3 = row.get("mp3")
        print(
            f"  - {row.get('source')}：{row.get('outcome')}（PCM {row.get('pcm')}"
            + (f" -> MP3 {mp3}" if mp3 else "")
            + (f"；原因 {row.get('reason')}" if row.get("reason") else "")
            + "）",
            flush=True,
        )

    offline = outcome.get("offline") or {}
    print(
        f"步骤 2/{len(POST_STEPS)} {steps['offline']}：{offline.get('hook')}"
        f"（state={offline.get('state')}）",
        flush=True,
    )
    if offline.get("error"):
        print(f"  离线那一层的说明：{offline['error']}", flush=True)
    for source, row in (offline.get("sources") or {}).items():
        for key in ("transcript", "speakers", "report"):
            value = (row or {}).get(key)
            if value:
                print(f"  - {source} 的 {key}：{Path(str(directory), str(value))}", flush=True)

    minutes = outcome.get("minutes") or {}
    print(
        f"步骤 {len(POST_STEPS)}/{len(POST_STEPS)} {steps['minutes']}："
        f"{minutes.get('hook')}（state={minutes.get('state')}）",
        flush=True,
    )
    if minutes.get("reason") and exit_code != EXIT_OK:
        print(f"  纪要那一层的说明：{minutes['reason']}", flush=True)

    # 幂等闸那一档：两层都没跑，因为两层都已经收干净了
    if offline.get("skipped") and minutes.get("skipped") and exit_code == EXIT_OK:
        print(
            f"无待办：离线段与纪要段都已是终态、产物齐（离线段 state="
            f"{offline.get('state')}，纪要段 state={minutes.get('state')}，音频已归档）"
            f"，这一趟没有重跑任何一段（退出码 {_exit_name(exit_code)}）",
            flush=True,
        )

    # `running` 拒跑那一档：**点名那一个状态文件**，并给出 `--force` 这句话
    if exit_code == EXIT_REJECTED:
        print(
            "拒跑：有状态文件停在 running（上一趟还在跑，或者崩在半路），"
            "这一趟没有调引擎、也没有拉起子会话。",
            flush=True,
        )
        for layer, label in ((offline, "离线段"), (minutes, "纪要段")):
            if layer.get("hook") == LAYER_RUNNING:
                print(
                    f"  - {label}：{Path(str(layer.get('status_path') or '')).name} 停在 running",
                    flush=True,
                )
        print(
            f"出路：确认上一趟已经收工之后，加 --force 再来（meeting post {directory} --force）",
            flush=True,
        )

    artifacts = outcome.get("artifacts") or {}
    print("产物：", flush=True)
    for name, path in artifacts.items():
        print(f"  - {name}：{path}", flush=True)

    print(f"结论：{outcome.get('state')}（退出码 {_exit_name(exit_code)}）", flush=True)

    if not outcome.get("ok", False):
        print(
            "出路：看清上面每一层的结论与产物路径后再跑一次；"
            f"`meeting post {directory}`（现在这一趟的退出码是 {_exit_name(exit_code)}）",
            file=sys.stderr,
            flush=True,
        )


# ---------------------------------------------------------------------------
# 会后编排（两条入口共用这一段）
# ---------------------------------------------------------------------------


def run_post(
    session_dir: Path | str,
    *,
    force: bool = False,
    engine: Any = None,
    minutes_launcher: Any = None,
    archive_converter: Callable[..., bool] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """把「跑会后一套」跑一遍，交回一份可读摘要（`post()` 与收尾第 4 步共用）。

    四件事，按顺序：

      1. **音频归档补齐**（`sync_archive()`）：两路各自独立判、独立转。判据与收尾第 2 步
         一致 —— 该路 PCM 非空（字节数 `>= EMPTY_PCM_BYTES`）且（MP3 缺席 或 MP3 比 PCM 旧）
         才转；空 PCM 不产 MP3、已比 PCM 新的 MP3 不重转；转码失败如实记 `failed`、
         **PCM 一个字节都不动**，退出码走 `EXIT_FAILURE`。转码实现只有一份
         （`meeting/capture/archive.py` 的 `convert_to_mp3` + `mp3_path_for`），本模块不另写。
      2. **跑离线那一趟**：`offline_runner.run_session(session_dir, engine=engine,
         minutes_launcher=…, force=…)` —— **模块属性调用**，好让用例把 `run_session`
         整个换掉；两个注入参数**一路向下传**（落在 `_announce_minutes()` 的形参上，
         不落在模块全局上）。
      3. **等纪要那一层的终态**：纪要挂在离线链尾巴上（`_announce_minutes()`），产物是场次
         目录根的 `meeting_minutes_status.json` 与 `meeting_final.md`；**要等它落定才报结论**。
         等待是事件驱动的轮询，上界用现成的纪要墙钟键 `minutes.timeout_seconds`。
         三条会拉起纪要的路径（首跑 / `--force` / 离线已终态只补纪要）**共用同一处等待**
         （`_wait_for_settled_minutes()`）：判据是「盘上那份状态与**拉起之前**的基线快照
         （`_minutes_baseline()`）不同、并且已经落在终态」，于是上一趟留在盘上的终态不会被
         当成这一趟的账。结论如实三分：新终态 `done` 且产物过判据 → 成功档；新终态 `failed`、
         或 `done` 而产物过不了判据、或读不动 → 如实失败；到上界仍未落定 → 现成的 `timeout`
         那一档（承认它还在跑，既不谎报成功也不写成 `failed`）。
      4. **汇总**：每层的结论 + 逐条产物的**绝对路径** + 退出码，都在返回的摘要里。

    ## 幂等闸：判定一律落在**花钱之前**

    两层各有自己的「已完成」判据（**状态文件终态 + 产物齐**，缺一条就不算完成）：

      - **离线段**：`meeting_offline_status.json` 在、`state == "done"`，且三份产物齐
        （本场声明的每一路各一份 `meeting_offline_transcript_<source>.md` /
        `meeting_offline_speakers_<source>.json`，外加一份 `meeting_offline_report.md`）；
      - **纪要段**：`meeting_minutes_status.json` 在、`state == "done"`，且 `meeting_final.md`
        过得了现成的四条判据（`minutes/launch.py::check_final_artifact()`，复用不重写）。

    据此分四档，**判定在 `sync_archive()` 之后、`run_session()` 之前做完**：

      - **两层都到终态**（且音频已归档）→ 打印「无待办」，`EXIT_OK`，**一个引擎调用、
        一个子进程都不产生**；
      - **只差一层** → 只跑那一层（`run_session(..., offline=False)` 时离线段一个引擎都不调，
        `minutes=False` 时那一道 `_announce_minutes()` 不拉起子会话）；
      - **任一层停在 `running`** → 默认**拒绝这一趟**（`EXIT_REJECTED`），提示里点名那一个
        状态文件并给出「加 `--force` 再来」—— 两个 `meeting post` 对同一目录齐发时，
        第二趟因此不会跟着拉起第二个子会话；
      - **`--force`** → 无视上面全部的终态判定，**两层都重跑**（`--force` 是本轮唯一会
        再次花钱的开关）。

    `minutes_launcher` 缺省 `None` 时走产品路径：离线段这一趟**跑**时由
    `runner.run_session` 尾巴上那道 `_announce_minutes()` 拉起纪要，离线段**已是终态**时
    由本函数自己经同一道 `_announce_minutes()` 拉起（**缺什么补什么**的那一半不许只对
    注入了假件的路径生效）。用例塞一个假启动器时，它的交回值说明这一趟走到哪一档 ——
    「这一趟有没有再拉起子会话」于是在用例里留下一份可核对的账。**等待只看那一档、不看是
    谁拉起的**（真启动器与假启动器在时序上同形：`launch()` 返回即算启动成功、终态晚一步才落）：
    被拉到的那一档一律等这一趟新落的终态，一份终态都没落的假件因此等到上界、如实报 `timeout`
    —— 这也正是真机上「子会话还在跑」的现场。
    拉起点整个缺席 / 被再入闸拦下 / 解析不出实现时没有终态会来，不等，按盘上那一刻的
    两半判据出结论（缺席与拉起点抛错两档仍如实记失败）。

    **离线那一趟整个抛出来时本函数原样上抛**（抛之前先把 `meeting_offline_status.json`
    补成 `failed`）：`meeting post` 这一条入口自己接住它并按 `EXIT_FAILURE` 收场，
    收尾第 4 步那条入口的线程体接住它、按既有契约把那一格记成 `failed` ——
    两条入口各自的「如实记账」因此都留在自己的落点上。**别的失败一律在摘要里回答，
    不抛**：归档转不动、离线某一层落 `failed`、等纪要超时都走 `exit_code` 那一格。
    """
    directory = Path(session_dir) if session_dir else None
    if directory is None or not str(directory):
        raise ValueError("run_post 需要场次目录（收到空值）")

    started = time.monotonic()
    budget = _minutes_timeout_seconds()
    if deadline is None:
        deadline = started + float(budget)
    directory = directory.resolve()

    result: dict[str, Any] = {
        "session_id": directory.name,
        "session_dir": str(directory),
        "force": bool(force),
        "started_at": started,
        "deadline": float(deadline),
        "minutes_timeout_seconds": float(budget),
        "archive": sync_archive(directory, converter=archive_converter),
        "offline": None,
        "minutes": None,
        "artifacts": {},
    }

    if not directory.is_dir():
        result.update(_summarize_run(result, error=f"场次目录不存在：{directory}"))
        return result

    if result["archive"]["state"] == ARCHIVE_FAILED:
        # 有一路转不动：如实停在归档这一层，**照样跑离线那一趟**（归档失败不许带走离线链：
        # 离线那一层自己按「PCM 非空优先、否则 MP3」取音频，PCM 一个字节都没被动过）。
        try:
            _log().error("音频归档补齐失败，离线那一趟照常跑：%s", directory)
        except Exception:  # pragma: no cover - 日志尚未初始化时只走终端
            pass

    # ---- 幂等闸：两层各自的现状，判定落在花钱之前 ----
    offline_status = _offline_status(directory)
    minutes_status = _minutes_status(directory)
    # **拉起之前那一份基线快照**（纪要状态文件的现场）：拉起之后要等的「新终态」，就是
    # 与它不同的那一份。三条路径（首跑 / `--force` / 离线已终态只补纪要）共用这一格 ——
    # 盘上留着上一趟的终态时，等到**这一趟新落的**那一份才算数。
    minutes_baseline = _minutes_baseline(directory)
    offline_running = _layer_is_running(offline_status, ("state",))
    minutes_running = _layer_is_running(minutes_status, ("state",))
    # **已经交付过**（web 分支的终态 `delivered`、产物还没落）：这一趟也不拉起第二个会话 ——
    # 再拉一趟只会让同一场生成两遍。它与 `running` 那一档的区别是「上一趟还在不在跑」：
    # 交付那条路的语义是「投递即完事」，这一层不去猜 DSH 那边写没写完。
    minutes_delivered = _minutes_is_delivered(minutes_status)
    if not force and (offline_running or minutes_running or minutes_delivered):
        result["offline"] = _skipped_offline_outcome(
            directory, offline_status, running=offline_running
        )
        result["minutes"] = _skipped_minutes_outcome(
            directory,
            minutes_status,
            running=minutes_running,
            delivered=minutes_delivered,
        )
        result["artifacts"] = _artifact_paths(directory)
        # 退出码**不在这里定**：`_summarize_run()` 按两层的结论与归档那一层一起判
        #（它在 `archive_failed` / 离线段失败两档上一律取 `EXIT_FAILURE`，省得与这里打架）。
        result.update(_summarize_run(result))
        return result

    # 「这一层已是终态」与「这一趟到底跑不跑」是两件事：带 `--force` 时前者的取值照旧，
    # 但两层都要重跑（`--force` 是本轮唯一会再次花钱的开关）。
    offline_complete = _offline_layer_done(directory, offline_status)
    minutes_complete = _minutes_layer_done(directory, minutes_status)
    offline_done = bool(offline_complete and not force)
    minutes_done = bool(minutes_complete and not force)
    # 「这一趟没有待办」的**前置**是「音频已归档」那一条（任务书「关键决定 4」）：归档这一层
    # 有一路转不动时不许走这一档 —— 那一趟既不能报 `EXIT_OK`，也不能打一句「音频已归档」。
    if offline_done and minutes_done and result["archive"]["state"] != ARCHIVE_FAILED:
        # 两层都到终态、产物齐、音频也归档过了：这一趟**没有待办**。
        # 一个引擎调用、一个子进程都不产生（判据落在上面两条，不在这一档的措辞里）。
        result["offline"] = _finished_offline_outcome(directory, offline_status)
        result["minutes"] = _finished_minutes_outcome(directory, minutes_status)
        result["artifacts"] = _artifact_paths(directory)
        return _summarize_run(result)

    announced: list[dict[str, Any]] = []
    offline_error: str | None = None
    runner_result: dict[str, Any] | None = None
    if offline_done:
        # 离线段已是终态：**一个引擎调用都不产生**（判据是假引擎的调用次数为 0），
        # 三份离线产物的 sha256 因此一个字节不变。
        try:
            _log().info("离线段已是终态且产物齐，这一趟不重跑：%s", directory)
        except Exception:  # pragma: no cover - 日志尚未初始化时只走终端
            pass
    else:
        # 尾巴上那一道通知（`_announce_minutes()`）的解与注两个参数**向下传**：
        # `run_session` 把它们原样交给那道闸与启动器，本模块不替换任何模块全局属性。
        # `minutes_launcher` 非空时用一个记账包装把它记下来（这一趟的结论按假件交回的
        # 结果出）；`force` 这一格是「这一趟该不该无视纪要段的终态」。
        minutes_launcher_lane = (
            _capturing_launcher(minutes_launcher, announced)
            if minutes_launcher is not None
            else None
        )
        try:
            runner_result = offline_runner.run_session(
                directory,
                engine=engine,
                minutes_launcher=minutes_launcher_lane,
                force=bool(force),
            )
        except BaseException as exc:  # 离线这一层整个抛出来：如实记，**再原样上抛**
            offline_error = f"{type(exc).__name__}: {exc}"
            try:
                _log().exception("会后离线那一趟抛出来了：%s", directory)
            except Exception:  # pragma: no cover - 同上
                pass
            # 上抛的理由：`meeting post` 这一条入口自己接住它（摘要里那一格如实写、这一趟按
            # `EXIT_FAILURE` 收场），而收尾第 4 步那条入口的线程体接住它之后把
            # `meeting_offline_status.json` 补成 `failed` —— 那一格因此不会永远停在 `running`
            # （`PostProcessHook` 的既有契约：任何异常都落进状态文件、绝不外传给收尾流程）。
            # 在**上抛之前**把状态文件补一版 `failed`，好让直接调 `run_post()` 的读者也有账可查。
            _mark_offline_failed(directory, offline_error)
            raise

    result["offline"] = _offline_outcome(
        directory, runner_result, error=offline_error, skipped=offline_done
    )
    result["minutes"] = _minutes_step_outcome(
        directory,
        runner_result=runner_result,
        offline_done=offline_done,
        minutes_complete=minutes_complete,
        launcher=minutes_launcher,
        announced=announced,
        deadline=float(deadline),
        baseline=minutes_baseline,
        force=bool(force),
    )
    result["artifacts"] = _artifact_paths(directory)
    return _summarize_run(result)


def _minutes_step_outcome(
    directory: Path,
    *,
    runner_result: dict[str, Any] | None,
    offline_done: bool,
    minutes_complete: bool,
    launcher: Any,
    announced: list[dict[str, Any]],
    deadline: float,
    baseline: dict[str, Any],
    force: bool = False,
) -> dict[str, Any]:
    """纪要段这一趟的结论：**谁跑过就用谁的账**，没跑过就按盘上的现状出。

    四种情形，按「这一趟有没有经过离线链的尾巴」分开：

      - **离线链跑过**（`runner_result` 有内容）：尾巴上那道 `_announce_minutes()` 已经把
        纪要段该做的事做完了（闸放行→拉起，或闸拦下→`already_done`），这一格按它的交回值出；
      - **离线链没跑**（离线段已是终态）：这一层单独走一遍
        `_announce_minutes(..., launcher=…)` —— 拉起这一步与离线那一段本来就有各自的闸，
        离线段收工不该连带把纪要段钉住（`--force` 之外，这是「缺什么补什么」的另一半）。
        **注入的假启动器与产品路径都要走这一步**：`launcher` 非空时它被调到一次（用例靠
        这份记账核「这一趟有没有再花钱」），`launcher` 缺省 `None` 时由 `_announce_minutes()`
        自己按 `minutes.hook` 解析实现并拉起（其中那道 `MEETING_MINUTES_CHILD=1` 再入闸
        照旧管用）—— 产品路径因此不再只报盘上现状、不再缺一层也不补；
      - **两层都到终态**（`minutes_complete` 为真）：这一格在这一档根本走不到（`run_post()`
        在它之前就按「无待办」早退），留在这里只是兜住直接调本函数的读者。

    `baseline` 是**拉起之前**那一份现场快照（`_minutes_baseline()`）：拉起成功之后，
    「等到这一趟新落的终态」这条判据就落在它身上，三条路径因此共用同一处等待。

    `force` 是这一趟的 `--force`，**透到那道闸上**（`_announce_minutes(..., force=…)`）：
    「只补纪要」这一支的开关与产品路径那两支同源，不是写死的。
    """
    if runner_result is not None:
        announce = dict(runner_result.get("minutes") or {})
        if announced:
            announce.update(announced[0])
        # 交回值里那一格 `state` 交给 `_minutes_from_launcher()` 按档位判：`already_done`
        # 那一档按闸读到的那一份出；`launched` 那一档**不看它**（拉起那一刻它答不出落定
        # 没有），一律等这一趟新落的终态。
        return _minutes_from_launcher(
            directory,
            announce or None,
            baseline=baseline,
            deadline=deadline,
        )
    if launcher is not None:
        try:
            outcome = offline_runner._announce_minutes(
                directory, launcher=launcher, force=force
            )
        except BaseException as exc:  # 纪要这一层炸了不许带走离线那三份产物
            try:
                _log().exception("会后纪要那一层没能拉起来：%s", directory)
            except Exception:  # pragma: no cover - 日志尚未初始化时只走终端
                pass
            return _minutes_from_launcher(
                directory,
                {"hook": "failed", "reason": f"{type(exc).__name__}: {exc}"},
                baseline=baseline,
                deadline=deadline,
            )
        return _minutes_from_launcher(
            directory, outcome, baseline=baseline, deadline=deadline
        )
    if minutes_complete:
        return _finished_minutes_outcome(directory, _minutes_status(directory))
    return _launch_minutes_on_the_product_path(
        directory, force=force, deadline=deadline, baseline=baseline
    )


def _quiet_skip_announce(outcome: dict[str, Any]) -> dict[str, Any]:
    """把「安静跳过」那一档的交回值整成 `_minutes_from_launcher()` 认的形状。

    只取该函数用得到的那几格（`hook` / `announced` / `reason`）再加一格 `launcher`：
    `_minutes_from_launcher()` 的缺省把 `launcher` 写成 `"injected"`，而这一趟走的是
    **产品路径**（`_announce_minutes()` 自己按 `minutes.hook` 解析），这一格因此明写
    `product` —— 摘要里那一格说的是「这一层的结论是谁拉起来的那一档给的」，产品路径的
    安静跳过不该顶着注入那一档的名字。

    `announced` 明写成 `False`：「这一趟没有再拉起子会话」正是这一档在说的话。
    """
    hook = str(outcome.get("hook") or "")
    return {
        "hook": hook,
        "announced": False,
        "launcher": "product",
        "reason": outcome.get("reason"),
    }


def _launch_minutes_on_the_product_path(
    directory: Path,
    *,
    force: bool,
    deadline: float | None = None,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """产品路径下补纪要那一层：经**同一道** `_announce_minutes()` 解析实现并拉起，**等它落定**。

    走到这一格的现场是「离线段已是终态、纪要段没到终态」—— 离线链整段没跑，尾巴上那道
    通知因此也没走到，而纪要这一层的拉起点就挂在它上面。**不补这一层的话，纪要再也补不出来**
    （唯一的出路是 `--force`，那会连离线段一起重跑）：`meeting post <场次目录>` 对旧场次目录
    「缺什么补什么」这半条在产品路径上因此必须走这一格。

    拉起这一步**不是本函数自己的**：参数一路交给 `_announce_minutes()`（这里不替换任何模块
    全局属性），于是它自己的两道闸照旧管用 —— `MEETING_MINUTES_CHILD=1`（这一趟是 DSH 子会话
    自己）安静跳过，`minutes.hook` 已是终态且产物过判据时不调启动器。

    **这一场已经在飞时当场拒跑**：真实现（`backends/minutes.py::MinutesHook.launch`）在建线程
    之前先把「在飞」落到盘上，读到盘上停着的那一版 `running` 就抛 `AlreadyRunningError` ——
    `_announce_minutes()` 把它交回 `hook = running`，这一格据此**不等**（没有第二个终态会来），
    按现成的 `running` 拒跑档收场（`EXIT_REJECTED` + 点名状态文件 + `--force`）。`force` 那一趟
    走的是另一条：`--force` 一路传到 `launch(..., force=True)`，在飞判据跳过，照常重跑。

    **拉起 ≠ 落定，因此拉起成功之后要等这一层的终态**：真启动器起一条 daemon 线程就返回
    （`backends/minutes.py::MinutesHook.launch`），线程体第一件事是把状态文件落成 `running`，
    整条链要跑几十分钟。等待交给现成的 `_minutes_outcome()`（上界取现成的配置键
    `minutes.timeout_seconds`，事件驱动轮询、落定即返回），等到的终态再由
    `_settled_minutes_outcome()` **如实映射**成这一层的结论与退出码。

    拉起点整个缺席 / 被再入闸拦下 / 解析不出实现时**不等**：这一趟什么都没被拉起来，
    没有终态会来。**再入闸那一档（`skipped`）安静跳过** —— 走 `_minutes_from_launcher()`
    那一个出口，除 `launcher` 之外逐格与「注入假启动器的那一档」相同、不记失败（退出码按
    现成的 `_minutes_exit_code()` 走成功档），也不把再入闸的理由念成这一层的失败原因
    （`_quiet_skip_announce()`）。**缺席与解析不出实现这两档照旧按盘上那一刻的两半判据
    出结论**（状态文件 `done` **且** `meeting_final.md` 过得了那四条判据才算补上了），
    其余如实记失败。

    `baseline` 缺省（直接调本函数的读者）时**现取一份**（此刻的现场就是「拉起之前」）：
    产品路径那一趟由 `run_post()` 在离线那一段之前取好一路传下来，等待的判据因此三条路径同源。
    """
    if baseline is None:
        baseline = _minutes_baseline(directory)
    try:
        announce = offline_runner._announce_minutes(directory, force=force)
    except BaseException as exc:  # 纪要这一层炸了不许带走离线那三份产物
        try:
            _log().exception("会后纪要那一层没能拉起来：%s", directory)
        except Exception:  # pragma: no cover - 日志尚未初始化时只走终端
            pass
        return _minutes_from_launcher(
            directory,
            {"hook": "failed", "reason": f"{type(exc).__name__}: {exc}"},
            baseline=baseline,
            deadline=deadline,
        )

    outcome = dict(announce or {})
    if str(outcome.get("hook") or "") == "launched":
        return _settled_minutes_outcome(
            directory, outcome, deadline=deadline, baseline=baseline
        )

    if str(outcome.get("hook") or "") == LAYER_RUNNING:
        # **这一场已经在飞了**：`_announce_minutes()` 交回的 `running` 说的是「盘上那一版
        # 已经把这一层占住了」，这一趟没有再拉起子会话（`launch()` 的 claim 拒了它）。
        # 结论与「进门前就撞上 `running`」那一档同格，摘要据此取 `EXIT_REJECTED`。
        return _minutes_from_launcher(
            directory, outcome, baseline=baseline, deadline=deadline
        )

    if str(outcome.get("hook") or "") == "skipped":
        # **这一趟安静跳过**（`skipped` = 子会话里的再入闸拦下）：这一层没有被拉起来，
        # 也没有任何终态会来。结论走**注入那一档同一个出口**（`_minutes_from_launcher()`），
        # 于是「除 `launcher` 之外逐格相同」：`hook` / `state` / `skipped` / `announced` /
        # 那几条路径与等待秒数全与注入那一档一致，只有 `launcher` 那一格仍写 `product`
        # （这一趟走的是产品路径）—— 档位因此两条路对齐：**安静跳过不记失败**，退出码按
        # 现成的 `_minutes_exit_code()` 走成功档，不把再入闸的理由（「这一趟是 DSH 子会话
        # 自己」）念成这一层的失败原因。
        # 此前这一档掉进下面那段兜底里：`hook` 报成 `finished`、`state` 报成 `failed`、
        # 这一趟按 `EXIT_FAILURE` 收场，与注入那一档的 `skipped` / `EXIT_OK` 不对称。
        #
        # **`absent` 那一档不走这里**（`minutes.hook` 已配、解析却交回「没有实现」）：
        # 它与「配置里明确关掉这一层」是两件事，现成的用例把它钉在失败档上
        # （`tests/test_post_idempotency.py::test_the_product_path_still_reports_a_launch_that_fails`
        # 的 `no-implementation` 一格）。这一处只对齐再入闸那一档。
        return _minutes_from_launcher(
            directory,
            _quiet_skip_announce(outcome),
            baseline=baseline,
            deadline=deadline,
        )

    if _minutes_layer_done(directory):
        return _finished_minutes_outcome(directory, _minutes_status(directory))

    status = _minutes_status(directory)
    reason = str(outcome.get("reason") or "")
    if not reason:
        reason = (
            "纪要这一层没被拉起来（"
            f"{outcome.get('hook') or 'absent'}），终态是 {status.get('state') or '缺席'}、"
            "meeting_final.md 也没过那四条判据"
        )
    return {
        "hook": "finished",
        "state": LAYER_FAILED,
        "reason": reason,
        "launcher": str(outcome.get("selector") or "product"),
        "announced": bool(outcome.get("announced")),
        "status_path": str(directory / offline_runner.MINUTES_STATUS_NAME),
        "final": (
            str(directory / MINUTES_FINAL_NAME)
            if (directory / MINUTES_FINAL_NAME).is_file()
            else None
        ),
        "waited_seconds": 0.0,
        "skipped": False,
    }


def _settled_minutes_outcome(
    directory: Path,
    launch_outcome: dict[str, Any],
    *,
    deadline: float | None,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """拉起成功之后：**等这一层落终态**，再按等到的终态出结论。

    这一处等待是三条路径（首跑 / `--force` / 离线已终态只补纪要）**共用**的那一处
    （`_wait_for_settled_minutes()`）：判据是「盘上那份状态与拉起之前的 `baseline` 不同、
    并且已经是一个终态」，于是上一趟留在盘上的终态不会被当成这一趟的账。这一格只做
    「等到的终态 → 这一层的结论」这一段映射，于是三档各有自己的落点，谁也不写成别人那一档：

      - `done` 且产物过判据 → 这一层这一趟真的跑完并交出了产物（成功档）；
      - `done` 而产物没过那四条判据 → 这一层没交出纪要，如实记失败（**两半判据缺一不可**）；
      - `delivered` → **已交付 DSH web 服务端生成**：这一层的活到此为止，产物还没（或者
        不会）经这一层的四条判据校验 —— 交付那条路的语义就是「投递即完事」，因此这一档
        **不按失败算、也不等产物**（产物在 DSH GUI 那条会话里看得见）；
      - `failed` / 状态文件读不动 → 照它自己那一档如实记（`hook` / `state` / `reason` 原样带上）；
      - 到上界还没落定 → `timeout` 那一档：如实报「还没跑完」，不谎报成功、也不写成 `failed`。

    `skipped` 一律为假：走到这一格说明这一趟**真的把这一层拉起来了**，「无待办」那一句
    （「这一趟没有重跑任何一段」）在它身上不适用。
    """
    waited = _wait_for_settled_minutes(directory, baseline=baseline, deadline=deadline)
    row = dict(waited)
    state = str(row.get("state") or "")
    if state == LAYER_DELIVERED:
        row.update(
            hook=LAYER_DELIVERED,
            state=LAYER_DELIVERED,
            reason=(
                "已交付 DSH web 服务端生成（meeting_final.md 还没落盘）：这一层的活到此为止，"
                "产物在 DSH GUI 那条会话里看得见"
            ),
        )
    elif state == LAYER_DONE and not _minutes_layer_done(directory):
        row.update(
            hook="finished",
            state=LAYER_FAILED,
            reason=(
                "纪要状态文件落成 done，但 meeting_final.md 过不了那四条判据"
                "（终态与产物缺一不可）"
            ),
        )
    row.update(
        launcher=str(launch_outcome.get("selector") or "product"),
        announced=bool(launch_outcome.get("announced")),
        skipped=False,
    )
    return row


# ---------------------------------------------------------------------------
# 幂等闸：两层各自的「已完成」判据与那三种「不必跑」的结论
# ---------------------------------------------------------------------------


def _capturing_launcher(
    launcher: Any, announced: list[dict[str, Any]]
) -> Callable[[str], Any]:
    """把注入的假启动器包一层**只记账**的包装：它交回的那个对象原样记在 `announced` 里。

    「这一趟有没有再拉起子会话」因此在用例侧留下一份可核对的账（`launcher.calls`），
    而编排函数这一侧也拿得到假件的交回值（结论按它出）。**不碰任何模块全局属性**：
    这个包装只是 `run_session(..., minutes_launcher=…)` 的一个实参。
    """

    def wrapped(target: Any) -> Any:
        returned = launcher(str(target))
        announced.append({"returned": returned})
        return returned

    return wrapped


def _skipped_offline_outcome(
    directory: Path, status: dict[str, Any], *, running: bool = False
) -> dict[str, Any]:
    """离线段这一趟不跑时的结论（`running` 拒跑那一档与「离线段已是终态」那一档共用）。"""
    state = status.get("state")
    hook = LAYER_RUNNING if running else (LAYER_DONE if state == "done" else LAYER_ABSENT)
    return {
        "state": state,
        "hook": hook,
        "error": status.get("error"),
        "partial": bool(status.get("partial")),
        "sources": {},
        "transcripts": {},
        "skipped": True,
    }


def _skipped_minutes_outcome(
    directory: Path, status: dict[str, Any], *, running: bool, delivered: bool = False
) -> dict[str, Any]:
    """纪要段这一趟不跑时的结论（`running` 拒跑那一档与「已交付、不重复拉起」那一档共用）。"""
    final_path = directory / MINUTES_FINAL_NAME
    if running:
        hook, reason = LAYER_RUNNING, _running_reason(directory, status)
    elif delivered:
        hook, reason = LAYER_DELIVERED, _delivered_reason(directory, status)
    else:
        hook, reason = LAYER_ABSENT, None
    return {
        "hook": hook,
        "state": status.get("state"),
        "reason": reason,
        "status_path": str(directory / offline_runner.MINUTES_STATUS_NAME),
        "final": str(final_path) if final_path.is_file() else None,
        "waited_seconds": 0.0,
        "skipped": True,
    }


def _delivered_reason(directory: Path, status: dict[str, Any]) -> str:
    """「已交付、这一趟不重复拉起」那一档的提示：点名会话与出路（`--force`）。"""
    dsh = status.get("dsh")
    session_id = str(dsh.get("session_id") or "") if isinstance(dsh, dict) else ""
    where = f"（DSH 会话 {session_id}）" if session_id else ""
    return (
        f"{offline_runner.MINUTES_STATUS_NAME} 停在 {LAYER_DELIVERED}：这一场已经交付 "
        f"DSH web 服务端生成{where}，而 {MINUTES_FINAL_NAME} 还没落盘 —— "
        "这一趟不再拉起第二个会话；要重新交付加 --force"
    )


def _finished_offline_outcome(directory: Path, status: dict[str, Any]) -> dict[str, Any]:
    """「无待办」那一档里离线段的结论：它与真跑过一遍的结论**逐格同形**。

    `sources` 按本场声明的每一路从产物契约定死的名字补上 —— 判据要求三份产物齐，
    因此这一格里的每一条路径都真的在盘上。
    """
    row = _skipped_offline_outcome(directory, status)
    row.update(
        hook=LAYER_DONE,
        sources={
            str(source): {
                "transcript": f"meeting_offline_transcript_{source}.md",
                "speakers": f"meeting_offline_speakers_{source}.json",
                "report": "meeting_offline_report.md",
            }
            for source in offline_runner.declared_sources(directory)
        },
    )
    return row


def _finished_minutes_outcome(directory: Path, status: dict[str, Any]) -> dict[str, Any]:
    """「无待办」那一档里纪要段的结论（产物过判据，不拉起子会话）。

    `state` 那一格**照盘上那一份如实报**：产物过判据可能是 `done`（headless 那一趟校验过），
    也可能是 `delivered`（web 分支交付过、DSH 那边写完了）—— 这里不替它改词。
    """
    final_path = directory / MINUTES_FINAL_NAME
    return {
        "hook": "already_done",
        "state": str(status.get("state") or LAYER_DONE),
        "reason": "纪要已是终态且 meeting_final.md 过得了那四条判据，不再拉起子会话",
        "status_path": str(directory / offline_runner.MINUTES_STATUS_NAME),
        "final": str(final_path) if final_path.is_file() else None,
        "waited_seconds": 0.0,
        "skipped": True,
    }


def _running_reason(directory: Path, status: dict[str, Any]) -> str:
    """`running` 那一档的提示：**点名那一个状态文件**并给出 `--force` 那句话。"""
    return (
        f"{status.get('status_name')} 停在 running（上一趟还在跑，或者崩在半路）："
        f"这一趟拒绝执行；确认上一趟已经收工之后，加 --force 再来"
    )


def _layer_is_running(status: dict[str, Any], keys: tuple[str, ...]) -> bool:
    """这一层的状态文件是不是停在 `running`（上一趟还在跑 / 崩在半路留在那一档）。"""
    return any(str(status.get(key) or "") == LAYER_RUNNING for key in keys)


def _minutes_is_delivered(status: dict[str, Any]) -> bool:
    """纪要这一层是不是**已经交付给 DSH web 服务端**（终态 `delivered`）。

    web 分支的语义是「投递 accepted 即算交付完成」：交付那一版状态就此留在盘上，
    产物由 DSH 那边写。这一档因此既不算「正在跑」（没有本进程的线程在跑），也不算
    「已完成」（产物还没经这一层的四条判据）—— 它是**第三种**「不必再拉起」的情形：
    已经交付过，重复拉起只会让同一场生成两遍（`--force` 是重新交付的出路）。
    """
    return str(status.get("state") or "") == LAYER_DELIVERED


def _missing_names(directory: Path, names: Iterable[str]) -> list[str]:
    """交回 `names` 里**不在盘上**的那些（判据缺一不可的那一半）。"""
    return [str(name) for name in names if not (directory / str(name)).is_file()]


def _offline_artifacts_complete(directory: Path, status: dict[str, Any]) -> bool:
    """离线段的三份产物是不是都在：逐路的两份按本场声明的每一路各算一份，外加一份报告。"""
    names = ["meeting_offline_report.md"]
    for source in offline_runner.declared_sources(directory):
        names.append(f"meeting_offline_transcript_{source}.md")
        names.append(f"meeting_offline_speakers_{source}.json")
    return not _missing_names(directory, names)


def _offline_layer_done(directory: Path, status: dict[str, Any] | None = None) -> bool:
    """离线段是不是**已经完成**：状态文件 `done` **且**三份产物齐（缺一条就不算完成）。"""
    document = _offline_status(directory) if status is None else status
    if str(document.get("state") or "") != LAYER_DONE:
        return False
    return _offline_artifacts_complete(directory, document)


def _minutes_status(directory: Path) -> dict[str, Any]:
    """读 `meeting_minutes_status.json` —— **走纪要那一层自己的读方**（逐字段校验）。

    与 `meeting/offline/runner.py::_read_minutes_status()` 同一个口径：那份现成读方
    （`minutes.status.read_status()`）不认残文件，缺字段 / 版本不认识 / `state` 不在三档里
    一律拒收。读不动时这一层按「没跑完」算（`state` 交回 `None`），**不猜**。

    读方在**函数体内 import**：本模块对重依赖的惰性纪律一个字不动
    （`import meeting.cli` 仍然不拉 `meeting.minutes` 那一包）。
    """
    path = directory / offline_runner.MINUTES_STATUS_NAME
    if not path.is_file():
        return {"status_name": offline_runner.MINUTES_STATUS_NAME}
    try:
        from meeting.minutes.status import read_status

        document = read_status(path)
    except (ImportError, OSError, ValueError) as exc:
        return {
            "status_name": offline_runner.MINUTES_STATUS_NAME,
            "unreadable": f"{type(exc).__name__}: {exc}",
        }
    document["status_name"] = offline_runner.MINUTES_STATUS_NAME
    return document


def _minutes_layer_done(directory: Path, status: dict[str, Any] | None = None) -> bool:
    """纪要段是不是**已经完成**：`meeting_final.md` 过得了那四条判据。

    **判据只看产物，不看状态那一格**：`done`（headless 那一趟校验过）与 `delivered`
    （web 分支交付过、DSH 那边写完了）两种终态都可能对应「产物已经在盘上」，因此
    「这一场要不要补跑」由这四条判据回答。状态那一格管的是另外两件事：`running`
    （上一趟还在跑，拒跑）与 `delivered` 而产物还没落（已经交付过，不重复拉起）。

    产物那四条判据**复用现成的**（`minutes/launch.py::check_final_artifact()`，函数体内
    import），不在这里重写一份。
    """
    document = _minutes_status(directory) if status is None else status
    if str(document.get("state") or "") in (LAYER_RUNNING, LAYER_FAILED):
        # 停在 running / failed 的那一份谈不上「完成」（`failed` 那一档要让它再试）
        return False
    try:
        from meeting.minutes.launch import check_final_artifact

        missing = tuple(check_final_artifact(directory) or ())
    except Exception:  # pragma: no cover - 判据本身跑不动时按「没跑完」算
        return False
    return not missing


def _mark_offline_failed(directory: Path, error: str) -> None:
    """把 `meeting_offline_status.json` 补成 `failed`（**尽力而为**：补不上只留一条 log）。

    与 `meeting/backends/post.py` 的 `_mark_failed()` 同一个口径：先读回上一版、保留里面的
    场次编号 / 开始时刻 / 路清单 / 逐路产物，再写终态与原因。写盘方式走 `artifacts.py` 的
    `write_status()`（原子替换），**字段与取值域一个字不改**。
    """
    try:
        from meeting.offline import artifacts

        path = directory / artifacts.STATUS_NAME
        existing: dict[str, Any] = {}
        if path.exists():
            try:
                existing = artifacts.read_status(path)
            except artifacts.ArtifactContractError:
                existing = {}
        artifacts.write_status(
            path,
            session_id=str(existing.get("session_id") or directory.name),
            state=LAYER_FAILED,
            started_at=str(existing.get("started_at") or _now_iso()),
            sources=list(existing.get("sources") or []),
            artifacts=dict(existing.get("artifacts") or {}),
            finished_at=_now_iso(),
            error=str(error),
        )
    except Exception:  # pragma: no cover - 补写失败时不掩盖原来那条异常
        try:
            _log().exception("会后离线状态文件补写失败（原异常照上面那条 log）")
        except Exception:
            pass


def _now_iso() -> str:
    """现在的 ISO 时刻（与 `meeting/offline/runner.py` 的 `_now_iso()` 同形）。"""
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _failure_summary(
    directory: Path, archive: dict[str, Any], error: str
) -> dict[str, Any]:
    """编排函数整个抛出来时的那份摘要：这一趟按 `EXIT_FAILURE` 收场，现场照实报。"""
    return _summarize_run(
        {
            "session_id": directory.name,
            "session_dir": str(directory),
            "started_at": time.monotonic(),
            "archive": archive,
            "offline": {"hook": LAYER_FAILED, "state": None, "error": error, "sources": {}},
            "minutes": None,
            "artifacts": {},
        },
        error=error,
    )


# ---------------------------------------------------------------------------
# 音频归档补齐（PCM → MP3）
# ---------------------------------------------------------------------------


def sync_archive(
    session_dir: Path | str,
    *,
    sources: Iterable[str] | None = None,
    converter: Callable[[Path, Path], bool] | None = None,
) -> dict[str, Any]:
    """按收尾第 2 步同一套判据补齐两路的 MP3 归档，交回逐路记账。

    **转码实现只有一份**：`meeting/capture/archive.py` 的 `convert_to_mp3()` 与
    `mp3_path_for()`（函数体内 import，与本模块对重依赖的惰性纪律一致），这里只负责
    「该不该转」这一层判据 —— 连**空 PCM 那个阈值**也取它的 `EMPTY_PCM_BYTES`
    （`meeting_audio_*.mp3` 这个落点则由 `archive_mp3_path_for()` 转交给它的
    `mp3_path_for()`）：本模块不留第二份拷贝，那边换了值 / 换了形这里跟着走。

      - 该路 PCM 非空（字节数 `>= EMPTY_PCM_BYTES`）且（**MP3 缺席** 或 **MP3 比 PCM 旧**）
        → 转一次（`converted`）：`mtime` 上 MP3 比 PCM 新说明这一段音频已经归档过，
        R8 的「反复重跑」在它身上不该再花一次转码；
      - 空 PCM（0 / 1 字节，采集侧没选中的那一路就是这个形状）→ 不产 MP3、也不调转码
        （`no_work`：这一路本来就没有音频可归档）；
      - PCM 不在场 → 同样 `no_work`；
      - 转码抛错（`ffmpeg` 缺席、`pydub` 起不来这类）→ 该路记 `failed`，并把这一趟可能
        写了一半的 MP3 落盘字节**原样收回**（转码前的旧 MP3 内容与缺席状态都复原），
        **PCM 一个字节都不动**（不 unlink、不截断）。

    **两路各自独立判、独立转**：一路转不动不带停另一路（归档这一层的每一路都是自己的账）。
    `sources` 缺省取 `offline_runner.declared_sources()`（`session.json` 声明的那几路）；
    `converter` 是用例的接缝（缺省就是上面那一个真实现）。
    """
    directory = Path(session_dir)
    names = tuple(sources) if sources is not None else offline_runner.declared_sources(directory)
    apply_conversion = converter if converter is not None else _archive_converter()

    rows: list[dict[str, Any]] = []
    for source in names:
        pcm_path = directory / f"meeting_audio_{source}.pcm"
        mp3_path = archive_mp3_path_for(pcm_path)
        row: dict[str, Any] = {
            "source": str(source),
            "pcm": str(pcm_path.resolve()),
            "mp3": None,
            "outcome": OUTCOME_NO_WORK,
            "reason": None,
        }
        if not pcm_path.is_file():
            row["reason"] = "归档 PCM 不在场"
            rows.append(row)
            continue

        size = pcm_path.stat().st_size
        if size < EMPTY_PCM_BYTES:
            row["reason"] = f"归档 PCM 是 {size} 字节（空），不产 MP3"
            rows.append(row)
            continue

        if mp3_path.is_file() and mp3_path.stat().st_mtime > pcm_path.stat().st_mtime:
            row.update(
                mp3=str(mp3_path.resolve()),
                outcome=OUTCOME_NO_WORK,
                reason="MP3 已经比 PCM 新，不重复转",
            )
            rows.append(row)
            continue

        # 转码前把 MP3 的现场收好：转砸了要能原样放回去
        had_mp3 = mp3_path.is_file()
        payload = mp3_path.read_bytes() if had_mp3 else None
        try:
            produced = bool(apply_conversion(pcm_path, mp3_path))
        except Exception as exc:
            _restore_payload(mp3_path, payload)
            row.update(outcome=OUTCOME_FAILED, reason=f"{type(exc).__name__}: {exc}")
            try:
                _log().error("音频归档补齐失败：%s（%s）", pcm_path, row["reason"])
            except Exception:  # pragma: no cover - 日志尚未初始化时只留记账
                pass
            rows.append(row)
            continue

        if produced and mp3_path.is_file():
            row.update(mp3=str(mp3_path.resolve()), outcome=OUTCOME_CONVERTED)
        elif produced:  # pragma: no cover - 转码器说产出了却不见文件：如实记成失败
            row.update(
                outcome=OUTCOME_FAILED, reason="转码器报成功，但 MP3 没落盘"
            )
        else:
            row.update(outcome=OUTCOME_NO_WORK, reason="转码器按空音频处理，没有产出 MP3")
        rows.append(row)

    state = ARCHIVE_DONE
    if any(row["outcome"] == OUTCOME_FAILED for row in rows):
        state = ARCHIVE_FAILED
    elif all(row["outcome"] == OUTCOME_NO_WORK for row in rows):
        state = ARCHIVE_SKIPPED
    return {"state": state, "sources": rows}


def _archive_converter() -> Callable[[Path, Path], bool]:
    """真转码那一个函数（**函数体内 import**：本模块的惰性纪律）。"""
    from meeting.capture.archive import convert_to_mp3

    return convert_to_mp3


def archive_mp3_path_for(pcm_path: Path) -> Path:
    """这一路 PCM 的 MP3 归档落点 —— **求路径也只走那一份现成实现**。

    `meeting/capture/archive.py::mp3_path_for()` 是全仓唯一的求法（`with_suffix(".mp3")`
    那一份拷贝住在本模块里，将来那边换了形这边静默跟不上）。与 `_archive_converter()`
    同一条纪律：落地现在实现上，只在**函数体内** import。
    """
    from meeting.capture.archive import mp3_path_for

    return mp3_path_for(pcm_path)


def _restore_payload(path: Path, payload: bytes | None) -> None:
    """把转码前的那份 MP3 内容放回去（`payload is None` = 那一趟本来就没有 MP3）。"""
    try:
        if payload is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(payload)
    except OSError:  # pragma: no cover - 复原失败时下面的 log 里留着这一路 failed 的账
        try:
            _log().warning("转码失败后 MP3 的现场没能复原：%s", path, exc_info=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 每层的结论与产物
# ---------------------------------------------------------------------------


def _minutes_timeout_seconds() -> float:
    """等纪要那一层的墙钟上界（秒）：现成的纪要墙钟键 `minutes.timeout_seconds`。"""
    raw: Any = None
    try:
        raw = config_module.get("minutes.timeout_seconds")
    except Exception:  # pragma: no cover - 配置层还没起来时走默认表
        raw = None
    if raw is None:
        raw = config_module.DEFAULT_VALUES.get("minutes.timeout_seconds")
    try:
        value = float(raw)
    except (TypeError, ValueError):  # pragma: no cover - 表里那一格认不出来时的兜底
        return MINUTES_TIMEOUT_FALLBACK_SECONDS
    if value <= 0:  # pragma: no cover - 取值域由配置层把关，这里只兜残缺的配置对象
        return MINUTES_TIMEOUT_FALLBACK_SECONDS
    return value


def _offline_outcome(
    directory: Path,
    runner_result: dict[str, Any] | None,
    *,
    error: str | None = None,
    skipped: bool = False,
) -> dict[str, Any]:
    """离线那一层的结论：终态、摘要里那几格、以及逐路产物名（都带绝对路径）。

    分档按「盘上的终态」与「这一趟有没有抛出」两条一起来：状态文件落 `failed`、或者
    离线那一趟整个抛出来（`error` 有内容、状态文件可能压根没落）都记 `failed`；
    终态 `done` 记 `done`；盘上什么都没有才记 `absent`（产品路径上这一档只在
    `run_session` 还没跑过时出现）。

    `skipped = True` 时这一层**这一趟没跑**（幂等闸判它已是终态）：状态文件里已有的
    终态照样如实报出来，只多一格 `skipped` 说明这一趟没进引擎。
    """
    status = _offline_status(directory)
    state = status.get("state")
    failure = status.get("error") or error
    if state == "failed" or (state is None and error is not None):
        hook = LAYER_FAILED
    elif state == "done":
        hook = LAYER_DONE
    else:
        hook = LAYER_ABSENT
    row: dict[str, Any] = {
        "state": state,
        "hook": hook,
        "error": failure,
        "partial": bool(status.get("partial")),
        "sources": {},
        "transcripts": {},
        "skipped": bool(skipped),
    }
    summary = runner_result if isinstance(runner_result, dict) else {}
    if summary.get("session_dir"):
        row["session_dir"] = str(summary["session_dir"])
    if summary.get("report"):
        row["report"] = str(summary["report"])
    if summary.get("status"):
        row["status"] = str(summary["status"])
    if isinstance(summary.get("minutes"), dict):
        row["announce"] = summary["minutes"]
    per_source = summary.get("transcripts")
    if isinstance(per_source, dict):
        for source, item in per_source.items():
            if not isinstance(item, dict):
                continue
            row["transcripts"][str(source)] = item
            row["sources"][str(source)] = {
                key: item.get(key)
                for key in ("transcript", "speakers", "report")
                if item.get(key)
            }
    if not row["sources"]:
        # 离线那一趟没交回摘要（整体抛出这类）时，按产物契约定死的名字补上那几格
        row["sources"] = {
            str(source): {
                "transcript": f"meeting_offline_transcript_{source}.md",
                "speakers": f"meeting_offline_speakers_{source}.json",
                "report": "meeting_offline_report.md",
            }
            for source in offline_runner.declared_sources(directory)
        }
    return row


def _offline_status(directory: Path) -> dict[str, Any]:
    """读 `meeting_offline_status.json`（不在场 / 读不动时交回空映射，不抛）。

    交回的那一份多一格 `status_name`（这一个状态文件的名字）：幂等闸在 `running` 那一档
    的提示里要点名**是哪一个**状态文件，那一句话的取值来源就是这一格。
    """
    import json

    path = directory / offline_runner.artifacts.STATUS_NAME
    document: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = None
        if isinstance(loaded, dict):
            document = loaded
    document["status_name"] = offline_runner.artifacts.STATUS_NAME
    return document


def _minutes_from_launcher(
    directory: Path,
    announce: Any,
    *,
    baseline: dict[str, Any],
    deadline: float | None = None,
) -> dict[str, Any]:
    """交回值是离线链尾巴（或产品路径）那道通知时，这一层的结论怎么出。

    「这一趟有没有再拉起一次子会话」在这一档是可核对的记账（假启动器自己记），
    交回的结果说明这一趟走到哪一档；分档只看拉起这一步自己的档位（`hook`），**不看是谁
    拉起的** —— 真实现与注入的假件在时序上同形（`launch()` 返回即算启动成功，终态由后台
    那条线程或子进程写进 `meeting_minutes_status.json`）：

      - `launched`：启动器被调到，**拉起不等于落定** —— 这一档一律**等这一趟新落的终态**
        （`_wait_for_settled_minutes()`，判据落在 `baseline` 上）：首跑 / `--force` /
        离线已终态只补纪要三条产品路径、以及注入假件那一档**共用同一处等待**，一处分支
        都不留；等不到就如实报 `timeout`，既不谎报成功也不写成 `failed`。
      - `already_done`：闸拦下（这一趟**没有再拉起**），结论就在盘上那份终态里；
      - `skipped` / `absent`：子会话闸 / `minutes.hook` 缺席（后一半才看 `state`）；
      - `failed`：纪要这一层自己抛出异常（没有终态会来，不等）。

    交回值里那一格 `state` **不当结论**：拉起那一刻它答不出「落定没有」，而盘上与
    `baseline` 相同的那一份是**上一趟**的账。这一层的结论只从「等到的这一版」或
    「闸拦下时读到的这一版」来。

    `announce` 缺席（离线那一趟没走到尾钩）时交回 `None` 那一档，由摘要按缺结论报。

    摘要里 `launcher` 那一格说的是「这一层的结论是谁拉起来的那一档给的」：`announce` 自己
    带了这一格时**原样留着**（产品路径的安静跳过那一档由 `_quiet_skip_announce()` 明写
    `product`），没带才按缺省的 `injected` 算。
    """
    outcome = announce if isinstance(announce, dict) else None
    status_path = directory / offline_runner.MINUTES_STATUS_NAME
    final_path = directory / MINUTES_FINAL_NAME
    missing = object()
    provided_launcher = outcome.get("launcher", missing) if outcome else missing
    result: dict[str, Any] = {
        "hook": "injected",
        "state": None,
        "reason": None,
        "launcher": (
            "injected" if provided_launcher is missing else str(provided_launcher)
        ),
        "status_path": str(status_path),
        "final": str(final_path) if final_path.is_file() else None,
        "waited_seconds": 0.0,
    }
    if outcome is None:
        result["reason"] = "离线那一趟没走到纪要钩子，这一趟没有纪要那一层的结论"
        return result

    hook = str(outcome.get("hook") or "")
    returned = outcome.get("returned")
    state = outcome.get("state")
    if hook == "launched":
        # **拉起不等于落定**，而这与「谁拉起的」无关：真实现起一条 daemon 线程就返回
        # （`backends/minutes.py::MinutesHook.launch`），注入的假启动器在时序上同形 ——
        # 两者的终态都只由盘上那份状态文件回答。因此这一档一律等**这一趟新落的终态**
        # （`_wait_for_settled_minutes()`：判据落在 `baseline` 上），等不到就如实报
        # `timeout`；交回值里那一格 `state` **不**当结论（拉起那一刻它答不出落定没有），
        # 盘上留下的上一趟终态也不当这一趟的账。
        waited = _wait_for_settled_minutes(
            directory, baseline=baseline, deadline=deadline
        )
        result.update(
            hook=waited["hook"],
            state=waited["state"],
            reason=waited["reason"],
            final=waited["final"],
            waited_seconds=waited["waited_seconds"],
            announced=bool(outcome.get("announced", True)),
        )
        return result

    failed = getattr(returned, "failure", None) is not None or (
        isinstance(returned, dict) and bool(returned.get("failure"))
    )
    if hook == "already_done":
        result.update(
            hook=hook,
            # `state` 照盘上那一份如实报（`done` 或 `delivered` 都可能落进这一档）
            state=str(_minutes_status(directory).get("state") or LAYER_DONE),
            announced=False,
            reason=outcome.get("reason") or result["reason"],
        )
    elif hook == LAYER_DELIVERED:
        # **这一场已经交付过**（盘上那一版状态停在 `delivered`、产物还没落）：
        # `_minutes_gate()` 据此没有拉起第二个会话。结论与「进门前就撞上 delivered」
        # 那一档同格（`_skipped_minutes_outcome()`：`hook = delivered`）。
        result.update(
            _skipped_minutes_outcome(
                directory, _minutes_status(directory), running=False, delivered=True
            )
        )
        result["launcher"] = "product"
        result["announced"] = False
        result["reason"] = outcome.get("reason") or result.get("reason")
    elif hook == LAYER_RUNNING:
        # **这一场已经在飞了**（`minutes.status.mark_running()` 的 claim 读到盘上那一版
        # `running`）：`launch()` 当场拒了这一次拉起，这一趟没有起线程、没有拉起子会话。
        # 结论与「进门前就撞上 `running`」那一档**同格**（`_skipped_minutes_outcome()`：
        # `hook = running`、理由点名状态文件并给出 `--force`），摘要按现成的 `running`
        # 拒跑档收场（`_summarize_run()` 见到 `hook == running` 就取 `EXIT_REJECTED`）。
        result.update(
            _skipped_minutes_outcome(
                directory, _minutes_status(directory), running=True
            )
        )
        result["launcher"] = "product"
        result["announced"] = False
        result["reason"] = outcome.get("reason") or result.get("reason")
    elif hook in ("skipped", "absent"):
        result.update(hook=hook, state=None, announced=False, reason=outcome.get("reason"))
    elif hook == "failed":
        result.update(hook=hook, state=LAYER_FAILED, announced=False, reason=outcome.get("reason"))
    else:
        # 交回值缺 `announced` 那一格时按「真的拉起过」算；闸拦下那一档的 `announced`
        # 是显式的 `False`（那一条路不发 `returned`）。
        result.update(
            hook=hook or "launched",
            state=state if state is not None else (LAYER_FAILED if failed else LAYER_DONE),
            announced=bool(outcome.get("announced", True)),
            reason=(
                f"假启动器报了这一档：{getattr(returned, 'failure', None)}" if failed else None
            ),
        )
    return result


def _minutes_outcome(
    directory: Path,
    *,
    deadline: float | None = None,
    timeout: float | None = None,
    poll_seconds: float = MINUTES_POLL_SECONDS,
    launched: bool = False,
) -> dict[str, Any]:
    """纪要那一层的结论：等它的**终态**落定，再如实报出来。

    等待的上界 `timeout` 缺省取现成的纪要墙钟键 `minutes.timeout_seconds`；编排函数还会把
    它自己那一趟的绝对时刻 `deadline` 传下来，两者取更早的那一个。**这个等待是这一层唯一
    读得到的完成通知**：启动器起的是 daemon 线程（`launch()` 返回即算启动成功），终态由那条
    线程或一个 headless 子进程写进 `meeting_minutes_status.json` —— 编排函数手里没有可 join
    的句柄、也没有跨进程的回调，于是只能盯着盘上那一份文件，每读一次之间让出 `poll_seconds`
    （缺省 50 毫秒）。落定就立刻返回，不等满上界；到点还没落定就以 `timeout` 收场。

    `launched = True` 是「这一趟刚把启动器拉起来、正等它交终态」那一档：那一刻状态文件可能
    **还没出现**（真启动器起的是 daemon 线程，线程体写第一版 `running` 要等一小会儿），
    因此这一档把「文件还没出现」也算成「还没落定」、接着等到它出现或到点。缺省 `False`
    （读者只想知道盘上现状）照旧把缺席当成 `absent` 当场交回，不空等。

    落定之后按状态文件分档：

      - `already_done`：这一层的终态早就是 `done`、`meeting_final.md` 也过得了那四条判据，
        离线那一趟尾巴上的闸因此**没调启动器**（R8 的「反复重跑不再拉起子会话」在此可读）；
      - `absent`：这一层这一趟没有留下状态文件（`minutes.hook` 缺席或子会话闸那一档）；
      - `finished`：按状态文件的终态分档（`done` / `failed`），`state` 就是那一档；
      - `unreadable` / `timeout`：状态文件读不动（坏 JSON、缺字段）/ 到点还没落定。
    """
    status_path = directory / offline_runner.MINUTES_STATUS_NAME
    final_path = directory / MINUTES_FINAL_NAME
    budget = _minutes_timeout_seconds() if timeout is None else float(timeout)

    if not status_path.is_file() and not launched:
        if final_path.is_file():
            return {
                "hook": "finished",
                "state": "done",
                "reason": None,
                "status_path": str(status_path.resolve()),
                "final": str(final_path.resolve()),
                "waited_seconds": 0.0,
            }
        return {
            "hook": "absent",
            "state": None,
            "reason": "纪要那一层这一趟没有留下状态文件",
            "status_path": str(status_path.resolve()),
            "final": None,
            "waited_seconds": 0.0,
        }

    begun = time.monotonic()
    state = _read_minutes_state(status_path)

    def unsettled() -> bool:
        """盘上那一刻还看不出终态吗（`launched` 那一档：文件还没出现也算还没落定）。"""
        if not status_path.is_file():
            return bool(launched)
        return state == LAYER_RUNNING

    while unsettled():
        remaining = _remaining_seconds(begun, budget, deadline)
        if remaining <= 0.0:
            state = "timeout"
            break
        time.sleep(min(float(poll_seconds), remaining))
        state = _read_minutes_state(status_path)

    if state == "timeout":
        reason = (
            f"等纪要终态超过 {budget:.0f} 秒（上界取 minutes.timeout_seconds）还没落定"
        )
    elif state == "unreadable":
        reason = "纪要状态文件读不动（坏 JSON 或缺字段）"
    elif state == LAYER_FAILED:
        reason = "纪要那一层落 failed（详见 meeting_minutes_status.json 与 working/minutes-run.log）"
    else:
        reason = None
    return {
        "hook": state if state in ("unreadable", "timeout") else "finished",
        "state": state,
        "reason": reason,
        "status_path": str(status_path.resolve()),
        "final": str(final_path.resolve()) if final_path.is_file() else None,
        "waited_seconds": round(time.monotonic() - begun, 3),
    }


def _remaining_seconds(begun: float, budget: float, deadline: float | None) -> float:
    """等纪要还剩多少秒：`budget` 与 `deadline` 里更早的那一个说了算。"""
    remaining = float(budget) - (time.monotonic() - begun)
    if deadline is not None:
        remaining = min(remaining, float(deadline) - time.monotonic())
    return remaining



def _read_minutes_state(status_path: Path) -> str | None:
    """纪要状态文件里的 `state`；文件不在 / JSON 坏 / 缺字段一律交回「读不动」。"""
    import json

    try:
        document = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unreadable"
    if not isinstance(document, dict) or not document.get("state"):
        return "unreadable"
    return str(document["state"])


def _minutes_file_mark(path: Path) -> tuple[int, int] | None:
    """状态文件这一版的「新写没新写」标记：`(mtime_ns, size)`；文件不在场时交回 `None`。

    纪要那一层的写方是**原子替换**（`minutes/status.py::write_status()` 经
    `write_text_atomic()`，每次落盘都换一份新文件），因此这一对值在「重新写了一份」时
    一定会变。用它就够分辨「上一趟留下的那一版」与「这一趟新落的这一版」，
    不需要新增任何配置键、也不改纪要那一层的契约。
    """
    try:
        info = path.stat()
    except OSError:
        return None
    return (int(info.st_mtime_ns), int(info.st_size))


def _minutes_baseline(directory: Path) -> dict[str, Any]:
    """**拉起之前**取一份纪要状态文件的现场快照（等待的判据就落在它与之后的差别上）。

    三格：文件在不在、这一版落的是什么 `state`、以及这一版自己的标记
    （`_minutes_file_mark()`）。拉起之后等到「标记变了、并且已经是一个终态」就算这一趟
    落定 —— 盘上留着上一趟的终态（`failed`，或 `done` 而产物不在）时，它不会被当成
    这一趟的账。

    这一份只读盘、不改任何文件；取的时刻是「离线那一段跑起来之前」（`run_post()` 里
    幂等闸读完现状之后），因此三条路径拿到的都是同一种基线。
    """
    status_path = directory / offline_runner.MINUTES_STATUS_NAME
    return {
        "exists": status_path.is_file(),
        "state": _read_minutes_state(status_path),
        "mark": _minutes_file_mark(status_path),
    }


def _minutes_settled(
    state: str | None, mark: tuple[int, int] | None, baseline: dict[str, Any]
) -> bool:
    """盘上这一版是不是**这一趟新落的终态**（等纪要终态那条判据）。

    两半缺一不可：

      - **新落的**：这一版的标记与基线那一版不同（基线里文件不在场时，出现了就算新）；
      - **终态**：`state` 已经落在 `running` 之外，并且这一版**读得出内容** —— 标记变了而
        状态读不出来（空文件 / 坏 JSON）时按「还在写」接着等：纪要那一层的写方是原子替换
        （先写独占的临时文件再 `os.replace`），读方偶尔会撞上换文件那一瞬间（本机实测到过
        一次：刚换上去的那一版被读成空，当场报成 `unreadable`）。真是一份读不动的残文件时，
        等下去同样有落点：到上界如实报 `timeout`（它确实还没落定）。

    上一趟留在盘上的终态标记与基线相同，因此**不**算落定 —— 那正是「只补纪要」那一趟
    原来 0.17 毫秒就收场的那一格。
    """
    if mark == baseline.get("mark"):
        return False
    if not mark or not int(mark[1]):
        return False
    return str(state or "") not in (LAYER_RUNNING, "unreadable")


def _wait_for_settled_minutes(
    directory: Path,
    *,
    baseline: dict[str, Any],
    deadline: float | None = None,
    timeout: float | None = None,
    poll_seconds: float = MINUTES_POLL_SECONDS,
) -> dict[str, Any]:
    """拉起纪要那一层之后：**等这一趟新落的终态**，再如实报出来。

    三条路径（首跑 / `--force` / 离线已终态只补纪要）**共用这一处等待**：拉起这一步自己的
    契约是「返回即算启动成功」（`backends/minutes.py::MinutesHook.launch` 起一条 daemon
    线程就返回），终态由那条线程写进 `meeting_minutes_status.json`，编排函数手里没有可
    join 的句柄，于是等待就落在「盯盘上那一份文件」上：每读一次之间让出 `poll_seconds`
    （缺省 50 毫秒），落定就立刻返回，不等满上界。

    上界取现成的纪要墙钟键 `minutes.timeout_seconds`（`timeout` 缺省时读它），编排函数另把
    自己那一趟的绝对时刻 `deadline` 传下来，两者取更早的那一个。到点还没落定 → `timeout`
    那一档：如实报「还在跑」，既不写成功也不写 `failed`。

    落定的那一版按 `state` 分档（与 `_minutes_outcome()` 同一套口径）：

      - `done` / `failed` → `hook="finished"`、`state` 就是那一档；
      - `unreadable` → 状态文件读不动（坏 JSON 或缺字段，且到上界都没换成一份读得动的）；
      - `timeout` → 到上界时那一版还是 `running`，或者连一份读得动的状态文件都还没出现
        （`_minutes_settled()` 里那两半：新落的、且读得出一个终态）。
    """
    status_path = directory / offline_runner.MINUTES_STATUS_NAME
    final_path = directory / MINUTES_FINAL_NAME
    budget = _minutes_timeout_seconds() if timeout is None else float(timeout)

    begun = time.monotonic()
    state = _read_minutes_state(status_path)
    mark = _minutes_file_mark(status_path)
    while not _minutes_settled(state, mark, baseline):
        remaining = _remaining_seconds(begun, budget, deadline)
        if remaining <= 0.0:
            state = "timeout"
            break
        time.sleep(min(float(poll_seconds), remaining))
        state = _read_minutes_state(status_path)
        mark = _minutes_file_mark(status_path)

    if state == "timeout":
        reason = (
            f"等纪要终态超过 {budget:.0f} 秒（上界取 minutes.timeout_seconds）还没落定"
        )
    elif state == "unreadable":
        reason = "纪要状态文件读不动（坏 JSON 或缺字段）"
    elif state == LAYER_FAILED:
        reason = "纪要那一层落 failed（详见 meeting_minutes_status.json 与 working/minutes-run.log）"
    else:
        reason = None
    return {
        "hook": state if state in ("unreadable", "timeout") else "finished",
        "state": state,
        "reason": reason,
        "status_path": str(status_path.resolve()),
        "final": str(final_path.resolve()) if final_path.is_file() else None,
        "waited_seconds": round(time.monotonic() - begun, 3),
    }


def _minutes_exit_code(minutes: dict[str, Any]) -> int:
    """纪要那一层值不值得让这一趟以 `EXIT_FAILURE` 收场。

    「这一场本来就没打算出纪要」（`minutes.hook` 缺席）与「子会话闸拦下了」（`skipped`）
    两档都按成功算；**「已交付 DSH web 服务端」（`delivered`）也按成功算** —— 交付这一步
    做成了，后面的活不归这一层管；等超时、状态文件读不动、以及真实的 `failed` 三档如实记失败。
    """
    hook = minutes.get("hook")
    state = minutes.get("state")
    if hook == "already_done":
        return EXIT_OK
    if hook in ("absent", "skipped"):
        return EXIT_OK
    if hook == LAYER_DELIVERED or state == LAYER_DELIVERED:
        return EXIT_OK
    if hook in ("unreadable", "timeout"):
        return EXIT_FAILURE
    if state == LAYER_FAILED:
        return EXIT_FAILURE
    return EXIT_OK


def _artifact_paths(directory: Path) -> dict[str, str]:
    """这一趟该报出来的产物（**逐条绝对路径**；不在场的产物不列）。"""
    names = [
        (offline_runner.artifacts.STATUS_NAME, "会后离线状态文件"),
        ("meeting_offline_report.md", "会后离线运行报告"),
        (offline_runner.MINUTES_STATUS_NAME, "会后纪要状态文件"),
        (MINUTES_FINAL_NAME, "会后纪要"),
    ]
    for source in offline_runner.declared_sources(directory):
        names.append((f"meeting_offline_transcript_{source}.md", f"{source} 的离线转写"))
        names.append((f"meeting_offline_speakers_{source}.json", f"{source} 的说话人对照表"))
    for source in offline_runner.declared_sources(directory):
        names.append((f"meeting_audio_{source}.mp3", f"{source} 的归档 MP3"))
    found: dict[str, str] = {}
    for name, label in names:
        path = directory / name
        if path.is_file():
            found[label] = str(path.resolve())
    return found


def _summarize_run(result: dict[str, Any], *, error: str | None = None) -> dict[str, Any]:
    """把三层的结果汇总成一份摘要：结论、逐条产物绝对路径、退出码。

    退出码**按三层的结论在这里一起判**（调用方只写每层的结论，不预先钉死这一格）：

      - 编排函数整个抛出来（`error`）、归档那一层 `failed`、离线那一层 `failed` → `EXIT_FAILURE`；
      - 任一层停在 `running`（幂等闸拒跑那一档）→ `EXIT_REJECTED`；
      - 其余按纪要那一层的结论取（`_minutes_exit_code()`），两层都收干净时就是 `EXIT_OK`。

    归档那一层 `failed` 与 `running` 拒跑两档的先后**不是随意的**：前者说的是「这一趟真去转了、
    没转成」，后者说的是「这一趟压根没被允许跑」，两档同时在场时以拒跑那一档收场。

    **退出码与「结论：」那一行分两格判**（复合现场两句话都说得对）：退出码按上面那条先后
    取（拒跑优先），而 `state` 照实写这一趟落在了哪一层 —— 归档转不动 + 某层停在 `running`
    时因此是「退出码 `EXIT_REJECTED` + 结论 `failed`」：终端既点名那个状态文件、给出
    `--force` 这句话，也不把归档那一层的账吞掉。

    **`EXIT_OK` 的前置是「这一趟要做的都做成了」**：归档 `failed`、纪要缺席 / 没落终态几档
    因此都不会走到 `EXIT_OK`（那一格不再由调用方预先写死，写死过的那两处正是谎报的来源）。

    摘要那一格 `state` 是终端上「结论：」那一行念的词，因此**不与退出码脱钩、也不替某一层
    改口**：纪要那一层到上界还没落定时（`hook == "timeout"`）如实写 `timeout`，不写成
    `failed` —— 它还在跑，说成失败与说成成功一样是谎报（退出码仍然不是成功档）。
    """
    directory = Path(str(result["session_dir"]))
    archive = result.get("archive") or {}
    offline = result.get("offline") or {}
    minutes = result.get("minutes") or {}
    archive_failed = archive.get("state") == ARCHIVE_FAILED
    offline_failed = offline.get("hook") == LAYER_FAILED
    rejected = _layer_is_running(offline, ("hook",)) or _layer_is_running(
        minutes, ("hook",)
    )

    if rejected:  # 幂等闸拒跑那一档：这一趟压根没被允许跑，退出码与终端提示都走它
        exit_code = EXIT_REJECTED
    elif error is not None or archive_failed or offline_failed:
        exit_code = EXIT_FAILURE
    elif minutes:
        exit_code = _minutes_exit_code(minutes)
    else:
        exit_code = EXIT_OK

    # 「结论：」那一行与退出码各说各的一格：复合现场（归档转不动 + 某层停在 `running`）时
    # 退出码走拒跑那一档（终端因此点名状态文件并给出 `--force`），而结论如实写这一趟的落点。
    if error is not None or archive_failed or offline_failed:
        state = LAYER_FAILED
    elif exit_code == EXIT_OK:
        state = LAYER_DONE
    elif exit_code == EXIT_REJECTED:
        state = LAYER_RUNNING
    elif minutes.get("hook") == "timeout":
        # 纪要那一层到上界还没落定：这一趟如实报「还没跑完」它自己那一档，不写成 `failed`
        #（它的退出码由 `_minutes_exit_code()` 给，照样不是成功档）。
        state = "timeout"
    else:
        state = LAYER_FAILED

    reasons = [
        str(item)
        for item in (
            error,
            offline.get("error") if offline_failed else None,
            minutes.get("reason") if exit_code != EXIT_OK else None,
        )
        if item
    ]
    result.update(
        state=state,
        ok=exit_code == EXIT_OK,
        exit_code=exit_code,
        exit_code_name=_exit_name(exit_code),
        error="；".join(reasons) or None,
        elapsed_seconds=round(time.monotonic() - float(result.get("started_at") or 0.0), 3),
        artifacts=result.get("artifacts") or _artifact_paths(directory),
    )
    return result


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
    for name, value in EXIT_NAMES:
        if value == code:
            return name
    return f"未知退出码 {code}"  # pragma: no cover - 五档之外不自造


__all__ = [
    "ACTIONS",
    "ARCHIVE_DONE",
    "ARCHIVE_FAILED",
    "ARCHIVE_SKIPPED",
    "EMPTY_PCM_BYTES",
    "EXIT_FAILURE",
    "EXIT_INTERRUPTED",
    "EXIT_NAMES",
    "EXIT_OK",
    "EXIT_REJECTED",
    "EXIT_STARTUP_UNMET",
    "LOG_TAIL_LINES_ON_FAILURE",
    "MINUTES_FINAL_NAME",
    "MINUTES_POLL_SECONDS",
    "MINUTES_TIMEOUT_FALLBACK_SECONDS",
    "OUTCOME_CONVERTED",
    "OUTCOME_FAILED",
    "OUTCOME_NO_WORK",
    "POST_FLAGS",
    "POST_STEPS",
    "POST_USAGE",
    "USAGE",
    "main",
    "post",
    "run_post",
    "sync_archive",
    "web",
]
