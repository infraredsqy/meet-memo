"""离线链尾巴上「怎么拉起纪要」的编排（自 `meeting/offline/runner` 拆出）。

`run_session()` 的三份产物与状态文件都落定之后，还要按 `minutes.hook` 把会后纪要
那一层拉起来 —— 这一段（通知、隔离闸、终态闸、实现拉起）与离线转写主链
（取音频 / 切块 / 静音门 / 产物落盘）**没有共享状态**，因此单独成模块，
`runner.run_session()` 与 `meeting post` 的人工入口各留一处调用点。

依赖方向只有一条：调用方（`meeting.offline.runner` 与 `meeting.cli`）import 本模块；
本模块**不** import 它们。闸与判据照旧走函数体内 import（`meeting.minutes` 那一包
不进本模块的顶层 import 图）。通知与打桩都经本模块本体的属性
（`minutes_tail._announce_minutes` 等）：cli 与用例只认这一处，编排器不在自己的
命名空间里替它转发。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

#: 会后纪要那一层的状态文件名（`meeting/minutes/status.py` 的 `STATUS_NAME` 同值）。
#: 这里只留一个字符串常量、**不 import 那个包**：离线这条链不该因为多一个钩子就把
#: `meeting.minutes` 整包拉进自己的 import 图（那一包的模块归钩子自己去 import）。
MINUTES_STATUS_NAME = "meeting_minutes_status.json"

#: 会后纪要那一层的产物名（`meeting/minutes/launch.py` 的 `FINAL_NAME` 同值；同上，
#: 只留字符串常量）。起跑之前那道闸（`_minutes_gate()`）按它校验「产物落没落、合不合判据」。
MINUTES_FINAL_NAME = "meeting_final.md"

#: 隔离闸的话术（`meeting/minutes/status.py` 的 `ISOLATION_MESSAGE` 同值；同上，
#: 只留字符串常量）。离线链拉起纪要的开关**缺省假**：只有 `meeting post` 这条人工入口
#: 显式传 `announce_minutes=True`，其余调用（测试、脚本、库调用）一律安静跳过。
MINUTES_GATE_MESSAGE = "纪要层隔离：本次非人工发起，跳过"


def _announce_minutes(
    session_dir: Path | str,
    *,
    launcher: Any = None,
    force: bool = False,
    announce_minutes: bool = False,
    deadline: float | None = None,
    status_reader: Any = None,
    artifact_checker: Any = None,
    captured: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """三份离线产物与状态文件都落定之后，通知一次会后纪要那一层（`minutes.hook`）。

    **顺序不能反**：离线转写就是纪要层的输入，先出纪要就永远只有降级档；调用点落在
    `run_session()` 最后。四条边界一条不放宽：

    - `announce_minutes` 缺省 `False`：隔离闸——不解析钩子、不调启动器、盘上零写动作。
      只有 `meeting post` 两条人工入口显式传 `True`（放行后作为 `deliver=True` 下传，
      钩子里的隔离闸逐层放行）；
    - `minutes.hook = null` → 解析成缺席档安静跳过；`MEETING_MINUTES_CHILD=1` → 同样跳过
      （但调用方显式递了 `launcher` 的替身档照常调到，用例靠它的登记核有没有再花钱）；
    - 这一层炸了**不许影响离线三份产物**、也不许改 `meeting_offline_status.json`
      （`try/except BaseException` 兜住只记 log）；
    - 拉起之前还有 `_minutes_gate()`：已 `done` 且产物过判据时不再拉起（R8 的
      「反复重跑不重复拉子会话」在自动入口上的落点）；`force = True` 跳过那道闸。

    `launcher` / `force` / `deadline` / `status_reader` / `artifact_checker` / `captured`
    均为注入点与透传格（缺省走产品路径）。返回值只进 `run_session()` 摘要、**不写进任何
    产物**——界面与下游读的是 `meeting_minutes_status.json`（归纪要层自己写）。
    """
    import os as _os

    if not announce_minutes:
        # 隔离闸：非人工发起，一切之前安静跳过。
        _log.info("%s（场次目录 %s）", MINUTES_GATE_MESSAGE, session_dir)
        return {"hook": "skipped", "announced": False, "reason": MINUTES_GATE_MESSAGE}

    in_child_session = str(_os.environ.get("MEETING_MINUTES_CHILD", "")).strip() == "1"
    if in_child_session and launcher is None:
        return {"hook": "skipped", "announced": False, "reason": "子会话（MEETING_MINUTES_CHILD=1）"}

    already = _minutes_gate(
        session_dir,
        force=force,
        status_reader=status_reader,
        artifact_checker=artifact_checker,
    )
    if already is not None:
        _log.info(
            "会后纪要已是终态且产物过判据，这一趟不再拉起子会话：%s", session_dir
        )
        return already

    minutes_status_path = Path(session_dir) / MINUTES_STATUS_NAME
    if launcher is not None:
        # 调用方递了替身：不解析 minutes.hook 的实现（替的就是它），直接登记；
        # 替身认 force/deliver 就递下去，只收场次目录的老形状按位置参调。
        kwargs: dict[str, Any] = {}
        if _accepts_keyword(launcher, "force"):
            kwargs["force"] = bool(force)
        if _accepts_keyword(launcher, "deliver"):
            kwargs["deliver"] = True
        returned: Any = launcher(str(session_dir), **kwargs)
        if captured is not None:
            captured["returned"] = returned
        return {
            "hook": "launched",
            "announced": True,
            "selector": "injected",
            "status": MINUTES_STATUS_NAME,
            "status_path": str(minutes_status_path),
        }

    try:
        from meeting.backends.minutes import AlreadyRunningError
        from meeting.backends.registry import resolve_minutes_hook

        resolution = resolve_minutes_hook()
        if not resolution.has_implementation:
            _log.info("会后纪要未启用（minutes.hook = %s）：这一场不产生纪要", resolution.selector)
            return {
                "hook": "absent",
                "announced": False,
                "selector": resolution.selector,
                "reason": "minutes.hook 缺席（自动跑已关掉）",
            }
        _launch_minutes_implementation(
            resolution.implementation, session_dir, force=force, deliver=bool(announce_minutes)
        )
        if captured is not None:
            captured["returned"] = None
        # 返回即算启动成功（与收尾第 4 步同一条契约）：后台线程跑成什么样由状态文件回答。
        return {
            "hook": "launched",
            "announced": True,
            "selector": resolution.selector,
            "status": MINUTES_STATUS_NAME,
            "status_path": str(minutes_status_path),
        }
    except AlreadyRunningError as exc:
        # 这一场已经在飞：launch() 建线程前用 claim 锁把「读现状 + 写 running」裹成
        # 跨进程原子段，读到 running 就抛这一档——本趟没起线程没拉子会话。
        # 如实报 running，cli 据此走现成的拒跑档（EXIT_REJECTED + --force 那句话）。
        _log.warning("会后纪要那一层已经在飞，这一趟不再拉起：%s（%s）", session_dir, exc)
        return {
            "hook": "running",
            "announced": False,
            "selector": "already_running",
            "reason": str(exc),
            "status": MINUTES_STATUS_NAME,
            "status_path": str(minutes_status_path),
        }
    except BaseException:  # 纪要这一层炸了绝不带走离线产物，也不改离线状态文件
        _log.exception(
            "会后纪要那一层没能拉起来（离线产物与状态文件照旧）：%s", session_dir
        )
        return {"hook": "failed", "announced": False, "reason": "纪要钩子抛出异常（详见主 log）"}


def _launch_minutes_implementation(
    implementation: Any,
    session_dir: Path | str,
    *,
    force: bool = False,
    deliver: bool = False,
) -> None:
    """把纪要那一层的实现叫起来（带上本趟 `--force` 与人工放行两格）。

    真实现的 `launch()` 收 `force` 与 `deliver`：建线程前过隔离闸、读盘上状态，读到
    `running` 拒跑，带 `--force` 放行。收不收某一格由实现自己的签名回答
    （`_accepts_keyword()`）：替身按老形状只收场次目录时照旧按位置参调。
    """
    call = implementation.launch
    kwargs: dict[str, Any] = {}
    if _accepts_keyword(call, "force"):
        kwargs["force"] = bool(force)
    if _accepts_keyword(call, "deliver"):
        kwargs["deliver"] = bool(deliver)
    if kwargs:
        call(str(session_dir), **kwargs)
        return
    call(str(session_dir))


def _accepts_keyword(call: Any, name: str) -> bool:
    """这个可调用对象收不收名为 `name` 的关键字（看不出来时按「不收」算）。"""
    import inspect

    try:
        parameters = inspect.signature(call).parameters
    except (TypeError, ValueError):  # pragma: no cover - 签名看不出来的那些可调用对象
        return False
    if name in parameters:
        return True
    return any(item.kind is item.VAR_KEYWORD for item in parameters.values())


def _minutes_gate(
    session_dir: Path | str,
    *,
    force: bool = False,
    status_reader: Any = None,
    artifact_checker: Any = None,
) -> dict[str, Any] | None:
    """纪要**起跑之前**那道闸：产物过判据时交回 `already_done`（done/delivered 都算），
    已交付而产物还没落交回 `delivered`（交付那条路的语义是投递即完事，不猜它写没写完；
    再拉第二个会话只会让同一场生成两遍），其余（没过判据、running/failed、没状态文件）
    交回 `None` 放行给启动器按「缺什么补什么」重跑。`force = True` 上面几条都不做直接放行。

    判据实现都借现成的（`minutes.status.read_status()` 逐字段校验、
    `check_final_artifact()`，都是函数体内 import——本模块顶层不拉 `meeting.minutes`）；
    本函数**只读盘**。`status_reader` / `artifact_checker` 是用例接缝。
    """
    if force:
        _log.info("会后纪要被 --force 重跑：这一趟不看终态，直接拉起：%s", session_dir)
        return None

    # 纪要那一层的四档词表（函数体内 import：本模块顶层不拉 meeting.minutes）
    from meeting.minutes.status import STATE_DELIVERED, STATE_FAILED, STATE_RUNNING

    directory = Path(session_dir)
    status_path = directory / MINUTES_STATUS_NAME
    if not status_path.is_file():
        return None

    reader = status_reader if status_reader is not None else _read_minutes_status
    checker = artifact_checker if artifact_checker is not None else _final_artifact_check
    try:
        document = reader(status_path)
    except Exception:  # pragma: no cover - 读不动就按「没跑完」放行，交给启动器
        _log.warning("会后纪要状态文件读不动，照常拉起：%s", status_path, exc_info=True)
        return None
    if not isinstance(document, dict):
        return None
    state = str(document.get("state") or "")
    if state in (STATE_RUNNING, STATE_FAILED, ""):
        # 没跑完 / 跑砸了 / 状态文件里那一格读不出来：放行给启动器（现状口径一个字没动）
        return None

    try:
        missing = tuple(checker(directory) or ())
    except Exception:  # pragma: no cover - 判据本身出错时同样按「没跑完」放行
        _log.warning("会后纪要产物判据跑不动，照常拉起：%s", directory, exc_info=True)
        return None

    if not missing:
        return {
            "hook": "already_done",
            "announced": False,
            "state": state or "done",
            "status": MINUTES_STATUS_NAME,
            "status_path": str(status_path),
            "final": str(directory / MINUTES_FINAL_NAME),
            "reason": "纪要已是终态且 meeting_final.md 过得了那四条判据，不再拉起子会话",
        }

    if state == STATE_DELIVERED:
        # 已经交付过 DSH web 服务端，产物还没落：**不重复拉起**（再拉一趟会让同一场生成两遍）
        _log.info("会后纪要已交付 DSH web 服务端（产物还没落），这一趟不再拉起：%s", session_dir)
        return {
            "hook": "delivered",
            "announced": False,
            "state": "delivered",
            "status": MINUTES_STATUS_NAME,
            "status_path": str(status_path),
            "final": None,
            "reason": (
                "这一场已经交付 DSH web 服务端生成（meeting_final.md 还没落盘）："
                "这一趟不再拉起第二个会话；要重新交付加 --force"
            ),
        }

    return None


def _read_minutes_status(path: Path) -> dict[str, Any]:
    """读纪要层的状态文件——**走那一层现成的读方**（逐字段校验，缺字段的残文件在这里
    就被拒收，闸据此按「没跑完」放行，而不是把 `{"state": "done"}` 当成已跑完）。
    坏 JSON / 不合约抛 `StatusError`，调用方接住放行。函数体内 import（惰性纪律）。
    """
    from meeting.minutes.status import read_status

    return read_status(path)


def _final_artifact_check(session_dir: Path | str) -> tuple[str, ...]:
    """产物判据的现成实现（**函数体内 import**：本模块顶层不拉 `meeting.minutes` 那一包）。"""
    from meeting.minutes.launch import check_final_artifact

    return check_final_artifact(session_dir)

