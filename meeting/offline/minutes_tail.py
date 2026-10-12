"""离线链尾巴上「怎么拉起纪要」的编排（自 `meeting/offline/runner.py` 拆出）。

`run_session()` 的三份产物与状态文件都落定之后，还要按 `minutes.hook` 把会后纪要
那一层拉起来 —— 这一段（通知、隔离闸、终态闸、实现拉起）与离线转写主链
（取音频 / 切块 / 静音门 / 产物落盘）**没有共享状态**，因此单独成模块，
`runner.run_session()` 只留一处调用点。

依赖方向只有一条：`meeting.offline.runner` import 本模块；本模块**不** import
`runner`。闸与判据照旧走函数体内 import（`meeting.minutes` 那一包不进本模块的
顶层 import 图）；`runner` 顶层把下面的名字 re-import 进自己的命名空间，
`run_session()` 的调用点与既有用例经 `offline_runner._announce_minutes` 等的
属性访问、打桩面都照旧。
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

    **顺序不能反**：离线这条转写就是纪要那一层的输入，先出纪要就永远只有降级档
    （spec「自动跑挂在哪一步」）。调用点因此落在 `run_session()` 的**最后**：状态文件的
    终态已经写完了，纪要这一层读到的东西才是齐的。

    `announce_minutes` 是**人工放行开关**（缺省 `False`）：不放行就在一切之前安静跳过 ——
    不解析钩子、不调启动器、盘上零写动作，只留主 log 一行。整个项目与 DSH 隔离，只有
    `meeting post` 两条路（首跑的尾巴与「离线段已终态、只补纪要」的直调）显式传 `True`；
    放行之后再把这一格作为 `deliver=True` 下传给真实现（`MinutesHook.launch(deliver=True)`），
    钩子里那道隔离闸因此逐层放行。

    四条边界，一条都不许放宽：

      - 这一层炸了**不许影响离线那三份产物**，也不许改 `meeting_offline_status.json`
        —— 整段用 `try/except BaseException` 兜住，只在 logger 上记一条；
      - 非人工发起（`announce_minutes = False`）→ **安静跳过**（隔离闸）；
      - `minutes.hook = null`（配置里写 JSON `null`）→ 解析成缺席档 → **安静跳过**；
      - 环境里有 `MEETING_MINUTES_CHILD=1`（这一趟是 DSH 子会话自己）→ 同样安静跳过
        （再入闸还有一道在 `meeting.minutes.runner.run()` 的最前面，这里早一步省掉一次解析）。

    **拉起之前还有一道闸**（`_minutes_gate()`）：纪要那一层的终态已是 `done` 且
    `meeting_final.md` 过得了那四条判据时**不调启动器**，交回
    `{"hook": "already_done", "announced": False, …}` —— R8 的「反复重跑不再拉起子会话」
    在**自动这条入口**上的落点（`meeting post` 的重跑可以不进 `run_session`，而自动那条
    入口每次收尾都会进来）。这道闸只读盘，不改纪要那一层任何文件、也不改状态文件的
    字段与取值域。

    `launcher` 是启动器的注入点（缺省 `None` = 走产品路径：按 `minutes.hook` 解析出来的
    实现）。`force = True` 时**跳过那道闸**（`--force` 的语义：无视终态，这一层重跑），
    `force = False` 时那道闸照旧。`deadline` 是拉起的上界，本函数不用它，只原样交给启动器
    （`launch()` 的既有签名只收场次目录时按位置给）。`status_reader` / `artifact_checker`
    是那道闸的两个接缝，缺省落在纪要那一层的实现上；`captured` 是给调用方留的一格记账
    （真把启动器调到时就地记下它交回的那个对象、给的是谁 —— 调用方据此在没有真纪要状态
    文件可读时也能出结论），不给就什么都不记。

    **再入闸那一档只挡产品路径**：`MEETING_MINUTES_CHILD=1`（这一趟是 DSH 子会话自己）
    时安静跳过 —— 但调用方**显式递了 `launcher`** 的那一档照常把它调到：那个假件就是
    「真拉起」这一步的替身，它自己什么都不起，用例靠它的调用记账核「这一趟有没有再花一次
    钱」。注入件因此连这道闸都不必摘（它本来也不该摘：那道闸的落点是产品路径）。

    返回值只进 `run_session()` 的摘要（`{"hook": …, "announced": …, "status": …}`），
    **不写进任何产物**：这一格的读者是调用方与用例，界面与下游读的是
    `meeting_minutes_status.json`（它归纪要那一层自己写）。
    """
    import os as _os

    if not announce_minutes:
        # **隔离闸**：非人工发起，一切之前安静跳过 —— 不解析钩子、不调启动器、盘上零写动作。
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
        # 调用方递了替身：这一档不解析 `minutes.hook` 的实现（替的就是它），直接记账。
        # 替身认 `force` / `deliver` 那两格时原样递下去（真实现都认：`MinutesHook.launch`），
        # 只收场次目录的老形状照旧按位置参调它。
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
        # **返回即算启动成功**（与收尾第 4 步同一条契约）：后台那条线程跑成什么样，
        # 由 `meeting_minutes_status.json` 回答。
        return {
            "hook": "launched",
            "announced": True,
            "selector": resolution.selector,
            "status": MINUTES_STATUS_NAME,
            "status_path": str(minutes_status_path),
        }
    except AlreadyRunningError as exc:
        # **这一场已经在飞了**：`launch()` 在建线程之前先读了盘上那一版状态
        #（`minutes.status.mark_running()` 用 claim 锁把「读现状 + 写 running」裹成一段
        # 跨进程原子操作），读到 `running` 就当场抛这一档 —— 这一趟没有起线程、没有拉起
        # 子会话。这里如实报成 `running`：`meeting/cli.py` 的 `_minutes_from_launcher()`
        # 据此出这一层拒跑的结论，摘要按现成的 `running` 拒跑档收场
        #（`EXIT_REJECTED` + 点名状态文件 + `--force` 那句话）。
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
    """把纪要那一层的实现叫起来（**带上这一趟的 `--force` 与人工放行两格**）。

    真实现（`meeting.backends.minutes:MinutesHook`）的 `launch()` 收 `force` 与 `deliver`：
    它在**建线程之前**先过那道隔离闸（`deliver=True` 才放行 —— 两条人工入口给的），再读盘上
    那一版状态，读到 `running` 就拒跑（这一趟于是没有第二个子会话），而带 `--force` 时放行
    —— 「无视现状重跑」那条既有语义因此能一路走到拉起这一步。

    收不收某一格由**实现自己**回答（`_accepts_keyword()` 看它的签名认不认这个关键字）：替身
    （以及将来别的实现）按老形状只收场次目录时，这一趟照旧按位置参调它 —— `--force` 在
    那一档由调用方（`meeting/cli.py` 的现状判定与 `_minutes_step_outcome()`）体现出来。
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
    """纪要**起跑之前**那道闸：已经跑完且产物过判据时交回 `already_done`，
    已经交付过而产物还没落时交回 `delivered`，其余交回 `None`（放行给启动器）。

    判据**以产物为准**（`meeting_final.md` 过得了现成的四条判据
    `minutes/launch.py::check_final_artifact()`），再按状态文件那一格分三种情形：

      1. 产物**过判据** → `already_done`：这一场真的完成了（终态是 `done` 还是 `delivered`
         都算 —— `delivered` 那一档说的是「已交付」，产物落了就说明 DSH 那边写完了）；
      2. 产物**还没落**、状态是 `delivered` → 交回 `delivered`：这一场**已经交付过** DSH web
         服务端，它可能正在写。交付那条路的语义是「投递即完事」，这一层不去猜它写没写完；
         但**再拉起第二个会话只会让同一场生成两遍**，因此这一档不重复拉起、如实报出去；
      3. 其余（产物没过判据、或停在 `running` / `failed` / 压根没有状态文件）→ `None`：
         放行给启动器，按「缺什么补什么」重新拉一次（`failed` 那一档尤其不能跳过：
         那一趟没交出纪要，得让它再试）。

    `force = True` 时**上面几条都不做**、直接交回 `None`（放行）：`--force` 的语义就是
    「无视这一层的终态、重跑」（`--force` 是本轮唯一会再次花钱的开关）。

    判据的实现**都借现成的**：状态文件走纪要那一层自己的读方
    `minutes.status.read_status()`（逐字段校验：`session_id` / `state` / `started_at` 缺一
    即拒收、`state` 必须在四档里、`artifacts` 必须是对象），产物校验调现成的
    `check_final_artifact()`（**两处都是函数体内 import**，本模块顶层的惰性纪律一个字不动：
    `import meeting.offline.runner` 仍然不拉 `meeting.minutes` 那一包）。
    本函数**只读盘**：不改纪要那一层任何文件，也不碰状态文件的字段与取值域。

    `status_reader` / `artifact_checker` 是两个接缝（缺省落在上面那两处实现上），
    用例靠它们把「闸开」与「闸关」两档都走一遍。
    """
    if force:
        _log.info("会后纪要被 --force 重跑：这一趟不看终态，直接拉起：%s", session_dir)
        return None

    # 纪要那一层的四档词表（函数体内 import：本模块顶层的惰性纪律一个字不动）
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
    """读纪要那一层的状态文件 —— **走那一层现成的读方**（`minutes.status.read_status()`）。

    本模块**不另写一份 `json.load` + 只认 `state` 一格**的读法：那份现成读方会逐字段校验
    （`schema_version` 认得、`session_id` / `state` / `started_at` 一个不缺、`state` 在三档里、
    `artifacts` 是对象），缺字段的残文件因此在这里就被拒收 —— 闸据此按「没跑完」放行、
    交给启动器重跑，而不是把一份 `{"state": "done"}` 当成「已跑完」。

    坏 JSON / 顶层不是对象 / 字段不合约时抛 `minutes.status.StatusError`（`ValueError` 子类）：
    调用方 `_minutes_gate()` 接住它按「没跑完」放行。**函数体内 import**：本模块顶层的惰性
    纪律一个字不动（`import meeting.offline.runner` 仍然不拉 `meeting.minutes` 那一包）。
    """
    from meeting.minutes.status import read_status

    return read_status(path)


def _final_artifact_check(session_dir: Path | str) -> tuple[str, ...]:
    """产物判据的现成实现（**函数体内 import**：本模块顶层不拉 `meeting.minutes` 那一包）。"""
    from meeting.minutes.launch import check_final_artifact

    return check_final_artifact(session_dir)

