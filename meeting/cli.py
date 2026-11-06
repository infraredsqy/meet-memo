"""命令行入口（phase1 §3.3 / §3.4 / R15）。

命令词三个：`meeting web` / `meeting post` / `meeting replay`——每条仍是 `meeting <动词>`
的两词命令形（R15）。`replay` 是重放验证工具（`a3b684bd`），只验证、不是第二条生产路径。

参数面走 argparse（`_Parser`）：三个动作词各有一个解析器，`-h` / `--help` 打印说明后按
`EXIT_OK` 收场；用法错不由 argparse 直接退（它默认打英文模板），而是抛 `ValueError`
交回入口函数打用法说明、按 `EXIT_REJECTED` 收场——退出码与终端口径都不变。动作词的路由
留在 `main()`：`post()` / `replay()` 是直接可调的入口（用例 seam），各自解析各自的串。

五个退出码常量在本模块模块级定义（§3.4 冻结表），每个出口一律 `sys.exit(<常量>)`，
log 写常量名——本模块不出现裸数字。`config.py` / `logging_setup.py` 不定义退出码常量，
只抛已定的异常，由 `main()` 映射。配置与日志的时序四步定死（§3.5 第 1 条，不得对调）：
① 默认日志目录起 bootstrap 日志 → ② 读配置并值级校验 → ③ 按生效配置重建服务级日志 →
④ 生效 `log.dir` 写不进去即「主 log 不可写」。`meeting post` 的编排收在 `run_post()`：
**编排只有这一条入口**——收尾第 4 步不拉起任何会后动作，「生成会后精细稿」按钮走
`run_session` 直路。服务器信号接管必须在服务器运行入口之前调用。
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, NoReturn, Sequence

from meeting import config as config_module
from meeting import logging_setup
from meeting.capture import transcode as transcode_module
from meeting.offline import progress as offline_progress
from meeting.offline import replay as offline_replay
from meeting.offline import runner as offline_runner
from meeting.offline.artifacts import (
    REPORT_NAME,
    SPEAKERS_NAME_TEMPLATE,
    TRANSCRIPT_NAME_TEMPLATE,
)
from meeting.service import app as service_app
from meeting.session import (
    archive_name,
    segment_archive_paths,
)

# 退出码表（§3.4 冻结：标识符与取值都是冻结项）

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

ACTIONS = ("web", "post", "replay")

#: `post` 认的开关（**只有** `--force` 一个；它是唯一会再次花钱的开关）
POST_FLAGS: tuple[str, ...] = ("--force",)

#: `replay` 认的开关（三个都带取值）
REPLAY_FLAGS: tuple[str, ...] = ("--seconds", "--chunks", "--out")

#: 主解析器的描述（动作词清单与各自的一句话；RawDescription 原样排版）
MAIN_DESCRIPTION = """\
本地会议录制服务。动作词三个，每条都是 `meeting <动作词>` 的两词命令形（R15）：

  web     起本地服务（只监听 127.0.0.1），前台跑到 Ctrl+C
  post    只跑会后那一套：音频归档补齐 -> 会后离线 -> 会后纪要，跑完就退
  replay  拿旧录音全速重跑离线链（验证用）：只验证、不是第二条生产路径

每条命令都认 -h / --help，看各自那份更细的用法，例如 `meeting post --help`。
"""

#: 主解析器的 epilog：五个退出码与各自触发条件（口径 = README「退出码」节的正本；
#: 逐档触发条件与 Ctrl+C 两阶段语义见 docs/service.md）
EXIT_CODE_EPILOG = """\
退出码：
  0    正常收场（空闲态按 Ctrl+C 收场，或 `meeting post` 收尾干净）
  1    收尾有步骤 failed（或 post / replay 那一趟没做成、服务起步出意外）
  2    端口被占、用法错误、配置值非法
  3    启动前提不成立（config.json 损坏、主 log 不可写）
  130  收尾中途第二次 Ctrl+C（立即中止，盘上绝不写 done）
"""

# ---------------------------------------------------------------------------
# argparse 参数面（用法说明的正本：`-h` / `--help` 与用法错时打的就是这一份）
# ---------------------------------------------------------------------------


class _UsageHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """help 排版：描述与 epilog 原样保留（RawDescription）；分组标题与前缀换中文。"""

    _SECTION_TITLES = {
        "positional arguments": "位置参数",
        "options": "选项",
        "optional arguments": "选项",
    }

    def add_usage(self, usage: Any, actions: Any, groups: Any, prefix: Any = None) -> None:
        super().add_usage(usage, actions, groups, "用法：" if prefix is None else prefix)

    def start_section(self, heading: str) -> None:
        super().start_section(self._SECTION_TITLES.get(heading, heading))


class _Parser(argparse.ArgumentParser):
    """本 CLI 的 argparse 解析器：用法错不进 argparse 的默认出口。

    argparse 默认的用法错出口是打一句英文模板然后按退出码 2 收场——退出码碰巧同
    `EXIT_REJECTED`，但终端口径不是。这里改成 `error()` 抛 `ValueError`（那句话就是
    终端要打的原因），由入口函数打印用法说明并按 `EXIT_REJECTED` 收场；`-h` / `--help`
    仍走 argparse 本来的出口（打印说明、按 `EXIT_OK` 退）。`allow_abbrev` 关掉：
    `--for` 这类缩写展开不是既有行为（手写解析那一版不认缩写，改了就是静默变行为）。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["add_help"] = False
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)
        self.add_argument(
            "-h",
            "--help",
            action="help",
            default=argparse.SUPPRESS,
            help="打印这份说明并退出（退出码 0）",
        )
        # 用法错文案的两半句，由各 builder 按自己的参数面填
        self.flags_label = "本轮不认任何开关"
        self.positional_label = ""

    def error(self, message: str) -> NoReturn:
        raise ValueError(self._zh_message(message))

    def _zh_message(self, message: str) -> str:
        """把 argparse 的英文模板翻成终端一直用的那几句中文（只翻本 CLI 会产生的几条）。

        认不出的模板原样放行——与其翻错不如照实说。模板清单按 Python 3.13 的
        argparse 实测（`tests/test_cli_post.py` 的参数面用例逐条压着这几句话）。
        """
        if message.startswith("the following arguments are required: "):
            name = message.removeprefix("the following arguments are required: ")
            return f"缺少位置参数 <{name}>"
        matched = re.fullmatch(r"unrecognized arguments: (.+)", message)
        if matched is not None:
            extras = matched.group(1).split()
            flag = next((item for item in extras if item.startswith("-")), None)
            if flag is not None:
                return f"未定义的开关：{flag!r}（{self.flags_label}）"
            if self.positional_label:
                return (
                    f"位置参数只收一个 <{self.positional_label}>，收到 "
                    + "、".join(repr(item) for item in extras)
                )
            return f"未识别的参数：{matched.group(1)}"
        matched = re.fullmatch(r"argument (.+?): (.+)", message)
        if matched is not None:
            name, rest = matched.group(1), matched.group(2)
            if rest == "expected one argument":
                return f"{name} 缺少取值"
            if rest.startswith("ignored explicit argument"):
                return f"{name} 不带取值，却收到 {rest.removeprefix('ignored explicit argument ')}"
            return f"{name} {rest}"
        return message


def _build_main_parser() -> _Parser:
    """`meeting [-h]` 的解析器：只管总说明（描述、动作词清单、退出码 epilog）。"""
    return _Parser(
        prog="meeting",
        usage="meeting <动作词> [-h]",
        description=MAIN_DESCRIPTION,
        epilog=EXIT_CODE_EPILOG,
        formatter_class=_UsageHelpFormatter,
    )


def _build_web_parser() -> _Parser:
    """`meeting web [-h]` 的解析器：`web()` 不认任何开关（缺了也不猜）。"""
    return _Parser(
        prog="meeting web",
        usage="meeting web [-h]",
        description=(
            "起本地服务（只监听 127.0.0.1），前台跑到 Ctrl+C。\n"
            "端口走配置键 web.port 或环境变量 MEETING_PORT，被占就拒绝启动（退出码 2）；\n"
            "起服务时会打开浏览器，MEETING_NO_BROWSER=1 关掉这一步。"
        ),
        formatter_class=_UsageHelpFormatter,
    )


def _add_post_arguments(parser: _Parser) -> None:
    parser.add_argument(
        "session_dir",
        metavar="场次目录",
        help="要收干净的哪一场（不猜最新一场、不列目录内容、没有交互菜单）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "无视两层的终态，重跑离线段与纪要段"
            "（这是唯一会再次花钱的开关：离线段与纪要段都会再花一次钱）"
        ),
    )


def _build_post_parser() -> _Parser:
    """`meeting post <场次目录> [--force]` 的解析器。"""
    parser = _Parser(
        prog="meeting post",
        usage="meeting post <场次目录> [--force]",
        description=(
            "只跑会后那一套：音频归档补齐（PCM -> MP3）-> 会后离线（离线转写与说话人聚类）\n"
            "-> 会后纪要（纪要生成），跑完打印每层的结论与产物绝对路径就退。\n"
            "不开界面、不起服务；重跑是安全的（两层各有「已完成」判据），任一层停在\n"
            "running 默认拒跑，确认上一趟已经收工之后加 --force 再来。"
        ),
        formatter_class=_UsageHelpFormatter,
    )
    _add_post_arguments(parser)
    parser.flags_label = "本轮只认 " + " / ".join(POST_FLAGS)
    parser.positional_label = "场次目录"
    return parser


def _seconds_value(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"要一个正数，收到 {text!r}") from None


def _chunks_value(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"要一个正整数，收到 {text!r}") from None


def _add_replay_arguments(parser: _Parser) -> None:
    parser.add_argument(
        "session_dir",
        metavar="场次目录",
        help="拿哪一场的录音来重放（素材只读，原目录一个字节不动）",
    )
    parser.add_argument(
        "--seconds",
        type=_seconds_value,
        metavar="N",
        help="只重放前 N 秒（上限表达一：秒）",
    )
    parser.add_argument(
        "--chunks",
        type=_chunks_value,
        metavar="N",
        help="只重放每路前 N 块（按生效的 post.chunk_seconds 换算成秒；该值 <= 0 时这一档拒跑）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        metavar="目录",
        help="拷贝与产物的落点（每遍给一个新目录；缺省落在系统临时目录）",
    )


def _build_replay_parser() -> _Parser:
    """`meeting replay <场次目录> [--seconds N | --chunks N] [--out 目录]` 的解析器。"""
    parser = _Parser(
        prog="meeting replay",
        usage="meeting replay <场次目录> [--seconds N | --chunks N] [--out 目录]",
        description=(
            "重放验证工具：把那一场的素材截断复制进 --out（缺省落系统临时目录），\n"
            "在拷贝上全速重跑会后离线转写链，跑完打印实时率、每块耗时与失败原因分布。\n"
            "上限两种给其一（都不给就是整场，同给两个拒绝）；只验证离线链、绝不拉纪要。"
        ),
        formatter_class=_UsageHelpFormatter,
    )
    _add_replay_arguments(parser)
    parser.flags_label = "本轮只认 " + " / ".join(REPLAY_FLAGS)
    parser.positional_label = "场次目录"
    return parser


#: `meeting [-h | --help]` 认的写法（总说明；子命令各自的 `-h` 归各自的解析器）
_HELP_FLAGS: tuple[str, ...] = ("-h", "--help")


# 会后编排（`meeting post` 这条入口的 run_post()；收尾不自动跑、按钮走 run_session 直路）

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

#: 离线那一层**一路整条没交回、另一路跑成**那一档的结论词：它是这一层的终态（产物可用），
#: 与 `done` 分开写，好让读方在一格上就看出「有一路整条没有」（`artifacts.STATES` 同值）。
LAYER_PARTIAL = "partial"

#: 离线那一层的**被打断**档（「打断会后」节点 b1c6a58e，`artifacts.STATES` 同值）：
#: 跑到块边界被人停的**终态**。补记录判据把它算进「已落定」（打断之后逃出来的异常不许把
#: 它盖成 `failed`），幂等闸**不**把它当 `running` —— 打断之后重按按钮 / 再敲
#: `meeting post` 从头再跑，不需要 `--force`。
LAYER_INTERRUPTED = "interrupted"

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
#: 这里只能读盘、没有可挂的事件：纪要那一层由启动器起一条 daemon 线程，终态由那条线程
#: 或 headless 子进程写进 `meeting_minutes_status.json`——编排函数手里没有可 join 的句柄，
#: 盘上那份状态文件就是唯一的完成通知。等待因此有界：上界取 `minutes.timeout_seconds`。
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


# 入口


def main(argv: list[str] | None = None) -> None:
    """唯一入口：`web` / `post` / `replay` 三个词。

    动作词的路由留在本函数（`post()` / `replay()` 是直接可调的入口，各自的参数面
    归各自的解析器）；`-h` / `--help` 与「缺少动作词 / 未定义的动作词」两档用法错的
    终端说法与退出码保持既有口径。
    """
    parser = _build_main_parser()
    args = list(sys.argv[1:] if argv is None else argv)

    if not args:
        print(parser.format_help(), end="", file=sys.stderr)
        print("退出原因与退出码：用法错误（缺少动作词） -> EXIT_REJECTED", file=sys.stderr)
        sys.exit(EXIT_REJECTED)

    action, rest = args[0], args[1:]
    if action == "web":
        web_parser = _build_web_parser()
        try:
            web_parser.parse_args(rest)
        except ValueError as exc:
            print(web_parser.format_help(), end="", file=sys.stderr)
            print(f"用法错误：{exc}", file=sys.stderr)
            print("退出原因与退出码：用法错误（web 的参数） -> EXIT_REJECTED", file=sys.stderr)
            sys.exit(EXIT_REJECTED)
        web()
        return
    if action == "post":
        post(rest)
        return
    if action == "replay":
        replay(rest)
        return
    if action in _HELP_FLAGS:
        # `meeting [-h | --help]`：打印总说明后按 EXIT_OK 退（help action 自己收场）。
        parser.parse_args(args)
        return

    print(f"未定义的动作词：{action!r}", file=sys.stderr)
    print(parser.format_usage(), end="", file=sys.stderr)
    print("退出原因与退出码：用法错误（未定义的动作词） -> EXIT_REJECTED", file=sys.stderr)
    sys.exit(EXIT_REJECTED)


# ---------------------------------------------------------------------------
# `meeting post` —— 参数面（校验落在可被用例直接调用的函数上，不埋在 main() 里）
# ---------------------------------------------------------------------------


def _parse_post_args(argv: Sequence[str]) -> dict[str, Any]:
    """解析 `meeting post` 后面那一串（argparse）：交回 `{"dir": <path>, "force": <bool>}`。

    **不猜目录、不挂菜单、不列目录内容**（R17 / 不做 TUI）：位置参数缺了就报用法，
    给了就用它；指到不存在的路径就报那一条路径。`--force` 是唯一会再次花钱的开关，
    重复给也照收（幂等的布尔值）。

    抛出来的 `ValueError` 里那句话就是终端要打的原因（argparse 的用法错经
    `_Parser.error()` 接到这里），调用方（`post()`）接住它，打用法说明 + `EXIT_REJECTED`。
    """
    parser = _build_post_parser()
    namespace = parser.parse_args(list(argv))
    directory = Path(str(namespace.session_dir))
    if not directory.is_dir():
        raise ValueError(f"场次目录不存在（或者不是目录）：{directory}")
    return {"dir": directory.resolve(), "force": bool(namespace.force)}


# `meeting post` —— 参数面（校验落在可被用例直接调用的函数上，不埋在 main() 里）


def _load_config_or_exit(
    *,
    config_source_after_rebuild: bool = False,
    log_rebuild_failure: bool = True,
) -> config_module.Config:
    """三入口（web / post / replay）共用的启动前奏：bootstrap 日志 → 读配置并生效 → 按生效配置重建服务级日志。

    与 §3.5 的时序定死（不得对调）：配置损坏 → `EXIT_STARTUP_UNMET`；取值非法 →
    `EXIT_REJECTED`；主 log 不可写 → `EXIT_STARTUP_UNMET`——异常一律在本函数里
    接住，不冒到解释器变成解释器默认码，终端话术与退出码三入口逐字同源。

    两个关键字参数只管 log 流的形状，默认值取 post/replay 的旧行为（共用实现不拆，
    三入口各按旧本逐字还原）：
    - `config_source_after_rebuild`：「配置来源」行落重建③**之前**（bootstrap 日志，
      post/replay 旧本）还是**之后**（生效 log.dir/level/轮转，web 旧本）——web 传 True。
    - `log_rebuild_failure`：重建③失败的 log 流里落不落那行 error——web/post 旧本落，
      replay 旧本不落——replay 传 False。终端话术与退出码两档完全一致。

    为什么这条路必须有这一段：`run_post()` 的每个取值都经 `config_module.get(...)` 现读
    （`minutes.hook` 决定要不要真拉起花钱的子会话、`post.engine` 决定造不造真引擎）——
    没读进来的话它们全部退回 `DEFAULT_VALUES`，用户显式写的 `minutes.hook = null` 会被
    当成缺省 `auto` 照旧拉起子会话。读取时机在幂等闸与花钱之前：配置本身就决定这一趟
    要不要花钱，判早了会按默认值误判。交回的配置供调用方接着用。
    """
    # ① bootstrap 日志：默认日志目录 + 默认轮转参数。默认日志目录不可写时 bootstrap
    # 建不起来：这一刻只有终端可写，是否拒绝启动由 ③（生效 log.dir）决定。
    try:
        logging_setup.setup_service_logging(config_module.default_log_dir())
    except OSError as exc:
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
    if not config_source_after_rebuild:
        # post/replay 旧本：这行在重建③之前记，落 bootstrap 日志（web 那份在重建③之后）。
        try:
            _log().info(
                "配置来源：%s；生效值清单：\n%s",
                loaded.config_path(),
                config_module.format_applied(loaded),
            )
        except Exception:  # pragma: no cover - 同上
            pass

    # ③ 按生效配置重建服务级日志：不重建的话用户配的 `log.dir` 不生效、日志照旧落默认目录。
    log_dir = loaded.get("log.dir")
    try:
        logging_setup.setup_service_logging(
            log_dir,
            level=loaded.get("log.level"),
            max_bytes=loaded.get("log.max_bytes"),
            backup_count=loaded.get("log.backup_count"),
        )
    except OSError as exc:
        # 主 log 不可写 → 拒绝启动（不再照 V1 那样静默降级）
        print(
            f"启动前提不成立：主 log 不可写：{log_dir}（{type(exc).__name__}: {exc}）",
            file=sys.stderr,
            flush=True,
        )
        if log_rebuild_failure:
            # web/post 旧本在 log 流里另落一行 error；replay 旧本没有（传 False 还原）。
            try:
                _log().error(
                    "启动前提不成立：主 log 不可写：%s（%s: %s） -> EXIT_STARTUP_UNMET",
                    log_dir, type(exc).__name__, exc,
                )
            except Exception:  # pragma: no cover - 同上
                pass
        sys.exit(EXIT_STARTUP_UNMET)

    if config_source_after_rebuild:
        # web 旧本：这行在重建③之后记，落生效 log.dir/level/轮转（post/replay 那份在重建③之前）。
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

    解析参数 → 读配置并生效（`_load_config_or_exit()`）→ 按生效配置重建服务级日志
    （主 log 不可写 → `EXIT_STARTUP_UNMET`）→ 调 `run_post()` → 打终端输出并按摘要的
    `exit_code` 挑 `sys.exit(<常量>)`。每个出口都 `sys.exit(<常量>)`，之前一律
    `sys.stdout.flush()`——不 flush 的话管道里读不到（用例也没法断言）。
    `Ctrl+C` 由本函数接住，按强制打断档的既有常量收场。
    """
    parser = _build_post_parser()
    try:
        options = _parse_post_args(sys.argv[2:] if argv is None else argv)
    except ValueError as exc:
        print(parser.format_help(), end="", file=sys.stderr, flush=True)
        print(f"用法错误：{exc}", file=sys.stderr, flush=True)
        print("退出原因与退出码：用法错误（post 的参数） -> EXIT_REJECTED", file=sys.stderr, flush=True)
        sys.stdout.flush()
        sys.exit(EXIT_REJECTED)

    # 配置层在幂等闸与花钱之前读进来（§3.5 第 1 条，与 `web()` 同源）：这一趟里每个取值
    # 都从它现读。启动前奏（bootstrap 日志 → 读配置 → 重建服务级日志）走三入口共用的那一份。
    loaded = _load_config_or_exit()

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
        # 编排函数整个抛出来（它自己也接不住的那一档）：现场照实打到终端，不让调用方只看到一个栈。
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


# `meeting replay` —— 重放验证工具（a3b684bd：只读素材、产物写自己的落地、绝不拉纪要）


def _parse_replay_args(argv: Sequence[str]) -> dict[str, Any]:
    """解析 `meeting replay` 后面那一串（argparse）：交回 `{"dir", "seconds", "chunks", "out"}`。

    形状照 `_parse_post_args()`：校验落在可直调的函数上、`ValueError` 里那句话就是
    终端文案。三个开关都带取值（`--seconds 300` 这种）；上限两种给其一，都给与都不给
    的裁决在 `offline_replay.cap_to_seconds()` 里（这里只认形）。`--out` 只记形状，
    存不存在由工具侧现建。
    """
    parser = _build_replay_parser()
    namespace = parser.parse_args(list(argv))
    directory = Path(str(namespace.session_dir))
    if not directory.is_dir():
        raise ValueError(f"场次目录不存在（或者不是目录）：{directory}")
    return {
        "dir": directory.resolve(),
        "seconds": namespace.seconds,
        "chunks": namespace.chunks,
        "out": namespace.out,
    }


def replay(argv: Sequence[str] | None = None) -> None:
    """`meeting replay <场次目录> [--seconds N | --chunks N] [--out 目录]` —— 跑完就退。

    与 `post()` 同一套骨架：解析参数（用法错 → 用法说明 + `EXIT_REJECTED`）→
    **读配置并生效**（`_load_config_or_exit()`：上限换算与造引擎都读用户那份配置）→
    按生效配置重建服务级日志 → 调 `offline_replay.run_replay()` → 打读数并按整场
    `state` 挑 `sys.exit(<常量>)`（`failed` → `EXIT_FAILURE`，其余 → `EXIT_OK`；
    上限/素材那一路的 `ValueError` → `EXIT_REJECTED`）。

    **每个出口都 `sys.exit(<常量>)`，`sys.exit()` 之前一律 `sys.stdout.flush()`**（本模块
    既有纪律）。这一条**不碰纪要层**：`run_replay()` 直调 `run_session(minutes=False)`，
    `announce_minutes` 保持缺省的隔离闸——本工具只验证离线转写链。
    """
    parser = _build_replay_parser()
    try:
        options = _parse_replay_args(sys.argv[2:] if argv is None else argv)
    except ValueError as exc:
        print(parser.format_help(), end="", file=sys.stderr, flush=True)
        print(f"用法错误：{exc}", file=sys.stderr, flush=True)
        print(
            "退出原因与退出码：用法错误（replay 的参数） -> EXIT_REJECTED",
            file=sys.stderr,
            flush=True,
        )
        sys.stdout.flush()
        sys.exit(EXIT_REJECTED)

    # 配置与日志的时序照 post() 那一套：上限按块的换算读 `post.chunk_seconds`、缺省引擎读
    # `post.engine/model/dtype`，都必须来自用户写的那份 config.json 而不是默认值表。
    # 启动前奏（bootstrap 日志 → 读配置 → 重建服务级日志）走三入口共用的那一份；
    # 重建失败的 log 流里不落 error 行是 replay 旧本的形状（log_rebuild_failure=False 还原）。
    loaded = _load_config_or_exit(log_rebuild_failure=False)

    try:
        _log().info(
            "重放开始：%s（seconds=%s，chunks=%s，out=%s）",
            options["dir"], options["seconds"], options["chunks"], options["out"],
        )
    except Exception:  # pragma: no cover - 日志尚未初始化时只打印终端
        pass

    try:
        reading = offline_replay.run_replay(
            options["dir"],
            seconds=options["seconds"],
            chunks=options["chunks"],
            work_dir=options["out"],
            config=loaded,
        )
    except KeyboardInterrupt:
        print("重放被强制打断（Ctrl+C）。", file=sys.stderr, flush=True)
        try:
            _log().error("重放被强制打断 -> EXIT_INTERRUPTED")
        except Exception:  # pragma: no cover - 同上
            pass
        sys.stdout.flush()
        sys.exit(EXIT_INTERRUPTED)
    except ValueError as exc:
        # 上限换算不出 / 素材目录指不对这一档：如实拒绝，不打引擎。
        print(f"拒跑：{exc}", file=sys.stderr, flush=True)
        try:
            _log().error("重放拒跑：%s -> EXIT_REJECTED", exc)
        except Exception:  # pragma: no cover - 同上
            pass
        sys.stdout.flush()
        sys.exit(EXIT_REJECTED)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        try:
            _log().exception("重放抛出来了：%s", options["dir"])
        except Exception:  # pragma: no cover - 同上
            pass
        print(f"重放失败：{error}", file=sys.stderr, flush=True)
        sys.stdout.flush()
        sys.exit(EXIT_FAILURE)

    print(offline_replay.format_reading(reading), flush=True)
    summary_state = str((reading.get("summary") or {}).get("state") or "")
    exit_code = EXIT_FAILURE if summary_state == "failed" else EXIT_OK
    try:
        _log().info(
            "退出原因与退出码：重放结束（%s） -> %s", summary_state, _exit_name(exit_code)
        )
    except Exception:  # pragma: no cover - 同上
        pass
    print(
        f"退出原因与退出码：重放结束（{summary_state}） -> {_exit_name(exit_code)}",
        flush=True,
    )
    sys.stdout.flush()
    sys.exit(exit_code)


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
    progress: offline_progress.OfflineProgressLedger | None = None,
) -> dict[str, Any]:
    """把「跑会后一套」跑一遍，交回一份可读摘要（`post()` 与收尾第 4 步共用）。

    四件事，按顺序：

      1. 音频归档补齐（`sync_archive()`）：两路各自独立判、独立转。判据是转码独立工序那一份
         （`meeting/capture/transcode.py` 的凭据核对）：凭据认账不重转、空段不产 MP3、
         没凭据 / 不认账的重转一次；转码失败如实记 `failed`、PCM 一个字节都不动。
         转码实现只有一份（`capture.archive` 的 `convert_to_mp3` + `mp3_path_for`）。
      2. 跑离线那一趟：`offline_runner.run_session(...)` 走模块属性调用，好让用例整个换掉；
         注入参数一路向下传，不落在模块全局上。`announce_minutes=True` 是人工放行那一格
         （隔离闸）：离线链拉起纪要的开关缺省假，只有 `meeting post` 这条人工入口显式传真。
         逐块进度记录在这一条入口上也立起来（`progress` 缺省时按 `runtime._run_post_offline()`
         同一副样子立一份），页面读会后进度不因「这一趟是命令行拉起的」而空着
         （`docs/design-intent.md` 第 12 条：落盘跟着链走，不挑入口）。
      3. 等纪要那一层的终态：纪要挂在离线链尾巴上（`_announce_minutes()`），要等它落定才报
         结论。三条拉起路径共用同一处等待（`_wait_for_settled_minutes()`）：判据是盘上那份
         状态与拉起之前的基线（`_minutes_baseline()`）不同、且已落终态，于是上一趟留在盘上的
         终态不会被当成这一趟的结果。结论如实三分：新终态 `done` 且产物过判据 → 成功档；
         `failed` / 产物过不了判据 / 读不动 → 如实失败；到上界仍未落定 → `timeout` 那一档。
      4. 汇总：每层结论 + 逐条产物绝对路径 + 退出码，都在返回的摘要里。

    幂等闸判定一律落在花钱之前（`sync_archive()` 之后、`run_session()` 之前）。
    「已完成」判据两层各一份：离线段 = 状态文件 `done` 且三份产物齐；纪要段 = 状态文件
    `done` 且 `meeting_final.md` 过得了四条判据（`minutes/launch.py::check_final_artifact()`，
    复用不重写）。分四档：两层都到终态（且音频已归档）→ 「无待办」，`EXIT_OK`，一个引擎
    调用、一个子进程都不产生；只差一层 → 只跑那一层；任一层停在 `running` → 默认拒跑
    （`EXIT_REJECTED`，点名状态文件并给 `--force`）；`--force` → 无视全部终态判定两层都重跑
    （它是唯一会再次花钱的开关）。

    `minutes_launcher` 缺省 `None` 走产品路径：离线段跑时由 `run_session` 尾巴那道闸拉起纪要，
    离线段已完成时由本函数经同一道闸补拉（「缺什么补什么」不许只对注入假件的路径生效）。
    等待只看那一档、不看是谁拉起的（真启动器与假件时序同形）；拉起点整个缺席 / 被再入闸
    拦下 / 解析不出实现时没有终态会来，不等，按盘上那一刻的两半判据出结论。

    离线那一趟整个抛出来时本函数原样上抛（抛之前先把状态文件补成 `failed`）：`meeting post`
    入口与收尾线程体各自接住、各自如实登记。别的失败一律在摘要里回答，不抛。
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
    # 拉起之前那一份基线快照：拉起之后要等的「新终态」就是与它不同的那一份。
    # 三条路径共用这一格——盘上留着上一趟的终态时，等到这一趟新落的那一份才算数。
    minutes_baseline = _minutes_baseline(directory)
    offline_running = _layer_is_running(offline_status, ("state",))
    minutes_running = _layer_is_running(minutes_status, ("state",))
    # 已经交付过（web 分支的终态 `delivered`、产物还没落）：不拉起第二个会话。
    # 它与 `running` 的区别是「上一趟还在不在跑」：交付那条路的语义是「投递即完事」，
    # 这一层不去猜 DSH 那边写没写完。
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
        # 退出码不在这里定：`_summarize_run()` 按两层的结论与归档那一层一起判。
        result.update(_summarize_run(result))
        return result

    # 「这一层已完成」与「这一趟到底跑不跑」是两件事：带 `--force` 时前者取值照旧，
    # 但两层都要重跑。
    offline_complete = _offline_layer_done(directory, offline_status)
    minutes_complete = _minutes_layer_done(directory, minutes_status)
    offline_done = bool(offline_complete and not force)
    minutes_done = bool(minutes_complete and not force)
    # 「这一趟没有待办」的前置是「音频已归档」（任务书「关键决定 4」）：归档有一路转不动
    # 时不许走这一档——既不能报 `EXIT_OK`，也不能打一句「音频已归档」。
    if offline_done and minutes_done and result["archive"]["state"] != ARCHIVE_FAILED:
        # 两层都到终态、产物齐、音频已归档：一个引擎调用、一个子进程都不产生。
        result["offline"] = _finished_offline_outcome(directory, offline_status)
        result["minutes"] = _finished_minutes_outcome(directory, minutes_status)
        result["artifacts"] = _artifact_paths(directory)
        return _summarize_run(result)

    announced: list[dict[str, Any]] = []
    offline_error: str | None = None
    runner_result: dict[str, Any] | None = None
    if offline_done:
        # 离线段已完成：一个引擎调用都不产生，三份离线产物的 sha256 一个字节不变。
        try:
            _log().info("离线段已是终态且产物齐，这一趟不重跑：%s", directory)
        except Exception:  # pragma: no cover - 日志尚未初始化时只走终端
            pass
    else:
        # 尾巴上那道通知的解与注两个参数向下传，本模块不替换任何模块全局属性；
        # `minutes_launcher` 非空时用登记包装记下它的交回值。
        minutes_launcher_lane = (
            _capturing_launcher(minutes_launcher, announced)
            if minutes_launcher is not None
            else None
        )
        # 逐块进度记录在这一条入口上也落（`docs/design-intent.md` 第 12 条：
        # 落盘跟着链走、不挑是哪个按钮拉起的）；调用方注入的那一份优先。
        if progress is None:
            progress = offline_progress.OfflineProgressLedger.for_session(directory)
        try:
            # 人工入口显式放行（隔离闸）：纪要那一层只有人工发起才被拉起。
            runner_result = offline_runner.run_session(
                directory,
                engine=engine,
                minutes_launcher=minutes_launcher_lane,
                force=bool(force),
                announce_minutes=True,
                progress=progress,
            )
        except BaseException as exc:  # 离线这一层整个抛出来：如实记，再原样上抛
            offline_error = f"{type(exc).__name__}: {exc}"
            try:
                _log().exception("会后离线那一趟抛出来了：%s", directory)
            except Exception:  # pragma: no cover - 同上
                pass
            # 上抛之前把状态文件补一版 `failed`，好让直接调 `run_post()` 的读者也有记录可查。
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
    """纪要段这一趟的结论：谁跑过就用谁的记录，没跑过就按盘上的现状出。

    离线链跑过（`runner_result` 有内容）→ 尾巴上那道闸已把纪要段该做的事做完，按它的交回值出。
    离线链没跑（离线段已完成）→ 这一层单独走一遍 `_announce_minutes(..., launcher=…)`：
    拉起这一步与离线那一段各有自己的闸，离线段收工不该连带把纪要段钉住——注入的假启动器
    与产品路径都要走这一步（用例靠登记核「这一趟有没有再花钱」）。`baseline` 是拉起之前的
    现场快照，等待判据落在它身上。`force` 透到那道闸上，不写死。
    """
    if runner_result is not None:
        announce = dict(runner_result.get("minutes") or {})
        if announced:
            announce.update(announced[0])
        # `state` 那一格交给 `_minutes_from_launcher()` 按档位判：`launched` 那一档不看它
        # （拉起那一刻答不出落定没有），一律等这一趟新落的终态。
        return _minutes_from_launcher(
            directory,
            announce or None,
            baseline=baseline,
            deadline=deadline,
        )
    if launcher is not None:
        try:
            # 人工入口显式放行（隔离闸）：只补纪要这一路（注入假启动器的形状）同样传真。
            outcome = offline_runner._announce_minutes(
                directory, launcher=launcher, force=force, announce_minutes=True
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

    `launcher` 明写 `product`（这一趟走的是产品路径，不顶着注入那一档的名字）；
    `announced` 明写成 `False`——「这一趟没有再拉起子会话」正是这一档在说的话。
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
    """产品路径下补纪要那一层：经同一道 `_announce_minutes()` 解析实现并拉起，等它落定。

    走到这一格的现场是「离线段已完成、纪要段没到终态」——离线链整段没跑，尾巴上那道通知
    也没走到，而纪要的拉起点就挂在它上面。不补的话纪要再也补不出来（唯一出路 `--force`
    会连离线段一起重跑）。拉起这一步不是本函数自己的：参数一路交给 `_announce_minutes()`，
    它自己的两道闸照旧管用（`MEETING_MINUTES_CHILD=1` 再入闸安静跳过、终态且产物过判据
    时不调启动器）。

    已经在飞时当场拒跑：真实现在建线程之前先把「在飞」落到盘上，读到 `running` 就抛
    `AlreadyRunningError` → `hook = running`，这一格据此不等（没有第二个终态会来），
    按现成的拒跑档收场；`--force` 一路传到 `launch(..., force=True)`，在飞判据跳过。

    拉起 ≠ 落定：真启动器起一条 daemon 线程就返回，整条链要跑几十分钟。等待交给
    `_settled_minutes_outcome()`（上界取 `minutes.timeout_seconds`）。拉起点缺席 / 被再入闸
    拦下 / 解析不出实现时不等——再入闸那一档安静跳过、不记失败（`_quiet_skip_announce()`）；
    缺席与解析不出实现按盘上那一刻的两半判据出结论，其余如实记失败。

    `baseline` 缺省时现取一份（此刻的现场就是「拉起之前」），三条路径的等待判据同源。
    """
    if baseline is None:
        baseline = _minutes_baseline(directory)
    try:
        # 人工入口显式放行（隔离闸）：「缺什么补什么」这半条也走人工那一档。
        announce = offline_runner._announce_minutes(
            directory, force=force, announce_minutes=True
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

    outcome = dict(announce or {})
    if str(outcome.get("hook") or "") == "launched":
        return _settled_minutes_outcome(
            directory, outcome, deadline=deadline, baseline=baseline
        )

    if str(outcome.get("hook") or "") == LAYER_RUNNING:
        # 盘上那一版已经把这一层占住了，这一趟没有再拉起子会话；结论与「进门前就撞上
        # running」那一档同格，摘要据此取 `EXIT_REJECTED`。
        return _minutes_from_launcher(
            directory, outcome, baseline=baseline, deadline=deadline
        )

    if str(outcome.get("hook") or "") == "skipped":
        # 这一趟安静跳过（skipped = 子会话里的再入闸拦下）：没有终态会来。走注入那一档
        # 同一个出口，两条路档位对齐——安静跳过不记失败，不把再入闸的理由念成失败原因。
        # （`absent` 那一档不走这里：它与「配置里明确关掉」是两件事，用例把它钉在失败档上。）
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
    """拉起成功之后：等这一层落终态，再按等到的终态出结论。

    等待是三条路径共用的一处（`_wait_for_settled_minutes()`），判据落在拉起之前的
    `baseline` 上。这一格只做「等到的终态 → 结论」的映射：`done` 且产物过判据 → 成功档；
    `done` 而产物没过那四条判据 → 如实记失败（两半判据缺一不可）；`delivered` → 已交付
    DSH web 服务端，交付那条路的语义是「投递即完事」，不按失败算、也不等产物；`failed` /
    读不动 → 照它自己那一档如实记；到上界还没落定 → `timeout`（不谎报成功、不写成 `failed`）。

    `skipped` 一律为假：走到这一格说明这一趟真的把这一层拉起来了，「无待办」那一句不适用。
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


# 幂等闸：两层各自的「已完成」判据与那三种「不必跑」的结论


def _capturing_launcher(
    launcher: Any, announced: list[dict[str, Any]]
) -> Callable[[str], Any]:
    """把注入的假启动器包一层只登记的包装：它交回的对象原样记在 `announced` 里。

    「这一趟有没有再拉起子会话」因此在用例侧留下可核对的记录；不碰任何模块全局属性。
    """

    def wrapped(target: Any) -> Any:
        returned = launcher(str(target))
        announced.append({"returned": returned})
        return returned

    return wrapped


def _skipped_offline_outcome(
    directory: Path, status: dict[str, Any], *, running: bool = False
) -> dict[str, Any]:
    """离线段这一趟不跑时的结论（`running` 拒跑那一档与「无待办」那一档共用）。"""
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
                "transcript": TRANSCRIPT_NAME_TEMPLATE.format(source=source),
                "speakers": SPEAKERS_NAME_TEMPLATE.format(source=source),
                "report": REPORT_NAME,
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
    """纪要这一层是不是已经交付给 DSH web 服务端（终态 `delivered`）。

    web 分支的语义是「投递 accepted 即算交付完成」：这一档既不算「正在跑」（本进程没有
    线程在跑）也不算「已完成」（产物还没过四条判据）——它是第三种「不必再拉起」的情形：
    已经交付过，重复拉起只会让同一场生成两遍（`--force` 是重新交付的出路）。
    """
    return str(status.get("state") or "") == LAYER_DELIVERED


def _missing_names(directory: Path, names: Iterable[str]) -> list[str]:
    """交回 `names` 里**不在盘上**的那些（判据缺一不可的那一半）。"""
    return [str(name) for name in names if not (directory / str(name)).is_file()]


def _offline_artifacts_complete(directory: Path, status: dict[str, Any]) -> bool:
    """离线段的三份产物是不是都在：逐路的两份按本场声明的每一路各算一份，外加一份报告。"""
    names = [REPORT_NAME]
    for source in offline_runner.declared_sources(directory):
        names.append(TRANSCRIPT_NAME_TEMPLATE.format(source=source))
        names.append(SPEAKERS_NAME_TEMPLATE.format(source=source))
    return not _missing_names(directory, names)


def _offline_layer_done(directory: Path, status: dict[str, Any] | None = None) -> bool:
    """离线段是不是**已经完成**：状态文件 `done` **且**三份产物齐（缺一条就不算完成）。"""
    document = _offline_status(directory) if status is None else status
    if str(document.get("state") or "") != LAYER_DONE:
        return False
    return _offline_artifacts_complete(directory, document)


def _minutes_status(directory: Path) -> dict[str, Any]:
    """读 `meeting_minutes_status.json`——走纪要那一层自己的读方（逐字段校验，不认残文件）。

    读不动时这一层按「没跑完」算（`state` 交回 `None`），不猜。读方在函数体内 import：
    `import meeting.cli` 仍然不拉 `meeting.minutes` 那一包。
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
    """纪要段是不是已经完成：`meeting_final.md` 过得了那四条判据。

    判据只看产物、不看状态那一格：`done` 与 `delivered` 两种终态都可能对应「产物已在盘上」，
    状态那格管的是另外两件事（`running` 拒跑、`delivered` 而产物还没落）。四条判据复用现成的
    `minutes/launch.py::check_final_artifact()`（函数体内 import），不重写一份。
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
    """把 `meeting_offline_status.json` 补成 `failed`（尽力而为：补不上只留一条 log）。

    字段口径只有一份（`meeting/offline/artifacts.py` 的 `mark_failed_if_unset()`）：
    先读回上一版，盘上停着终态记录就沿用它的 `error` 等字段——异常从 `run_session` 逃出来
    之前离线段自己已经现场写过一版，那一版里的话比这里的通用串准；否则补一版 `failed`。
    """
    try:
        from meeting.offline import artifacts

        artifacts.mark_failed_if_unset(directory, error)
    except Exception:  # pragma: no cover - 补写失败时不掩盖原来那条异常
        try:
            _log().exception("会后离线状态文件补写失败（原异常照上面那条 log）")
        except Exception:
            pass


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


# 音频归档补齐（PCM → MP3）


def sync_archive(
    session_dir: Path | str,
    *,
    sources: Iterable[str] | None = None,
    converter: Callable[[Path, Path], bool] | None = None,
) -> dict[str, Any]:
    """按转码独立工序的同一套判据补齐各路各段的 MP3 归档，交回逐段登记。

    完成判据只有一份：`meeting/capture/transcode.py` 的凭据核对（`segment_done()` /
    `ensure_segment()`）——「这一段写完了」以逐段凭据为准，产物文件在那儿不算完成。
    归档补齐、服务里的转码工序、`transcode_run` 补跑走同一个模块的同一段判据，不许抄
    第二份；转码实现也只有原来那一份；段列表来自 `meeting/session.py` 的
    `segment_archive_paths()`。凭据认账 → `no_work`；空段 PCM → 不产 MP3、落空段凭据；
    没凭据 / 不认账 → 转一次（MP3 先写临时名字再原子落位，转砸了旧产物原样保留，
    PCM 一个字节都不动）；转码抛错 → 该段记 `failed`，别的段照转。

    `sources` 缺省取 `offline_runner.declared_sources()`；`converter` 是用例的接缝。
    """
    directory = Path(session_dir)
    names = tuple(sources) if sources is not None else offline_runner.declared_sources(directory)
    apply_conversion = converter if converter is not None else transcode_module.default_converter()

    rows: list[dict[str, Any]] = []
    for source in names:
        entries = [
            entry for entry in transcode_module.plan_segments(directory, (source,))
        ]
        if not entries:
            rows.append(
                {
                    "source": str(source),
                    "segment": None,
                    "pcm": str((directory / archive_name(source, 1)).resolve()),
                    "mp3": None,
                    "outcome": OUTCOME_NO_WORK,
                    "reason": "归档 PCM 不在场",
                }
            )
            continue
        for entry in entries:
            row: dict[str, Any] = {
                "source": str(source),
                "segment": entry.segment,
                "pcm": str(entry.pcm.resolve()),
                "mp3": None,
                "outcome": OUTCOME_NO_WORK,
                "reason": None,
            }
            result = transcode_module.ensure_segment(entry, apply_conversion)
            state = str(result.get("state"))
            if state == transcode_module.ROW_FAILED:
                row.update(outcome=OUTCOME_FAILED, reason=result.get("error"))
                try:
                    _log().error("音频归档补齐失败：%s（%s）", entry.pcm, row["reason"])
                except Exception:  # pragma: no cover - 日志尚未初始化时只留登记
                    pass
                rows.append(row)
                continue
            if result.get("reused"):
                row.update(
                    mp3=str(entry.mp3.resolve()) if result.get("mp3") else None,
                    outcome=OUTCOME_NO_WORK,
                    reason="凭据认账，不重转" if result.get("mp3") else "空段凭据在，不产 MP3",
                )
                rows.append(row)
                continue
            if state == transcode_module.ROW_SKIPPED:
                row.update(
                    outcome=OUTCOME_NO_WORK,
                    reason=f"归档 PCM 是 {entry.pcm.stat().st_size} 字节（空），不产 MP3",
                )
                rows.append(row)
                continue
            if result.get("mp3"):
                row.update(mp3=str(entry.mp3.resolve()), outcome=OUTCOME_CONVERTED)
            else:  # pragma: no cover - 转码器说产出了却不见文件：如实记成失败
                row.update(outcome=OUTCOME_FAILED, reason="转码器报成功，但 MP3 没落盘")
            rows.append(row)

    state = ARCHIVE_DONE
    if any(row["outcome"] == OUTCOME_FAILED for row in rows):
        state = ARCHIVE_FAILED
    elif all(row["outcome"] == OUTCOME_NO_WORK for row in rows):
        state = ARCHIVE_SKIPPED
    return {"state": state, "sources": rows}


# 每层的结论与产物


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

    分档按「盘上的终态」与「这一趟有没有抛出」两条一起来：状态文件落 `failed` 或整趟抛出
    → `failed`；`done` 与 `partial` 都记 `done`——两档产物都在盘上、这一层都收了工，
    这一格的诚实落在 `state` 那一格与逐路产物上（`partial` 照盘上那份报，不折成 `done`）；
    盘上什么都没有才记 `absent`。`skipped = True` 表示这一层这一趟没跑（幂等闸判它已完成），
    已有终态照样如实报。
    """
    status = _offline_status(directory)
    state = status.get("state")
    failure = status.get("error") or error
    if state == "failed" or (state is None and error is not None):
        hook = LAYER_FAILED
    elif state in ("done", LAYER_PARTIAL):
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
                "transcript": TRANSCRIPT_NAME_TEMPLATE.format(source=source),
                "speakers": SPEAKERS_NAME_TEMPLATE.format(source=source),
                "report": REPORT_NAME,
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

    分档只看拉起这一步自己的档位（`hook`），不看是谁拉起的——真实现与注入的假件在时序上
    同形（`launch()` 返回即算启动成功，终态由后台线程或子进程写进状态文件）：

      - `launched`：拉起不等于落定——一律等这一趟新落的终态（判据落在 `baseline` 上），
        三条产品路径与注入假件那一档共用同一处等待；等不到如实报 `timeout`；
      - `already_done`：闸拦下（没有再拉起），结论就在盘上那份终态里；
      - `skipped` / `absent`：子会话闸 / `minutes.hook` 缺席（后一半才看 `state`）；
      - `failed`：这一层自己抛出异常（没有终态会来，不等）。

    交回值里的 `state` 不当结论：拉起那一刻答不出「落定没有」，盘上与 `baseline` 相同的
    那一份是上一趟的记录。`announce` 缺席（没走到尾钩）时交回 `None` 那一档。
    `launcher` 那一格：`announce` 自己带了就原样留着，没带才按缺省 `injected` 算。
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
        # 拉起不等于落定，与「谁拉起的」无关：两者的终态都只由盘上那份状态文件回答。
        # 一律等这一趟新落的终态（判据落在 `baseline` 上）；交回值里的 `state` 不当结论。
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
        # 这一场已经交付过（状态停在 `delivered`、产物还没落）：结论与「进门前就撞上
        # delivered」那一档同格。
        result.update(
            _skipped_minutes_outcome(
                directory, _minutes_status(directory), running=False, delivered=True
            )
        )
        result["launcher"] = "product"
        result["announced"] = False
        result["reason"] = outcome.get("reason") or result.get("reason")
    elif hook == LAYER_RUNNING:
        # 已经在飞了（claim 读到盘上那一版 `running`，`launch()` 当场拒了）：结论与
        # 「进门前就撞上 running」那一档同格，摘要按现成的拒跑档收场。
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

    纪要那层的写方是原子替换（每次落盘都换一份新文件），这对值在「重新写了一份」时一定变，
    够分辨上一趟与这一趟的版本，不需要新增配置键、不改纪要层的契约。
    """
    try:
        info = path.stat()
    except OSError:
        return None
    return (int(info.st_mtime_ns), int(info.st_size))


def _minutes_baseline(directory: Path) -> dict[str, Any]:
    """拉起之前取一份纪要状态文件的现场快照（等待判据就落在它与之后的差别上）。

    三格：文件在不在、这一版落的 `state`、这一版的标记。取的时刻是「离线那一段跑起来
    之前」，三条路径拿到的都是同一种基线。
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
    """盘上这一版是不是这一趟新落的终态（等纪要终态那条判据，两半缺一不可）。

    新落的：这一版的标记与基线那一版不同。终态：`state` 落在 `running` 之外、且这一版
    读得出内容——标记变了而状态读不出（空文件 / 坏 JSON）按「还在写」接着等：写方是
    原子替换，读方偶尔撞上换文件那一瞬间（本机实测到过：刚换上去的那一版被读成空）；
    真是残文件时到上界如实报 `timeout`。上一趟留在盘上的终态标记与基线相同，不算落定。
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
    """拉起纪要那一层之后：等这一趟新落的终态，再如实报出来。

    三条路径共用这一处等待：拉起契约是「返回即算启动成功」，终态由后台线程写进状态文件，
    编排函数手里没有可 join 的句柄，等待就落在盯盘上那一份文件上（每次读之间让出
    `poll_seconds`，落定即返回）。上界取 `minutes.timeout_seconds`（`timeout` 缺省时读它），
    与调用方的绝对时刻 `deadline` 取更早者；到点没落定 → `timeout`（不写成功也不写 `failed`）。

    落定的那一版按 `state` 分档：`done` / `failed` → `hook="finished"`；`unreadable` →
    状态文件读不动；`timeout` → 到上界还是 `running` 或还没出现读得动的状态文件。
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
        (REPORT_NAME, "会后离线运行报告"),
        (offline_runner.MINUTES_STATUS_NAME, "会后纪要状态文件"),
        (MINUTES_FINAL_NAME, "会后纪要"),
    ]
    for source in offline_runner.declared_sources(directory):
        names.append((TRANSCRIPT_NAME_TEMPLATE.format(source=source), f"{source} 的离线转写"))
        names.append((SPEAKERS_NAME_TEMPLATE.format(source=source), f"{source} 的说话人对照表"))
    for source in offline_runner.declared_sources(directory):
        for path in segment_archive_paths(directory, source, suffix=".mp3"):
            names.append((path.name, f"{source} 的归档 MP3（{path.stem.rsplit('_', 1)[-1]} 段）"))
    found: dict[str, str] = {}
    for name, label in names:
        path = directory / name
        if path.is_file():
            found[label] = str(path.resolve())
    return found


def _summarize_run(result: dict[str, Any], *, error: str | None = None) -> dict[str, Any]:
    """把三层的结果汇总成一份摘要：结论、逐条产物绝对路径、退出码。

    退出码按三层的结论在这里一起判（调用方只写每层的结论，不预先钉死这一格）：
    整趟抛出 / 归档 `failed` / 离线 `failed` → `EXIT_FAILURE`；任一层停在 `running` →
    `EXIT_REJECTED`；其余按纪要层结论取。归档 `failed` 与拒跑两档同时在场时以拒跑收场
    ——前者是「真去转了没转成」，后者是「压根没被允许跑」。

    退出码与「结论：」那一行分两格判：拒跑优先定退出码，`state` 照实写这一趟落在哪一层
    （复合现场因此是「`EXIT_REJECTED` + 结论 `failed`」，既点名状态文件也不吞归档的结果）。
    `EXIT_OK` 的前置是「这一趟要做的都做成了」。纪要层到上界还没落定如实写 `timeout`，
    不写成 `failed`——它还在跑，说成失败与说成成功一样是谎报（退出码照样不是成功档）。
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
    # 退出码走拒跑那一档，结论如实写这一趟的落点。
    if error is not None or archive_failed or offline_failed:
        state = LAYER_FAILED
    elif exit_code == EXIT_OK:
        state = LAYER_DONE
    elif exit_code == EXIT_REJECTED:
        state = LAYER_RUNNING
    elif minutes.get("hook") == "timeout":
        # 到上界还没落定：如实报它自己那一档，不写成 `failed`（退出码照样不是成功档）。
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
    # ①②③ 启动前奏（bootstrap 日志 → 读配置并生效 → 按生效配置重建服务级日志）：
    # 三入口共用 `_load_config_or_exit()` 那一份，不再逐行重抄；「配置来源」行按 web 旧本
    # 落重建③之后（生效 log.dir/level/轮转），故传 config_source_after_rebuild=True。
    loaded = _load_config_or_exit(config_source_after_rebuild=True)

    logger = _log()
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
    print(f"log 路径：{runtime.log_path}", flush=True)
    # 终端上这两样同样要落 log：没在终端前面守着的人靠 log 复核时，这两行就是他要找的东西。
    logger.info(
        "启动就绪：URL：%s；log 路径：%s", url, runtime.log_path
    )

    # ⑥ 装信号处理函数，再把服务器跑起来，前台接收 Ctrl+C
    service_app.install_shutdown_signal_handlers()

    exit_code = EXIT_OK
    try:
        app = service_app.build_app(runtime)
        server = service_app.ServiceServer(service_app.build_server_config(app))
        # 只在这一处把服务器对象交给模块级关停目标；运行时对象自己不持有服务器引用。
        service_app.set_server(server)
        try:
            server.run(sockets=[sock])
        except KeyboardInterrupt:  # pragma: no cover - 处理函数已接管，这里只是兜底
            exit_code = EXIT_OK
        except Exception:
            logger.exception("服务异常退出")
            exit_code = EXIT_FAILURE
    except Exception:
        # `server.run()` 这一路之外的意外（组装应用、构造服务器对象这些起步步骤）：
        # 按收尾失败档的同一个常量收场（§3.5「不许让异常冒到 uvicorn / 解释器」+ §8
        # 「不许静默」）。起步步骤没进服务器主循环，但同样是一条退出路径。
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
        # 退出码按「本次关停落在哪一档」挑；挑完立刻把收尾失败档的现场打到终端——
        # 这一步必须留在 `finally` 里：关停途中又抛异常的情形都要经由这里说话。
        exit_code = _drain_exit_code(runtime, fallback=exit_code)
        _report_drain_outcome(runtime, logger, exit_code)

    logger.info("退出原因与退出码：服务已关停 -> %s", _exit_name(exit_code))
    sys.exit(exit_code)


def _drain_exit_code(runtime, *, fallback: int = EXIT_OK) -> int:
    """按本次关停落在哪一档挑退出码（五档都是 cli.py 模块级的常量标识符）。

    强制打断档（第二次 `Ctrl+C`）→ `EXIT_INTERRUPTED`；收尾有步骤 `failed` →
    `EXIT_FAILURE`；其余（空闲退出 / 收尾干净）→ `EXIT_OK`。

    `fallback` 带的是另一路信息——「服务自己有没有出事」（`server.run()` 抛异常时 `cli`
    的 `finally` 把 `EXIT_FAILURE` 传进来），与上面三档正交：收尾干净判不出「服务是不是
    异常退出」，因此原样沿用（服务异常退出 + 收尾干净的组合是 `EXIT_FAILURE`，不被收尾
    干净那格抹成 `EXIT_OK`）。这条语义在 `tests/test_shutdown_signal.py` 的
    `test_drain_exit_code_matrix_including_the_server_crash_fallback` 里逐档写死。
    退出码的合并到此为止：终端文案不跟着走，`_report_drain_outcome()` 按现场有没有
    `failed` 步挑话。
    """
    if getattr(runtime, "shutdown_interrupted", False):
        return EXIT_INTERRUPTED
    ended = getattr(runtime.snapshot, "ended", None)
    if ended is not None and any(step.status == "failed" for step in ended.steps):
        return EXIT_FAILURE
    # 收尾干净 / 空闲态：沿用 `server.run()` 那一侧的兜底，收尾干净这一格抹不掉服务异常退出。
    return fallback


def _drain_reported_failed(runtime) -> bool:
    """现场档位的判据：收尾里有没有步骤 `failed`（只看现场，不看退出码）。

    `EXIT_FAILURE` 里压着两件正交的事（收尾有 `failed` 步 / `server.run()` 自己抛异常）；
    合并看退出码会让「收尾三步全绿、只是服务器自己炸了」被说成收尾失败，与盘上那一版对不上。
    """
    ended = getattr(getattr(runtime, "snapshot", None), "ended", None)
    if ended is None:
        return False
    return any(step.status == "failed" for step in ended.steps)


def _report_drain_outcome(runtime, logger: logging.Logger, exit_code: int) -> None:
    """退出时把现场打到终端——按真实档位说话，两档文案分开（§5.4 行 256、§3.5）。

    强制打断档（第二次 `Ctrl+C`）：盘上留在 `draining` + `interrupted`，说「收尾被强制
    打断」那一套——不是「服务不自杀」（此前不看档位只按现场挑话，会在「强制打断 + 现场
    有 `failed` 步」打出误报，审查问题 M11）。收尾失败档（现场找得到 `failed` 步）：照
    §5.4 行 256 倒 log 路径与服务 log 最后几行——判据看现场不看退出码（`EXIT_FAILURE`
    里还压着「`server.run()` 抛异常」这另一件事，那件事由 `web()` 自己说清）；其余情形
    一个字都不打。
    """
    if exit_code == EXIT_INTERRUPTED:
        # 强制打断档：先说档位，再说盘上留在哪一格与出路（盘上那一版写法见 §3.5 行为表）。
        print(
            "收尾被强制打断（盘上留在 draining + interrupted，未写 ended_at）；"
            f"log 路径：{runtime.log_path}",
            file=sys.stderr,
            flush=True,
        )
        print(
            "出路：看清现场后用 `meeting post <场次目录>` 收干净，或重启服务。",
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
    "main",
    "post",
    "run_post",
    "sync_archive",
    "web",
]
