"""纪要生成的状态文件（spec「交付物形状」里的 `meeting_minutes_status.json`）。

**它与 `meeting_offline_status.json` 是两份**，各说各的一层：那一份说「会后离线转写跑成什么样」，
这一份说「会后纪要生成跑成什么样」。两者互不覆盖（`offline` 的产物由它自己的写入方负责），
读方按文件名分辨。

形状照 `meeting/offline/artifacts.py` 的 `write_status()` / `read_status()` 那条路子定：

  - 顶层必填 `schema_version` / `session_id` / `state` / `mode` / `branch`；
  - 写盘**原子替换**（半份状态文件比晚一点落地坏得多：界面随时可能在读它）；
  - 读方对**不认识的 `schema_version`**、缺 `state`、`state` 不在三档里一律拒收，**不猜**。

三档状态与 `meeting/backends/post.py` 的 `STATE_RUNNING` / `STATE_DONE` / `STATE_FAILED`
**同名同义**：一份纪要的生成是一次「跑起来 → 跑完 / 跑砸」的活，与后端的账目对齐，
下游（`meeting post` 与界面）因此只有一套词要认。

`branch` 记的是**实际走的那一条**启动分支（不是配置里写的那个词）：`auto` / `web` 本轮都落
headless 子进程，配置里写 `web` 而实际走了 headless 时，这一格记 `headless` —— 读状态文件的人
据此知道这一趟到底是怎么拉起来的。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from meeting.minutes.inputs import INPUT_MODES, write_text_atomic

#: 状态文件的模式版本
SCHEMA_VERSION = 1

#: 本实现认识的主版本号（读方按它拒收）
KNOWN_SCHEMA_MAJOR = 1

#: 状态文件名（住在场次目录根，与离线那一份并列）
STATUS_NAME = "meeting_minutes_status.json"

#: 状态只有三档（与 `meeting/backends/post.py` 的三个常量同名同义）
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATES: tuple[str, ...] = (STATE_RUNNING, STATE_DONE, STATE_FAILED)

#: 再入闸那一档：子会话（`MEETING_MINUTES_CHILD=1`）里不再拉起 DSH，这一趟记 `skipped`
#: （**不在三档里**：它既没有跑起来，也不算失败）。
#:
#: 这一档**只出现在返回值里**：子会话不去覆盖父进程那一趟的状态文件，因此
#: `write_status()` / `read_status()` 仍然只认上面那三档。
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

    `state` 收三档之外还收 `skipped`：再入闸那一趟（子会话里）**只在返回值里**记这一档，
    它不落盘，因此读方那三档一个字不动。
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


__all__ = [
    "ARTIFACT_FINAL",
    "ARTIFACT_INPUTS",
    "ARTIFACT_LOG",
    "BRANCHES",
    "BRANCH_AUTO",
    "BRANCH_HEADLESS",
    "BRANCH_WEB",
    "KNOWN_SCHEMA_MAJOR",
    "SCHEMA_VERSION",
    "STATES",
    "STATUS_NAME",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_RUNNING",
    "STATE_SKIPPED",
    "StatusError",
    "build_status",
    "now_iso",
    "parse_status_document",
    "read_status",
    "status_path",
    "write_session_status",
    "write_status",
]
