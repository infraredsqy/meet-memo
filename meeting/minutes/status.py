"""纪要生成的状态文件（spec「交付物形状」里的 `meeting_minutes_status.json`）。

**它与 `meeting_offline_status.json` 是两份**，各说各的一层：那一份说「会后离线转写跑成什么样」，
这一份说「会后纪要生成跑成什么样」。两者互不覆盖（`offline` 的产物由它自己的写入方负责），
读方按文件名分辨。

形状照 `meeting/offline/artifacts.py` 的 `write_status()` / `read_status()` 那条路子定：

  - 顶层必填 `schema_version` / `session_id` / `state` / `mode` / `branch`；
  - 写盘**原子替换**（半份状态文件比晚一点落地坏得多：界面随时可能在读它）；
  - 读方对**不认识的 `schema_version`**、缺 `state`、`state` 不在四档里一律拒收，**不猜**。

四档状态与 `meeting/backends/post.py` 的 `STATE_RUNNING` / `STATE_DONE` / `STATE_FAILED`
**同名同义**：一份纪要的生成是一次「跑起来 → 跑完 / 跑砸」的活，与后端的记录对齐，
下游（`meeting post` 与界面）因此只有一套词要认。第四档 `delivered` 是 web 分支那一档：
**这一场已经交付给 DSH web 服务端去生成，后面的活不归这一层管**（用户停不停 DSH 是用户的事）
—— 它与 `done` 的区别正在这里：`done` 说的是「纪要认真校验过、产物在盘上」，`delivered`
说的是「投递成功了，产物还没（或者不会）经这一层校验」。读方按产物判「这一场要不要补跑」，
按 `delivered` / `running` 判「这一场是不是已经交付过、别再拉起第二个会话」。

`branch` 记的是**实际走的那一条**启动分支（不是配置里写的那个词）：走 headless 子进程时记
`headless`，经 DSH web 服务端交付时记 `web` —— 读状态文件的人据此知道这一趟到底是怎么
拉起来的。**在飞那一版**是个例外：那一刻还没探 DSH web
服务端在不在，因此那一份记的是**配置里写的那个词**（`launch.configured_branch()`），
由 `runner` 探完之后的下一版覆盖成实际的这一条。在飞那一版由 `meeting/minutes/claim.py`
落盘（同一场不挂两趟的 claim 锁也住在那边）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from meeting.minutes.inputs import INPUT_MODES, MODE_NONE, write_text_atomic

#: 状态文件的模式版本
SCHEMA_VERSION = 1

#: 本实现认识的主版本号（读方按它拒收）
KNOWN_SCHEMA_MAJOR = 1

#: 状态文件名（住在场次目录根，与离线那一份并列）
STATUS_NAME = "meeting_minutes_status.json"

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

#: 隔离闸的统一话术（三处同源：`MinutesHook.launch()` 的闸、`run_session()` 尾巴上的闸、
#: `_announce_minutes()` 的闸）。整个项目与 DSH 隔离：只有两条**人工**入口
#: （`meeting post` 与工作台「直接出纪要」按钮）显式放行，其余调用一律跳过。
ISOLATION_MESSAGE = "纪要层隔离：本次非人工发起，跳过"

#: 放行参数的下传说明（`DeliveryForbidden` 拒绝话术的括号补语，**一处定稿**）：
#: 放行参数叫 `allow_dsh=True`、由两条人工入口经 `MinutesHook.launch(deliver=True)` 下传。
#: headless 启动器、web 交付与编排层三处拒绝话术都接这一份，措辞不再各漂各的。
ISOLATION_GRANT = (
    "放行参数是 allow_dsh=True，由人工入口经 MinutesHook.launch(deliver=True) 下传"
)

#: `dsh.failure` 格里「超时」那一档的词：headless 与 web 两条分支的失败词落进
#: **同一张状态文件的同一格**，同名词必须同值，因此只此一份（两分支各自的
#: `FAILURES` 词表从这里取它）。
FAILURE_TIMEOUT = "timeout"

#: 启动分支：配置里的三个词（`auto` / `headless` / `web`）与实际走的两条
BRANCH_AUTO = "auto"
BRANCH_HEADLESS = "headless"
BRANCH_WEB = "web"
BRANCHES: tuple[str, ...] = (BRANCH_AUTO, BRANCH_HEADLESS, BRANCH_WEB)

#: 交付物文件名的规范常量（`launch.py` / `dsh_web.py` 从这里 import，名字不再各存一份）：
#: 工作目录与工作目录下那两个文件、以及一份纪要自己的 log 文件名
WORKING_DIR = "working"
RUN_PROMPT_NAME = "run_prompt.md"
MINUTES_RUN_LOG_NAME = "minutes-run.log"

#: 状态文件里 `artifacts` 的三格（相对场次目录的路径）——从上面的裸名派生，
#: 取值与从前逐字节相同（`ARTIFACT_FINAL` 在场次目录根，不带前缀）
ARTIFACT_FINAL = "meeting_final.md"
ARTIFACT_INPUTS = f"{WORKING_DIR}/inputs.json"
ARTIFACT_LOG = f"{WORKING_DIR}/{MINUTES_RUN_LOG_NAME}"


class StatusError(ValueError):
    """状态文件这一层自己的错误（写方拿到不合契约的形状 / 读方拒收一份文件）。

    继承 `ValueError`，与 `meeting` 里别的「值不对」错误同档。
    """


class DeliveryForbidden(RuntimeError):
    """**DSH 触达被拒**：这一趟没有带上人工放行标记，DSH 一步都不许碰。

    项目与 DSH 隔离：`dsh_web.probe()` / `dsh_web.handoff()` 与 headless 拉起
    （`launch.launch_headless()`）各带一道**默认拒绝**的放行参数（`allow_dsh=False`），
    不放行就当场抛本错误 —— **一次 TCP、一次 RPC、一个子进程都不发生**。放行参数由
    `MinutesHook.launch(deliver=True)` 起、经 `minutes.runner.run()` 逐层下传，只有
    `meeting post` 与「直接出纪要」按钮这两条**人工**入口会给真。未来任何代码绕过纪要层
    直接 import 这些函数，也在这一步被挡住（纵深防御的最后一道）。
    """


def now_iso() -> str:
    """现在的 ISO 时刻（与 `meeting/offline/runner` 编排主模块的 `_now_iso()` 同形）。"""
    return datetime.now().isoformat(timespec="seconds")


def finished_at_for(state: str) -> str:
    """状态文件 `finished_at` 那一格的取值（**唯一出处**）。

    `state = "running"`（在飞那一版）**明写成空串**——读方据此分得出「正在跑」；
    其余各档取当前时刻。`write_session_status()` 把 `None` 当「调用方没给」并补上
    当前时刻（那条口径给终局那几份用），所以在飞的这一版必须显式递空串而不能递
    `None`。`runner._status_document()` 与 `meeting/minutes/claim.py` 的在飞落盘
    都从这里取。
    """
    return "" if state == STATE_RUNNING else now_iso()


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
    `branch` 是**实际走的**启动分支；`dsh` 那一格记子进程的结果
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


def write_run_log(
    path: str | Path,
    *,
    header: Sequence[str],
    sections: Sequence[tuple[str, str]],
) -> None:
    """把这一趟的记录写进 `working/minutes-run.log`（**两路共用的唯一写盘实现**）。

    文件骨架取 web 交付那一路的（两套里信息更全的一套）：`# ` 头行若干——每一行
    「键：值」，给人排查用的现场事实，可选的行（工作区、失败原因）在场才落——
    然后一个空行，`## ` 分节若干（节名 + 节正文 + 空行），原子替换落盘。
    头行与分节的**内容**归各分支自己（headless 是命令 / cwd / 退出码与 stdout/stderr，
    web 是交付三步的请求与应答摘要，没走到的节写明占位），两路记录的事实本来就不同，
    一行都不丢；骨架与落盘只有这一份，读这份文件的人不再按 branch 猜两种布局。
    （放这里：这份产物名与 `artifacts.log` 那一格都登记在 status.py，写它的公共
    实现就近放同处，`launch` / `dsh_web` 两路都已在 import 它，不添新依赖边。）
    """
    lines = [*header, ""]
    for title, body in sections:
        lines.extend([f"## {title}", body, ""])
    write_text_atomic(path, "\n".join(lines))


def mark_failed(session_dir: str | Path, error: str) -> dict[str, Any]:
    """把纪要状态文件补成 `failed` + 原因；盘上那一版的身世能留的都留着。

    「读回盘上那一版 → 沿用 `mode` / `branch` / `started_at` / `artifacts` / `dsh` 五格
    （缺格按缺省补：`none` / `headless` / 现在 / 空 / 空）→ 覆盖 `state = "failed"` 与
    `error`」这段写法，从前在两个调用点逐字各抄一份（`meeting/backends/minutes.py` 的
    线程体兜底、`meeting/service/runtime.py` 的 claim 退回兜底支），现在只有这一份 ——
    claim 闸与启动核对读的都是这份文件，补记录口径因此只能有一处。

    读不动盘上那一版（文件不在场、不合契约）时按空记录补。这一层没有 offline 那份
    「已是终态记录就沿用」的判据（`meeting/offline/artifacts.py::mark_failed_if_unset`）：
    纪要这一层的补记录只发生在「这一趟整个炸出来」的现场，`state` 如实落 `failed`。
    写盘走 `write_session_status()`（原子替换、过读方契约）；写不下去时异常原样抛给
    调用方，由调用方记 log（不掩盖各自那条更早的异常）。返回写下去的那份状态。
    """
    session = Path(session_dir)
    existing: dict[str, Any] = {}
    path = status_path(session)
    if path.exists():
        try:
            existing = read_status(path)
        except StatusError:
            existing = {}
    return write_session_status(
        session,
        state=STATE_FAILED,
        mode=str(existing.get("mode") or "none"),
        branch=str(existing.get("branch") or "headless"),
        started_at=str(existing.get("started_at") or now_iso()),
        error=str(error),
        artifacts=dict(existing.get("artifacts") or {}),
        dsh=dict(existing.get("dsh") or {}),
    )


__all__ = [
    "ARTIFACT_FINAL",
    "ARTIFACT_INPUTS",
    "ARTIFACT_LOG",
    "BRANCHES",
    "BRANCH_AUTO",
    "BRANCH_HEADLESS",
    "BRANCH_WEB",
    "DeliveryForbidden",
    "FAILURE_TIMEOUT",
    "ISOLATION_GRANT",
    "ISOLATION_MESSAGE",
    "KNOWN_SCHEMA_MAJOR",
    "MINUTES_RUN_LOG_NAME",
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
    "finished_at_for",
    "mark_failed",
    "now_iso",
    "parse_status_document",
    "read_status",
    "RUN_PROMPT_NAME",
    "status_path",
    "write_run_log",
    "write_session_status",
    "write_status",
    "WORKING_DIR",
]
