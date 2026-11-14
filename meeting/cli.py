"""命令行入口。

命令词三个：`meeting web` / `meeting post` / `meeting replay`——每条都是 `meeting <动词>`
的两词命令形。`replay` 是重放验证工具，只服务验证；生产路径只认 `post`。

参数面走 argparse（`_Parser`）：三个动作词各有一个解析器，`-h` / `--help` 打印说明后按
`EXIT_OK` 收场；用法错不由 argparse 直接退（它默认打英文模板），而是抛 `ValueError`
交回入口函数打用法说明、按 `EXIT_REJECTED` 收场——退出码与终端口径都不变。动作词的路由
留在 `main()`：`post()` / `replay()` 是直接可调的入口（用例 seam），各自解析各自的串。

五个退出码常量在本模块模块级定义，每个出口一律 `sys.exit(<常量>)`，
log 写常量名——本模块不出现裸数字。`config.py` / `logging_setup.py` 不定义退出码常量，
只抛已定的异常，由 `main()` 映射。配置与日志的时序四步定死（不得对调）：
① 默认日志目录起 bootstrap 日志 → ② 读配置并值级校验 → ③ 按生效配置重建服务级日志 →
④ 生效 `log.dir` 写不进去即「主 log 不可写」。`meeting post` 的编排收在
`meeting/post_run.py` 的 `run_post()`：**编排只有这一条入口**——收尾第 4 步不拉起任何
会后动作，「生成会后精细稿」按钮走 `run_session` 直路。服务器信号接管必须在服务器
运行入口之前调用。
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import Any, NoReturn, Sequence

from meeting import config as config_module
from meeting import logging_setup
from meeting.offline import replay as offline_replay
from meeting.post_run import (
    LAYER_RUNNING,
    POST_STEPS,
    _failure_summary,
    run_post,
)
from meeting.service import app as service_app

# 退出码表（标识符与取值都是冻结项）

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

REPLAY_FLAGS: tuple[str, ...] = ("--seconds", "--chunks", "--out")

MAIN_DESCRIPTION = """\
本地会议录制服务。动作词三个，每条都是 `meeting <动作词>` 的两词命令形（R15）：

  web     起本地服务（只监听 127.0.0.1），前台跑到 Ctrl+C
  post    只跑会后那一套：音频归档补齐 -> 会后离线 -> 会后纪要，跑完就退
  replay  拿旧录音全速重跑离线链（验证用）：只验证、不是第二条生产路径

每条命令都认 -h / --help，看各自那份更细的用法，例如 `meeting post --help`。
"""

#: 主解析器的 epilog：五个退出码与各自触发条件（口径以 README「退出码」节为正本）。
EXIT_CODE_EPILOG = """\
退出码：
  0    正常收场（空闲态按 Ctrl+C 收场，或 `meeting post` 收尾干净）
  1    收尾有步骤 failed（或 post / replay 那一趟没做成、服务起步出意外）
  2    端口被占、用法错误、配置值非法
  3    启动前提不成立（config.json 损坏、主 log 不可写）
  130  收尾中途第二次 Ctrl+C（立即中止，盘上绝不写 done）
"""


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
    `EXIT_REJECTED`，终端打出来的却是另一套英文模板。这里改成 `error()` 抛 `ValueError`
    （那句话就是终端要打的原因），由入口函数打印用法说明并按 `EXIT_REJECTED` 收场；
    `-h` / `--help` 仍走 argparse 本来的出口（打印说明、按 `EXIT_OK` 退）。`allow_abbrev`
    关掉：开关只认全名，`--for` 这类缩写按未知参数当场拒绝，行为与既有口径保持一致。
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

        认不出的模板原样放行，让原文自己到终端说话。
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


#: 收尾失败档退出时往终端倒的服务 log 行数
LOG_TAIL_LINES_ON_FAILURE = 20


def _log() -> logging.Logger:
    return logging_setup.get_logger("cli")


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


def _parse_post_args(argv: Sequence[str]) -> dict[str, Any]:
    """解析 `meeting post` 后面那一串（argparse）：交回 `{"dir": <path>, "force": <bool>}`。

    **不猜目录、不挂菜单、不列目录内容**：位置参数缺了就报用法，
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


def _load_config_or_exit(
    *,
    config_source_after_rebuild: bool = False,
    log_rebuild_failure: bool = True,
) -> config_module.Config:
    """三入口（web / post / replay）共用的启动前奏：bootstrap 日志 → 读配置并生效 → 按生效配置重建服务级日志。

    时序不得对调：配置损坏 → `EXIT_STARTUP_UNMET`；取值非法 →
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
        # 主 log 不可写 → 拒绝启动
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

    # 配置层在幂等闸与花钱之前读进来（与 `web()` 同源）：这一趟里每个取值
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


def web() -> None:
    """`meeting web` —— 走启动序列，前台跑到 `Ctrl+C`。

    **退出码有五条出口，全都显式 `sys.exit(<常量>)`**（不许让异常冒到 uvicorn /
    解释器那里变成解释器默认码）：启动前提不成立 → `EXIT_STARTUP_UNMET`；配置值非法 /
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
        # 按收尾失败档的同一个常量收场（不许让异常冒到 uvicorn / 解释器，也不静默）。
        # 起步步骤没进服务器主循环，但同样是一条退出路径。
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
    干净那格抹成 `EXIT_OK`）。
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
    """退出时把现场打到终端——按真实档位说话，两档文案分开。

    强制打断档（第二次 `Ctrl+C`）：盘上留在 `draining` + `interrupted`，说「收尾被强制
    打断」那一套——不是「服务不自杀」（此前不看档位只按现场挑话，会在「强制打断 + 现场
    有 `failed` 步」打出误报）。收尾失败档（现场找得到 `failed` 步）：倒 log 路径与服务
    log 最后几行——判据看现场不看退出码（`EXIT_FAILURE`
    里还压着「`server.run()` 抛异常」这另一件事，那件事由 `web()` 自己说清）；其余情形
    一个字都不打。
    """
    if exit_code == EXIT_INTERRUPTED:
        # 强制打断档：先说档位，再说盘上留在哪一格与出路。
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
    "EXIT_FAILURE",
    "EXIT_INTERRUPTED",
    "EXIT_NAMES",
    "EXIT_OK",
    "EXIT_REJECTED",
    "EXIT_STARTUP_UNMET",
    "LOG_TAIL_LINES_ON_FAILURE",
    "POST_FLAGS",
    "main",
    "post",
    "web",
]
