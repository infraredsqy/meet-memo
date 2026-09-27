"""纪要生成的状态文件（spec「交付物形状」里的 `meeting_minutes_status.json`）。

**它与 `meeting_offline_status.json` 是两份**，各说各的一层：那一份说「会后离线转写跑成什么样」，
这一份说「会后纪要生成跑成什么样」。两者互不覆盖（`offline` 的产物由它自己的写入方负责），
读方按文件名分辨。

形状照 `meeting/offline/artifacts.py` 的 `write_status()` / `read_status()` 那条路子定：

  - 顶层必填 `schema_version` / `session_id` / `state` / `mode` / `branch`；
  - 写盘**原子替换**（半份状态文件比晚一点落地坏得多：界面随时可能在读它）；
  - 读方对**不认识的 `schema_version`**、缺 `state`、`state` 不在四档里一律拒收，**不猜**。

四档状态与 `meeting/backends/post.py` 的 `STATE_RUNNING` / `STATE_DONE` / `STATE_FAILED`
**同名同义**：一份纪要的生成是一次「跑起来 → 跑完 / 跑砸」的活，与后端的账目对齐，
下游（`meeting post` 与界面）因此只有一套词要认。第四档 `delivered` 是 web 分支那一档：
**这一场已经交付给 DSH web 服务端去生成，后面的活不归这一层管**（用户停不停 DSH 是用户的事）
—— 它与 `done` 的区别正在这里：`done` 说的是「纪要认真校验过、产物在盘上」，`delivered`
说的是「投递成功了，产物还没（或者不会）经这一层校验」。读方按产物判「这一场要不要补跑」，
按 `delivered` / `running` 判「这一场是不是已经交付过、别再拉起第二个会话」。

`branch` 记的是**实际走的那一条**启动分支（不是配置里写的那个词）：走 headless 子进程时记
`headless`，经 DSH web 服务端交付时记 `web` —— 读状态文件的人据此知道这一趟到底是怎么
拉起来的。**在飞那一版**（`mark_running()` 落的 `running`）是个例外：那一刻还没探 DSH web
服务端在不在，因此那一份记的是**配置里写的那个词**（`launch.configured_branch()`），
由 `runner` 探完之后的下一版覆盖成实际的这一条。
"""

from __future__ import annotations

import json
import logging
import os
import time as _time
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from meeting.minutes.inputs import INPUT_MODES, MODE_NONE, write_text_atomic

_log = logging.getLogger("meeting.minutes.status")

#: 状态文件的模式版本
SCHEMA_VERSION = 1

#: 本实现认识的主版本号（读方按它拒收）
KNOWN_SCHEMA_MAJOR = 1

#: 状态文件名（住在场次目录根，与离线那一份并列）
STATUS_NAME = "meeting_minutes_status.json"

#: 「在飞」那一版落盘时的**claim 锁**名（住在场次目录根，与状态文件并列）。
#:
#: 它保证「读一遍现状 + 写一版 `running`」这件事**跨进程原子**：两个 `meeting post`
#: 前后脚进来时，只有一个拿得到它，另一个在锁在场期间读到的就是那一版 `running`。
#: 它是**瞬态**文件（写完那一版 `running` 就删掉），产物清单里没有它的名字：进程被杀
#: 也只是把它留在盘上，下一趟按下面那段有界等待兜住 —— 锁在场而状态已经离开 `running`
#: 时，说明那一趟已经收工或压根没起来，接着拿锁就行）。
LOCK_NAME = "meeting_minutes_status.lock"

#: 拿 claim 锁失败时最多再试几次（每次之间让出 `LOCK_WAIT_SECONDS`）
LOCK_ATTEMPTS = 3

#: 拿 claim 锁失败时每次让出的时长（秒）：只为盖住「锁刚建出来、那一版 `running` 还没写完」
#: 那个**微秒级**的缝，不是用来等一整条纪要链的。
LOCK_WAIT_SECONDS = 0.05

#: 拉起之前那一版 `running` 里 `mode` 的取值（输入档位此刻还不知道，用现成的 `none` 档）。
#:
#: 状态文件里 `mode` 那一格由 `build_status()` 收下（`runner` 随后那一版会按真实输入覆盖它），
#: 读方（`parse_status_document()`）判的是四格必填与四档取值，因此这一版照样读得回来。
MODE_UNKNOWN = MODE_NONE

#: 状态四档（前三档与 `meeting/backends/post.py` 的三个常量同名同义）
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
#: 第四档：**已交付 DSH web 服务端生成**（web 分支的终态）。
#:
#: 「投递 accepted 即算交付完成」是这一档的语义：这一层不等 DSH 那边把纪要写完，
#: 因此**产物校验不在这条路上**。读方按产物判「要不要补跑」，按这一档判「已经交付过、
#: 别再拉起第二个会话」（`meeting post` 的重复拉起闸）。
STATE_DELIVERED = "delivered"
STATES: tuple[str, ...] = (STATE_RUNNING, STATE_DONE, STATE_FAILED, STATE_DELIVERED)

#: 再入闸那一档：子会话（`MEETING_MINUTES_CHILD=1`）里不再拉起 DSH，这一趟记 `skipped`
#: （**不在四档里**：它既没有跑起来，也不算失败）。
#:
#: 这一档**只出现在返回值里**：子会话不去覆盖父进程那一趟的状态文件，因此
#: `write_status()` / `read_status()` 仍然只认上面那四档。
STATE_SKIPPED = "skipped"

#: 启动分支：配置里的三个词（`auto` / `headless` / `web`）与实际走的两条
BRANCH_AUTO = "auto"
BRANCH_HEADLESS = "headless"
BRANCH_WEB = "web"
BRANCHES: tuple[str, ...] = (BRANCH_AUTO, BRANCH_HEADLESS, BRANCH_WEB)

#: 状态文件里 `artifacts` 的三格（相对场次目录的路径）
ARTIFACT_FINAL = "meeting_final.md"
ARTIFACT_INPUTS = "working/inputs.json"
ARTIFACT_LOG = "working/minutes-run.log"


class StatusError(ValueError):
    """状态文件这一层自己的错误（写方拿到不合契约的形状 / 读方拒收一份文件）。

    继承 `ValueError`，与 `meeting` 里别的「值不对」错误同档。
    """


class AlreadyRunningError(RuntimeError):
    """**纪要这一层已经在飞了**：状态文件停在 `running`，这一趟拒绝再拉起一个。

    它是「同一个场次目录上再拉一次子会话」这条路的**终点**：抛出它的那一趟没有起线程、
    没有拉起任何子会话，调用方据此把这一趟按 `running` 拒跑档收场（`--force` 是出路）。
    继承 `RuntimeError`：这一档说的是运行期状态冲突（同一个目录上已经有一趟在跑），
    与 `StatusError` 那类「文件内容不合契约」的值错误各说各的一层。
    """


def now_iso() -> str:
    """现在的 ISO 时刻（与 `meeting/offline/runner.py` 的 `_now_iso()` 同形）。"""
    return datetime.now().isoformat(timespec="seconds")


def build_status(
    *,
    session_id: str,
    state: str,
    mode: str,
    branch: str,
    started_at: str,
    finished_at: str | None = None,
    error: str | None = None,
    artifacts: Mapping[str, Any] | None = None,
    dsh: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """组一份状态（**纯函数**，不碰盘）：写出去与读回来都以它为准。

    `mode` 是这一趟的输入档位（`both` / `live_only` / `offline_only` / `none`），
    `branch` 是**实际走的**启动分支；`dsh` 那一格记子进程的账
    （`executable` / `exit_code` / `seconds`），没拉起子进程时是空对象。

    `state` 收四档之外还收 `skipped`：再入闸那一趟（子会话里）**只在返回值里**记这一档，
    它不落盘，因此读方那四档一个字不动。
    """
    if state not in (*STATES, STATE_SKIPPED):
        raise StatusError(f"状态只认 {(*STATES, STATE_SKIPPED)}，收到 {state!r}")
    if mode not in INPUT_MODES:
        raise StatusError(f"档位只认 {INPUT_MODES}，收到 {mode!r}")
    if branch not in BRANCHES:
        raise StatusError(f"启动分支只认 {BRANCHES}，收到 {branch!r}")
    if not str(session_id).strip():
        raise StatusError(f"session_id 不该是空的，收到 {session_id!r}")
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": str(session_id),
        "state": state,
        "mode": mode,
        "branch": branch,
        "started_at": str(started_at),
        "finished_at": None if finished_at is None else str(finished_at),
        "error": None if error is None else str(error),
        "artifacts": dict(artifacts or {}),
        "dsh": dict(dsh or {}),
    }


def write_status(path: str | Path, document: Mapping[str, Any]) -> Path:
    """落一份状态文件（原子替换）；写之前先过一遍读方契约（写出去的必须读得回来）。"""
    payload = dict(document)
    parse_status_document(payload)
    return write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_status(path: str | Path) -> dict[str, Any]:
    """读回一份状态文件（文件不在、JSON 坏掉、字段不合约一律抛 `StatusError`）。"""
    target = Path(path)
    if not target.exists():
        raise StatusError(f"状态文件不在：{target}")
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise StatusError(f"状态文件读不动：{target}（{type(exc).__name__}: {exc}）") from exc
    return parse_status_document(document)


def parse_status_document(document: Any) -> dict[str, Any]:
    """校验一份状态（**读方拒收不认识的版本与缺字段的文件，不猜**）。"""
    if not isinstance(document, Mapping):
        raise StatusError(f"状态文件顶层该是对象，收到 {type(document).__name__}")
    version = document.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise StatusError(f"状态文件的 schema_version 要是整数，收到 {version!r}")
    if version != KNOWN_SCHEMA_MAJOR:
        raise StatusError(
            f"状态文件的主版本号不认识：{version}（本实现只认 {KNOWN_SCHEMA_MAJOR}）"
        )
    for key in ("session_id", "state", "started_at"):
        if not document.get(key):
            raise StatusError(f"状态文件缺 {key}")
    if document["state"] not in STATES:
        raise StatusError(f"状态只认 {STATES}，收到 {document['state']!r}")
    if not isinstance(document.get("artifacts"), Mapping):
        raise StatusError("状态文件的 artifacts 该是对象")
    return dict(document)


def status_path(session_dir: str | Path) -> Path:
    """状态文件的落点（场次目录根）。"""
    return Path(session_dir) / STATUS_NAME


def write_session_status(
    session_dir: str | Path,
    *,
    state: str,
    mode: str,
    branch: str,
    started_at: str,
    finished_at: str | None = None,
    error: str | None = None,
    artifacts: Mapping[str, Any] | None = None,
    dsh: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """按场次目录落一份状态文件，返回那份状态（`runner` 的每一步都走它）。"""
    session = Path(session_dir)
    document = build_status(
        session_id=session.name,
        state=state,
        mode=mode,
        branch=branch,
        started_at=started_at,
        finished_at=finished_at if finished_at is not None else now_iso(),
        error=error,
        artifacts=artifacts,
        dsh=dsh,
    )
    write_status(status_path(session), document)
    return document


def lock_path(session_dir: str | Path) -> Path:
    """claim 锁的落点（场次目录根，与状态文件并列）。"""
    return Path(session_dir) / LOCK_NAME


def mark_running(
    session_dir: str | Path,
    *,
    branch: str = BRANCH_HEADLESS,
    started_at: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """**把「这一层在飞」写到盘上**，返回那一版状态；已经在飞时抛 `AlreadyRunningError`。

    这是纪要那一层「正在跑」这件事的**唯一持久落点**，供拉起方（
    `meeting/backends/minutes.py::MinutesHook.launch()`）在**建线程之前**同步调用一次：
    调用返回时，盘上那份 `meeting_minutes_status.json` 已经读得出 `state = "running"`，
    于是另一个进程（用户随后手敲的 `meeting post`、或开会结束时自动跑的那一趟）当场
    就能看见「上一趟还在跑」，按现成的 `running` 拒跑档收场 —— 拉起与拒跑因此落在同一份
    账上，谁也漏不掉。

    **读一遍现状 + 写一版 `running` 这件事跨进程原子**：整段由 `LOCK_NAME` 那把
    `O_CREAT | O_EXCL` 的 claim 锁裹住，同一时刻只有一个进程进得来。拿不到锁时先让出
    `LOCK_WAIT_SECONDS` 再读一遍状态文件（那一版 `running` 就在锁里写，最多几十微秒的缝）：

      - 读到的状态停在 `running` —— 另一趟正在飞，抛 `AlreadyRunningError`（**不覆盖它的账**）；
      - 读到别的取值（上一趟的 `done` / `failed`、或者压根没有这份文件）—— 上一趟已经收工，
        接着拿锁就行（进程崩在半路时锁会留在盘上，这一段有界等待正是为它准备的）。

    `force = True` 时**跳过上面那条在飞判据**：`--force` 的语义就是「无视这一层的现状、
    重跑」（它是本轮唯一会再次花钱的开关），于是同一条在飞判据在 `force` 那一档照常放行，
    这一趟把那一版 `running` 原子覆盖成自己的（`finished_at` 仍是空串，读方看到的还是
    「正在跑」，只是账换成了这一趟的）。

    写出去的形状走现成的 `build_status()` / `write_status()`：四档取值域、四个必填字段、
    原子替换一个字都不动，这一版与 `runner` 线程体随后落的那一版**同形**，因此线程体那一版
    是原子覆盖（幂等：谁来写都读到同一份形态）。`finished_at` 明写成空串（`runner.py` 里
    「先落 `running` 再拉起 DSH」那一段同一条口径），读方据此分得出「正在跑」。
    """
    session = Path(session_dir)
    session.mkdir(parents=True, exist_ok=True)
    moment = now_iso() if started_at is None else str(started_at)
    lock = lock_path(session)
    attempts = max(1, int(LOCK_ATTEMPTS))
    skip_check = bool(force)

    for attempt in range(attempts):
        if _claim_lock(lock):
            try:
                if not skip_check and _peek_state(session) == STATE_RUNNING:
                    raise AlreadyRunningError(_already_running_reason(session))
                return _write_in_flight(session, moment=moment, branch=branch)
            finally:
                with suppress(OSError):
                    lock.unlink(missing_ok=True)
        if not skip_check and _peek_state(session) == STATE_RUNNING:
            raise AlreadyRunningError(_already_running_reason(session))
        if attempt + 1 < attempts:
            _time.sleep(LOCK_WAIT_SECONDS)

    # 有界等待走完、锁仍在盘上（上一趟崩在半路留下的残锁），而状态文件此刻读不到「在飞」：
    # 接着往下走，由调用方真的把这一趟拉起来 —— 「上一趟崩了」那条出路必须留着。
    _log.warning(
        "claim 锁留在盘上（上一趟可能崩在半路），状态文件读不到在飞，这一趟照常拉起：%s", lock
    )
    return _write_in_flight(session, moment=moment, branch=branch)


def _already_running_reason(session: Path) -> str:
    """拒跑那一档的理由（点名状态文件与它的落点）。"""
    return (
        f"纪要这一层已经在飞（{STATUS_NAME} 停在 {STATE_RUNNING}）："
        f"这一趟不再拉起子会话：{session}"
    )


def _write_in_flight(session: Path, *, moment: str, branch: str) -> dict[str, Any]:
    """落那一版「在飞」的状态文件（形状与 `runner` 线程体那一版同形，取值为空的那几格）。"""
    return write_session_status(
        session,
        state=STATE_RUNNING,
        mode=MODE_UNKNOWN,
        branch=str(branch),
        started_at=moment,
        finished_at="",
        artifacts={},
        dsh={},
    )


def _claim_lock(lock: Path) -> bool:
    """试着把 claim 锁建出来（`O_CREAT | O_EXCL`：跨进程原子，成没成只有一个是真）。"""
    try:
        handle = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with suppress(OSError):
        os.write(handle, f"{os.getpid()} {now_iso()}\n".encode("utf-8"))
    with suppress(OSError):
        os.close(handle)
    return True


def _peek_state(session: Path) -> str | None:
    """读一眼盘上那一版的 `state`；文件不在场 / 读不动时交回 `None`（按「还没起来」算）。"""
    try:
        return str(read_status(status_path(session)).get("state") or "")
    except (OSError, StatusError, ValueError):
        return None


__all__ = [
    "ARTIFACT_FINAL",
    "ARTIFACT_INPUTS",
    "ARTIFACT_LOG",
    "AlreadyRunningError",
    "BRANCHES",
    "BRANCH_AUTO",
    "BRANCH_HEADLESS",
    "BRANCH_WEB",
    "KNOWN_SCHEMA_MAJOR",
    "LOCK_ATTEMPTS",
    "LOCK_NAME",
    "LOCK_WAIT_SECONDS",
    "MODE_UNKNOWN",
    "SCHEMA_VERSION",
    "STATES",
    "STATUS_NAME",
    "STATE_DELIVERED",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_RUNNING",
    "STATE_SKIPPED",
    "StatusError",
    "build_status",
    "lock_path",
    "mark_running",
    "now_iso",
    "parse_status_document",
    "read_status",
    "status_path",
    "write_session_status",
    "write_status",
]
