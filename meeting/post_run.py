"""`meeting post` 的会后编排：`run_post()` 与它的辅助函数族。

编排只有 `run_post()` 这一条入口：`meeting/cli.py` 的 `post()` 与收尾第 4 步共用；
收尾第 4 步不拉起任何会后动作，「生成会后精细稿」按钮走 `run_session` 直路。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from meeting import config as config_module
from meeting import logging_setup
from meeting.capture import transcode as transcode_module
from meeting.offline import minutes_tail
from meeting.offline import progress as offline_progress
from meeting.offline import runner as offline_runner
from meeting.offline.runner import session_info as offline_session_info
from meeting.offline.artifacts import (
    REPORT_NAME,
    SPEAKERS_NAME_TEMPLATE,
    TRANSCRIPT_NAME_TEMPLATE,
)
from meeting.session import (
    archive_name,
    segment_archive_paths,
)


ARCHIVE_DONE = "done"
ARCHIVE_SKIPPED = "skipped"
ARCHIVE_FAILED = "failed"

#: 归档那一层每个源的三档结论（「转出了」/「没什么要转的」/「转砸了」）
OUTCOME_CONVERTED = "converted"
OUTCOME_NO_WORK = "no_work"
OUTCOME_FAILED = "failed"

LAYER_DONE = "done"
LAYER_ABSENT = "absent"
LAYER_FAILED = "failed"

#: 离线那一层**一路整条没交回、另一路跑成**那一档的结论词：它是这一层的终态（产物可用），
#: 与 `done` 分开写，好让读方在一格上就看出「有一路整条没有」（`artifacts.STATES` 同值）。
LAYER_PARTIAL = "partial"

#: 离线那一层的**被打断**档（「打断会后」节点，`artifacts.STATES` 同值）：
#: 跑到块边界被人停的**终态**。补记录判据把它算进「已落定」（打断之后逃出来的异常不许把
#: 它盖成 `failed`），幂等闸**不**把它当 `running` —— 打断之后重按按钮 / 再敲
#: `meeting post` 从头再跑，不需要 `--force`。
LAYER_INTERRUPTED = "interrupted"

#: 两层共用的「上一趟还在跑 / 崩在半路」那一档（`meeting post` 默认拒跑的那一档）
LAYER_RUNNING = "running"

#: 纪要那一层独有的**第四档**：已交付 DSH web 服务端生成、产物还没落。读方按它判
#: 「这一场已经交付过、别再拉起第二个会话」，按产物判「完成没有」。
LAYER_DELIVERED = "delivered"

MINUTES_FINAL_NAME = "meeting_final.md"

#: 等纪要那一层终态的上界（秒）的兜底档：**只在连配置表都读不到时**用它，
#: 正常路径读的是现成的纪要墙钟键 `minutes.timeout_seconds`。
MINUTES_TIMEOUT_FALLBACK_SECONDS = 14400.0

#: 等纪要时每一次读盘之间那一小会儿：编排函数手里没有可 join 的句柄（终态由后台线程或
#: headless 子进程写进状态文件），盘上那份状态文件就是唯一的完成通知。
MINUTES_POLL_SECONDS = 0.05

POST_STEPS: tuple[tuple[str, str], ...] = (
    ("archive", "音频归档补齐（PCM -> MP3）"),
    ("offline", "会后离线（离线转写与说话人聚类）"),
    ("minutes", "会后纪要（纪要生成）"),
)


def _log() -> logging.Logger:
    return logging_setup.get_logger("cli")


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
    # 「这一趟没有待办」的前置是「音频已归档」：归档有一路转不动
    # 时不许走这一档——既不能报 `EXIT_OK`，也不能打一句「音频已归档」。
    if offline_done and minutes_done and result["archive"]["state"] != ARCHIVE_FAILED:
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
        # 逐块进度记录在这一条入口上也落（落盘跟着链走、不挑是哪个按钮拉起的）；
        # 调用方注入的那一份优先。
        if progress is None:
            # 场次元信息（编号与路清单）从它的家现取（`runner/session_info.py`），
            # 逐块进度记录自己不再反向问编排器。
            progress = offline_progress.OfflineProgressLedger(
                directory,
                session_id=offline_session_info.session_id_of(directory),
                sources=offline_session_info.declared_sources(directory),
            )
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
            outcome = minutes_tail._announce_minutes(
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
        announce = minutes_tail._announce_minutes(
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
        "status_path": str(directory / minutes_tail.MINUTES_STATUS_NAME),
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
        "status_path": str(directory / minutes_tail.MINUTES_STATUS_NAME),
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
        f"{minutes_tail.MINUTES_STATUS_NAME} 停在 {LAYER_DELIVERED}：这一场已经交付 "
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
        "status_path": str(directory / minutes_tail.MINUTES_STATUS_NAME),
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
    return str(status.get("state") or "") == LAYER_DELIVERED


def _missing_names(directory: Path, names: Iterable[str]) -> list[str]:
    """交回 `names` 里**不在盘上**的那些（判据缺一不可的那一半）。"""
    return [str(name) for name in names if not (directory / str(name)).is_file()]


def _offline_artifacts_complete(directory: Path, status: dict[str, Any]) -> bool:
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
    path = directory / minutes_tail.MINUTES_STATUS_NAME
    if not path.is_file():
        return {"status_name": minutes_tail.MINUTES_STATUS_NAME}
    try:
        from meeting.minutes.status import read_status

        document = read_status(path)
    except (ImportError, OSError, ValueError) as exc:
        return {
            "status_name": minutes_tail.MINUTES_STATUS_NAME,
            "unreadable": f"{type(exc).__name__}: {exc}",
        }
    document["status_name"] = minutes_tail.MINUTES_STATUS_NAME
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
    status_path = directory / minutes_tail.MINUTES_STATUS_NAME
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
    status_path = directory / minutes_tail.MINUTES_STATUS_NAME
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
    原子替换，读方偶尔撞上换文件那一瞬间；真是残文件时到上界如实报 `timeout`。
    上一趟留在盘上的终态标记与基线相同，不算落定。
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
    status_path = directory / minutes_tail.MINUTES_STATUS_NAME
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
    from meeting.cli import EXIT_FAILURE, EXIT_OK

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
        (minutes_tail.MINUTES_STATUS_NAME, "会后纪要状态文件"),
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
    # 退出码五档与翻名表冻结在 `meeting/cli.py`：函数体内 import——cli 的模块级
    # import 指到本模块，两边在 import 期互相等会转圈。
    from meeting.cli import EXIT_FAILURE, EXIT_OK, EXIT_REJECTED, _exit_name

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


__all__ = [
    "ARCHIVE_DONE",
    "ARCHIVE_FAILED",
    "ARCHIVE_SKIPPED",
    "MINUTES_FINAL_NAME",
    "MINUTES_POLL_SECONDS",
    "MINUTES_TIMEOUT_FALLBACK_SECONDS",
    "OUTCOME_CONVERTED",
    "OUTCOME_FAILED",
    "OUTCOME_NO_WORK",
    "POST_STEPS",
    "run_post",
    "sync_archive",
]
