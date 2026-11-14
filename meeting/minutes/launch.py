"""headless 启动器。

一件事：**把一个 DSH headless 子进程拉起来，然后校验它到底交没交出纪要**。

## 这一条分支只管 headless

本模块**只**管 headless 子进程这一条路：`subprocess.run` 等它退出、再按四条判据校验产物，
四档失败各记各的名字。`auto` 那一档现在由 `runner` 决定走哪条分支 —— 探到 DSH web 服务端
在跑就走**交付**那条路（`meeting/minutes/dsh_web.py`：`workspace/create` → `session/create`
→ `session/prompt`，投递 accepted 即算交付完成，终态记 `delivered`），探不到才落到这里。

两件事因此各归各位：**这一层能等的只有自己起的那个子进程**；交给 DSH 服务端那一趟跑多久
不归这一层管（用户停不停 DSH 是用户的事）。状态文件里 `branch` 那一格记**实际走的**那一条
（`headless` 或 `web`）。

## 三条硬口径

  - **argv 里只放一段短指针**：提示词正文写进 `working/run_prompt.md`，argv 只带一句「去读哪个
    文件」。`cmd.exe` 会在第一个换行处截断多行参数，
    正文走 argv 是把整份提示词赌在命令行解析上；
  - **绝不 `input()`、绝不弹窗**：这是一条无人值守路径，任何交互都在等一个不会来的回车。
    子进程一律不带窗口（本模块在 Windows 上给 `CREATE_NO_WINDOW`）；
  - **不能只看退出码**：退出码 0 只是必要条件，
    产物四条判据（`check_final_artifact()`）缺任何一条都算这一趟失败。

## 三档失败要分得开

  - **解析不到可执行文件**（`FAILURE_EXECUTABLE_MISSING`）：Windows 上 npm 全局安装只生成
    `.cmd` / `.ps1` shim，传裸名会 `WinError 2`，因此先 `shutil.which()` 解析真实路径；
  - **子进程退出码非 0**（`FAILURE_EXIT_CODE`）；

超时按失败处理，错误里写清超时秒数与提示词路径（超时那一档同样留 `FAILURE_TIMEOUT`）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meeting.minutes.inputs import write_text_atomic
from meeting.minutes.status import (
    ARTIFACT_FINAL as FINAL_NAME,
    ARTIFACT_LOG,
    BRANCH_AUTO,
    BRANCH_HEADLESS,
    BRANCH_WEB,
    RUN_PROMPT_NAME,
    WORKING_DIR,
    DeliveryForbidden,
    FAILURE_TIMEOUT,
    ISOLATION_GRANT,
    ISOLATION_MESSAGE,
    write_run_log,
)

#: 说话人附录那两段固定标题（启动器的产物校验按它们判）
APPENDIX_HEADING = "## 说话人附录"
UNKNOWN_HEADING = "### 猜不出的"

#: argv 里那段短指针（**只指盘上那份提示词**，正文一个字都不进 argv）
PROMPT_POINTER = (
    f"请读 `{WORKING_DIR}/{RUN_PROMPT_NAME}` 并完整执行其中的流程，当前目录即场次目录。"
)

#: 子进程 profile（headless 一次性语义：回合结束进程就退出）
HEADLESS_PROFILE = "headless"

#: 子进程环境里显式加上去的三格
ENV_PERMISSION_MODE = ("DSH_PERMISSION_MODE", "danger-full-access")
ENV_PYTHON_IO = ("PYTHONIOENCODING", "utf-8")
#: 再入闸的标记：子会话看到它就**不再拉起 DSH**（闸落在 `runner.run()` 的最前面）
ENV_CHILD_FLAG = "MEETING_MINUTES_CHILD"

#: 解析不到可执行文件时的那半句话（Windows 上 npm 只生成 `.cmd` / `.ps1` shim）
MISSING_EXECUTABLE_HINT = (
    "Windows 上 npm 全局安装只生成 .cmd / .ps1 shim，传裸名会 WinError 2；"
    "先 shutil.which(\"dsh\") 解析出真实路径，或者把 dsh 装到 PATH 上"
)

#: 三档失败的名字（读方按它分档，不必去解析错误消息）。`FAILURE_TIMEOUT` 的唯一定义点
#: 在 `status.py`（两条分支的失败词落进同一格 `dsh.failure`，同名词必须同值）。
FAILURE_EXECUTABLE_MISSING = "executable_missing"
FAILURE_EXIT_CODE = "exit_code"
FAILURE_ARTIFACT_MISSING = "artifact_missing"
FAILURES: tuple[str, ...] = (
    FAILURE_EXECUTABLE_MISSING,
    FAILURE_EXIT_CODE,
    FAILURE_ARTIFACT_MISSING,
    FAILURE_TIMEOUT,
)

#: 附录那张表算不算「写了东西」的格数门槛：表头与数据行都要有这么多格非空。
#: 3 这个数是**表头最窄的形状**（`| 路 | 编号 | 人名 | 把握 | 依据 |` 是 5 格，真产物里
#: 更短的写法不存在），比它少的行（`| — | — |` 这种凑格）不算一张表的一行。
MIN_TABLE_CELLS = 3

#: 产物校验的四条判据（缺哪一条就报哪一个词）
CHECK_FINAL_MISSING = "meeting_final.md 不在场次目录根"
CHECK_FINAL_EMPTY = "meeting_final.md 去空白之后是空的"
CHECK_APPENDIX_HEADING = f"meeting_final.md 里没有 `{APPENDIX_HEADING}`"
CHECK_UNKNOWN_HEADING = f"meeting_final.md 的附录里没有 `{UNKNOWN_HEADING}`"
CHECK_APPENDIX_ROW = "说话人附录那一节里一条数据行都没有（只有表头或只有空行）"

#: Windows 上不弹窗的那一格（POSIX 上没有这个常量）
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class LaunchError(RuntimeError):
    """启动器这一层自己的错误（场次目录为空这类调用方错误）。

    **「跑不起来」不走这里**：解析不到可执行文件、退出码非 0、产物缺失、超时四档都在
    `LaunchResult.failure` 里如实回答，调用方（`runner`）据此把状态记 `failed`。
    """


@dataclass(frozen=True)
class LaunchResult:
    """一次拉起的结果（成功与三档失败共用同一个形状）。

    `executable` 是**实际拉起的那个路径**（解析不到时是空字符串），`exit_code` 是子进程的退出码
    （没跑起来时是 `None`），`seconds` 是这一次的墙钟秒数，`log` 是那份 stdout / stderr 的落点，
    `checks` 是产物校验逐条报出来的问题（成功时是空元组）。
    """

    ok: bool
    branch: str
    executable: str
    exit_code: int | None
    seconds: float
    log: str
    prompt: str
    checks: tuple[str, ...] = ()
    failure: str | None = None
    error: str | None = None
    command: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        """状态文件里 `dsh` 那一格要的三样（`executable` / `exit_code` / `seconds`）。"""
        return {
            "executable": self.executable,
            "exit_code": self.exit_code,
            "seconds": round(float(self.seconds), 3),
            "branch": self.branch,
            "log": self.log,
            "failure": self.failure,
        }


def configured_branch(configured: str | None) -> str:
    """配置里的取值 → **这一层此刻能确定的那一条**（校验取值域，原样交回）。

    拉起之前落的那一版「在飞」状态（`MinutesHook.launch()` 建线程之前经 `mark_running()`
    落的那一份）走的是这一条：那一刻还没探 DSH web 服务端在不在跑，因此那一版 `branch`
    记的就是**配置里写的那个词**（`auto` / `headless` / `web`）；`runner` 探完之后落的
    下一版才记**实际走的**那一条。取值域不合法时当场抛 `LaunchError`。
    """
    value = (configured or BRANCH_AUTO).strip() or BRANCH_AUTO
    if value not in (BRANCH_AUTO, BRANCH_HEADLESS, BRANCH_WEB):
        raise LaunchError(f"启动分支只认 auto / headless / web，收到 {configured!r}")
    return value


def find_executable(name: str = "dsh", *, which: Any = None) -> str:
    """解析 DSH 可执行文件的真实路径；解析不到交回空字符串。

    `which` 是注入点（用例塞一个假的解析器，或者直接给一个假可执行文件的路径）。
    """
    resolver = shutil.which if which is None else which
    found = resolver(name)
    return str(found) if found else ""


def check_final_artifact(session_dir: str | Path) -> tuple[str, ...]:
    """校验 `meeting_final.md` 的四条判据，交回**缺的那几条**（全过就是空元组）。

    四条（逐条都有能触发的测试）：

      1. 文件在、去空白后非空；
      2. 里面有 `## 说话人附录`；
      3. 里面有 `### 猜不出的`；
      4. 附录那一节里有一张**真的写了东西**的表：表头（至少 3 格非空）+ 分隔行 + 至少一行
         数据行（同样至少 3 格非空）。一排排格子里填进了内容，这一节才算落了地。

    第 4 条只认「附录那一节里」的行：写在附录之前或之后的表格不算，否则一个只写了正文、
    附录是空壳的产物会被判合格。**认表头只看「一行表格够不够宽」，不看第一格写的是哪个词**
    （见 `_has_appendix_data_row()`）。
    """
    final = Path(session_dir) / FINAL_NAME
    if not final.is_file():
        return (CHECK_FINAL_MISSING,)
    text = final.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return (CHECK_FINAL_EMPTY,)
    missing: list[str] = []
    if APPENDIX_HEADING not in text:
        missing.append(CHECK_APPENDIX_HEADING)
    if UNKNOWN_HEADING not in text:
        missing.append(CHECK_UNKNOWN_HEADING)
    if APPENDIX_HEADING in text and UNKNOWN_HEADING in text:
        if not _has_appendix_data_row(text):
            missing.append(CHECK_APPENDIX_ROW)
    return tuple(missing)


def _appendix_body(text: str) -> str:
    """附录那一节的正文（从 `## 说话人附录` 到 `### 猜不出的` 之间，不含两处标题）。"""
    start = text.find(APPENDIX_HEADING) + len(APPENDIX_HEADING)
    end = text.find(UNKNOWN_HEADING)
    if end <= start:
        return text[start:]
    return text[start:end]


def _cells(line: str) -> list[str]:
    """一行表格 → 逐格的内容（去掉两边那两根竖线与每格前后的空白）。"""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_separator_row(cells: Sequence[str]) -> bool:
    """分隔行（`| --- | --- |`）：每一格都只由 `-` / `:` / 空白组成。

    `| | |` 这种一条格都没有的行落进「每一格都…」的空集里，得先把它挡在外面 —— 那种行既没有
    表头的宽度，也填不进任何内容。
    """
    return bool(cells) and all(set(cell) <= {"-", ":", " "} for cell in cells)


def _is_table_row(line: str) -> bool:
    """这一行是不是表格里的一行（去掉缩进后以 `|` 开头）。

    判据只看「有没有那根竖线」，**不看第一格写的是什么词**：表头第一格写成「路」/「路径」/
    「说话人」都仍是同一张表（`prompts/meeting_final.md` 里并没有「逐字照抄『路』」这条硬约束，
    模型换个词是够得着的）。
    """
    return line.strip().startswith("|")


def _row_cell_count(line: str) -> int:
    """这一行有几个**非空**格（`—` 是非空字符，所以「一格内容 + 四格 `—`」算 5 格）。"""
    return sum(1 for cell in _cells(line) if cell)


def _has_appendix_data_row(text: str) -> bool:
    """附录那一节里有没有一张**真的写了东西**的表（表头 + 分隔行 + 至少一行数据行）。

    三件事同时成立才算：表头（至少 `MIN_TABLE_CELLS` 格非空）、分隔行、以及分隔行之后
    至少一行数据行（同样至少 `MIN_TABLE_CELLS` 格非空）。**表头第一格写的是什么词不参与判断**：
    一张只有表头 + 分隔行、一条数据都没有的空附录不算「有数据行」。
    """
    lines = [
        line for line in _appendix_body(text).splitlines() if _is_table_row(line)
    ]
    header_index = next(
        (index for index, line in enumerate(lines) if _row_cell_count(line) >= MIN_TABLE_CELLS),
        None,
)
    if header_index is None:
        return False
    for line in lines[header_index + 1 :]:
        if _is_separator_row(_cells(line)):
            continue  # 分隔行（表头与数据行之间那一条）
        if _row_cell_count(line) >= MIN_TABLE_CELLS:
            return True
    return False


def build_child_env(
    base: Mapping[str, str] | None = None,
    *,
    child: bool = True,
) -> dict[str, str]:
    """子进程的环境：`os.environ` 的副本 + 三格显式取值。

    `DSH_PERMISSION_MODE=danger-full-access` 是 headless 的硬要求（没有界面应答审批），
    `PYTHONIOENCODING=utf-8` 是 Windows 上中文输出的口径，`MEETING_MINUTES_CHILD=1` 是**再入闸**
    （子会话看到它就不再拉起 DSH；这道闸不靠模型自觉）。
    """
    env = dict(os.environ if base is None else base)
    env[ENV_PERMISSION_MODE[0]] = ENV_PERMISSION_MODE[1]
    env[ENV_PYTHON_IO[0]] = ENV_PYTHON_IO[1]
    if child:
        env[ENV_CHILD_FLAG] = "1"
    return env


def render_command(executable: str, *, pointer: str = PROMPT_POINTER) -> tuple[str, ...]:
    """子进程的 argv（**正文一个字都不进 argv**，只放一段短指针）。"""
    return (executable, "--profile", HEADLESS_PROFILE, pointer)


def launch_headless(
    session_dir: str | Path,
    *,
    prompt_text: str,
    executable: str | None = None,
    timeout: float = 14400.0,
    config_selector: str | None = None,
    which: Any = None,
    runner: Any = None,
    env: Mapping[str, str] | None = None,
    allow_dsh: bool = False,
) -> LaunchResult:
    """把提示词写到盘上，拉起一个 headless DSH 子进程，再校验产物。

    `prompt_text` 是**已经渲染好的**提示词全文（`runner` 从 `prompt.render_session_prompt()`
    拿到它）；本函数把它写进 `working/run_prompt.md`，argv 里只放指过去的那一句话。

    `allow_dsh` 是**默认拒绝**的人工放行参数：不放行（缺省）就在**任何动作之前**当场抛
    `DeliveryForbidden` —— 目录不建、提示词不写盘、可执行文件不解析、子进程不起。放行标记
    由 `MinutesHook.launch(deliver=True)` 经 `runner.run()` 下传（两条人工入口才给真）；
    未来任何代码绕过纪要层直接 import 本函数，也起不来一个 DSH 子进程。

    `executable` 缺席时按 `shutil.which("dsh")` 解析（`which` 是解析器的注入点）；
    解析不到**当场**记 `FAILURE_EXECUTABLE_MISSING` 并说明 npm shim 那件事，不尝试拉起。

    `runner` 是 `subprocess.run` 的注入点：用例塞一个假实现（或者干脆用假可执行文件），
    产品路径上用真的。四档失败（解析不到 / 退出码非 0 / 产物缺失 / 超时）都在返回值里
    如实回答，**不抛异常** —— 调用方要的是「把这一趟记成 failed + 原因」。
    """
    if not allow_dsh:
        raise DeliveryForbidden(
            f"launch_headless() 被拒：{ISOLATION_MESSAGE}（{ISOLATION_GRANT}）"
)
    directory = Path(session_dir)
    if not str(directory):
        raise LaunchError("launch_headless 需要场次目录（收到空值）")
    directory.mkdir(parents=True, exist_ok=True)

    prompt_path = directory / WORKING_DIR / RUN_PROMPT_NAME
    write_text_atomic(prompt_path, prompt_text)
    relative_prompt = f"{WORKING_DIR}/{RUN_PROMPT_NAME}"
    log_path = directory / ARTIFACT_LOG
    branch = BRANCH_HEADLESS

    exe = executable if executable is not None else find_executable(which=which)
    if not exe:
        message = (
            f"解析不到 DSH 可执行文件（找的是 dsh）：{MISSING_EXECUTABLE_HINT}"
            f"；提示词已写在 {relative_prompt}"
)
        _write_log(
            log_path,
            command=("dsh", "--profile", HEADLESS_PROFILE, PROMPT_POINTER),
            cwd=directory,
            exit_code=None,
            error=message,
            stdout="",
            stderr="",
)
        return LaunchResult(
            ok=False,
            branch=branch,
            executable="",
            exit_code=None,
            seconds=0.0,
            log=ARTIFACT_LOG,
            prompt=relative_prompt,
            failure=FAILURE_EXECUTABLE_MISSING,
            error=message,
            command=("dsh",),
)

    command = render_command(exe)
    child_env = build_child_env(env)
    spawn = subprocess.run if runner is None else runner
    begun = time.monotonic()
    completed: Any = None
    timeout_error: str | None = None
    try:
        completed = spawn(
            list(command),
            cwd=str(directory),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=float(timeout),
            env=child_env,
            creationflags=_NO_WINDOW,
)
    except subprocess.TimeoutExpired as exc:
        timeout_error = (
            f"DSH 子进程超时：{float(timeout)} 秒（提示词 {relative_prompt}）——"
            f"按失败处理，这一趟没有可用的纪要"
)
        completed = exc
    seconds = time.monotonic() - begun

    stdout = _text_of(getattr(completed, "stdout", None))
    stderr = _text_of(getattr(completed, "stderr", None))
    exit_code = getattr(completed, "returncode", None)
    _write_log(
        log_path,
        command=command,
        cwd=directory,
        exit_code=exit_code,
        error=timeout_error,
        stdout=stdout,
        stderr=stderr,
        seconds=seconds,
)

    if timeout_error is not None:
        return LaunchResult(
            ok=False,
            branch=branch,
            executable=exe,
            exit_code=exit_code if isinstance(exit_code, int) else None,
            seconds=seconds,
            log=ARTIFACT_LOG,
            prompt=relative_prompt,
            failure=FAILURE_TIMEOUT,
            error=timeout_error,
            command=command,
)

    if exit_code != 0:
        message = (
            f"DSH 子进程退出码非 0：{exit_code}（命令 {_command_text(command)}；"
            f"cwd {directory}；stdout/stderr 见 {ARTIFACT_LOG}）"
)
        return LaunchResult(
            ok=False,
            branch=branch,
            executable=exe,
            exit_code=exit_code,
            seconds=seconds,
            log=ARTIFACT_LOG,
            prompt=relative_prompt,
            failure=FAILURE_EXIT_CODE,
            error=message,
            command=command,
)

    checks = check_final_artifact(directory)
    if checks:
        message = (
            f"DSH 子进程退出码 0，但产物没落盘 / 不合判据：{'；'.join(checks)}"
            f"（提示词 {relative_prompt}；stdout/stderr 见 {ARTIFACT_LOG}）"
)
        return LaunchResult(
            ok=False,
            branch=branch,
            executable=exe,
            exit_code=exit_code,
            seconds=seconds,
            log=ARTIFACT_LOG,
            prompt=relative_prompt,
            checks=checks,
            failure=FAILURE_ARTIFACT_MISSING,
            error=message,
            command=command,
)

    return LaunchResult(
        ok=True,
        branch=branch,
        executable=exe,
        exit_code=exit_code,
        seconds=seconds,
        log=ARTIFACT_LOG,
        prompt=relative_prompt,
        command=command,
)


def _text_of(value: Any) -> str:
    """子进程那两股输出（`None` / `bytes` 都收）：一律按文本处理。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _command_text(command: Sequence[str]) -> str:
    return " ".join(str(item) for item in command)


def _write_log(
    path: Path,
    *,
    command: Sequence[str],
    cwd: Path,
    exit_code: int | None,
    error: str | None,
    stdout: str,
    stderr: str,
    seconds: float | None = None,
) -> None:
    """把这一趟的记录写进 `working/minutes-run.log`（**头一行是命令、cwd 与退出码**）。

    写盘骨架走 `status.write_run_log()`（两路共用的唯一实现），这里只出本分支的
    头行与分节。头那一行是给事后排查用的：现场出问题时先看的就是「跑的是哪条命令、
    在哪个目录、退出码是多少」。
    """
    header = [
        f"# 命令：{_command_text(command)}",
        f"# cwd：{cwd}",
        f"# 退出码：{'（没跑起来）' if exit_code is None else exit_code}"
        + ("" if seconds is None else f"；耗时：{float(seconds):.3f}s"),
    ]
    if error:
        header.append(f"# 失败：{error}")
    write_run_log(
        path,
        header=header,
        sections=(("stdout", stdout.rstrip("\n")), ("stderr", stderr.rstrip("\n"))),
)


def interpreter() -> str:
    """当前解释器的路径（假可执行文件与文档里那两条命令都用它）。"""
    return sys.executable


__all__ = [
    "APPENDIX_HEADING",
    "CHECK_APPENDIX_HEADING",
    "CHECK_APPENDIX_ROW",
    "CHECK_FINAL_EMPTY",
    "CHECK_FINAL_MISSING",
    "CHECK_UNKNOWN_HEADING",
    "DeliveryForbidden",
    "ENV_CHILD_FLAG",
    "ENV_PERMISSION_MODE",
    "ENV_PYTHON_IO",
    "FAILURES",
    "FAILURE_ARTIFACT_MISSING",
    "FAILURE_EXECUTABLE_MISSING",
    "FAILURE_EXIT_CODE",
    "FAILURE_TIMEOUT",
    "FINAL_NAME",
    "HEADLESS_PROFILE",
    "LaunchError",
    "LaunchResult",
    "MISSING_EXECUTABLE_HINT",
    "MIN_TABLE_CELLS",
    "PROMPT_POINTER",
    "RUN_PROMPT_NAME",
    "UNKNOWN_HEADING",
    "build_child_env",
    "check_final_artifact",
    "find_executable",
    "interpreter",
    "launch_headless",
    "render_command",
    "configured_branch",
    "WORKING_DIR",
]
