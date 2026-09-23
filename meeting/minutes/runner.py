"""会后纪要生成的一次运行（`41465462` spec「自动跑挂在哪一步」那一层的编排）。

一次 `run(session_dir)` 走完这些事：

    再入闸（子会话里不再拉起 DSH）→ 落一份 `state="running"` 的状态（读方据此分得出
    「没跑」与「正在跑」）→ 找输入 → 写 `working/inputs.json` → 分块
    → 渲染提示词写 `working/run_prompt.md` → 拉起 headless DSH → 校验产物
    → 把 `meeting_minutes_status.json` 覆盖成 `done` / `failed` → 把那份状态交回调用方

## 先落 `running` 再拉起 DSH

一次生成最长 14400 秒（`minutes.timeout_seconds` 的上界是 86400）。早先状态文件是**跑完才第一次
落盘**，这期间读方（`meeting post`、界面）看到的是一份不存在的文件，「这一场还没跑」与
「这一场正在跑」分不出来。因此拉起之前先写一份 `running`（带 `started_at` / `mode` / `branch`
与空的 `dsh` 那一格），终局那一趟再覆盖它 —— `meeting/backends/minutes.py` 那句
「不会永远停在 `running`」于是有了对象：进程被杀时，`_mark_failed()` 就是把这一份改成 `failed`。

再入闸那一档（`MEETING_MINUTES_CHILD=1`）**不写状态文件**，那条口径一个字没动。

## 顺序不能反

离线那条转写**就是这一层的输入**（spec：「先出纪要就会永远只有降级档」）。调用方
（`meeting/offline/runner.py` 的尾巴、`meeting post`）必须在三份离线产物落完之后调它，
本模块不自己去等、也不去探测离线链的状态。

## 再入闸放在最前面

`MEETING_MINUTES_CHILD=1`（启动器给子进程加的那一格）说明**这一趟是 DSH 子会话自己**在跑：
它再做一遍分块、再拉起一个 DSH，就是无限套娃。闸落在**配置解析层**（本函数的第一件事），
不靠提示词里的嘱咐 —— 提示词那道闸防的是模型顺着 `prompts/` 自己摸到入口，两道闸各管一头。

这一档**不写状态文件**、返回一份 `state = "skipped"` 的状态（`skipped` 不在三档里：
它既没有跑起来，也不算失败），原因写在那份返回值里。选这一条而不是「写一份 skipped 状态」
的理由：子会话的场次目录里那份状态文件归**父进程那一趟**所有，子会话去覆盖它会让
「这一趟跑成什么样」这件事在盘上被后写的子进程擦掉。

## 注入点

`launcher` 与 `executable` 都是接缝：用例塞一个假启动器（或一个假可执行文件），
产品路径上走 `launch.launch_headless` 与 `shutil.which("dsh")`。**测试里绝不真调系统上的 dsh**。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from meeting import config as config_module
from meeting.minutes import launch as launch_module
from meeting.minutes import status as status_module
from meeting.minutes.chunk import DEFAULT_CHUNK_LINES, chunk_session, discover_inputs, mode_for
from meeting.minutes.contract import DEFAULT_MAX_SEGMENTS, expected_lines_from_chunks
from meeting.minutes.inputs import (
    MODE_NONE,
    SourceEntry,
    build_inputs_document,
    entries_for_session,
    inputs_path,
    write_inputs,
)
from meeting.minutes.prompt import PromptTemplateError, render_session_prompt
from meeting.minutes.status import ARTIFACT_FINAL, ARTIFACT_INPUTS, ARTIFACT_LOG

_log = logging.getLogger("meeting.minutes.runner")

#: `minutes.hook` 的缺省取值（与 `config.DEFAULT_VALUES` 同一格同值）
DEFAULT_HOOK = "auto"

#: `minutes.timeout_seconds` 的缺省（四小时：R8 的「会后可以挂到第二天早上」）
DEFAULT_TIMEOUT_SECONDS = 14400.0

#: 一次运行的墙钟上限兜底（配置层取值域的上界是 86400，这里不另设口径，只防 0/负数）
MIN_TIMEOUT_SECONDS = 1.0

#: 状态文件里 `artifacts` 那三格（相对场次目录的路径）—— 跑起来那一份与终局那一份同源
_STATUS_ARTIFACTS: dict[str, str] = {
    "final": ARTIFACT_FINAL,
    "inputs": ARTIFACT_INPUTS,
    "log": ARTIFACT_LOG,
}


def _status_document(
    directory: Path,
    *,
    state: str,
    mode: str,
    branch: str,
    started_at: str,
    error: str | None = None,
    result: Any = None,
    with_dsh: bool = False,
) -> dict[str, Any]:
    """落一份状态文件并把它交回调用方（本模块的唯一记账出口）。

    `state = "running"` 那一份（拉起 DSH **之前**落）走的是同一条路：收尾时刻**明写成空串**
    —— `write_session_status()` 把 `None` 当成「调用方没给」并补上当前时刻（那条口径给终局
    那几份用），而还在跑的这一份要的正是「收尾时刻空着」，读方据此分得出「正在跑」。
    """
    finished_at = "" if state == status_module.STATE_RUNNING else status_module.now_iso()
    document = status_module.write_session_status(
        directory,
        state=state,
        mode=mode,
        branch=branch,
        started_at=started_at,
        finished_at=finished_at,
        error=error,
        artifacts=_STATUS_ARTIFACTS,
        dsh=result.as_dict() if (with_dsh and result is not None) else {},
    )
    document["session_dir"] = str(directory)
    document["status"] = status_module.STATUS_NAME
    return document


def run(
    session_dir: str | Path,
    *,
    config: Any = None,
    launcher: Callable[..., Any] | None = None,
    executable: str | None = None,
    which: Any = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """对一场会跑一次纪要生成，返回状态 dict（与 `meeting_minutes_status.json` 同形）。

    `launcher` 是启动器的注入点（默认 `launch.launch_headless`）；`executable` 直接给定
    「要拉起哪个可执行文件」（用例塞假的那个），不给就按 `shutil.which("dsh")` 解析。

    `config` 是可选的那一份配置对象（`meeting.config.Config`），缺省读进程当前生效值。
    """
    directory = Path(session_dir) if session_dir else None
    if directory is None or not str(directory):
        raise ValueError("minutes.runner.run 需要场次目录（收到空值）")
    directory.mkdir(parents=True, exist_ok=True)

    env = os.environ if environ is None else environ
    session_id = directory.name
    started_at = status_module.now_iso()
    branch = launch_module.resolve_branch(_config_value("minutes.hook", config, DEFAULT_HOOK))

    # 再入闸：子会话里不再拉起 DSH，也不写状态文件（见模块头）
    if str(env.get(launch_module.ENV_CHILD_FLAG, "")).strip() == "1":
        return {
            **status_module.build_status(
                session_id=session_id,
                state=status_module.STATE_SKIPPED,
                mode=MODE_NONE,
                branch=branch,
                started_at=started_at,
                finished_at=started_at,
                error=(
                    f"再入闸：环境里有 {launch_module.ENV_CHILD_FLAG}=1，"
                    "这一趟是 DSH 子会话自己，不再拉起 DSH、不写状态文件"
                ),
            ),
            "session_dir": str(directory),
            "status": status_module.STATUS_NAME,
        }

    chunk_lines = _config_value("minutes.chunk_lines", config, DEFAULT_CHUNK_LINES)
    max_segments = _config_value("minutes.max_segments", config, DEFAULT_MAX_SEGMENTS)
    timeout = _timeout_seconds(config)
    spawn = launch_module.launch_headless if launcher is None else launcher

    def settle(
        state: str,
        *,
        error: str | None = None,
        result: Any = None,
        mode: str = MODE_NONE,
        with_dsh: bool = False,
    ) -> dict[str, Any]:
        """落一份终态状态文件并把它交回调用方（`_status_document()` 的终局入口）。"""
        return _status_document(
            directory,
            state=state,
            mode=mode,
            branch=branch,
            started_at=started_at,
            error=error,
            result=result,
            with_dsh=with_dsh,
        )

    # 零、拉起之前先落一份 `running`：这一趟最长 14400 秒，读方（`meeting post`、界面）
    # 在这期间要分得出「没跑」与「正在跑」。终局那一份随后覆盖它。
    _status_document(
        directory,
        state=status_module.STATE_RUNNING,
        mode=MODE_NONE,
        branch=branch,
        started_at=started_at,
    )

    # 一、找输入 → 写 inputs.json（**清单照落**：读方要看得见这一场查过了）
    entries: tuple[SourceEntry, ...] = entries_for_session(directory)
    document = build_inputs_document(directory, entries)
    mode = document["mode"]
    write_inputs(inputs_path(directory), document)
    if mode == MODE_NONE:
        # 一条转写都没有：**不启动 DSH**（spec：这一档记 failed + 原因，不许静默出空纪要）
        message = (
            f"这一场的输入一条都没有（找的是 meeting_transcript_<source>.md 与 "
            f"meeting_offline_transcript_<source>.md），不启动 DSH、不出纪要：{directory}"
        )
        _log.error("会后纪要：%s", message)
        return settle(status_module.STATE_FAILED, error=message)

    # 二、分块（每一条输入各切各的：两条都在时切的是会后那份权威稿）
    try:
        chunks = chunk_session(directory, discover_inputs(directory), chunk_lines=chunk_lines)
    except Exception as exc:
        message = f"切块失败：{type(exc).__name__}: {exc}"
        _log.error("会后纪要：%s", message)
        return settle(status_module.STATE_FAILED, error=message, mode=mode)

    # 三、渲染提示词（模板归 prompts/meeting_final.md；渲染不出来就不启动子会话）
    try:
        prompt_text = render_session_prompt(
            directory, mode=mode, max_segments=int(max_segments)
        )
    except PromptTemplateError as exc:
        message = f"提示词渲染失败：{exc}"
        _log.error("会后纪要：%s", message)
        return settle(status_module.STATE_FAILED, error=message, mode=mode)

    # 四、拉起 DSH 子进程（启动器自己写 working/run_prompt.md 并校验产物）
    result = spawn(
        directory,
        prompt_text=prompt_text,
        executable=executable,
        timeout=timeout,
        config_selector=_config_value("minutes.hook", config, DEFAULT_HOOK),
        which=which,
    )
    if not getattr(result, "ok", False):
        message = str(getattr(result, "error", "") or "启动器报告失败")
        _log.error("会后纪要：%s", message)
        return settle(
            status_module.STATE_FAILED, error=message, result=result, mode=mode, with_dsh=True
        )

    # 五、终态（产物校验已经在启动器里过了：走到这里说明四条判据都成立）
    stem = _chunk_summary(chunks)
    return settle(
        status_module.STATE_DONE,
        error=None,
        result=result,
        mode=mode,
        with_dsh=True,
    ) | {"chunks": stem}


def _chunk_summary(chunks: Mapping[str, Any]) -> dict[str, Any]:
    """分块那一趟的账（每条 source 的行数与块数），进状态文件之外的返回值。

    状态文件的形状是冻结的（spec 那张表），因此这一格只出现在返回值里：`meeting post`
    与用例用它核「切了几块」。
    """
    return {
        str(source): {
            "transcript": getattr(report, "transcript", ""),
            "total_lines": int(getattr(report, "total_lines", 0)),
            "chunk_count": int(getattr(report, "chunk_count", 0)),
        }
        for source, report in chunks.items()
    }


def expected_lines_for(session_dir: str | Path, chunks: Mapping[str, Any]) -> dict[str, int]:
    """`chunk_session()` 的结果 → 每条 source 的真实行数（契约校验的输入）。"""
    return expected_lines_from_chunks(chunks)


def _timeout_seconds(config: Any = None) -> float:
    """`minutes.timeout_seconds` 的生效值（秒）；拿不到或不是正数时退回缺省档。"""
    raw = _config_value("minutes.timeout_seconds", config, DEFAULT_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    if value < MIN_TIMEOUT_SECONDS:
        return DEFAULT_TIMEOUT_SECONDS
    return value


def _config_value(key: str, config: Any, fallback: Any) -> Any:
    """取一个配置键的生效值（**注入的配置对象优先，其次进程当前生效值**）。

    与 `meeting/offline/runner.py` 的 `read_chunk_seconds()` 同一条路子：注入对象先看，
    没有就走模块级 `config.get()`（它为未 `load()` 的进程退回默认值表）。取值域由
    `config.validate_value()` 把关，这里只兜「调用方塞了一份残缺的配置对象」。
    """
    getter = getattr(config, "get", None)
    if callable(getter):
        raw = getter(key)
        if raw is not None:
            return raw
    try:
        value = config_module.get(key)
    except Exception:  # pragma: no cover - 配置层缺席时退回本模块的缺省档
        return fallback
    return fallback if value is None else value


def hook_selector(config: Any = None) -> str:
    """`minutes.hook` 的生效取值（`auto` / `null` / `dev` / `module:Class`）。"""
    return str(_config_value("minutes.hook", config, DEFAULT_HOOK))


def chunk_lines_of(config: Any = None) -> int:
    """`minutes.chunk_lines` 的生效取值。"""
    return int(_config_value("minutes.chunk_lines", config, DEFAULT_CHUNK_LINES))


def max_segments_of(config: Any = None) -> int:
    """`minutes.max_segments` 的生效取值。"""
    return int(_config_value("minutes.max_segments", config, DEFAULT_MAX_SEGMENTS))


def mode_of(session_dir: str | Path) -> str | None:
    """这一场按盘上的输入落在哪一档（`chunk.mode_for()` 的快捷入口）。"""
    return mode_for(discover_inputs(session_dir))


__all__ = [
    "DEFAULT_HOOK",
    "DEFAULT_TIMEOUT_SECONDS",
    "MIN_TIMEOUT_SECONDS",
    "chunk_lines_of",
    "expected_lines_for",
    "hook_selector",
    "max_segments_of",
    "mode_of",
    "run",
]
